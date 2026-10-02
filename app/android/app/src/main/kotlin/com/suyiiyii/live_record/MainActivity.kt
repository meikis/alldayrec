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
import android.view.WindowManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(engine: FlutterEngine) {
        super.configureFlutterEngine(engine)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)  // the page is meant to be watched
        MethodChannel(engine.dartExecutor.binaryMessenger, "live_record/recorder").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                        requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
                    val i = Intent(this, RecorderService::class.java)
                    for (k in listOf("server", "token", "device")) i.putExtra(k, call.argument<String>(k))
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
                "upload" -> {  // pieces left from a stop, a kill or a lost network: send them without recording
                    if (RecorderService.slicer == null && RecorderService.uploader == null)
                        startForegroundService(Intent(this, RecorderService::class.java).setAction("upload"))
                    result.success(null)
                }
                "status" -> result.success(RecorderService.status(File(filesDir, "chunks")))
                else -> result.notImplemented()
            }
        }
    }
}

/** The microphone stays open; the continuous Opus frame stream is cut into self-contained Ogg files
 *  whose length depends on the app state, and named by the wall time of their first frame. */
class Slicer(private val dir: File, private val fgS: Int, private val bgS: Int, private val offS: Int) {
    @Volatile var foreground = true
    @Volatile var screenOff = false
    @Volatile private var running = false
    @Volatile var files = 0
    @Volatile var error: String? = null
    private lateinit var thread: Thread
    private val sr = 16000

    fun sliceS() = if (screenOff) offS else if (foreground) fgS else bgS

    fun start() {
        android.util.Log.i("live-record", "slicer start fg=$fgS bg=$bgS off=$offS")
        running = true
        thread = Thread { try { loop() } catch (e: Exception) { error = e.toString() } }
        thread.start()
    }

    fun stop() { android.util.Log.i("live-record", "slicer stop"); running = false; thread.join(5000) }

    private fun loop() {
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
        val pcm = ByteArray(sr * 2 / 50)  // 20 ms
        var samples = 0L                  // fed to the encoder
        var format: MediaFormat? = null
        var muxer: MediaMuxer? = null
        var track = 0
        var part: File? = null
        var fileStartUs = 0L
        var fileSlice = 0
        var lastState = sliceS()
        val info = MediaCodec.BufferInfo()

        fun openFile(ptsUs: Long) {
            val startMs = startWall + ptsUs / 1000
            part = File(dir, "$startMs.part")
            muxer = MediaMuxer(part!!.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG).also {
                track = it.addTrack(format!!)
                it.start()
            }
            fileStartUs = ptsUs
            fileSlice = sliceS()
        }

        fun closeFile() {
            muxer?.let { it.stop(); it.release() }
            muxer = null
            part?.let { it.renameTo(File(it.path.removeSuffix(".part") + ".ogg")); files++ }
            part = null
        }

        fun drain(eos: Boolean) {
            while (true) {
                val i = codec.dequeueOutputBuffer(info, if (eos) 10000 else 0)
                if (i == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) { format = codec.outputFormat; continue }
                if (i < 0) { if (eos && i == MediaCodec.INFO_TRY_AGAIN_LATER) continue else break }
                val buf = codec.getOutputBuffer(i)!!
                if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                    val state = sliceS()
                    // rotate at a frame boundary: the slice length is up, or the state changed to a shorter slice
                    if (muxer != null && (info.presentationTimeUs - fileStartUs >= fileSlice * 1_000_000L ||
                            (state != lastState && state < fileSlice && info.presentationTimeUs - fileStartUs >= state * 1_000_000L))) closeFile()
                    lastState = state
                    if (muxer == null) openFile(info.presentationTimeUs)
                    val rel = MediaCodec.BufferInfo().apply { set(info.offset, info.size, info.presentationTimeUs - fileStartUs, info.flags) }
                    muxer!!.writeSampleData(track, buf, rel)
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
}
