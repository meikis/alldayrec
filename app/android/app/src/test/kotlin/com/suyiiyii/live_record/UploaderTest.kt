package com.suyiiyii.live_record

import android.content.SharedPreferences
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File
import java.lang.reflect.Proxy

/** One request is one stretch from its first piece's start: the backlog queued after what is said now is older
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

    /** A recorder's queue in its status: only its own finished pieces, the oldest's age from its start. */
    @Test fun queued() {
        val dir = kotlin.io.path.createTempDirectory().toFile()
        File(dir, "1000@LR-EMU-BEEF.ogg").writeBytes(ByteArray(2048))
        File(dir, "2000@LR-EMU-BEEF.ogg").writeBytes(ByteArray(1024))
        File(dir, "500@LR-P1-BDAE.ogg").writeBytes(ByteArray(4096))   // another recorder's
        File(dir, "3000@LR-EMU-BEEF.part").writeBytes(ByteArray(4096))  // still being written
        val prefs = Proxy.newProxyInstance(javaClass.classLoader, arrayOf(SharedPreferences::class.java)) { _, _, _ -> null } as SharedPreferences
        assertEquals(mapOf<String, Any?>("queue_n" to 2, "queue_kb" to 3L, "queue_age_s" to 60L, "upload_fails" to 0, "upload_ok_s" to null),
                     Uploader(prefs, dir, "", "", "phone", null).queued("LR-EMU-BEEF", now = 61_000))
        dir.deleteRecursively()
    }
}
