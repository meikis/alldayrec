"""A broken stored import pauses without losing audio or hiding unrelated failures."""
import contextlib
import datetime as dt
import subprocess
import threading

import anyio
import numpy as np
import pytest

from alldayrec import audio, db, server

START = 1700000000000


def stored(tmp_path, body=b'not decodable audio', start=START):
    path = audio.T().recordings / f'{start}.wav'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    db.conn().execute('INSERT INTO pieces (device, start_ms, seconds, path, pos, size) '
                      'VALUES (?, ?, 3600, ?, 0, ?)', ('fixture', start, path.name, len(body)))
    return path


@pytest.mark.parametrize('failure', ['invalid', 'empty', 'timeout'])
def test_lasting_caps_decode_attempts_and_preserves_recording(tmp_path, monkeypatch, failure):
    t = db.open_db(tmp_path / 'live.db')
    path = stored(tmp_path)
    original_bytes = path.read_bytes()
    original_row = tuple(db.conn().execute('SELECT * FROM pieces').fetchone())
    resume = dt.datetime.fromtimestamp(START / 1000).isoformat()
    db.put_kv('finalized_to:fixture', resume)
    attempts, notes = [], []
    decode = audio.decode_process
    sleep = anyio.sleep

    def failing(args, output):
        attempts.append(1)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('ffmpeg', 300)
        return 0 if failure == 'empty' else decode(args, output)

    async def no_wait(seconds):
        await sleep(0)

    async def consume(device):
        stream = audio.stream(device, dt.datetime.fromisoformat(db.kv(f'finalized_to:{device}')))
        try:
            await anext(stream)
            pytest.fail('undecodable audio must not be skipped')
        finally:
            await stream.aclose()

    async def run():
        initial_attempts = len(attempts)
        done = anyio.Event()

        def note(text, level):
            notes.append(text)
            # Stop the old unbounded lasting() loop after observing five attempts.
            if '已暂停' in text or len(attempts) - initial_attempts >= 5:
                done.set()

        monkeypatch.setattr(audio.ws, 'note', note)
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.lasting, 'fixture')
                await done.wait()
                # Paused tasks remain cancellable at shutdown.
                tg.cancel_scope.cancel()

    monkeypatch.setattr(audio, 'decode_process', failing)
    monkeypatch.setattr(server, 'pipeline', consume)
    monkeypatch.setattr(anyio, 'sleep', no_wait)
    anyio.run(run)
    assert len(attempts) == audio.DECODE_ATTEMPTS == 3
    assert '已暂停' in notes[-1] and '重启服务器' in notes[-1]
    assert '重启处理任务' not in ''.join(notes)
    assert t.read_to['fixture'] < START
    assert db.kv('finalized_to:fixture') == resume
    assert tuple(db.conn().execute('SELECT * FROM pieces').fetchone()) == original_row
    assert path.read_bytes() == original_bytes
    # A new reader (as on server restart) gets a fresh budget from the same
    # finalized position; the bad file has neither moved nor been acknowledged.
    anyio.run(run)
    assert len(attempts) == 2 * audio.DECODE_ATTEMPTS
    assert db.kv('finalized_to:fixture') == resume
    assert tuple(db.conn().execute('SELECT * FROM pieces').fetchone()) == original_row
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize('failure', [ValueError('bug'), RuntimeError('bug'), OSError('storage failed')])
@pytest.mark.parametrize('stage', ['enter', 'block'])
def test_unrelated_errors_propagate(tmp_path, monkeypatch, failure, stage):
    db.open_db(tmp_path / 'live.db')
    stored(tmp_path)
    closed, notes = [], []

    @contextlib.contextmanager
    def broken(*args):
        try:
            if stage == 'enter':
                raise failure
            def blocks():
                yield np.ones(audio.SR, np.int16)
                raise failure
            yield blocks(), 0
        finally:
            closed.append(True)

    monkeypatch.setattr(audio, 'piece_blocks', broken)
    monkeypatch.setattr(audio.ws, 'note', lambda *args: notes.append(args))

    async def run():
        stream = audio.stream('fixture', None)
        try:
            if stage == 'block':
                await anext(stream)
            with pytest.raises(type(failure), match=str(failure)):
                await anext(stream)
        finally:
            await stream.aclose()
    anyio.run(run)
    assert closed == [True]
    assert not notes


def test_cancel_during_retry_and_close_after_block(tmp_path, monkeypatch):
    db.open_db(tmp_path / 'live.db')
    stored(tmp_path)
    attempts, closed = [], []

    @contextlib.contextmanager
    def timeout(*args):
        attempts.append(1)
        try:
            raise subprocess.TimeoutExpired('ffmpeg', 300)
            yield
        finally:
            closed.append(True)

    async def run():
        retrying = anyio.Event()
        async def wait(seconds):
            assert seconds == 60
            retrying.set()
            await anyio.sleep_forever()
        monkeypatch.setattr(anyio, 'sleep', wait)
        monkeypatch.setattr(audio, 'piece_blocks', timeout)
        stream = audio.stream('fixture', None)
        async with anyio.create_task_group() as tg:
            tg.start_soon(anext, stream)
            await retrying.wait()
            tg.cancel_scope.cancel()
        await stream.aclose()
        assert len(attempts) == 1

        @contextlib.contextmanager
        def success(*args):
            try:
                yield iter([np.ones(audio.SR, np.int16)]), 0
            finally:
                closed.append(True)
        monkeypatch.setattr(audio, 'piece_blocks', success)
        stream = audio.stream('fixture', None)
        await anext(stream)
        await stream.aclose()
    anyio.run(run)
    assert closed == [True, True]
    assert audio.T().read_to['fixture'] < START


def test_cancellation_during_decode_closes_context(tmp_path, monkeypatch):
    db.open_db(tmp_path / 'live.db')
    stored(tmp_path)
    entered, release = threading.Event(), threading.Event()
    closed, notes = [], []

    @contextlib.contextmanager
    def decoding(*args):
        entered.set()
        assert release.wait(5)
        try:
            yield iter([np.ones(audio.SR, np.int16)]), 0
        finally:
            closed.append(True)

    monkeypatch.setattr(audio, 'piece_blocks', decoding)
    monkeypatch.setattr(audio.ws, 'note', lambda *args: notes.append(args))

    async def run():
        stream = audio.stream('fixture', None)
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(anext, stream)
                assert await anyio.to_thread.run_sync(lambda: entered.wait(5))
                tg.cancel_scope.cancel()
                release.set()
        finally:
            release.set()
            await stream.aclose()
    anyio.run(run)
    assert closed == [True]
    assert not notes
    assert audio.T().read_to['fixture'] < START


def test_success_resets_budget_and_continues_in_order(tmp_path, monkeypatch):
    db.open_db(tmp_path / 'live.db')
    x = np.arange(audio.SR, dtype=np.int16)
    recordings = [audio.wav(x), audio.wav(x), audio.wav(x[:audio.SR // 4])]
    paths = [stored(tmp_path, body, START + offset) for offset, body in zip((0, 1000, 2000), recordings)]
    db.conn().execute('UPDATE pieces SET seconds = 1')
    # Exercise two long pieces then the existing short-piece path with tiny PCM.
    db.conn().execute('UPDATE pieces SET seconds = .25 WHERE start_ms = ?', (START + 2000,))
    monkeypatch.setattr(audio, 'WHOLE_S', .5)
    decode = audio.decode_process
    sleep = anyio.sleep
    attempts, notes = [], []

    def transient(args, output):
        attempts.append(1)
        if len(attempts) in (1, 2, 4, 5):
            return 1
        return decode(args, output)

    async def no_wait(seconds):
        assert seconds == 60
        await sleep(0)

    monkeypatch.setattr(audio, 'decode_process', transient)
    monkeypatch.setattr(anyio, 'sleep', no_wait)
    monkeypatch.setattr(audio.ws, 'note', lambda text, level: notes.append(text))

    async def run():
        # Resume half way through the first import, preserving previous progress.
        stream = audio.stream('fixture', dt.datetime.fromtimestamp(START / 1000 + .5))
        try:
            for offset, expected in ((.5, x[audio.SR // 2:]), (1, x), (2, x[:audio.SR // 4])):
                at, actual = await anext(stream)
                assert at == dt.datetime.fromtimestamp(START / 1000 + offset)
                np.testing.assert_array_equal(actual, expected)
        finally:
            await stream.aclose()
    anyio.run(run)
    assert len(attempts) == 6
    assert len(notes) == 4 and all('后重试' in text for text in notes)
    assert audio.T().read_to['fixture'] == START + 1000
    assert [path.read_bytes() for path in paths] == recordings
