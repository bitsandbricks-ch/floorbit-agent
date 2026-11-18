"""The disk buffer (floorbit#465): syslog that can't be sent goes to disk, survives a restart, and is
sent oldest first behind the live lines once the hosted Floorbit is back."""
import json
import os
import threading
import time

import pytest

from floorbit_agent import syslog_relay
from floorbit_agent.spool import Spool


class Hosted:
    """Takes syslog batches, or fails while it is away."""

    def __init__(self):
        self.away = False
        self.turned_off = False
        self.batches = []
        self.lock = threading.Lock()

    def send(self, lines, dropped):
        if self.turned_off:
            raise syslog_relay.TurnedOff('Syslog is turned off')
        if self.away:
            raise ConnectionError('hosted Floorbit away')
        with self.lock:
            self.batches.append([line['raw'] for line in lines])

    @property
    def got(self):
        return [raw for batch in self.batches for raw in batch]


def relay(hosted, tmp_path, **kw):
    spool = Spool(str(tmp_path / 'syslog'), log=lambda *_: None)
    return syslog_relay.Relay(hosted.send, 0, log=lambda *_: None, spool=spool, **kw)


def test_lines_get_a_stable_id_whichever_agent_heard_them():
    a, b = syslog_relay.Relay(None, 0), syslog_relay.Relay(None, 0)
    a.receive(b'<14>%LINK-3-UPDOWN: Gi1/0/1 up', '192.0.2.21', now=100)
    b.receive(b'<14>%LINK-3-UPDOWN: Gi1/0/1 up', '192.0.2.21', now=100.4)
    b.receive(b'<14>%LINK-3-UPDOWN: Gi1/0/1 up', '192.0.2.22', now=100.4)
    assert a.lines[0]['id'] == b.lines[0]['id'] != b.lines[1]['id']


def test_while_the_hosted_side_is_away_lines_go_to_disk_and_come_back_in_order(tmp_path):
    hosted = Hosted()
    r = relay(hosted, tmp_path)
    hosted.away = True
    for n in range(5):
        r.receive(f'old {n}', '192.0.2.21')
    with pytest.raises(ConnectionError):
        r.flush_once()
    assert len(r.lines) == 0 and len(r.spool) == 5          # out of memory, on disk
    hosted.away = False
    r.receive('live', '192.0.2.21')
    r.flush_once()
    assert hosted.got == ['live', 'old 0', 'old 1', 'old 2', 'old 3', 'old 4']   # live first, then oldest first
    assert len(r.spool) == 0 and r.replayed == 5


def test_the_buffer_survives_a_restart(tmp_path):
    hosted = Hosted()
    hosted.away = True
    first = relay(hosted, tmp_path)
    for n in range(3):
        first.receive(f'line {n}', '192.0.2.21')
    first.stop()                                            # docker stop while the uplink is down
    hosted.away = False
    second = relay(hosted, tmp_path)                        # the next start, same state volume
    assert len(second.spool) == 3
    second.flush_once()
    assert hosted.got == ['line 0', 'line 1', 'line 2']


def test_replay_is_paced_so_live_lines_keep_flowing(tmp_path, monkeypatch):
    monkeypatch.setattr(syslog_relay, 'BATCH', 10)
    hosted = Hosted()
    r = relay(hosted, tmp_path)
    hosted.away = True
    for n in range(100):
        r.receive(f'old {n}', '192.0.2.21')
    with pytest.raises(ConnectionError):
        r.flush_once()
    hosted.away = False
    r.flush_once()
    assert len(hosted.got) == syslog_relay.REPLAY * 10      # this round's share only
    while len(r.spool):
        r.flush_once()
    assert hosted.got == [f'old {n}' for n in range(100)]


def test_a_send_that_fails_mid_replay_keeps_the_batch(tmp_path):
    hosted = Hosted()
    r = relay(hosted, tmp_path)
    r.spool.put([{'raw': 'kept', 'from': '192.0.2.21', 'at': 1, 'id': 'x'}])
    hosted.away = True
    with pytest.raises(ConnectionError):
        r.flush_once()
    assert len(r.spool) == 1
    hosted.away = False
    r.flush_once()
    assert hosted.got == ['kept']


def test_turned_off_on_the_hosted_side_drains_the_buffer(tmp_path):
    hosted = Hosted()
    r = relay(hosted, tmp_path)
    r.spool.put([{'raw': 'x', 'from': '192.0.2.21', 'at': 1, 'id': 'x'}])
    hosted.turned_off = True
    with pytest.raises(syslog_relay.TurnedOff):
        r.flush_once()
    assert len(r.spool) == 0 and r.all_dropped == 1


def test_size_and_age_limits_drop_the_oldest_first(tmp_path):
    spool = Spool(str(tmp_path / 's'), max_bytes=400, log=lambda *_: None)
    for n in range(10):
        spool.put([{'raw': f'line {n}' + 'x' * 50}])
    assert spool.size <= 400 and spool.dropped > 0
    _, records = spool.oldest()
    assert records[0]['raw'].startswith(f'line {spool.dropped}')       # the oldest went first
    aged = Spool(str(tmp_path / 'a'), max_age=60, log=lambda *_: None)
    aged.put([{'raw': 'old'}])
    first = os.path.join(aged.folder, aged.oldest()[0])
    os.utime(first, (0, 0))
    aged.put([{'raw': 'new'}])
    assert [r['raw'] for r in aged.oldest()[1]] == ['new'] and aged.dropped == 1


def test_a_full_disk_drops_and_says_so_once_then_recovers(tmp_path, monkeypatch):
    logged = []
    spool = Spool(str(tmp_path / 's'), log=logged.append)
    real_open = open

    def full(path, *a, **kw):
        if str(path).endswith('.tmp'):
            raise OSError(28, 'No space left on device')
        return real_open(path, *a, **kw)
    monkeypatch.setattr('builtins.open', full)
    assert spool.put([{'raw': 'a'}]) is False and spool.put([{'raw': 'b'}]) is False
    assert spool.dropped == 2 and len([line for line in logged if 'cannot write' in line]) == 1
    monkeypatch.setattr('builtins.open', real_open)
    assert spool.put([{'raw': 'c'}]) is True and len(spool) == 1
    assert any('can be written again' in line for line in logged)


def test_damaged_or_half_written_files_are_left_out(tmp_path):
    folder = tmp_path / 's'
    folder.mkdir()
    (folder / '000000000000.jsonl').write_text('{"raw": "fine"}\n')
    (folder / '000000000001.jsonl').write_text('{"raw": "cut off\n')
    (folder / '000000000002.jsonl.tmp').write_text('{"raw": "never renamed"}\n')
    spool = Spool(str(folder), log=lambda *_: None)
    assert not (folder / '000000000002.jsonl.tmp').exists()
    name, records = spool.oldest()
    assert records == [{'raw': 'fine'}]
    spool.done(name)
    assert spool.oldest() is None and spool.dropped == 1 and len(spool) == 0
    spool.put([{'raw': 'next'}])
    assert spool.oldest()[0] == '000000000002.jsonl'       # the counter goes on, no name reused


def test_files_are_private_to_the_agent(tmp_path):
    spool = Spool(str(tmp_path / 's'), log=lambda *_: None)
    assert oct(os.stat(spool.folder).st_mode & 0o777) == '0o700'


def test_buffer_limits_come_from_the_environment():
    from floorbit_agent.plugin_host import spool_limits
    assert spool_limits({}) == (2048 * 1024 ** 2, 24 * 3600)
    assert spool_limits({'FLOORBIT_AGENT_BUFFER_MAX_MB': '512', 'FLOORBIT_AGENT_BUFFER_MAX_HOURS': '6'}) == \
        (512 * 1024 ** 2, 6 * 3600)


@pytest.mark.parametrize('setting', [
    {'FLOORBIT_AGENT_BUFFER_MAX_MB': 'lots'}, {'FLOORBIT_AGENT_BUFFER_MAX_MB': 'inf'},
    {'FLOORBIT_AGENT_BUFFER_MAX_MB': 'nan'}, {'FLOORBIT_AGENT_BUFFER_MAX_MB': '1e400'},
    {'FLOORBIT_AGENT_BUFFER_MAX_MB': '0'}, {'FLOORBIT_AGENT_BUFFER_MAX_HOURS': 'nan'},
    {'FLOORBIT_AGENT_BUFFER_MAX_HOURS': '0'}, {'FLOORBIT_AGENT_BUFFER_MAX_HOURS': '-1'}])
def test_a_buffer_number_that_is_no_size_keeps_the_default(setting):
    from floorbit_agent.plugin_host import spool_limits
    assert spool_limits(setting) == (2048 * 1024 ** 2, 24 * 3600)


def test_a_burst_of_syslog_is_read_by_one_thread_with_room_in_the_kernel():
    """A thread per datagram lost four in five lines of a 25,000/s burst; one reader keeps up."""
    import socket
    r = syslog_relay.Relay(lambda lines, d: None, 0, bind='127.0.0.1', log=lambda *_: None)
    port = r.start()
    try:
        asked = r._server.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        assert asked > 212992                      # more than Linux's usual default
        threads = threading.active_count()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            for n in range(500):
                u.sendto(f'<14>line {n}'.encode(), ('127.0.0.1', port))
        deadline = time.monotonic() + 5
        while r.received < 500 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert r.received == 500 and threading.active_count() <= threads + 1
    finally:
        r.stop(flush=False)


def test_a_batch_of_long_lines_is_split_to_fit_what_the_hosted_side_takes(tmp_path):
    """2000 lines of 2048 emoji are 47 MB as JSON: the hosted side refused it (413) or, gzipped, read
    it as empty and took none. Posts now stay under BATCH_BYTES, live and from the disk buffer."""
    hosted = Hosted()
    sizes = []
    send = hosted.send

    def measured(lines, dropped):
        sizes.append(len(json.dumps({'lines': lines, 'dropped': dropped})))
        return send(lines, dropped)

    r = relay(hosted, tmp_path)
    r.send = measured
    for n in range(2000):
        r.receive(f'{n:04d}' + '😀' * 3000, '192.0.2.21')
    r.flush_once()
    assert len(hosted.got) == 2000 and [raw[:4] for raw in hosted.got] == [f'{n:04d}' for n in range(2000)]
    assert len(sizes) > 1 and max(sizes) <= syslog_relay.BATCH_BYTES + 64 * 1024

    hosted.away, sizes[:] = True, []
    for _ in range(2000):
        r.receive('😀' * 2048, '192.0.2.21')
    with pytest.raises(ConnectionError):
        r.flush_once()
    hosted.away = False
    while len(r.spool):
        r.flush_once()
    assert max(sizes) <= syslog_relay.BATCH_BYTES + 64 * 1024
