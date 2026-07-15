#!/usr/bin/env bash
# Build one pinned CPU-only whisper.cpp CLI and model under a home-local runtime.
set -euo pipefail

ASR_ROOT="${QAB_ASR_ROOT:-$HOME/.local/share/qq-agent-bridge/asr}"
WHISPER_CPP_REPOSITORY="https://github.com/ggml-org/whisper.cpp.git"
WHISPER_CPP_COMMIT="080bbbe85230f624f0b52127f1ae1218247989f9"
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

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/qq-agent-bridge-whisper.XXXXXX")"
SOURCE_DIR="$TEMP_DIR/whisper.cpp"
BUILD_DIR="$TEMP_DIR/build"
RELEASE_NAME="whisper-${WHISPER_CPP_COMMIT:0:12}-$(date +%s)-$$"
RELEASE_DIR="$ASR_ROOT/releases/$RELEASE_NAME"
STAGING_DIR=""
CURRENT_NEXT="$ASR_ROOT/.current-${RELEASE_NAME}"

cleanup() {
  rm -rf -- "$TEMP_DIR"
  if [[ -n "$STAGING_DIR" ]]; then
    rm -rf -- "$STAGING_DIR"
  fi
  rm -f -- "$CURRENT_NEXT"
}
trap cleanup EXIT

printf 'Installing whisper.cpp %s to %s\n' "$WHISPER_CPP_COMMIT" "$ASR_ROOT"
printf 'Model: %s\nSHA-256: %s\n' "$MODEL_NAME" "$MODEL_SHA256"

git clone --no-checkout "$WHISPER_CPP_REPOSITORY" "$SOURCE_DIR"
git -C "$SOURCE_DIR" fetch --depth 1 origin "$WHISPER_CPP_COMMIT"
git -C "$SOURCE_DIR" checkout --detach "$WHISPER_CPP_COMMIT"
CHECKED_OUT_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
if [[ "$CHECKED_OUT_COMMIT" != "$WHISPER_CPP_COMMIT" ]]; then
  printf 'whisper.cpp checkout mismatch: expected %s, got %s\n' \
    "$WHISPER_CPP_COMMIT" "$CHECKED_OUT_COMMIT" >&2
  exit 1
fi

cmake -S "$SOURCE_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_EXAMPLES=ON -DBUILD_SHARED_LIBS=OFF
cmake --build "$BUILD_DIR" --config Release --target whisper-cli -j "${QAB_ASR_BUILD_JOBS:-2}"

mkdir -p "$ASR_ROOT/releases"
STAGING_DIR="$(mktemp -d "$ASR_ROOT/.staging.XXXXXX")"
STAGED_RELEASE="$STAGING_DIR/release"
STAGED_BINARY="$STAGED_RELEASE/bin/whisper-cli"
STAGED_MODEL="$STAGED_RELEASE/models/$MODEL_NAME"
mkdir -p "$STAGED_RELEASE/bin" "$STAGED_RELEASE/models"

install -m 755 "$BUILD_DIR/bin/whisper-cli" "$STAGED_BINARY"
DEPENDENCY_REPORT=""
if ! DEPENDENCY_REPORT="$(LC_ALL=C ldd "$STAGED_BINARY" 2>&1)"; then
  if [[ "$DEPENDENCY_REPORT" != *"not a dynamic executable"* ]]; then
    printf 'Unable to inspect whisper-cli dependencies:\n%s\n' "$DEPENDENCY_REPORT" >&2
    exit 1
  fi
fi
if [[ "$DEPENDENCY_REPORT" == *"not found"* ]]; then
  printf 'Staged whisper-cli has unresolved dynamic libraries:\n%s\n' "$DEPENDENCY_REPORT" >&2
  exit 1
fi
curl --fail --location --retry 3 --output "$STAGED_MODEL" "$MODEL_URL"
MODEL_ACTUAL_SHA256="$(sha256sum "$STAGED_MODEL" | awk '{print $1}')"
if [[ "$MODEL_ACTUAL_SHA256" != "$MODEL_SHA256" ]]; then
  printf 'Model SHA-256 mismatch: expected %s, got %s\n' "$MODEL_SHA256" "$MODEL_ACTUAL_SHA256" >&2
  exit 1
fi
if [[ ! -x "$STAGED_BINARY" || ! -f "$STAGED_MODEL" ]]; then
  printf 'Staged release is incomplete: %s\n' "$STAGED_RELEASE" >&2
  exit 1
fi

mv -- "$STAGED_RELEASE" "$RELEASE_DIR"
ln -s "releases/$RELEASE_NAME" "$CURRENT_NEXT"
mv -Tf -- "$CURRENT_NEXT" "$ASR_ROOT/current"

printf 'Published release: %s\n' "$RELEASE_DIR"
printf 'Current binary: %s/current/bin/whisper-cli\n' "$ASR_ROOT"
printf 'Current model: %s/current/models/%s\n' "$ASR_ROOT" "$MODEL_NAME"
