#!/usr/bin/env bash
# Build one pinned CPU-only whisper.cpp CLI and model under a home-local runtime.
set -euo pipefail

ASR_ROOT="${QAB_ASR_ROOT:-$HOME/.local/share/qq-agent-bridge/asr}"
WHISPER_CPP_REPOSITORY="https://github.com/ggml-org/whisper.cpp.git"
WHISPER_CPP_REF="v1.8.6"
MODEL_NAME="ggml-tiny-q8_0.bin"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_NAME}"
MODEL_SHA256="c2085835d3f50733e2ff6e4b41ae8a2b8d8110461e18821b09a15c40c42d1cca"

HOME_ROOT="$(realpath -m "$HOME")"
ASR_ROOT="$(realpath -m "$ASR_ROOT")"
case "$ASR_ROOT" in
  "$HOME_ROOT"/*) ;;
  *)
    printf 'QAB_ASR_ROOT must be inside HOME: %s\n' "$ASR_ROOT" >&2
    exit 2
    ;;
esac

BIN_DIR="$ASR_ROOT/bin"
MODEL_DIR="$ASR_ROOT/models"
BIN_PATH="$BIN_DIR/whisper-cli"
MODEL_PATH="$MODEL_DIR/$MODEL_NAME"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qq-agent-bridge-whisper.XXXXXX")"
SOURCE_DIR="$TEMP_DIR/whisper.cpp"
BUILD_DIR="$TEMP_DIR/build"
MODEL_TMP="$TEMP_DIR/$MODEL_NAME"
trap 'rm -rf "$TEMP_DIR"' EXIT

printf 'Installing whisper.cpp %s to %s\n' "$WHISPER_CPP_REF" "$ASR_ROOT"
printf 'Model: %s\nSHA-256: %s\n' "$MODEL_NAME" "$MODEL_SHA256"

git clone --depth 1 --branch "$WHISPER_CPP_REF" "$WHISPER_CPP_REPOSITORY" "$SOURCE_DIR"
cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_EXAMPLES=ON
cmake --build "$BUILD_DIR" --config Release --target whisper-cli -j "${QAB_ASR_BUILD_JOBS:-2}"

curl --fail --location --retry 3 --output "$MODEL_TMP" "$MODEL_URL"
MODEL_ACTUAL_SHA256="$(sha256sum "$MODEL_TMP" | awk '{print $1}')"
if [[ "$MODEL_ACTUAL_SHA256" != "$MODEL_SHA256" ]]; then
  printf 'Model SHA-256 mismatch: expected %s, got %s\n' "$MODEL_SHA256" "$MODEL_ACTUAL_SHA256" >&2
  exit 1
fi

mkdir -p "$BIN_DIR" "$MODEL_DIR" "$ASR_ROOT/cache"
install -m 755 "$BUILD_DIR/bin/whisper-cli" "$BIN_PATH"
mv "$MODEL_TMP" "$MODEL_PATH"

printf 'Installed binary: %s\nInstalled model: %s\n' "$BIN_PATH" "$MODEL_PATH"
