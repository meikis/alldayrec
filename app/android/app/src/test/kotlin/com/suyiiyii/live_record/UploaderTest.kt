package com.suyiiyii.live_record

import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/** One request is one stretch from its first piece's start (#130): the backlog queued after what is said now is older
 *  and must not ride along in its batch (the emulator run lost 2 min of backlog that way). */
class UploaderTest {
    private fun f(start: Long, dev: String = "LR-EMU-BEEF") = File("$start@$dev.ogg")

    @Test fun batch() {
        val now = (0 until 3).map { f(1_000_000L + it * 1000) }
        assertEquals(now, Uploader.batch(now + f(900_000)))  // the backlog piece is older: not in it
        assertEquals(now.take(1), Uploader.batch(now.take(1) + f(1_000_000L + 2600)))  // a gap: not in it
        assertEquals(now.take(2), Uploader.batch(now.take(2) + f(1_002_000, "LR-P1-BDAE")))  // another device
        assertEquals(listOf(f(900_000)), Uploader.batch(listOf(f(900_000), f(960_000))))  // 60 s pieces go one by one
    }
}
