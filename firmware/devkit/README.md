# BLE 录音硬件固件（Omi devkit 改）

XIAO nRF52840 Sense 上的录音固件：PDM 麦克风 → Opus 16 kHz → BLE 实时推给手机或浏览器；没有设备在听时写进 SD 卡，事后补传。
基于 [BasedHardware/omi](https://github.com/BasedHardware/omi) `omi/firmware/devkit` 的 2e878f6 版本（MIT，见 LICENSE），上游原样在本目录的第一个提交里，之后的提交是我们的改动。

## 构建与刷写（nRF Connect SDK v2.7.0，全命令行）

```bash
nrfutil install sdk-manager && nrfutil sdk-manager install --ncs-version v2.7.0   # 一次
./lr.sh build sdmod        # 焊在右排的 SD 模块（正式）
./lr.sh build expansion    # Seeed XIAO 扩展底板（只做测试）
./lr.sh flash sdmod        # 运行中的固件收到 1200 bps 触碰自己进 UF2 bootloader，不用按 reset
```

固件第一次刷进（或卡死）时才需要双击 reset 进 bootloader。

## 接线（sdmod）

模块（3.3V CS MOSI CLK MISO GND）沿 XIAO 右排对齐：3V3–3.3V、D10–CS、D9–MOSI、D8–CLK、D7–MISO，GND 飞线回右排 GND。
左排 D0–D6 留空：D4/D5 是排针上仅有的非「low frequency I/O」引脚，留给 I2C（RTC）；D6 留给 UART 日志。见 `overlay/lr_sdmod.overlay`。

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
