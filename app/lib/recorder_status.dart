// Presentation of native recorder hardware and pause status.

/// What a BLE recorder runs on (its status' `charge`; none from an older firmware).
const chargeText = {'battery': '用电池', 'charging': '充电中', 'full': '已充满'};

/// A BLE recorder's card when it does not record to it: its status' store is an errno. An error with nothing mounted
/// this boot (total_kb 0) is no card or one not seated (-19; a mount that timed out, -116: LR-P3-2420 without one,
/// 10-07); -28 full; -16 lent as a USB disk; any other error a card that failed once mounted. tile: under 存储卡.
({String tile, String line, String sub})? cardState(Map hw) {
  final store = hw['store'] as int?, total = (hw['total_kb'] as num?) ?? 0;
  if (store == null || store == 0) return null;
  return switch (store) {
    -28 => (tile: '已满', line: '存储卡已满', sub: '离线录音已停止'),
    -16 => (tile: 'U 盘中', line: '存储卡在 U 盘模式', sub: '离线录音暂停'),
    _ when total == 0 => (tile: '没插卡', line: '没插存储卡', sub: '没插卡或没插好：只能实时上传，手机不在旁边时录不下来'),
    _ => (tile: '出错', line: '存储卡出错', sub: '重插或换一张卡；实时上传照常'),
  };
}

/// What a recorder's scan response says of its power and card, for the add-recorder list (after the
/// state byte, whose bits 3-4 are the charge, the battery mV / 20 and the card's errno); '' from an older firmware.
String advText(Map d) {
  final state = d['state'] as int, mv = d['mv'] as int? ?? -1, card = d['card'] as int? ?? -1;
  if (state < 0 || mv < 0) return '';
  final charge = ['battery', 'charging', 'full'].elementAtOrNull(state >> 3 & 3);
  return [
    if (charge == 'battery' && mv > 0) '电池 ${(mv / 1000).toStringAsFixed(2)} V' else ?chargeText[charge],
    switch (card) { 0 => '卡正常', 28 => '卡满', 16 => 'U 盘中', 19 || 116 => '没插卡', _ => '卡出错' },
  ].join(' · ');
}

/// A BLE recorder's battery and card in a few words.
String hwLine(Map? hw) {
  if (hw == null || hw.isEmpty) return '';
  final free = hw['free_kb'], total = hw['total_kb'];
  final card = cardState(hw)?.line ?? (total is num && total > 0 ? '卡剩 ${(free / 1048576).toStringAsFixed(1)} GB' : '');
  return [if (hw['battery'] != null) '电量 ${hw['battery']}%', ?chargeText[hw['charge']], if (card.isNotEmpty) card].join(' · ');
}

/// Minutes the recorder's pause has left (0: recording), from the link's status.
int pauseLeftMin(Map status) {
  final until = (status['pausedUntil'] as num?) ?? 0;
  final left = until - DateTime.now().millisecondsSinceEpoch;
  return left > 0 ? (left / 60000).ceil() : 0;
}
