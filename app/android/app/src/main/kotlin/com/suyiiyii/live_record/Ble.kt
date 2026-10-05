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

/** The BLE recorder (XIAO / ProMicro nRF52840, our firmware): one record stream (firmware src/lr_store.h) it writes to
 *  its card and sends to a phone that follows it, the same bytes at the same file and offset. */
object Ble {
    val STORE: UUID = UUID.fromString("7c3e0000-1a2b-4c5d-8e9f-6c6976657265")
    val TIME: UUID = UUID.fromString("7c3e0001-1a2b-4c5d-8e9f-6c6976657265")
    val CTRL: UUID = UUID.fromString("7c3e0002-1a2b-4c5d-8e9f-6c6976657265")
    val DATA: UUID = UUID.fromString("7c3e0003-1a2b-4c5d-8e9f-6c6976657265")
    val LIVE: UUID = UUID.fromString("7c3e0004-1a2b-4c5d-8e9f-6c6976657265")
    val BAS: UUID = UUID.fromString("0000180f-0000-1000-8000-00805f9b34fb")
    val BATTERY: UUID = UUID.fromString("00002a19-0000-1000-8000-00805f9b34fb")
    val DIS: UUID = UUID.fromString("0000180a-0000-1000-8000-00805f9b34fb")
    val MODEL: UUID = UUID.fromString("00002a24-0000-1000-8000-00805f9b34fb")
    val FW_REV: UUID = UUID.fromString("00002a26-0000-1000-8000-00805f9b34fb")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    const val TIME_EVERY_MS = 10 * 60_000L  // the phone's clock into the stream again: the board's drifts

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

    fun start() = scanner?.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.STORE)).build()),
        ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), cb)

    fun stop() = scanner?.stopScan(cb)

    /** Seen in the last 5 s, nearest first. */
    fun list() = seen.entries.filter { System.currentTimeMillis() - it.value.at < 5000 }.sortedByDescending { it.value.rssi }
        .map { (n, s) -> mapOf("name" to n, "address" to s.address, "rssi" to s.rssi, "state" to s.state, "busy" to s.busy) }
}

/** Finds the recorder by scanning for its service (always: the board's address is random static, and a device made
 *  from a bare address after a Bluetooth restart is tried as public and times out, status 147, 10-03): the one the
 *  person chose on the add-recorder page (prefs bleName). Each connection runs a session on its own thread, one GATT
 *  operation at a time: pair, only right after that choice (prefs bindPending; with bindForce the recorder then
 *  forgets its other phones), follow the record stream into a mirror of the board's files (`recorder`/<name>, see
 *  Mirror), READ what the mirror lacks (what was recorded while away, a lost notify), put the phone's clock into the
 *  stream on connect and every 10 minutes, turn the mirror into dated Ogg pieces in `chunks` (the upload queue) and
 *  delete from the card what is mirrored and uploaded. Battery and card are read every minute. A dropped link is
 *  reconnected with autoConnect, which waits for the device to come back in range. Permissions are checked before the
 *  service starts.
 *  Binding is the app's soft limit (issue #56): a recorder that forgot this phone (another phone took it) is not
 *  paired again behind the person's back; the link stops and says so (prefs bleKicked) until they add it again. */
@SuppressLint("MissingPermission")
class BleLink(private val ctx: Context, private val prefs: SharedPreferences, private val chunks: File, private val undated: File,
              private val sliceS: () -> Int, private val onPiece: (Int) -> Unit,
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

    /** Turning Bluetooth off tears the link down without a callback (10-03: the link stayed "connected" with no data). */
    private val adapterState = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.getIntExtra(BluetoothAdapter.EXTRA_STATE, 0)) {
                BluetoothAdapter.STATE_TURNING_OFF -> {
                    Log.i("live-record", "ble adapter off")
                    main.removeCallbacksAndMessages(null)
                    gatt?.let { abort(it) }
                    gatt?.close(); gatt = null
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
        scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.STORE)).build()),
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
    private val live = LinkedBlockingQueue<ByteArray>()

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
        return block().also { val ms = android.os.SystemClock.elapsedRealtime() - t0; Log.i("live-record", "ble $what: $ms ms"); DeviceLog.step(what, ms) }
    }

    private fun session(g: BluetoothGatt) {
        val name = prefs.getString("bleName", null) ?: g.device.name ?: g.device.address
        try {
            step("已连接，协商包长") { op("mtu") { g.requestMtu(512) } }  // a record per notify
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
            try { read(g, Ble.STORE, Ble.TIME) } catch (e: Exception) {
                if (!adding && e.message in AUTH_FAILED) return kicked(g, "${e.message}") else throw e
            }
            live.clear(); ctrl.clear(); data.clear()
            subscribe(g, Ble.STORE, Ble.CTRL)  // replies to LIST/READ/DELETE and the app's USB disk switch
            subscribe(g, Ble.STORE, Ble.DATA)
            subscribe(g, Ble.STORE, Ble.LIVE)  // first: nothing the stream adds after LIST is missed
            var synced = step("给设备对时") { sync(g) }
            val st = step("读取设备状态") { status(g, name) }
            Log.i("live-record", "ble status: boot ${st.boot} frame ${st.frame} " + hw.entries.joinToString(" ") { "${it.key}=${it.value}" })
            if (adding) {
                if (prefs.getBoolean("bindForce", false)) step("让录音设备忘掉其他手机") {
                    val r = reply(g, byteArrayOf(5), 0x85)  // FORGET: every phone but this one
                    Log.i("live-record", "ble forgot other phones: ${r?.let { le(it).getInt(1) }}")
                }
                prefs.edit().remove("bindPending").remove("bindForce").apply()
                Log.i("live-record", "ble bound $name")
            }
            val m = Mirror(File(ctx.filesDir, "recorder/${Ble.tag(name)}"))
            mirrorName = Ble.tag(name); boot = st.boot
            catchUp(g, m)
            DeviceLog.read(mirrorName, { write(g, Ble.STORE, Ble.CTRL, it) }, ctrl, data)
            DeviceLog.connected(mirrorName)
            state = "已连接 · 实时上传"
            var lastStatus = android.os.SystemClock.elapsedRealtime()
            var open = -1L  // the file LIVE goes on in
            while (gatt === g && on) {
                val p = live.poll(1, TimeUnit.SECONDS)
                if (gatt !== g || !on) break
                if (p != null) {
                    val b = le(p)
                    val f = b.getInt(0).toLong() and 0xffffffffL
                    if (f != open) { val moved = open >= 0; open = f; if (moved) catchUp(g, m) }  // a new file: LIST says how the last ended
                    m.put(f, b.getInt(4).toLong() and 0xffffffffL, p.copyOfRange(8, p.size))?.let { hole ->
                        Log.i("live-record", "ble LIVE hole in %08X at $hole: READ".format(f))
                        catchUp(g, m)
                    }
                    if (live.isNotEmpty()) continue  // a burst: take it all before the rest
                }
                pieces(m)
                val now = android.os.SystemClock.elapsedRealtime()
                if (System.currentTimeMillis() - synced > Ble.TIME_EVERY_MS) synced = sync(g)
                if (now - lastStatus > 60_000) {
                    jobs.poll()?.invoke(g)
                    status(g, name)
                    DeviceLog.read(mirrorName, { write(g, Ble.STORE, Ble.CTRL, it) }, ctrl, data)
                    clean(g, m)
                    lastStatus = now
                    Log.i("live-record", "ble status: battery ${hw["battery"]}% card ${hw["store"]} free ${hw["free_kb"]} KB")
                } else jobs.poll()?.invoke(g)
            }
        } catch (e: Gone) {
        } catch (e: Exception) {
            Log.w("live-record", "ble session: $e")
            if (gatt === g) { state = "录音设备出错：${e.message}"; g.disconnect() }
        }
    }

    /** The phone's clock into the stream: the board puts a 'T' record (this frame was at this time) where it is. */
    private fun sync(g: BluetoothGatt): Long {
        val now = System.currentTimeMillis()
        write(g, Ble.STORE, Ble.TIME, le(ByteArray(8)).putLong(now).array())
        return now
    }

    private class Status(val boot: Int, val frame: Long, val store: Int)

    /** The recorder no longer knows this phone: no pairing behind the person's back, no reconnecting. */
    private fun kicked(g: BluetoothGatt, why: String) {
        Log.w("live-record", "ble kicked: $why")
        prefs.edit().putBoolean("bleKicked", true).apply()
        DeviceLog.app(Ble.tag(prefs.getString("bleName", null) ?: g.device.address), "kicked", "录音设备不再认这台手机（$why），不再重连", "error")
        state = KICKED
        g.disconnect()
    }

    /** Pause the recording for `s` seconds (0: go on now); the recorder keeps the time itself. */
    fun pause(s: Int) {
        if (gatt == null) throw Exception("录音设备没连上")
        jobs.put { g ->
            write(g, Ble.STORE, Ble.CTRL, le(ByteArray(5)).put(0x20).putInt(s).array())  // PAUSE
            pausedUntil = if (s > 0) System.currentTimeMillis() + s * 1000L else 0L
            DeviceLog.app(mirrorName, if (s > 0) "pause" else "resume", if (s > 0) "暂停录音 ${if (s % 60 == 0) "${s / 60} 分钟" else "$s 秒"}" else "恢复录音")
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
    @Volatile var syncNote: String? = null  // the last catch-up, for the app's recorder page
    @Volatile var syncDoneKb = 0L           // the catch-up under way: taken of total (0 of 0: none)
    @Volatile var syncTotalKb = 0L

    /** The board's SD card as a USB disk on the computer it is plugged into (CTRL 0x06), or back. */
    fun usbDisk(on: Boolean) {
        if (gatt == null) { usbNote = "录音设备没连上"; return }
        usbNote = if (on) "正在切换到 U 盘模式" else "正在切回录音"
        jobs.put { g ->
            val r = reply(g, byteArrayOf(6, if (on) 1 else 0), 0x86)
            val err = r?.get(5)?.toInt()
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

    /** A CTRL command and its reply (op), other replies passed over; null: none in 15 s. */
    private fun reply(g: BluetoothGatt, cmd: ByteArray, op: Int): ByteArray? {
        ctrl.removeIf { it[0] == op.toByte() }
        write(g, Ble.STORE, Ble.CTRL, cmd)
        val until = System.currentTimeMillis() + 15_000
        while (System.currentTimeMillis() < until) {
            val r = ctrl.poll(1, TimeUnit.SECONDS) ?: continue
            if (r[0] == op.toByte()) return r
        }
        return null
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

    /** LIST, then READ whatever the mirror lacks, oldest first: what the board recorded while the phone was away (it
     *  keeps writing the card; the stream goes on in the same file), files from earlier boots, a lost notify. */
    private fun catchUp(g: BluetoothGatt, m: Mirror) {
        val r = mutableListOf<Pair<Long, Long>>()
        ctrl.removeIf { it[0] == 0x81.toByte() || it[0] == 0x80.toByte() }
        write(g, Ble.STORE, Ble.CTRL, byteArrayOf(1))  // LIST
        var open = -1L
        while (true) {
            val x = ctrl.poll(10, TimeUnit.SECONDS) ?: throw Exception("卡上文件列表超时")
            if (x[0] == 0x81.toByte()) r += (le(x).getInt(1).toLong() and 0xffffffffL) to (le(x).getInt(5).toLong() and 0xffffffffL)
            if (x[0] == 0x80.toByte()) {
                if (x[1] != 0.toByte()) Log.w("live-record", "ble LIST error ${x[1]}")
                if (x.size >= 6) open = le(x).getInt(2).toLong() and 0xffffffffL
                break
            }
        }
        m.listed(r.toMap(), open)
        // the open file last, listed or not (a full LIST may leave it out): READ goes up to what the stream has finished
        val todo = m.missing().filter { it.first != open } + listOf(open to m.have(open)).filter { open >= 0 }
        val total = todo.sumOf { (f, from) -> maxOf(0, m.files[f]!!.size - from) }
        Log.i("live-record", "ble LIST: ${r.size} files, open %08X, ${todo.size} to READ, $total B".format(open))
        // a short interval for it (the result is logged below); the recorder puts its slow one back after the last READ
        if (total > 0) g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
        var done = 0L
        syncTotalKb = total / 1024; syncDoneKb = 0
        val t0 = android.os.SystemClock.elapsedRealtime()
        for ((f, from) in todo) {
            state = "补传离线录音"
            var stalls = 0
            while (f == open || m.have(f) < m.files[f]!!.size) {
                val before = m.have(f)
                take(g, m, f) { syncDoneKb = (done + it - from) / 1024 }
                pieces(m)
                // the open file: one READ, up to the stream's end as it began; LIVE (subscribed first) has every record
                // from there. Reading on never ended while recording: each READ found what the last one took time for (#56)
                if (f == open) break
                if (m.have(f) == before && ++stalls > 3) { m.noMore(f); break }  // the card has no more of it
            }
            done += m.have(f) - from
        }
        Log.i("live-record", "ble caught up: $done B in ${(android.os.SystemClock.elapsedRealtime() - t0) / 1000} s")
        syncNote = "${java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date())} 补传录音设备的录音 ${done / 1024} KB"
        if (done > 0) DeviceLog.app(mirrorName, "sync", "补传 ${done / 1024} KB，用时 ${(android.os.SystemClock.elapsedRealtime() - t0) / 1000} s")
        syncDoneKb = 0; syncTotalKb = 0
    }

    /** One READ of `f` from what the mirror has, into it, until the board says it is at the end (0x82) or a notify is
     *  lost (the caller asks again from there). */
    private fun take(g: BluetoothGatt, m: Mirror, f: Long, progress: (Long) -> Unit) {
        data.clear()
        ctrl.removeIf { it[0] == 0x82.toByte() }
        write(g, Ble.STORE, Ble.CTRL, le(ByteArray(9)).put(2).putInt(f.toInt()).putInt(m.have(f).toInt()).array())  // READ
        var last = System.currentTimeMillis()
        while (System.currentTimeMillis() - last < 10_000) {
            val d = data.poll(200, TimeUnit.MILLISECONDS)
            if (d == null) {
                if (ctrl.any { it[0] == 0x82.toByte() } && data.isEmpty()) return  // the end; its DATA came before it
                continue
            }
            last = System.currentTimeMillis()
            if (m.put(f, le(d).getInt(0).toLong() and 0xffffffffL, d.copyOfRange(4, d.size)) != null) {
                // a hole: STOP, and the rest of this READ (up to its 0x82) is not taken for the next one, maybe of another file
                write(g, Ble.STORE, Ble.CTRL, byteArrayOf(3))
                val until = System.currentTimeMillis() + 3000
                while (ctrl.none { it[0] == 0x82.toByte() } && System.currentTimeMillis() < until) Thread.sleep(50)
                data.clear()
                return
            }
            if (m.have(f) % 32768 < d.size) progress(m.have(f))
        }
    }

    private var mirrorName = ""  // the session's: the board's name as pieces carry it, its boot now
    private var boot = 0

    /** The mirror into dated pieces (Mirror.pieces): the live file in pieces as long as the app's slice. */
    private fun pieces(m: Mirror) {
        val n = m.pieces(chunks, undated, mirrorName, sliceS() * 100, boot)
        if (n > 0) onPiece(n)
    }

    /** Files mirrored whole and uploaded: off the card (DELETE) and out of the mirror. */
    private fun clean(g: BluetoothGatt, m: Mirror) {
        for (f in m.done(chunks)) {
            val r = reply(g, le(ByteArray(5)).put(4).putInt(f.toInt()).array(), 0x84) ?: return  // DELETE
            val err = r[5].toInt()
            Log.i("live-record", "ble DELETE %08X: $err".format(f))
            if (err == 0 || err == -2) m.drop(f)  // gone (-2: not on this card)
        }
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
                DeviceLog.disconnected(mirrorName.ifEmpty { Ble.tag(g.device.name ?: g.device.address) }, status)
                lastSeen = java.text.SimpleDateFormat("HH:mm", java.util.Locale.getDefault()).format(java.util.Date())
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
                Ble.LIVE -> live.put(value)
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
