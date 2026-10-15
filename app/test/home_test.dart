import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:live_record/main.dart' as app;
import 'package:live_record/home.dart';
import 'package:live_record/settings_page.dart';
import 'package:live_record/upload_page.dart';
import 'package:live_record/recorder_page.dart';

void main() {
  testWidgets('application pages remain navigable and dispose their polling', (tester) async {
    SharedPreferences.setMockInitialValues({'server': 'http://127.0.0.1:1'});
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(const MethodChannel('live_record/recorder'), (call) async {
      if (call.method == 'deviceName') return 'Smoke phone';
      if (call.method == 'nearby') return <Object>[];
      if (call.method == 'status') return {'running': false, 'uploading': false, 'pending': 0};
      if (call.method == 'recorder' || call.method == 'permissions') return <String, Object>{};
      return null;
    });
    final handler = FlutterError.onError;
    final platformHandler = tester.binding.platformDispatcher.onError;
    app.main();
    await tester.pumpAndSettle();
    expect(find.byType(Home), findsOneWidget);
    expect(find.text('开始录音'), findsOneWidget);
    for (final entry in <String, Type>{'设置': SettingsPage, '上传': UploadPage, '录音设备': RecorderPage}.entries) {
      final scaffold = tester.state<ScaffoldState>(find.byType(Scaffold).first);
      scaffold.openDrawer();
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(NavigationDrawerDestination, entry.key));
      await tester.pumpAndSettle();
      expect(find.byType(entry.value), findsOneWidget);
    }
    await tester.pumpWidget(const SizedBox());
    await tester.pumpAndSettle();
    FlutterError.onError = handler;
    tester.binding.platformDispatcher.onError = platformHandler;
    expect(tester.takeException(), isNull);
  });
}
