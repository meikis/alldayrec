package com.suyiiyii.live_record

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.file.Files

/** The record stream as the phone takes it: mirror (LIVE with holes READ again), pieces dated by the nearest 'T',
 *  frames held for a boot without one, closed files finished and deletable once their pieces are uploaded. */
class StoreTest {
    private fun le(n: Int, fill: ByteBuffer.() -> Unit) = ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN).apply(fill).array()
    private fun a(first: Int, n: Int) = le(6) { put('A'.code.toByte()); put(n.toByte()); putInt(first) } +
        (0 until n).flatMap { listOf<Byte>(2, it.toByte(), it.toByte()) }.toByteArray()
    private fun t(frame: Int, epoch: Long) = le(13) { put('T'.code.toByte()); putInt(frame); putLong(epoch) }
    private fun tmp() = Files.createTempDirectory("lr").toFile()
    private fun names(out: File) = out.list()!!.filter { it.endsWith(".ogg") }.sortedBy { it.substringBefore("@").toLong() }

    @Test fun mirror() {
        val dir = tmp(); val out = tmp(); val undated = tmp()
        val f = 1L shl 16 or 3  // boot 1, segment 3: the open file
        val stream = a(0, 10) + a(10, 10) + t(20, 1_000_000) + a(20, 10) + a(40, 10)  // frames 30..39 lost on the way
        val m = Mirror(dir)
        assertNull(m.put(f, 0, stream.copyOfRange(0, 40)))
        assertEquals(40L, m.put(f, 60, stream.copyOfRange(60, 80)))  // a lost notify: READ from 40
        assertNull(m.put(f, 30, stream.copyOfRange(30, stream.size)))  // READ: overlaps what is there
        assertEquals(stream.size.toLong(), m.have(f))
        m.listed(mapOf(f to 80L), f)

        // the open file: pieces of >= 15 frames at record ends, a gap ends one, the last frames wait
        assertEquals(2, m.pieces(out, undated, "LR-P1-BDAE", 15, 1))
        assertEquals(listOf("999800@LR-P1-BDAE.ogg", "1000000@LR-P1-BDAE.ogg"), names(out))

        // the stream moved on (the board rebooted: boot 2): this file is closed and whole, its last frames go too
        val g = 2L shl 16
        assertNull(m.put(g, 0, a(0, 5)))  // boot 2, no 'T' yet: waits
        m.listed(mapOf(f to stream.size.toLong(), g to 21L), g)
        assertEquals(1, m.pieces(out, undated, "LR-P1-BDAE", 15, 2))
        assertEquals("1000200@LR-P1-BDAE.ogg", names(out).last())
        assertEquals(emptyList<Long>(), m.done(out))  // pieces not uploaded yet
        out.listFiles()!!.forEach { it.delete() }   // the uploader took them
        assertEquals(listOf(f), m.done(out))

        // state survives a restart; boot 2 gets its 'T' later in the stream and its frames go then
        val m2 = Mirror(dir)
        assertEquals(listOf(f), m2.done(out))
        assertNull(m2.put(g, 21, t(5, 2_000_000)))
        assertEquals(0, m2.pieces(out, undated, "LR-P1-BDAE", 15, 2))  // 5 frames of the open file: wait for more
        assertNull(m2.put(g, 34, a(5, 10)))
        assertEquals(1, m2.pieces(out, undated, "LR-P1-BDAE", 15, 2))
        assertEquals(listOf("1999950@LR-P1-BDAE.ogg"), names(out))
        m2.drop(f)
        assertEquals(0L, m2.have(f))
    }

    /** What is said now sent ahead of a backlog still on the card (#130): where its first piece starts, before it is made. */
    @Test fun nextStart() {
        val dir = tmp(); val out = tmp(); val undated = tmp()
        val m = Mirror(dir)
        val old = 1L shl 16 or 1; val open = 1L shl 16 or 2  // a closed file not read yet; the one the stream goes on in
        m.listed(mapOf(old to 72L, open to 85L), open)
        assertNull(m.nextStart(open))  // nothing of the open file yet
        m.put(open, 0, t(100, 5_000_000) + a(100, 10) + a(110, 10))
        assertEquals(5_000_000L, m.nextStart(open))  // its first frame, by its 'T'
        assertEquals(1, m.pieces(out, undated, "X", 15, 1))
        assertEquals(listOf("5000000@X.ogg"), names(out))  // starts where nextStart said
        assertNull(m.nextStart(open))  // all of it in pieces
        m.put(old, 0, a(80, 10) + a(90, 10))  // the backlog, read off the card afterwards
        assertNull(m.nextStart(open))  // older files are not ahead
        assertEquals(4_999_800L, m.nextStart(old))  // dated by the same boot's 'T'
        assertEquals(1, m.pieces(out, undated, "X", 15, 1))
        assertEquals(listOf("4999800@X.ogg", "5000000@X.ogg"), names(out))  // older than what went ahead
    }

    /** #109: a piece appends one line to the state instead of writing all of it again (at 1 s pieces that grew with
     *  every piece of the open file); a restart reads the lines back, finds the 'T' again and goes on where it was. */
    @Test fun stateAppended() {
        val dir = tmp(); val out = tmp(); val undated = tmp()
        val f = 1L shl 16 or 1; val state = File(dir, "state.txt")
        fun key() = Files.readAttributes(state.toPath(), java.nio.file.attribute.BasicFileAttributes::class.java).fileKey()
        val m = Mirror(dir)
        var off = t(0, 1_000_000).size.toLong()
        m.put(f, 0, t(0, 1_000_000))
        m.listed(mapOf(f to off), f)  // as LIST leaves it
        val key = key(); val size = state.length()
        for (s in 0 until 60) {  // a minute in 1 s pieces (the foreground slice), one pieces() a second
            for (i in 0 until 10) { val r = a(s * 100 + i * 10, 10); m.put(f, off, r); off += r.size }
            assertEquals(1, m.pieces(out, undated, "LR-P1-BDAE", 100, 1))
        }
        assertEquals(key, key())  // appended to, never replaced
        assertEquals(true, (state.length() - size) / 60 < 40)  // a line a piece
        val m2 = Mirror(dir)
        assertEquals(m.files[f]!!.cut, m2.files[f]!!.cut)
        assertEquals(m.files[f]!!.pieces, m2.files[f]!!.pieces)
        assertEquals(0, m2.pieces(out, undated, "LR-P1-BDAE", 100, 1))  // nothing made again
        for (i in 0 until 10) { val r = a(6000 + i * 10, 10); m2.put(f, off, r); off += r.size }
        assertEquals(1, m2.pieces(out, undated, "LR-P1-BDAE", 100, 1))
        assertEquals(61, names(out).size)
        assertEquals("1060000@LR-P1-BDAE.ogg", names(out).last())  // dated by the 'T' scanned again
        m2.listed(mapOf(f to off), f)  // the next whole save folds the lines in
        assertEquals(false, state.readText().contains("\nC "))
        assertEquals(61, Mirror(dir).files[f]!!.pieces.size)
    }

    @Test fun undated() {
        val dir = tmp(); val out = tmp(); val undated = tmp()
        val m = Mirror(dir)
        val f = 4L shl 16  // boot 4 lost power before any phone came: no 'T', and boot 5 runs now
        m.put(f, 0, a(0, 10) + a(20, 3))  // two runs (frames 10..19 lost): one undated file
        m.listed(mapOf(f to 51L, (5L shl 16) to 0L, (3L shl 16) to 0L), 5L shl 16)  // boot 3's file: listed, empty
        assertEquals(0, m.pieces(out, undated, "LR-X1-567E", 500, 5))
        assertEquals(1, undated.list()!!.size)
        assertEquals(listOf(3L shl 16, f), m.done(out))  // the empty file too: nothing in it, nothing to wait for
    }

    @Test fun fullList() {
        val m = Mirror(tmp())
        val old = 6L shl 16; val open = 7L shl 16 or 2
        m.put(old, 0, a(0, 10))
        // 64 other files: a full LIST, which may have left out `old` and the open file: neither is closed
        m.listed((0L until 64L).associate { (1L shl 16 or it) to 0L }, open)
        assertEquals(false, m.files[old]!!.closed)
        assertEquals(false, m.files[open]!!.closed)
        m.listed(mapOf(open to 0L), open)  // a short LIST: `old` is gone from the card
        assertEquals(true, m.files[old]!!.closed)
    }

    @Test fun ogg() {
        val ogg = Store.oggOpus(List(120) { byteArrayOf(0xF8.toByte(), 0xFF.toByte(), 0xFE.toByte()) })
        assertEquals(5, Regex("OggS").findAll(String(ogg, Charsets.ISO_8859_1)).count())
    }
}

/** The list names a recorder without its scan response (10-04: a recorder busy with another phone gave none). */
class NameTest {
    @Test fun name() {
        assertEquals("LR-X1-567E", Ble.fullName(listOf("LR-X1-567E", null), "ED:AF:08:75:56:7E"))  // scan response
        assertEquals("LR-X1-567E", Ble.fullName(listOf("LR-X1", null), "ED:AF:08:75:56:7E"))       // advertisement only
        assertEquals("LR-P1-BDAE", Ble.fullName(listOf(null, "LR-P1-BDAE"), "D6:07:F3:A3:BD:AE"))  // the name cache
        assertEquals(null, Ble.fullName(listOf("Omi", null), "D6:07:F3:A3:BD:AE"))
    }
}
