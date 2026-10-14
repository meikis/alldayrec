// The recorder's event log (firmware/devkit/src/lr_log.h), put in words as lr_sync.py's `log` does; kept in step with
// lr_log.h by hand: a code it does not know shows its raw arguments.
import type { Upgrade } from "./api";

export interface Rec {
  boot: number;
  t: number; // the codec frame, or uptime ms when code has UPTIME
  code: number;
  x: number;
  a: number;
  b: number;
}
export const UPTIME = 0x80;
export const BOOT = 1,
  FAULT = 2,
  DISCONNECT = 10,
  DFU = 21,
  HEARTBEAT = 22,
  SD_WRITES = 25,
  POWER = 29;

const RESETREAS: [number, string][] = [
  [0, "复位键 RESETPIN"],
  [1, "看门狗 DOG"],
  [2, "软件复位 SREQ"],
  [3, "死锁 LOCKUP"],
  [16, "关机后唤醒 OFF"],
  [17, "LPCOMP"],
  [18, "调试接口 DIF"],
  [19, "NFC"],
  [20, "插 USB VBUS"],
];
const HCI: Record<number, string> = {
  0x05: "认证失败",
  0x06: "缺配对密钥",
  0x08: "连接超时（信号丢了）",
  0x13: "手机断开",
  0x14: "手机资源不足",
  0x15: "手机关机",
  0x16: "设备主动断开",
  0x1a: "不支持的功能",
  0x1f: "未指明",
  0x22: "链路层响应超时",
  0x28: "时刻已过",
  0x2a: "事务冲突",
  0x3b: "参数不可接受",
  0x3d: "MIC 校验失败",
  0x3e: "连接没建立起来",
};
const SECURITY = [
  "成功",
  "认证失败",
  "缺 PIN 或密钥",
  "OOB 不可用",
  "认证要求不符",
  "不支持配对",
  "不允许配对",
  "参数无效",
  "密钥被拒",
  "未指明",
];
const FAULTS = ["CPU 异常", "伪中断", "栈检查", "内核 oops", "内核 panic"];
const ERRNO: Record<number, string> = {
  1: "EPERM",
  2: "ENOENT",
  5: "EIO",
  11: "EAGAIN",
  12: "ENOMEM",
  13: "EACCES",
  16: "EBUSY",
  19: "ENODEV",
  22: "EINVAL",
  28: "ENOSPC",
  77: "EBADMSG",
  105: "ENOBUFS",
  116: "ETIMEDOUT",
  120: "EALREADY",
  128: "ENOTCONN",
  134: "ENOTSUP",
};

const hex = (v: number, n: number) => v.toString(16).padStart(n, "0");
const err = (v: number) => {
  const s = v | 0; // an errno, negative
  return s ? `${s}${ERRNO[-s] ? " -" + ERRNO[-s] : ""}` : "成功";
};
const reas = (v: number) =>
  RESETREAS.filter(([b]) => (v >>> b) & 1)
    .map(([, n]) => n)
    .join("+") || "上电";
const param = (a: number, b: number) =>
  `间隔 ${(a & 0xffff) * 1.25} ms，延迟 ${a >>> 16}，超时 ${b * 10} ms`;
// APPEND_ERR's x (lr_log.h): which part of an append failed
const APPEND = ["", "写卡跟不上，本段余下只走实时：", "重启时卡卡在写入：", "文件长度没写上卡："];
const stack = (v: number) => (v === 0xffff ? "-" : String(v));
const phy = (v: number) => ({ 1: "1M", 2: "2M", 4: "Coded" })[v] ?? String(v);
const clock = (ms: number) => new Date(ms).toLocaleString("zh-CN", { hour12: false });
const kb = (b: number) => (b / 1024).toFixed(1);
function sdWrites(r: Rec) {
  const n = [r.b & 0x3ff, (r.b >>> 10) & 0x3ff, (r.b >>> 20) & 0x3ff, r.x];
  return (
    `${n[0] + n[1] + n[2] + n[3]} 次，共 ${r.a & 0xfffff} ms，最长 ${r.a >>> 20} ms；` +
    `<20 ms ${n[0]}，20–49 ms ${n[1]}，50–200 ms ${n[2]}，>200 ms ${n[3]}`
  );
}
const peak = (p: number) => (p ? `${Math.round(20 * Math.log10(p / 256))} dBFS` : "低于 -48 dBFS");
const ascii = (...c: number[]) => String.fromCharCode(...c.map((v) => v & 0xff));
function health(r: Rec) {
  const zero = r.a & 0xfffff,
    pdm = r.a >>> 20,
    full = r.b & 0xffffff;
  return (
    `芯片 ${(r.x << 24) >> 24} °C，麦克风全 0 ${zero} ms，PDM 错误 ${pdm} 次，满幅样本 ${full} 个，` +
    `最响 ${peak(r.b >>> 24)}`
  );
}
function cardId(r: Rec) {
  const name = ascii(r.a, r.a >>> 8, r.a >>> 16, r.a >>> 24, r.b);
  return `厂商 0x${hex(r.x, 2)}，OEM ${ascii(r.b >>> 8, r.b >>> 16)}，型号 ${name}，版本 ${r.b >>> 28}.${(r.b >>> 24) & 0xf}`;
}
const cardSize = (r: Rec) =>
  `${r.x ? "SDHC/SDXC" : "SDSC"}，${((r.a * 512) / 1e9).toFixed(2)} GB（${r.a} 扇区），` +
  `${2000 + (r.b >>> 4)} 年 ${r.b & 0xf} 月生产`;
const sdErrors = (r: Rec) =>
  `CRC 错 ${r.a & 0xffff} 次，超时 ${r.a >>> 16} 次（其中忙 ${r.b & 0xffff} 次），其他错 ${r.b >>> 16} 次，` +
  `重试 ${r.x} 次`;
function readDone(r: Rec) {
  const ms = r.b & 0x3fffff;
  return (
    `${kb(r.a)} KB，用时 ${(ms / 1000).toFixed(1)} s（${kb((r.a / Math.max(ms, 1)) * 1000)} KB/s），` +
    `每包 ${r.b >>> 22} B，连接间隔 ${r.x * 1.25} ms`
  );
}

// code: [name, what it means, its arguments in words]
const EVENTS: Record<number, [string, string, (r: Rec) => string]> = {
  1: [
    "BOOT",
    "开机",
    (r) =>
      `上次结束于 ${reas(r.a)}，电池 ${r.x * 20} mV，固件 ${hex(r.b & 0xfffffff, 7)}${(r.b >>> 28) & 1 ? "+" : ""}`,
  ],
  2: ["FAULT", "崩溃", (r) => `${FAULTS[r.a] ?? r.a}，PC 0x${hex(r.b, 8)}（上一次运行）`],
  3: [
    "MOUNT",
    "挂载存储卡",
    // no answer (ETIMEDOUT) or no device: no card, or one not seated (LR-P3-2420 without one, 10-07)
    (r) => `第 ${r.x} 次：${err(r.a)}${[-116, -19].includes(r.a | 0) ? "（没插卡或没插好）" : ""}`,
  ],
  4: [
    "STORE",
    "离线存储就绪",
    (r) =>
      `音频批次 ${r.a}（文件 ${hex(r.a, 4).toUpperCase()}xxxx），剩 ${Math.floor(r.b / 1024)} MB` +
      (r.x & 1 ? "；卡上的计数读不出" : "") + (r.x & 2 ? "；新计数没写进卡" : ""),
  ],
  5: ["SLOW", "写卡慢", (r) => `${r.a} ms（${r.b} B）`],
  6: ["APPEND_ERR", "写卡失败", (r) => `${APPEND[r.x] ?? ""}${err(r.a)}（${r.b} B）`],
  7: ["CARD_FULL", "存储卡满", (r) => `${r.x ? "停止录音" : "恢复录音"}，剩 ${r.a} KB`],
  8: ["USB_DISK", "U 盘模式", (r) => (r.x ? "卡借给 USB" : `卡收回：${err(r.a)}`)],
  9: ["CONNECT", "连上", (r) => param(r.a, r.b)],
  10: ["DISCONNECT", "断开", (r) => `0x${hex(r.x, 2)} ${HCI[r.x] ?? ""}`.trim()],
  11: ["PARAM", "连接参数", (r) => param(r.a, r.b)],
  12: ["MTU", "包长", (r) => `${r.x ? "链路数据长度" : "ATT MTU"} 发 ${r.a} 收 ${r.b}`],
  13: ["PHY", "PHY", (r) => `发 ${phy(r.a)} 收 ${phy(r.b)}`],
  14: ["TIME", "对时", (r) => clock(r.b * 2 ** 32 + r.a)],
  15: ["SECURITY", "加密", (r) => `等级 ${r.x}${r.a ? "，" + (SECURITY[r.a] ?? r.a) : ""}`],
  16: ["PAIRED", "配对", (r) => (r.x ? "已绑定" : "未绑定")],
  17: ["PAIR_FAIL", "配对失败", (r) => SECURITY[r.x] ?? String(r.x)],
  18: ["FORGET", "解除绑定", (r) => `${r.a} 个绑定：${r.x ? "过期的绑定" : "手机要求"}`],
  19: ["DROPPED", "丢帧", (r) => `${r.a} 帧，本次开机共 ${r.b}`],
  20: ["SEGMENT", "新文件", (r) => `${hex(r.a, 8).toUpperCase()}.BIN`],
  21: [
    "DFU",
    "进入升级",
    (r) => ({ 0x57: "UF2 bootloader", 0xa8: "OTA bootloader" })[r.x] ?? `0x${hex(r.x, 2)}`,
  ],
  22: [
    "HEARTBEAT",
    "心跳",
    (r) => `电池 ${r.x * 20} mV，剩 ${Math.floor(r.a / 1024)} MB，本次开机丢 ${r.b} 帧`,
  ],
  23: [
    "STACKS",
    "栈余量",
    (r) =>
      `codec ${stack(r.a >>> 16)}，pusher ${stack(r.a & 0xffff)}，store ${stack(r.b >>> 16)}，BT RX ${stack(r.b & 0xffff)} B`,
  ],
  24: [
    "SD_RESET",
    "SD 复位",
    (r) =>
      `CMD12 r1 0x${hex(r.a, 2)}，CMD0 r1 0x${hex(r.b, 2)}${r.b === 1 ? "（空闲）" : "（卡没响应）"}`,
  ],
  25: ["SD_WRITES", "写卡统计（10 分钟）", sdWrites],
  26: ["READ", "补传一次", readDone],
  27: ["READ_TIME", "补传耗时", (r) => `读卡 ${r.a} ms，蓝牙发送 ${r.b} ms`],
  28: [
    "SWITCH",
    "开关",
    (r) =>
      `${r.x ? "开" : "关"}：${["录音", "关机（System OFF，拨开或插 USB 唤醒）", "插着 USB，不录音", "重启"][r.a] ?? r.a}`,
  ],
  29: [
    "POWER",
    "电源",
    (r) =>
      [
        `电池供电，VDDH ${r.x * 20} mV`,
        "掉电预警：VDDH 低于 3.3 V 或 VDD 低于 2.8 V",
        `电量过低（${r.x * 20} mV）：停止录音，关机（System OFF），插 USB 或拨一下开关唤醒`,
      ][r.a] ?? `${r.a}`,
  ],
  30: [
    "LIVE_LOST",
    "实时漏发",
    (r) =>
      `${r.a} 条记录（${r.b} B）没能实时发出（${r.x ? "蓝牙断开" : "蓝牙卡住"}），卡上照存，手机从卡里补`,
  ],
  31: ["HEALTH", "麦克风和芯片（10 分钟）", health],
  32: ["CARD_ID", "存储卡型号", cardId],
  33: ["CARD_SIZE", "存储卡容量", cardSize],
  34: ["SD_ERRORS", "存储卡驱动出错（10 分钟）", sdErrors],
};
// every few minutes or every connection: hidden unless asked for
const ROUTINE = new Set([9, 11, 12, 13, 14, 15, 20, 22, 23, 25, 32, 33]);

export interface Said {
  name: string;
  label: string;
  text: string;
  level: "" | "warn" | "bad";
  routine: boolean;
}

export function describe(r: Rec): Said {
  const code = r.code & ~UPTIME;
  const [name, label, fmt] = EVENTS[code] ?? [
    `0x${hex(code, 2)}`,
    "未知事件",
    () => `x ${r.x} a ${r.a} b ${r.b}`,
  ];
  const bad =
    code === FAULT ||
    code === 6 ||
    code === 17 ||
    code === 19 ||
    (code === 3 && r.a !== 0) ||
    (code === 7 && r.x !== 0) ||
    (code === 24 && r.b !== 1) ||
    (code === 29 && r.a !== 0) || // the power-fail warning, a low battery's System OFF
    (code === BOOT && (r.a & 0b1010) !== 0); // the last boot ended by the watchdog or a lockup
  const warn =
    [BOOT, DISCONNECT, DFU, 5, 8, 18, 30, 34].includes(code) ||
    (code === 31 && (r.a & 0xfffff) + (r.a >>> 20) > 0); // the mic gave all-zero frames, or PDM errors
  return {
    name,
    label,
    text: fmt(r) + (r.code & UPTIME ? `（开机后 ${(r.t / 1000).toFixed(1)} s）` : ""),
    level: bad ? "bad" : warn ? "warn" : "",
    routine: ROUTINE.has(code) || (code === 29 && !r.a) || (code === 31 && !warn),
  };
}

/** The battery mV a BOOT, HEARTBEAT or POWER record carries (0: not read, as in the power-fail warning). */
export const millivolts = (r: Rec) =>
  [BOOT, HEARTBEAT, POWER].includes(r.code & ~UPTIME) ? r.x * 20 : 0;

/** The share of its 10 minutes the card spent on appends (%), by an SD_WRITES record; null for any other. */
export const cardBusy = (r: Rec) =>
  (r.code & ~UPTIME) === SD_WRITES ? Math.round((r.a & 0xfffff) / 600) / 10 : null;

const span = (ms: number) =>
  ms < 60_000
    ? `${Math.floor(ms / 1000)} 秒`
    : `${Math.floor(ms / 60_000)} 分 ${Math.floor(ms / 1000) % 60} 秒`;
const hash = (v: string | null) => v?.split("-").pop() ?? "?"; // revisions are YYYYMMDD.HHMMSS-<hash>

/** A firmware update in a line: 1751f88 → c95516b，用时 3 分 11 秒，成功. */
export function upgradeText(u: Upgrade): string {
  const where = { start: "进入升级模式前", boot: "传输前", send: `传输到 ${u.pct}%` }[
    u.stage ?? ""
  ];
  const end =
    u.ok == null
      ? `，${u.why ?? "还没有结果"}`
      : u.ok
        ? "，成功"
        : `，失败${where ? `（${where}）` : ""}${u.why ? "：" + u.why : ""}`;
  return (
    `${hash(u.from)} → ${hash(u.to)}${u.via === "usb" ? "（USB）" : ""}` +
    `${u.ms == null ? "" : "，用时 " + span(u.ms)}${end}` +
    (u.back_ms == null ? "" : `，${span(u.back_ms)}后连回`)
  );
}
