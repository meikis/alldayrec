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

/** Foreground service that owns recording and uploading, so neither needs the Flutter UI alive.
 *  START_STICKY: when the system kills the process the service comes back with a null intent and
 *  resumes from the config saved in SharedPreferences, noting the interruption. */
class RecorderService : Service() {
    companion object {
        const val CHANNEL = "live_record"
        @Volatile var slicer: Slicer? = null
        @Volatile var uploader: Uploader? = null
        @Volatile var interruptedAt: Long = 0
        @Volatile var foregroundApp = true
        fun status(): Map<String, Any?> = mapOf(
            "running" to (slicer != null), "files" to slicer?.files, "slice" to slicer?.sliceS(), "error" to slicer?.error,
            "sent" to uploader?.sent, "pending" to uploader?.pending(), "note" to uploader?.note,
            "interruptedAt" to if (interruptedAt > 0) interruptedAt else null)
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
        if (intent?.action == "stop") {
            Thread { stopAll(prefs); stopForeground(STOP_FOREGROUND_REMOVE); stopSelf() }.start()
            return START_NOT_STICKY
        }
        // a start from the UI carries extras; after a kill the system may redeliver them, so a restart is
        // recognised by the process: a new pid while the last run was still active
        if (slicer == null && prefs.getBoolean("active", false) && prefs.getInt("pid", 0) != Process.myPid()) {
            interruptedAt = System.currentTimeMillis()
            Log.w("live-record", "restarted after being killed")
        }
        if (intent?.extras != null) {
            prefs.edit().apply {
                for (k in listOf("server", "token", "device")) putString(k, intent.getStringExtra(k))
                for (k in listOf("foreground", "background", "screenOff")) putInt(k, intent.getIntExtra(k, 20))
                putBoolean("archive", intent.getBooleanExtra("archive", true))
            }.apply()
        } else if (!prefs.getBoolean("active", false)) {
            stopSelf(); return START_NOT_STICKY
        }
        prefs.edit().putBoolean("active", true).putInt("pid", Process.myPid()).apply()
        startForeground(1, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        if (slicer == null) {
            val dir = File(filesDir, "chunks").apply { mkdirs() }
            dir.listFiles { f -> f.name.endsWith(".part") }?.forEach { it.renameTo(File(it.path.removeSuffix(".part") + ".ogg")) }  // cut short by a kill: still valid Ogg
            Log.i("live-record", "starting slicer")
            slicer = Slicer(dir, prefs.getInt("foreground", 5), prefs.getInt("background", 20), prefs.getInt("screenOff", 60)).also {
                it.foreground = foregroundApp
                it.screenOff = !(getSystemService(POWER_SERVICE) as PowerManager).isInteractive
                it.start()
            }
            val archive = if (prefs.getBoolean("archive", true)) File(getExternalFilesDir(null), "recordings") else null
            uploader = Uploader(dir, prefs.getString("server", "")!!, prefs.getString("token", "")!!, prefs.getString("device", "phone")!!, archive)
                .also { it.start() }
        }
        return START_STICKY
    }

    private fun stopAll(prefs: android.content.SharedPreferences) {
        val at = System.currentTimeMillis()
        prefs.edit().putBoolean("active", false).apply()
        slicer?.stop(); slicer = null
        uploader?.let { u ->
            val t0 = System.currentTimeMillis()
            while (u.pending() > 0 && System.currentTimeMillis() - t0 < 120_000) Thread.sleep(500)
            u.running = false
            u.end(at)  // the server may close the conversation now
        }
        uploader = null
    }

    private fun notification(): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val text = if (interruptedAt > 0) "曾在 ${SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(interruptedAt))} 被系统中断，已自动恢复" else ""
        return Notification.Builder(this, CHANNEL).setContentTitle("live-record 正在录音").setContentText(text)
            .setSmallIcon(android.R.drawable.ic_btn_speak_now).setContentIntent(open).setOngoing(true).build()
    }

    override fun onDestroy() {
        Log.w("live-record", "onDestroy slicer=${slicer != null}")
        unregisterReceiver(screen)
        slicer?.stop(); slicer = null
        uploader?.running = false; uploader = null
        super.onDestroy()
    }
}

/** Uploads the .ogg files in start order, one at a time, retrying forever; a file is archived or
 *  deleted once the server has it, so the directory is the offline queue. */
class Uploader(private val dir: File, private val server: String, private val token: String, private val device: String,
               private val archive: File?) : Thread() {
    @Volatile var running = true
    @Volatile var sent = 0
    @Volatile var note: String? = null
    private var fails = 0

    fun pending() = dir.listFiles { f -> f.name.endsWith(".ogg") }?.size ?: 0

    private fun files() = dir.listFiles { f -> f.name.endsWith(".ogg") }?.sortedBy { it.name } ?: emptyList()

    override fun run() {
        while (running) {
            val f = files().firstOrNull()
            if (f == null) { sleep(1000); continue }
            try {
                val code = post("$server/api/chunks?device=${enc(device)}&start=${f.nameWithoutExtension}", f.readBytes(), "audio/ogg")
                when {
                    code == 200 -> { keep(f); sent++; fails = 0; note = null }
                    code == 429 -> { note = "服务端积压中，20 秒后继续"; sleep(20_000) }
                    code == 400 -> { f.delete(); note = "服务端无法解码一块，已丢弃" }
                    else -> throw Exception("HTTP $code")
                }
            } catch (e: Exception) {
                fails++
                note = "上传失败 ${e.message}"
                sleep(5000L * minOf(fails, 6))
            }
        }
    }

    fun end(atMs: Long) {
        try { post("$server/api/chunks/end?device=${enc(device)}&t=$atMs", ByteArray(0), null) } catch (e: Exception) { note = "结束信号失败 ${e.message}" }
    }

    private fun keep(f: File) {
        if (archive == null) { f.delete(); return }
        val day = File(archive, SimpleDateFormat("yyyy-MM-dd", Locale.US).format(Date(f.nameWithoutExtension.toLong()))).apply { mkdirs() }
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
        c.disconnect()
        return code
    }

    private fun enc(s: String) = URLEncoder.encode(s, "UTF-8")
}
