package com.suyiiyii.live_record

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import java.util.concurrent.BlockingQueue
import java.util.concurrent.CompletableFuture
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** The BLE recorder (XIAO / ProMicro nRF52840, Omi devkit firmware + our offline store): 16 kHz mono Opus in 10 ms
 *  frames, each notify `[seq_lo, seq_hi, sub_index] + payload`, a frame split over notifies when the MTU is small. */
object Ble {
    val SERVICE: UUID = UUID.fromString("19b10000-e8f2-537e-4f6c-d104768a1214")
    val AUDIO: UUID = UUID.fromString("19b10001-e8f2-537e-4f6c-d104768a1214")
    val CODEC: UUID = UUID.fromString("19b10002-e8f2-537e-4f6c-d104768a1214")
    val STORE: UUID = UUID.fromString("7c3e0000-1a2b-4c5d-8e9f-6c6976657265")
    val TIME: UUID = UUID.fromString("7c3e0001-1a2b-4c5d-8e9f-6c6976657265")
    val CTRL: UUID = UUID.fromString("7c3e0002-1a2b-4c5d-8e9f-6c6976657265")
    val DATA: UUID = UUID.fromString("7c3e0003-1a2b-4c5d-8e9f-6c6976657265")
    val BAS: UUID = UUID.fromString("0000180f-0000-1000-8000-00805f9b34fb")
    val BATTERY: UUID = UUID.fromString("00002a19-0000-1000-8000-00805f9b34fb")
    val DIS: UUID = UUID.fromString("0000180a-0000-1000-8000-00805f9b34fb")
    val MODEL: UUID = UUID.fromString("00002a24-0000-1000-8000-00805f9b34fb")
    val FW_REV: UUID = UUID.fromString("00002a26-0000-1000-8000-00805f9b34fb")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    const val OPUS_16K = 20       // the codec characteristic's value for 16 kHz mono Opus, 10 ms frames
    const val FRAME_US = 10_000L

    /** One notify and when it arrived; `data` null = the link went down. */
    class Packet(val at: Long, val data: ByteArray?)

    /** What the Ogg muxer needs for 16 kHz mono Opus: OpusHead, codec delay, seek pre-roll. The muxer writes csd-0
     *  into the file as is, so `preSkip` (48 kHz samples, RFC 7845 §5.1) is what the decoder drops at the start. */
    fun opusFormat(preSkip: Int = 0): MediaFormat = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 16000, 1).apply {
        fun le(n: Int, fill: ByteBuffer.() -> Unit) = ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN).apply { fill(); flip() }
        setByteBuffer("csd-0", le(19) { put("OpusHead".toByteArray()); put(1); put(1); putShort(preSkip.toShort()); putInt(16000); putShort(0); put(0) })
        setByteBuffer("csd-1", le(8) { putLong(preSkip * 1_000_000_000L / 48000) })
        setByteBuffer("csd-2", le(8) { putLong(80_000_000) })
    }

    /** A unit's full name (LR-P1-BDAE: board and address); the advertisement alone carries only LR-P1. */
    fun fullName(n: String?) = n != null && Regex("LR-[A-Z0-9]+-[0-9A-F]{4}").matches(n)

    /** The name pieces carry and the server sees: safe in a file name (the web gateway cleans it the same way). */
    fun tag(n: String) = n.replace(Regex("[^A-Za-z0-9._-]"), "_")
}

/** Wall time of the recorder's live frames. The board has no clock: a run is anchored at the phone's time of its first
 *  frame and counted on in 10 ms steps. A run ends (and the next frame starts a piece at its own arrival time) when
 *  frames arrive over 2 s later than counted (lost before they got a seq) or the count runs over 1 s ahead of arrival
 *  (the board's clock fast: audio cannot arrive before it was recorded, issue #25). `end`, where the last frame ends,
 *  is kept by the caller across restarts: a run started after a disconnect or a new recording never starts before it,
 *  so later audio is never pulled earlier (Omi app issue #4101). Plain Kotlin: the unit test drives it. */
class BleClock(var end: Long) {
    private var anchor = 0L
    private var frames = 0L

    /** A frame that arrived at `at`: its wall start (ms) and whether it begins a new run (a new piece). */
    fun next(at: Long): Pair<Long, Boolean> {
        var fresh = anchor == 0L
        if (!fresh) {
            val due = anchor + frames * Ble.FRAME_US / 1000
            if (at - due > 2000) { Log.w("live-record", "ble frames ${at - due} ms late: new run"); fresh = true }
            else if (due - at > 1000) { Log.w("live-record", "ble count ${due - at} ms ahead of arrival: new run"); fresh = true }
        }
        if (fresh) {
            // ahead: the count was wrong, arrival is the better time even if it overlaps the run before
            anchor = if (anchor != 0L && at < end) at else maxOf(at, end)
            frames = 0
        }
        val t = anchor + frames * Ble.FRAME_US / 1000
        frames++
        end = anchor + frames * Ble.FRAME_US / 1000
        return t to fresh
    }

    /** A seq gap or a disconnect: the next frame starts a run. */
    fun brk() { anchor = 0 }
}

/** Finds the recorder by scanning for its service (always: the board's address is random static, and a device made
 *  from a bare address after a Bluetooth restart is tried as public and times out, status 147, 10-03): the bound one
 *  (prefs bleName), or the first full name seen, which becomes bound. Each connection runs a session on its own
 *  thread, one GATT operation at a time: pair (the firmware lets a new phone pair only while it is on USB power or
 *  just reset), set the board's clock, take what it recorded offline (pieces into `chunks`, files deleted from its card
 *  once the phone has them), then subscribe to the live audio and put every notify on `out`; battery and card are read
 *  every minute. A dropped link is reconnected with autoConnect, which waits for the device to come back in range;
 *  each drop puts a null packet so the slicer ends the run. Permissions are checked before the service starts. */
@SuppressLint("MissingPermission")
class BleLink(private val ctx: Context, private val out: BlockingQueue<Ble.Packet>, private val prefs: SharedPreferences,
              private val chunks: File, private val undated: File,
              private val onName: (String) -> Unit, private val report: (String, Map<String, Any?>) -> Unit) {
    @Volatile var state = "正在找录音设备"
    @Volatile var hw: Map<String, Any?> = emptyMap()
    @Volatile private var on = true
    private val adapter = ctx.getSystemService(BluetoothManager::class.java).adapter
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var gatt: BluetoothGatt? = null
    private val raw = File(ctx.filesDir, "ble-raw").apply { mkdirs() }  // store files taken from the card, until they are pieces

    /** Turning Bluetooth off tears the link down without a callback (10-03: the link stayed "connected" with no data). */
    private val adapterState = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.getIntExtra(BluetoothAdapter.EXTRA_STATE, 0)) {
                BluetoothAdapter.STATE_TURNING_OFF -> {
                    Log.i("live-record", "ble adapter off")
                    main.removeCallbacksAndMessages(null)
                    gatt?.let { abort(it) }
                    gatt?.close(); gatt = null
                    out.put(Ble.Packet(System.currentTimeMillis(), null))
                    state = "蓝牙已关闭"
                }
                BluetoothAdapter.STATE_ON -> if (on && gatt == null) { Log.i("live-record", "ble adapter on"); main.removeCallbacksAndMessages(null); scan() }
            }
        }
    }

    fun start() {
        ctx.registerReceiver(adapterState, IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED))
        scan()
    }

    fun stop() {
        on = false
        ctx.unregisterReceiver(adapterState)
        main.removeCallbacksAndMessages(null)
        try { adapter.bluetoothLeScanner?.stopScan(scanCb) } catch (_: Exception) {}
        gatt?.let { abort(it); it.disconnect(); it.close() }
        gatt = null
    }

    private fun scan() {
        val scanner = adapter.bluetoothLeScanner ?: return retry("蓝牙未打开") { scan() }
        val bound = prefs.getString("bleName", null)
        state = if (bound != null) "正在找 $bound" else "正在找录音设备"
        // filtered by service: unfiltered scans are paused while the screen is off
        scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.SERVICE)).build()),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCb)
    }

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(type: Int, r: ScanResult) {
            // the full name (LR-P1-BDAE) comes in the scan response or from the cache; a result with only LR-P1 waits
            val name = listOfNotNull(r.scanRecord?.deviceName, r.device.name).firstOrNull(Ble::fullName) ?: return
            val bound = prefs.getString("bleName", null)
            if (bound != null && bound != name) return
            adapter.bluetoothLeScanner?.stopScan(this)
            Log.i("live-record", "ble found ${r.device.address} $name rssi=${r.rssi}")
            if (bound == null) prefs.edit().putString("bleName", name).apply()  // the first one seen is ours from now on
            prefs.edit().putString("bleAddress", r.device.address).apply()  // for a firmware update
            onName(Ble.tag(name))
            if (on) connect(r.device, false)
        }
        override fun onScanFailed(code: Int) = retry("扫描失败 $code") { scan() }
    }

    private fun retry(why: String, delayMs: Long = 5000, what: () -> Unit) {
        state = "$why，${delayMs / 1000} 秒后重试"
        if (on) main.postDelayed({ if (on) what() }, delayMs)
    }

    /** The first try connects directly (quick); after a drop autoConnect waits for the device without a timeout. */
    private fun connect(d: BluetoothDevice, auto: Boolean) {
        state = if (auto) "录音设备已断开，等待重连" else "正在连接录音设备"
        gatt = d.connectGatt(ctx, auto, cb, BluetoothDevice.TRANSPORT_LE) ?: return retry("连接失败") { connect(d, true) }
    }

    @Volatile private var pairingRefused = false

    //
    // One GATT operation at a time: the session thread starts one and waits for its callback
    //
    private class Gone : Exception("录音设备已断开")
    @Volatile private var pending: CompletableFuture<ByteArray>? = null
    private val ctrl = LinkedBlockingQueue<ByteArray>()
    private val data = LinkedBlockingQueue<ByteArray>()

    private fun abort(@Suppress("UNUSED_PARAMETER") g: BluetoothGatt) { pending?.completeExceptionally(Gone()) }

    private fun op(what: String, timeoutS: Long = 15, start: () -> Boolean): ByteArray {
        val f = CompletableFuture<ByteArray>()
        pending = f
        var tries = 0
        while (!start()) {  // the stack is busy with something else (a notify subscription by Android itself)
            if (++tries > 20 || gatt == null) throw Exception("$what 无法发起")
            Thread.sleep(50)
        }
        try { return f.get(timeoutS, TimeUnit.SECONDS) } catch (e: java.util.concurrent.ExecutionException) { throw e.cause!! }
    }

    private fun done(status: Int, value: ByteArray = ByteArray(0)) {
        val f = pending ?: return
        if (status == BluetoothGatt.GATT_SUCCESS) f.complete(value) else f.completeExceptionally(Exception("GATT $status"))
    }

    private fun chr(g: BluetoothGatt, s: UUID, c: UUID) = g.getService(s)?.getCharacteristic(c) ?: throw Exception("设备上没有 $c")

    private fun read(g: BluetoothGatt, s: UUID, c: UUID) = chr(g, s, c).let { ch -> op("read $c") { g.readCharacteristic(ch) } }

    @Suppress("DEPRECATION")
    private fun write(g: BluetoothGatt, s: UUID, c: UUID, v: ByteArray) {
        val ch = chr(g, s, c)
        op("write $c") {
            if (Build.VERSION.SDK_INT >= 33) g.writeCharacteristic(ch, v, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
            else { ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT; ch.value = v; g.writeCharacteristic(ch) }
        }
    }

    @Suppress("DEPRECATION")
    private fun subscribe(g: BluetoothGatt, s: UUID, c: UUID) {
        val ch = chr(g, s, c)
        g.setCharacteristicNotification(ch, true)
        val d = ch.getDescriptor(Ble.CCCD)
        op("subscribe $c") {
            if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) == BluetoothGatt.GATT_SUCCESS
            else { d.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE; g.writeDescriptor(d) }
        }
    }

    private fun le(b: ByteArray) = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)

    /** Bonded or not at all: the firmware asks for encryption on connect, so Android pairs by itself (Just Works); a
     *  phone it refuses stays BOND_NONE. */
    private fun bond(d: BluetoothDevice): Boolean {
        if (d.bondState == BluetoothDevice.BOND_BONDED) return true
        state = "正在配对"
        if (d.bondState == BluetoothDevice.BOND_NONE) d.createBond()
        val until = System.currentTimeMillis() + 60_000
        Thread.sleep(500)
        while (d.bondState == BluetoothDevice.BOND_BONDING && System.currentTimeMillis() < until) Thread.sleep(200)
        return d.bondState == BluetoothDevice.BOND_BONDED
    }

    private fun session(g: BluetoothGatt) {
        val name = prefs.getString("bleName", null) ?: g.device.name ?: g.device.address
        try {
            state = "已连接，准备中"
            op("mtu") { g.requestMtu(512) }  // one frame per notify
            g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)  // a fixed short interval; the result is logged below
            if (!bond(g.device)) {
                pairingRefused = true
                state = "配对被录音设备拒绝：把它接上 USB 电源（或重启它后 5 分钟内）再连"
                g.disconnect()
                return
            }
            op("services") { g.discoverServices() }
            val codec = read(g, Ble.SERVICE, Ble.CODEC).firstOrNull()?.toInt()?.and(0xff)
            if (codec != Ble.OPUS_16K) { state = "设备编码 $codec 不支持（只支持 16 kHz Opus）"; return }
            val now = System.currentTimeMillis()
            write(g, Ble.STORE, Ble.TIME, le(ByteArray(8)).putLong(now).array())  // the clock its offline audio is dated by
            val st = status(g, name)
            if (st.store == 0 || st.store == -28) offline(g, name, Triple(st.boot, st.frame, now))
            subscribe(g, Ble.SERVICE, Ble.AUDIO)
            state = "已连接"
            while (gatt === g && on) {  // battery and card, every minute while the audio runs
                Thread.sleep(60_000)
                if (gatt === g && on) status(g, name)
            }
        } catch (e: Gone) {
        } catch (e: Exception) {
            Log.w("live-record", "ble session: $e")
            if (gatt === g) { state = "录音设备出错：${e.message}"; g.disconnect() }
        }
    }

    private class Status(val boot: Int, val frame: Long, val store: Int)

    /** The board's state, kept for the app's page and sent to the server under the board's name. */
    private fun status(g: BluetoothGatt, name: String): Status {
        val t = le(read(g, Ble.STORE, Ble.TIME))
        val s = Status(t.getShort(0).toInt() and 0xffff, t.getInt(2).toLong() and 0xffffffffL, t.getShort(6).toInt())
        val h = mutableMapOf<String, Any?>("name" to name, "store" to s.store, "free_kb" to (t.getInt(8).toLong() and 0xffffffffL),
            "total_kb" to (t.getInt(12).toLong() and 0xffffffffL), "battery" to read(g, Ble.BAS, Ble.BATTERY).firstOrNull()?.toInt())
        for ((k, c) in listOf("model" to Ble.MODEL, "fw" to Ble.FW_REV))
            h[k] = hw[k] ?: try { String(read(g, Ble.DIS, c)) } catch (e: Exception) { null }
        hw = h
        prefs.edit().putString("bleModel", h["model"] as String?).putString("bleFw", h["fw"] as String?).apply()  // for an update later
        report(Ble.tag(name), h - "name")
        return s
    }

    /** Everything on the card goes to the phone before the live audio starts: while nobody listens the board keeps
     *  recording to the card, so its files are older than any live piece and reach the server first, in order. A file
     *  is deleted from the card once it is on the phone; files are turned into pieces when the card is empty. */
    private fun offline(g: BluetoothGatt, name: String, live: Triple<Int, Long, Long>) {
        ctrl.clear(); data.clear()
        subscribe(g, Ble.STORE, Ble.CTRL)
        subscribe(g, Ble.STORE, Ble.DATA)
        var taken = 0; var bytes = 0L
        for (pass in 1..5) {  // the card fills again (slowly) while we take it
            write(g, Ble.STORE, Ble.CTRL, byteArrayOf(1))  // LIST
            val files = mutableListOf<Pair<Long, Long>>()
            while (true) {
                val r = ctrl.poll(10, TimeUnit.SECONDS) ?: throw Exception("卡上文件列表超时")
                if (r[0] == 0x81.toByte()) files += (le(r).getInt(1).toLong() and 0xffffffffL) to (le(r).getInt(5).toLong() and 0xffffffffL)
                if (r[0] == 0x80.toByte()) break
            }
            if (files.isEmpty()) break
            val total = files.sumOf { it.second }
            for ((f, size) in files) {
                state = "同步离线录音 ${taken + 1} 个文件 · ${(bytes + total) / 1024} KB 中 ${bytes / 1024} KB"
                val b = take(g, f, size) { state = "同步离线录音 · ${(bytes + it) / 1024} / ${(total + bytes) / 1024} KB" }
                File(raw, "%08X.BIN".format(f)).writeBytes(b)
                write(g, Ble.STORE, Ble.CTRL, le(ByteArray(5)).put(4).putInt(f.toInt()).array())  // DELETE
                while ((ctrl.poll(10, TimeUnit.SECONDS) ?: throw Exception("删除卡上文件超时"))[0] != 0x84.toByte()) {}
                taken++; bytes += size
            }
        }
        if (taken > 0) Log.i("live-record", "ble offline: $taken files, $bytes B")
        pieces(name, live)
    }

    /** One file from the card; a hole (a lost notify) is asked again from where it is. */
    private fun take(g: BluetoothGatt, f: Long, size: Long, progress: (Long) -> Unit): ByteArray {
        val b = ByteArrayOutputStream()
        var stalls = 0
        while (b.size() < size) {
            data.clear()
            write(g, Ble.STORE, Ble.CTRL, le(ByteArray(9)).put(2).putInt(f.toInt()).putInt(b.size()).array())  // READ from here
            val before = b.size()
            while (true) {
                val d = data.poll(5, TimeUnit.SECONDS)
                if (d == null) break  // stalled
                val off = le(d).getInt(0).toLong() and 0xffffffffL
                if (off == b.size().toLong()) { b.write(d, 4, d.size - 4); if (b.size() % 32768 < d.size) progress(b.size().toLong()) }
                else if (off > b.size()) break  // a hole: ask again from b.size()
                if (b.size() >= size) break
            }
            ctrl.removeIf { it[0] == 0x82.toByte() }  // the end marks of this or an interrupted READ
            if (b.size() == before && ++stalls > 3) throw Exception("读取卡上文件 %08X 失败".format(f))
        }
        return b.toByteArray()
    }

    /** The files taken so far as pieces: dated runs into the upload queue under the board's name, runs of a boot that
     *  was never synced (it lost power before any phone saw it) kept in `undated`, as Ogg, for a person to place.
     *  ponytail: all files in memory at once, fine for hours of Opus (~14 MB/h); stream per boot if the card is weeks. */
    private fun pieces(name: String, live: Triple<Int, Long, Long>) {
        val files = raw.listFiles { f -> f.name.endsWith(".BIN") }?.associate { it.name.removeSuffix(".BIN").toLong(16) to it.readBytes() } ?: return
        if (files.isEmpty()) return
        state = "整理离线录音"
        val tag = Ble.tag(name)
        var n = 0; var lost = 0
        for (run in Store.runs(files, live)) {
            if (run.startMs != null) n += Store.writePieces(run, chunks, tag).size
            else {
                undated.mkdirs()
                File(undated, "$tag-boot${run.boot}-${undated.list()?.size ?: 0}.ogg").writeBytes(Store.oggOpus(run.frames))
                lost++
            }
        }
        raw.listFiles()?.forEach { it.delete() }
        Log.i("live-record", "ble offline: $n pieces, $lost undated runs")
    }

    private val cb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i("live-record", "ble state=$newState status=$status")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Thread { session(g) }.start()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                abort(g)
                g.close()
                if (gatt !== g) return  // an old link, already given up (Bluetooth turned off)
                gatt = null
                out.put(Ble.Packet(System.currentTimeMillis(), null))
                // a refused pairing waits longer: the person has to plug the recorder in first
                val wait = if (pairingRefused) 30_000L else 0L
                pairingRefused = false
                if (on) main.postDelayed({ if (on) connect(g.device, true) }, wait)
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i("live-record", "ble mtu=$mtu status=$status")
            done(BluetoothGatt.GATT_SUCCESS)
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) = done(status)

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) = done(status, value)

        @Deprecated("before Android 13")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) =
            onCharacteristicRead(g, c, c.value ?: ByteArray(0), status)

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) = done(status)

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) = done(status)

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            when (c.uuid) {
                Ble.AUDIO -> out.put(Ble.Packet(System.currentTimeMillis(), value))
                Ble.CTRL -> ctrl.put(value)
                Ble.DATA -> data.put(value)
            }
        }

        @Deprecated("before Android 13")
        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) =
            onCharacteristicChanged(g, c, c.value.clone())

        /** Hidden in the SDK but called on every connection parameter update (the Nordic BLE library relies on it). */
        @Suppress("unused")
        fun onConnectionUpdated(g: BluetoothGatt, interval: Int, latency: Int, timeout: Int, status: Int) {
            Log.i("live-record", "ble connection interval=${interval * 1.25} ms latency=$latency timeout=${timeout * 10} ms status=$status")
        }
    }
}
