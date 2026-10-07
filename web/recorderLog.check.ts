// Check of src/lib/recorderLog.ts against lr_log.h's packed events (the same values as lr_sync.py's selftest), and
// firmware updates in a line (the server's upgrades() as test_device_log.py has them):
//   node --experimental-strip-types web/recorderLog.check.ts
import { describe, upgradeText } from "./src/lib/recorderLog.ts";

const said = (code: number, x: number, a: number, b: number) =>
  describe({ boot: 1, t: 100, code, x, a, b }).text;
const check = (got: string, want: string) => {
  if (got !== want) throw new Error(`${got} != ${want}`);
};
check(
  said(25, 1, 601 | (250 << 20), 4 | (2 << 10) | (2 << 20)),
  "9 次，共 601 ms，最长 250 ms；<20 ms 4，20–49 ms 2，50–200 ms 2，>200 ms 1",
);
check(
  said(25, 0, (0xfffff | (0xfff << 20)) >>> 0, 0),
  "0 次，共 1048575 ms，最长 4095 ms；<20 ms 0，20–49 ms 0，50–200 ms 0，>200 ms 0",
); // a ≥ 2^31: unsigned
check(
  said(26, 12, 409600, (10000 | (491 << 22)) >>> 0),
  "400.0 KB，用时 10.0 s（40.0 KB/s），每包 491 B，连接间隔 15 ms",
);
check(said(27, 0, 3000, 6500), "读卡 3000 ms，蓝牙发送 6500 ms");
const up = {
  at: "",
  app: true,
  from: "20261006.120000-1751f88",
  to: "20261007.023223-c95516b",
  why: null,
};
check(
  upgradeText({ ...up, app: false, from: "1751f88", to: "c95516b", ms: 192_000, ok: true }),
  "1751f88 → c95516b，用时 3 分 12 秒，成功",
);
check(
  upgradeText({ ...up, ms: 180_000, ok: true, back_ms: 30_000 }),
  "1751f88 → c95516b，用时 3 分 0 秒，成功，30 秒后连回",
);
check(
  upgradeText({ ...up, ms: 60_000, ok: false, why: "Device disconnected", stage: "send", pct: 40 }),
  "1751f88 → c95516b，用时 1 分 0 秒，失败（传输到 40%）：Device disconnected",
);
check(
  upgradeText({ ...up, ms: 20_500, ok: false, why: "连不上录音设备", stage: "start", pct: 0 }),
  "1751f88 → c95516b，用时 20 秒，失败（进入升级模式前）：连不上录音设备",
);
check(upgradeText({ ...up, from: null, ms: null, ok: null }), "? → c95516b，还没有结果");
check(
  upgradeText({
    ...up,
    app: false,
    from: "28758c4",
    to: "28758c4",
    ms: 60_000,
    ok: null,
    why: "重启后版本没变",
    via: "usb",
  }),
  "28758c4 → 28758c4（USB），用时 1 分 0 秒，重启后版本没变",
);
console.log("recorderLog check ok");
