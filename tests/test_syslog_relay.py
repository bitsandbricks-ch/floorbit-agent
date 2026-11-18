"""The syslog relay's sending."""
import threading
import time

from floorbit_agent.syslog_relay import Relay


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
