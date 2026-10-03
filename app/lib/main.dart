// live-record client. Native code keeps the microphone open, cuts the stream into pieces whose length follows
// the app state and uploads them in order: on Android RecorderService.kt (Opus/Ogg, START_STICKY, survives a kill),
// on iOS Recorder.swift (ADTS AAC, `audio` background mode). This side is settings, status and the pages in a WebView.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:mobile_scanner/mobile_scanner.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';

// piece seconds per app state; the server's /api/client-config overrides these (kept from its last answer)
const defaultSlices = {'foreground': 1, 'background': 20, 'screenOff': 60};
const recordingRed = Color(0xffc62828);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(MaterialApp(
    home: const Home(),
    debugShowCheckedModeBanner: false,
    theme: ThemeData(colorSchemeSeed: const Color(0xff3b4cca), useMaterial3: true),
  ));
}

class Settings {
  String server = 'https://lr.example.com', token = '';
  String device = 'phone';  // the phone's own name from the system settings, not set here (BLE audio goes up as the board's name)
  String source = 'mic';  // 'mic' or 'ble' (the BLE recorder; Android only)
  bool archive = true;
  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance(), s = Settings();
    s.server = p.getString('server') ?? s.server;
    s.token = p.getString('token') ?? '';
    s.device = (await Recorder.deviceName()).replaceAll(RegExp(r'[^\p{L}\p{N}._-]+', unicode: true), '_');
    s.archive = p.getBool('archive') ?? true;
    s.source = Platform.isAndroid ? p.getString('source') ?? 'mic' : 'mic';
    return s;
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('server', server);
    await p.setString('token', token);
    await p.setBool('archive', archive);
    await p.setString('source', source);
  }
}

/// Piece lengths from the server, so they change without a new build; the last answer is kept for offline starts.
class Slices {
  static Future<Map<String, int>> cached() async {
    final p = await SharedPreferences.getInstance();
    return {for (final k in defaultSlices.keys) k: p.getInt('slice_$k') ?? defaultSlices[k]!};
  }

  /// The server's lengths, stored; null when it cannot be asked (offline, an older server).
  static Future<Map<String, int>?> fetch(Settings s) async {
    if (s.token.isEmpty) return null;
    try {
      final c = HttpClient()..connectionTimeout = const Duration(seconds: 10);
      final req = await c.getUrl(Uri.parse('${s.server}/api/client-config'));
      req.headers.set('Authorization', 'Bearer ${s.token}');
      final res = await req.close().timeout(const Duration(seconds: 15));
      final body = jsonDecode(await res.transform(utf8.decoder).join()) as Map;
      c.close();
      final got = (body['slice'] as Map).cast<String, dynamic>();
      final out = {for (final k in defaultSlices.keys) k: (got[k] as num?)?.toInt() ?? defaultSlices[k]!};
      if (out.values.any((v) => v < 1)) return null;
      final p = await SharedPreferences.getInstance();
      for (final e in out.entries) {
        await p.setInt('slice_${e.key}', e.value);
      }
      return out;
    } catch (_) {
      return null;
    }
  }
}

class Recorder {
  static const _ch = MethodChannel('live_record/recorder');
  static Future<void> start(Settings s, Map<String, int> slices) => _ch.invokeMethod('start', {
        'server': s.server, 'token': s.token, 'device': s.device, 'archive': s.archive, 'source': s.source, ...slices});
  static Future<void> slices(Map<String, int> slices) => _ch.invokeMethod('slices', slices);  // a running recorder takes them at once
  static Future<void> stop() => _ch.invokeMethod('stop');
  static Future<void> upload() => _ch.invokeMethod('upload');  // pieces left and not recording: send them anyway
  static Future<void> foreground(bool on) => _ch.invokeMethod('foreground', {'on': on});
  static Future<Map> status() async => (await _ch.invokeMethod('status')) as Map;
  static Future<String> deviceName() async => (await _ch.invokeMethod('deviceName')) as String;
  // the BLE recorder (Android): which one is bound, what it last said, the firmware this build carries
  static Future<Map> recorder() async => (await _ch.invokeMethod('recorder')) as Map;
  static Future<void> unbind() => _ch.invokeMethod('unbind');
  static Future<void> dfu() => _ch.invokeMethod('dfu');
  static Future<void> usbDisk(bool on) => _ch.invokeMethod('usbDisk', {'on': on});
  static bool hold = false;  // the link is taken down on purpose (unbind, firmware update): do not bring it back meanwhile

  /// Takes the BLE link down, runs `f`, and lets it come back (the home page's ticker restarts it).
  static Future<void> withoutLink(Future<void> Function() f) async {
    hold = true;
    try {
      await stop();
      while ((await status())['running'] == true) {
        await Future.delayed(const Duration(milliseconds: 300));
      }
      await f();
    } finally {
      hold = false;
    }
  }
}

/// A BLE recorder's battery and card in a few words.
String hwLine(Map? hw) {
  if (hw == null || hw.isEmpty) return '';
  final store = hw['store'], free = hw['free_kb'], total = hw['total_kb'];
  final card = store == -19 ? '没有存储卡' : store == -28 ? '存储卡已满' : total is num && total > 0 ? '卡剩 ${(free / 1048576).toStringAsFixed(1)} GB' : '';
  return [if (hw['battery'] != null) '电量 ${hw['battery']}%', if (card.isNotEmpty) card].join(' · ');
}

class Home extends StatefulWidget {
  const Home({super.key});
  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> with WidgetsBindingObserver {
  Settings? settings;
  Map status = {};
  String note = '';
  bool stopping = false;
  int page = 0;  // 0 字幕, 1 对话, 2 说话人, 3 上传, 4 录音设备, 5 设置
  final webs = <WebViewController?>[null, null, null];  // one per web page: each keeps its scroll while another is shown
  Timer? ticker;
  int ticks = 0;

  static const titles = ['字幕', '对话', '说话人', '上传', '录音设备', '设置'];
  static const paths = ['/', '/conversations', '/speakers'];
  static const uploadPage = 3, recorderPage = 4, settingsPage = 5;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Settings.load().then((s) {
      setState(() {
        settings = s;
        _loadWeb();
      });
      _slices();
    });
    ticker = Timer.periodic(const Duration(seconds: 2), (_) async {  // the service may be running from before this UI opened
      if (++ticks % 150 == 0) _slices();  // every 5 min: the server may have changed the lengths
      final s = await Recorder.status();
      // uploading does not wait for recording: pieces left over (a stop, a kill, a lost network) go now
      if (s['running'] != true && s['uploading'] != true && (s['pending'] ?? 0) > 0 && settings?.token.isNotEmpty == true) {
        await Recorder.upload();
      }
      if (mounted) setState(() => status = s);
      // the BLE recorder records by itself: with it as the source the link just runs, no start/stop (a failed start,
      // e.g. a permission still to grant, is tried again every 30 s)
      if (ble && !Recorder.hold && s['dfuBusy'] != true && s['running'] != true && settings!.token.isNotEmpty &&
          DateTime.now().difference(autoStarted).inSeconds >= 30) {
        autoStarted = DateTime.now();
        await _start();
      }
    });
  }

  DateTime autoStarted = DateTime(2000);
  bool get ble => Platform.isAndroid && settings?.source == 'ble';

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    Recorder.foreground(state == AppLifecycleState.resumed);
    if (state == AppLifecycleState.resumed) _slices();
  }

  /// Ask the server for piece lengths; a running recorder switches to them at its next piece.
  Future<void> _slices() async {
    final s = settings;
    if (s == null) return;
    final got = await Slices.fetch(s);
    if (got != null && running) await Recorder.slices(got);
  }

  bool get running => status['running'] == true;

  void _loadWeb() {
    final s = settings!;
    if (s.token.isEmpty) return;
    WebViewController make() => WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..clearCache();  // the page changes with server deploys
    for (var i = 0; i < webs.length; i++) {  // ?token= sets the cookie, then the page follows itself
      webs[i] ??= make();
      webs[i]!.loadRequest(Uri.parse('${s.server}${paths[i]}?token=${s.token}'));
    }
  }

  Future<void> _saved() async {
    await settings!.save();
    _loadWeb();
    // the source changed under a running recorder: stop it; with the BLE recorder as the new source the ticker starts it
    final changed = running && status['source'] != settings!.source;
    if (changed) await _stop();
    autoStarted = DateTime(2000);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(running && !changed ? '已保存，下次开始录音时生效' : '已保存'), behavior: SnackBarBehavior.floating));
    setState(() => page = 0);
  }

  Future<void> _back() async {  // back key: history inside the page's WebView first, then to 字幕, then leave
    final w = page < webs.length ? webs[page] : null;
    if (w != null && await w.canGoBack()) return w.goBack();
    if (page != 0) return setState(() => page = 0);
    SystemNavigator.pop();
  }

  Future<void> _start() async {
    if (settings!.source == 'mic' && !await AudioRecorder().hasPermission()) return setState(() => note = '没有麦克风权限');
    note = '';
    try {
      await Recorder.start(settings!, await Slices.fetch(settings!) ?? await Slices.cached());
    } on PlatformException catch (e) {
      setState(() => note = '无法开始录音 ${e.message}');
    }
  }

  Future<void> _stop() async {
    setState(() => stopping = true);
    await Recorder.stop();
    while ((await Recorder.status())['running'] == true) {
      await Future.delayed(const Duration(milliseconds: 500));
    }
    setState(() => stopping = false);
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return PopScope(canPop: false, onPopInvokedWithResult: (didPop, _) => didPop ? null : _back(), child: Scaffold(
      appBar: AppBar(
        title: Text(titles[page]),
        actions: [
          if (running) const Padding(padding: EdgeInsets.only(right: 4), child: _Live()),
          if (page < webs.length && webs[page] != null)
            IconButton(icon: const Icon(Icons.refresh), tooltip: '刷新', onPressed: () => webs[page]!.reload()),
        ],
      ),
      drawer: NavigationDrawer(
        selectedIndex: page,
        onDestinationSelected: (i) {
          setState(() => page = i);
          Navigator.pop(context);
        },
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(28, 20, 16, 4),
            child: Text('Live Record', style: Theme.of(context).textTheme.titleLarge),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(28, 0, 16, 16),
            child: Text(settings == null ? '' : '${settings!.device} · ${Uri.tryParse(settings!.server)?.host ?? ''}',
                style: TextStyle(color: cs.onSurfaceVariant)),
          ),
          const NavigationDrawerDestination(
              icon: Icon(Icons.subtitles_outlined), selectedIcon: Icon(Icons.subtitles), label: Text('字幕')),
          const NavigationDrawerDestination(
              icon: Icon(Icons.forum_outlined), selectedIcon: Icon(Icons.forum), label: Text('对话')),
          const NavigationDrawerDestination(
              icon: Icon(Icons.people_outline), selectedIcon: Icon(Icons.people), label: Text('说话人')),
          const NavigationDrawerDestination(
              icon: Icon(Icons.cloud_upload_outlined), selectedIcon: Icon(Icons.cloud_upload), label: Text('上传')),
          const NavigationDrawerDestination(
              icon: Icon(Icons.bluetooth_outlined), selectedIcon: Icon(Icons.bluetooth), label: Text('录音设备')),
          const NavigationDrawerDestination(
              icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings), label: Text('设置')),
        ],
      ),
      body: settings == null
          ? const Center(child: CircularProgressIndicator())
          : IndexedStack(index: page, children: [  // keeps the WebView alive across pages
              for (final w in webs)
                w == null ? _Empty(onSetup: () => setState(() => page = settingsPage)) : WebViewWidget(controller: w),
              UploadPage(settings: settings!, status: status, visible: page == uploadPage),
              RecorderPage(visible: page == recorderPage),
              SettingsPage(settings: settings!, onSaved: _saved),
            ]),
      bottomNavigationBar: page == 0 && settings != null ? _controlBar(cs) : null,
    ));
  }

  /// The BLE recorder as the source: nothing to start or stop, only where its audio is.
  Widget _bleBar(ColorScheme cs) {
    final s = status, hw = (s['hw'] as Map?) ?? {};
    final link = '${s['link'] ?? '正在启动'}';
    final syncDone = (s['syncDone'] as num?) ?? 0, syncTotal = (s['syncTotal'] as num?) ?? 0;
    final connected = link.startsWith('已连接'), syncing = syncTotal > 0;
    final (IconData icon, Color color) = syncing
        ? (Icons.sync_rounded, cs.tertiary)
        : connected
            ? (Icons.bluetooth_connected_rounded, Colors.green.shade600)
            : (Icons.bluetooth_searching_rounded, Colors.orange.shade700);
    final small = TextStyle(fontSize: 12, color: cs.onSurfaceVariant);
    final detail = [
      hwLine(hw),
      if ((s['pending'] ?? 0) > 0) '待传 ${s['pending']} 块',
    ].where((x) => x.isNotEmpty).join(' · ');
    return Material(
      color: cs.surfaceContainer,
      child: SafeArea(
        top: false,
        child: InkWell(
          onTap: () => setState(() => page = recorderPage),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 16, 12),
            child: Row(children: [
              Icon(icon, color: color),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  Text(syncing ? '补传离线录音 ${(syncDone / 1024).toStringAsFixed(1)} / ${(syncTotal / 1024).toStringAsFixed(1)} MB' : link,
                      style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500), maxLines: 2, overflow: TextOverflow.ellipsis),
                  if (syncing) ...[
                    const SizedBox(height: 6),
                    LinearProgressIndicator(value: syncDone / syncTotal, borderRadius: BorderRadius.circular(2)),
                  ],
                  if (detail.isNotEmpty) Text(detail, style: small),
                  if (s['note'] != null) Text('${s['note']}', style: small.copyWith(color: cs.error), maxLines: 2, overflow: TextOverflow.ellipsis),
                  if (note.isNotEmpty) Text(note, style: small.copyWith(color: cs.error)),
                ]),
              ),
              Icon(Icons.chevron_right_rounded, color: cs.onSurfaceVariant),
            ]),
          ),
        ),
      ),
    );
  }

  Widget _controlBar(ColorScheme cs) {
    if (ble) return _bleBar(cs);
    final s = status;
    final small = TextStyle(fontSize: 12, color: cs.onSurfaceVariant);
    String headline, detail = '';
    if (stopping) {
      headline = '停止中';
      detail = '等最后 ${s['pending'] ?? 0} 块传完';
    } else if (running) {
      headline = '录音中 · ${s['slice']} 秒一块';
      if (s['link'] != null) headline += ' · ${s['link']}';
      detail = '已传 ${s['sent']} · 待传 ${s['pending']}';
      final hw = hwLine(s['hw'] as Map?);
      if (hw.isNotEmpty) detail += ' · $hw';
    } else {
      headline = note.isEmpty ? '未在录音' : note;
      final n = s['pending'] ?? 0;
      if (n > 0) detail = s['uploading'] == true ? '正在补传 $n 块' : '待传 $n 块';
    }
    final warn = [
      if (s['error'] != null) '录音错误 ${s['error']}',
      if (s['note'] != null) '${s['note']}',  // also when stopped: iOS reports leftovers sent after a kill
      if (running && s['interruptedAt'] != null)
        '${TimeOfDay.fromDateTime(DateTime.fromMillisecondsSinceEpoch(s['interruptedAt'])).format(context)} 被系统中断过，已恢复',
    ];
    return Material(
      color: cs.surfaceContainer,
      child: SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 10, 16, 10),
          child: Row(children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                Text(headline, style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w500)),
                if (detail.isNotEmpty) Text(detail, style: small),
                for (final w in warn) Text(w, style: small.copyWith(color: cs.error), maxLines: 2, overflow: TextOverflow.ellipsis),
              ]),
            ),
            const SizedBox(width: 12),
            FilledButton.icon(
              style: FilledButton.styleFrom(
                  backgroundColor: running ? recordingRed : null, minimumSize: const Size(0, 48), padding: const EdgeInsets.symmetric(horizontal: 20)),
              onPressed: stopping ? null : (running ? _stop : _start),
              icon: Icon(running ? Icons.stop_rounded : Icons.mic_rounded),
              label: Text(running ? '停止' : '开始录音'),
            ),
          ]),
        ),
      ),
    );
  }
}

/// The phone's upload queue (from the native side's status) and how far the server has got with this device's audio.
class UploadPage extends StatefulWidget {
  final Settings settings;
  final Map status;
  final bool visible;
  const UploadPage({super.key, required this.settings, required this.status, required this.visible});
  @override
  State<UploadPage> createState() => _UploadPageState();
}

class _UploadPageState extends State<UploadPage> {
  Map? server;  // this device's entry of /api/status
  String serverNote = '', dev = '';  // dev: the name its audio goes up as (the BLE recorder's own, or the setting)
  Timer? timer;

  @override
  void initState() {
    super.initState();
    timer = Timer.periodic(const Duration(seconds: 5), (_) => widget.visible ? _fetch() : null);
    _fetch();
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  Future<void> _fetch() async {
    final s = widget.settings;
    if (s.token.isEmpty) return;
    try {
      final c = HttpClient()..connectionTimeout = const Duration(seconds: 10);
      final req = await c.getUrl(Uri.parse('${s.server}/api/status'));
      req.headers.set('Authorization', 'Bearer ${s.token}');
      final res = await req.close().timeout(const Duration(seconds: 15));
      final body = jsonDecode(await res.transform(utf8.decoder).join()) as Map;
      c.close();
      // the BLE recorder's audio goes up under its own name (LR-P1-BDAE), not the device setting
      final ble = s.source == 'ble' ? (await Recorder.recorder())['name'] as String? : null;
      dev = ble?.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_') ?? s.device;
      final mine = (body['devices'] as List? ?? []).cast<Map>().where((d) => d['device'] == dev).firstOrNull;
      if (mounted) setState(() { server = mine; serverNote = mine == null ? '服务端还没有 $dev 的处理任务' : ''; });
    } catch (e) {
      if (mounted) setState(() => serverNote = '取不到服务端状态：$e');
    }
  }

  static String _at(Object? v) {
    if (v == null) return '—';
    final t = v is int ? DateTime.fromMillisecondsSinceEpoch(v) : DateTime.tryParse('$v');
    if (t == null) return '—';
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(t.month)}-${two(t.day)} ${two(t.hour)}:${two(t.minute)}';
  }

  static String _span(num s) {
    final m = (s / 60).round();
    return m >= 60 ? '${m ~/ 60} 小时 ${m % 60} 分' : '$m 分钟';
  }

  static String _size(num b) => b < 1048576 ? '${(b / 1024).round()} KB' : '${(b / 1048576).toStringAsFixed(1)} MB';

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), cs = t.colorScheme, st = widget.status, srv = server;
    final pending = (st['pending'] ?? 0) as int, bytes = (st['pendingBytes'] ?? 0) as num;
    final running = st['running'] == true, uploading = st['uploading'] == true;
    final (String chip, Color chipColor, IconData chipIcon) = running
        ? ('录音中，边录边传', Colors.red.shade600, Icons.fiber_manual_record_rounded)
        : uploading
            ? ('正在上传', cs.tertiary, Icons.cloud_upload_rounded)
            : pending > 0
                ? ('等待上传', Colors.orange.shade700, Icons.schedule_rounded)
                : ('已全部上传', Colors.green.shade600, Icons.cloud_done_rounded);
    final left = (srv?['left_s'] ?? 0) as num, eta = srv?['eta_s'] as num?;
    final caughtUp = left < 30;
    final name = dev.isEmpty ? widget.settings.device : dev;
    return RefreshIndicator(
      onRefresh: _fetch,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 32), children: [
        heroCard(context,
            icon: widget.settings.source == 'ble' ? Icons.mic_external_on_rounded : Icons.smartphone_rounded,
            title: name,
            chips: [stateChip(chip, chipColor, chipIcon)]),
        const SizedBox(height: 12),
        IntrinsicHeight(
          child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            statTile(context,
                icon: Icons.inventory_2_rounded,
                color: pending > 0 ? Colors.orange.shade700 : Colors.green.shade600,
                label: '待传',
                value: pending == 0 ? '0' : '$pending 块',
                sub: pending == 0 ? '没有积压' : '${_size(bytes)} · 最早录于 ${_at(st['oldest'])}'),
            const SizedBox(width: 12),
            statTile(context,
                icon: Icons.cloud_done_rounded, color: cs.primary, label: '本次已传', value: '${st['sent'] ?? 0} 块', sub: '这次开机以来'),
          ]),
        ),
        if (st['note'] != null)
          Card.filled(
            margin: const EdgeInsets.only(top: 12),
            color: cs.errorContainer,
            child: ListTile(
                leading: Icon(Icons.error_outline_rounded, color: cs.onErrorContainer),
                title: Text('${st['note']}', style: TextStyle(color: cs.onErrorContainer))),
          ),
        if (pending > 0 && !running && !uploading)
          Padding(
              padding: const EdgeInsets.only(top: 12),
              child: FilledButton.icon(onPressed: Recorder.upload, icon: const Icon(Icons.cloud_upload_rounded), label: const Text('立即上传'))),
        sectionCard(
            context,
            '服务端处理',
            srv == null
                ? Row(children: [
                    Icon(serverNote.startsWith('取不到') ? Icons.cloud_off_rounded : Icons.hourglass_empty_rounded,
                        color: serverNote.startsWith('取不到') ? cs.error : cs.outline),
                    const SizedBox(width: 8),
                    Expanded(child: Text(serverNote.isEmpty ? '加载中…' : serverNote)),
                  ])
                : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Row(children: [
                      Icon(caughtUp ? Icons.check_circle_rounded : Icons.autorenew_rounded,
                          color: caughtUp ? Colors.green.shade600 : cs.tertiary),
                      const SizedBox(width: 8),
                      Expanded(
                          child: Text(caughtUp ? '已追上，录音都已转写' : '还有 ${_span(left)} 录音待处理 · 排队 ${srv['queued']} 块',
                              style: t.textTheme.bodyLarge)),
                    ]),
                    if (!caughtUp) ...[
                      const SizedBox(height: 10),
                      const LinearProgressIndicator(minHeight: 6),
                      const SizedBox(height: 6),
                      Text(eta == null ? '' : '预计约 ${_span(eta)} 后完成', style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
                    ],
                    const Divider(height: 24),
                    ListTile(
                        contentPadding: EdgeInsets.zero,
                        dense: true,
                        leading: const Icon(Icons.download_done_rounded),
                        title: const Text('已收到'),
                        trailing: Text(_at(srv['received_to']))),
                    ListTile(
                        contentPadding: EdgeInsets.zero,
                        dense: true,
                        leading: const Icon(Icons.task_alt_rounded),
                        title: const Text('已定稿'),
                        trailing: Text(_at(srv['done_to']))),
                  ])),
      ]),
    );
  }
}

/// A titled block of a native page (recorder, upload).
Widget sectionCard(BuildContext context, String title, Widget child) {
  final t = Theme.of(context), cs = t.colorScheme;
  return Card.filled(
      margin: const EdgeInsets.only(top: 12),
      color: cs.surfaceContainerLow,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: t.textTheme.labelLarge?.copyWith(color: cs.onSurfaceVariant)),
          const SizedBox(height: 10),
          child,
        ]),
      ));
}

/// One number with its icon, a line under it and, given a fraction, a bar (battery, card, queue).
Widget statTile(BuildContext context,
    {required IconData icon, required Color color, required String label, required String value, String? sub, double? fraction}) {
  final t = Theme.of(context), cs = t.colorScheme;
  return Expanded(
    child: Card.filled(
      margin: EdgeInsets.zero,
      color: cs.surfaceContainerLow,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(icon, color: color, size: 22),
            const SizedBox(width: 6),
            Text(label, style: t.textTheme.labelLarge?.copyWith(color: cs.onSurfaceVariant)),
          ]),
          const SizedBox(height: 10),
          Text(value, style: t.textTheme.headlineSmall?.copyWith(fontWeight: FontWeight.w600)),
          if (sub != null) Text(sub, style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
          if (fraction != null) ...[
            const SizedBox(height: 10),
            LinearProgressIndicator(value: fraction, color: color, backgroundColor: color.withValues(alpha: 0.15),
                minHeight: 6, borderRadius: BorderRadius.circular(3)),
          ],
        ]),
      ),
    ),
  );
}

/// The header of a native page: an icon, a name, and chips saying what is going on.
Widget heroCard(BuildContext context, {required IconData icon, required String title, required List<Widget> chips}) {
  final t = Theme.of(context), cs = t.colorScheme;
  return Card.filled(
    margin: const EdgeInsets.only(top: 8),
    color: cs.primaryContainer,
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Row(children: [
        CircleAvatar(radius: 28, backgroundColor: cs.primary, child: Icon(icon, color: cs.onPrimary, size: 30)),
        const SizedBox(width: 16),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: t.textTheme.titleLarge?.copyWith(color: cs.onPrimaryContainer, fontWeight: FontWeight.w600)),
            const SizedBox(height: 6),
            Wrap(spacing: 8, runSpacing: 6, children: chips),
          ]),
        ),
      ]),
    ),
  );
}

/// A status chip in one colour.
Widget stateChip(String label, Color color, IconData icon) => Chip(
      avatar: Icon(icon, size: 18, color: color),
      label: Text(label),
      visualDensity: VisualDensity.compact,
      side: BorderSide(color: color.withValues(alpha: 0.5)),
    );

class _Live extends StatelessWidget {
  const _Live();
  @override
  Widget build(BuildContext context) => Row(mainAxisSize: MainAxisSize.min, children: [
        Container(width: 8, height: 8, decoration: const BoxDecoration(color: recordingRed, shape: BoxShape.circle)),
        const SizedBox(width: 6),
        const Text('REC', style: TextStyle(color: recordingRed, fontWeight: FontWeight.w600, fontSize: 13)),
      ]);
}

class _Empty extends StatelessWidget {
  final VoidCallback onSetup;
  const _Empty({required this.onSetup});
  @override
  Widget build(BuildContext context) => Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Icon(Icons.key_outlined, size: 40, color: Theme.of(context).colorScheme.outline),
          const SizedBox(height: 12),
          const Text('填好 Token 后显示字幕'),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: onSetup, child: const Text('去设置')),
        ]),
      );
}

class SettingsPage extends StatefulWidget {
  final Settings settings;
  final Future<void> Function() onSaved;
  const SettingsPage({super.key, required this.settings, required this.onSaved});
  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final server = TextEditingController(text: widget.settings.server),
      token = TextEditingController(text: widget.settings.token);
  late bool archive = widget.settings.archive;
  late String source = widget.settings.source;
  bool showToken = false;

  Future<void> _save() async {
    FocusScope.of(context).unfocus();
    final s = widget.settings;
    s.server = server.text.trim().replaceFirst(RegExp(r'/$'), '');
    s.token = token.text.trim();
    s.archive = archive;
    s.source = source;
    await widget.onSaved();
  }

  /// The web page's "连接手机" QR fills these two fields instead of typing them by hand, then saves the same way
  /// the 保存 button does.
  Future<void> _scan() async {
    final r = await Navigator.push<Map<String, String>>(
        context, MaterialPageRoute(builder: (_) => const QrScanPage()));
    if (r == null) return;
    server.text = r['server']!;
    token.text = r['token']!;
    await _save();
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    Widget section(String s) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 24, 4, 8),
        child: Text(s, style: t.textTheme.labelLarge?.copyWith(color: t.colorScheme.primary)));
    return ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 32), children: [
      section('服务器'),
      TextField(controller: server, keyboardType: TextInputType.url,
          decoration: const InputDecoration(labelText: '地址', border: OutlineInputBorder())),
      const SizedBox(height: 12),
      TextField(
        controller: token,
        obscureText: !showToken,
        decoration: InputDecoration(
          labelText: 'Token',
          border: const OutlineInputBorder(),
          suffixIcon: IconButton(
              icon: Icon(showToken ? Icons.visibility_off_outlined : Icons.visibility_outlined),
              onPressed: () => setState(() => showToken = !showToken)),
        ),
      ),
      const SizedBox(height: 12),
      OutlinedButton.icon(
          onPressed: _scan, icon: const Icon(Icons.qr_code_scanner_rounded), label: const Text('扫码登录')),
      const SizedBox(height: 12),
      InputDecorator(
          decoration: const InputDecoration(labelText: '设备名', helperText: '取自系统设置里的手机名称；BLE 录音硬件的音频按硬件名上传',
              border: OutlineInputBorder()),
          child: Text(widget.settings.device)),
      section('录音'),
      if (Platform.isAndroid)
        Padding(
          padding: const EdgeInsets.only(bottom: 12),
          child: SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'mic', icon: Icon(Icons.mic_none), label: Text('手机麦克风')),
              ButtonSegment(value: 'ble', icon: Icon(Icons.bluetooth), label: Text('BLE 录音硬件')),
            ],
            selected: {source},
            onSelectionChanged: (v) => setState(() => source = v.first),
          ),
        ),
      Card.outlined(
        margin: EdgeInsets.zero,
        child: Column(children: [
          SwitchListTile(
            title: const Text('本地保留录音'),
            subtitle: Text(Platform.isIOS
                ? '上传后存到「文件」App › 我的 iPhone › live-record › recordings › 日期'
                : '上传后存到 Android/data/com.suyiiyii.live_record/files/recordings/日期/'),
            value: archive,
            onChanged: (v) => setState(() => archive = v),
          ),
          const Divider(height: 1),
          FutureBuilder<Map<String, int>>(
            future: Slices.cached(),
            builder: (_, snap) {
              final s = snap.data ?? defaultSlices;
              return ListTile(
                title: const Text('切片长度（服务端下发）'),
                subtitle: Text(Platform.isIOS  // iOS has no reliable screen-off signal; background stands for it
                    ? '前台 ${s['foreground']} 秒 · 后台 ${s['background']} 秒'
                    : '前台 ${s['foreground']} 秒 · 后台 ${s['background']} 秒 · 熄屏 ${s['screenOff']} 秒'),
              );
            },
          ),
        ]),
      ),
      const SizedBox(height: 24),
      FilledButton(onPressed: _save, style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)), child: const Text('保存')),
    ]);
  }
}

/// Scans the web page's "连接手机" QR (`liverecord://setup?server=...&token=...`); pops with {'server', 'token'}
/// on a match. Other QR contents get a hint and the scanner stays open (repeated frames of the same one don't spam it).
class QrScanPage extends StatefulWidget {
  const QrScanPage({super.key});
  @override
  State<QrScanPage> createState() => _QrScanPageState();
}

class _QrScanPageState extends State<QrScanPage> {
  final controller = MobileScannerController();
  String? lastRaw;

  void _onDetect(BarcodeCapture capture) {
    if (!mounted) return;  // a stray detection after the page is already popping
    final raw = capture.barcodes.isEmpty ? null : capture.barcodes.first.rawValue;
    if (raw == null || raw == lastRaw) return;
    lastRaw = raw;
    final uri = Uri.tryParse(raw);
    if (uri == null || uri.scheme != 'liverecord' || uri.host != 'setup' || !uri.queryParameters.containsKey('server')) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('不是登录二维码，请在网页的「连接手机」里扫'), behavior: SnackBarBehavior.floating));
      return;
    }
    Navigator.pop(context, {'server': uri.queryParameters['server']!, 'token': uri.queryParameters['token'] ?? ''});
  }

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) =>
      Scaffold(appBar: AppBar(title: const Text('扫码登录')), body: MobileScanner(controller: controller, onDetect: _onDetect));
}

/// The BLE recorder (Android): which one is bound and, as last read over the link, its battery, card, offline sync
/// and firmware; the card as a USB disk on a computer, a firmware update when this build carries a newer one, unbinding.
class RecorderPage extends StatefulWidget {
  final bool visible;
  const RecorderPage({super.key, required this.visible});
  @override
  State<RecorderPage> createState() => _RecorderPageState();
}

class _RecorderPageState extends State<RecorderPage> {
  Map r = {}, st = {};
  Timer? timer;

  @override
  void initState() {
    super.initState();
    if (Platform.isAndroid) {
      _load();
      timer = Timer.periodic(const Duration(seconds: 2), (_) => widget.visible ? _load() : null);
    }
  }

  @override
  void dispose() {
    timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    final a = await Recorder.recorder(), b = await Recorder.status();
    if (mounted) setState(() { r = a; st = b; });
  }

  Future<void> _call(Future<void> Function() f) async {
    try {
      await f();
    } on PlatformException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message ?? '$e'), behavior: SnackBarBehavior.floating));
    }
    _load();
  }

  static String _size(num kb) => kb >= 1048576 ? '${(kb / 1048576).toStringAsFixed(2)} GB' : kb >= 1024 ? '${(kb / 1024).toStringAsFixed(1)} MB' : '$kb KB';

  static IconData _batteryIcon(int? p) {
    if (p == null) return Icons.battery_unknown_rounded;
    const icons = [Icons.battery_0_bar_rounded, Icons.battery_1_bar_rounded, Icons.battery_2_bar_rounded, Icons.battery_3_bar_rounded,
      Icons.battery_4_bar_rounded, Icons.battery_5_bar_rounded, Icons.battery_6_bar_rounded, Icons.battery_full_rounded];
    return icons[(p * 7 / 100).round().clamp(0, 7)];
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), cs = t.colorScheme;
    if (!Platform.isAndroid) return const Center(child: Text('BLE 录音硬件目前只支持 Android'));
    final name = r['name'] as String?, model = r['model'] as String?;
    final hw = (st['hw'] as Map?) ?? {};
    final fw = (hw['fw'] ?? r['fw']) as String?;
    final bundled = (r['bundled'] as Map?)?[model] as String?;
    // revisions are date-hash: a later date is newer; the same one is not an update
    final newer = bundled != null && (fw == null || bundled.compareTo(fw) > 0);
    final recording = st['running'] == true, linked = recording && hw.isNotEmpty;
    final link = '${st['link'] ?? ''}';
    final store = hw['store'] as int?, free = (hw['free_kb'] as num?) ?? 0, total = (hw['total_kb'] as num?) ?? 0;
    final battery = hw['battery'] as int?;
    final syncDone = (st['syncDone'] as num?) ?? 0, syncTotal = (st['syncTotal'] as num?) ?? 0;
    final syncing = recording && syncTotal > 0;
    final lent = store == -16;
    final dfu = (st['dfu'] ?? r['dfu']) as String?;

    // connection chip: what the link is doing, in one colour
    final (String chip, Color chipColor, IconData chipIcon) = !recording
        ? ('未连接', cs.outline, Icons.bluetooth_disabled_rounded)
        : syncing
            ? ('同步中', cs.tertiary, Icons.sync_rounded)
            : link.startsWith('已连接')
                ? ('已连接', Colors.green.shade600, Icons.bluetooth_connected_rounded)
                : ('连接中', cs.secondary, Icons.bluetooth_searching_rounded);

    Widget section(String title, Widget child) => sectionCard(context, title, child);
    Widget stat({required IconData icon, required Color color, required String label, required String value, String? sub, double? fraction}) =>
        statTile(context, icon: icon, color: color, label: label, value: value, sub: sub, fraction: fraction ?? 0);

    final batteryColor = battery == null ? cs.outline : battery <= 20 ? cs.error : battery <= 50 ? Colors.orange.shade700 : Colors.green.shade600;
    final cardColor = store == null || store == -16 ? cs.outline : store != 0 ? cs.error : free < total * 0.1 ? Colors.orange.shade700 : cs.primary;
    final (String cardValue, String? cardSub) = !linked
        ? ('—', null)
        : store == -19
            ? ('没有卡', '离线录音不可用')
            : store == -28
                ? ('已满', '离线录音已停止')
                : lent
                    ? ('U 盘中', '离线录音暂停')
                    : store != 0
                        ? ('出错', '错误 $store')
                        : (_size(free), '可用，共 ${_size(total)}');

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 32), children: [
        // the device
        heroCard(context, icon: Icons.mic_external_on_rounded, title: name ?? '未绑定', chips: [
          stateChip(chip, chipColor, chipIcon),
          if (model != null) Chip(label: Text(model), visualDensity: VisualDensity.compact),
        ]),
        if (name == null)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text('会连上看到的第一台录音设备，之后只连它', style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
          ),
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Text('录音设备开着就一直在录：手机在附近时实时上传，不在时存进它的卡里，回来后自动补传。要停止录音，关掉它的电源。',
              style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
        ),
        // battery and card
        const SizedBox(height: 12),
        IntrinsicHeight(
          child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            stat(icon: _batteryIcon(linked ? battery : null), color: batteryColor, label: '电量',
                value: linked && battery != null ? '$battery%' : '—', fraction: linked && battery != null ? battery / 100 : 0),
            const SizedBox(width: 12),
            stat(icon: Icons.sd_card_rounded, color: cardColor, label: '存储卡', value: cardValue, sub: cardSub,
                fraction: linked && total > 0 ? (total - free) / total : 0),
          ]),
        ),
        // offline sync
        section('离线录音同步', Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (syncing) ...[
            Row(children: [
              Icon(Icons.downloading_rounded, color: cs.tertiary),
              const SizedBox(width: 8),
              Expanded(child: Text('正在取回卡上的录音', style: t.textTheme.bodyLarge)),
              Text('${_size(syncDone)} / ${_size(syncTotal)}', style: t.textTheme.bodyMedium?.copyWith(color: cs.onSurfaceVariant)),
            ]),
            const SizedBox(height: 10),
            LinearProgressIndicator(value: syncTotal > 0 ? syncDone / syncTotal : null, minHeight: 8, borderRadius: BorderRadius.circular(4)),
            const SizedBox(height: 6),
            Text('取完再开始实时录音', style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
          ] else
            Row(children: [
              Icon(st['lastSync'] != null ? Icons.check_circle_rounded : Icons.cloud_sync_rounded,
                  color: st['lastSync'] != null ? Colors.green.shade600 : cs.outline),
              const SizedBox(width: 8),
              Expanded(child: Text((st['lastSync'] as String?) ?? '连上录音设备时自动取回它离线录下的音频')),
            ]),
        ])),
        // USB disk
        section('U 盘模式', Column(children: [
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            secondary: Icon(Icons.usb_rounded, color: lent ? cs.primary : cs.onSurfaceVariant),
            title: const Text('存储卡当 U 盘'),
            subtitle: Text(st['usbNote'] as String? ?? '录音设备插在电脑上时可用，约 20 秒后出现 OMI 盘；期间离线录音暂停'),
            value: lent,
            onChanged: linked ? (v) => _call(() => Recorder.usbDisk(v)) : null,
          ),
          if (lent)
            Row(children: [
              Icon(Icons.warning_amber_rounded, size: 18, color: Colors.orange.shade700),
              const SizedBox(width: 6),
              const Expanded(child: Text('切回前先在电脑上推出 OMI 盘')),
            ]),
        ])),
        // firmware and binding
        section('设备管理', Column(children: [
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.memory_rounded, color: cs.onSurfaceVariant),
            title: const Text('固件版本'),
            subtitle: Text([fw ?? '—', if (newer) '可升级到 $bundled', ?dfu].join('\n')),
            trailing: newer
                ? FilledButton.tonalIcon(
                    onPressed: () => _call(() => Recorder.withoutLink(Recorder.dfu)),
                    icon: const Icon(Icons.system_update_alt_rounded, size: 18),
                    label: const Text('升级'))
                : null,
          ),
          if (newer)
            Text('升级时先拔掉录音设备的 USB；升级期间暂停同步', style: t.textTheme.bodySmall?.copyWith(color: cs.onSurfaceVariant)),
          const Divider(height: 20),
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.phonelink_lock_rounded, color: cs.onSurfaceVariant),
            title: const Text('配对'),
            subtitle: const Text('新手机配对时，录音设备需接着 USB 电源，或刚重启 5 分钟内'),
          ),
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading: Icon(Icons.link_off_rounded, color: name != null ? cs.error : cs.outline),
            title: Text('解除绑定', style: TextStyle(color: name != null ? cs.error : cs.outline)),
            subtitle: const Text('之后连上看到的第一台录音设备'),
            onTap: name != null ? () => _call(() => Recorder.withoutLink(Recorder.unbind)) : null,
          ),
        ])),
      ]),
    );
  }
}
