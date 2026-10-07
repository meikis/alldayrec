// apkOffer: the update banner shows the server's APK only when its build number is higher than this build's and than
// the one whose offer was closed.
import 'package:flutter_test/flutter_test.dart';
import 'package:live_record/update.dart';

Map apk(int code) => {'versionName': '1.4.$code', 'versionCode': code, 'size': 71311007};

void main() {
  test('newer than this build: offered', () {
    expect(apkOffer(apk(18), 17, 0), apk(18));
  });
  test('the same or an older build: nothing', () {
    expect(apkOffer(apk(17), 17, 0), isNull);
    expect(apkOffer(apk(16), 17, 0), isNull);
  });
  test('closed for that version: not again, a later one is', () {
    expect(apkOffer(apk(18), 17, 18), isNull);
    expect(apkOffer(apk(19), 17, 18), apk(19));
  });
  test('anything else from the server: nothing', () {
    expect(apkOffer(null, 17, 0), isNull);
    expect(apkOffer({'detail': 'no apk uploaded'}, 17, 0), isNull);
    expect(apkOffer({'versionCode': '18'}, 17, 0), isNull);
  });
}
