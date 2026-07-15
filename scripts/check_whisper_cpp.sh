#!/usr/bin/env bash
# Verify a home-local whisper.cpp deployment without writing to the repository.
set -euo pipefail

ASR_ROOT="${QAB_ASR_ROOT:-$HOME/.local/share/qq-agent-bridge/asr}"
HOME_ROOT="$(realpath -m "$HOME")"
ASR_ROOT="$(realpath -m "$ASR_ROOT")"
case "$ASR_ROOT" in
  "$HOME_ROOT"/*) ;;
  *)
    printf 'QAB_ASR_ROOT must be inside HOME: %s\n' "$ASR_ROOT" >&2
    exit 2
    ;;
esac

BINARY="${QAB_WHISPER_BINARY:-$ASR_ROOT/bin/whisper-cli}"
MODEL="${QAB_WHISPER_MODEL:-$ASR_ROOT/models/ggml-tiny-q8_0.bin}"
LANGUAGE="${QAB_WHISPER_LANGUAGE:-zh}"
WAV="${1:-}"

if [[ $# -gt 1 ]]; then
  printf 'Usage: %s [WAV]\n' "$0" >&2
  exit 2
fi

if [[ ! -x "$BINARY" ]]; then
  printf 'whisper-cli is missing or not executable: %s\n' "$BINARY" >&2
  exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  printf 'Whisper model is missing: %s\n' "$MODEL" >&2
  exit 1
fi

print_timing() {
  local label="$1"
  local started="$2"
  local status="$3"
  printf '%s: exit=%s elapsed_seconds=%s\n' "$label" "$status" "$(( $(date +%s) - started ))"
}

help_started="$(date +%s)"
if "$BINARY" --help >/dev/null; then
  help_status=0
else
  help_status=$?
fi
print_timing 'help check' "$help_started" "$help_status"
if [[ "$help_status" -ne 0 ]]; then
  exit "$help_status"
fi

if [[ -z "$WAV" ]]; then
  printf 'No WAV supplied; binary and model checks completed.\n'
  exit 0
fi
if [[ ! -f "$WAV" ]]; then
  printf 'WAV input is missing: %s\n' "$WAV" >&2
  exit 1
fi
case "$WAV" in
  *.wav|*.WAV) ;;
  *)
    printf 'Smoke input must be a WAV file: %s\n' "$WAV" >&2
    exit 2
    ;;
esac

OUTPUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qq-agent-bridge-whisper-check.XXXXXX")"
trap 'rm -rf "$OUTPUT_DIR"' EXIT
transcribe_started="$(date +%s)"
if "$BINARY" -m "$MODEL" -f "$WAV" -l "$LANGUAGE" -otxt -of "$OUTPUT_DIR/transcript" -nt -np; then
  transcribe_status=0
else
  transcribe_status=$?
fi
print_timing 'WAV smoke check' "$transcribe_started" "$transcribe_status"
if [[ "$transcribe_status" -ne 0 ]]; then
  exit "$transcribe_status"
fi

TRANSCRIPT="$OUTPUT_DIR/transcript.txt"
if [[ ! -f "$TRANSCRIPT" ]]; then
  printf 'whisper-cli completed without a transcript: %s\n' "$TRANSCRIPT" >&2
  exit 1
fi
printf 'Transcript:\n'
cat "$TRANSCRIPT"
