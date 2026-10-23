// The BLE recorder's firmware from the server (server/firmware.py): every version of the recorder's model with its
// notes, the newest sent over Bluetooth (Dfu.kt on Android, DfuRun.swift on iOS) only when the person asks, and the
// recorder's last updates (/api/upgrades).
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import 'page_widgets.dart';
import 'recorder.dart';
import 'settings.dart';

const _ch = MethodChannel('live_record/recorder');

Future<http.Response> _get(String path) async {
  final s = await Settings.load();
  return http.get(Uri.parse('${s.server}$path'), headers: {'Authorization': 'Bearer ${s.token}'}).timeout(const Duration(seconds: 60));
}

/// The recorder page's way in: the revision the recorder runs and how an update goes.
class FirmwareTile extends StatelessWidget {
  final String name;
  final String? model, fw, dfu;  // the bound recorder's model and revision as last read; the update's progress
  final bool dfuBusy;
  const FirmwareTile({super.key, required this.name, required this.model, required this.fw, required this.dfu, required this.dfuBusy});

  @override
  Widget build(BuildContext context) {
    final model = this.model;
    return ListTile(
      contentPadding: EdgeInsets.zero,
      leading: Icon(Icons.system_update_rounded, color: Theme.of(context).colorScheme.onSurfaceVariant),
      title: const Text('固件更新'),
      subtitle: Text([fw ?? '—', ?dfu].join('\n')),
      trailing: dfuBusy
          ? const SizedBox.square(dimension: 24, child: CircularProgressIndicator(strokeWidth: 2.5))
          : const Icon(Icons.chevron_right_rounded),
      onTap: model == null
          ? null
          : () => Navigator.push(context, MaterialPageRoute(builder: (_) => FirmwarePage(name: name, model: model))),
    );
  }
}

/// One recorder's firmware: the revision it runs, the newest of its model and the update to it, every version of the
/// model newest first with its notes (the ones it would get open), and its last updates.
class FirmwarePage extends StatefulWidget {
  final String name, model;
  const FirmwarePage({super.key, required this.name, required this.model});
  @override
  State<FirmwarePage> createState() => _FirmwarePageState();
}

class _FirmwarePageState extends State<FirmwarePage> {
  Map r = {}, st = {};  // the link, read as the recorder page does
  List<Map>? versions, upgrades;  // the server's, newest first
  String? versionsError, upgradesError, note;  // note: what the last download said
  bool busy = false;
  Timer? timer;

  Map get hw => (st['hw'] as Map?) ?? {};
  String? get fw => (hw['fw'] ?? r['fw']) as String?;
  bool get linked => st['running'] == true && hw.isNotEmpty;
  bool get dfuBusy => st['dfuBusy'] == true;
  Map? get latest => versions?.firstOrNull;
  // revisions are YYYYMMDD.HHMMSS-<commit> (lr.sh): a later one sorts later as text
  bool newer(Map v) => fw == null || (v['version'] as String).compareTo(fw!) > 0;

  @override
  void initState() {
    super.initState();
    _poll();
    _fetch();
    timer = Timer.periodic(const Duration(seconds: 2), (_) => _poll());
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  Future<void> _poll() async {
    final a = await Recorder.recorder(), b = await Recorder.status(), was = dfuBusy;
    if (!mounted) return;
    setState(() { r = a; st = b; });
    if (was && !dfuBusy) _fetch();  // an update ended: its record
  }

  Future<void> _fetch() async {
    Future<(List<Map>?, String?)> list(String path) async {
      try {
        final res = await _get(path);
        if (res.statusCode != 200) return (null, 'HTTP ${res.statusCode}');
        return ((jsonDecode(utf8.decode(res.bodyBytes)) as List).cast<Map>(), null);
      } catch (e) {
        return (null, '$e');
      }
    }
    final since = DateTime.now().subtract(const Duration(days: 90)).toIso8601String().substring(0, 10);
    final (v, u) = await (
      list('/api/firmware?model=${Uri.encodeQueryComponent(widget.model)}'),
      list('/api/upgrades?device=${Uri.encodeQueryComponent(widget.name)}&since=$since'),
    ).wait;
    if (!mounted) return;
    setState(() {
      versions = v.$1;
      versionsError = v.$2;
      upgrades = u.$1?.reversed.toList();
      upgradesError = u.$2;
    });
  }

  Future<void> _update() async {
    final m = latest!, battery = linked ? hw['battery'] as int? : null;  // as the page showed it: no battery once the link is down
    if (await _confirm(m, battery) != true) return;
    setState(() { busy = true; note = '正在下载固件'; });
    try {
      final res = await _get('/api/firmware/${m['model']}/${m['version']}.zip');
      if (res.statusCode != 200) throw 'HTTP ${res.statusCode}';
      note = null;
      // the native side checks again: the model the recorder said it is, and the package's sha256; version and battery
      // go into the device log (DfuLog)
      await Recorder.withoutLink(() => _ch.invokeMethod('dfu',
          {'model': m['model'], 'zip': res.bodyBytes, 'sha256': m['sha256'], 'version': m['version'], 'battery': battery}));
    } on PlatformException catch (e) {
      note = e.message;
    } catch (e) {
      note = '下载失败：$e';
    }
    if (mounted) setState(() => busy = false);
  }

  Future<bool?> _confirm(Map m, int? battery) {
    final cs = Theme.of(context).colorScheme, model = widget.model;
    final same = m['model'] == model, low = battery != null && battery < 30;
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
            Text('${fw ?? '未知'} → ${m['version']}'),
            line(same ? Icons.check_circle_rounded : Icons.block_rounded,
                same ? '型号一致：$model' : '型号不符：录音设备是 $model，固件是 ${m['model']}，不能升级',
                same ? Colors.green.shade600 : cs.error),
            line(Icons.usb_off_rounded, '先拔掉录音设备的 USB：插着 USB 升级会卡在擦除'),
            line(Icons.battery_charging_full_rounded, '电量保持在 30% 以上${battery == null ? '' : '（现在 $battery%）'}', low ? cs.error : null),
            line(Icons.phonelink_ring_rounded, '升级要几分钟：手机放在录音设备旁边，别关 App；期间不录音也不同步'),
            line(Icons.info_outline_rounded, '中途失败、录音设备不见了：插 USB 用 lr.sh flash 刷（升级模式下的录音设备换了地址，再点升级连不上）'),
          ]),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          FilledButton(onPressed: same ? () => Navigator.pop(c, true) : null, child: const Text('开始升级')),
        ],
      ),
    );
  }

  static String _when(String? iso) => iso == null ? '—' : iso.replaceFirst('T', ' ').padRight(16).substring(0, 16).trim();

  static String _took(num ms) {
    final s = (ms / 1000).round();
    return s < 60 ? '$s 秒' : '${s ~/ 60} 分 ${s % 60} 秒';
  }

  Widget _tag(String text, Color color) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
        decoration: BoxDecoration(color: color.withValues(alpha: 0.15), borderRadius: BorderRadius.circular(4)),
        child: Text(text, style: TextStyle(color: color, fontSize: 12, fontWeight: FontWeight.w600)),
      );

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), cs = t.colorScheme;
    final small = t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant);
    final m = latest, fw = this.fw, wait = busy || dfuBusy, dfu = (st['dfu'] ?? r['dfu']) as String?;
    final canUpdate = m != null && newer(m);
    // LR-P1's bootloader (nice!nano 0.6.0) erases the firmware when the transfer starts, and a reset midway leaves it
    // in USB-only update mode: a failed Bluetooth update always ends on USB (10-05), so it is updated over USB only
    // (LR-P2, the same board kept for tests with bootloader 0.10.0, keeps the button: its updates are the test)
    final usbOnly = widget.model == 'LR-P1';
    final (String chip, Color chipColor, IconData chipIcon) = versions == null && versionsError == null
        ? ('检查中', cs.outline, Icons.hourglass_empty_rounded)
        : canUpdate
            ? ('有新版本', cs.tertiary, Icons.new_releases_rounded)
            : m != null
                ? ('已是最新', Colors.green.shade600, Icons.check_circle_rounded)
                : ('没有固件', cs.outline, Icons.cloud_off_rounded);

    return Scaffold(
      appBar: AppBar(title: const Text('固件更新')),
      body: RefreshIndicator(
        onRefresh: () => Future.wait([_poll(), _fetch()]),
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 32), children: [
          heroCard(context, icon: Icons.memory_rounded, title: widget.name, chips: [
            stateChip(chip, chipColor, chipIcon),
            Chip(label: Text(widget.model), visualDensity: VisualDensity.compact),
          ]),
          sectionCard(context, '版本', Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            ListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              leading: const Icon(Icons.memory_rounded),
              title: const Text('当前版本'),
              subtitle: Text(fw ?? '还不知道：连上录音设备后读取'),
            ),
            ListTile(
              contentPadding: EdgeInsets.zero,
              dense: true,
              leading: const Icon(Icons.cloud_download_rounded),
              title: const Text('最新版本'),
              subtitle: Text(m != null
                  ? '${m['version']}\n发布于 ${_when(m['uploaded'] as String?)}'
                  : versionsError != null
                      ? '查不到：$versionsError'
                      : versions == null
                          ? '正在查服务器'
                          : '服务器上还没有 ${widget.model} 的固件'),
            ),
            if (wait) ...[
              const SizedBox(height: 4),
              const LinearProgressIndicator(minHeight: 6),
            ],
            for (final s in [?note, ?dfu]) Padding(padding: const EdgeInsets.only(top: 8), child: Text(s)),
            const SizedBox(height: 8),
            if (usbOnly)
              Text('LR-P1 只能插 USB 升级：lr.sh flash promicro（蓝牙升级中途一断，只能插 USB 救回）', style: small)
            else if (canUpdate)
              FilledButton.icon(
                onPressed: wait ? null : _update,
                icon: const Icon(Icons.system_update_alt_rounded),
                label: const Text('升级到最新版本'),
                style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
              )
            else if (m != null && fw != null)
              Text('已是最新版本', style: small),
          ])),
          sectionCard(context, '更新日志', versions == null || versions!.isEmpty
              ? Text(versionsError != null ? '查不到：$versionsError' : versions == null ? '正在查服务器' : '还没有发布过的版本', style: small)
              : Column(children: [
                  for (final v in versions!)
                    ExpansionTile(
                      tilePadding: EdgeInsets.zero,
                      shape: const Border(),
                      collapsedShape: const Border(),
                      expandedCrossAxisAlignment: CrossAxisAlignment.start,
                      childrenPadding: const EdgeInsets.only(bottom: 12),
                      // the ones an update would bring, else the one it runs
                      initiallyExpanded: canUpdate ? newer(v) : v['version'] == fw,
                      title: Wrap(spacing: 6, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
                        Text('${v['version']}', style: t.textTheme.bodyLarge),
                        if (identical(v, m)) _tag('最新', cs.primary),
                        if (v['version'] == fw) _tag('当前', Colors.green.shade700),
                      ]),
                      subtitle: Text('发布于 ${_when(v['uploaded'] as String?)}', style: small),
                      children: [
                        if ('${v['notes'] ?? ''}'.trim().isEmpty) Text('没有说明', style: small),
                        for (final l in '${v['notes'] ?? ''}'.split('\n').where((l) => l.trim().isNotEmpty))  // a change a line (lr_release.py)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 4),
                            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                              const Text('•  '),
                              Expanded(child: Text(l.trim())),
                            ]),
                          ),
                      ],
                    ),
                ])),
          sectionCard(context, '最近的升级', upgrades == null || upgrades!.isEmpty
              ? Text(upgradesError != null ? '查不到：$upgradesError' : upgrades == null ? '正在查服务器' : '最近 90 天没有升级过', style: small)
              : Column(children: [
                  for (final u in upgrades!.take(5))
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      dense: true,
                      leading: u['ok'] == true
                          ? Icon(Icons.check_circle_rounded, color: Colors.green.shade600)
                          : u['ok'] == false
                              ? Icon(Icons.error_rounded, color: cs.error)
                              : Icon(Icons.help_rounded, color: cs.outline),
                      title: Text('${u['from'] ?? '?'} → ${u['to'] ?? '?'}'),
                      subtitle: Text([
                        _when(u['at'] as String?),
                        u['ok'] == true ? '成功' : u['ok'] == false ? '失败' : '没有结果',
                        if (u['ms'] != null) '用时 ${_took(u['ms'] as num)}',
                        if (u['why'] != null) '${u['why']}',
                      ].join(' · ')),
                    ),
                ])),
        ]),
      ),
    );
  }
}
