// live-record Android client. The native side (MainActivity.kt) keeps the microphone open and cuts the
// continuous Opus stream into Ogg files whose length follows the app state; this side uploads them in
// order to the server's /api/chunks and shows the transcript page in a WebView.
import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:webview_flutter/webview_flutter.dart';

const sliceForegroundS = 5, sliceBackgroundS = 20, sliceScreenOffS = 60;

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  FlutterForegroundTask.initCommunicationPort();
  runApp(const MaterialApp(home: Home(), debugShowCheckedModeBanner: false));
}

class Settings {
  String server = 'https://lr.example.com', token = '', device = 'phone';
  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance(), s = Settings();
    s.server = p.getString('server') ?? s.server;
    s.token = p.getString('token') ?? '';
    s.device = p.getString('device') ?? s.device;
    return s;
  }

  Future<void> save() async {
    final p = await SharedPreferences.getInstance();
    await p.setString('server', server);
    await p.setString('token', token);
    await p.setString('device', device);
  }
}

/// The native slicer: files named <start ms>.ogg appear in `dir`; .part is the one being written.
class Recorder {
  static const _ch = MethodChannel('live_record/recorder');
  static Future<void> start(String dir) => _ch.invokeMethod('start',
      {'dir': dir, 'foreground': sliceForegroundS, 'background': sliceBackgroundS, 'screenOff': sliceScreenOffS});
  static Future<void> stop() => _ch.invokeMethod('stop');
  static Future<void> foreground(bool on) => _ch.invokeMethod('foreground', {'on': on});
  static Future<Map?> status() async => (await _ch.invokeMethod('status')) as Map?;
}

/// Uploads the .ogg files in start order, one at a time, retrying forever; a file is deleted once
/// the server has it, so the directory is the offline queue.
class Uploader {
  final Directory dir;
  final Settings settings;
  final void Function(String) onStatus;
  bool running = false;
  int sent = 0, fails = 0;
  Uploader(this.dir, this.settings, this.onStatus);

  List<File> get pending =>
      dir.listSync().whereType<File>().where((f) => f.path.endsWith('.ogg')).toList()..sort((a, b) => a.path.compareTo(b.path));

  Future<void> run() async {
    running = true;
    while (running) {
      final files = pending;
      if (files.isEmpty) {
        await Future.delayed(const Duration(seconds: 1));
        continue;
      }
      final f = files.first, start = f.uri.pathSegments.last.split('.').first;
      try {
        final r = await http
            .post(Uri.parse('${settings.server}/api/chunks?device=${Uri.encodeComponent(settings.device)}&start=$start'),
                headers: {'Authorization': 'Bearer ${settings.token}', 'Content-Type': 'audio/ogg'}, body: await f.readAsBytes())
            .timeout(const Duration(seconds: 60));
        if (r.statusCode == 200) {
          await f.delete();
          sent++;
          fails = 0;
        } else if (r.statusCode == 429) {
          onStatus('服务端积压中，20 秒后继续');  // catching up after an outage: keep the file, wait
          await Future.delayed(const Duration(seconds: 20));
          continue;
        } else if (r.statusCode == 400) {
          await f.delete();  // undecodable piece (e.g. an empty one from a crash): nothing to retry
          onStatus('服务端无法解码一块，已丢弃');
        } else {
          throw Exception('HTTP ${r.statusCode} ${r.body}');
        }
      } catch (e) {
        fails++;
        onStatus('上传失败 $e');
        await Future.delayed(Duration(seconds: 5 * (fails > 6 ? 6 : fails)));
      }
    }
  }
}

class Home extends StatefulWidget {
  const Home({super.key});
  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> with WidgetsBindingObserver {
  Settings? settings;
  bool on = false;
  Uploader? uploader;
  Timer? ticker;
  String status = '未开始', note = '';
  WebViewController? web;
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
    FlutterForegroundTask.init(
      androidNotificationOptions: AndroidNotificationOptions(channelId: 'live_record', channelName: '录音中'),
      iosNotificationOptions: const IOSNotificationOptions(),
      foregroundTaskOptions: ForegroundTaskOptions(eventAction: ForegroundTaskEventAction.nothing()),
    );
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (on) Recorder.foreground(state == AppLifecycleState.resumed);
  }

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
    if (!await AudioRecorder().hasPermission()) return _note('没有麦克风权限');
    await FlutterForegroundTask.requestNotificationPermission();
    final dir = Directory('${(await getApplicationDocumentsDirectory()).path}/chunks')..createSync(recursive: true);
    await FlutterForegroundTask.startService(
        serviceTypes: [ForegroundServiceTypes.microphone], notificationTitle: 'live-record 正在录音', notificationText: '');
    await Recorder.start(dir.path);
    uploader = Uploader(dir, settings!, _note);
    unawaited(uploader!.run());
    setState(() => on = true);
    ticker = Timer.periodic(const Duration(seconds: 2), (_) async {
      final s = await Recorder.status();
      if (!mounted) return;
      setState(() => status = s == null
          ? '录音中'
          : '录音中 · 切片 ${s['slice']} 秒 · 已切 ${s['files']} 已传 ${uploader?.sent} 待传 ${uploader?.pending.length}${s['error'] != null ? ' · 错误 ${s['error']}' : ''}');
    });
  }

  Future<void> _stop() async {
    final at = DateTime.now().millisecondsSinceEpoch, s = settings!;
    ticker?.cancel();
    await Recorder.stop();
    setState(() => status = '停止，等最后几块传完');
    final t0 = DateTime.now();
    while (uploader != null && uploader!.pending.isNotEmpty && DateTime.now().difference(t0).inSeconds < 120) {
      await Future.delayed(const Duration(milliseconds: 500));
    }
    uploader?.running = false;
    try {  // tells the server the conversation can close now instead of after its idle timeout
      await http.post(Uri.parse('${s.server}/api/chunks/end?device=${Uri.encodeComponent(s.device)}&t=$at'),
          headers: {'Authorization': 'Bearer ${s.token}'}).timeout(const Duration(seconds: 15));
    } catch (e) {
      _note('结束信号失败 $e');
    }
    await FlutterForegroundTask.stopService();
    final left = uploader?.pending.length ?? 0;
    setState(() {
      on = false;
      status = left == 0 ? '已停止' : '已停止，剩余 $left 块会在下次开始时继续传';
    });
  }

  void _note(String s) => mounted ? setState(() => note = s) : null;

  @override
  Widget build(BuildContext context) {
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
                Expanded(child: Text('$status${note.isEmpty ? '' : '\n$note'}', style: const TextStyle(fontSize: 12, color: Colors.black54))),
              ]),
            ]),
          ),
          Expanded(child: web == null ? const Center(child: Text('填好 Token 后显示字幕')) : WebViewWidget(controller: web!)),
        ]),
      ),
    );
  }
}
