"""VRRP mode against the real keepalived (issue #3): two agents in two network namespaces on one
link, two VIPs. Checks that each holds its preferred VIP, that the other takes over when one stops
or turns unhealthy, and how fast. Needs root (namespaces) and keepalived; touches nothing outside
its namespaces. Linux only.

    sudo python scripts/vrrp_check.py
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

VIPS = ('198.51.100.10', '198.51.100.11')     # TEST-NET-2: never a real network
NODES = {'a': ('198.51.100.1', '1'), 'b': ('198.51.100.2', '2')}


def node(name, prefer):
    """One agent's VRRP part, inside its namespace. SIGUSR1 flips its health."""
    from floorbit_agent import vrrp
    from floorbit_agent.metrics import Metrics
    healthy = [True]
    signal.signal(signal.SIGUSR1, lambda *_: healthy.__setitem__(0, not healthy[0]))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    settings = vrrp.settings_from({'FLOORBIT_AGENT_VRRP_VIPS': ','.join(f'{v}/24' for v in VIPS),
                                   'FLOORBIT_AGENT_VRRP_INTERFACE': f'veth-{name}',
                                   'FLOORBIT_AGENT_VRRP_PREFER': prefer})
    k = vrrp.Keepalived(settings, Metrics(), log=lambda line: print(json.dumps({'log': line}), flush=True),
                        healthy=lambda: healthy[0])
    k.start()
    try:
        while True:
            time.sleep(3600)
    finally:
        k.stop()


def sh(*args):
    subprocess.run(args, check=True)


def holds(ns):
    out = subprocess.run(['ip', 'netns', 'exec', ns, 'ip', '-o', '-4', 'addr'], capture_output=True, text=True).stdout
    return sorted(v for v in VIPS if f' {v}/' in out)


def until(check, seconds=30):
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        if check():
            return time.monotonic() - started
        time.sleep(0.05)
    raise SystemExit(f'FAILED: not within {seconds} s')


def start(name):
    p = subprocess.Popen(['ip', 'netns', 'exec', f'fbv-{name}', sys.executable, __file__, '--node', name,
                          '--prefer', NODES[name][1]], stdout=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: [print(f'  {name}: {json.loads(line)["log"]}') for line in p.stdout],
                     daemon=True).start()
    return p


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--node')
    p.add_argument('--prefer')
    args = p.parse_args()
    if args.node:
        return node(args.node, args.prefer)
    if os.geteuid() != 0:
        raise SystemExit('Run it as root: it makes two network namespaces.')
    for name in NODES:
        sh('ip', 'netns', 'add', f'fbv-{name}')
    sh('ip', 'link', 'add', 'veth-a', 'netns', 'fbv-a', 'type', 'veth', 'peer', 'name', 'veth-b', 'netns', 'fbv-b')
    procs = {}
    try:
        for name, (ip, _) in NODES.items():
            sh('ip', 'netns', 'exec', f'fbv-{name}', 'ip', 'addr', 'add', f'{ip}/24', 'dev', f'veth-{name}')
            sh('ip', 'netns', 'exec', f'fbv-{name}', 'ip', 'link', 'set', f'veth-{name}', 'up')
            sh('ip', 'netns', 'exec', f'fbv-{name}', 'ip', 'link', 'set', 'lo', 'up')
        procs = {name: start(name) for name in NODES}
        t = until(lambda: holds('fbv-a') == [VIPS[0]] and holds('fbv-b') == [VIPS[1]])
        print(f'1. both up: a holds {VIPS[0]}, b holds {VIPS[1]} (after {t:.1f} s)')

        procs['a'].send_signal(signal.SIGUSR1)
        t = until(lambda: holds('fbv-b') == sorted(VIPS) and holds('fbv-a') == [])
        print(f'2. a not healthy: b holds both after {t:.1f} s')
        procs['a'].send_signal(signal.SIGUSR1)
        t = until(lambda: holds('fbv-a') == [VIPS[0]])
        print(f'3. a healthy again: takes {VIPS[0]} back after {t:.1f} s (preempt_delay 10 s)')

        procs['a'].terminate()
        procs['a'].wait(10)
        t = until(lambda: holds('fbv-b') == sorted(VIPS))
        print(f'4. a stopped: b holds both after {t:.1f} s')

        procs['a'] = start('a')
        t = until(lambda: holds('fbv-a') == [VIPS[0]] and holds('fbv-b') == [VIPS[1]])
        print(f'5. a started again: back to one each after {t:.1f} s')

        procs['b'].kill()                         # no goodbye: b's keepalived lives on until it notices
        os.system('ip netns pids fbv-b | xargs -r kill -9')
        t = until(lambda: holds('fbv-a') == sorted(VIPS))
        print(f'6. b killed hard: a holds both after {t:.1f} s')
        print('OK')
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
                proc.wait(10)
        for name in NODES:
            os.system(f'ip netns pids fbv-{name} | xargs -r kill -9; ip netns del fbv-{name}')


if __name__ == '__main__':
    sys.exit(main())
