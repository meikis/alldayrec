package com.suyiiyii.live_record

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.os.Process
import android.util.Log
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.LinkedBlockingQueue

/** Foreground service that owns recording and uploading, so neither needs the Flutter UI alive. Uploading does not
 *  depend on recording: after a stop (or a launch, or a kill, with pieces left) the service stays up as a dataSync
 *  service until the queue is empty, sends the end signal after the last piece and stops itself.
 *  START_STICKY: when the system kills the process the service comes back with a null intent and
 *  resumes from the config saved in SharedPreferences, noting the interruption. */
class RecorderService : Service() {
    companion object {
        const val CHANNEL = "live_record"
        @Volatile var slicer: Slicer? = null
        @Volatile var uploader: Uploader? = null
        @Volatile var link: BleLink? = null  // the BLE recorder, when it is the source instead of the microphone
        @Volatile var interruptedAt: Long = 0
        @Volatile var foregroundApp = true
        fun status(dir: File): Map<String, Any?> {
            val q = Uploader.queue(dir)
            return mapOf(
                "running" to (slicer != null), "uploading" to (uploader != null), "files" to slicer?.files, "slice" to slicer?.sliceS(),
                "error" to slicer?.error, "sent" to uploader?.sent, "note" to uploader?.note,
                "pending" to q.size, "pendingBytes" to q.sumOf { it.length() }, "oldest" to q.firstOrNull()?.let(Uploader::startMs),
                "interruptedAt" to if (interruptedAt > 0) interruptedAt else null, "link" to link?.state, "tag" to slicer?.tag, "hw" to link?.hw,
                "dfu" to Dfu.state, "dfuBusy" to Dfu.busy, "source" to (if (slicer == null) null else if (link != null) "ble" else "mic"), "usbNote" to link?.usbNote, "lastSync" to link?.syncNote,
                "syncDone" to link?.syncDoneKb, "syncTotal" to link?.syncTotalKb)
        }
    }

    private val screen = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) { slicer?.screenOff = i.action == Intent.ACTION_SCREEN_OFF }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
            .createNotificationChannel(NotificationChannel(CHANNEL, "录音中", NotificationManager.IMPORTANCE_LOW))
        registerReceiver(screen, IntentFilter().apply { addAction(Intent.ACTION_SCREEN_OFF); addAction(Intent.ACTION_SCREEN_ON) })
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val prefs = getSharedPreferences("recorder", MODE_PRIVATE)
        Log.i("live-record", "onStartCommand action=${intent?.action} extras=${intent?.extras != null} slicer=${slicer != null} pid=${Process.myPid()} lastPid=${prefs.getInt("pid", 0)}")
        val dir = File(filesDir, "chunks").apply { mkdirs() }
        if (intent?.action == "stop") {
            stopRecording(prefs, dir)
            return START_STICKY
        }
        if (intent?.action == "upload") {  // from the UI: pieces left (and maybe an end) after a kill or a lost network
            if (slicer == null) uploadOnly(prefs, dir, prefs.getLong("endAt", 0L).takeIf { it > 0 })
            return START_STICKY
        }
        // a start from the UI carries a fresh nonce; after a kill the system restarts us with no intent or
        // redelivers the old one, so: same or no nonce while the last run was still active = we were killed
        val nonce = intent?.getLongExtra("nonce", 0L) ?: 0L
        if (nonce != 0L && nonce != prefs.getLong("nonce", 0L)) {
            interruptedAt = 0
            prefs.edit().putLong("nonce", nonce).apply()
        } else if (slicer == null && prefs.getBoolean("active", false) && prefs.getInt("pid", 0) != Process.myPid()) {
            interruptedAt = System.currentTimeMillis()
            Log.w("live-record", "restarted after being killed")
        }
        if (intent?.extras != null) {
            prefs.edit().apply {
                for (k in listOf("server", "token", "device", "source")) putString(k, intent.getStringExtra(k))
                for (k in listOf("foreground", "background", "screenOff")) putInt(k, intent.getIntExtra(k, 20))
                putBoolean("archive", intent.getBooleanExtra("archive", true))
            }.apply()
        } else if (!prefs.getBoolean("active", false)) {  // restarted after a kill while only uploading, or nothing to do
            uploadOnly(prefs, dir, prefs.getLong("endAt", 0L).takeIf { it > 0 })
            return START_STICKY
        }
        prefs.edit().putBoolean("active", true).putInt("pid", Process.myPid()).apply()
        val ble = prefs.getString("source", "mic") == "ble"
        startForeground(1, notification(), if (ble) ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE else ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        if (slicer == null) {
            dir.listFiles { f -> f.name.endsWith(".part") }?.forEach { it.renameTo(File(it.path.removeSuffix(".part") + ".ogg")) }  // cut short by a kill: still valid Ogg
            Log.i("live-record", "starting slicer")
            val packets = if (ble) LinkedBlockingQueue<Ble.Packet>() else null
            val clock = if (ble) BleClock(prefs.getLong("bleEnd", 0L)) else null
            slicer = Slicer(dir, prefs.getInt("foreground", 5), prefs.getInt("background", 20), prefs.getInt("screenOff", 60), packets, clock,
                            { prefs.edit().putLong("bleEnd", it).apply() }).also {
                it.foreground = foregroundApp
                it.screenOff = !(getSystemService(POWER_SERVICE) as PowerManager).isInteractive
                it.start()
            }
            if (packets != null) link = BleLink(this, packets, prefs, dir, File(getExternalFilesDir(null), "undated"),
                { slicer?.tag = it }, { name, hw -> uploader?.report(name, hw) }).also { it.start() }
            prefs.edit().remove("endAt").remove("endDevice").apply()  // a new recording carries on the conversation: no end for the old stop
            ensureUploader(prefs, dir)
        }
        return START_STICKY
    }

    private fun ensureUploader(prefs: android.content.SharedPreferences, dir: File): Uploader {
        uploader?.let { return it }
        val archive = if (prefs.getBoolean("archive", true)) File(getExternalFilesDir(null), "recordings") else null
        return Uploader(dir, prefs.getString("server", "")!!, prefs.getString("token", "")!!, prefs.getString("device", "phone")!!, archive)
            .also { uploader = it; it.start() }
    }

    /** The microphone goes now; the queue keeps uploading, the end signal goes after its last piece. */
    private fun stopRecording(prefs: android.content.SharedPreferences, dir: File) {
        val at = System.currentTimeMillis()
        prefs.edit().putBoolean("active", false).putLong("endAt", at).putString("endDevice", slicer?.tag).apply()  // kept: a kill before the queue empties still ends it
        link?.stop(); link = null
        slicer?.stop(); slicer = null
        uploadOnly(prefs, dir, at)
    }

    /** Stays up while pieces are left and nothing records; then sends `endAt` (if any) and stops. */
    private fun uploadOnly(prefs: android.content.SharedPreferences, dir: File, endAt: Long?) {
        if (Uploader.queue(dir).isEmpty() && endAt == null) { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf(); return }
        startForeground(1, notification(Uploader.queue(dir).size), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        val u = ensureUploader(prefs, dir)
        if (draining) return
        draining = true
        Thread {
            val nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            while (slicer == null && Uploader.queue(dir).isNotEmpty()) {
                nm.notify(1, notification(Uploader.queue(dir).size))
                Thread.sleep(2000)
            }
            draining = false
            if (slicer != null) return@Thread  // recording again: the service carries on as before
            endAt?.let { u.end(it, prefs.getString("endDevice", null)) }  // the server may close the conversation now
            prefs.edit().remove("endAt").remove("endDevice").apply()
            u.running = false; uploader = null
            stopForeground(STOP_FOREGROUND_REMOVE); stopSelf()
        }.start()
    }

    @Volatile private var draining = false

    private fun notification(uploading: Int? = null): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val title = if (uploading != null) "live-record 正在上传 · 剩 $uploading 块" else if (link != null) "live-record 正在同步录音设备" else "live-record 正在录音"
        val text = if (uploading == null && interruptedAt > 0) "曾在 ${SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(interruptedAt))} 被系统中断，已自动恢复" else ""
        return Notification.Builder(this, CHANNEL).setContentTitle(title).setContentText(text)
            .setSmallIcon(if (uploading != null) android.R.drawable.stat_sys_upload else android.R.drawable.ic_btn_speak_now)
            .setContentIntent(open).setOngoing(true).build()
    }

    override fun onDestroy() {
        Log.w("live-record", "onDestroy slicer=${slicer != null}")
        unregisterReceiver(screen)
        link?.stop(); link = null
        slicer?.stop(); slicer = null
        uploader?.running = false; uploader = null
        super.onDestroy()
    }
}

/** Uploads the .ogg files in start order, retrying forever; a file is archived or deleted once the server has it,
 *  so the directory is the offline queue. Back-to-back pieces waiting together go as one request (each piece is a
 *  whole Ogg stream, so their bytes in a row decode as one): with 1 s pieces one request per piece cannot keep up
 *  when a request takes over a second (10-03: the queue grew to 16 s). */
class Uploader(private val dir: File, private val server: String, private val token: String, private val device: String,
               private val archive: File?) : Thread() {
    @Volatile var running = true
    @Volatile var sent = 0
    @Volatile var note: String? = null
    private var fails = 0

    companion object {
        const val MAX_BATCH = 30        // pieces in one request
        const val BATCH_GAP_MS = 1500L  // starts further apart than this are not one stretch (only 1 s pieces get batched)
        /** The pieces waiting, oldest first: the directory is the queue. */
        fun queue(dir: File) = dir.listFiles { f -> f.name.endsWith(".ogg") }?.sortedBy { it.name } ?: emptyList()
        /** A piece's file name is its start (ms) and, for the BLE recorder, @ its name: start@LR-P1-BDAE.ogg */
        fun startMs(f: File) = f.nameWithoutExtension.substringBefore('@').toLong()
        fun deviceOf(f: File) = f.nameWithoutExtension.substringAfter('@', "")
    }

    private fun files() = queue(dir)

    /** The oldest pieces of one device that follow each other without a gap (a stop or a kill leaves one), at most MAX_BATCH. */
    private fun batch(q: List<File>): List<File> {
        val out = mutableListOf(q[0])
        for (f in q.drop(1)) {
            if (out.size >= MAX_BATCH || startMs(f) - startMs(out.last()) > BATCH_GAP_MS || deviceOf(f) != deviceOf(out[0])) break
            out += f
        }
        return out
    }

    override fun run() {
        while (running) {
            val q = files()
            if (q.isEmpty()) { sleep(200); continue }
            val b = batch(q)
            try {
                val code = post("$server/api/chunks?device=${enc(deviceOf(b[0]).ifEmpty { device })}&start=${startMs(b[0])}",
                                b.fold(ByteArray(0)) { acc, f -> acc + f.readBytes() }, "audio/ogg")
                Log.i("live-record", "upload ${b.size} pieces of ${deviceOf(b[0]).ifEmpty { device }} from ${startMs(b[0])}: HTTP $code")
                when {
                    code == 200 -> { b.forEach(::keep); sent += b.size; fails = 0; note = null }
                    code == 400 -> { b.forEach { it.delete() }; note = "服务端无法解码一块，已丢弃" }
                    else -> throw Exception("HTTP $code")
                }
            } catch (e: Exception) {
                Log.w("live-record", "upload failed (${fails + 1}): ${e.message}")
                fails++
                note = "上传失败 ${e.message}"
                sleep(5000L * minOf(fails, 6))
            }
        }
    }

    /** What the phone last read from its BLE recorder, for the server's progress page; best effort, off this thread. */
    fun report(device: String, hw: Map<String, Any?>) = Thread {
        try { post("$server/api/device-status?device=${enc(device)}", org.json.JSONObject(hw).toString().toByteArray(), "application/json") } catch (_: Exception) {}
    }.start()

    fun end(atMs: Long, bleName: String? = null) {
        try { post("$server/api/chunks/end?device=${enc(bleName ?: device)}&t=$atMs", ByteArray(0), null) } catch (e: Exception) { note = "结束信号失败 ${e.message}" }
    }

    private fun keep(f: File) {
        if (archive == null) { f.delete(); return }
        val day = File(archive, SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date(startMs(f)))).apply { mkdirs() }
        try { f.copyTo(File(day, f.name), overwrite = true) } catch (e: Exception) { note = "归档失败 ${e.message}" }  // internal -> external: no rename
        f.delete()
    }

    private fun post(url: String, body: ByteArray, type: String?): Int {
        val c = URL(url).openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.connectTimeout = 15_000; c.readTimeout = 60_000; c.doOutput = true
        c.setRequestProperty("Authorization", "Bearer $token")
        if (type != null) c.setRequestProperty("Content-Type", type)
        c.outputStream.use { it.write(body) }
        val code = c.responseCode
        (if (code < 400) c.inputStream else c.errorStream)?.use { it.readBytes() }  // read to the end: the connection is reused
        return code
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")
}
