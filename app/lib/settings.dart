// Persisted client configuration and server-managed recording slice lengths.
import 'dart:convert';
import 'dart:io';

import 'package:shared_preferences/shared_preferences.dart';

import 'recorder.dart';

// piece seconds per app state; the server's /api/client-config overrides these (kept from its last answer)
const defaultSlices = {'foreground': 1, 'background': 20, 'screenOff': 60};

class Settings {
  String server = '', token = '';  // your server: typed in 设置 or scanned from the web page's 连接手机 QR
  String device = 'phone';  // the phone's own name from the system settings, not set here (BLE audio goes up as the board's name)
  String source = 'mic';  // 'mic' or 'ble' (the BLE recorder)
  bool archive = true;
  static Future<Settings> load() async {
    final p = await SharedPreferences.getInstance(), s = Settings();
    s.server = p.getString('server') ?? s.server;
    s.token = p.getString('token') ?? '';
    s.device = (await Recorder.deviceName()).replaceAll(RegExp(r'[^\p{L}\p{N}._-]+', unicode: true), '_');
    s.archive = p.getBool('archive') ?? true;
    s.source = p.getString('source') ?? 'mic';
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

/// The server stops waiting for the older audio on a recorder's card (its hold) once this phone no longer has
/// the recorder: unbound, or another bound in its place. The backlog will not come from here; without this the server
/// waited 12 h after the phone last said the hold (10-07: LR-P3-116C, 17:47 to 05:47). Best effort.
Future<void> releaseHold(String name) async {
  try {
    final s = await Settings.load();
    if (s.token.isEmpty) return;
    final c = HttpClient()..connectionTimeout = const Duration(seconds: 10);
    final dev = Uri.encodeQueryComponent(name.replaceAll(RegExp(r'[^A-Za-z0-9._-]'), '_'));  // as its audio goes up
    final req = await c.postUrl(Uri.parse('${s.server}/api/hold/release?device=$dev'));
    req.headers.set('Authorization', 'Bearer ${s.token}');
    await (await req.close().timeout(const Duration(seconds: 15))).drain<void>();
    c.close();
  } catch (_) {}
}
