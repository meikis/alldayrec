// The update with Nordic's DFU library (NordicDFU, a Swift package), as Dfu.kt does it on Android: the recorder reboots
// into its Adafruit bootloader (legacy DFU) and the package goes in 20-byte packets, a receipt every 8 (lr_dfu.py, #33).
// Where iOS differs:
//  - the app sends the recorder into the bootloader itself, with the control point's 0x06 as lr_dfu.py does. The
//    library's way (Start DFU, 0x01 0x04) gets a one-byte notification back from our firmware (transport.c), which the
//    iOS library takes for a broken response and stops on; Android's ignores it
//  - no Bluetooth address: the bootloader (AdaDFU on the recorder's address + 1, unbonded) is the first device that
//    advertises the legacy DFU service, as the library's own selector picks it (Android looks for address + 1)
//  - the library connects with a CBCentralManager of its own; BleLink's is gone meanwhile (Recorder.withoutLink)
import CoreBluetooth
import CryptoKit
import NordicDFU

extension Dfu {
  private static var run: DfuRun?  // the library holds its delegates weakly

  static func sha256(_ d: Data) -> String { SHA256.hash(data: d).map { String(format: "%02x", $0) }.joined() }

  /// Updates the recorder with peripheral identifier `id`; throws when `zip` is no DFU package.
  static func start(_ id: UUID, _ zip: Data) throws {
    let fw = try DFUFirmware(zipFile: zip)
    state = "准备升级"
    busy = true
    NSLog("live-record dfu %d B to %@", zip.count, id.uuidString)
    run = DfuRun(id, fw)
  }

  fileprivate static func end(_ s: String) {
    state = s
    busy = false
    run = nil
  }

  fileprivate static func failed(_ why: String, code: Int? = nil) {
    log?.fail(code, why)
    end("升级失败：\(why)（录音设备还在就再试一次，不见了就插 USB 用 lr.sh flash 刷）")
  }
}

/// One update, on the main thread: connect to the recorder (iOS encrypts with the bond: the control point takes only a
/// bonded phone), write 0x06, and once it has dropped the link, find the bootloader and hand it to the library.
private final class DfuRun: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate, DFUServiceDelegate, DFUProgressDelegate, LoggerDelegate {
  static let service = CBUUID(string: "00001530-1212-EFDE-1523-785FEABCD123"), cp = CBUUID(string: "00001531-1212-EFDE-1523-785FEABCD123")
  private let id: UUID, fw: DFUFirmware
  private var central: CBCentralManager?
  private var recorder: CBPeripheral?
  private var wrote = false
  private var deadline: DispatchWorkItem?
  private var controller: DFUServiceController?

  init(_ id: UUID, _ fw: DFUFirmware) {
    self.id = id
    self.fw = fw
    super.init()
    central = CBCentralManager(delegate: self, queue: nil)
  }

  /// Fails with `why` unless the next step comes within `s` seconds (a connect request on iOS never times out).
  private func within(_ s: Double, _ why: String) {
    deadline?.cancel()
    let w = DispatchWorkItem { [weak self] in self?.fail(why) }
    deadline = w
    DispatchQueue.main.asyncAfter(deadline: .now() + s, execute: w)
  }

  private func fail(_ why: String) {
    guard controller == nil else { return }  // the library's now: it reports its own end
    NSLog("live-record dfu: %@", why)
    deadline?.cancel()
    if let c = central {
      c.delegate = nil
      if c.state == .poweredOn { c.stopScan(); if let p = recorder { c.cancelPeripheralConnection(p) } }
    }
    Dfu.failed(why)
  }

  func centralManagerDidUpdateState(_ c: CBCentralManager) {
    guard c.state == .poweredOn else { if c.state != .unknown { fail("蓝牙不可用") }; return }
    guard let p = c.retrievePeripherals(withIdentifiers: [id]).first else { return fail("这台 iPhone 不认识录音设备") }
    recorder = p
    p.delegate = self
    Dfu.state = "正在连接录音设备"
    c.connect(p)
    within(20, "连不上录音设备")
  }

  func centralManager(_ c: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) { fail("连不上录音设备") }

  func centralManager(_ c: CBCentralManager, didConnect p: CBPeripheral) {
    Dfu.state = "录音设备重启进入升级模式"
    p.discoverServices([DfuRun.service])
  }

  func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
    guard let s = p.services?.first(where: { $0.uuid == DfuRun.service }) else { return fail("录音设备上没有升级服务") }
    p.discoverCharacteristics([DfuRun.cp], for: s)
  }

  func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor s: CBService, error: Error?) {
    guard let ch = s.characteristics?.first(where: { $0.uuid == DfuRun.cp }) else { return fail("录音设备上没有升级服务") }
    wrote = true
    p.writeValue(Data([0x06]), for: ch, type: .withResponse)  // it resets before it answers
    within(15, "录音设备没有重启")  // iOS sees the link gone at its supervision timeout (up to 6 s)
  }

  func peripheral(_ p: CBPeripheral, didWriteValueFor ch: CBCharacteristic, error: Error?) {
    NSLog("live-record dfu: 0x06 written: %@", error.map { "\($0)" } ?? "ok")
    if let e = error as? CBATTError { fail("录音设备拒绝进入升级模式（\(e.localizedDescription)）") }  // it answered: no reset
  }

  func centralManager(_ c: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
    guard wrote else { return fail("录音设备断开了") }
    c.scanForPeripherals(withServices: [DfuRun.service])
    within(30, "录音设备没有进入升级模式")
  }

  func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral, advertisementData a: [String: Any], rssi: NSNumber) {
    let name = a[CBAdvertisementDataLocalNameKey] as? String ?? p.name ?? ""
    if name.hasPrefix("LR-") { return }  // a recorder on its firmware (lr_dfu.py)
    NSLog("live-record dfu: bootloader %@ %@ rssi %@", name, p.identifier.uuidString, rssi)
    deadline?.cancel()
    c.stopScan()
    c.delegate = nil  // the library connects with its own central
    let i = DFUServiceInitiator(queue: DispatchQueue(label: "dfu"))  // Bluetooth on its own queue, callbacks on main
    i.delegate = self
    i.progressDelegate = self
    i.logger = self
    i.packetReceiptNotificationParameter = 8  // as Dfu.kt and lr_dfu.py: on 16 the XIAO's bootloader stalled mid-image
    controller = i.with(firmware: fw).start(target: p)
  }

  func dfuStateDidChange(to s: DFUState) {
    switch s {
    case .starting: Dfu.state = "开始升级"; Dfu.log?.reached("boot")
    case .uploading: Dfu.log?.reached("send")
    case .completed: Dfu.log?.done(); Dfu.end("升级完成，录音设备已重启")  // no .aborted: nothing here aborts (Android's notification can)
    default: break
    }
  }

  func dfuError(_ e: DFUError, didOccurWithMessage m: String) {
    NSLog("live-record dfu error %d: %@", e.rawValue, m)
    Dfu.failed(m, code: e.rawValue)
  }

  func dfuProgressDidChange(for part: Int, outOf totalParts: Int, to progress: Int, currentSpeedBytesPerSecond: Double, avgSpeedBytesPerSecond: Double) {
    Dfu.state = String(format: "升级中 %d%% · %.1f KB/s", progress, avgSpeedBytesPerSecond / 1000)  // Android's B/ms
    Dfu.log?.progress(progress, kbps: avgSpeedBytesPerSecond / 1000)
  }

  func logWith(_ level: LogLevel, message: String) {
    if level.rawValue >= LogLevel.application.rawValue { NSLog("live-record dfu %@ %@", level.name(), message) }
  }
}
