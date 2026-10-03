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
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import java.util.concurrent.BlockingQueue

/** The BLE recorder (XIAO nRF52840 Sense, Omi devkit firmware): 16 kHz mono Opus in 10 ms frames, each notify
 *  `[seq_lo, seq_hi, sub_index] + payload`, a frame split over notifies when the MTU is small. */
object Ble {
    val SERVICE: UUID = UUID.fromString("19b10000-e8f2-537e-4f6c-d104768a1214")
    val AUDIO: UUID = UUID.fromString("19b10001-e8f2-537e-4f6c-d104768a1214")
    val CODEC: UUID = UUID.fromString("19b10002-e8f2-537e-4f6c-d104768a1214")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    const val OPUS_16K = 20       // the codec characteristic's value for 16 kHz mono Opus, 10 ms frames
    const val FRAME_US = 10_000L

    /** One notify and when it arrived; `data` null = the link went down. */
    class Packet(val at: Long, val data: ByteArray?)

    /** What the Ogg muxer needs for frames it did not encode itself: OpusHead (pre-skip 0), codec delay, seek pre-roll. */
    fun opusFormat(): MediaFormat = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 16000, 1).apply {
        fun le(n: Int, fill: ByteBuffer.() -> Unit) = ByteBuffer.allocate(n).order(ByteOrder.LITTLE_ENDIAN).apply { fill(); flip() }
        setByteBuffer("csd-0", le(19) { put("OpusHead".toByteArray()); put(1); put(1); putShort(0); putInt(16000); putShort(0); put(0) })
        setByteBuffer("csd-1", le(8) { putLong(0) })
        setByteBuffer("csd-2", le(8) { putLong(80_000_000) })
    }
}

/** Finds the recorder by scanning for its service (always: the board's address is random static, and a device made
 *  from a bare address after a Bluetooth restart is tried as public and times out, status 147, 10-03), subscribes to its audio and
 *  puts every notify on `out`. A dropped link is reconnected with autoConnect, which waits for the device to come back
 *  in range; each drop puts a null packet so the slicer ends the run. Permissions are checked before the service starts. */
@SuppressLint("MissingPermission")
class BleLink(private val ctx: Context, private val out: BlockingQueue<Ble.Packet>) {
    @Volatile var state = "正在找录音设备"
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
        gatt?.let { it.disconnect(); it.close() }
        gatt = null
    }

    private fun scan() {
        val scanner = adapter.bluetoothLeScanner ?: return retry("蓝牙未打开") { scan() }
        state = "正在找录音设备"
        // filtered by service: unfiltered scans are paused while the screen is off
        scanner.startScan(listOf(ScanFilter.Builder().setServiceUuid(ParcelUuid(Ble.SERVICE)).build()),
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), scanCb)
    }

    private val scanCb = object : ScanCallback() {
        override fun onScanResult(type: Int, r: ScanResult) {
            adapter.bluetoothLeScanner?.stopScan(this)
            Log.i("live-record", "ble found ${r.device.address} ${r.scanRecord?.deviceName} rssi=${r.rssi}")
            if (on) connect(r.device, false)
        }
        override fun onScanFailed(code: Int) = retry("扫描失败 $code") { scan() }
    }

    private fun retry(why: String, what: () -> Unit) {
        state = "$why，5 秒后重试"
        if (on) main.postDelayed({ if (on) what() }, 5000)
    }

    /** The first try connects directly (quick); after a drop autoConnect waits for the device without a timeout. */
    private fun connect(d: BluetoothDevice, auto: Boolean) {
        state = if (auto) "录音设备已断开，等待重连" else "正在连接录音设备"
        gatt = d.connectGatt(ctx, auto, cb, BluetoothDevice.TRANSPORT_LE) ?: return retry("连接失败") { connect(d, true) }
    }

    private val cb = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            Log.i("live-record", "ble state=$newState status=$status")
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                state = "已连接，准备中"
                g.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)  // a fixed short interval; the result is logged below
                g.requestMtu(512)  // one frame per notify
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                g.close()
                if (gatt !== g) return  // an old link, already given up (Bluetooth turned off)
                gatt = null
                out.put(Ble.Packet(System.currentTimeMillis(), null))
                if (on) main.post { if (on) connect(g.device, true) }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            Log.i("live-record", "ble mtu=$mtu status=$status")
            g.discoverServices()
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val codec = g.getService(Ble.SERVICE)?.getCharacteristic(Ble.CODEC)
            if (codec == null) { state = "设备上没有录音服务"; return }
            g.readCharacteristic(codec)
        }

        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, status: Int) {
            val id = value.firstOrNull()?.toInt()?.and(0xff)
            if (id != Ble.OPUS_16K) { state = "设备编码 $id 不支持（只支持 16 kHz Opus）"; return }
            val audio = g.getService(Ble.SERVICE).getCharacteristic(Ble.AUDIO)
            g.setCharacteristicNotification(audio, true)
            val cccd = audio.getDescriptor(Ble.CCCD)
            if (Build.VERSION.SDK_INT >= 33) g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            else { cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE; g.writeDescriptor(cccd) }
            state = "已连接"
        }

        @Deprecated("before Android 13")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) =
            onCharacteristicRead(g, c, c.value ?: ByteArray(0), status)

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            out.put(Ble.Packet(System.currentTimeMillis(), value))
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
