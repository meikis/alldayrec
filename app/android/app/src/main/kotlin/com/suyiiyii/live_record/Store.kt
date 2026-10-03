package com.suyiiyii.live_record

import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.random.Random

/** The BLE recorder's offline store (firmware src/lr_store.h, reference client firmware/devkit/lr_sync.py): files of
 *  'A' records (consecutive 10 ms Opus frames from `first`) and 'T' records (frame `frame` was at `epochMs`), named
 *  boot << 16 | segment. A frame's wall time is its boot's sync + 10 ms per frame. Plain Kotlin: the unit test runs it. */
object Store {
    const val FRAME_MS = 10L
    const val PIECE_FRAMES = 6000  // an offline run goes up in 60 s pieces

    sealed class Rec
    class Audio(val first: Long, val frames: List<ByteArray>) : Rec()
    class Sync(val frame: Long, val epochMs: Long) : Rec()

    /** The records of one file in order; a record cut short by power loss ends it. */
    fun records(b: ByteArray): List<Rec> {
        val out = mutableListOf<Rec>()
        val v = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)
        var i = 0
        while (i < b.size) {
            if (b[i] == 'A'.code.toByte() && i + 6 <= b.size) {
                val n = b[i + 1].toInt() and 0xff
                val first = v.getInt(i + 2).toLong() and 0xffffffffL
                var j = i + 6
                val frames = ArrayList<ByteArray>(n)
                repeat(n) {
                    if (j >= b.size) return out
                    val len = b[j].toInt() and 0xff
                    if (j + 1 + len > b.size) return out
                    frames += b.copyOfRange(j + 1, j + 1 + len)
                    j += 1 + len
                }
                out += Audio(first, frames)
                i = j
            } else if (b[i] == 'T'.code.toByte() && i + 13 <= b.size) {
                out += Sync(v.getInt(i + 1).toLong() and 0xffffffffL, v.getLong(i + 5))
                i += 13
            } else return out
        }
        return out
    }

    /** A run of consecutive frames: its first frame's wall time (null: its boot was never synced) and the frames. */
    class Run(val boot: Int, val startMs: Long?, val frames: MutableList<ByteArray>)

    /** files: file id -> bytes; live: (boot, frame, epochMs) read at this connection (its 'T' may sit in the open
     *  segment). The nearest sync of the boot dates a run (a later one is as good: the mic's clock runs on). */
    fun runs(files: Map<Long, ByteArray>, live: Triple<Int, Long, Long>?): List<Run> {
        val syncs = HashMap<Int, MutableList<Sync>>()
        live?.let { syncs.getOrPut(it.first) { mutableListOf() } += Sync(it.second, it.third) }
        val audio = sortedMapOf<Int, MutableList<Audio>>()
        for ((f, b) in files.toSortedMap()) {
            val boot = (f shr 16).toInt()
            for (r in records(b)) when (r) {
                is Sync -> syncs.getOrPut(boot) { mutableListOf() } += r
                is Audio -> audio.getOrPut(boot) { mutableListOf() } += r
            }
        }
        val out = mutableListOf<Run>()
        for ((boot, recs) in audio) {
            val ts = syncs[boot]
            fun at(frame: Long) = ts?.minByOrNull { Math.abs(it.frame - frame) }?.let { it.epochMs + (frame - it.frame) * FRAME_MS }
            var first = -1L
            var cur: Run? = null
            for (a in recs) {
                if (cur != null && a.first == first + cur.frames.size) { cur.frames += a.frames; continue }
                cur?.let { out += it }
                first = a.first
                cur = Run(boot, at(a.first), a.frames.toMutableList())
            }
            cur?.let { out += it }
        }
        return out
    }

    /** A dated run as Ogg pieces in `dir`, named start@tag.ogg like the slicer's (written .part, then renamed, so the
     *  uploader never takes half a file); returns the pieces. */
    fun writePieces(run: Run, dir: File, tag: String): List<File> {
        val start = run.startMs ?: return emptyList()
        return run.frames.chunked(PIECE_FRAMES).mapIndexed { k, frames ->
            val name = "${start + k * PIECE_FRAMES * FRAME_MS}@$tag"
            val part = File(dir, "$name.part")
            part.writeBytes(oggOpus(frames))
            File(dir, "$name.ogg").also { part.renameTo(it) }
        }
    }

    private val CRC = IntArray(256) { i ->
        var r = i shl 24
        repeat(8) { r = if (r and 0x80000000.toInt() != 0) (r shl 1) xor 0x04c11db7 else r shl 1 }
        r
    }

    private fun page(packets: List<ByteArray>, flags: Int, granule: Long, serial: Int, seq: Int): ByteArray {
        val lacing = ByteArrayOutputStream()
        for (p in packets) {
            var n = p.size
            while (true) { lacing.write(minOf(n, 255)); if (n < 255) break; n -= 255 }
        }
        val lace = lacing.toByteArray()
        val body = packets.sumOf { it.size }
        val out = ByteBuffer.allocate(27 + lace.size + body).order(ByteOrder.LITTLE_ENDIAN)
        out.put("OggS".toByteArray()).put(0).put(flags.toByte()).putLong(granule).putInt(serial).putInt(seq).putInt(0)
            .put(lace.size.toByte()).put(lace)
        packets.forEach { out.put(it) }
        val a = out.array()
        var crc = 0
        for (b in a) crc = (crc shl 8) xor CRC[((crc ushr 24) xor (b.toInt() and 0xff)) and 0xff]
        ByteBuffer.wrap(a, 22, 4).order(ByteOrder.LITTLE_ENDIAN).putInt(crc)
        return a
    }

    /** 16 kHz mono Opus packets of FRAME_MS each into one Ogg Opus file (RFC 7845), as web/src/lib/ogg.ts does. */
    fun oggOpus(frames: List<ByteArray>): ByteArray {
        val serial = Random.nextInt()
        val head = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN)
            .put("OpusHead".toByteArray()).put(1).put(1).putShort(0).putInt(16000).putShort(0).put(0).array()
        val tags = "OpusTags".toByteArray() + ByteArray(8)
        val out = ByteArrayOutputStream()
        out.write(page(listOf(head), 2, 0, serial, 0))
        out.write(page(listOf(tags), 0, 0, serial, 1))
        var seq = 2
        for (i in frames.indices step 50) {
            val end = minOf(i + 50, frames.size)
            out.write(page(frames.subList(i, end), if (end == frames.size) 4 else 0, end * FRAME_MS * 48, serial, seq++))
        }
        return out.toByteArray()
    }
}
