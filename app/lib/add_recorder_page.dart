import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import 'page_widgets.dart';
import 'recorder.dart';
import 'recorder_status.dart';
import 'settings.dart';

/// The add-recorder page's rows after a scan (Recorder.nearby: the ones seen in the last 5 s, strongest first). A row
/// keeps its place while the page is open, so the one under a finger is the one it pressed: one already listed is
/// updated where it is (found by address, or by its full name: one name, one row), a new one goes to the end (the first
/// scan's come strongest first) and one missing from 3 scans in a row stays where it was, 'gone' (Android restarts its
/// scan every 30 s, which empties one). 'nearest' marks the strongest of several not gone, and moves to another only
/// 6 dB stronger: RSSI swings a few dB from scan to scan.
List<Map> mergeNearby(List<Map> rows, List scan) {
  final out = [for (final r in rows) {...r, 'missed': (r['missed'] as int) + 1}];
  for (final Map d in scan) {
    var i = out.indexWhere((r) => r['address'] == d['address']);
    if (i < 0 && RegExp(r'^LR-[A-Z0-9]+-[0-9A-F]{4}$').hasMatch(d['name'])) i = out.indexWhere((r) => r['name'] == d['name']);
    if (i < 0) {
      out.add({...d, 'missed': 0});
    } else if (out[i]['missed'] != 0) {  // else this scan has it twice (one name, two addresses): the first, stronger, stays
      out[i] = {...d, 'missed': 0, 'nearest': out[i]['nearest']};
    }
  }
  for (final r in out) {
    r['gone'] = r['missed'] >= 3;
  }
  final live = out.where((r) => !r['gone']).toList();
  Map? best;
  for (final r in live) {
    if (best == null || r['rssi'] > best['rssi']) best = r;
  }
  final held = live.where((r) => r['nearest'] == true).firstOrNull;
  final nearest = held != null && best!['rssi'] - held['rssi'] < 6 ? held : best;
  for (final r in out) {
    r['nearest'] = live.length > 1 && identical(r, nearest);
  }
  return out;
}

/// Adding a recorder: the permissions the link needs, then the recorders around in the order found (mergeNearby: they
/// do not move while the page is open, the nearest is marked) with the state each advertises. An unbound one binds with
/// one tap; one bound to another phone only after a confirmation (it then forgets that phone); one connected to another
/// phone cannot be reached until that phone lets go; one no longer seen is greyed out. Pops true once one is chosen.
class AddRecorderPage extends StatefulWidget {
  const AddRecorderPage({super.key});
  @override
  State<AddRecorderPage> createState() => _AddRecorderPageState();
}

class _AddRecorderPageState extends State<AddRecorderPage> {
  Map perms = {};
  List<Map> found = [];
  Timer? timer;
  bool scanning = false;

  @override
  void initState() {
    super.initState();
    _tick();
    timer = Timer.periodic(const Duration(seconds: 1), (_) => _tick());
  }

  @override
  void dispose() {
    timer?.cancel();
    Recorder.scan(false);
    super.dispose();
  }

  int ticks = 0;

  Future<void> _tick() async {
    final p = await Recorder.permissions();
    // (re)started once permitted and then every 30 s: a scan started with Bluetooth off finds nothing, and Android
    // allows an app 5 scan starts in 30 s
    if (p['bluetooth'] == true && p['bluetoothOn'] == true && (!scanning || ++ticks % 30 == 0)) {
      scanning = true;
      await Recorder.scan(true);
    }
    final f = await Recorder.nearby();
    if (mounted) setState(() { perms = p; found = mergeNearby(found, f); });
  }

  Future<void> _bind(Map d, bool force) async {
    if (force) {
      final ok = await showDialog<bool>(context: context, builder: (c) => AlertDialog(
        title: Text('绑定 ${d['name']}？'),
        content: const Text('它已经绑定了别的手机。绑定到这台手机后，原来的手机将无法再连接它。'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('取消')),
          TextButton(onPressed: () => Navigator.pop(c, true), child: const Text('强制绑定')),
        ],
      ));
      if (ok != true) return;
    }
    final old = (await Recorder.recorder())['name'] as String?;
    await Recorder.withoutLink(() => Recorder.bind(d['name'], d['address'], force));
    if (old != null && old != d['name']) await releaseHold(old);  // the one bound before: no longer this phone's
    if (mounted) Navigator.pop(context, true);
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), cs = t.colorScheme;
    final small = t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant);
    Widget perm(String key, String title, String why) => ListTile(
          contentPadding: EdgeInsets.zero,
          leading: Icon(perms[key] == true ? Icons.check_circle_rounded : Icons.error_outline_rounded,
              color: perms[key] == true ? Colors.green.shade600 : cs.error),
          title: Text(title),
          subtitle: Text(why),
          trailing: perms[key] == true ? null : TextButton(onPressed: () => Recorder.askPermission(key), child: const Text('允许')),
        );
    return Scaffold(
      appBar: AppBar(title: const Text('添加录音设备')),
      body: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 32), children: [
        if (perms['bluetoothOn'] == false)
          Padding(padding: const EdgeInsets.only(top: 12), child: Text('蓝牙没有打开：打开后这里会开始找录音设备', style: TextStyle(color: cs.error))),
        if (['bluetooth', 'notifications', 'battery'].any((k) => perms.isNotEmpty && perms[k] != true))
          sectionCard(context, '权限', Column(children: [
            perm('bluetooth', Platform.isIOS ? '蓝牙' : '附近的设备', '找到并连接录音设备'),
            perm('notifications', '通知', '后台连接录音设备时要显示一条通知'),
            perm('battery', '后台运行', '不受电池优化限制，录音设备才能整天连着'),
          ])),
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Text('打开录音设备（插上充电线即可）。没绑定的录音设备蓝灯慢闪，名字印在它身上，或看标着「最近」的那台。', style: small),
        ),
        if (perms['bluetooth'] == true && found.isEmpty)
          const Padding(padding: EdgeInsets.all(24), child: Center(child: Text('正在找附近的录音设备…'))),
        for (final d in found) _row(d, cs),
      ]),
    );
  }

  Widget _row(Map d, ColorScheme cs) {
    final state = d['state'] as int, rssi = d['rssi'] as int;
    // busy: not connectable (connected to another phone), told by the advertisement itself; the state byte comes in
    // the scan response, which a busy recorder seldom gets across
    final busy = d['busy'] == true || (state >= 0 && state & 2 != 0), bound = state >= 0 && state & 1 != 0;
    final near = rssi > -60 ? '就在旁边' : rssi > -80 ? '附近' : '较远';
    final (String what, Widget? action) = d['gone']
        ? ('信号弱，暂时找不到', null)
        : busy
            ? ('正连着其他手机：先在那台手机上解除绑定，或关掉它的蓝牙', null)
            : bound
                ? ('已绑定其他手机', TextButton(onPressed: () => _bind(d, true), child: const Text('强制绑定')))
                : (state < 0 ? '旧固件，状态未知' : '未绑定', FilledButton(onPressed: () => _bind(d, false), child: const Text('绑定')));
    return Card.outlined(
      child: ListTile(
        enabled: !d['gone'],
        leading: Icon(Icons.mic_external_on_rounded, color: d['gone'] || busy || bound ? cs.outline : cs.primary),
        title: Row(children: [
          Flexible(child: Text(d['name'])),
          if (d['nearest']) Padding(padding: const EdgeInsets.only(left: 8), child: Badge(label: const Text('最近'), backgroundColor: cs.primary)),
        ]),
        subtitle: Text(d['gone'] ? what : [what, near, advText(d)].where((s) => s.isNotEmpty).join(' · ')),
        trailing: action,
      ),
    );
  }
}
