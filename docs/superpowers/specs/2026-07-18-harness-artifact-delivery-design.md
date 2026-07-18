# Harness Artifact Delivery Design

## Problem

Task and code agents can generate a real file but emit a malformed resource directive, omit the directive, or claim that a file was sent before the bridge has validated or delivered it. A production trace showed this exact malformed line:

```text
QQBOT_SEND_FILE: <token> .../video-summary.md主人，视频总结已经整理好...
```

The real Markdown file existed in the current job outbox. The parser treated the appended prose as part of the path, rejected that non-existent path, and the bridge still sent the agent's earlier success wording. This created a false delivery even though the existing path and OneBot delivery layers were behaving as implemented.

The harness must own delivery truth. Agent text is an untrusted request to deliver an artifact, not evidence that an artifact exists or reached QQ.

## Considered Approaches

### 1. Prompt and skill hardening only

Tell every agent to put directives on separate lines and verify files before replying. This is cheap but cannot enforce correctness. Model formatting errors and unsupported agent runtimes will still produce false deliveries.

### 2. Automatically send every file found in the outbox

Ignore directives and discover files after the task. This recovers omitted directives, but an outbox may contain source Markdown, HTML, temporary media, and multiple output formats. Sending all files leaks implementation artifacts and guessing among them is unsafe.

### 3. Transactional harness delivery with bounded repair

Keep directives as the explicit artifact selection protocol, harden their parsing, validate selected files, make a narrowly safe unique-file recovery when possible, and run at most one repair turn when selection still fails. Deliver resources before releasing success text. This is the selected approach because it fixes the observed failure while preserving current security boundaries and remaining compatible with Cursor, Codex, and Claude CLIs.

## Artifact Contract

The outgoing resource parser will return a structured collection result rather than only clean text, resources, and warning strings. The result records:

- cleaned user-visible text;
- validated and staged resources;
- whether the agent attempted any resource directives;
- recoverable and unresolved directive errors;
- whether any resource was recovered from a malformed directive or unique outbox candidate.

An artifact becomes deliverable only after all existing checks pass: current job token, current outbox inode, workspace containment, regular file type, single hard link, size limits, stable copy, and voice duration rules. Recovery never weakens these checks.

## Deterministic Recovery

The parser first handles a valid directive normally. When a directive path is malformed by text appended without a newline, it may recover the longest prefix that resolves to an existing regular file inside the current job outbox. Any suffix after that prefix is returned to cleaned text only when it is separated from the path unambiguously; otherwise it is discarded as malformed protocol payload.

When no usable directive remains, the harness may select a file automatically only if the current outbox contains exactly one eligible top-level regular file. Hidden files, directories, symlinks, hard-linked files, files exceeding configured limits, and nested temporary artifacts are not candidates. If there are zero or multiple candidates, the harness does not guess.

## Bounded Agent Repair

If the agent attempted delivery but deterministic recovery cannot produce all requested resources, the harness invokes the same configured agent runtime once with a repair prompt containing:

- the original user task;
- a machine-readable list of artifact validation failures;
- the current outbox path and resource token already assigned to the job;
- an instruction to repair or select only the missing deliverable, without repeating completed research or emitting success prose before verification.

The repair turn uses the task model and the existing model-usage fallback. It is limited to one attempt and a short timeout bounded by the parent job's remaining timeout. Repair output goes through the exact same parser and security validation. A repair failure cannot trigger another repair.

The bridge will log the repair reason, attempt, selected paths, and final delivery state without logging the secret resource token.

## Delivery Transaction

Resource-producing jobs use this order:

1. Run the agent and collect its final output.
2. Parse, recover, and validate requested resources.
3. Run the single repair turn only when required.
4. Copy validated resources into the stable sending directory.
5. Await each OneBot resource send call and record success or failure.
6. Release the agent's cleaned final text only after all selected resources were sent successfully.

This reverses the current unsafe order where success prose is sent before files. If every resource succeeds, normal final text follows the files. If a resource send fails, success prose is suppressed and replaced with a deterministic message saying which artifact type failed and that the task result was not delivered. For partial delivery, the bridge reports the delivered and failed counts without claiming full success.

Tasks with no artifact directive and no eligible outbox file keep the current text-only behavior. This avoids treating ordinary task answers as missing-file failures.

## Failure Messages

User-visible failures are generated by the bridge, not the agent:

- Validation failed after repair: `文件没有成功生成或无法验证，本次未发送。`
- OneBot delivery failed: `文件已经生成，但发送到 QQ 失败，本次未确认交付。`
- Partial delivery: `已发送 N 个资源，另有 M 个发送失败。`

Internal paths, resource tokens, stack traces, and sandbox details are never included. Agent-authored claims such as “文件发你啦” are not sent on a failed transaction.

## Components

`outgoing_resources.py` remains responsible for parsing, deterministic recovery, filesystem validation, and stable staging. It exposes a structured result while retaining a compatibility wrapper only if existing internal callers need it.

A small artifact delivery coordinator owns the one-shot repair and delivery state machine. It is independent from OneBot details and accepts callbacks for agent repair and resource sending, which keeps it directly testable.

`main.py` supplies job context, the agent repair callback, and OneBot adapter callbacks. It appends conversation memory only after the final delivery outcome is known, so memory cannot preserve a false “file sent” claim.

## Tests

Unit tests will cover:

- a directive with prose glued to an existing filename recovers the file;
- missing, ambiguous, nested, linked, oversized, and out-of-outbox files are never guessed;
- a unique eligible top-level outbox file can recover an omitted or broken directive;
- successful output does not invoke repair;
- unresolved output invokes repair exactly once;
- failed repair does not recurse;
- final success text is withheld until resource send succeeds;
- OneBot send failure suppresses agent success prose and returns a deterministic failure;
- partial delivery reports accurate counts;
- memory stores the verified delivery outcome rather than the rejected agent claim.

An application-level test will run a fake agent that first emits a malformed or missing path and then repairs it, asserting the observable QQ message and file-send order. Existing resource security tests and the full test suite must remain green.

## Non-Goals

- Inferring from arbitrary natural-language task text that a file was requested.
- Sending all files recursively from an outbox.
- Retrying research, video processing, or document generation indefinitely.
- Treating a successful local file copy as proof that QQ accepted the upload.
- Replacing the existing resource token and outbox security model.
