// live-record Android client. RecorderService.kt (native, START_STICKY) keeps the microphone open,
// cuts the Opus stream into Ogg pieces whose length follows the app state, uploads them in order and
// survives the process being killed. This side is settings, status and the transcript page in a WebView.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';

const sliceForegroundS = 5, sliceBackgroundS = 20, sliceScreenOffS = 60;
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
  String server = 'https://lr.example.com', token = '', device = 'phone';
  bool archive = true;
  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance(), s = Settings();
    s.server = p.getString('server') ?? s.server;
    s.token = p.getString('token') ?? '';
    s.device = p.getString('device') ?? s.device;
    s.archive = p.getBool('archive') ?? true;
    return s;
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('server', server);
    await p.setString('token', token);
    await p.setString('device', device);
    await p.setBool('archive', archive);
  }
}

class Recorder {
  static const _ch = MethodChannel('live_record/recorder');
  static Future<void> start(Settings s) => _ch.invokeMethod('start', {
        'server': s.server, 'token': s.token, 'device': s.device, 'archive': s.archive,
        'foreground': sliceForegroundS, 'background': sliceBackgroundS, 'screenOff': sliceScreenOffS});
  static Future<void> stop() => _ch.invokeMethod('stop');
  static Future<void> foreground(bool on) => _ch.invokeMethod('foreground', {'on': on});
  static Future<Map> status() async => (await _ch.invokeMethod('status')) as Map;
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
  int page = 0;  // 0 字幕, 1 对话, 2 设置
  WebViewController? web, convWeb;  // the live page keeps its scroll while the history is browsed
  Timer? ticker;

  static const titles = ['字幕', '对话', '设置'];

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Settings.load().then((s) => setState(() {
          settings = s;
          _loadWeb();
        }));
    ticker = Timer.periodic(const Duration(seconds: 2), (_) async {  // the service may be running from before this UI opened
      final s = await Recorder.status();
      if (mounted) setState(() => status = s);
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) => Recorder.foreground(state == AppLifecycleState.resumed);

  bool get running => status['running'] == true;

  void _loadWeb() {
    final s = settings!;
    if (s.token.isEmpty) return;
    WebViewController make() => WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..clearCache();  // the page changes with server deploys
    web ??= make();
    convWeb ??= make();
    web!.loadRequest(Uri.parse('${s.server}/?token=${s.token}'));  // sets the cookie, then the page follows itself
    convWeb!.loadRequest(Uri.parse('${s.server}/conversations?token=${s.token}'));
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
    final w = [web, convWeb, null][page];
    if (w != null && await w.canGoBack()) return w.goBack();
    if (page != 0) return setState(() => page = 0);
    SystemNavigator.pop();
  }

  Future<void> _start() async {
    if (!await AudioRecorder().hasPermission()) return setState(() => note = '没有麦克风权限');
    note = '';
    await Recorder.start(settings!);
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
        actions: [if (running) const Padding(padding: EdgeInsets.only(right: 16), child: _Live())],
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
              icon: Icon(Icons.settings_outlined), selectedIcon: Icon(Icons.settings), label: Text('设置')),
        ],
      ),
      body: settings == null
          ? const Center(child: CircularProgressIndicator())
          : IndexedStack(index: page, children: [  // keeps the WebView alive across pages
              web == null
                  ? _Empty(onSetup: () => setState(() => page = 2))
                  : WebViewWidget(controller: web!),
              convWeb == null
                  ? _Empty(onSetup: () => setState(() => page = 2))
                  : WebViewWidget(controller: convWeb!),
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
      detail = '已传 ${s['sent']} · 待传 ${s['pending']}';
    } else {
      headline = note.isEmpty ? '未在录音' : note;
    }
    final warn = [
      if (s['error'] != null) '录音错误 ${s['error']}',
      if (running && s['note'] != null) '${s['note']}',
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
      token = TextEditingController(text: widget.settings.token),
      device = TextEditingController(text: widget.settings.device);
  late bool archive = widget.settings.archive;
  bool showToken = false;

  Future<void> _save() async {
    FocusScope.of(context).unfocus();
    final s = widget.settings;
    s.server = server.text.trim().replaceFirst(RegExp(r'/$'), '');
    s.token = token.text.trim();
    s.device = device.text.trim().isEmpty ? 'phone' : device.text.trim();
    s.archive = archive;
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
      TextField(controller: device,
          decoration: const InputDecoration(labelText: '设备名', helperText: '服务端按设备名区分录音来源', border: OutlineInputBorder())),
      section('录音'),
      Card.outlined(
        margin: EdgeInsets.zero,
        child: Column(children: [
          SwitchListTile(
            title: const Text('本地保留录音'),
            subtitle: const Text('上传后存到 Android/data/com.suyiiyii.live_record/files/recordings/日期/'),
            value: archive,
            onChanged: (v) => setState(() => archive = v),
          ),
          const Divider(height: 1),
          const ListTile(
            title: Text('切片长度'),
            subtitle: Text('前台 $sliceForegroundS 秒 · 后台 $sliceBackgroundS 秒 · 熄屏 $sliceScreenOffS 秒'),
          ),
        ]),
      ),
      const SizedBox(height: 24),
      FilledButton(onPressed: _save, style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)), child: const Text('保存')),
    ]);
  }
}
