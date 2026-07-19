# Built-in Storage Maintenance Design

## Goal

Add a bounded, in-process storage maintenance service to QQ Agent Bridge so long-running deployments do not exhaust the host disk. The service manages only application-owned data and coordinates with Agent activity before deleting mutable files.

Managed areas:

1. The configured Agent sandbox home.
2. Agent trace files.
3. Received and generated QQ resources below the configured resource root.

The feature does not depend on cron, systemd timers, or deployment-specific shell scripts.

## Non-goals

- Do not clean arbitrary files in `/tmp`, the user's real home, or any part of the Agent workspace outside the configured resource root.
- Do not follow symbolic links or cross a configured root boundary.
- Do not archive files or upload them elsewhere.
- Do not expose a new QQ command in this change.
- Do not terminate active jobs to reclaim space.
- Do not treat the maintenance service as a general-purpose disk cleaner.

## Configuration

Add a top-level `storage_maintenance` block:

```yaml
storage_maintenance:
  enabled: true
  interval_seconds: 21600       # 6 hours
  min_free_bytes: 5368709120    # 5 GiB

  sandbox:
    max_bytes: 2147483648       # 2 GiB
    retention_seconds: 1209600  # 14 days

  traces:
    max_bytes: 536870912        # 512 MiB
    retention_seconds: 1209600  # 14 days

  resources:
    max_bytes: 5368709120              # 5 GiB
    retention_seconds: 604800           # 7 days
    transient_retention_seconds: 86400  # 24 hours
```

Configuration loading clamps `interval_seconds` to 60 seconds through 7 days, byte limits to 0 through 1 TiB, and retention periods to 0 through 365 days. A zero capacity disables capacity-based deletion for that area; a zero retention period disables age-based deletion for that area. `enabled: false` disables all automatic maintenance.

The user's local configuration and `config.example.yaml` enable the balanced defaults above. Reloading configuration updates limits and intervals for subsequent maintenance runs without restarting OneBot. Changing the actual managed roots still follows the existing restart requirements for Agent and resource configuration.

## Architecture

### StorageMaintainer

Create a focused `StorageMaintainer` module responsible for:

- resolving and validating managed roots;
- scanning bounded candidate sets without following links;
- applying age and capacity policies;
- checking host free space;
- coordinating startup, periodic, and pressure-triggered runs;
- returning a structured summary rather than sending QQ messages.

The maintainer has no dependency on OneBot message contents. It receives configuration and an activity coordinator from `App`.

### StorageActivityGate

Cleanup and Agent work must not mutate shared state concurrently. A `StorageActivityGate` provides two asynchronous leases:

- **activity lease:** used by Agent work, proactive model calls, schedule parsing, and resource preparation;
- **maintenance lease:** prevents new activity leases and waits for existing leases to finish before cleanup starts.

Activity leases are re-entrant within the same asyncio task so an App job can cover resource preparation and invoke a wrapped Agent adapter without deadlocking. Cancellation releases leases in `finally` blocks.

Maintenance never cancels jobs. New work may wait briefly while a bounded maintenance run holds the exclusive lease.

### Lifecycle integration

`App` owns one maintainer and one background maintenance task.

1. Run startup maintenance before accepting OneBot events or starting scheduled work.
2. Start a periodic loop after the App is ready.
3. At job completion, perform only a cheap free-space check. If pressure exists, coalesce a maintenance request instead of scanning inline.
4. On shutdown, cancel and await the periodic task without delaying Agent process cleanup.
5. On reload, update the maintainer policy and wake the loop if the interval changed.

Only one maintenance run may execute at a time. Repeated pressure signals collapse into one pending run.

## Root validation

Every managed root is resolved from existing configuration and validated before scanning:

- sandbox home must pass the same dedicated application-state-root policy as the Agent adapter;
- trace root must remain outside the Agent workspace;
- resource root must remain inside the configured Agent workspace;
- the root and every traversed component must be real directories, not symbolic links;
- deletion candidates must remain descendants of their validated root;
- entries are inspected with `lstat`/`os.scandir` and never followed through links;
- unknown top-level entries are counted but not deleted.

A validation failure skips that area and records a class-only warning. It does not widen access or fail bridge startup.

## Cleanup policies

### Sandbox home

Protected files:

- `.config/cursor/auth.json`
- `.cursor/cli-config.json`
- `.cursor/agent-cli-state.json`
- current workspace trust markers

Normal cleanup order:

1. Remove known disposable cache contents under `.cache`.
2. Remove `.npm/_cacache`, `.npm/_logs`, and stale `.npm/_npx` entries.
3. Remove Cursor chat directories older than the retention period.
4. Remove old worker logs and stale project assets.
5. If the sandbox still exceeds its cap, delete the oldest eligible chats and caches until it is at or below the cap.

The bridge's QQ conversation memory is independent of Cursor chat databases. Removing completed Cursor chats does not clear QQ memory.

Pressure cleanup uses the same candidate allowlist more aggressively. It never deletes protected authentication/configuration files. Unknown `.local` contents are reported but not removed automatically; tasks must continue to use `micromamba base`, and unexpected user-local installs remain visible for operator review.

### Agent traces

Only regular `*.jsonl` files directly owned by the trace subsystem are eligible.

1. Delete files older than 14 days.
2. Sort remaining files by modification time, oldest first.
3. Delete until total eligible size is at or below 512 MiB.

The maintenance lease ensures no active trace writer is open during deletion.

### QQ resources

The configured resource root is divided into known classes:

- dated received-resource directories: retain for 7 days;
- `outgoing`: retain completed job directories for 24 hours;
- `sending`: retain staged delivery directories for 24 hours;
- generated runtime skill bundle: preserve and allow its existing preparation logic to refresh it.

After age-based cleanup, eligible resource directories are ordered by modification time and removed oldest first until the managed resource total is at or below 5 GiB. Current job directories and unknown top-level entries are never candidates.

Directory removal is bottom-up, does not follow symlinks, and tolerates files disappearing concurrently.

## Disk-pressure behavior

Free space is checked using the filesystem containing each managed root. A pressure run is requested when any relevant filesystem has less than 5 GiB available.

Pressure cleanup follows this order:

1. disposable sandbox caches;
2. old Cursor chats and logs;
3. old Agent traces;
4. transient outgoing/sending resources;
5. old received resources.

After cleanup, free space is checked again. If it remains below the threshold, the bridge logs at most one warning per hour. This design does not reject new work solely because the threshold remains low; the existing explicit `ENOSPC` error handling remains the final fail-safe.

## Bounded work

Maintenance must not monopolize the event loop or scan unbounded trees.

- Filesystem scanning and deletion run in `asyncio.to_thread` while the maintenance lease blocks Agent activity.
- Each area scans at most 100,000 candidate entries per run.
- Candidate metadata is collected once per run and reused for age/capacity decisions.
- A run has a 30-second wall-clock budget; reaching it stops safely and continues on the next cycle.
- Errors for one candidate or area do not stop other areas.

## Observability and privacy

Each run emits one start log and one summary log containing:

- trigger: startup, periodic, or pressure;
- elapsed time;
- scanned candidate count;
- removed file/directory count;
- released bytes;
- skipped area count;
- free bytes before and after pressure cleanup.

Logs do not include QQ message text, resource names, authentication values, Agent prompts, or full user-controlled paths. Debug logs may include fixed area labels such as `sandbox`, `traces`, and `resources`, but not candidate filenames.

## Failure handling

- Missing roots are treated as empty and may be created later by their owning subsystem.
- Permission errors, malformed timestamps, filesystem races, and individual unlink failures are non-fatal.
- A failed startup maintenance run does not prevent the bot from starting.
- Cancellation stops at candidate boundaries and releases the maintenance lease.
- Cleanup never sends user-visible QQ progress messages.

## Testing

Unit tests cover:

- default and loaded configuration values;
- age-based deletion for every managed area;
- oldest-first capacity trimming;
- protected-file preservation;
- unknown-entry preservation;
- symbolic-link and root-escape rejection;
- missing roots and filesystem races;
- disk-pressure ordering and post-cleanup measurement;
- candidate and wall-clock bounds;
- activity/maintenance mutual exclusion and cancellation;
- trigger coalescing and single-run behavior;
- periodic task startup, reload, and shutdown;
- privacy-safe summary logging.

App-level tests prove that active jobs block cleanup, queued work resumes after cleanup, job completion schedules only a cheap pressure check, and maintenance failures do not break replies or scheduled tasks.

## Acceptance criteria

- The bridge performs startup and six-hour maintenance without external schedulers.
- Active Agent/resource work and cleanup cannot overlap.
- Managed areas converge toward their configured retention and capacity limits.
- Authentication/configuration files, current jobs, unknown entries, and paths outside validated roots are preserved.
- Low-space conditions trigger coalesced pressure cleanup.
- Cleanup remains non-fatal, bounded, observable, and privacy-safe.
