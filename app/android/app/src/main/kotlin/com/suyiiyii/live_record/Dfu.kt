package com.suyiiyii.live_record

import android.content.Context
import android.net.Uri
import no.nordicsemi.android.dfu.DfuBaseService
import no.nordicsemi.android.dfu.DfuProgressListenerAdapter
import no.nordicsemi.android.dfu.DfuServiceInitiator
import no.nordicsemi.android.dfu.DfuServiceListenerHelper
import java.io.File

/** Firmware updates over Bluetooth with Nordic's DFU library: the app asks the running firmware to reboot into the
 *  Adafruit bootloader (legacy DFU, as firmware/devkit/lr_dfu.py does) and sends the package for its model, which
 *  the recorder page downloaded from the server (firmware.dart, server/firmware.py). Only while not recording: the
 *  recorder takes one connection at a time. */
object Dfu {
    @Volatile var state: String? = null
    @Volatile var busy = false  // from start until done, aborted or failed: the app keeps the recorder link down

    fun start(ctx: Context, address: String, name: String, zip: File) {
        DfuServiceInitiator.createDfuNotificationChannel(ctx)
        DfuServiceListenerHelper.registerProgressListener(ctx, object : DfuProgressListenerAdapter() {
            override fun onDeviceConnecting(a: String) { state = "正在连接录音设备" }
            override fun onEnablingDfuMode(a: String) { state = "录音设备重启进入升级模式" }
            override fun onDfuProcessStarting(a: String) { state = "开始升级" }
            override fun onProgressChanged(a: String, percent: Int, speed: Float, avgSpeed: Float, part: Int, parts: Int) {
                state = "升级中 $percent% · ${"%.1f".format(avgSpeed)} KB/s"
            }
            override fun onDfuCompleted(a: String) { state = "升级完成，录音设备已重启"; busy = false; DfuServiceListenerHelper.unregisterProgressListener(ctx, this) }
            override fun onDfuAborted(a: String) { state = "升级已取消"; busy = false; DfuServiceListenerHelper.unregisterProgressListener(ctx, this) }
            override fun onError(a: String, error: Int, errorType: Int, message: String?) {
                state = "升级失败：$message（录音设备还在就再试一次，不见了就插 USB 用 lr.sh flash 刷）"; busy = false
                android.util.Log.w("live-record", "dfu error $error type $errorType: $message")
                DfuServiceListenerHelper.unregisterProgressListener(ctx, this)
            }
        })
        state = "准备升级"; busy = true
        android.util.Log.i("live-record", "dfu ${zip.name} to $address")
        DfuServiceInitiator(address).setDeviceName(name).setKeepBond(true)
            .setForceDfu(false)
            .setForceScanningForNewAddressInLegacyDfu(true)  // the bootloader advertises as AdaDFU on another address
            // As lr_dfu.py (issue #33): the Adafruit bootloader stalls mid-image on PRN 16 (XIAO) and on big packets.
            // The library asks for MTU 517 and sends MTU-3 bytes a packet (244 at the bootloader's 247): keep 20 B.
            .setPacketsReceiptNotificationsValue(8)
            .disableMtuRequest()
            .setZip(Uri.fromFile(zip))
            .start(ctx, DfuService::class.java)
    }
}

class DfuService : DfuBaseService() {
    override fun getNotificationTarget(): Class<MainActivity> = MainActivity::class.java
}
