package com.suyiiyii.live_record

import org.junit.Assert.assertEquals
import org.junit.Test

/** The foreground notification says what the link and the upload are doing (10-06: it never changed). */
class NoticeTest {
    private val now = 1_800_000_000_000L
    private val hw = mapOf("battery" to 68, "store" to 0)
    private fun ble(vararg kv: Pair<String, Any?>) = RecorderService.notice(mapOf("running" to true, "source" to "ble", "pending" to 0) + kv, now)

    @Test fun link() {
        assertEquals("正在找 LR-X1-567E" to "", ble("link" to "正在找 LR-X1-567E", "hw" to hw))  // the battery read before is stale
        assertEquals("已连接 · 实时上传" to "电量 68%", ble("link" to "已连接 · 实时上传", "hw" to hw, "pending" to 1, "oldest" to now - 25_000))
        // at connect only the file being recorded holds back LIVE, not the backlog (old firmware: up to 16 MiB; #135: none)
        assertEquals("接上实时前先取正在录的这段 · 还差 1.2 MB" to "电量 68%",
            ble("link" to "正在接上实时", "hw" to hw, "syncDone" to 512L, "syncTotal" to 45568L, "syncLive" to 1228L))
        assertEquals("正在接上实时" to "", ble("link" to "正在接上实时", "hw" to hw))
        assertEquals("实时上传 · 后台补传 1.5 / 44.5 MB" to "电量 68%",  // #130: a backlog no longer holds back what is said now
            ble("link" to "已连接 · 实时上传", "hw" to hw, "syncDone" to 1536L, "syncTotal" to 45568L))
        assertEquals("已连接 · 实时上传" to "上传失败 HTTP 502 · 已暂停，还剩 15 分钟 · 电量 9% · 存储卡已满 · 待传 40 块",
            ble("link" to "已连接 · 实时上传", "note" to "上传失败 HTTP 502", "pausedUntil" to now + 14 * 60_000 + 1,
                "hw" to mapOf("battery" to 9, "store" to -28), "pending" to 40, "oldest" to now - 300_000))
    }

    @Test fun micAndUpload() {
        assertEquals("live-record 正在录音" to "", RecorderService.notice(mapOf("running" to true, "source" to "mic", "pending" to 0), now))
        assertEquals("live-record 正在上传 · 剩 7 块" to "", RecorderService.notice(mapOf("running" to false, "pending" to 7), now))
    }
}
