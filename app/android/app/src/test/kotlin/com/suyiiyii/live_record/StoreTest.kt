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
