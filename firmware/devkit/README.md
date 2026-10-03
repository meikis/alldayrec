# BLE 录音硬件固件（Omi devkit 改）

XIAO nRF52840 Sense 上的录音固件：PDM 麦克风 → Opus 16 kHz → BLE 实时推给手机或浏览器；没有设备在听时写进 SD 卡，事后补传。
基于 [BasedHardware/omi](https://github.com/BasedHardware/omi) `omi/firmware/devkit` 的 2e878f6 版本（MIT，见 LICENSE），上游原样在本目录的第一个提交里，之后的提交是我们的改动。

## 型号与命名

| 型号 | 板子 | `lr.sh` 变体 | 电池读数 |
|---|---|---|---|
| `LR-X1` | XIAO nRF52840 Sense（板载麦克风，焊 SD 模块和电池） | `sdmod` | 板载分压 AIN7 |
| `LR-P1` | ProMicro nRF52840（nice!nano 兼容，外接 PDM 麦克风和 SD 模块） | `promicro` | 芯片内部 VDDH/5（电池接 VDDH，不用外接电阻） |

每台的蓝牙名是型号加身份地址末两字节，如 `LR-P1-BDAE`：地址跟着芯片走，刷固件不变。广播包放型号（`LR-P1`），扫描响应放完整名字；
设备信息服务里 Model 是型号，Hardware Revision 是板子。客户端都按音频服务 UUID `19b10000-…` 找设备，不看名字。

## 构建与刷写（nRF Connect SDK v2.7.0，全命令行）

```bash
nrfutil install sdk-manager && nrfutil sdk-manager install --ncs-version v2.7.0   # 一次
./lr.sh build sdmod        # 焊在右排的 SD 模块（正式）
./lr.sh build expansion    # Seeed XIAO 扩展底板（只做测试）
./lr.sh build promicro     # ProMicro nRF52840（nice!nano 兼容）+ 外接 PDM 麦克风 + SD 模块（低成本方案）
./lr.sh flash sdmod        # 运行中的固件收到 1200 bps 触碰自己进 UF2 bootloader，不用按 reset
```

固件第一次刷进（或卡死）时才需要双击 reset 进 bootloader。

## 发布到 App 在线升级

```bash
LR_TOKEN=<服务器 AUTH_TOKEN> LR_FIRMWARE_TOKEN=<服务器 FIRMWARE_TOKEN> \
  uv run --no-project python lr_release.py sdmod promicro --server https://lr.example.com --notes "改了什么"
```

逐个变体构建、打包、上传；型号取自那次构建自己的 `.config`（`CONFIG_BT_DIS_MODEL`），只收 sdmod→LR-X1、promicro→LR-P1。
只发布已在 origin/main 上、本目录无未提交改动的提交。App 录音设备页「检查更新」按型号取服务器上最新的一个，用户点了才升级。
固件版本号（DIS Firmware Revision）是 `YYYYMMDD.HHMMSS-<短哈希>`，取提交时间，按文本比较即新旧；本目录有未提交改动时末尾加 `+`，这种构建服务器不收。

## 接线（sdmod）

模块（3.3V CS MOSI CLK MISO GND）沿 XIAO 右排对齐：3V3–3.3V、D10–CS、D9–MOSI、D8–CLK、D7–MISO，GND 飞线回右排 GND。
左排 D0–D6 留空：D4/D5 是排针上仅有的非「low frequency I/O」引脚，留给 I2C（RTC）；D6 留给 UART 日志。见 `overlay/lr_sdmod.overlay`。

## 接线（promicro）

USB 朝上。麦克风（3V GND SEL CLK DAT）转 180° 插在左排第 1–5 个旁边、同一行：P0.06–DAT、P0.08–CLK、GND–SEL、GND–GND、P0.17–3V（GPIO 供电）。
SD 模块跳线：P0.20–CS、P0.22–MOSI、P0.24–CLK、P1.00–MISO（左排第 6–9 个，满速引脚），VCC（右排第 4 个，P0.13 常开）–3V3，GND（右排第 2 个）–GND。
右排第 3 个是 RST，不要接地。bootloader 是 nice!nano UF2 0.6.0 / S140 6.1.1，应用从 0x26000 起（`overlay/lr_promicro.overlay`）。

## 离线存储与补传

协议和文件格式见 `src/lr_store.h`；参考客户端 `lr_sync.py`（对时、列文件、读取、按帧号还原墙钟时间、导出 WAV、可选删除）：

```bash
uv run --no-project --with bleak python lr_sync.py OUT_DIR [--delete]
uv run --no-project --with bleak python lr_sync.py --selftest
```

## 已知问题

- SD 用 legacy SPI（`nordic,nrf-spi`）8 MHz：SPIM（EasyDMA）在焊好的模块上初始化挂死，原因未查
- NCS 2.7 `card_ioctl()` 在 `DISK_IOCTL_CTRL_SYNC` 后缺 `break`，FatFs 每次 sync 都报 -EIO；DELETE 以 `fs_stat` 确认文件已删
- 热复位时 SD 卡可能卡在命令中间只能断电恢复；1200 bps 复位前会先卸载，看门狗复位仍有风险
- 开机前约 0.3 s 音频丢失；`MIC_GAIN` 偏高会削顶；BLE 连接间隔（~20 Hz）串进麦克风的「哒哒」声未修
