#!/usr/bin/env bash
# Standalone: bash .github/scripts/firmware-setup-idf.sh
# Prerequisites: Linux x86_64, bash, git, curl, tar, xz, uv, C compiler,
# cmake, ninja, make, and standard ESP-IDF host libraries (libusb/OpenSSL).
# Installation is entirely user-owned; never sudo, Docker or machine configuration.
set -euo pipefail
CACHE=${LR_FIRMWARE_CACHE:-${HOME}/.cache/live-record-ci}
mkdir -p "$CACHE"
CACHE=$(realpath "$CACHE")
IDF="$CACHE/esp-idf-v5.5.5"
IDF_COMMIT=b774170ff46c393eeb5e495ea37936038d3f4f4f
for command in git uv cmake ninja cc; do
  command -v "$command" >/dev/null || { echo "Required host tool missing: $command" >&2; exit 1; }
done
if [ ! -d "$IDF/.git" ]; then
  git clone --depth 1 --recursive --shallow-submodules --branch v5.5.5 https://github.com/espressif/esp-idf.git "$IDF"
fi
[ "$(git -C "$IDF" rev-parse HEAD)" = "$IDF_COMMIT" ] || { echo "ESP-IDF cache is not pinned v5.5.5; use a new LR_FIRMWARE_CACHE" >&2; exit 1; }
# Honor an existing exact clone; no fetch/update for already installed builds.
if git -C "$IDF" submodule status --recursive | LC_ALL=C grep -qE '^[-+U]'; then
  git -C "$IDF" submodule update --init --recursive --depth 1
fi
export IDF_TOOLS_PATH="$CACHE/idf-tools-v5.5.5"
if [ ! -f "$CACHE/idf-v5.5.5.ready" ]; then
  uv python install 3.11
  PYTHON=$(uv python find 3.11)
  export PATH="$(dirname "$PYTHON"):$PATH"
  "$IDF/install.sh" esp32s3
  touch "$CACHE/idf-v5.5.5.ready"
fi
# export.sh must be sourced per job, after selecting the same Python interpreter.
ENV_FILE="$CACHE/idf-v5.5.5.env"
{
  printf 'export IDF_PATH=%q\n' "$IDF"
  printf 'export IDF_TOOLS_PATH=%q\n' "$IDF_TOOLS_PATH"
  printf 'export PATH=%q:"$PATH"\n' "$(dirname "$(uv python find 3.11)")"
  printf 'source %q\n' "$IDF/export.sh"
  printf 'export FIRMWARE_TOOLCHAIN_METADATA=%q\n' "$CACHE/idf-v5.5.5.metadata.json"
  printf 'export FIRMWARE_SDK_MANIFEST=%q\n' "$IDF/tools/tools.json"
} > "$ENV_FILE"
printf '{"sdk":"ESP-IDF","version":"v5.5.5","sdk_commit":"%s","target":"esp32s3","python":"3.11","tools":"official tools/tools.json via install.sh"}\n' "$IDF_COMMIT" > "$CACHE/idf-v5.5.5.metadata.json"
[ -z "${GITHUB_OUTPUT:-}" ] || printf 'env_file=%s\n' "$ENV_FILE" >> "$GITHUB_OUTPUT"
printf 'Environment: source %q\n' "$ENV_FILE"
