// live-record client. Native code keeps the microphone open, cuts the stream into pieces whose length follows
// the app state and uploads them in order: on Android RecorderService.kt (Opus/Ogg, START_STICKY, survives a kill),
// on iOS Recorder.swift (ADTS AAC, `audio` background mode). This side is settings, status and the pages in a WebView.
import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
  int page = 0;  // 0 字幕, 1 对话, 2 说话人, 3 上传, 4 设置
  final webs = <WebViewController?>[null, null, null];  // one per web page: each keeps its scroll while another is shown
  Timer? ticker;
  int ticks = 0;

  static const titles = ['字幕', '对话', '说话人', '上传', '设置'];
  static const paths = ['/', '/conversations', '/speakers'];
  static const uploadPage = 3, settingsPage = 4;

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
    });
  }

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
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(running ? '已保存，下次开始录音时生效' : '已保存'), behavior: SnackBarBehavior.floating));
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
              icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings), label: Text('设置')),
        ],
      ),
      body: settings == null
          ? const Center(child: CircularProgressIndicator())
          : IndexedStack(index: page, children: [  // keeps the WebView alive across pages
              for (final w in webs)
                w == null ? _Empty(onSetup: () => setState(() => page = settingsPage)) : WebViewWidget(controller: w),
              UploadPage(settings: settings!, status: status, visible: page == uploadPage),
              SettingsPage(settings: settings!, onSaved: _saved),
            ]),
      bottomNavigationBar: page == 0 && settings != null ? _controlBar(cs) : null,
    ));
  }

  Widget _controlBar(ColorScheme cs) {
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
  String serverNote = '';
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
      final dev = ble?.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_') ?? s.device;
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

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context), st = widget.status, srv = server;
    final pending = (st['pending'] ?? 0) as int, bytes = (st['pendingBytes'] ?? 0) as num;
    final state = st['running'] == true ? '录音中，边录边传' : st['uploading'] == true ? '正在上传' : pending > 0 ? '等待上传' : '空闲，已全部上传';
    Widget row(String k, String v, {bool bad = false}) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 88, child: Text(k, style: TextStyle(color: t.colorScheme.onSurfaceVariant))),
          Expanded(child: Text(v, style: bad ? TextStyle(color: t.colorScheme.error) : null)),
        ]));
    Widget card(String title, List<Widget> rows) => Card.outlined(
        margin: const EdgeInsets.only(top: 12),
        child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: t.textTheme.titleSmall),
              const SizedBox(height: 6),
              ...rows,
            ])));
    final left = (srv?['left_s'] ?? 0) as num, eta = srv?['eta_s'] as num?;
    return RefreshIndicator(
      onRefresh: _fetch,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 32), children: [
        card('本机（${st['tag'] ?? widget.settings.device}）', [
          row('状态', state),
          row('待传', pending == 0 ? '无' : '$pending 块 · ${bytes < 1048576 ? '${(bytes / 1024).round()} KB' : '${(bytes / 1048576).toStringAsFixed(1)} MB'}'),
          row('最早一块', pending == 0 ? '—' : '录于 ${_at(st['oldest'])}'),
          row('本次已传', '${st['sent'] ?? 0} 块'),
          if (st['note'] != null) row('提示', '${st['note']}', bad: true),
        ]),
        if (pending > 0 && st['running'] != true && st['uploading'] != true)
          Padding(
              padding: const EdgeInsets.only(top: 12),
              child: FilledButton.icon(onPressed: Recorder.upload, icon: const Icon(Icons.cloud_upload), label: const Text('立即上传'))),
        card('服务端处理', [
          if (srv == null) row('状态', serverNote.isEmpty ? '加载中…' : serverNote, bad: serverNote.startsWith('取不到'))
          else ...[
            row('已收到', _at(srv['received_to'])),
            row('已定稿', _at(srv['done_to'])),
            row('待处理', left < 30 ? '已追上' : '${_span(left)} 录音 · 排队 ${srv['queued']} 块'),
            if (left >= 30) row('预计完成', eta == null ? '—' : '约 ${_span(eta)}'),
          ],
        ]),
      ]),
    );
  }
}

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
      if (Platform.isAndroid && source == 'ble') const Padding(padding: EdgeInsets.only(bottom: 12), child: RecorderCard()),
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

/// The bound BLE recorder: name, firmware, battery and card as last read, unbinding, and a firmware update when this
/// build carries a newer one for its board.
class RecorderCard extends StatefulWidget {
  const RecorderCard({super.key});
  @override
  State<RecorderCard> createState() => _RecorderCardState();
}

class _RecorderCardState extends State<RecorderCard> {
  Map r = {}, st = {};
  Timer? timer;

  @override
  void initState() {
    super.initState();
    _load();
    timer = Timer.periodic(const Duration(seconds: 2), (_) => _load());
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

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    final name = r['name'] as String?, model = r['model'] as String?, fw = r['fw'] as String?;
    final bundled = (r['bundled'] as Map?)?[model] as String?;
    final recording = st['running'] == true;
    final hw = hwLine(st['hw'] as Map?);
    final dfu = (st['dfu'] ?? r['dfu']) as String?;
    return Card.outlined(
      margin: EdgeInsets.zero,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('录音硬件', style: t.textTheme.titleSmall),
          const SizedBox(height: 6),
          Text(name == null ? '未绑定：开始录音时连上看到的第一台，之后只连它' : '已绑定 $name${model != null ? '（$model）' : ''}'),
          if (fw != null) Text('固件 $fw${bundled != null && bundled != fw ? '，可升级到 $bundled' : ''}', style: TextStyle(color: t.colorScheme.onSurfaceVariant)),
          if (recording && st['link'] != null) Text('${st['link']}${hw.isNotEmpty ? ' · $hw' : ''}', style: TextStyle(color: t.colorScheme.onSurfaceVariant)),
          if (dfu != null) Text(dfu, style: TextStyle(color: t.colorScheme.primary)),
          const Text('新手机配对时，录音设备需接着 USB 电源或刚重启 5 分钟内', style: TextStyle(fontSize: 12)),
          Row(children: [
            if (name != null)
              TextButton(onPressed: recording ? null : () => _call(Recorder.unbind), child: const Text('解除绑定')),
            if (bundled != null && bundled != fw)
              TextButton(onPressed: recording ? null : () => _call(Recorder.dfu), child: const Text('升级固件')),
          ]),
        ]),
      ),
    );
  }
}
