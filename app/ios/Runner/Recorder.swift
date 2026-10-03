// iOS counterpart of RecorderService.kt + Slicer: the microphone stays open under the `audio` background mode,
// the stream is cut into ADTS AAC pieces (a piece cut short by a kill still decodes) and uploaded in order.
// iOS has no START_STICKY: after a kill, the next launch uploads what was left and the owner restarts by hand.
import AVFoundation
import Flutter
import UIKit

enum Recorder {
  static var slicer: Slicer?
  static var uploader: Uploader?
  static var interruptedAt: Int64?
  static var foregroundApp = true
  static let defaults = UserDefaults(suiteName: "recorder")!
  static let dir: URL = {
    let d = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("chunks")
    try? FileManager.default.createDirectory(at: d, withIntermediateDirectories: true)
    return d
  }()

  static func register(_ messenger: FlutterBinaryMessenger) {
    FlutterMethodChannel(name: "live_record/recorder", binaryMessenger: messenger).setMethodCallHandler { call, result in
      let a = call.arguments as? [String: Any] ?? [:]
      switch call.method {
      case "start": result(start(a).map { FlutterError(code: "start", message: $0, details: nil) })
      case "stop": stop(); result(nil)
      case "foreground":
        foregroundApp = a["on"] as? Bool ?? true
        slicer?.foreground = foregroundApp
        result(nil)
      case "slices":  // new lengths from the server: the running slicer cuts its next piece with them
        slicer?.setSlices(fg: a["foreground"] as? Int ?? 1, bg: a["background"] as? Int ?? 20)
        result(nil)
      case "status": result(status())
      case "deviceName": result(UIDevice.current.name)  // "iPhone" since iOS 16 without the user-assigned-device-name entitlement
      case "upload": if Uploader.pending(dir) > 0 { upload() }; result(nil)
      default: result(FlutterMethodNotImplemented)
      }
    }
    resume()
  }

  static func status() -> [String: Any] {
    let q = Uploader.files(dir)
    let bytes = q.reduce(0) { $0 + ((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
    return ["running": slicer != nil, "uploading": uploader != nil && !q.isEmpty, "files": slicer?.files ?? NSNull(),
            "slice": slicer?.sliceS() ?? NSNull(), "error": slicer?.error ?? NSNull(), "sent": uploader?.sent ?? NSNull(),
            "pending": q.count, "pendingBytes": bytes, "oldest": q.first.flatMap { Int64($0.deletingPathExtension().lastPathComponent) } ?? NSNull(),
            "note": uploader?.note ?? NSNull(), "interruptedAt": interruptedAt ?? NSNull()]
  }

  private static func start(_ a: [String: Any]) -> String? {
    if slicer != nil { return nil }
    for k in ["server", "token", "device"] { defaults.set(a[k] as? String ?? "", forKey: k) }
    defaults.set(a["archive"] as? Bool ?? true, forKey: "archive")
    let s = Slicer(dir: dir, fgS: a["foreground"] as? Int ?? 1, bgS: a["background"] as? Int ?? 20)
    s.foreground = foregroundApp
    do { try s.start() } catch { return "\(error)" }
    slicer = s
    interruptedAt = nil
    defaults.set(true, forKey: "active")
    defaults.removeObject(forKey: "endAt")  // a new recording carries on: no end for the last stop
    UIApplication.shared.isIdleTimerDisabled = true  // the page is meant to be watched
    upload()
    return nil
  }

  private static func stop() {
    let at = now()
    defaults.set(false, forKey: "active")
    defaults.set(at, forKey: "endAt")  // sent after the last piece, also by a later launch if this one is killed first
    UIApplication.shared.isIdleTimerDisabled = false
    slicer?.stop(); slicer = nil
    // iOS gives about 30 s after the app leaves the screen; what is not sent by then goes on the next launch
    var task = UIBackgroundTaskIdentifier.invalid
    let release = {
      DispatchQueue.main.async {
        if task != .invalid { UIApplication.shared.endBackgroundTask(task); task = .invalid }
      }
    }
    task = UIApplication.shared.beginBackgroundTask(withName: "stop", expirationHandler: release)  // not ended on expiry = killed
    endAfterQueue(release)
  }

  /// Once every piece is up (however long that takes, across suspensions) and nothing records again, the saved end.
  private static func endAfterQueue(_ done: @escaping () -> Void = {}) {
    Thread.detachNewThread {
      while Uploader.pending(dir) > 0 && slicer == nil { Thread.sleep(forTimeInterval: 0.5) }
      let at = defaults.object(forKey: "endAt") as? Int64
      if slicer == nil, let at { uploader?.end(at); defaults.removeObject(forKey: "endAt") }  // the server may close the conversation now
      done()
    }
  }

  /// At launch: a piece being written when the process died is still valid ADTS; send what is left over.
  private static func resume() {
    let fm = FileManager.default
    var last: Int64 = 0
    for f in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [] {
      let m = (try? f.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate).map { Int64($0.timeIntervalSince1970 * 1000) } ?? 0
      last = max(last, m)
      if f.lastPathComponent.hasSuffix(".part.aac") {
        try? fm.moveItem(at: f, to: dir.appendingPathComponent(f.lastPathComponent.replacingOccurrences(of: ".part.aac", with: ".aac")))
      }
    }
    guard defaults.bool(forKey: "active") || Uploader.pending(dir) > 0 || defaults.object(forKey: "endAt") != nil else { return }
    let killed = defaults.bool(forKey: "active")
    defaults.set(false, forKey: "active")
    upload()
    if killed {
      uploader?.note = "上次录音被系统终止，未传的 \(Uploader.pending(dir)) 块正在补传，需要手动重新开始"
      if last > 0 { defaults.set(last, forKey: "endAt") }  // close the conversation the kill left open
    }
    endAfterQueue()  // unless a new recording takes over first
  }

  /// One uploader for the queue directory; a new start only refreshes its settings, so no piece goes twice.
  private static func upload() {
    let archive = defaults.bool(forKey: "archive")
      ? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("recordings") : nil
    let u = uploader ?? Uploader(dir: dir)
    u.server = defaults.string(forKey: "server") ?? ""
    u.token = defaults.string(forKey: "token") ?? ""
    u.device = defaults.string(forKey: "device") ?? "phone"
    u.archive = archive
    if uploader == nil { uploader = u; u.start() }
  }

  static func now() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
}

/// The input node's stream, resampled to 16 kHz mono, written as AAC files whose length follows the app state and
/// named by the wall time of their first sample. An interruption (a call) or a route change (AirPods) ends the
/// piece and re-anchors the wall time when recording resumes.
final class Slicer {
  var foreground = true
  private(set) var files = 0
  private(set) var error: String?
  private let dir: URL
  private var fgS: Int, bgS: Int  // set on q
  private let engine = AVAudioEngine()
  private let out = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
  private let q = DispatchQueue(label: "slicer")  // owns everything below
  private var converter: AVAudioConverter?
  private var file: AVAudioFile?, part: URL?
  private var running = false
  private var anchorMs: Int64 = 0, samples: Int64 = 0, fileStart: Int64 = 0, fileSlice = 0
  private var observers: [NSObjectProtocol] = []

  init(dir: URL, fgS: Int, bgS: Int) { self.dir = dir; self.fgS = fgS; self.bgS = bgS }

  func sliceS() -> Int { foreground ? fgS : bgS }

  func setSlices(fg: Int, bg: Int) { q.async { self.fgS = fg; self.bgS = bg } }

  func start() throws {
    let session = AVAudioSession.sharedInstance()
    try session.setCategory(.record, mode: .measurement)  // no voice processing, like Android's MIC source
    try session.setActive(true)
    q.sync { running = true }
    try restart()
    let nc = NotificationCenter.default
    observers = [
      nc.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: nil) { [weak self] n in
        guard let self, let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt else { return }
        if AVAudioSession.InterruptionType(rawValue: raw) == .began {
          self.q.async { self.closeFile() }
        } else {
          do { try session.setActive(true); try self.restart(); Recorder.interruptedAt = Recorder.now() }
          catch { self.error = "中断后恢复失败 \(error)" }
        }
      },
      nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
        do { try self?.restart() } catch { self?.error = "切换输入后恢复失败 \(error)" }
      },
    ]
  }

  func stop() {
    observers.forEach(NotificationCenter.default.removeObserver)
    engine.inputNode.removeTap(onBus: 0)
    engine.stop()
    q.sync { running = false; closeFile() }
    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
  }

  /// (Re)attach the tap with the input's current format and start a new piece at the current wall time.
  private func restart() throws {
    let input = engine.inputNode
    input.removeTap(onBus: 0)
    engine.stop()
    let format = input.outputFormat(forBus: 0)
    converter = AVAudioConverter(from: format, to: out)
    q.sync { closeFile(); anchorMs = Recorder.now(); samples = 0 }
    input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buf, _ in self?.convert(buf) }
    engine.prepare()
    try engine.start()
  }

  private func convert(_ buf: AVAudioPCMBuffer) {
    guard let converter,
          let o = AVAudioPCMBuffer(pcmFormat: out, frameCapacity: AVAudioFrameCount(Double(buf.frameLength) * out.sampleRate / buf.format.sampleRate) + 16)
    else { return }
    var fed = false, err: NSError?
    converter.convert(to: o, error: &err) { _, status in
      if fed { status.pointee = .noDataNow; return nil }
      fed = true; status.pointee = .haveData; return buf
    }
    if let err { error = "\(err)"; return }
    if o.frameLength > 0 { q.async { self.write(o) } }
  }

  private func write(_ b: AVAudioPCMBuffer) {
    guard running else { return }
    let sr = Int64(out.sampleRate), state = sliceS(), at = samples - fileStart
    // rotate: the slice length is up, or the state changed to a shorter slice that is already over
    if file != nil && (at >= Int64(fileSlice) * sr || (state < fileSlice && at >= Int64(state) * sr)) { closeFile() }
    do {
      if file == nil {
        let url = dir.appendingPathComponent("\(anchorMs + samples * 1000 / sr).part.aac")  // .aac picks the ADTS container
        file = try AVAudioFile(forWriting: url, settings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: out.sampleRate,
                                                           AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 24000],
                               commonFormat: .pcmFormatFloat32, interleaved: false)
        part = url; fileStart = samples; fileSlice = state
      }
      try file!.write(from: b)
    } catch { self.error = "\(error)" }
    samples += Int64(b.frameLength)
  }

  private func closeFile() {
    guard let p = part else { return }
    file = nil  // releasing the AVAudioFile flushes and closes it
    part = nil
    try? FileManager.default.moveItem(at: p, to: p.deletingLastPathComponent().appendingPathComponent(p.lastPathComponent.replacingOccurrences(of: ".part.aac", with: ".aac")))
    files += 1
  }
}

/// Uploads the .aac files in start order, one at a time, retrying forever; a file is archived or deleted once the
/// server has it, so the directory is the offline queue.
final class Uploader: Thread {
  var server = "", token = "", device = "phone", archive: URL?
  private(set) var sent = 0
  var note: String?
  private let dir: URL
  private var fails = 0

  init(dir: URL) { self.dir = dir }

  static func files(_ dir: URL) -> [URL] {
    ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
      .filter { $0.pathExtension == "aac" && Int64($0.deletingPathExtension().lastPathComponent) != nil }
      .sorted { $0.lastPathComponent < $1.lastPathComponent }
  }

  static func pending(_ dir: URL) -> Int { files(dir).count }

  override func main() {
    while true {
      guard let f = Uploader.files(dir).first else { Thread.sleep(forTimeInterval: 1); continue }
      do {
        let code = try post("\(server)/api/chunks?device=\(enc(device))&start=\(f.deletingPathExtension().lastPathComponent)",
                            try Data(contentsOf: f), "audio/aac")
        switch code {
        case 200: keep(f); sent += 1; fails = 0; note = nil
        case 400: try? FileManager.default.removeItem(at: f); note = "服务端无法解码一块，已丢弃"
        default: throw URLError(.badServerResponse, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"])
        }
      } catch {
        fails += 1
        note = "上传失败 \(error.localizedDescription)"
        Thread.sleep(forTimeInterval: 5 * Double(min(fails, 6)))
      }
    }
  }

  func end(_ atMs: Int64) {
    do { _ = try post("\(server)/api/chunks/end?device=\(enc(device))&t=\(atMs)", Data(), nil) } catch { note = "结束信号失败 \(error.localizedDescription)" }
  }

  private func keep(_ f: URL) {
    let fm = FileManager.default
    guard let archive else { try? fm.removeItem(at: f); return }
    let fmt = DateFormatter()
    fmt.locale = Locale(identifier: "en_US_POSIX"); fmt.dateFormat = "yyyy-MM-dd"
    let day = archive.appendingPathComponent(fmt.string(from: Date(timeIntervalSince1970: Double(f.deletingPathExtension().lastPathComponent)! / 1000)))
    do {
      try fm.createDirectory(at: day, withIntermediateDirectories: true)
      try? fm.removeItem(at: day.appendingPathComponent(f.lastPathComponent))
      try fm.moveItem(at: f, to: day.appendingPathComponent(f.lastPathComponent))
    } catch { note = "归档失败 \(error.localizedDescription)"; try? fm.removeItem(at: f) }
  }

  private func post(_ url: String, _ body: Data, _ type: String?) throws -> Int {
    guard let u = URL(string: url) else { throw URLError(.badURL) }
    var r = URLRequest(url: u, timeoutInterval: 60)
    r.httpMethod = "POST"
    r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
    if let type { r.setValue(type, forHTTPHeaderField: "Content-Type") }
    var code = 0, err: Error?
    let done = DispatchSemaphore(value: 0)
    URLSession.shared.uploadTask(with: r, from: body) { _, resp, e in
      code = (resp as? HTTPURLResponse)?.statusCode ?? 0; err = e; done.signal()
    }.resume()
    done.wait()
    if let err { throw err }
    return code
  }

  private func enc(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? s }
}
