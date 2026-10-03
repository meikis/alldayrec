// OpusPieces (Runner/OggOpus.swift) on a Mac, read back with ffmpeg as the server does: every piece decodes to
// exactly its n samples, the pieces of two runs put end to end are the input sample for sample in time (no gap,
// no overlap), and a piece cut short by a kill still decodes.
//   swiftc -o /tmp/oggopus app/ios/Runner/OggOpus.swift app/ios/OggOpusCheck/main.swift && /tmp/oggopus
import AVFoundation

setbuf(stdout, nil)
let sr = 16000
let total = 47 * sr + 1234
let src: [Float] = (0..<total).map { i in  // tones that never line up again (a clear peak at the right lag), slowly swelling
  let t = Double(i) / Double(sr)
  return Float([113.0, 177, 291, 347, 529].reduce(0) { $0 + 0.08 * sin(2 * .pi * $1 * t) } * (1 + 0.5 * sin(t * 3)))
}
let dir = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("oggopus-\(getpid())")
try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
let pcm = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

var pieces: [(url: URL, first: Int, n: Int)] = []  // first: in the whole input
func record(_ from: Int, _ to: Int, slices: [Int]) throws {
  var i = 0
  let run = try OpusPieces(open: { first in dir.appendingPathComponent("\(from + Int(first)).ogg") },
                           full: { n in n >= Int64(slices[i % slices.count]) },
                           closed: { url, first, n in pieces.append((url, from + Int(first), Int(n))); i += 1 })
  var at = from
  while at < to {  // buffers of uneven length, as a tap resampled to 16 kHz gives them
    let len = min(to - at, 1000 + (at * 7919) % 700)
    let b = AVAudioPCMBuffer(pcmFormat: pcm, frameCapacity: AVAudioFrameCount(len))!
    b.frameLength = AVAudioFrameCount(len)
    for k in 0..<len { b.floatChannelData![0][k] = src[at + k] }
    try run.write(b)
    at += len
  }
  try run.finish()
}
let cut = 31 * sr + 77  // an interruption: a second run takes over, its first piece with pre_skip again
try record(0, cut, slices: [sr, sr, 5 * sr, sr / 2, 3 * sr + 5])
try record(cut, total, slices: [20 * sr, sr])

func decode(_ url: URL) -> [Float] {
  let p = Process(), out = Pipe(), err = Pipe()
  p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
  p.arguments = ["ffmpeg", "-v", "error", "-i", url.path, "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"]
  p.standardOutput = out; p.standardError = err
  try! p.run()
  let d = out.fileHandleForReading.readDataToEndOfFile()
  p.waitUntilExit()
  let e = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
  precondition(p.terminationStatus == 0 && e.isEmpty, "ffmpeg: \(e)")
  return d.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
}

var all: [Float] = [], next = 0, bytes = 0
for p in pieces {
  let x = decode(p.url)
  precondition(x.count == p.n, "\(p.url.lastPathComponent): decoded \(x.count), n \(p.n)")
  precondition(p.first == next, "\(p.url.lastPathComponent): starts at \(p.first), the one before ended at \(next)")
  next = p.first + p.n
  all += x
  bytes += (try! p.url.resourceValues(forKeys: [.fileSizeKey]).fileSize!)
}
precondition(all.count == total, "decoded \(all.count) of \(total)")

func corr(_ a: ArraySlice<Float>, _ b: ArraySlice<Float>) -> Float {
  let ab = zip(a, b).reduce(0) { $0 + $1.0 * $1.1 }, aa = a.reduce(0) { $0 + $1 * $1 }, bb = b.reduce(0) { $0 + $1 * $1 }
  return ab / (aa * bb).squareRoot()
}
// where the decoded audio lines up best with the input, early in the first run, in its middle, in the second run, at
// the end: the same everywhere (no piece or run shifts it). Apple's encoder sits 2 samples (0.1 ms) early at 16 kHz
// (ffmpeg's libopus on the same tones: 0), so within 3 samples counts as aligned
func lag(_ at: Int) -> Int { (-200...200).max { corr(all[at + $0 ..< at + $0 + 8000], src[at ..< at + 8000]) < corr(all[at + $1 ..< at + $1 + 8000], src[at ..< at + 8000]) }! }
let lags = [4000, total / 2, cut + 4000, total - 20000].map(lag)
precondition(Set(lags).count == 1 && abs(lags[0]) <= 3, "decoded audio is \(lags) samples off")
let whole = corr(all[0...], src[0...])
let seams = pieces.dropFirst().map { corr(all[$0.first ..< $0.first + 320], src[$0.first ..< $0.first + 320]) }  // first 20 ms of each piece
precondition(whole > 0.85, "correlation \(whole)")

// killed while writing: the file ends mid-page; what is whole still decodes
let killed = dir.appendingPathComponent("killed.ogg")
let long = pieces.max { $0.n < $1.n }!
let body = try Data(contentsOf: long.url)
try body.prefix(body.count * 2 / 3).write(to: killed)
let k = decode(killed).count
precondition(k > 0 && k < long.n, "a cut-short piece decodes to \(k)")

print("ok: \(pieces.count) pieces in 2 runs, each decodes to exactly n, \(all.count) samples gap-free and aligned (lag \(lags[0]) everywhere, correlation \(whole),"
      + " first 20 ms of a piece min \(seams.min()!)); \(String(format: "%.1f", Double(bytes) * 8 / Double(total) * Double(sr) / 1000)) kbps;"
      + " a piece cut at 2/3 decodes to \(k) of \(long.n)")
try FileManager.default.removeItem(at: dir)
