// Opus packets (16 kHz mono, as the BLE recorder sends them) into one Ogg Opus file (RFC 7845), so a piece
// POSTs to /api/chunks like the apps' pieces. No re-encoding: each BLE frame is one Ogg packet.

const CRC = new Uint32Array(256).map((_, i) => {
  let r = i << 24;
  for (let k = 0; k < 8; k++) r = r & 0x80000000 ? (r << 1) ^ 0x04c11db7 : r << 1;
  return r >>> 0;
});

function page(
  packets: Uint8Array[],
  flags: number,
  granule: number,
  serial: number,
  seq: number,
): Uint8Array {
  const lacing: number[] = [];
  for (const p of packets) {
    for (let n = p.length; ; n -= 255) {
      lacing.push(Math.min(n, 255));
      if (n < 255) break;
    }
  }
  const body = packets.reduce((s, p) => s + p.length, 0);
  const out = new Uint8Array(27 + lacing.length + body);
  const v = new DataView(out.buffer);
  out.set([0x4f, 0x67, 0x67, 0x53, 0, flags]); // "OggS", version 0
  v.setUint32(6, granule % 0x100000000, true);
  v.setUint32(10, Math.floor(granule / 0x100000000), true);
  v.setUint32(14, serial, true);
  v.setUint32(18, seq, true);
  out[26] = lacing.length;
  out.set(lacing, 27);
  let o = 27 + lacing.length;
  for (const p of packets) out.set(p, (o += p.length) - p.length);
  let crc = 0;
  for (const b of out) crc = ((crc << 8) ^ CRC[((crc >>> 24) ^ b) & 0xff]) >>> 0;
  v.setUint32(22, crc, true);
  return out;
}

/** frames: Opus packets of `frameMs` each. Granule positions count 48 kHz samples, as RFC 7845 asks. */
export function oggOpus(frames: Uint8Array[], frameMs = 10, rate = 16000): Blob {
  const head = new Uint8Array(19);
  head.set(new TextEncoder().encode("OpusHead"));
  head[8] = 1; // version
  head[9] = 1; // channels
  new DataView(head.buffer).setUint32(12, rate, true); // pre-skip 0, gain 0, mapping 0
  const tags = new Uint8Array(16);
  tags.set(new TextEncoder().encode("OpusTags")); // empty vendor, no comments
  const serial = (Math.random() * 0x100000000) >>> 0;
  const pages = [page([head], 2, 0, serial, 0), page([tags], 0, 0, serial, 1)];
  const per = 50; // half a second per page
  for (let i = 0; i < frames.length; i += per) {
    const last = i + per >= frames.length;
    pages.push(
      page(
        frames.slice(i, i + per),
        last ? 4 : 0,
        Math.min(i + per, frames.length) * frameMs * 48,
        serial,
        pages.length,
      ),
    );
  }
  return new Blob(pages as BlobPart[], { type: "audio/ogg" });
}
