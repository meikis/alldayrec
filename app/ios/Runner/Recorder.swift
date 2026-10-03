// iOS counterpart of RecorderService.kt + Slicer: the microphone stays open under the `audio` background mode,
// the stream is cut into Ogg Opus pieces (a piece cut short by a kill still decodes) and uploaded in order.
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
        if foregroundApp { slicer?.resume() }  // a recording the system stopped and did not give back (from the background iOS may refuse)
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
            "pending": q.count, "pendingBytes": bytes, "oldest": q.first.flatMap { Uploader.piece($0)?.start } ?? NSNull(),
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
    defaults.set(false, forKey: "active")
    if defaults.object(forKey: "endAt") == nil {  // set already: the recording stopped earlier, at a gap (gap(from:))
      defaults.set(now(), forKey: "endAt")  // sent after the last piece, also by a later launch if this one is killed first
    }
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

  /// Once every piece is up (however long that takes, across suspensions), the saved end, unless recording started
  /// or resumed meanwhile (which removes it).
  private static func endAfterQueue(_ done: @escaping () -> Void = {}) {
    Thread.detachNewThread {
      while Uploader.pending(dir) > 0 && defaults.object(forKey: "endAt") != nil { Thread.sleep(forTimeInterval: 0.5) }
      if let at = defaults.object(forKey: "endAt") as? Int64 { uploader?.end(at); defaults.removeObject(forKey: "endAt") }  // the server may close the conversation now
      done()
    }
  }

  /// The system stopped the recording at `at` and it could not resume: the server gets `at` as the recording's end
  /// once the pieces before it are up, so its conversation closes instead of waiting for audio that is not coming.
  static func gap(from at: Int64) {
    guard defaults.object(forKey: "endAt") == nil else { return }
    defaults.set(at, forKey: "endAt")
    endAfterQueue()
  }

  static func resumed(after at: Int64) {
    interruptedAt = at
    defaults.removeObject(forKey: "endAt")
  }

  /// At launch: a piece being written when the process died still decodes up to its last page; send what is left over.
  private static func resume() {
    let fm = FileManager.default
    var last: Int64 = 0
    for f in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey])) ?? [] {
      let m = (try? f.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate).map { Int64($0.timeIntervalSince1970 * 1000) } ?? 0
      last = max(last, m)
      if f.lastPathComponent.contains(".part.") {  // no sample count: <start>.ogg (or .aac from the app before)
        try? fm.moveItem(at: f, to: dir.appendingPathComponent(f.lastPathComponent.replacingOccurrences(of: ".part.", with: ".")))
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

  static func clock(_ ms: Int64) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss"
    return f.string(from: Date(timeIntervalSince1970: Double(ms) / 1000))
  }
}

/// The input node's stream, resampled to 16 kHz mono and encoded as Ogg Opus pieces (OggOpus.swift) whose length
/// follows the app state, each named by the wall time of its first sample and its sample count. A run is one
/// encoder over continuous audio; an interruption (a call), a route change (AirPods) or a media services reset ends
/// it, and the next run re-anchors the wall time. Everything that starts or stops the engine runs on the main
/// thread; the run and the files belong to `q`.
final class Slicer {
  var foreground = true
  private(set) var files = 0
  private(set) var error: String?
  private let dir: URL
  private var fgS: Int, bgS: Int  // set on q
  private var engine = AVAudioEngine()  // new after a media services reset
  private let out = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: OpusPieces.rate, channels: 1, interleaved: false)!
  private let q = DispatchQueue(label: "slicer")  // owns run, anchorMs, fileSlice
  private var run: OpusPieces?
  private var anchorMs: Int64 = 0, fileSlice = 0
  private var gapFrom: Int64?  // main: the system stopped the recording at this time and it has not resumed yet
  private var observers: [NSObjectProtocol] = []

  init(dir: URL, fgS: Int, bgS: Int) { self.dir = dir; self.fgS = fgS; self.bgS = bgS }

  func sliceS() -> Int { foreground ? fgS : bgS }

  func setSlices(fg: Int, bg: Int) { q.async { self.fgS = fg; self.bgS = bg } }

  func start() throws {
    try activate()
    try restart()
    let nc = NotificationCenter.default, session = AVAudioSession.sharedInstance()
    observers = [
      nc.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] n in
        guard let self, let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt else { return }
        // ended: resume whether or not it says shouldResume. It may never come; then the app coming to the front resumes
        if AVAudioSession.InterruptionType(rawValue: raw) == .began { self.pause() } else { self.resume() }
      },
      nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: .main) { [weak self] _ in
        guard let self else { return }
        self.pause()
        self.engine = AVAudioEngine()  // every audio object of before is dead; the session is set up again in resume
        self.resume()
      },
      nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: .main) { [weak self] n in
        guard let self, n.object as AnyObject === self.engine, self.gapFrom == nil else { return }
        do { try self.restart() } catch { self.pause(); self.failed("切换输入后恢复失败", error) }
      },
    ]
  }

  func stop() {
    observers.forEach(NotificationCenter.default.removeObserver)
    engine.inputNode.removeTap(onBus: 0)
    engine.stop()
    q.sync { finishRun() }
    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
  }

  /// Mixable, so playing a video or a podcast neither silences it nor interrupts the recording (.record did both);
  /// recording is passive and leaves the output where the user has it: .allowBluetoothA2DP keeps AirPods available as
  /// an output (playAndRecord hides them otherwise; A2DP is output only). No .allowBluetooth: the input would move to
  /// the AirPods' call microphone. No .defaultToSpeaker: it would force the sound off headphones. .measurement: no
  /// voice processing.
  private func activate() throws {
    let session = AVAudioSession.sharedInstance()
    try session.setCategory(.playAndRecord, mode: .measurement, options: [.mixWithOthers, .allowBluetoothA2DP])
    if #available(iOS 14.5, *) { try session.setPrefersNoInterruptionsFromSystemAlerts(true) }  // a call as a banner: only answering it interrupts
    try session.setActive(true)
  }

  /// The system took the microphone: the piece being written ends here and the gap starts.
  private func pause() {
    if gapFrom == nil { gapFrom = Recorder.now() }
    error = "\(Recorder.clock(gapFrom!)) 起被系统中断，等待恢复"
    q.sync { finishRun() }
  }

  /// Records again after a pause (interruption over, media services back, the app back in front). iOS may refuse
  /// to start recording from the background: then the gap stays open, the page says since when and the server
  /// gets its start as the end of the recording, and the next try is when the app comes to the front.
  func resume() {
    guard let from = gapFrom else { return }
    do {
      try activate()
      try restart()
      gapFrom = nil
      error = nil
      Recorder.resumed(after: from)
    } catch { failed("中断后恢复失败", error) }
  }

  private func failed(_ what: String, _ e: Error) {
    error = "\(Recorder.clock(gapFrom ?? Recorder.now())) 起没有录音：\(what)（\(e.localizedDescription)），回到前台时重试"
    Recorder.gap(from: gapFrom ?? Recorder.now())
  }

  /// (Re)attach the tap with the input's current format and start a new run at the current wall time. Each tap
  /// has its own resampler and run, so a buffer of the old tap still on its way cannot reach the new ones.
  private func restart() throws {
    let input = engine.inputNode
    input.removeTap(onBus: 0)
    engine.stop()
    let format = input.outputFormat(forBus: 0)
    guard let resampler = AVAudioConverter(from: format, to: out) else { throw NSError(domain: "Slicer", code: 1, userInfo: [NSLocalizedDescriptionKey: "no input (\(format))"]) }
    let run = try q.sync { () throws -> OpusPieces in
      finishRun()
      anchorMs = Recorder.now()
      let r = try OpusPieces(open: { [unowned self] first in
        fileSlice = sliceS()
        return dir.appendingPathComponent("\(anchorMs + first * 1000 / Int64(OpusPieces.rate)).part.ogg")
      }, full: { [unowned self] n in  // the slice length is up, or the state changed to a shorter slice that is already over
        let sr = Int64(OpusPieces.rate), state = sliceS()
        return n >= Int64(fileSlice) * sr || (state < fileSlice && n >= Int64(state) * sr)
      }, closed: { [unowned self] url, _, n in  // <start>.part.ogg becomes <start>_<n>.ogg
        try? FileManager.default.moveItem(at: url, to: url.deletingLastPathComponent().appendingPathComponent(url.lastPathComponent.replacingOccurrences(of: ".part.ogg", with: "_\(n).ogg")))
        files += 1
      })
      self.run = r
      return r
    }
    input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buf, _ in self?.convert(buf, resampler, run) }
    engine.prepare()
    try engine.start()
  }

  private func convert(_ buf: AVAudioPCMBuffer, _ resampler: AVAudioConverter, _ run: OpusPieces) {
    guard let o = AVAudioPCMBuffer(pcmFormat: out, frameCapacity: AVAudioFrameCount(Double(buf.frameLength) * out.sampleRate / buf.format.sampleRate) + 16)
    else { return }
    var fed = false, err: NSError?
    resampler.convert(to: o, error: &err) { _, status in
      if fed { status.pointee = .noDataNow; return nil }
      fed = true; status.pointee = .haveData; return buf
    }
    if let err { error = "\(err)"; return }
    if o.frameLength > 0 { q.async { do { try run.write(o) } catch { self.error = "\(error)" } } }
  }

  private func finishRun() {  // on q
    do { try run?.finish() } catch { self.error = "\(error)" }
    run = nil
  }
}

/// Uploads the .ogg files (.aac from the app before) in start order, one at a time, retrying forever; a file is archived or deleted once the
/// server has it, so the directory is the offline queue.
final class Uploader: Thread {
  var server = "", token = "", device = "phone", archive: URL?
  private(set) var sent = 0
  var note: String?
  private let dir: URL
  private var fails = 0

  init(dir: URL) { self.dir = dir }

  /// A queued file's start (unix ms) and sample count: <start>_<n>.ogg, or <start>.ogg for a piece a kill cut short.
  static func piece(_ f: URL) -> (start: Int64, n: Int64?)? {
    let p = f.deletingPathExtension().lastPathComponent.split(separator: "_").map { Int64($0) }
    guard ["ogg", "aac"].contains(f.pathExtension), (1...2).contains(p.count), let start = p[0] else { return nil }
    return (start, p.count == 2 ? p[1] : nil)
  }

  static func files(_ dir: URL) -> [URL] {
    ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
      .compactMap { f in piece(f).map { (f, $0.start) } }
      .sorted { $0.1 < $1.1 }.map { $0.0 }
  }

  static func pending(_ dir: URL) -> Int { files(dir).count }

  override func main() {
    while true {
      guard let f = Uploader.files(dir).first, let p = Uploader.piece(f) else { Thread.sleep(forTimeInterval: 1); continue }
      do {
        let code = try post("\(server)/api/chunks?device=\(enc(device))&start=\(p.start)" + (p.n.map { "&n=\($0)" } ?? ""),
                            try Data(contentsOf: f), f.pathExtension == "ogg" ? "audio/ogg" : "audio/aac")
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
    let day = archive.appendingPathComponent(fmt.string(from: Date(timeIntervalSince1970: Double(Uploader.piece(f)!.start) / 1000)))
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
