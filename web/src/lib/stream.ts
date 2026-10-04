// The BLE recorder's record stream (firmware src/lr_store.h) as this page follows it: one of the board's files from a
// record boundary on, the bytes of LIVE (and of READ, for a hole) put in place by offset; its 'A' records become runs
// of consecutive frames, dated by the boot's nearest 'T' record (the phone's clock the page wrote with TIME) + 10 ms a
// frame; 'T'-less frames wait for one. The phone app does the same with a mirror of every file (Store.kt).

const FRAME_MS = 10;

export class RecordStream {
  file = -1; // the file followed, from offset `base`
  private base = 0;
  private buf = new Uint8Array(0);
  private parsed = 0; // where the next record starts (in buf)
  private ahead = new Map<number, Uint8Array>(); // bytes past a hole, until it is filled
  private syncs = new Map<number, [number, number][]>(); // clock (boot; the file for boot 0) -> [frame, epoch ms]
  private run: Uint8Array[] = [];
  private first = -1;
  private held: { clock: number; first: number; frames: Uint8Array[] }[] = [];

  private emit: (frames: Uint8Array[], start: number) => void;
  private sliceFrames: number;

  constructor(emit: (frames: Uint8Array[], start: number) => void, sliceFrames: number) {
    this.emit = emit;
    this.sliceFrames = sliceFrames;
  }

  /** Follow `file` from `offset` (a record boundary: LIST's size, or 0); a run of the file before ends here. */
  follow(file: number, offset: number) {
    this.cut();
    this.file = file;
    this.base = offset;
    this.buf = new Uint8Array(0);
    this.parsed = 0;
    this.ahead.clear();
  }

  /** Where the bytes the page has end: a READ goes on from here. */
  end() {
    return this.base + this.buf.length;
  }

  /** Bytes of `file` at `offset`. Returns where a hole starts when they come after one (READ from there), else null. */
  put(file: number, offset: number, b: Uint8Array): number | null {
    if (file !== this.file) return null;
    if (offset > this.end()) {
      this.ahead.set(offset, b);
      return this.end();
    }
    this.append(offset, b);
    for (let more = true; more; ) {
      more = false;
      for (const [o, x] of this.ahead) {
        if (o > this.end()) continue;
        this.ahead.delete(o);
        this.append(o, x);
        more = true;
      }
    }
    this.parse();
    this.base += this.parsed; // what is parsed is in runs now: kept no longer
    this.buf = this.buf.slice(this.parsed);
    this.parsed = 0;
    return this.ahead.size ? this.end() : null;
  }

  private append(offset: number, b: Uint8Array) {
    const skip = this.end() - offset;
    if (skip >= b.length) return;
    const next = new Uint8Array(this.buf.length + b.length - skip);
    next.set(this.buf);
    next.set(b.subarray(skip), this.buf.length);
    this.buf = next;
  }

  private clock() {
    return this.file >>> 16 ? this.file >>> 16 : this.file;
  }

  private parse() {
    const b = this.buf,
      v = new DataView(b.buffer, b.byteOffset, b.byteLength);
    let i = this.parsed;
    for (;;) {
      if (b[i] === 0x41 && i + 6 <= b.length) {
        const n = b[1 + i],
          first = v.getUint32(i + 2, true),
          frames: Uint8Array[] = [];
        let j = i + 6;
        for (let k = 0; k < n; k++) {
          if (j >= b.length || j + 1 + b[j] > b.length) return;
          frames.push(b.slice(j + 1, j + 1 + b[j]));
          j += 1 + b[j];
        }
        if (this.run.length && first !== this.first + this.run.length) this.cut(); // frames lost on the way
        if (!this.run.length) this.first = first;
        this.run.push(...frames);
        if (this.run.length >= this.sliceFrames) this.cut();
        i = this.parsed = j;
      } else if (b[i] === 0x54 && i + 13 <= b.length) {
        const c = this.clock(),
          ts = this.syncs.get(c) ?? [];
        ts.push([v.getUint32(i + 1, true), Number(v.getBigUint64(i + 5, true))]);
        this.syncs.set(c, ts);
        i = this.parsed = i + 13;
        this.release();
      } else return;
    }
  }

  /** The run so far as a piece, if its boot has a 'T'; else it waits for one. */
  cut() {
    if (!this.run.length) return;
    this.held.push({ clock: this.clock(), first: this.first, frames: this.run });
    this.run = [];
    this.release();
  }

  private release() {
    this.held = this.held.filter(({ clock, first, frames }) => {
      const ts = this.syncs.get(clock);
      if (!ts?.length) return true;
      const [f, e] = ts.reduce((a, t) => (Math.abs(t[0] - first) < Math.abs(a[0] - first) ? t : a));
      this.emit(frames, e + (first - f) * FRAME_MS);
      return false;
    });
  }
}
