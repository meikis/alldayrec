# BLE 录音硬件固件（Omi devkit 改）

XIAO nRF52840 Sense 上的录音固件：PDM 麦克风 → Opus 16 kHz → 记录流：上电就一直写 SD 卡，手机连着时同一份记录（文件号 + 偏移）同时推过去，断开期间的部分手机回来后用 READ 补齐。
基于 [BasedHardware/omi](https://github.com/BasedHardware/omi) `omi/firmware/devkit` 的 2e878f6 版本（MIT，见 LICENSE），上游原样在本目录的第一个提交里，之后的提交是我们的改动。

## 型号与命名

| 型号 | 板子 | `lr.sh` 变体 | 电池读数 |
|---|---|---|---|
| `LR-X1` | XIAO nRF52840 Sense（板载麦克风，焊 SD 模块和电池） | `sdmod` | 板载分压 AIN7 |
| `LR-P1` | ProMicro nRF52840（nice!nano 兼容，外接 PDM 麦克风和 SD 模块） | `promicro` | 芯片内部 VDDH/5（电池接 VDDH，不用外接电阻） |
| `LR-P2` | 同 LR-P1 的板子，专做测试的那一块（bootloader 0.10.0，SWD 线常接，#110） | `promicro-p2` | 同 LR-P1 |
| `LR-P3` | 同 LR-P1 的板子，飞线接 SD 模块、PDM 麦（SEL 接 3V）和拨动开关（P1.15）装进外壳的量产版（#122）；新机先跑 `selftest.py run SERIAL --p3` | `promicro-p3` | 同 LR-P1 |

每台的蓝牙名是型号加身份地址末两字节，如 `LR-P1-BDAE`：地址跟着芯片走，刷固件不变。广播包放型号（`LR-P1`），扫描响应放完整名字；
设备信息服务里 Model 是型号，Hardware Revision 是板子。客户端都按记录流服务 UUID `7c3e0000-…`（广播里有）找设备。

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

## 记录流

协议和文件格式见 `src/lr_store.h`。要点：
- 每 10 帧（100 ms）封一条 `'A'` 记录；封好的记录立即通过 LIVE 通知推给订阅的手机（`file offset bytes`），同时进 RAM 批缓冲，攒到 `CONFIG_LR_STORE_BATCH`（4 KB，约 1 s）整批写卡。意外复位最多丢这一批。
- 手机写 TIME 时，固件往流里插一条 `'T'`（帧号 = 手机时间）；手机是时钟的权威源，按同一次开机里最近的 `'T'` 算每块的开始时间。
- 连接不再开新段：段只在开机、16 MB、卡写不进（无卡 / 卡满 / 借给 USB / 写失败）时切换。LIST 列出正在写的段，READ 读它时先把已封好的记录写卡再读。
- 卡上的文件永远是流里同名文件的前缀：卡出问题时流照常推，卡恢复后从新段继续。

参考客户端 `lr_sync.py`（对时、列文件含当前段、读取、可选跟读 LIVE 并和卡上逐字节比对、按 `'T'` 还原墙钟时间、导出 WAV、可选删除）：

```bash
uv run --no-project --with bleak python lr_sync.py OUT_DIR [--follow 60] [--delete]
uv run --no-project --with bleak python lr_sync.py --selftest
# 记录流的主机端检查（LIVE 与卡上字节一致、批写、'T'、丢帧、写卡失败），命令见文件头
cc ... test/store_check.c
```

## 已知问题

- SD 用 legacy SPI（`nordic,nrf-spi`）8 MHz：SPIM（EasyDMA）在焊好的模块上初始化挂死，原因未查
- NCS 2.7 `card_ioctl()` 在 `DISK_IOCTL_CTRL_SYNC` 后缺 `break`，FatFs 每次 sync 都报 -EIO（数据已写进去，写卡时忽略）；DELETE 以 `fs_stat` 确认文件已删
- 卡写失败或开机时没卡：流照常推（TIME 的 store 显示错误），每分钟重新挂载一次卡，成功后从新段继续写（开机时没卡的这次才计开机次数）
- `'T'` 的帧号取的是对时那一刻正在编码的帧，这一帧的声音比它早约 0–100 ms（麦克风按 100 ms 一块交数据），时间系统性偏晚约 50 ms
- 热复位时 SD 卡可能卡在命令中间只能断电恢复；1200 bps 复位前会先卸载，看门狗复位仍有风险
- 开机前约 0.3 s 音频丢失；`MIC_GAIN` 偏高会削顶；BLE 连接间隔（~20 Hz）串进麦克风的「哒哒」声未修
