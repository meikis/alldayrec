// A fake LR recorder on the Mac (CBPeripheralManager): the record stream of firmware/devkit/src/lr_store.h, enough to
// drive the iPhone app's BLE link end to end without a board (run.sh). Run in a work directory holding tone.ogg (10 ms
// Opus frames, replayed as the mic's); the card lives in ./card and survives a restart: the frames of the time the fake
// was down are written on start, as a board records while the phone is away. A new card has a boot that never got a
// 'T' (undated), a closed segment and an open one with frames the phone has not seen. Unlike a board: no battery or
// device information service (macOS will not publish 180F/180A), no encryption (no pairing dialog to tap), FORGET does
// not drop the link.
//   swiftc -O -o fake fake.swift && ./fake [NAME]      env DROP=37 (every Nth LIVE record lost), ROLL=90 (s a segment)
import CoreBluetooth
import Foundation

setbuf(stdout, nil)
let name = CommandLine.arguments.dropFirst().first ?? "LR-MAC-BEEF"
let dropEvery = Int(ProcessInfo.processInfo.environment["DROP"] ?? "37")!   // every Nth LIVE record lost on the way
let rollS = Double(ProcessInfo.processInfo.environment["ROLL"] ?? "90")!     // a new segment this often
let boot: UInt32 = 5
let cardDir = URL(fileURLWithPath: "card")
func now() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
func le<T: FixedWidthInteger>(_ v: T) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
func u32(_ d: Data, _ i: Int) -> UInt32 { d.subdata(in: (d.startIndex + i)..<(d.startIndex + i + 4)).withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) } }
func log(_ s: String) { print("\(Date().formatted(.iso8601.time(includingFractionalSeconds: true))) \(s)") }

let opus: [Data] = {  // the packets of tone.ogg, its OpusHead and OpusTags left out
  let b = [UInt8](try! Data(contentsOf: URL(fileURLWithPath: "tone.ogg")))
  var out: [Data] = [], cur: [UInt8] = [], i = 0
  while i + 27 <= b.count {
    let n = Int(b[i + 26])
    var j = i + 27 + n
    for l in b[(i + 27)..<(i + 27 + n)] {
      cur += b[j..<(j + Int(l))]
      j += Int(l)
      if l < 255 { out.append(Data(cur)); cur = [] }
    }
    i = j
  }
  return Array(out.dropFirst(2))
}()
func record(_ first: UInt32, _ n: Int) -> Data {
  var r = Data([0x41, UInt8(n)]) + le(first)
  for k in 0..<n { let p = opus[Int(first + UInt32(k)) % opus.count]; r.append(UInt8(p.count)); r += p }
  return r
}
func uuid(_ n: Int) -> CBUUID { CBUUID(string: String(format: "7c3e%04x-1a2b-4c5d-8e9f-6c6976657265", n)) }

final class Fake: NSObject, CBPeripheralManagerDelegate {
  let q = DispatchQueue(label: "fake")
  var pm: CBPeripheralManager!
  let time = CBMutableCharacteristic(type: uuid(1), properties: [.read, .write], value: nil, permissions: [.readable, .writeable])
  let ctrl = CBMutableCharacteristic(type: uuid(2), properties: [.write, .notify], value: nil, permissions: [.writeable])
  let data = CBMutableCharacteristic(type: uuid(3), properties: [.notify], value: nil, permissions: [])
  let live = CBMutableCharacteristic(type: uuid(4), properties: [.notify], value: nil, permissions: [])
  let battery = CBMutableCharacteristic(type: CBUUID(string: "2A19"), properties: [.read], value: Data([87]), permissions: [.readable])
  let model = CBMutableCharacteristic(type: CBUUID(string: "2A24"), properties: [.read], value: Data("LR-MAC".utf8), permissions: [.readable])
  let fw = CBMutableCharacteristic(type: CBUUID(string: "2A26"), properties: [.read], value: Data("20261005.000000-e2e".utf8), permissions: [.readable])
  var files: [UInt32: Data] = [:]
  var open: UInt32 = boot << 16 | 1
  var frame: UInt32 = 0          // the codec's frame number since boot
  var pausedUntil: Int64 = 0
  var liveCount = 0
  var subscribed = Set<CBUUID>()
  var mtu = 20
  var queue: [(CBMutableCharacteristic, Data)] = []
  var rolled = Date()
  var events: [Data] = []  // the event log (lr_log.h records, 16 B), seq = 7000 + index
  func event(_ code: UInt8, _ a: UInt32 = 0, _ b: UInt32 = 0) { events.append(le(UInt16(boot)) + le(frame) + Data([code, 0]) + le(a) + le(b)) }

  override init() {
    super.init()
    load()
    pm = CBPeripheralManager(delegate: self, queue: q)
    q.async { self.tick() }
  }

  // the card
  func load() {
    try? FileManager.default.createDirectory(at: cardDir, withIntermediateDirectories: true)
    if let st = try? JSONSerialization.jsonObject(with: Data(contentsOf: cardDir.appendingPathComponent("state.json"))) as? [String: Any] {
      for n in try! FileManager.default.contentsOfDirectory(atPath: cardDir.path) where n.hasSuffix(".BIN") {
        files[UInt32(n.prefix(8), radix: 16)!] = try! Data(contentsOf: cardDir.appendingPathComponent(n))
      }
      open = UInt32(st["open"] as! Int); frame = UInt32(st["frame"] as! Int)
      let away = Int((now() - (st["at"] as! Int64)) / 10) / 10 * 10  // what the board recorded while the fake was down
      for _ in 0..<(away / 10) { files[open, default: Data()] += record(frame, 10); frame += 10 }
      log("card loaded: \(files.mapValues(\.count)), recorded \(away) frames while down, frame \(frame)")
    } else {
      // boot 4: never synced (undated); boot 5 segment 0: closed, dated by this boot's 'T' to come; segment 1: open
      files[4 << 16] = (0..<30).map { record(UInt32($0 * 10), 10) }.reduce(Data(), +)
      files[boot << 16] = (0..<50).map { record(UInt32($0 * 10), 10) }.reduce(Data(), +)
      files[open] = (50..<80).map { record(UInt32($0 * 10), 10) }.reduce(Data(), +)
      frame = 800
      log("new card: \(files.mapValues(\.count)), frame \(frame)")
    }
    save()
  }

  func save() {
    for (f, b) in files { try! b.write(to: cardDir.appendingPathComponent(String(format: "%08X.BIN", f))) }
    let st: [String: Any] = ["open": open, "frame": frame, "at": now()]
    try! JSONSerialization.data(withJSONObject: st).write(to: cardDir.appendingPathComponent("state.json"))
  }

  // the stream: a record per 10 frames (100 ms of the mic's clock, kept to the wall clock), to the card and to a
  // following phone
  lazy var clock0 = (at: now(), frame: frame)
  func tick() {
    defer { q.asyncAfter(deadline: .now() + 0.02) { self.tick() } }
    while Int64(frame - clock0.frame) * 10 + 100 <= now() - clock0.at {
      if -rolled.timeIntervalSinceNow > rollS {
        rolled = Date()
        open += 1
        files[open] = Data()
        log("segment \(String(format: "%08X", open))")
      }
      if pausedUntil <= now() { put(record(frame, 10)) }
      frame += 10
      if frame % 1000 == 0 { save(); log("frame \(frame) at \(now()), its own clock \(clock0.at + Int64(frame - clock0.frame) * 10)") }
    }
  }

  func put(_ r: Data) {
    let off = UInt32(files[open, default: Data()].count)
    files[open, default: Data()] += r
    guard subscribed.contains(live.uuid) else { return }
    liveCount += 1
    if dropEvery > 0 && liveCount % dropEvery == 0 { log("LIVE dropped at \(String(format: "%08X", open))+\(off)"); return }
    let room = mtu - 8
    var i = 0
    while i < r.count {
      let m = min(room, r.count - i)
      send(live, le(open) + le(off + UInt32(i)) + r.subdata(in: i..<(i + m)))
      i += m
    }
  }

  func send(_ c: CBMutableCharacteristic, _ v: Data) {
    queue.append((c, v))
    pump()
  }

  func pump() {
    while let (c, v) = queue.first {
      guard pm.updateValue(v, for: c, onSubscribedCentrals: nil) else { return }  // full: peripheralManagerIsReady
      queue.removeFirst()
    }
  }

  func reply(_ op: UInt8, _ a: UInt32, _ b: Data = Data()) { send(ctrl, Data([op]) + le(a) + b) }

  func command(_ v: Data) {
    let op = v.first ?? 0
    switch op {
    case 0x01:
      for f in files.keys.sorted().prefix(64) { reply(0x81, f, le(UInt32(files[f]!.count))) }
      send(ctrl, Data([0x80, 0]) + le(open))
      log("LIST: \(files.keys.sorted().map { String(format: "%08X:%d", $0, files[$0]!.count) }) open \(String(format: "%08X", open))")
    case 0x02:
      let f = u32(v, 1), off = Int(u32(v, 5))
      let b = files[f] ?? Data(), end = max(off, b.count)
      var i = off
      while i < b.count { let m = min(mtu - 4, b.count - i); send(data, le(UInt32(i)) + b.subdata(in: i..<(i + m))); i += m }
      reply(0x82, f, le(UInt32(end)))
      log("READ \(String(format: "%08X", f)) from \(off): \(b.count - min(off, b.count)) B")
    case 0x03:
      queue.removeAll { $0.0 === data }
      log("STOP")
    case 0x04:
      let f = u32(v, 1)
      let err: Int8 = f == open ? -16 : files[f] == nil ? -2 : 0
      if err == 0 { files[f] = nil; try? FileManager.default.removeItem(at: cardDir.appendingPathComponent(String(format: "%08X.BIN", f))) }
      reply(0x84, f, Data([UInt8(bitPattern: err)]))
      log("DELETE \(String(format: "%08X", f)): \(err)")
    case 0x05: reply(0x85, 0); log("FORGET \(v.count > 1 && v[v.startIndex + 1] == 1 ? "all" : "others")")
    case 0x06: reply(0x86, UInt32(v[v.startIndex + 1]), Data([UInt8(bitPattern: -107)])); log("USB_DISK")
    case 0x07:
      let from = Int(u32(v, 1)), first = max(from - 7000, 0)
      var i = first
      while i < events.count { let n = min(4, events.count - i); send(data, le(UInt32(7000 + i)) + events[i..<(i + n)].reduce(Data(), +)); i += n }
      reply(0x87, UInt32(7000 + events.count))
      log("LOG from \(from): \(max(0, events.count - first)) records")
    case 0x20: let s = u32(v, 1); pausedUntil = s > 0 ? now() + Int64(s) * 1000 : 0; log("PAUSE \(s) s")
    default: log("unknown command \(v as NSData)")
    }
  }

  func status() -> Data {
    var v = le(UInt16(boot)) + le(frame) + le(Int16(0)) + le(UInt32(30_000_000)) + le(UInt32(31_000_000))
    v += Data(count: 42 - v.count)
    v += Data([1]) + le(UInt32(max(0, (pausedUntil - now() + 999) / 1000)))
    return v
  }

  func peripheralManagerDidUpdateState(_ p: CBPeripheralManager) {
    log("state \(p.state.rawValue)")
    guard p.state == .poweredOn else { return }
    let store = CBMutableService(type: uuid(0), primary: true)
    store.characteristics = [time, ctrl, data, live]
    let bas = CBMutableService(type: CBUUID(string: "180F"), primary: true)
    bas.characteristics = [battery]
    let dis = CBMutableService(type: CBUUID(string: "180A"), primary: true)
    dis.characteristics = [model, fw]
    for s in [store, bas, dis] { p.add(s) }
    advertise()
  }

  func advertise() { pm.startAdvertising([CBAdvertisementDataLocalNameKey: name, CBAdvertisementDataServiceUUIDsKey: [uuid(0)]]) }

  func peripheralManager(_ p: CBPeripheralManager, didAdd s: CBService, error: Error?) { log("service \(s.uuid): \(error.map { "\($0)" } ?? "ok")") }
  func peripheralManagerDidStartAdvertising(_ p: CBPeripheralManager, error: Error?) { log("advertising \(name): \(error.map { "\($0)" } ?? "ok")") }

  func peripheralManager(_ p: CBPeripheralManager, central: CBCentral, didSubscribeTo c: CBCharacteristic) {
    mtu = central.maximumUpdateValueLength
    subscribed.insert(c.uuid)
    log("subscribed \(c.uuid) mtu \(mtu + 3)")
    if c.uuid == live.uuid {
      p.stopAdvertising(); event(9, 24, 400)  // connected: only this phone (the board goes non-connectable)
      if !(files[open] ?? Data()).isEmpty {  // as the board: the stream goes on in a new file for its follower (#130)
        open += 1; files[open] = Data(); rolled = Date()
        log("segment \(String(format: "%08X", open)) for the follower")
      }
    }
  }

  func peripheralManager(_ p: CBPeripheralManager, central: CBCentral, didUnsubscribeFrom c: CBCharacteristic) {
    subscribed.remove(c.uuid)
    log("unsubscribed \(c.uuid)")
    if subscribed.isEmpty { queue.removeAll(); advertise() }
  }

  func peripheralManagerIsReady(toUpdateSubscribers p: CBPeripheralManager) { pump() }

  func peripheralManager(_ p: CBPeripheralManager, didReceiveRead r: CBATTRequest) {
    let v = r.characteristic.uuid == time.uuid ? status() : (r.characteristic as? CBMutableCharacteristic)?.value ?? Data()
    guard r.offset <= v.count else { return p.respond(to: r, withResult: .invalidOffset) }
    r.value = v.subdata(in: r.offset..<v.count)
    p.respond(to: r, withResult: .success)
    if r.characteristic.uuid == time.uuid && r.offset == 0 { log("status read: frame \(frame)") }
  }

  func peripheralManager(_ p: CBPeripheralManager, didReceiveWrite rs: [CBATTRequest]) {
    for r in rs {
      let v = r.value ?? Data()
      if r.characteristic.uuid == time.uuid {
        let e = Int64(bitPattern: v.withUnsafeBytes { $0.loadUnaligned(as: UInt64.self) })
        put(Data([0x54]) + le(frame) + le(e))
        event(14, UInt32(truncatingIfNeeded: e), UInt32(truncatingIfNeeded: e >> 32))
        log("TIME: frame \(frame) = \(e) (here \(now()), phone \(e - now()) ms ahead)")
      } else if r.characteristic.uuid == ctrl.uuid {
        command(v)
      }
    }
    p.respond(to: rs[0], withResult: .success)
  }
}

let fake = Fake()
signal(SIGINT) { _ in fake.q.sync { fake.save() }; exit(0) }
signal(SIGTERM) { _ in fake.q.sync { fake.save() }; exit(0) }
RunLoop.main.run()
