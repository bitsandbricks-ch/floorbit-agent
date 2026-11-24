#!/usr/bin/env python3
"""Replay the event's chatter into the lab: syslog, SNMP traps, NetFlow, the LAN cache's logs.

Every line is made from lab/topology.py, so its seats, MACs and IPs match the simulated switches.
The formats follow what the gear sends (NX-OS, Catalyst IOS, OPNsense's filterlog, lancache.net's
access log, lancache-dns's bind query log); docs/lab/capture.md asks the event's gear for real
samples to check them against.

    python3 lab/replay/replay.py --once           # one round of everything, then stop
    python3 lab/replay/replay.py                  # a round every LAB_REPLAY_EVERY seconds (10)
    python3 lab/replay/replay.py --device core-01 # only what the core sends (in its container)

Where things go (env, comma-separated host:port lists; empty: that stream is off):
  LAB_SYSLOG_TARGETS   UDP syslog, e.g. agent-a:5514,agent-b:5514 (every target gets every
                       line, as switches with two logging hosts do: the hosted side keeps one)
  LAB_TRAP_TARGETS     SNMPv2c traps (linkDown/linkUp), e.g. telegraf:1162
  LAB_TRAP_V3          one device's own SNMPv3 trap user (floorbit#975), e.g.
                       access-a1=lab-trap:SHA:auth-pass:AES:priv-pass@agent-a:1162: that device
                       also sends a v3 trap and a v2c one to that listener, each round
  LAB_FLOW_TARGETS     NetFlow v9, e.g. agent-a:2055
  LAB_LANCACHE_DIR     a folder to append logs/access.log and dns/query.log to
  LAB_PUSH_URL         Influx line protocol from an OPNsense running Telegraf, POSTed here
  LAB_STREAM_URL       the core's gNMI and Cisco MDT stream and the 9800's AP stream, as
                       Telegraf's gnmi and cisco_telemetry_mdt inputs hand them on (line
                       protocol), POSTed here
  LAB_TELEMETRY_TOKEN_FILE  the collector's ingest token for both (an agent's
                       plugins/telemetry/ingest-token), sent as "Authorization: Token ..."
"""
import argparse
import os
import pathlib
import random
import socket
import struct
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from topology import LAB, WLC

FAC_LOCAL7 = 23
STEAM = 'lancache.steamcontent.com'
SERVICES = [('steam', STEAM, '/depot/{d}/chunk/{h}', 'Valve/Steam HTTP Client 1.0'),
            ('epicgames', 'download.epicgames.com', '/Builds/Org/{h}/default/ChunksV4/{d}/{h}.chunk',
             'EpicGamesLauncher/16.0.0'),
            ('blizzard', 'level3.blizzard.com', '/tpr/wow/data/{h}/{h}', 'Blizzard Agent'),
            ('riot', 'lol.dyn.riotcdn.net', '/channels/public/bundles/{h}.bundle', 'RiotClient/80.0')]
DEPOTS = (228988, 228990, 730, 1172470, 578080)


def targets(name):
    out = []
    for item in [t.strip() for t in os.environ.get(name, '').split(',') if t.strip()]:
        host, _, port = item.rpartition(':')
        out.append((host, int(port)))
    return out


# --- syslog ----------------------------------------------------------------------------------

def pri(severity, facility=FAC_LOCAL7):
    return f'<{facility * 8 + severity}>'


def nxos(now, text, severity=5):
    """NX-OS with `logging timestamp milliseconds` off: no hostname, the year in the stamp."""
    return f'{pri(severity)}: {time.strftime("%Y %b %d %H:%M:%S", time.gmtime(now))} UTC: {text}'


def ios(dev, seq, now, text, severity=5):
    """Catalyst IOS with `service timestamps log datetime msec show-timezone` and NTP. IOS always
    puts its message counter first; the hostname only comes with `logging origin-id hostname`,
    which the lab turns on for the distribution switch only, so both forms are replayed."""
    stamp = time.strftime('%b %d %H:%M:%S', time.gmtime(now)) + f'.{int(now * 1000) % 1000:03d}'
    origin = f'{dev}: ' if dev == LAB.dist.name else ''
    return f'{pri(severity)}{seq}: {origin}{stamp} UTC: {text}'


def filterlog(fw, now, seq, src, dst, sport, dport, action='block'):
    """OPNsense's filterlog over RFC 5424, as its remote logging sends it."""
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(now))
    csv = (f'77,,,02f4bab031b57d1e30553ce08e0ec131,igb1,match,{action},in,4,0x0,,64,{seq % 65535},0,DF,6,tcp,'
           f'60,{src},{dst},{sport},{dport},0,S,{seq * 7919 % 4294967295},,64240,,mss;sackOK;TS;nop;wscale')
    return f'<134>1 {stamp} {fw}.lab.example filterlog 51234 - [meta sequenceId="{seq}"] {csv}'


def syslog_round(rnd, now):
    """[(sender ip, line)] for one round: link flaps, a MAC flap, STP, DHCP snooping, firewall."""
    rng = random.Random(rnd)
    out = []
    seat = rng.choice([s for s in LAB.seats if s.online])
    sw = LAB.device(seat.switch)
    port = seat.port
    for state, sev in (('down', 3), ('up', 3)):
        out.append((sw.ip, ios(sw.name, rnd * 10 + len(out), now,
                               f'%LINK-3-UPDOWN: Interface {port}, changed state to {state}', sev)))
        out.append((sw.ip, ios(sw.name, rnd * 10 + len(out), now,
                               f'%LINEPROTO-5-UPDOWN: Line protocol on Interface {port}, changed state to {state}')))
    if rnd % 3 == 0:
        a, b = rng.sample([s for s in LAB.seats if s.switch == sw.name and s.online], 2)
        mac = a.mac.replace(':', '')
        out.append((sw.ip, ios(sw.name, rnd * 10 + 9, now,
                               f'%SW_MATM-4-MACFLAP_NOTIF: Host {mac[0:4]}.{mac[4:8]}.{mac[8:12]} in vlan {a.vlan} '
                               f'is flapping between port {_short(a.port)} and port {_short(b.port)}', 4)))
    if rnd % 5 == 0:
        out.append((LAB.dist.ip, ios(LAB.dist.name, rnd, now,
                                     '%SPANTREE-2-BLOCK_BPDUGUARD: Received BPDU on port Gi3/7 with BPDU Guard '
                                     'enabled. Disabling port.', 2)))
    out.append((LAB.core.ip, nxos(now, f'%ETHPORT-5-SPEED: Interface Ethernet1/{10 + rnd % 2}, operational speed '
                                       'changed to 10 Gbps')))
    if rnd % 4 == 0:
        out.append((LAB.core.ip, nxos(now, '%ARP-3-DUP_SRCIP_PROBE:  arp [10541]  Duplicate address Detected - '
                                           f'Probe packet with source ip 0.0.0.0, target ip {seat.ip} on interface '
                                           f'Vlan{seat.vlan}, from {seat.mac}', 3)))
    for fw in LAB.firewalls:
        player = rng.choice(LAB.seats)
        out.append((fw.ip, filterlog(fw.name, now, rnd * 3 + 1, player.ip, '203.0.113.25', 40000 + rnd % 20000, 25)))
    return out


def _short(name):
    return name.replace('GigabitEthernet', 'Gi').replace('TenGigabitEthernet', 'Te')


def send_syslog(lines, dests):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _sender, line in lines:
            for dest in dests:
                s.sendto(line.encode(), dest)


# --- SNMP traps ------------------------------------------------------------------------------

def trap_round(rnd):
    """[(device, ifIndex, ifName, up)]: the same flap the syslog reports."""
    rng = random.Random(rnd)
    seat = rng.choice([s for s in LAB.seats if s.online])
    sw = LAB.device(seat.switch)
    p = sw.port(seat.port)
    return [(sw, p.if_index, _short(p.name), False), (sw, p.if_index, _short(p.name), True)]


def send_traps(traps, dests):
    for sw, if_index, name, up in traps:
        oid = '1.3.6.1.6.3.1.1.5.4' if up else '1.3.6.1.6.3.1.1.5.3'
        for host, port in dests:
            # net-snmp's snmptrap: the lab image has it; a trap carries no secret but the community.
            subprocess.run(['snmptrap', '-v2c', '-c', 'public', f'{host}:{port}', '', oid,
                            f'1.3.6.1.2.1.2.2.1.1.{if_index}', 'i', str(if_index),
                            f'1.3.6.1.2.1.2.2.1.7.{if_index}', 'i', '1',
                            f'1.3.6.1.2.1.2.2.1.8.{if_index}', 'i', '1' if up else '2',
                            f'1.3.6.1.2.1.31.1.1.1.1.{if_index}', 's', name,
                            '1.3.6.1.6.3.18.1.3.0', 'a', sw.ip],
                           check=False, capture_output=True, timeout=10)


def v3_spec():
    """(device, user, auth, auth pass, priv, priv pass, (host, port)) from LAB_TRAP_V3, or None."""
    text = os.environ.get('LAB_TRAP_V3', '').strip()
    if not text:
        return None
    device, _, rest = text.partition('=')
    login, _, dest = rest.rpartition('@')
    user, auth, auth_pass, priv, priv_pass = login.split(':')
    host, _, port = dest.rpartition(':')
    return device, user, auth, auth_pass, priv, priv_pass, (host, int(port))


# Ports no poll ever reports, so a test can tell these traps from everything else.
V3_PORT = (4242, 'v3-trap')
V2C_ON_V3_PORT = (4243, 'v2c-on-v3')


def _link_down(if_index, name, ip):
    return ['1.3.6.1.6.3.1.1.5.3', f'1.3.6.1.2.1.2.2.1.1.{if_index}', 'i', str(if_index),
            f'1.3.6.1.2.1.2.2.1.8.{if_index}', 'i', '2', f'1.3.6.1.2.1.31.1.1.1.1.{if_index}', 's', name,
            '1.3.6.1.6.3.18.1.3.0', 'a', ip]


def send_v3_traps(sw, spec):
    """A linkDown as the device's SNMPv3 trap user, and a v2c one to the same listener: a v3
    listener still takes v1 and v2c (floorbit telemetry/telegraf.py)."""
    _device, user, auth, auth_pass, priv, priv_pass, (host, port) = spec
    # A trap's engine is the sender's own: one per device, fixed, so the listener's keys hold.
    engine = '0x80001f8880' + ''.join(f'{int(o):02x}' for o in sw.ip.split('.'))
    v3 = ['snmptrap', '-v3', '-e', engine, '-u', user, '-l', 'authPriv', '-a', auth, '-A', auth_pass,
          '-x', priv, '-X', priv_pass, f'{host}:{port}', '', *_link_down(*V3_PORT, sw.ip)]
    v2c = ['snmptrap', '-v2c', '-c', 'public', f'{host}:{port}', '', *_link_down(*V2C_ON_V3_PORT, sw.ip)]
    for cmd in (v3, v2c):
        subprocess.run(cmd, check=False, capture_output=True, timeout=10)
    return 2


# --- NetFlow v9 ------------------------------------------------------------------------------

TEMPLATE_ID = 256
# (field, length): bytes, packets, proto, src port, src v4, input if, dst port, dst v4, output if,
# last and first switched (uptime ms).
FIELDS = ((1, 4), (2, 4), (4, 1), (7, 2), (8, 4), (10, 2), (11, 2), (12, 4), (14, 2), (21, 4), (22, 4))
DESTS = [('203.0.113.80', 443, 6, 'web'), ('198.51.100.27', 27015, 17, 'game'), ('10.10.200.10', 80, 6, 'lancache'),
         ('198.51.100.53', 3478, 17, 'voice'), ('192.0.2.200', 443, 6, 'web')]


def netflow_packets(rnd, now, seq):
    """NetFlow v9 packets from the core: a template, then the round's flows (both directions)."""
    rng = random.Random(rnd)
    uptime = int((now % 86400) * 1000) + 1000
    records = []
    for seat in [s for s in LAB.seats if s.online]:
        for _ in range(rng.randint(1, 3)):
            dst, dport, proto, kind = rng.choice(DESTS)
            down = rng.randint(200_000, 40_000_000) if kind in ('lancache', 'web') else rng.randint(20_000, 400_000)
            up = max(down // 20, 2000)
            sport = rng.randint(30000, 60000)
            records.append((up, max(up // 900, 1), proto, sport, seat.ip, 1, dport, dst, 2,
                            uptime - 500, uptime - 9500))
            records.append((down, max(down // 1300, 1), proto, dport, dst, 2, sport, seat.ip, 1, uptime - 500,
                            uptime - 9500))
    fmt = struct.Struct('!IIBH4sHH4sHII')
    packets = []
    template = struct.pack('!HH', TEMPLATE_ID, len(FIELDS)) + b''.join(struct.pack('!HH', f, n) for f, n in FIELDS)
    template_set = struct.pack('!HH', 0, 4 + len(template)) + template
    per = 25
    for start in range(0, len(records), per):
        chunk = records[start:start + per]
        body = b''.join(fmt.pack(b, p, pr, sp, socket.inet_aton(s), i, dp, socket.inet_aton(d), o, last, first)
                        for b, p, pr, sp, s, i, dp, d, o, last, first in chunk)
        pad = (-len(body)) % 4
        data_set = struct.pack('!HH', TEMPLATE_ID, 4 + len(body) + pad) + body + b'\0' * pad
        sets = (template_set if start == 0 else b'') + data_set
        count = len(chunk) + (1 if start == 0 else 0)
        packets.append(struct.pack('!HHIIII', 9, count, uptime, int(now), seq + len(packets), 1) + sets)
    return packets


def send_flows(packets, dests):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for pkt in packets:
            for dest in dests:
                s.sendto(pkt, dest)


# --- LAN cache ---------------------------------------------------------------------------------

def lancache_lines(rnd, now):
    """(access log lines, DNS query log lines) for one round."""
    rng = random.Random(rnd)
    access, dns = [], []
    stamp = time.strftime('%d/%b/%Y:%H:%M:%S +0000', time.gmtime(now))
    qstamp = time.strftime('%d-%b-%Y %H:%M:%S', time.gmtime(now)) + '.123'
    players = [s for s in LAB.seats if s.online]
    # One popular download most players fetch, and some background noise.
    depot = DEPOTS[rnd % len(DEPOTS)]
    for seat in rng.sample(players, 12):
        cache, host, path, agent = SERVICES[0] if rng.random() < 0.7 else rng.choice(SERVICES)
        h = f'{rng.getrandbits(80):020x}'
        url = path.format(d=depot, h=h)
        status = 'HIT' if rng.random() < 0.8 else 'MISS'
        size = rng.choice((1048576, 1048576, 524288, 2097152))
        access.append(f'[{cache}] {seat.ip} / - - - [{stamp}] "GET {url} HTTP/1.1" 200 {size} "-" "{agent}" '
                      f'"{status}" "{host}" "-"')
        port = rng.randint(1024, 65535)
        dns.append(f'{qstamp} queries: info: client @0x7f3b2c00{rng.randint(1000, 9999)} {seat.ip}#{port} ({host}): '
                   f'query: {host} IN A + (10.10.200.11)')
    for seat in rng.sample(players, 6):
        site = rng.choice(('www.example.com', 'api.example.net', 'cdn.example.org', 'voice.example.com'))
        dns.append(f'{qstamp} queries: info: client @0x7f3b2c00{rng.randint(1000, 9999)} {seat.ip}#'
                   f'{rng.randint(1024, 65535)} ({site}): query: {site} IN AAAA + (10.10.200.11)')
    return access, dns


def write_lancache(folder, access, dns):
    folder = pathlib.Path(folder)
    (folder / 'logs').mkdir(parents=True, exist_ok=True)
    (folder / 'dns').mkdir(parents=True, exist_ok=True)
    with open(folder / 'logs' / 'access.log', 'a') as f:
        f.writelines(line + '\n' for line in access)
    with open(folder / 'dns' / 'query.log', 'a') as f:
        f.writelines(line + '\n' for line in dns)


# --- OPNsense Telegraf push ------------------------------------------------------------------

def opnsense_lines(rnd, now):
    """Influx line protocol as os-telegraf on OPNsense sends it: interfaces, pf, system."""
    ns = int(now) * 1_000_000_000
    out = []
    for n, fw in enumerate(LAB.firewalls, start=1):
        host = f'{fw.name}.lab.example'
        for p in fw.ports:
            octets = (rnd + 1) * 125_000_000 * (n + 1) // 10
            out.append(f'net,host={host},interface={p.name} bytes_recv={octets}i,bytes_sent={octets // 4}i,'
                       f'err_in=0i,err_out=0i,drop_in=0i,drop_out=0i {ns}')
        out.append(f'pf,host={host} entries={1800 + rnd * 7 % 400}i,searches={rnd * 90000}i,match={rnd * 1200}i {ns}')
        out.append(f'cpu,host={host},cpu=cpu-total usage_idle={100 - fw.cpu:.1f},usage_system=2.0,usage_user=1.5 {ns}')
        out.append(f'mem,host={host} used_percent={fw.mem_used_mb * 100 / (fw.mem_used_mb + fw.mem_free_mb):.1f} {ns}')
    return out


def streamed_lines(rnd, now):
    """The Nexus core's model-driven telemetry, in the shape Telegraf hands it on: gNMI (OpenConfig
    interface state, sampled) and Cisco MDT dial-out (the same paths, from the core's address).
    Made from topology.py until docs/lab/capture.md brings a recorded stream to replace it."""
    core, ns = LAB.core, int(now) * 1_000_000_000
    out = []
    for p in [p for p in core.ports if p.kind == 'ethernet' and p.descr]:
        octets = (rnd + 1) * max(p.traffic_bps, 1_000_000) * 10 // 8
        state = 'UP' if p.up else 'DOWN'
        counters = (f'state/counters/in-octets={octets}i,state/counters/out-octets={octets // 2}i,'
                    f'state/counters/in-errors=0i,state/counters/out-errors=0i')
        out.append(f'fb_gnmi_if,device={core.name},driver=gnmi,name={p.name},source={core.ip} '
                   f'{counters},state/oper-status="{state}",state/admin-status="UP" {ns}')
        # MDT dial-out carries no device tag and no token of its own: the collector knows it by
        # the node name (or address) the device was set up with.
        out.append(f'fb_mdt_if,mdt_source={core.name},name={p.name} state/oper-status="{state}" {ns}')
    return out


def wireless_lines(rnd, now):
    """The 9800's MDT for its access points (capwap-data, radio-oper-data, rrm-measurement), in the
    shape Telegraf's cisco_telemetry_mdt input hands it on with Floorbit's aliases. Counts only: a
    controller's client list is never subscribed to."""
    ns, out = int(now) * 1_000_000_000, []
    for ap in WLC.aps:
        key = f'mdt_source={WLC.name},wtp_mac={ap.mac}'
        out.append(f'fb_wlc_ap,{key} name="{ap.name}",device-detail/static-info/ap-models/model="{ap.model}",'
                   f'ap-state/ap-operation-state="registered" {ns}')
        for slot, band, channel, width, use, clients in ap.radios:
            radio = f'{key},radio_slot_id={slot}'
            out.append(f'fb_wlc_radio,{radio} oper-state="radio-up",current-active-band="{band}",'
                       f'phy-ht-cfg/cfg-data/curr-freq={channel}i,'
                       f'phy-ht-cfg/cfg-data/chan-width="chan-width-{width}-mhz" {ns}')
            # A little movement per round, so the history isn't a flat line.
            out.append(f'fb_wlc_rrm,{radio} load/cca-util-percentage={use + rnd % 5}i,'
                       f'load/stations={clients + rnd % 3}i {ns}')
    return out


def telemetry_token():
    path = os.environ.get('LAB_TELEMETRY_TOKEN_FILE')
    try:
        return pathlib.Path(path).read_text().strip() if path else ''
    except OSError:
        return ''          # the agent hasn't written it yet: try again next round


def push(url, lines, token=''):
    headers = {'Content-Type': 'text/plain'}
    if token:
        headers['Authorization'] = f'Token {token}'
    req = urllib.request.Request(url, data='\n'.join(lines).encode(), method='POST', headers=headers)
    with urllib.request.urlopen(req, timeout=10) as answer:
        answer.read()


# --- the loop -------------------------------------------------------------------------------

def one_round(rnd, now, seq=0, device=None, log=print):
    """Send one round. With ``device``, only what that device sends itself (the lab runs this in
    each simulated device's container, so the packets come from the device's own address)."""
    me = LAB.device(device) if device and device != 'lancache-01' else None
    sent = {}
    if dests := targets('LAB_SYSLOG_TARGETS'):
        lines = [(ip, line) for ip, line in syslog_round(rnd, now) if not device or (me and ip == me.ip)]
        send_syslog(lines, dests)
        sent['syslog'] = len(lines)
    if dests := targets('LAB_TRAP_TARGETS'):
        traps = [t for t in trap_round(rnd) if not device or t[0] is me]
        send_traps(traps, dests)
        sent['traps'] = len(traps)
    if (spec := v3_spec()) and me is not None and me.name == spec[0]:
        sent['v3 traps'] = send_v3_traps(me, spec)
    if (dests := targets('LAB_FLOW_TARGETS')) and (not device or me is LAB.core):
        packets = netflow_packets(rnd, now, seq)
        send_flows(packets, dests)
        sent['netflow'] = len(packets)
    if (folder := os.environ.get('LAB_LANCACHE_DIR')) and device in (None, 'lancache-01'):
        access, dns = lancache_lines(rnd, now)
        write_lancache(folder, access, dns)
        sent['lancache'] = len(access) + len(dns)
    if (url := os.environ.get('LAB_PUSH_URL')) and (not device or (me and me.role == 'firewall')):
        lines = [line for line in opnsense_lines(rnd, now) if not me or f'host={me.name}.' in line]
        try:
            push(url, lines, telemetry_token())
            sent['push'] = len(lines)
        except OSError as exc:
            log(f'push to {url} failed: {exc}')
    if (url := os.environ.get('LAB_STREAM_URL')) and (not device or me is LAB.core):
        lines = streamed_lines(rnd, now) + wireless_lines(rnd, now)
        try:
            push(url, lines, telemetry_token())
            sent['stream'] = len(lines)
        except OSError as exc:
            log(f'stream to {url} failed: {exc}')
    return sent


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--once', action='store_true', help='one round, then stop')
    p.add_argument('--device', help='send only what this device sends (core-01, access-a1, fw-01, lancache-01, ...)')
    args = p.parse_args(argv)
    every = float(os.environ.get('LAB_REPLAY_EVERY', '10'))
    rnd, seq = 0, 0
    while True:
        sent = one_round(rnd, time.time(), seq, args.device)
        seq += sent.get('netflow', 0)
        print(f'round {rnd}: ' + (', '.join(f'{v} {k}' for k, v in sent.items()) or 'nothing to send to'), flush=True)
        if args.once:
            return 0
        rnd += 1
        time.sleep(every)


if __name__ == '__main__':
    sys.exit(main())
