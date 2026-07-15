# Task 5 Report: Isolated whisper.cpp Deployment and Smoke Checks

## Scope

Added a documented, explicit local deployment path for the Whisper runtime. The
deployment helpers keep the compiled CLI, model, and cache outside Git under a
home-local ASR root and do not touch the project virtual environment, mamba, or
system package locations.

## Files Changed

- `runtime/asr/README.md`
  - Documents the default runtime layout, the pinned `v1.8.6` source release,
    and the Tiny Q8 model SHA-256.
  - Supplies an exact `whisper:` YAML example with absolute paths and smoke-check
    commands.
- `scripts/install_whisper_cpp.sh`
  - Resolves and restricts `QAB_ASR_ROOT` to a path below `$HOME`.
  - Clones the pinned source into a temporary directory, performs a CPU Release
    CMake build, copies only `whisper-cli`, downloads the model, verifies its
    SHA-256, and removes all temporary source/build files on exit.
- `scripts/check_whisper_cpp.sh`
  - Fails for a missing/non-executable binary or missing model, validates
    `whisper-cli --help`, and optionally transcribes one explicitly supplied
    WAV.
  - Uses a temporary output directory for the optional transcript, prints exit
    status and elapsed seconds, and does not write next to the WAV or into the
    repository.
- `.gitignore`
  - Ignores ASR cache, model binaries, and WAVs under `runtime/asr/`.
- `tests/test_deployment_docs.py`
  - Adds a focused contract suite for safe shell settings, home-local defaults,
    pinned source/model integrity, checker coverage, runtime artifact ignores,
    and enabling YAML documentation.

## TDD Evidence

The initial focused test run was intentionally red because the scripts, README,
and ASR ignore entries did not exist:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_deployment_docs.py
```

Result: `4 failed` due to the missing deployment files and ignore entries.

After implementation, two assertions were refined: the portable
`/usr/bin/env bash` shebang is not a write to a system directory, and the
checker reports `elapsed_seconds` rather than a literal `time` token. A final
regex correction restored the intended word-boundary system-write check.

## Verification

Focused documentation suite:

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_deployment_docs.py
```

Shell syntax and whitespace checks:

```bash
bash -n scripts/install_whisper_cpp.sh scripts/check_whisper_cpp.sh
git diff --check
```

These commands are rerun after this report is added and before the commit.

## Concerns

- Per task instruction, no real clone, model download, CMake build, dependency
  installation, or audio transcription was run. The installer must be reviewed
  and invoked explicitly later against the intended home-local destination.
- The source pin is the immutable upstream release tag `v1.8.6`; the Tiny Q8
  model is additionally protected by its full SHA-256.
- The scripts require standard local tools already appropriate for this manual
  deployment: `git`, `cmake`, a C/C++ compiler, `curl`, `sha256sum`, and
  `realpath`.

## Task 5 Review Fix

The deployment review identified two release-integrity gaps: `v1.8.6` was a
mutable reference rather than an exact source object, and the installer wrote
the live binary and model paths independently. This fix replaces the tag with
the benchmark commit `080bbbe85230f624f0b52127f1ae1218247989f9`. The installer
fetches that object, checks it out detached, and compares `git rev-parse HEAD`
to the expected SHA before CMake runs.

Installation now creates a staging directory below `QAB_ASR_ROOT`, stages both
the executable and model there, validates the model SHA-256, and atomically
moves the complete release into `releases/`. It then swaps a temporary relative
symlink into `current` with `mv -Tf`; failures before that final rename leave
the prior `current` symlink untouched. The checker and documented YAML paths
resolve through `current/bin` and `current/models`.

`tests/test_deployment_docs.py` now runs the installer and checker as
subprocesses under temporary `HOME` and `QAB_ASR_ROOT` values. Fake `git`,
`cmake`, `curl`, `sha256sum`, and `whisper-cli` commands keep the tests fully
offline. The suite proves a checksum mismatch retains the prior current release,
a successful install publishes a complete new current release after SHA checkout
verification, and the checker accepts a stub deployment while rejecting a
missing WAV input.

## Review Fix Verification

```bash
/home/wkj/projects/qq-bot/.venv/bin/pytest -q tests/test_deployment_docs.py
bash -n scripts/install_whisper_cpp.sh scripts/check_whisper_cpp.sh
/home/wkj/projects/qq-bot/.venv/bin/pytest -q
```

Results before commit: focused deployment suite `7 passed`; full suite
`417 passed, 11 skipped`. No network fetch, CMake build, system package action,
or mamba write was performed during this review fix.
