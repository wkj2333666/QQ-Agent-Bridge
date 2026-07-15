# Isolated whisper.cpp Runtime

This optional runtime supplies the local `whisper-cli` binary and model used by
the bridge's `whisper` configuration. It is deliberately outside the repository
and defaults to:

```text
$HOME/.local/share/qq-agent-bridge/asr/
  bin/whisper-cli
  models/ggml-tiny-q8_0.bin
  cache/
```

The installer builds the pinned `whisper.cpp` release `v1.8.6` as a CPU Release
build. It downloads `ggml-tiny-q8_0.bin` and verifies its SHA-256 before placing
it in the runtime:

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
  binary: "/home/alice/.local/share/qq-agent-bridge/asr/bin/whisper-cli"
  model: "/home/alice/.local/share/qq-agent-bridge/asr/models/ggml-tiny-q8_0.bin"
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
