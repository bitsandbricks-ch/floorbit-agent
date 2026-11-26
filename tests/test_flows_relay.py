"""The agent's flow listener takes UDP from anyone on the event network: one packet the collector
can't read must not end the thread (Traffic then stayed empty until the agent restarted)."""
import socket
import struct
import threading
import time

from floorbit_agent.flows_relay import FlowsRelay


class Collector:
    def __init__(self):
        self.got, self.chores, self.flushed = [], 0, 0
        self.publish = None

    def flush(self):
        self.flushed += 1

    def enabled(self):
        return True

    def receive(self, data, addr):
        if data == b"bad":
            raise struct.error("unpack_from requires a buffer of at least 56 bytes")
        self.got.append(data)

    def housekeeping(self, now=None, refresh_seats=None):
        self.chores += 1
        if self.chores == 1:
            raise OSError(28, "No space left on device")


def wait_for(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_a_packet_the_collector_cannot_read_does_not_stop_the_listener():
    collector, lines = Collector(), []
    relay = FlowsRelay(agent=None, port=0, bind="127.0.0.1", collector=collector, log=lines.append)
    port = relay.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for data in (b"bad", b"bad", b"good"):
                sock.sendto(data, ("127.0.0.1", port))
            assert wait_for(lambda: collector.got == [b"good"])
        assert relay.dropped == 2 and relay.packets == 3
        assert any("could not read" in line for line in lines)
        # Housekeeping runs on its own thread once a second; one that fails is tried again.
        assert wait_for(lambda: any("housekeeping failed" in line for line in lines) and collector.chores >= 2)
        assert "agent-flows" in {t.name for t in threading.enumerate()}
    finally:
        relay.stop()


def test_stopping_writes_the_minutes_still_open():
    collector = Collector()
    relay = FlowsRelay(None, 0, bind="127.0.0.1", collector=collector, log=lambda *_: None)
    relay.start()
    relay.stop()
    assert collector.flushed == 1
