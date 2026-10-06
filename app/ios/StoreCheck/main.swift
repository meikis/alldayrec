// The record stream as the phone takes it (Runner/Store.swift), the cases of the Android app's StoreTest.kt and the data
// of firmware/devkit/lr_sync.py's selftest: mirror (LIVE with holes READ again), pieces dated by the nearest 'T', frames
// held for a boot without one, closed files finished and deletable once their pieces are uploaded; and the recorder's
// event log (Runner/DeviceLog.swift, DeviceLogTest.kt's case). Foundation only, so CI runs it on Linux:
//   cd app/ios && swiftc -o /tmp/storecheck Runner/Store.swift Runner/DeviceLog.swift StoreCheck/main.swift && /tmp/storecheck
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

// #109: a piece appends one line to the state instead of writing all of it again (at 1 s pieces that grew with every
// piece of the open file); a restart reads the lines back, finds the 'T' again and goes on where the last piece ended
do {
  let dir = tmp(), out = tmp(), undated = tmp()
  let f: Int64 = 1 << 16 | 1, state = dir.appendingPathComponent("state.txt").path
  func file() -> (ino: UInt64, size: UInt64) {
    let a = try! FileManager.default.attributesOfItem(atPath: state)
    return ((a[.systemFileNumber] as! NSNumber).uint64Value, (a[.size] as! NSNumber).uint64Value)
  }
  let m = Mirror(dir)
  var off = Int64(t(0, 1_000_000).count)
  _ = try m.put(f, 0, t(0, 1_000_000))
  m.listed([f: off], open: f)  // as LIST leaves it
  let before = file()
  for s in 0..<60 {  // a minute in 1 s pieces (the foreground slice), one pieces() a second
    for i in 0..<10 { let r = a(s * 100 + i * 10, 10); _ = try m.put(f, off, r); off += Int64(r.count) }
    check(try m.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 100, boot: 1), 1, "second \(s): one piece")
  }
  check(file().ino, before.ino, "the state file appended to, never replaced")
  check((file().size - before.size) / 60 < 40, true, "a line a piece")
  let m2 = Mirror(dir)
  check(m2.files[f]!.cut, m.files[f]!.cut, "cut after a restart")
  check(m2.files[f]!.pieces, m.files[f]!.pieces, "pieces after a restart")
  check(try m2.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 100, boot: 1), 0, "nothing made again")
  for i in 0..<10 { let r = a(6000 + i * 10, 10); _ = try m2.put(f, off, r); off += Int64(r.count) }
  check(try m2.pieces(out, undated, tag: "LR-P1-BDAE", liveFrames: 100, boot: 1), 1, "the next second")
  check(names(out).count, 61, "61 pieces, none twice")
  check(names(out).last, "1060000@LR-P1-BDAE.ogg", "dated by the 'T' scanned again")
  m2.listed([f: off], open: f)  // the next whole save folds the lines in
  check(try String(contentsOfFile: state, encoding: .utf8).contains("\nC "), false, "folded")
  check(Mirror(dir).files[f]!.pieces.count, 61, "and read back")
}

// ogg
do {
  let ogg = Store.oggOpus(Array(repeating: Data([0xF8, 0xFF, 0xFE]), count: 120))  // 120 silent 10 ms Opus frames
  check(count(ogg, "OggS"), 5, "pages: head, tags, 50 + 50 + 20 frames")
}

// the recorder's event log (DeviceLog.swift), the case of DeviceLogTest.kt and lr_sync.py --selftest: boot 7's TIME fell
// out of the log, boot 8's frames are dated by its TIME at frame 1000; LOG packets out of order, one sent twice
do {
  let T: Int64 = 1_700_000_000_000
  func r(_ boot: Int, _ t: Int, _ code: Int, _ x: Int = 0, _ a: Int64 = 0, _ b: Int64 = 0) -> [UInt8] {
    Store.le(UInt16(boot)) + Store.le(UInt32(t)) + [UInt8(code), UInt8(x)] + Store.le(UInt32(truncatingIfNeeded: a)) + Store.le(UInt32(truncatingIfNeeded: b))
  }
  func pkt(_ seq: Int, _ recs: [UInt8]...) -> [UInt8] { Store.le(UInt32(seq)) + recs.flatMap { $0 } }
  let packets = [pkt(5003, r(8, 400, 9, 0, 24, 400), r(8, 1000, 14, 0, T & 0xffff_ffff, T >> 32)),
                 pkt(5000, r(7, 123456, 22, 200, 2048, 7), r(8, 1500, 1 | 0x80, 205, 0x4, 0x1abcdef | 1 << 28)),
                 pkt(5002, r(8, 3200, 3 | 0x80, 1, -5)),
                 pkt(5003, r(8, 400, 9, 0, 24, 400)),
                 pkt(5005, r(8, 2000, 10, 0x13), r(8, 2100, 0x63, 1, 2, 3))]
  let recs = DeviceLog.records(packets)
  check(recs.map(\.seq), Array(5000...5006), "records by seq")
  check(recs[4], DeviceLog.Rec(seq: 5004, boot: 8, t: 1000, code: 14, x: 0, a: T & 0xffff_ffff, b: T >> 32), "TIME record")
  check(recs[2], DeviceLog.Rec(seq: 5002, boot: 8, t: 3200, code: 0x83, x: 1, a: 0xffff_fffb, b: 0), "u32 fields stay unsigned")
  check(recs[1].b, 0x1abcdef | 1 << 28, "b")
  // boot 7 undated; boot 8's uptime records at its frame 0 (T - 10 s); frames 10 ms each from the TIME at 1000
  check(DeviceLog.dates(recs, known: [:]), [nil, T - 10_000, T - 10_000, T - 6_000, T, T + 10_000, T + 11_000], "dates")
  // a later read of the same boot, its TIME known from the read before
  let later = DeviceLog.records([pkt(5007, r(8, 61_000, 22, 190))])
  check(DeviceLog.dates(later, known: [8: (1000, T)]), [T + 600_000], "dated by an earlier read's TIME")
  check(DeviceLog.dates(later, known: [7: (1000, T)]), [nil], "another boot's TIME: no")
  check(DeviceLog.cursor(recs, next: 5007), 5007, "all there")
  check(DeviceLog.cursor(recs.filter { $0.seq != 5002 }, next: 5007), 5002, "a notify lost: read again from it")
  check(DeviceLog.cursor([], next: 5007), 5007, "nothing new")
}

// why an upload failed (DeviceLog.why): the iPhone's "bad URL" of 10-06 as CFNetwork reports one, and a server address
// that is no URL; the host and the codes, never the token or the query
do {
  let path = "satisfied (Path is satisfied), interface: en0[802.11], ipv4, dns, proxy, uses wifi"
  let under = NSError(domain: "kCFErrorDomainCFNetwork", code: -1000, userInfo: ["_NSURLErrorNWPathKey": path])
  let bad = NSError(domain: "NSURLErrorDomain", code: -1000, userInfo: [
    NSLocalizedDescriptionKey: "bad URL", NSUnderlyingErrorKey: under, "_kCFStreamErrorDomainKey": 1, "_kCFStreamErrorCodeKey": 22,
    "NSErrorFailingURLStringKey": "https://lr.example.com/api/chunks?device=LR%2DP1%2DBDAE&start=1791246194000&token=secret"])
  check(DeviceLog.why(bad, server: "https://lr.example.com"),
        "bad URL（lr.example.com NSURLErrorDomain -1000，底层 kCFErrorDomainCFNetwork -1000，流 1/22，网络 \(path)）", "CFNetwork's bad URL")
  let own = NSError(domain: "NSURLErrorDomain", code: -1000, userInfo: [NSLocalizedDescriptionKey: "x"])
  check(DeviceLog.why(own, server: "https://lr.example.com \n"), "x（\"https://lr.example.com \\n\" NSURLErrorDomain -1000）",
        "an address with a space and a newline, as typed")
  check(DeviceLog.why(own, server: ""), "x（\"\" NSURLErrorDomain -1000）", "no address")
}

#if canImport(ObjectiveC)
// #109: the BLE session's loop (Ble.swift) on a thread that lives for hours: LIVE bytes into the mirror, then pieces().
// Foundation autoreleases what Mirror reads (FileHandle.readToEnd), so without a pool per pass the thread keeps all of
// it: 3.4 GB on the iPhone 10-06. Only on a Mac: Linux has no autorelease pools (CI runs the rest there).
do {
  func footprintMB() -> Double {
    var info = task_vm_info_data_t()
    var n = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    _ = withUnsafeMutablePointer(to: &info) { $0.withMemoryRebound(to: integer_t.self, capacity: Int(n)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &n) } }
    return Double(info.phys_footprint) / 1_048_576
  }
  func growth(pool: Bool) -> Double {
    let dir = tmp(), out = tmp(), m = Mirror(dir)
    let f: Int64 = 1 << 16 | 1
    var off = Int64(t(0, 1_000_000).count), frame = 0, mb = 0.0
    _ = try! m.put(f, 0, t(0, 1_000_000))
    let done = DispatchSemaphore(value: 0)
    Thread.detachNewThread {
      let before = footprintMB()
      for i in 0..<3000 {  // 5 min of audio in 100 ms records of 40-byte frames, 20 s pieces (the background slice)
        let pass = {
          let rec = Data([0x41, 10] + Store.le(UInt32(frame)) + (0..<10).flatMap { _ in [40] + [UInt8](repeating: 0x55, count: 40) })
          _ = try! m.put(f, off, rec); off += Int64(rec.count); frame += 10
          _ = try! m.pieces(out, out, tag: "LR-P1-BDAE", liveFrames: 2000, boot: 1)
          if i % 200 == 0 { clear(out) }  // uploaded
        }
        if pool { autoreleasepool(invoking: pass) } else { pass() }
      }
      mb = footprintMB() - before
      done.signal()
    }
    done.wait()
    return mb
  }
  let with = growth(pool: true), without = growth(pool: false)  // this order: a finished thread drains its pool
  print(String(format: "session loop, 5 min of audio: %.0f MB kept without a pool per pass, %.1f MB with", without, with))
  check(with < 10, true, "a pool per pass keeps the session's memory flat")
  check(without > 5 * max(with, 2), true, "without one it grows (else this check proves nothing)")
}
#endif

print("storecheck ok")
