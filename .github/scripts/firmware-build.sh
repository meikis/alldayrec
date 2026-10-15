#!/usr/bin/env bash
# Standalone after setup: FIRMWARE_FAMILY=devkit FIRMWARE_VARIANT=sdmod \
# FIRMWARE_ENV_FILE=~/.cache/live-record-ci/ncs-v2.7.0.env bash .github/scripts/firmware-build.sh
# RUNNER_TEMP defaults to an isolated temporary directory outside firmware source.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
FAMILY=${FIRMWARE_FAMILY:?Select devkit, selftest or blerelay}
VARIANT=${FIRMWARE_VARIANT:-$FAMILY}
source "${FIRMWARE_ENV_FILE:?Run firmware-setup-ncs.sh or firmware-setup-idf.sh first}"
export RUNNER_TEMP=${RUNNER_TEMP:-$(mktemp -d)}
export CMAKE_BUILD_PARALLEL_LEVEL=2 MAKEFLAGS=-j2 IDF_PY_BUILD_JOBS=2 LR_BUILD_JOBS=2
unset LR_LOG
VERSION="$(git log -1 --format=%cd --date=format:%Y%m%d.%H%M%S)-$(git rev-parse --short HEAD)"
case "$FAMILY" in
  devkit)
    case "$VARIANT" in sdmod|promicro|promicro-p2|promicro-p3|expansion) ;; *) echo 'Invalid recorder variant' >&2; exit 1 ;; esac
    git diff --quiet HEAD -- firmware/devkit || { echo 'Recorder source must be clean for a versioned artifact' >&2; exit 1; }
    bash firmware/devkit/lr.sh build "$VARIANT"
    uvx --from adafruit-nrfutil==0.5.3.post16 adafruit-nrfutil dfu genpkg \
      --dev-type 0x0052 --sd-req 0xFFFE \
      --application "firmware/devkit/build/lr-$VARIANT/zephyr/zephyr.hex" \
      "firmware/devkit/build/lr-$VARIANT/lr-$VARIANT.zip"
    ;;
  selftest)
    nrfutil sdk-manager toolchain launch --ncs-version v2.7.0 --install-dir "$LR_NCS_INSTALL_DIR" \
      --chdir "$NCS" -- west build -b xiao_ble/nrf52840 \
      -d "$ROOT/firmware/selftest/build" "$ROOT/firmware/selftest" -o=-j2 -- \
      -DNCS_TOOLCHAIN_VERSION=NONE "-DDTC_OVERLAY_FILE=$ROOT/firmware/selftest/promicro.overlay" \
      "-DBUILD_VERSION=$VERSION"
    ;;
  blerelay)
    export RELAY_KEY_FILE="$RUNNER_TEMP/firmware-relay.key"
    trap 'rm -f "$RELAY_KEY_FILE"' EXIT
    if [[ "${GITHUB_EVENT_NAME:-}" == push && "${GITHUB_REF:-}" == refs/tags/firmware-* ]]; then
      [[ "${RELAY_KEY_HEX:-}" =~ ^[0-9a-f]{64}$ ]] && [[ "$RELAY_KEY_HEX" != 0000000000000000000000000000000000000000000000000000000000000000 ]] || {
        echo 'Formal relay requires a non-test RELAY_KEY_HEX authentication secret' >&2; exit 1;
      }
      (umask 077; printf '%s\n' "$RELAY_KEY_HEX" > "$RELAY_KEY_FILE")
    else
      (umask 077; printf '%064d\n' 0 > "$RELAY_KEY_FILE")
    fi
    unset RELAY_KEY_HEX
    idf.py -C firmware/blerelay -DIDF_TARGET=esp32s3 "-DPROJECT_VER=$VERSION" build
    ;;
  *) echo 'Invalid firmware family' >&2; exit 1 ;;
esac
uv run --no-project --python 3.11 python .github/scripts/firmware-package.py "$FAMILY" "$VARIANT"
