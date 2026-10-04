// The recorder's event log (firmware/devkit/src/lr_log.h), put in words as lr_sync.py's `log` does; kept in step with
// lr_log.h by hand: a code it does not know shows its raw arguments.
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
  HEARTBEAT = 22;

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
const stack = (v: number) => (v === 0xffff ? "-" : String(v));
const phy = (v: number) => ({ 1: "1M", 2: "2M", 4: "Coded" })[v] ?? String(v);
const clock = (ms: number) => new Date(ms).toLocaleString("zh-CN", { hour12: false });

// code: [name, what it means, its arguments in words]
const EVENTS: Record<number, [string, string, (r: Rec) => string]> = {
  1: [
    "BOOT",
    "开机",
    (r) =>
      `上次结束于 ${reas(r.a)}，电池 ${r.x * 20} mV，固件 ${hex(r.b & 0xfffffff, 7)}${(r.b >>> 28) & 1 ? "+" : ""}`,
  ],
  2: ["FAULT", "崩溃", (r) => `${FAULTS[r.a] ?? r.a}，PC 0x${hex(r.b, 8)}（上一次运行）`],
  3: ["MOUNT", "挂载存储卡", (r) => `第 ${r.x} 次：${err(r.a)}`],
  4: [
    "STORE",
    "离线存储就绪",
    (r) =>
      `音频批次 ${r.a}（文件 ${hex(r.a, 4).toUpperCase()}xxxx），剩 ${Math.floor(r.b / 1024)} MB`,
  ],
  5: ["SLOW", "写卡慢", (r) => `${r.a} ms（${r.b} B）`],
  6: ["APPEND_ERR", "写卡失败", (r) => `${err(r.a)}（${r.b} B）`],
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
};
// every few minutes or every connection: hidden unless asked for
const ROUTINE = new Set([9, 11, 12, 13, 14, 15, 20, 22, 23]);

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
    (code === BOOT && (r.a & 0b1010) !== 0); // the last boot ended by the watchdog or a lockup
  const warn = [BOOT, DISCONNECT, DFU, 5, 8, 18].includes(code);
  return {
    name,
    label,
    text: fmt(r) + (r.code & UPTIME ? `（开机后 ${(r.t / 1000).toFixed(1)} s）` : ""),
    level: bad ? "bad" : warn ? "warn" : "",
    routine: ROUTINE.has(code),
  };
}

/** The battery mV a BOOT or HEARTBEAT record carries (0: not read). */
export const millivolts = (r: Rec) => ([BOOT, HEARTBEAT].includes(r.code & ~UPTIME) ? r.x * 20 : 0);
