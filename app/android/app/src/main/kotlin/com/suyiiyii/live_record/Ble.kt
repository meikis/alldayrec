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

    /** A unit's full name without its scan response: the board name in the advertisement (LR-P1) and the last two
     *  bytes of its address, as the firmware makes it (transport.c lr_set_name). A phone gets few scan responses from
     *  a recorder busy with another phone (slow non-connectable advertising, 10-04), so the list must not need them. */
    fun fullName(names: List<String?>, address: String): String? = names.firstOrNull(::fullName)
        ?: names.firstOrNull { it != null && Regex("LR-[A-Z0-9]+").matches(it) }?.let { "$it-" + address.takeLast(5).replace(":", "") }

    fun fullName(r: ScanResult) = fullName(listOf(r.scanRecord?.deviceName, r.device.name), r.device.address)

    /** The name pieces carry and the server sees: safe in a file name (the web gateway cleans it the same way). */
    fun tag(n: String) = n.replace(Regex("[^A-Za-z0-9._-]"), "_")

    /** The recorder's state in its scan response (firmware transport.c lr_status, company 0xFFFF); -1: an older
     *  firmware without it. */
    const val BOUND = 1
    const val CONNECTED = 2
    const val PAUSED = 4
    fun state(r: ScanResult) = r.scanRecord?.getManufacturerSpecificData(0xFFFF)?.firstOrNull()?.toInt()?.and(0xff) ?: -1

    /** The phone's own bond goes too (hidden API, as BLE libraries call it): a bond the recorder has forgotten would
     *  only fail to encrypt when this phone binds it again. */
    fun removeBond(d: BluetoothDevice): Boolean = try {
        d.javaClass.getMethod("removeBond").invoke(d) as Boolean
    } catch (e: Exception) { Log.w("live-record", "ble removeBond: $e"); false }
}

/** The add-recorder page's list: the recorders around and the state each advertises. */
@SuppressLint("MissingPermission")
class Nearby(ctx: Context) {
    private class Seen(val address: String, val rssi: Int, val state: Int, val busy: Boolean, val at: Long)
    private val seen = java.util.concurrent.ConcurrentHashMap<String, Seen>()
    private val scanner = ctx.getSystemService(BluetoothManager::class.java).adapter.bluetoothLeScanner
    private val cb = object : ScanCallback() {
        override fun onScanResult(type: Int, r: ScanResult) {
            val name = Ble.fullName(r) ?: return
            // the state byte is in the scan response: a result without one keeps the last known; a non-connectable
            // advertisement is a recorder connected to another phone, scan response or not
            val st = Ble.state(r).takeIf { it >= 0 } ?: seen[name]?.state ?: -1
            seen[name] = Seen(r.device.address, r.rssi, st, !r.isConnectable, System.currentTimeMillis())
        }
    }

    fun start() = scanner?.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.SERVICE)).build()),
        ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), cb)

    fun stop() = scanner?.stopScan(cb)

    /** Seen in the last 5 s, nearest first. */
    fun list() = seen.entries.filter { System.currentTimeMillis() - it.value.at < 5000 }.sortedByDescending { it.value.rssi }
        .map { (n, s) -> mapOf("name" to n, "address" to s.address, "rssi" to s.rssi, "state" to s.state, "busy" to s.busy) }
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
 *  from a bare address after a Bluetooth restart is tried as public and times out, status 147, 10-03): the one the
 *  person chose on the add-recorder page (prefs bleName). Each connection runs a session on its own thread, one GATT
 *  operation at a time: pair, only right after that choice (prefs bindPending; with bindForce the recorder then
 *  forgets its other phones), set the board's clock, take what it recorded offline (pieces into `chunks`, files deleted from its card
 *  once the phone has them), then subscribe to the live audio and put every notify on `out`; battery and card are read
 *  every minute. A dropped link is reconnected with autoConnect, which waits for the device to come back in range;
 *  each drop puts a null packet so the slicer ends the run. Permissions are checked before the service starts.
 *  Binding is the app's soft limit (issue #56): a recorder that forgot this phone (another phone took it) is not
 *  paired again behind the person's back; the link stops and says so (prefs bleKicked) until they add it again. */
@SuppressLint("MissingPermission")
class BleLink(private val ctx: Context, private val out: BlockingQueue<Ble.Packet>, private val prefs: SharedPreferences,
              private val chunks: File, private val undated: File,
              private val onName: (String) -> Unit, private val report: (String, Map<String, Any?>) -> Unit) {
    companion object {
        const val KICKED = "已被其他手机绑定：在「录音设备」页重新添加"
        // what Android reports when the link cannot be encrypted with the keys it has
        val AUTH_FAILED = setOf("GATT 5", "GATT 15", "GATT 137")
    }

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
        val bound = prefs.getString("bleName", null) ?: run { state = "未绑定录音设备"; return }
        if (prefs.getBoolean("bleKicked", false)) { state = KICKED; return }
        state = "正在找 $bound"
        // filtered by service: unfiltered scans are paused while the screen is off
        scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.SERVICE)).build()),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCb)
    }

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(type: Int, r: ScanResult) {
            val name = Ble.fullName(r) ?: return
            if (name != prefs.getString("bleName", null)) return
            val st = Ble.state(r)
            if (!r.isConnectable) { state = "$name 正连着其他手机"; return }
            adapter.bluetoothLeScanner?.stopScan(this)
            Log.i("live-record", "ble found ${r.device.address} $name rssi=${r.rssi} state=$st")
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
        state = if (auto) "录音设备不在附近，它在离线录音" + (lastSeen?.let { "（最后连接 $it）" } ?: "") + "，回来后自动补传"
                else "正在连接录音设备"
        gatt = d.connectGatt(ctx, auto, cb, BluetoothDevice.TRANSPORT_LE) ?: return retry("连接失败") { connect(d, true) }
    }

    @Volatile private var pairingFailed = false
    @Volatile private var lastSeen: String? = null  // HH:mm the link last went down
    @Volatile var pausedUntil = 0L                   // wall ms the recorder's pause ends (0: recording), as last known

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

    /** Bonded or not at all: the firmware asks for encryption on connect, so Android pairs by itself (Just Works). */
    private fun bond(d: BluetoothDevice): Boolean {
        if (d.bondState == BluetoothDevice.BOND_BONDED) return true
        state = "正在配对"
        if (d.bondState == BluetoothDevice.BOND_NONE) d.createBond()
        val until = System.currentTimeMillis() + 60_000
        Thread.sleep(500)
        while (d.bondState == BluetoothDevice.BOND_BONDING && System.currentTimeMillis() < until) Thread.sleep(200)
        return d.bondState == BluetoothDevice.BOND_BONDED
    }

    /** Runs one step of a session, shown on the app's status bar and logged with how long it took. */
    private fun <T> step(what: String, block: () -> T): T {
        state = what
        val t0 = android.os.SystemClock.elapsedRealtime()
        return block().also { Log.i("live-record", "ble $what: ${android.os.SystemClock.elapsedRealtime() - t0} ms") }
    }

    private fun session(g: BluetoothGatt) {
        val name = prefs.getString("bleName", null) ?: g.device.name ?: g.device.address
        try {
            step("已连接，协商包长") { op("mtu") { g.requestMtu(512) } }  // one frame per notify
            g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)  // a fixed short interval; the result is logged below
            val adding = prefs.getBoolean("bindPending", false)
            if (g.device.bondState != BluetoothDevice.BOND_BONDED) {
                if (!adding) return kicked(g, "this phone's bond is gone")
                if (!step("正在配对") { bond(g.device) }) {
                    Log.w("live-record", "ble pairing with $name failed")
                    pairingFailed = true
                    state = "配对没有完成，30 秒后重试"
                    g.disconnect()
                    return
                }
            }
            step("读取设备服务") { op("services") { g.discoverServices() } }
            // the first encrypted read: a recorder that no longer knows this phone fails it
            val codec = try { read(g, Ble.SERVICE, Ble.CODEC) } catch (e: Exception) {
                if (!adding && e.message in AUTH_FAILED) return kicked(g, "${e.message}") else throw e
            }.firstOrNull()?.toInt()?.and(0xff)
            if (codec != Ble.OPUS_16K) { state = "设备编码 $codec 不支持（只支持 16 kHz Opus）"; Log.w("live-record", "ble codec $codec"); return }
            val now = System.currentTimeMillis()
            step("给设备对时") { write(g, Ble.STORE, Ble.TIME, le(ByteArray(8)).putLong(now).array()) }  // the clock its offline audio is dated by
            val st = step("读取设备状态") { status(g, name) }
            Log.i("live-record", "ble status: boot ${st.boot} frame ${st.frame} " + hw.entries.joinToString(" ") { "${it.key}=${it.value}" })
            subscribe(g, Ble.STORE, Ble.CTRL)  // replies to LIST/READ/DELETE and the app's USB disk switch
            if (adding) {
                if (prefs.getBoolean("bindForce", false)) step("让录音设备忘掉其他手机") {
                    ctrl.clear()
                    write(g, Ble.STORE, Ble.CTRL, byteArrayOf(5))  // FORGET: every phone but this one
                    Log.i("live-record", "ble forgot other phones: ${ctrl.poll(10, TimeUnit.SECONDS)?.let { le(it).getInt(1) }}")
                }
                prefs.edit().remove("bindPending").remove("bindForce").apply()
                Log.i("live-record", "ble bound $name")
            }
            if (st.store == 0 || st.store == -28) offline(g, name, Triple(st.boot, st.frame, now))
            else Log.i("live-record", "ble offline sync skipped: card state ${st.store}")
            subscribe(g, Ble.SERVICE, Ble.AUDIO)
            Log.i("live-record", "ble live audio on")
            state = "已连接 · 实时上传"
            while (gatt === g && on) {  // battery and card every minute while the audio runs; the app's requests between
                val job = jobs.poll(60, TimeUnit.SECONDS)
                if (gatt !== g || !on) break
                job?.invoke(g)
                status(g, name)
                Log.i("live-record", "ble status: battery ${hw["battery"]}% card ${hw["store"]} free ${hw["free_kb"]} KB")
            }
        } catch (e: Gone) {
        } catch (e: Exception) {
            Log.w("live-record", "ble session: $e")
            if (gatt === g) { state = "录音设备出错：${e.message}"; g.disconnect() }
        }
    }

    private class Status(val boot: Int, val frame: Long, val store: Int)

    /** The recorder no longer knows this phone: no pairing behind the person's back, no reconnecting. */
    private fun kicked(g: BluetoothGatt, why: String) {
        Log.w("live-record", "ble kicked: $why")
        prefs.edit().putBoolean("bleKicked", true).apply()
        state = KICKED
        g.disconnect()
    }

    /** Pause the recording for `s` seconds (0: go on now); the recorder keeps the time itself. */
    fun pause(s: Int) {
        if (gatt == null) throw Exception("录音设备没连上")
        jobs.put { g ->
            write(g, Ble.STORE, Ble.CTRL, le(ByteArray(5)).put(0x20).putInt(s).array())  // PAUSE
            pausedUntil = if (s > 0) System.currentTimeMillis() + s * 1000L else 0L
            Log.i("live-record", "ble pause $s s")
        }
    }

    /** Unbinding: the recorder forgets every phone, this one too (FORGET all), and the link goes. False: not connected. */
    fun forgetAll(): Boolean {
        if (gatt == null) return false
        val done = CompletableFuture<Boolean>()
        jobs.put { g -> done.complete(try { write(g, Ble.STORE, Ble.CTRL, byteArrayOf(5, 1)); true } catch (e: Exception) { false }) }
        return try { done.get(20, TimeUnit.SECONDS) } catch (e: Exception) { false }
    }

    /** What the app asks of the connected board, run on the session thread between status reads. */
    private val jobs = LinkedBlockingQueue<(BluetoothGatt) -> Unit>()
    @Volatile var usbNote: String? = null
    @Volatile var syncNote: String? = null  // the last offline sync, for the app's recorder page
    @Volatile var syncDoneKb = 0L           // the sync under way: taken of total (0 of 0: none)
    @Volatile var syncTotalKb = 0L

    /** The board's SD card as a USB disk on the computer it is plugged into (CTRL 0x06), or back. */
    fun usbDisk(on: Boolean) {
        if (gatt == null) { usbNote = "录音设备没连上"; return }
        usbNote = if (on) "正在切换到 U 盘模式" else "正在切回录音"
        jobs.put { g ->
            ctrl.clear()
            write(g, Ble.STORE, Ble.CTRL, byteArrayOf(6, if (on) 1 else 0))
            val r = ctrl.poll(15, TimeUnit.SECONDS)
            val err = r?.takeIf { it[0] == 0x86.toByte() }?.get(5)?.toInt()
            Log.i("live-record", "ble usb disk $on: $err")
            usbNote = when {
                err == null -> "录音设备没有回应"
                err == -107 -> "录音设备没接在电脑的 USB 上"
                err != 0 -> "切换失败 $err"
                on -> "U 盘模式：电脑上出现 OMI 盘（约 20 秒）；用完先在电脑上推出，再切回"
                else -> null
            }
        }
    }

    /** The board's state, kept for the app's page and sent to the server under the board's name. */
    private fun status(g: BluetoothGatt, name: String): Status {
        val t = le(read(g, Ble.STORE, Ble.TIME))
        val s = Status(t.getShort(0).toInt() and 0xffff, t.getInt(2).toLong() and 0xffffffffL, t.getShort(6).toInt())
        val h = mutableMapOf<String, Any?>("name" to name, "store" to s.store, "free_kb" to (t.getInt(8).toLong() and 0xffffffffL),
            "total_kb" to (t.getInt(12).toLong() and 0xffffffffL), "battery" to read(g, Ble.BAS, Ble.BATTERY).firstOrNull()?.toInt())
        if (t.limit() >= 47) pausedUntil = t.getInt(43).toLong().let { if (it > 0) System.currentTimeMillis() + it * 1000 else 0L }
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
        subscribe(g, Ble.STORE, Ble.DATA)
        var taken = 0; var bytes = 0L
        val t0 = android.os.SystemClock.elapsedRealtime()
        for (pass in 1..5) {  // the card fills again (slowly) while we take it
            val files = step("读取卡上文件列表") {
                write(g, Ble.STORE, Ble.CTRL, byteArrayOf(1))  // LIST
                val files = mutableListOf<Pair<Long, Long>>()
                while (true) {
                    val r = ctrl.poll(10, TimeUnit.SECONDS) ?: throw Exception("卡上文件列表超时")
                    if (r[0] == 0x81.toByte()) files += (le(r).getInt(1).toLong() and 0xffffffffL) to (le(r).getInt(5).toLong() and 0xffffffffL)
                    if (r[0] == 0x80.toByte()) { if (r.size > 1 && r[1] != 0.toByte()) Log.w("live-record", "ble LIST error ${r[1]}"); break }
                }
                files
            }
            Log.i("live-record", "ble LIST pass $pass: ${files.size} files, ${files.sumOf { it.second }} B")
            // a .part of a file the card no longer lists (taken by another phone) has nothing to go on with
            raw.listFiles { x -> x.name.endsWith(".part") }?.filter { p -> files.none { "%08X.part".format(it.first) == p.name } }?.forEach { it.delete() }
            if (files.isEmpty()) break
            val total = files.sumOf { it.second }
            // what an earlier, cut-short sync already has counts as done from the start, not once its file comes up
            var resumed = files.sumOf { (f, _) -> File(raw, "%08X.part".format(f)).length() }
            syncTotalKb = (total + bytes) / 1024; syncDoneKb = (bytes + resumed) / 1024
            // a file cut short last time first, then the rest oldest first (the card lists in directory order, where a
            // new segment can sit in a freed slot ahead of older ones)
            for ((f, size) in files.sortedWith(compareBy({ !File(raw, "%08X.part".format(it.first)).exists() }, { it.first }))) {
                state = "同步离线录音"
                val had = File(raw, "%08X.part".format(f)).length()
                resumed -= had
                val tf = android.os.SystemClock.elapsedRealtime()
                val b = take(g, f, size) { syncDoneKb = (bytes + resumed + it) / 1024 }
                val ms = android.os.SystemClock.elapsedRealtime() - tf
                Log.i("live-record", "ble took %08X: %d B (%d already here) in %.1f s, %d KB/s".format(f, size, had, ms / 1000.0, (size - had) / maxOf(ms, 1)))
                File(raw, "%08X.BIN".format(f)).writeBytes(b)
                File(raw, "%08X.part".format(f)).delete()
                write(g, Ble.STORE, Ble.CTRL, le(ByteArray(5)).put(4).putInt(f.toInt()).array())  // DELETE
                var r: ByteArray
                do { r = ctrl.poll(10, TimeUnit.SECONDS) ?: throw Exception("删除卡上文件超时") } while (r[0] != 0x84.toByte())
                if (r.size > 5 && r[5] != 0.toByte()) Log.w("live-record", "ble DELETE %08X: %d".format(f, r[5].toInt()))
                taken++; bytes += size
            }
        }
        Log.i("live-record", "ble offline: $taken files, $bytes B in ${(android.os.SystemClock.elapsedRealtime() - t0) / 1000} s")
        syncDoneKb = 0; syncTotalKb = 0
        pieces(name, live)
    }

    /** One file from the card; a hole (a lost notify) is asked again from where it is. */
    /** One file from the card, written as it comes to `raw`/<file>.part: a sync cut short (app killed, out of range)
     *  goes on from the bytes already here, not from the start of a file that may be 10 MB. */
    private fun take(g: BluetoothGatt, f: Long, size: Long, progress: (Long) -> Unit): ByteArray {
        val part = File(raw, "%08X.part".format(f))
        val b = ByteArrayOutputStream()
        if (part.exists()) b.write(part.readBytes().let { it.copyOf(minOf(it.size.toLong(), size).toInt()) })
        if (b.size() > 0) Log.i("live-record", "ble take %08X: going on from %d of %d B".format(f, b.size(), size))
        progress(b.size().toLong())
        val out = java.io.FileOutputStream(part).apply { write(b.toByteArray()) }
        var stalls = 0
        while (b.size() < size) {
            data.clear()
            write(g, Ble.STORE, Ble.CTRL, le(ByteArray(9)).put(2).putInt(f.toInt()).putInt(b.size()).array())  // READ from here
            val before = b.size()
            while (true) {
                val d = data.poll(5, TimeUnit.SECONDS)
                if (d == null) break  // stalled
                val off = le(d).getInt(0).toLong() and 0xffffffffL
                if (off == b.size().toLong()) {
                    b.write(d, 4, d.size - 4); out.write(d, 4, d.size - 4)
                    if (b.size() % 32768 < d.size) progress(b.size().toLong())
                }
                else if (off > b.size()) break  // a hole: ask again from b.size()
                if (b.size() >= size) break
            }
            ctrl.removeIf { it[0] == 0x82.toByte() }  // the end marks of this or an interrupted READ
            if (b.size() == before && ++stalls > 3) { out.close(); throw Exception("读取卡上文件 %08X 失败".format(f)) }
        }
        out.close()
        return b.toByteArray()
    }

    /** The files taken so far as pieces: dated runs into the upload queue under the board's name, runs of a boot that
     *  was never synced (it lost power before any phone saw it) kept in `undated`, as Ogg, for a person to place.
     *  ponytail: all files in memory at once, fine for hours of Opus (~14 MB/h); stream per boot if the card is weeks. */
    private fun pieces(name: String, live: Triple<Int, Long, Long>) {
        val files = raw.listFiles { f -> f.name.endsWith(".BIN") }?.associate { it.name.removeSuffix(".BIN").toLong(16) to it.readBytes() } ?: return
        if (files.isEmpty()) return
        state = "整理离线录音"
        Log.i("live-record", "ble pieces from ${files.size} files")
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
        syncNote = "${java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date())} 取回离线录音 " +
            "${files.values.sumOf { it.size.toLong() } / 1024} KB，生成 $n 块待上传" + (if (lost > 0) "，$lost 段没有时间（存在手机 undated 目录）" else "")
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
                lastSeen = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date())
                out.put(Ble.Packet(System.currentTimeMillis(), null))
                // a pairing that did not finish (a dialog left alone) waits a little; unbound or kicked: no reconnecting
                val wait = if (pairingFailed) 30_000L else 0L
                pairingFailed = false
                if (on && prefs.getString("bleName", null) != null && !prefs.getBoolean("bleKicked", false))
                    main.postDelayed({ if (on) connect(g.device, true) }, wait)
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
