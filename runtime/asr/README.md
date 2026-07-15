# Isolated whisper.cpp Runtime

This optional runtime supplies the local `whisper-cli` binary and model used by
the bridge's `whisper` configuration. It is deliberately outside the repository
and defaults to:

```text
$HOME/.local/share/qq-agent-bridge/asr/
  current -> releases/whisper-080bbbe85230-<timestamp>-<pid>
  current/bin/whisper-cli
  current/models/ggml-tiny-q8_0.bin
  releases/<complete-release>/
```

The installer fetches and checks out the exact `whisper.cpp` benchmark commit
`080bbbe85230f624f0b52127f1ae1218247989f9`, then verifies `git rev-parse HEAD`
before its CPU Release build. It stages `ggml-tiny-q8_0.bin` alongside the
binary, requests a static `whisper-cli` build, and verifies its SHA-256. Before
publication, the staged binary is inspected with `ldd`: a genuinely static
binary is accepted, while any unresolved library (including an unresolved
system library) rejects the release. It then publishes the complete release and
atomically replaces `current`. A failed or interrupted install leaves the
preceding `current` target intact.

The Tiny Q8 model SHA-256 is:

```text
c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca
```

From the repository root, review the destination and hash printed by the
installer, then run it explicitly:

```bash
QAB_ASR_ROOT="$HOME/.local/share/qq-agent-bridge/asr" bash scripts/install_whisper_cpp.sh
```

`QAB_ASR_ROOT` must resolve inside `$HOME`. The scripts do not use `sudo`, pip,
mamba, or system package managers.

## Bridge Configuration

YAML does not expand `$HOME`, so configure absolute paths. For a user whose
home directory is `/home/alice`, this is the exact block to add to `config.yaml`:

```yaml
whisper:
  enabled: true
  binary: "/home/alice/.local/share/qq-agent-bridge/asr/current/bin/whisper-cli"
  model: "/home/alice/.local/share/qq-agent-bridge/asr/current/models/ggml-tiny-q8_0.bin"
  language: "zh"
  timeout_seconds: 90
  max_concurrent: 1
  cache_enabled: true
  cache_root: "data/whisper-cache"
  cache_ttl_seconds: 86400
  cache_max_items: 256
```

Replace `/home/alice` with the value of `$HOME` for the bridge process. Keep
`enabled: false` until both paths exist and have been checked.

## Smoke Check

The checker never writes transcript output beside the supplied WAV. It first
checks the binary, model, and `--help`, then optionally transcribes one explicit
WAV into a temporary directory and prints its timing and exit status:

```bash
bash scripts/check_whisper_cpp.sh
bash scripts/check_whisper_cpp.sh /absolute/path/to/smoke.wav
```

Do not add models, WAV files, cache entries, or personal `config.yaml` to Git.
