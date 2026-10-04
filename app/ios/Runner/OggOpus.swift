// Opus in Ogg pages written by hand (RFC 7845; port of web/src/lib/ogg.ts). AudioToolbox encodes Opus
// (kAudioFormatOpus, iOS 11) but cannot write Ogg ('pck?'), and its CAF/MP4 hold nothing readable after a kill;
// an Ogg page is self-contained, so a piece cut short still decodes up to its last whole page.
// Checked on a Mac against ffmpeg: app/ios/OggOpusCheck/main.swift. The page itself is Ogg.page (Store.swift).
import AVFoundation

/// One run of continuous recording: 16 kHz mono float in, one Opus encoder (20 ms, 24 kbps), cut into Ogg Opus
/// pieces on packet boundaries. Positions are input samples since the run began; a piece holds [first, first + n)
/// and the pieces of a run follow each other without a gap. Only the first piece of a run says pre_skip (the
/// encoder's delay): the first packet of a later piece is already audio. Granule positions run on across the pieces
/// of a run, and the last page of the run trims the encoder's end padding, so each piece decodes to exactly n.
final class OpusPieces {
  static let rate = 16000.0
  private static let frame: Int64 = 320  // samples per packet (20 ms)
  private static let perPage = 50  // packets: a page a second
  private static let pcm = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false)!
  private let opus: AVAudioFormat
  private let encoder: AVAudioConverter
  private let delay: Int64  // samples the encoder puts before the audio: 104 (6.5 ms)
  private var packets: Int64 = 0, fed: Int64 = 0, finished = false
  private var piece: Piece?
  private let open: (Int64) throws -> URL  // a file for the piece starting at this sample
  private let full: (Int64) -> Bool  // whether the open piece, holding this many samples, ends here
  private let closed: (URL, Int64, Int64) -> Void  // a piece is complete: its file, first sample, n

  private struct Piece {
    let url: URL, h: FileHandle, first: Int64, serial = UInt32.random(in: 0...UInt32.max)
    var seq: UInt32 = 0, pending: [Data] = [], granule: Int64 = 0
  }

  init(open: @escaping (Int64) throws -> URL, full: @escaping (Int64) -> Bool, closed: @escaping (URL, Int64, Int64) -> Void) throws {
    var d = AudioStreamBasicDescription(mSampleRate: Self.rate, mFormatID: kAudioFormatOpus, mFormatFlags: 0, mBytesPerPacket: 0,
                                        mFramesPerPacket: UInt32(Self.frame), mBytesPerFrame: 0, mChannelsPerFrame: 1, mBitsPerChannel: 0, mReserved: 0)
    guard let opus = AVAudioFormat(streamDescription: &d), let encoder = AVAudioConverter(from: Self.pcm, to: opus) else {
      throw NSError(domain: "OpusPieces", code: 1, userInfo: [NSLocalizedDescriptionKey: "no Opus encoder"])
    }
    encoder.bitRate = 24000
    self.opus = opus; self.encoder = encoder; delay = Int64(encoder.primeInfo.leadingFrames)
    self.open = open; self.full = full; self.closed = closed
  }

  /// 16 kHz mono float samples, in order. Ignored once the run is finished (a buffer still on its way).
  func write(_ b: AVAudioPCMBuffer) throws {
    guard !finished else { return }
    fed += Int64(b.frameLength)
    try encode(b)
  }

  /// The run ends: the encoder's last packets go out and the open piece closes at the last sample fed.
  func finish() throws {
    guard !finished else { return }
    finished = true
    try encode(nil)
    if piece != nil { try close(end: fed) }
  }

  private func encode(_ b: AVAudioPCMBuffer?) throws {
    var given = false
    while true {
      let out = AVAudioCompressedBuffer(format: opus, packetCapacity: 16, maximumPacketSize: max(encoder.maximumOutputPacketSize, 1500))
      var err: NSError?
      let status = encoder.convert(to: out, error: &err) { _, s in
        if b == nil { s.pointee = .endOfStream; return nil }
        if given { s.pointee = .noDataNow; return nil }
        given = true; s.pointee = .haveData; return b
      }
      if let err { throw err }
      for i in 0..<Int(out.packetCount) {
        let d = out.packetDescriptions![i]
        try add(Data(bytes: out.data + Int(d.mStartOffset), count: Int(d.mDataByteSize)))
      }
      if status != .haveData { return }
    }
  }

  private func add(_ packet: Data) throws {
    let k = packets
    packets += 1
    if k * Self.frame - delay >= fed && finished { return }  // only end padding
    if piece == nil {
      let first = k == 0 ? 0 : k * Self.frame - delay
      let url = try open(first)
      FileManager.default.createFile(atPath: url.path, contents: nil)
      piece = Piece(url: url, h: try FileHandle(forWritingTo: url), first: first)
      var head = Data("OpusHead".utf8) + [1, 1]  // version, channels
      head += le(UInt16(k == 0 ? delay * 3 : 0)) + le(UInt32(Self.rate)) + [0, 0, 0]  // pre-skip (48 kHz), input rate, gain, mapping
      try page([head], flags: 2, granule: 0)
      try page([Data("OpusTags".utf8) + le(UInt32(0)) + le(UInt32(0))], flags: 0, granule: 0)  // no vendor, no comments
    }
    let lacing = piece!.pending.reduce(packet.count / 255 + 1) { $0 + $1.count / 255 + 1 }  // a page has at most 255
    if piece!.pending.count == Self.perPage || lacing > 255 { try page(piece!.pending, flags: 0, granule: piece!.granule); piece!.pending = [] }
    piece!.pending.append(packet)
    piece!.granule = (k + 1) * Self.frame * 3
    let end = (k + 1) * Self.frame - delay
    if !finished && full(end - piece!.first) { try close(end: end) }
  }

  /// The pages still pending, the last one ending the stream at `end` (input samples).
  private func close(end: Int64) throws {
    try page(piece!.pending, flags: 4, granule: (end + delay) * 3)
    let p = piece!
    piece = nil
    try p.h.close()
    closed(p.url, p.first, end - p.first)
  }

  private func page(_ packets: [Data], flags: UInt8, granule: Int64) throws {
    let out = Ogg.page(packets, flags: flags, granule: granule, serial: piece!.serial, seq: piece!.seq)  // Store.swift
    piece!.seq += 1
    try piece!.h.write(contentsOf: out)  // unbuffered: on disk for the next launch even if the process dies
  }

  private func le<T: FixedWidthInteger>(_ v: T) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
}
