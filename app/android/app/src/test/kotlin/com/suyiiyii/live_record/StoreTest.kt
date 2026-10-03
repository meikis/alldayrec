package com.suyiiyii.live_record

import org.junit.Assert.assertEquals
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** The same cases as firmware/devkit/lr_sync.py --selftest. */
class StoreTest {
    private fun le(n: Int, fill: ByteBuffer.() -> Unit) = ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN).apply(fill).array()
    private fun a(first: Int, n: Int) = le(6) { put('A'.code.toByte()); put(n.toByte()); putInt(first) } +
        (0 until n).flatMap { listOf<Byte>(2, it.toByte(), it.toByte()) }.toByteArray()
    private fun t(frame: Int, epoch: Long) = le(13) { put('T'.code.toByte()); putInt(frame); putLong(epoch) }

    @Test fun runs() {
        val files = mapOf(
            (1L shl 16) to a(0, 3) + t(2, 10_000) + a(3, 2) + a(9, 1),  // a gap at 5..8
            ((1L shl 16) or 1) to a(10, 1).dropLast(1).toByteArray(),  // cut short
            (2L shl 16) to a(0, 2),  // rebooted, never synced: time unknown
        )
        val got = Store.runs(files, Triple(3, 50L, 99_000L)).map { it.startMs to it.frames.size }
        assertEquals(listOf(9_980L to 5, 10_070L to 1, null to 2), got)
    }

    @Test fun ogg() {
        val ogg = Store.oggOpus(List(120) { byteArrayOf(0xF8.toByte(), 0xFF.toByte(), 0xFE.toByte()) })
        assertEquals(5, Regex("OggS").findAll(String(ogg, Charsets.ISO_8859_1)).count())
    }
}

/** Issue #25: the three ways a live frame's arrival disagrees with the count. */
class BleClockTest {
    @Test fun clock() {
        val c = BleClock(end = 1_000)
        assertEquals(1_000L to true, c.next(500))      // a restart: never before the last piece's end
        assertEquals(1_010L to false, c.next(520))     // counted on, though it arrived "early" (within 1 s)
        repeat(98) { c.next(1_000) }                    // frames 3..100: due 1_020..1_990
        assertEquals(2_000L to false, c.next(2_100))   // 100 ms late: still counted
        assertEquals(5_000L to true, c.next(5_000))    // 3 s late: frames were lost, a new run at arrival
        val (t, fresh) = c.next(3_000)                  // the count (5_010) 2 s ahead of arrival: re-anchored at arrival
        assertEquals(3_000L to true, t to fresh)
        c.brk()
        assertEquals(3_010L to true, c.next(2_000))    // after a gap: not before where the last frame ended
    }
}
