// errorSummary: an error nothing caught goes into the device log as its type and first frames, never its message.
import 'package:flutter_test/flutter_test.dart';
import 'package:live_record/recorder.dart';

void main() {
  test('type and frames, no message', () {
    final s = StackTrace.fromString('#0      _HomeState._tick (package:live_record/home.dart:210:7)\n'
        '<asynchronous suspension>\n#1      Timer._run (dart:async/timer.dart:5:1)\n');
    final (type, frames) = errorSummary(StateError('secret'), s);
    expect(type, 'StateError');
    expect(frames, ['_HomeState._tick (package:live_record/home.dart:210:7)', 'Timer._run (dart:async/timer.dart:5:1)']);
    expect(errorSummary(StateError('x'), s, n: 1).$2.length, 1);
    expect(errorSummary(StateError('x'), null).$2, isEmpty);
  });
}
