// Firmware updates over Bluetooth, the iOS side of the Android app's Dfu.kt: what the recorder page shows, the checks
// before an update starts, and what it leaves in the device log. The update itself is DfuRun.swift (Nordic's DFU
// library, which the Linux check in CI cannot build). Foundation only, so app/ios/StoreCheck runs the checks.
import Foundation

enum Dfu {
  static var state: String?  // on the main thread, as the method channel reads it
  static var busy = false  // from start until done, aborted or failed: the app keeps the recorder link down
  static var log: DfuLog?  // the update under way, or the last one until its recorder is back; main thread

  /// The recorder's revision as a status read it (BleLink): the first one after an update ends goes into its log.
  static func back(_ fw: String?) {
    guard let l = log, l.ended != nil else { return }
    log = nil
    l.back(fw)
  }

  /// Why the package may not go onto the bound recorder (error code, message), as MainActivity's "dfu" checks it: the
  /// model the recorder said it is must be the package's, and the bytes must be the ones the server listed. Nil: go on.
  static func refusal(want: String?, model: String?, id: String?, linked: Bool, sha256: String, expected: String?) -> (String, String)? {
    if busy { return ("busy", "正在升级") }
    if linked { return ("busy", "录音设备的连接还没断开，稍后再试") }
    guard let model, id.flatMap(UUID.init(uuidString:)) != nil else { return ("none", "还没连上过录音设备，不知道它的型号") }
    if want != model { return ("model", "这个固件是 \(want ?? "?") 的，录音设备是 \(model)，不能刷") }
    if sha256 != expected { return ("sha256", "固件下载不完整，再试一次") }
    return nil
  }
}

/// What an update leaves in the device log (DeviceLog, kind "dfu"), as DfuLog in Dfu.kt: its start, the recorder found
/// in its bootloader ("boot"), the transfer's start ("send") and every 25 % ("progress"), its end ("done", or "fail" with
/// the stage it got to) and the recorder's first status after it ("back": the revision it runs); each with `step` and
/// `ms` since the start (since the end for "back"). The server's upgrades() (db.py) joins them with the recorder's own
/// DFU and BOOT records.
final class DfuLog {
  typealias Out = (_ device: String, _ text: String, _ level: String?, _ fields: [String: Any]) -> Void
  let device: String, to: String
  var t0 = Date(), ended: Date?  // the check moves them back
  private var stage = "start", pct = 0, failed = false
  private let out: Out

  init(device: String, from: String?, to: String, bytes: Int, sha256: String, battery: Int?,
       out: @escaping Out = { DeviceLog.app($0, "dfu", $1, level: $2, fields: $3) }) {
    self.device = device
    self.to = to
    self.out = out
    note("start", "开始升级 \(from ?? "?") → \(to)，\(bytes / 1024) KB，sha \(sha256.prefix(8))，电量 \(battery.map { "\($0)%" } ?? "未知")",
         ["from": from ?? NSNull(), "to": to, "bytes": bytes, "sha": String(sha256.prefix(8)), "battery": battery ?? NSNull()])
  }

  private static func ms(_ since: Date) -> Int { Int(-since.timeIntervalSinceNow * 1000) }
  static func span(_ ms: Int) -> String { ms < 60_000 ? "\(ms / 1000) 秒" : "\(ms / 60_000) 分 \(ms / 1000 % 60) 秒" }

  /// "boot" (connected to the bootloader) or "send" (the image goes out).
  func reached(_ s: String) {
    stage = s
    note(s, "\(s == "boot" ? "进入升级模式" : "开始传输")，开始后 \(DfuLog.span(DfuLog.ms(t0)))")
  }

  func progress(_ p: Int, kbps: Double) {
    defer { pct = p }
    guard p < 100, p / 25 > pct / 25 else { return }
    note("progress", String(format: "传输 %d%%，平均 %.1f KB/s", p, kbps), ["pct": p, "kbps": (kbps * 10).rounded() / 10])
  }

  func done() {
    ended = Date()
    note("done", "升级完成，用时 \(DfuLog.span(DfuLog.ms(t0)))")
  }

  /// `code`: the library's error code, nil for the app's own reasons.
  func fail(_ code: Int?, _ error: String) {
    ended = Date()
    failed = true
    let at = stage == "start" ? "进入升级模式前" : stage == "boot" ? "进入升级模式后、传输前" : "传输到 \(pct)%"
    note("fail", "升级失败（\(at)）：\(error)\(code.map { "（错误 \($0)）" } ?? "")",
         ["stage": stage, "pct": pct, "code": code ?? NSNull(), "error": error], level: "error")
  }

  func back(_ fw: String?) {
    let ok = fw == to, ms = DfuLog.ms(ended ?? t0)
    note("back", "\(failed ? "升级失败后" : "升级后")连回，用时 \(DfuLog.span(ms))，固件 \(fw ?? "?")\(ok ? "（目标版本）" : "，不是目标版本")",
         ["ms": ms, "fw": fw ?? NSNull(), "ok": ok], level: ok || failed ? nil : "warn")
  }

  private func note(_ step: String, _ text: String, _ fields: [String: Any] = [:], level: String? = nil) {
    out(device, text, level, fields.merging(["step": step, "ms": DfuLog.ms(t0)]) { a, _ in a })
  }
}
