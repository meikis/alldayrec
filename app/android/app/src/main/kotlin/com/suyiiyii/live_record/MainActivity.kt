package com.suyiiyii.live_record

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import android.os.Build
import android.provider.Settings
import android.view.WindowManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.util.concurrent.BlockingQueue
import java.util.concurrent.TimeUnit

class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(engine: FlutterEngine) {
        super.configureFlutterEngine(engine)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)  // the page is meant to be watched
        MethodChannel(engine.dartExecutor.binaryMessenger, "live_record/recorder").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
                    if (call.argument<String>("source") == "ble") {
                        val need = (if (Build.VERSION.SDK_INT >= 31) listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
                                    else listOf(Manifest.permission.ACCESS_FINE_LOCATION))  // BLE scans need location before Android 12
                            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
                        if (need.isNotEmpty()) {
                            requestPermissions(need.toTypedArray(), 2)
                            return@setMethodCallHandler result.error("permission", "允许蓝牙权限后再点开始", null)
                        }
                    }
                    val i = Intent(this, RecorderService::class.java)
                    for (k in listOf("server", "token", "device", "source")) i.putExtra(k, call.argument<String>(k))
                    for (k in listOf("foreground", "background", "screenOff")) i.putExtra(k, call.argument<Int>(k)!!)
                    i.putExtra("archive", call.argument<Boolean>("archive") ?: true)
                    i.putExtra("nonce", System.currentTimeMillis())
                    startForegroundService(i)
                    result.success(null)
                }
                "stop" -> { startService(Intent(this, RecorderService::class.java).setAction("stop")); result.success(null) }
                "foreground" -> {
                    val on = call.argument<Boolean>("on")!!
                    RecorderService.foregroundApp = on
                    RecorderService.slicer?.foreground = on
                    result.success(null)
                }
                "slices" -> {  // new lengths from the server: the running slicer cuts its next piece with them, a restart reads them
                    val fg = call.argument<Int>("foreground")!!; val bg = call.argument<Int>("background")!!; val off = call.argument<Int>("screenOff")!!
                    getSharedPreferences("recorder", MODE_PRIVATE).edit().putInt("foreground", fg).putInt("background", bg).putInt("screenOff", off).apply()
                    RecorderService.slicer?.apply { fgS = fg; bgS = bg; offS = off }
                    result.success(null)
                }
                "upload" -> {  // pieces left from a stop, a kill or a lost network: send them without recording
                    if (RecorderService.slicer == null && RecorderService.uploader == null)
                        startForegroundService(Intent(this, RecorderService::class.java).setAction("upload"))
                    result.success(null)
                }
                "status" -> result.success(RecorderService.status(File(filesDir, "chunks")))
                "deviceName" -> result.success(Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL)  // the name in the system settings
                else -> result.notImplemented()
            }
        }
    }
}

/** Opus frames, from the microphone's encoder or from the BLE recorder (`packets`), cut into self-contained Ogg files
 *  whose length depends on the app state, and named by the wall time of their first frame. */
class Slicer(private val dir: File, @Volatile var fgS: Int, @Volatile var bgS: Int, @Volatile var offS: Int,
             private val packets: BlockingQueue<Ble.Packet>? = null) {
    @Volatile var foreground = true
    @Volatile var screenOff = false
    @Volatile private var running = false
    @Volatile var files = 0
    @Volatile var error: String? = null
    /** The BLE recorder's name: pieces carry it in their file name (start@name.ogg) and go up as that device. */
    @Volatile var tag: String? = null
    private lateinit var thread: Thread
    private val sr = 16000

    // the piece being written; only the slicer thread touches these
    private var format: MediaFormat? = null
    private var muxer: MediaMuxer? = null
    private var track = 0
    private var part: File? = null
    private var fileStartUs = 0L
    private var fileSlice = 0
    private var lastState = 0

    fun sliceS() = if (screenOff) offS else if (foreground) fgS else bgS

    fun start() {
        android.util.Log.i("live-record", "slicer start fg=$fgS bg=$bgS off=$offS ble=${packets != null}")
        running = true
        lastState = sliceS()
        thread = Thread { try { if (packets != null) bleLoop(packets) else micLoop() } catch (e: Exception) { error = e.toString() } }
        thread.start()
    }

    fun stop() { android.util.Log.i("live-record", "slicer stop"); running = false; thread.join(5000) }

    /** One Opus packet at `ptsUs` after `wallMs`: rotates at a frame boundary when the slice length is up, or the
     *  state changed to a shorter slice that is already over. */
    private fun write(buf: ByteBuffer, offset: Int, size: Int, flags: Int, wallMs: Long, ptsUs: Long) {
        val state = sliceS()
        if (muxer != null && (ptsUs - fileStartUs >= fileSlice * 1_000_000L ||
                (state != lastState && state < fileSlice && ptsUs - fileStartUs >= state * 1_000_000L))) closeFile()
        lastState = state
        if (muxer == null) {
            part = File(dir, "${wallMs + ptsUs / 1000}${tag?.let { "@$it" } ?: ""}.part")
            muxer = MediaMuxer(part!!.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG).also {
                track = it.addTrack(format!!)
                it.start()
            }
            fileStartUs = ptsUs
            fileSlice = state
        }
        muxer!!.writeSampleData(track, buf, MediaCodec.BufferInfo().apply { set(offset, size, ptsUs - fileStartUs, flags) })
    }

    private fun closeFile() {
        muxer?.let { it.stop(); it.release() }
        muxer = null
        part?.let { it.renameTo(File(it.path.removeSuffix(".part") + ".ogg")); files++ }
        part = null
    }

    private fun micLoop() {
        val startWall = System.currentTimeMillis()
        val minBuf = AudioRecord.getMinBufferSize(sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(MediaRecorder.AudioSource.MIC, sr, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, minBuf * 4)
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
        codec.configure(MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, sr, 1).apply {
            setInteger(MediaFormat.KEY_BIT_RATE, 24000)
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, sr * 2)  // one second of PCM
        }, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        codec.start()
        rec.startRecording()
        // The encoder's own header says pre-skip 104: its 6.5 ms lookahead counted at 16 kHz, in a 48 kHz field
        // (AOSP C2SoftOpusEnc), and every piece repeated it, cutting 2.2 ms of real audio at each seam (#45). The
        // encoder runs on across pieces, so only the run's first piece has the delay to skip: 6.5 ms = 312 at 48 kHz.
        val rest = Ble.opusFormat(0)
        format = Ble.opusFormat(312)
        val pcm = ByteArray(sr * 2 / 50)  // 20 ms
        var samples = 0L                  // fed to the encoder
        val info = MediaCodec.BufferInfo()

        fun drain(eos: Boolean) {
            while (true) {
                val i = codec.dequeueOutputBuffer(info, if (eos) 10000 else 0)
                if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) continue
                if (i < 0) { if (eos && i == MediaCodec.INFO_TRY_AGAIN_LATER) continue else break }
                if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                    write(codec.getOutputBuffer(i)!!, info.offset, info.size, info.flags, startWall, info.presentationTimeUs)
                    format = rest
                }
                codec.releaseOutputBuffer(i, false)
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
            }
        }

        while (running) {
            val n = rec.read(pcm, 0, pcm.size)
            if (n <= 0) continue
            val i = codec.dequeueInputBuffer(100_000)
            if (i >= 0) {
                codec.getInputBuffer(i)!!.apply { clear(); put(pcm, 0, n) }
                codec.queueInputBuffer(i, 0, n, samples * 1_000_000L / sr, 0)
                samples += n / 2
            }
            drain(false)
        }
        rec.stop(); rec.release()
        val i = codec.dequeueInputBuffer(100_000)
        if (i >= 0) codec.queueInputBuffer(i, 0, 0, samples * 1_000_000L / sr, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
        drain(true)
        closeFile()
        codec.stop(); codec.release()
    }

    /** The recorder's Opus frames go into the Ogg pieces as they are. It has no clock: a run of frames is anchored at
     *  the phone's time of its first frame and counted on in 10 ms steps. A seq gap, a disconnect, or frames arriving
     *  over 2 s later than counted (lost before they got a seq) end the run: the next frame starts a new piece at its
     *  own arrival time, so later audio is never pulled earlier (Omi app issue #4101). */
    private fun bleLoop(packets: BlockingQueue<Ble.Packet>) {
        format = Ble.opusFormat()
        var anchor = 0L      // wall ms of the run's first frame; 0 = no run
        var frames = 0L      // frames written in the run
        var end = 0L         // wall ms where the last written frame ends: a new run never starts before it
        var seq = -1
        val frame = ByteArrayOutputStream()  // the frame being put together from its packets
        fun breakRun() { frame.reset(); closeFile(); anchor = 0 }

        while (running) {
            val p = packets.poll(200, TimeUnit.MILLISECONDS) ?: continue
            val d = p.data
            if (d == null) { breakRun(); seq = -1; continue }  // disconnected
            if (d.size < 4) continue
            val s = (d[0].toInt() and 0xff) or ((d[1].toInt() and 0xff) shl 8)
            if (seq >= 0 && s != (seq + 1) and 0xffff) {
                android.util.Log.w("live-record", "ble seq gap $seq -> $s")
                breakRun()
            }
            seq = s
            if (d[2].toInt() == 0) {  // a new frame starts: the one before is whole
                if (frame.size() > 0) {
                    val b = frame.toByteArray(); frame.reset()
                    write(ByteBuffer.wrap(b), 0, b.size, 0, anchor, frames * Ble.FRAME_US)
                    frames++
                    end = anchor + frames * Ble.FRAME_US / 1000
                    if (frames % 1000 == 0L) android.util.Log.i("live-record", "ble counted time leads arrival by ${end - p.at} ms")
                }
                if (anchor != 0L && p.at - end > 2000) {
                    android.util.Log.w("live-record", "ble frames ${p.at - end} ms late: new run")
                    closeFile(); anchor = 0
                }
                if (anchor == 0L) { anchor = maxOf(p.at, end); frames = 0 }
            } else if (frame.size() == 0) continue  // the rest of a frame whose start was lost
            frame.write(d, 3, d.size - 3)
        }
        closeFile()  // a frame still being put together is dropped: it may be cut
    }
}
