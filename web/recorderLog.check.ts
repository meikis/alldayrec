// Check of src/lib/recorderLog.ts against lr_log.h's packed events (the same values as lr_sync.py's selftest):
//   node --experimental-strip-types web/recorderLog.check.ts
import { describe } from "./src/lib/recorderLog.ts";

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
console.log("recorderLog check ok");
