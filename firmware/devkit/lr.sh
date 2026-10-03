#!/bin/bash
# live-record builds of the devkit firmware, all from the command line (NCS v2.7.0 via nrfutil sdk-manager):
#   ./lr.sh build [sdmod|expansion]   sdmod: SPI SD module on the right header row (overlay/lr_sdmod.overlay); expansion: Seeed
#                                     Expansion Board Base for XIAO, SD CS on D2 (tests only)
#   ./lr.sh build promicro            ProMicro nRF52840 (nice!nano clone) + external PDM mic and SD module
#   ./lr.sh flash [sdmod|expansion|promicro]   a running firmware is sent to the UF2 bootloader by a 1200 bps touch
set -euo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
V=${2:-sdmod}
C=xiao_ble_sense_devkitv2-adafruit
OVL="$D/overlay/$C.overlay;$D/overlay/lr_sd.overlay"
# no speaker, no haptics; no Omi button: its code drives D4 and listens on D5, the I2C pins we keep free
EXTRA=(-DCONFIG_OMI_ENABLE_SPEAKER=n -DCONFIG_OMI_ENABLE_HAPTIC=n -DCONFIG_OMI_ENABLE_BUTTON=n)
case $V in
  sdmod) OVL="$OVL;$D/overlay/lr_sdmod.overlay" ;;
  expansion) OVL="$OVL;$D/overlay/xiao_expansion_base.overlay" ;;
  promicro) OVL="$OVL;$D/overlay/lr_promicro.overlay"  # LR-P1: battery on VDDH, P0.13/P0.17 are not charger pins here
            EXTRA+=(-DCONFIG_LR_BATTERY_VDDH=y '-DCONFIG_LR_NAME_PREFIX="LR-P1"' '-DCONFIG_BT_DEVICE_NAME="LR-P1"'
                    '-DCONFIG_BT_DIS_MODEL="LR-P1"' '-DCONFIG_BT_DIS_HW_REV_STR="ProMicro nRF52840"') ;;
  *) echo "unknown variant $V" >&2; exit 2 ;;
esac
UF2="$D/build/lr-$V/zephyr/zephyr.uf2"

case ${1:-} in
build)
  cd /opt/nordic/ncs/v2.7.0
  nrfutil sdk-manager toolchain launch --ncs-version v2.7.0 -- \
    west build -b xiao_ble/nrf52840/sense -d "$D/build/lr-$V" "$D" -- \
    -DNCS_TOOLCHAIN_VERSION=NONE -DCONF_FILE="$D/prj_$C.conf" -DCACHED_CONF_FILE="$D/prj_$C.conf" \
    -DDTC_OVERLAY_FILE="$OVL" -DPLATFORM=nrf52840 "${EXTRA[@]}"
  ;;
flash)
  [ -f "$UF2" ] || { echo "build first: $UF2 missing" >&2; exit 1; }
  uf2drive() { for v in /Volumes/*; do [ -f "$v/INFO_UF2.TXT" ] && { echo "$v"; return; }; done; return 0; }  # XIAO-SENSE, NICENANO, ...
  if [ -z "$(uf2drive)" ]; then
    port=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1) || true
    [ -n "$port" ] || { echo "no board on USB" >&2; exit 1; }
    stty -f "$port" 1200 || true
    for _ in $(seq 40); do [ -n "$(uf2drive)" ] && break; sleep 0.5; done
  fi
  drive=$(uf2drive)
  [ -n "$drive" ] || { echo "bootloader drive did not show up: double-tap reset once (old firmware?)" >&2; exit 1; }
  sleep 1
  cp -X "$UF2" "$drive/" 2>/dev/null || true  # the board reboots mid-copy; cp's complaint is expected
  for _ in $(seq 40); do ls /dev/cu.usbmodem* >/dev/null 2>&1 && break; sleep 0.5; done
  echo "flashed $V via $drive: $(ls /dev/cu.usbmodem* 2>/dev/null || echo 'no serial port yet')"
  ;;
*) sed -n 2,6p "$0"; exit 2 ;;
esac
