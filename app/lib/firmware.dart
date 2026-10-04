// The BLE recorder's firmware from the server (server/firmware.py): the newest package of the recorder's model, looked
// up and sent over Bluetooth (Dfu.kt) only when the person asks. Android only: on iOS the tile shows the revision.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import 'main.dart';

const _ch = MethodChannel('live_record/recorder');

/// The recorder's revision, a check for a newer one of its model, and the update.
class FirmwareTile extends StatefulWidget {
  final String? model, fw, dfu;  // the bound recorder's model and revision as last read; the update's progress
  final bool dfuBusy;
  final int? battery;
  const FirmwareTile({super.key, required this.model, required this.fw, required this.dfu, required this.dfuBusy, required this.battery});
  @override
  State<FirmwareTile> createState() => _FirmwareTileState();
}

class _FirmwareTileState extends State<FirmwareTile> {
  Map? latest;  // the server's newest package for the model
  String? note;  // what the last check or download said
  bool busy = false;

  // revisions are YYYYMMDD.HHMMSS-<commit> (lr.sh): a later one sorts later as text
  bool get newer => latest != null && (widget.fw == null || (latest!['version'] as String).compareTo(widget.fw!) > 0);

  Future<http.Response> _get(String path) async {
    final s = await Settings.load();
    return http.get(Uri.parse('${s.server}$path'), headers: {'Authorization': 'Bearer ${s.token}'}).timeout(const Duration(seconds: 60));
  }

  Future<void> _check() async {
    setState(() { busy = true; note = null; latest = null; });
    try {
      final res = await _get('/api/firmware/latest?model=${Uri.encodeQueryComponent(widget.model!)}');
      if (res.statusCode == 404) {
        note = '服务器上还没有 ${widget.model} 的固件';
      } else if (res.statusCode != 200) {
        note = '检查失败：HTTP ${res.statusCode}';
      } else {
        latest = jsonDecode(utf8.decode(res.bodyBytes)) as Map;
        if (!newer) note = '已是最新（服务器上 ${latest!['version']}）';
      }
    } catch (e) {
      note = '检查失败：$e';
    }
    if (mounted) setState(() => busy = false);
  }

  Future<void> _update() async {
    final m = latest!;
    if (await _confirm(m) != true) return;
    setState(() { busy = true; note = '正在下载固件'; });
    try {
      final res = await _get('/api/firmware/${m['model']}/${m['version']}.zip');
      if (res.statusCode != 200) throw 'HTTP ${res.statusCode}';
      note = null;
      // Dfu.kt checks again: the model the recorder said it is, and the package's sha256
      await Recorder.withoutLink(() => _ch.invokeMethod('dfu', {'model': m['model'], 'zip': res.bodyBytes, 'sha256': m['sha256']}));
    } on PlatformException catch (e) {
      note = e.message;
    } catch (e) {
      note = '下载失败：$e';
    }
    if (mounted) setState(() => busy = false);
  }

  Future<bool?> _confirm(Map m) {
    final cs = Theme.of(context).colorScheme, battery = widget.battery;
    final same = m['model'] == widget.model, low = battery != null && battery < 30;
    Widget line(IconData icon, String text, [Color? color]) => Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(icon, size: 18, color: color ?? cs.onSurfaceVariant),
            const SizedBox(width: 8),
            Expanded(child: Text(text, style: TextStyle(color: color))),
          ]),
        );
    return showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('升级固件'),
        content: SingleChildScrollView(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            Text('${widget.fw ?? '未知'} → ${m['version']}'),
            if ('${m['notes'] ?? ''}'.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text('${m['notes']}')),
            line(same ? Icons.check_circle_rounded : Icons.block_rounded,
                same ? '型号一致：${widget.model}' : '型号不符：录音设备是 ${widget.model}，固件是 ${m['model']}，不能升级',
                same ? Colors.green.shade600 : cs.error),
            line(Icons.usb_off_rounded, '先拔掉录音设备的 USB：插着 USB 升级会卡在擦除'),
            line(Icons.battery_charging_full_rounded, '电量保持在 30% 以上${battery == null ? '' : '（现在 $battery%）'}', low ? cs.error : null),
            line(Icons.phonelink_ring_rounded, '升级要几分钟：手机放在录音设备旁边，别关 App；期间不录音也不同步'),
            line(Icons.info_outline_rounded, '中途失败时录音设备会停在升级模式：再点一次升级，或插 USB 用 lr.sh flash 刷'),
          ]),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          FilledButton(onPressed: same ? () => Navigator.pop(c, true) : null, child: const Text('开始升级')),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), cs = t.colorScheme;
    final m = latest, wait = busy || widget.dfuBusy;
    final lines = [
      widget.fw ?? '—',
      if (m != null && newer) '可升级到 ${m['version']}（构建于 ${'${m['built']}'.replaceFirst('T', ' ').split('+').first}）',
      if (m != null && newer && '${m['notes'] ?? ''}'.isNotEmpty) '${m['notes']}',
      ?note,
      ?widget.dfu,
      if (Platform.isIOS) 'iPhone 上不能升级固件：用 Android 手机的 App，或插 USB 用 lr.sh flash',
    ];
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: Icon(Icons.memory_rounded, color: cs.onSurfaceVariant),
      title: const Text('固件版本'),
      subtitle: Text(lines.join('\n')),
      trailing: wait
          ? const SizedBox.square(dimension: 24, child: CircularProgressIndicator(strokeWidth: 2.5))
          : widget.model == null || Platform.isIOS
              ? null
              : newer
                  ? FilledButton.tonalIcon(onPressed: _update, icon: const Icon(Icons.system_update_alt_rounded, size: 18), label: const Text('升级'))
                  : TextButton(onPressed: _check, child: const Text('检查更新')),
    );
  }
}
