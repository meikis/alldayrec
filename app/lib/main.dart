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

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(home: Home(), debugShowCheckedModeBanner: false));
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
  WebViewController? web;
  Timer? ticker;
  final serverCtl = TextEditingController(), tokenCtl = TextEditingController(), deviceCtl = TextEditingController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    Settings.load().then((s) => setState(() {
          settings = s;
          serverCtl.text = s.server;
          tokenCtl.text = s.token;
          deviceCtl.text = s.device;
          _loadWeb();
        }));
    ticker = Timer.periodic(const Duration(seconds: 2), (_) async {  // the service may be running from before this UI opened
      final s = await Recorder.status();
      if (mounted) setState(() => status = s);
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) => Recorder.foreground(state == AppLifecycleState.resumed);

  void _loadWeb() {
    final s = settings!;
    if (s.token.isEmpty) return;
    web ??= WebViewController()..setJavaScriptMode(JavaScriptMode.unrestricted);
    web!.loadRequest(Uri.parse('${s.server}/?token=${s.token}'));  // sets the cookie, then the page follows itself
    setState(() {});
  }

  Future<void> _save() async {
    final s = settings!;
    s.server = serverCtl.text.trim().replaceFirst(RegExp(r'/$'), '');
    s.token = tokenCtl.text.trim();
    s.device = deviceCtl.text.trim().isEmpty ? 'phone' : deviceCtl.text.trim();
    await s.save();
    _loadWeb();
  }

  Future<void> _start() async {
    await _save();
    if (!await AudioRecorder().hasPermission()) return setState(() => note = '没有麦克风权限');
    note = '';
    await Recorder.start(settings!);
  }

  Future<void> _stop() async {
    setState(() => note = '停止中，等最后几块传完');
    await Recorder.stop();
    while ((await Recorder.status())['running'] == true) {
      await Future.delayed(const Duration(milliseconds: 500));
    }
    setState(() => note = '已停止');
  }

  String get line {
    if (status['running'] != true) return note.isEmpty ? '未开始' : note;
    final hhmm = status['interruptedAt'] == null
        ? ''
        : ' · ${TimeOfDay.fromDateTime(DateTime.fromMillisecondsSinceEpoch(status['interruptedAt'])).format(context)} 被系统中断过，已恢复';
    return '录音中 · 切片 ${status['slice']} 秒 · 已切 ${status['files']} 已传 ${status['sent']} 待传 ${status['pending']}'
        '${status['error'] != null ? ' · 录音错误 ${status['error']}' : ''}${status['note'] != null ? ' · ${status['note']}' : ''}$hhmm';
  }

  @override
  Widget build(BuildContext context) {
    final on = status['running'] == true;
    return Scaffold(
      body: SafeArea(
        child: Column(children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
            child: Column(children: [
              ExpansionTile(
                title: const Text('设置', style: TextStyle(fontSize: 14)),
                tilePadding: EdgeInsets.zero,
                children: [
                  TextField(controller: serverCtl, decoration: const InputDecoration(labelText: '服务器')),
                  TextField(controller: tokenCtl, decoration: const InputDecoration(labelText: 'Token'), obscureText: true),
                  TextField(controller: deviceCtl, decoration: const InputDecoration(labelText: '设备名')),
                  SwitchListTile(
                    title: const Text('本地保留录音', style: TextStyle(fontSize: 14)),
                    subtitle: const Text('Android/data/com.suyiiyii.live_record/files/recordings/日期/', style: TextStyle(fontSize: 11)),
                    value: settings?.archive ?? true,
                    onChanged: (v) => setState(() => settings?.archive = v),
                    contentPadding: EdgeInsets.zero,
                  ),
                  TextButton(onPressed: _save, child: const Text('保存')),
                ],
              ),
              Row(children: [
                FilledButton(
                  style: FilledButton.styleFrom(backgroundColor: on ? const Color(0xffc0392b) : const Color(0xff3b4cca)),
                  onPressed: settings == null ? null : (on ? _stop : _start),
                  child: Text(on ? '停止' : '开始录音'),
                ),
                const SizedBox(width: 12),
                Expanded(child: Text(line, style: const TextStyle(fontSize: 12, color: Colors.black54))),
              ]),
            ]),
          ),
          Expanded(child: web == null ? const Center(child: Text('填好 Token 后显示字幕')) : WebViewWidget(controller: web!)),
        ]),
      ),
    );
  }
}
