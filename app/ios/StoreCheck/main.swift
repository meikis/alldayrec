// The record stream as the phone takes it (Runner/Store.swift), the cases of the Android app's StoreTest.kt and the data
// of firmware/devkit/lr_sync.py's selftest: mirror (LIVE with holes READ again), pieces dated by the nearest 'T', frames
// held for a boot without one, closed files finished and deletable once their pieces are uploaded. Foundation only, so
// CI runs it on Linux:
//   swiftc -o /tmp/storecheck app/ios/Runner/Store.swift app/ios/StoreCheck/main.swift && /tmp/storecheck
import Foundation

func check<T: Equatable>(_ got: T, _ want: T, _ what: String, line: Int = #line) {
  if got != want { print("FAIL line \(line) \(what): got \(got), want \(want)"); exit(1) }
}

func a(_ first: Int, _ n: Int) -> Data {
  Data([0x41, UInt8(n)] + Store.le(UInt32(first)) + (0..<n).flatMap { [2, UInt8($0), UInt8($0)] })
}
func t(_ frame: Int, _ epoch: Int64) -> Data { Data([0x54] + Store.le(UInt32(frame)) + Store.le(UInt64(epoch))) }
func tmp() -> URL {
  let d = FileManager.default.temporaryDirectory.appendingPathComponent("storecheck-\(UUID().uuidString)")
  try! FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
  return d
}
func names(_ out: URL) -> [String] {
  ((try? FileManager.default.contentsOfDirectory(atPath: out.path)) ?? []).filter { $0.hasSuffix(".ogg") }
    .sorted { Int64($0.split(separator: "@")[0])! < Int64($1.split(separator: "@")[0])! }
}
func clear(_ d: URL) { for n in (try? FileManager.default.contentsOfDirectory(atPath: d.path)) ?? [] { try? FileManager.default.removeItem(at: d.appendingPathComponent(n)) } }
func count(_ hay: Data, _ needle: String) -> Int {
  let h = [UInt8](hay), n = [UInt8](needle.utf8)
  return (0...(h.count - n.count)).filter { Array(h[$0..<($0 + n.count)]) == n }.count
}

// mirror
do {
  let dir = tmp(), out = tmp(), undated = tmp()
  let f: Int64 = 1 << 16 | 3  // boot 1, segment 3: the open file
  let stream = a(0, 10) + a(10, 10) + t(20, 1_000_000) + a(20, 10) + a(40, 10)  // frames 30..39 lost on the way
  let m = Mirror(dir)
  check(try m.put(f, 0, stream.subdata(in: 0..<40)), nil, "first bytes")
  check(try m.put(f, 60, stream.subdata(in: 60..<80)), 40, "a lost notify: READ from 40")
  check(try m.put(f, 30, stream.subdata(in: 30..<stream.count)), nil, "READ overlapping what is there")
  check(m.have(f), Int64(stream.count), "mirrored")
  m.listed([f: 80], open: f)

  // the open file: pieces of >= 15 frames at record ends, a gap ends one, the last frames wait
  check(try m.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 1), 2, "open file pieces")
  check(names(out), ["999800@LR-P1-BDAE.ogg", "1000000@LR-P1-BDAE.ogg"], "open file names")

  // the stream moved on (the board rebooted: boot 2): this file is closed and whole, its last frames go too
  let g: Int64 = 2 << 16
  check(try m.put(g, 0, a(0, 5)), nil, "boot 2, no 'T' yet")
  m.listed([f: Int64(stream.count), g: 21], open: g)
  check(try m.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 2), 1, "closed file's last piece")
  check(names(out).last, "1000200@LR-P1-BDAE.ogg", "last piece")
  check(m.done(out), [], "pieces not uploaded yet")
  clear(out)  // the uploader took them
  check(m.done(out), [f], "deletable")

  // state survives a restart; boot 2 gets its 'T' later in the stream and its frames go then
  let m2 = Mirror(dir)
  check(m2.done(out), [f], "after a restart")
  check(try m2.put(g, 21, t(5, 2_000_000)), nil, "boot 2's 'T'")
  check(try m2.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 2), 0, "5 frames of the open file: wait for more")
  check(try m2.put(g, 34, a(5, 10)), nil, "more frames")
  check(try m2.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 2), 1, "boot 2 dated")
  check(names(out), ["1999950@LR-P1-BDAE.ogg"], "boot 2 piece")
  m2.drop(f)
  check(m2.have(f), 0, "dropped")
}

// undated
do {
  let dir = tmp(), out = tmp(), undated = tmp()
  let m = Mirror(dir)
  let f: Int64 = 4 << 16  // boot 4 lost power before any phone came: no 'T', and boot 5 runs now
  _ = try m.put(f, 0, a(0, 10) + a(20, 3))  // two runs (frames 10..19 lost): one undated file
  m.listed([f: 51, 5 << 16: 0, 3 << 16: 0], open: 5 << 16)  // boot 3's file: listed, empty
  check(try m.pieces(out, undated, tag: "LR-X1-567E", liveFrames: 500, boot: 5), 0, "nothing dated")
  check(try FileManager.default.contentsOfDirectory(atPath: undated.path), ["LR-X1-567E-00040000.ogg"], "one undated file")
  check(m.done(out), [3 << 16, f], "the empty file too: nothing in it, nothing to wait for")
}

// a full LIST
do {
  let m = Mirror(tmp())
  let old: Int64 = 6 << 16, open: Int64 = 7 << 16 | 2
  _ = try m.put(old, 0, a(0, 10))
  // 64 other files: a full LIST, which may have left out `old` and the open file: neither is closed
  m.listed(Dictionary(uniqueKeysWithValues: (0..<64).map { (Int64(1 << 16 | $0), Int64(0)) }), open: open)
  check(m.files[old]!.closed, false, "full LIST: old not closed")
  check(m.files[open]!.closed, false, "full LIST: open not closed")
  m.listed([open: 0], open: open)  // a short LIST: `old` is gone from the card
  check(m.files[old]!.closed, true, "short LIST: old closed")
}

// lr_sync.py's selftest data. boot 1: a gap at 5..8, a cut-short tail; its 'T' in the middle of the stream, after the
// frames it dates. boot 2: rebooted, never synced: time unknown. boot 3: the open file, its 'T' (this connection's) last
do {
  let dir = tmp(), out = tmp(), undated = tmp()
  let files: [Int64: Data] = [1 << 16: a(0, 3) + a(3, 2) + t(4, 10_000) + a(9, 1), 1 << 16 | 1: a(10, 1).dropLast(),
                              2 << 16: a(0, 2), 3 << 16: a(40, 10) + t(50, 99_000)]
  let m = Mirror(dir)
  for (f, b) in files { check(try m.put(f, 0, b), nil, "file \(f)") }
  m.listed(files.mapValues { Int64($0.count) }, open: 3 << 16)
  check(try m.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 3), 2, "boot 1's runs")
  check(names(out), ["9960@LR-P1-BDAE.ogg", "10050@LR-P1-BDAE.ogg"], "dated by a 'T' after them")
  check(try FileManager.default.contentsOfDirectory(atPath: undated.path), ["LR-P1-BDAE-00020000.ogg"], "boot 2 undated")
  m.listed(files.mapValues { Int64($0.count) }.merging([3 << 16 | 1: 0]) { $1 }, open: 3 << 16 | 1)  // the stream moved on
  check(try m.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 15, boot: 3), 1, "boot 3's run once its file closed")
  check(names(out).last, "98900@LR-P1-BDAE.ogg", "boot 3 dated by the 'T' after it")
}

// ogg
do {
  let ogg = Store.oggOpus(Array(repeating: Data([0xF8, 0xFF, 0xFE]), count: 120))  // 120 silent 10 ms Opus frames
  check(count(ogg, "OggS"), 5, "pages: head, tags, 50 + 50 + 20 frames")
}

print("storecheck ok")
