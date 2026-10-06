# 板子自检固件（nRF52840 Pro Micro：nice!nano 及其克隆）

新到的板子先刷这个，把板上能测的元件都过一遍，再刷我们的固件。它和 LR-P1 用同一套 bootloader 和分区（Adafruit UF2 bootloader + S140 6.1.1，应用从 0x26000 开始），通过 UF2 刷入，不需要 SWD。

```bash
python3 firmware/selftest/selftest.py build                   # 编译，NCS v2.7.0，和 lr.sh 一样
uv run firmware/selftest/selftest.py flash 1F8D7E0D3B8EFBD5   # 只刷这一块板
uv run firmware/selftest/selftest.py run 1F8D7E0D3B8EFBD5     # 跑全部检查，打印 PASS/FAIL 报告；有 FAIL 时退出码为 1
```

- **板子用 USB 序列号指定**：就是芯片的 DEVICEID，bootloader 和自检固件里都一样，`ioreg -p IOUSB -l -w0` 能看到。
- **`flash` 不会做 1200 baud 触碰**：板子停在 bootloader 时，按序列号找到它的 UF2 盘直接拷；板子在跑自检固件时，先发 `dfu` 命令让它进 bootloader。都不满足（比如板子在跑别的固件）就退出，提示双击 RST。拷之前会当场再核对一次序列号，所以别的板子同时插着也不会刷错。
- **第一次插上要允许**：新固件第一次出现在 Mac 上时，要在「允许配件连接」弹窗里点「允许」，否则 USB 设备停在 `!registered`，也没有串口。
- **不用脚本也能测**：用任何串口终端打开板子的串口，输入 `all` 也能跑板子自己能判断的那几项，每项回一行 `R {json}`，其中 `ok` 就是结论。

## 测什么、怎么判

| 项目 | 怎么测 | 通过标准 |
|---|---|---|
| 芯片、UICR | 读 FICR（型号、变体、容量、DEVICEID）和 UICR（REGOUT0、PSELRESET、APPROTECT、NFCPINS） | 只记录 |
| USB | 板子以 `LR selftest` 枚举，串口能收发 | 串口连得上、能应答 |
| 32 MHz 晶振起振 | 内核启动前，重启 HFXO 并计时 | 5 ms 内起振（规格书远小于 1 ms） |
| 32.768 kHz 晶振起振 | 内核启动前，把低频时钟切到晶振并计时；之后内核和射频都改用 RC，晶振坏了也不会卡死 | 1 s 内起振（典型 0.25 s） |
| 两颗晶振的相对频差 | 晶振上数 65536 个 RTC 节拍（2 s），用 CPU 周期计时；两颗都准时正好是 1.28 亿个周期 | 只记录，用来算下面的绝对精度 |
| 32 MHz 晶振精度 | 板子用 TIMER4 以 16 MHz 计数，每秒报 20 次，共 60 s；主机拿这些计数和 Mac 的时钟（NTP 校准）做线性拟合 | ±40 ppm（nRF52840 跑 BLE 的要求） |
| 32.768 kHz 晶振精度 | 相对频差加上 32 MHz 晶振的绝对频偏 | ±50 ppm：我们的固件用 Zephyr 默认值，告诉蓝牙对端睡眠时钟误差不超过 50 ppm。超了就要把声明值调大（如 `CONFIG_CLOCK_CONTROL_NRF_K32SRC_100PPM`），否则连接容易丢 |
| 射频发射 | 板子一直发不可连接广播，名字是 `LRT-<序列号>`；Mac 扫描 8 s | Mac 能收到，RSSI ≥ −80 dBm（板子离 Mac 1 m 以内） |
| 射频接收 | 板子被动扫描 5 s | 至少收到一台设备，最强 RSSI ≥ −90 dBm（附近总有手机或电脑） |
| GPIO 焊盘 | Pro Micro 封装上的 21 个 GPIO（两排排针，加上底边的 P1.01、P1.02、P1.07）：<br>① 每个脚单独上拉应读 1、下拉应读 0，否则就是连到了 GND 或 VDD；<br>② 每个脚依次输出低（其余上拉）、输出高（其余下拉），跟着变的脚和它短路（焊锡桥） | 没有卡死的脚，没有短路 |
| VDD | SAADC 量芯片稳压器的输出 | UICR REGOUT0 设定值 ±5%（REGOUT0 擦除时是 1.8 V） |
| VDDH（电池输入） | SAADC 量 VDDH/5。USB 和电池都从这里进来；插着 USB 时量到的是 USB 那一侧（经过板上的二极管） | 插 USB 时 4.3–5.5 V |
| P0.04 | nice!nano v2 的电池分压脚；克隆板上可能没有分压电阻 | 只记录 |
| 充电状态脚 | 这个封装上没有：充电灯直接接在充电芯片上（XIAO 是 P0.17） | 不判 |
| 蓝色 LED（P0.15） | 闪 5 次 | 人眼看 |
| DCDC（电感） | 依次打开 REG1、REG0 两路 DCDC，每次量 VDD。缺电感时芯片会掉电复位：主机看到串口消失，就报是哪一路 | 都不复位，VDD 都在范围内 |

测不到的：
- **焊盘虚接**（芯片引脚和焊盘没连上）：这要在焊盘上接线才能测出来。
- **复位键**：要人按。
- **充电电流**：要接电池。
- **VCC 开关**（P0.13 控制的 VCC 排针）：要在 VCC 排针上量电压。
