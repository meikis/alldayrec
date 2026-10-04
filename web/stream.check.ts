// Check of src/lib/stream.ts (the page following the BLE recorder's record stream):
//   node --experimental-strip-types web/stream.check.ts
import { RecordStream } from "./src/lib/stream.ts";

const le = (n: number, bytes: number) => Array.from({ length: bytes }, (_, i) => Number((BigInt(n) >> BigInt(8 * i)) & 0xffn));
const a = (first: number, n: number) => [0x41, n, ...le(first, 4), ...Array.from({ length: n }, (_, i) => [2, i, i]).flat()];
const t = (frame: number, epoch: number) => [0x54, ...le(frame, 4), ...le(epoch, 8)];
const pieces: [number, number][] = [];
const s = new RecordStream((frames, start) => pieces.push([start, frames.length]), 15);
const check = (ok: boolean, what: string) => {
  if (!ok) throw new Error(what + ": " + JSON.stringify(pieces));
};

// from offset 100 of file 1<<16 (LIST said the card has 100 B of it): no 'T' yet, frames wait
const f = 1 << 16;
s.follow(f, 100);
const b = new Uint8Array([...a(0, 10), ...a(10, 10), ...t(20, 1_000_000), ...a(20, 10), ...a(40, 10)]);
check(s.put(f, 100, b.subarray(0, 40)) === null, "taken");
check(s.put(f, 160, b.subarray(60, 80)) === 140, "a hole: READ from 140");
check(pieces.length === 0, "no 'T' yet");
check(s.put(f, 130, b.subarray(30)) === null, "the hole filled by READ, overlapping");
// 0..19 at 15+ frames, 20..29 ended by the gap; 40..49 wait for more
check(JSON.stringify(pieces) === JSON.stringify([[999_800, 20], [1_000_000, 10]]), "dated by the 'T'");
s.cut(); // stop
check(pieces.length === 3 && pieces[2][0] === 1_000_200, "the last run on stop");
console.log("stream check ok");
