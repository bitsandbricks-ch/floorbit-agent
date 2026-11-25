"""The syslog relay's sending."""
import threading
import time

from floorbit_agent.syslog_relay import HOLD, Relay


def test_the_last_send_on_the_way_out_waits_for_the_send_thread():
    """stop() sends what waits while the send thread may be sending too: never both at once."""
    inside, most = [0], [0]
    gate = threading.Event()

    def send(batch, dropped):
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        gate.wait(0.2)
        inside[0] -= 1

    relay = Relay(send, 0, log=lambda *_: None)
    for i in range(5000):
        relay.lines.append({'raw': f'line {i}', 'from': '192.0.2.1', 'at': 0, 'id': str(i)})
    t = threading.Thread(target=relay.flush_once)
    t.start()
    time.sleep(0.05)
    relay.stop()          # used to raise "pop from an empty deque" here
    t.join()
    assert most[0] == 1 and not relay.lines


def test_only_the_owner_sends_a_devices_lines_the_other_keeps_a_minute():
    relay = Relay(lambda lines, dropped: None, 0, keep=lambda sender: sender == "192.0.2.11")
    relay.receive("<13>link up", "192.0.2.11", now=1000)
    relay.receive("<13>link down", "192.0.2.12", now=1000)
    relay.receive("<13>too old", "192.0.2.12", now=1000 - HOLD - 1)
    assert [line["from"] for line in relay.lines] == ["192.0.2.11"]
    # The other agent went: its device is this one's now, and the last minute goes out after all.
    assert relay.take_over(lambda sender: True, now=1001) == 1
    assert [line["raw"] for line in relay.lines] == ["<13>link up", "<13>link down"]
    assert not relay.held


def test_without_owners_every_line_goes_out_as_before():
    relay = Relay(lambda lines, dropped: None, 0)
    relay.receive("<13>hello", "192.0.2.12")
    assert len(relay.lines) == 1 and not relay.held
