// node --experimental-strip-types web/stream.check.ts
import { readFileSync } from "node:fs";
import { RecordStream } from "./src/lib/stream.ts";

const fixture = JSON.parse(readFileSync(new URL("../fixtures/record-stream.json", import.meta.url), "utf8"));
const v = fixture.mirror;
const bytes = Uint8Array.from(Buffer.from(v.hex, "hex"));
const pieces: [number, number][] = [];
const check = (ok: boolean, what: string) => { if (!ok) throw new Error(what + ": " + JSON.stringify(pieces)); };
const s = new RecordStream((frames, start) => pieces.push([start, frames.length]), v.liveFrames);
s.follow(v.file, 0);
for (const d of v.deliveries) {
  check(s.put(v.file, d.start, bytes.subarray(d.start, d.end)) === d.hole, "shared delivery hole");
}
check(JSON.stringify(pieces) === JSON.stringify(v.pieces.slice(0, -1)), "shared dated open runs");
s.cut();
check(JSON.stringify(pieces) === JSON.stringify(v.pieces), "shared final run");
// RecordStream has no persistent mirror, undated archive or boot-reuse API (see fixture.notes).
console.log("stream check ok");
