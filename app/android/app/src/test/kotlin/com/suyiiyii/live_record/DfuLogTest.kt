package com.suyiiyii.live_record

import org.junit.Assert.assertEquals
import org.junit.Test

/** What an update leaves in the device log (DfuLog), StoreCheck's case for Dfu.swift: its steps with the fields the
 *  server's upgrades() reads, a line every 25 %, the stage a failure got to, and the revision the recorder came back on. */
class DfuLogTest {
    private data class Line(val text: String, val level: String?, val f: Map<String, Any?>)
    private val got = mutableListOf<Line>()
    private val out = { d: String, t: String, l: String?, f: Map<String, Any?> -> assertEquals("LR-P3-116C", d); got += Line(t, l, f) }
    private val old = "20261006.120000-1751f88"
    private val new = "20261007.023223-c95516b"
    private val sha = "858b5aaa" + "0".repeat(56)
    private fun ago(ms: Long) = System.currentTimeMillis() - ms

    @Test fun done() {
        val l = DfuLog("LR-P3-116C", old, new, 343133, sha, 83, out)
        l.t0 = ago(8_500); l.reached("boot")
        l.t0 = ago(21_000); l.reached("send")
        for (p in listOf(0, 10, 24, 25, 26, 49, 51, 76, 99, 100)) l.progress(p, 1.94)
        l.t0 = ago(192_400); l.done()
        l.ended = ago(28_200); l.back(new)
        assertEquals(listOf("start", "boot", "send", "progress", "progress", "progress", "done", "back"), got.map { it.f["step"] })
        assertEquals(listOf("开始升级 $old → $new，335 KB，sha 858b5aaa，电量 83%", "进入升级模式，开始后 8 秒", "开始传输，开始后 21 秒",
                            "传输 25%，平均 1.9 KB/s", "传输 51%，平均 1.9 KB/s", "传输 76%，平均 1.9 KB/s", "升级完成，用时 3 分 12 秒",
                            "升级后连回，用时 28 秒，固件 $new（目标版本）"), got.map { it.text })
        assertEquals(List(8) { null }, got.map { it.level })
        assertEquals(listOf(old, new, "858b5aaa", 343133, 83, 0L), listOf("from", "to", "sha", "bytes", "battery", "ms").map { got[0].f[it] })
        assertEquals(listOf(25, 51), listOf(got[3].f["pct"], got[4].f["pct"]))
        assertEquals(1.9, got[3].f["kbps"])
        assertEquals(1924L, (got[6].f["ms"] as Long) / 100)  // since the start
        assertEquals(282L, (got[7].f["ms"] as Long) / 100)   // since the end
        assertEquals(true, got[7].f["ok"])
    }

    @Test fun failed() {
        val l = DfuLog("LR-P3-116C", new, old, 343133, sha, null, out)
        l.reached("boot"); l.reached("send"); l.progress(40, 2.0)
        l.fail(4102, "Device disconnected")
        l.back(new)
        assertEquals("开始升级 $new → $old，335 KB，sha 858b5aaa，电量 未知", got[0].text)
        assertEquals(Line("升级失败（传输到 40%）：Device disconnected（错误 4102）", "error",
                          mapOf("step" to "fail", "stage" to "send", "pct" to 40, "code" to 4102, "error" to "Device disconnected")),
                     got[4].copy(f = got[4].f - "ms"))
        assertEquals(Line("升级失败后连回，用时 0 秒，固件 $new，不是目标版本", null, mapOf("step" to "back", "fw" to new, "ok" to false)),
                     got[5].copy(f = got[5].f - "ms"))

        got.clear()
        DfuLog("LR-P3-116C", null, new, 0, sha, 50, out).fail(null, "升级已取消")
        assertEquals("升级失败（进入升级模式前）：升级已取消", got[1].text)
        assertEquals(listOf(null, null), listOf(got[0].f["from"], got[1].f["code"]))
        val done = DfuLog("LR-P3-116C", old, new, 0, sha, 50, out)
        done.done(); done.back(old)
        assertEquals(listOf("升级后连回，用时 0 秒，固件 $old，不是目标版本", "warn"), listOf(got.last().text, got.last().level))
    }
}
