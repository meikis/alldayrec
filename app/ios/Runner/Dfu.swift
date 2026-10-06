// Firmware updates over Bluetooth, the iOS side of the Android app's Dfu.kt: what the recorder page shows, and the
// checks before an update starts. The update itself is DfuRun.swift (Nordic's DFU library, which the Linux check in CI
// cannot build). Foundation only, so app/ios/StoreCheck runs the checks.
import Foundation

enum Dfu {
  static var state: String?  // on the main thread, as the method channel reads it
  static var busy = false  // from start until done, aborted or failed: the app keeps the recorder link down

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
