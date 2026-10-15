// advText: a recorder's power and card from its scan response (firmware #155 F4), as the add-recorder list shows them.
import 'package:flutter_test/flutter_test.dart';
import 'package:live_record/recorder_status.dart';

void main() {
  test('power and card from the scan response', () {
    expect(advText({'state': 1, 'mv': 3840, 'card': 0}), '电池 3.84 V · 卡正常');
    expect(advText({'state': 1 | 1 << 3, 'mv': 4300, 'card': 19}), '充电中 · 没插卡');  // on USB the mV is the cable's
    expect(advText({'state': 2 << 3, 'mv': 4200, 'card': 28}), '已充满 · 卡满');
    expect(advText({'state': 0, 'mv': 3700, 'card': 5}), '电池 3.70 V · 卡出错');
    expect(advText({'state': 0, 'mv': 0, 'card': 16}), '用电池 · U 盘中');  // battery not read yet
  });

  test('nothing from an older firmware or no scan response yet', () {
    expect(advText({'state': 1, 'mv': -1, 'card': -1}), '');
    expect(advText({'state': -1, 'mv': -1, 'card': -1}), '');
    expect(advText({'state': 1}), '');  // a native list without the fields
  });
}
