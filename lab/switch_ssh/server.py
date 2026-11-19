#!/usr/bin/env python3
"""A lab switch's SSH: the Cisco command line a tech (or Floorbit through the agent) logs in to.

One process per simulated switch (lab/device.sh), built from lab/topology.py: Catalyst IOS on the
access and distribution switches, NX-OS on the core. It knows what Floorbit sends a switch (port
actions, the switch command templates, config backups, the browser terminal) and what a tech types
by hand, with IOS's abbreviations ("sh int status", "conf t", "int gi1/0/5", "no shut"):

    show running-config | startup-config | version | clock | inventory
    show interfaces status | counters errors | <port>      show ip interface brief
    show mac address-table [interface <port>]               terminal length|width <n>
    configure terminal > interface <port> > shutdown | no shutdown | description <text> |
        switchport access vlan <n> | switchport mode access|trunk | spanning-tree portfast
    clear counters <port>  (IOS asks [confirm], as it does)  write memory | copy run start
    end | exit | do <command> | enable | logout

A change does what the switch would: the port's status in its SNMP data changes (snmpsim serves the
edited copy, so LibreNMS and Telegraf see the port go down), it logs %LINK/%LINEPROTO (NX-OS:
%ETHPORT) and %SYS-5-CONFIG_I to the lab's syslog targets and sends linkDown/linkUp traps, and the
running config shows it. ``write memory`` keeps it over a restart; without it a restarted switch
comes back with its last saved config, as a real one does. Anything else answers "% Invalid input".

Settings (env): LAB_SSH_LISTEN (0.0.0.0:22), LAB_SSH_USER / LAB_SSH_PASSWORD (the lab's login, not a
secret), LAB_SSH_STATE (a folder for the host key and the saved config), LAB_SNMP_DATA (the device's
writable snmpsim folder), LAB_SYSLOG_TARGETS and LAB_TRAP_TARGETS as for replay.py.
"""
import argparse
import contextlib
import json
import os
import pathlib
import re
import socket
import sys
import threading
import time

import paramiko

LAB_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB_DIR))
sys.path.insert(0, str(LAB_DIR / 'replay'))
import replay  # noqa: E402
from topology import DOMAIN, LAB, SEAT_VLAN, SERVER_VLAN  # noqa: E402

IF_ADMIN, IF_OPER, IF_ALIAS = '1.3.6.1.2.1.2.2.1.7', '1.3.6.1.2.1.2.2.1.8', '1.3.6.1.2.1.31.1.1.1.18'
FAKE_HASH = '$9$lab.not.a.real.hash$Xq3v1yLm0W2bQh7cRt5sZu8eNa4oPk6dJf9gHi1Ty0'


def short(name):
    for long, abbrev in (('TenGigabitEthernet', 'Te'), ('GigabitEthernet', 'Gi'), ('Ethernet', 'Eth')):
        if name.startswith(long):
            return abbrev + name[len(long):]
    return name


class Switch:
    """The switch's state: per port admin status, description, VLAN and counters since clear."""

    def __init__(self, dev, state_dir, snmp_dir=None):
        self.dev = dev
        self.nxos = dev.role == 'core'
        self.lock = threading.Lock()
        self.state_dir = pathlib.Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snmp_dir = pathlib.Path(snmp_dir) if snmp_dir else None
        self.started = time.time()
        self.seq = 1000
        seats = {(s.switch, s.port): s for s in LAB.seats}
        self.ports = {}
        for p in dev.ports:
            seat = seats.get((dev.name, p.name))
            self.ports[p.name] = {'shut': not p.admin_up, 'descr': p.descr,
                                  'vlan': seat.vlan if seat else None, 'mode': 'access' if seat else (
                                      'trunk' if p.peer else 'access'),
                                  'portfast': bool(seat), 'cleared': self.started}
        saved = self._saved()
        for name, conf in (saved.get('ports') or {}).items():
            if name in self.ports:
                self.ports[name].update(conf)
        self.last_change = saved.get('changed') or self.started
        self.saved_at = saved.get('saved')
        for name in self.ports:
            self._snmp(name)

    # --- saved config ------------------------------------------------------------------------

    def _saved_file(self):
        return self.state_dir / f'{self.dev.name}-startup.json'

    def _saved(self):
        try:
            return json.loads(self._saved_file().read_text())
        except (OSError, ValueError):
            return {}

    def save(self):
        with self.lock:
            ports = {n: {k: v for k, v in c.items() if k != 'cleared'} for n, c in self.ports.items()}
            self.saved_at = time.time()
            body = {'ports': ports, 'changed': self.last_change, 'saved': self.saved_at}
        tmp = self._saved_file().with_suffix('.tmp')
        tmp.write_text(json.dumps(body))
        tmp.replace(self._saved_file())

    # --- what the port is now ----------------------------------------------------------------

    def port(self, name):
        return next(p for p in self.dev.ports if p.name == name)

    def oper_up(self, name):
        """Up when it is cabled (topology) and nobody shut it."""
        return self.port(name).up and not self.ports[name]['shut']

    def octets(self, name):
        p = self.port(name)
        if not self.oper_up(name):
            return 0, 0
        secs = time.time() - self.ports[name]['cleared']
        return int(p.traffic_bps * secs / 8 * 0.6), int(p.traffic_bps * secs / 8)

    # --- changes -----------------------------------------------------------------------------

    def set_shut(self, name, shut, who):
        with self.lock:
            was = self.ports[name]['shut']
            self.ports[name]['shut'] = shut
            self.last_change = time.time()
        if was != shut:
            self._snmp(name)
            self._announce(name, shut)

    def set(self, name, **conf):
        with self.lock:
            self.ports[name].update(conf)
            self.last_change = time.time()
        if 'descr' in conf:
            self._snmp(name)

    def clear(self, name):
        with self.lock:
            self.ports[name]['cleared'] = time.time()

    def _snmp(self, name):
        """The port as SNMP shows it, in the device's own snmpsim copy (it rereads a changed file)."""
        if not self.snmp_dir:
            return
        p = self.port(name)
        conf = self.ports[name]
        want = {f'{IF_ADMIN}.{p.if_index}': ('2', '2' if conf['shut'] else '1'),
                f'{IF_OPER}.{p.if_index}': ('2', '1' if self.oper_up(name) else '2'),
                f'{IF_ALIAS}.{p.if_index}': ('4', conf['descr'])}
        for path in sorted(self.snmp_dir.glob('*.snmprec')):
            lines = path.read_text().splitlines()
            out, changed = [], False
            for line in lines:
                oid, _, rest = line.partition('|')
                if oid in want:
                    kind = rest.split('|', 1)[0]
                    new = f'{oid}|{kind}|{want[oid][1]}'
                    changed |= new != line
                    line = new
                out.append(line)
            if changed:
                tmp = path.with_suffix('.tmp')
                tmp.write_text('\n'.join(out) + '\n')
                tmp.replace(path)

    def _announce(self, name, shut):
        """What the switch tells its logging hosts and trap receivers about the port."""
        now = time.time()
        up = self.oper_up(name)
        if self.nxos:
            lines = [f'%ETHPORT-5-IF_DOWN_ADMIN_DOWN: Interface {name} is down (Administratively down)'] if shut else (
                [f'%ETHPORT-5-IF_UP: Interface {name} is up in mode {self.ports[name]["mode"]}'] if up else [])
            self.log([replay.nxos(now, text) for text in lines])
        else:
            if shut:
                texts = [(f'%LINK-5-CHANGED: Interface {name}, changed state to administratively down', 5),
                         (f'%LINEPROTO-5-UPDOWN: Line protocol on Interface {name}, changed state to down', 5)]
            elif up:
                texts = [(f'%LINK-3-UPDOWN: Interface {name}, changed state to up', 3),
                         (f'%LINEPROTO-5-UPDOWN: Line protocol on Interface {name}, changed state to up', 5)]
            else:
                texts = []
            self.log([self.ios_line(now, text, sev) for text, sev in texts])
        if dests := replay.targets('LAB_TRAP_TARGETS'):
            p = self.port(name)
            replay.send_traps([(self.dev, p.if_index, short(name), up)], dests)

    def ios_line(self, now, text, severity=5):
        self.seq += 1
        return replay.ios(self.dev.name, self.seq, now, text, severity)

    def configured(self, user, peer):
        now = time.time()
        if self.nxos:
            self.log([replay.nxos(now, f'%VSHD-5-VSHD_SYSLOG_CONFIG_I: Configured from vty by {user} on {peer}@pts/0')])
        else:
            self.log([self.ios_line(now, f'%SYS-5-CONFIG_I: Configured from console by {user} on vty0 ({peer})')])

    def log(self, lines):
        dests = replay.targets('LAB_SYSLOG_TARGETS')
        if not dests or not lines:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            with contextlib.suppress(OSError):
                s.bind((self.dev.ip, 0))      # from the switch's own address, as Floorbit expects
            for line in lines:
                for dest in dests:
                    s.sendto(line.encode(), dest)

    # --- what show commands print --------------------------------------------------------------

    def running_config(self):
        return self._nxos_config() if self.nxos else self._ios_config()

    def _ios_config(self):
        d = self.dev
        out = ['Building configuration...', '', 'Current configuration : 0 bytes', '!',
               f'! Last configuration change at {stamp(self.last_change)} UTC by lab', '!',
               'version 15.2' if d.role == 'access' else 'version 03.11', 'no service pad',
               'service timestamps debug datetime msec', 'service timestamps log datetime msec show-timezone',
               'service password-encryption', '!', f'hostname {d.name}', '!',
               f'enable secret 9 {FAKE_HASH}', '!', f'username lab privilege 15 secret 9 {FAKE_HASH}',
               'aaa new-model', '!', f'ip domain-name {DOMAIN}', '!', 'spanning-tree mode rapid-pvst', '!']
        vlans = sorted({c['vlan'] for c in self.ports.values() if c['vlan']} | (
            set(SEAT_VLAN.values()) if d.role == 'distribution' else set()))
        for vlan in vlans:
            out += [f'vlan {vlan}', f' name seats-{chr(ord("A") + vlan - 101)}', '!']
        for p in d.ports:
            conf = self.ports[p.name]
            out.append(f'interface {p.name}')
            if conf['descr']:
                out.append(f' description {conf["descr"]}')
            if p.kind == 'vlan':
                if p.name == 'Vlan1' or p.name.endswith('100'):
                    out.append(f' ip address {d.ip} 255.255.255.0')
                else:
                    out.append(' no ip address')
            elif conf['mode'] == 'trunk':
                out += [' switchport mode trunk']
            else:
                if conf['vlan']:
                    out.append(f' switchport access vlan {conf["vlan"]}')
                out.append(' switchport mode access')
                if conf['portfast']:
                    out.append(' spanning-tree portfast')
            if conf['shut']:
                out.append(' shutdown')
            out.append('!')
        out += [f'ip default-gateway {d.ip.rsplit(".", 1)[0]}.1', '!',
                'snmp-server community public RO', 'snmp-server community s3cr3t-lab-rw RW',
                'snmp-server location lab hall', '!', 'logging origin-id hostname' if d.role == 'distribution'
                else '!', 'logging host 172.31.10.31', '!', 'line con 0', 'line vty 0 15',
                ' transport input ssh', '!', 'ntp server 172.31.10.1', 'end']
        size = len('\n'.join(out))
        out[2] = f'Current configuration : {size} bytes'
        return '\n'.join(out)

    def _nxos_config(self):
        d = self.dev
        out = ['', '!Command: show running-config', f'!Running configuration last done at: {stamp(self.last_change)}',
               f'!Time: {stamp(time.time())}', '', 'version 9.3(12) Bios:version 05.47', f'hostname {d.name}',
               'feature telemetry', 'feature interface-vlan', 'feature lacp', 'feature lldp', '',
               f'username admin password 5 {FAKE_HASH}  role network-admin',
               f'username lab password 5 {FAKE_HASH}  role network-admin', f'ip domain-name {DOMAIN}',
               'snmp-server community public group network-operator', '']
        for vlan in (100, *SEAT_VLAN.values(), SERVER_VLAN):
            out += [f'vlan {vlan}', f'  name {self.ports[f"Vlan{vlan}"]["descr"]}']
        out.append('')
        for p in d.ports:
            conf = self.ports[p.name]
            out.append(f'interface {p.name}')
            if conf['descr']:
                out.append(f'  description {conf["descr"]}')
            if p.kind == 'vlan':
                vlan = int(p.name[4:])
                net = '172.31.10.11/24' if vlan == 100 else f'10.10.{vlan}.1/24'
                out += [f'  ip address {net}']
            elif p.kind == 'mgmt':
                out += ['  vrf member management', f'  ip address {d.ip}/24']
            out.append('  shutdown' if conf['shut'] else '  no shutdown')
            out.append('')
        out += ['line console', 'line vty', 'telemetry', '  destination-group 1',
                '    ip address 172.31.10.31 port 57000 protocol gRPC encoding GPB', '']
        return '\n'.join(out)

    def status_table(self):
        rows = []
        for p in self.dev.ports:
            if p.kind != 'ethernet':
                continue
            conf = self.ports[p.name]
            status = 'disabled' if conf['shut'] else ('connected' if p.up else 'notconnect')
            vlan = 'trunk' if conf['mode'] == 'trunk' else str(conf['vlan'] or 1)
            speed = 'a-1000' if p.speed_mbps == 1000 else (f'{p.speed_mbps // 1000}G')
            duplex = 'a-half' if p.half_duplex else 'a-full'
            if not self.oper_up(p.name):
                speed, duplex = 'auto', 'auto'
            name = short(p.name)
            rows.append(f'{name:<10}{conf["descr"][:18]:<19}{status:<13}{vlan:<11}{duplex:>6} {speed:>6} '
                        + ('10/100/1000BaseTX' if p.speed_mbps <= 1000 else 'SFP-10GBase-SR'))
        head = f'{"Port":<10}{"Name":<19}{"Status":<13}{"Vlan":<11}{"Duplex":>6} {"Speed":>6} Type'
        if self.nxos:
            head = '-' * 80 + '\n' + head + '\n' + '-' * 80
        return '\n' + head + '\n' + '\n'.join(rows)

    def interface(self, name):
        p, conf = self.port(name), self.ports[name]
        up = self.oper_up(name)
        state = 'administratively down' if conf['shut'] else ('up' if up else 'down')
        rx, tx = self.octets(name)
        crc = 1843 if p.crc_errors and up else 0
        if self.nxos:
            return '\n'.join([
                f'{name} is {"up" if up else "down"}' + (' (Administratively down)' if conf['shut'] else ''),
                f'admin state is {"down" if conf["shut"] else "up"}, Dedicated Interface',
                f'  Hardware: 1000/10000/25000 Ethernet, address: 0022.bdf8.{p.if_index & 0xffff:04x}',
                f'  Description: {conf["descr"]}' if conf['descr'] else '  Description: ',
                f'  MTU 1500 bytes, BW {p.speed_mbps * 1000} Kbit, DLY 10 usec',
                f'  full-duplex, {p.speed_mbps // 1000} Gb/s' if up else '  auto-duplex, auto-speed',
                '  RX', '    0 unicast packets  0 multicast packets  0 broadcast packets',
                f'    0 input packets  {rx} bytes', f'    0 CRC  {crc} input error  0 short frame  0 overrun',
                '  TX', f'    0 output packets  {tx} bytes'])
        line = 'up (connected)' if up else 'down (disabled)' if conf['shut'] else 'down (notconnect)'
        return '\n'.join([
            f'{name} is {state}, line protocol is {line}',
            f'  Hardware is Gigabit Ethernet, address is 021a.b1{p.if_index >> 8 & 0xff:02x}.{p.if_index & 0xff:02x}00',
            f'  Description: {conf["descr"]}' if conf['descr'] else '',
            f'  MTU 1500 bytes, BW {p.speed_mbps * 1000} Kbit/sec, DLY 10 usec,',
            f'  {"Half" if p.half_duplex else "Full"}-duplex, {p.speed_mbps}Mb/s, media type is 10/100/1000BaseTX'
            if up else '  Auto-duplex, Auto-speed, media type is 10/100/1000BaseTX',
            f'  Last clearing of "show interface" counters {ago(self.ports[name]["cleared"])}',
            f'  5 minute input rate {int(p.traffic_bps * 0.6) if up else 0} bits/sec, 0 packets/sec',
            f'  5 minute output rate {p.traffic_bps if up else 0} bits/sec, 0 packets/sec',
            f'     0 packets input, {rx} bytes, 0 no buffer',
            f'     {crc} input errors, {crc} CRC, 0 frame, 0 overrun, 0 ignored',
            f'     0 packets output, {tx} bytes, 0 underruns']).replace('\n\n', '\n')

    def errors_table(self):
        rows = [f'{"Port":<12}{"Align-Err":>10}{"FCS-Err":>12}{"Xmit-Err":>12}{"Rcv-Err":>12}{"UnderSize":>11}']
        for p in self.dev.ports:
            if p.kind == 'ethernet':
                fcs = 1843 if p.crc_errors and self.oper_up(p.name) else 0
                rows.append(f'{short(p.name):<12}{0:>10}{fcs:>12}{0:>12}{fcs:>12}{0:>11}')
        return '\n' + '\n'.join(rows)

    def macs(self, only=None):
        rows = []
        for seat in LAB.seats:
            if seat.switch != self.dev.name and not (self.dev.role != 'access' and seat.online):
                continue
            port = seat.port if seat.switch == self.dev.name else self._uplink_to(seat.switch)
            if not seat.online or not port or (seat.switch == self.dev.name and not self.oper_up(port)):
                continue
            if only and port != only:
                continue
            mac = seat.mac.replace(':', '')
            rows.append((seat.vlan, f'{mac[0:4]}.{mac[4:8]}.{mac[8:12]}', short(port)))
        if self.nxos:
            body = ['Legend: ', '        * - primary entry, G - Gateway MAC, (R) - Routed MAC, O - Overlay MAC', '',
                    '   VLAN     MAC Address      Type      age     Secure NTFY Ports',
                    '---------+-----------------+--------+---------+------+----+------------------']
            body += [f'*  {v:<6}  {m}   dynamic  0         F      F    {p}' for v, m, p in rows]
            return '\n'.join(body)
        body = ['          Mac Address Table', '-------------------------------------------', '',
                'Vlan    Mac Address       Type        Ports', '----    -----------       --------    -----']
        body += [f' {v:<6} {m}    DYNAMIC     {p}' for v, m, p in rows]
        body.append(f'Total Mac Addresses for this criterion: {len(rows)}')
        return '\n'.join(body)

    def _uplink_to(self, switch):
        return next((p.name for p in self.dev.ports if p.peer and p.peer[0] == switch), None) or next(
            (p.name for p in self.dev.ports if p.peer and p.peer[0] != self.dev.name and
             LAB.device(p.peer[0]).role != 'firewall'), None)

    def version(self):
        d = self.dev
        up = int(time.time() - self.started)
        if self.nxos:
            return '\n'.join(['Cisco Nexus Operating System (NX-OS) Software', 'Software',
                              '  NXOS: version 9.3(12)', 'Hardware', f'  cisco Nexus9000 {d.model} Chassis',
                              f'  Device name: {d.name}', f'Kernel uptime is 0 day(s), {up // 3600} hour(s), '
                              f'{up // 60 % 60} minute(s), {up % 60} second(s)'])
        return '\n'.join([d.sys_descr, f'{d.name} uptime is {up // 3600} hours, {up // 60 % 60} minutes',
                          f'cisco {d.model} processor with 524288K bytes of memory.',
                          f'Model number                    : {d.model}',
                          f'System serial number            : {d.serial}'])

    def ip_brief(self):
        rows = [f'{"Interface":<23}{"IP-Address":<16}{"OK?":<4}{"Method":<7}{"Status":<22}Protocol']
        for p in self.dev.ports:
            ip = self.dev.ip if p.name in ('Vlan1', 'Vlan100', 'mgmt0') else 'unassigned'
            conf = self.ports[p.name]
            status = 'administratively down' if conf['shut'] else ('up' if self.oper_up(p.name) or p.kind != 'ethernet'
                                                                   else 'down')
            proto = 'up' if self.oper_up(p.name) or (p.kind != 'ethernet' and not conf['shut']) else 'down'
            rows.append(f'{p.name:<23}{ip:<16}{"YES":<4}{"NVRAM":<7}{status:<22}{proto}')
        return '\n'.join(rows)


def stamp(t):
    return time.strftime('%H:%M:%S %b %d %Y', time.gmtime(t))


def ago(t):
    secs = int(time.time() - t)
    return f'{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}'


# --- the command line ------------------------------------------------------------------------

IF = object()       # a port name, "Gi1/0/5" or "gigabitethernet 1/0/5"
NUM = object()
REST = object()     # the rest of the line


class Invalid(Exception):
    def __init__(self, at=0, message=None):
        super().__init__(message)
        self.at, self.message = at, message


def resolve_port(switch, word):
    m = re.fullmatch(r'([A-Za-z-]+)\s*(\d[\d/.:]*)', word.strip())
    if not m:
        return None
    kind, number = m.group(1).lower(), m.group(2)
    for p in switch.dev.ports:
        pm = re.fullmatch(r'([A-Za-z-]+)(\d[\d/.:]*)', p.name)
        if pm and pm.group(1).lower().startswith(kind) and pm.group(2) == number:
            return p.name
    return None


def match(switch, words, patterns):
    """(handler, args) for the pattern the words abbreviate; Invalid at the word that fits none."""
    best_at, found = 0, []
    for pattern, handler in patterns:
        args, i, ok = [], 0, True
        for key in pattern:
            if i >= len(words):
                ok = False
                break
            if key is REST:
                args.append(' '.join(words[i:]))
                i = len(words)
                continue
            if key is NUM:
                if not words[i].isdigit():
                    ok = False
                    break
                args.append(int(words[i]))
                i += 1
                continue
            if key is IF:
                name = resolve_port(switch, words[i])
                if name is None and i + 1 < len(words):
                    name = resolve_port(switch, words[i] + words[i + 1])
                    if name:
                        i += 1
                if name is None:
                    ok = False
                    best_at = max(best_at, i)
                    break
                args.append(name)
                i += 1
                continue
            if not key.startswith(words[i].lower()):
                ok = False
                best_at = max(best_at, i)
                break
            i += 1
        if ok and i == len(words):
            found.append((pattern, handler, args))
        elif ok:
            best_at = max(best_at, i)
    if not found:
        incomplete = any(len(words) < len(p) and all(
            isinstance(key, str) and key.startswith(w.lower()) for key, w in zip(p, words, strict=False))
            for p, _ in patterns)
        raise Invalid(best_at, 'incomplete' if incomplete else None)
    # "sh int gi1/0/5" fits the port pattern only; an exact keyword wins over a shorter abbreviation.
    exact = [f for f in found if all(not isinstance(k, str) or k == w.lower() or k.startswith(w.lower())
                                     for k, w in zip(f[0], words, strict=False))]
    if len(exact) > 1:
        longest = max(len(f[0]) for f in exact)
        exact = [f for f in exact if len(f[0]) == longest] or exact
    return exact[0][1], exact[0][2]


class Session:
    """One SSH shell: the prompt, the modes, the pager, line editing."""

    def __init__(self, switch, channel, user, peer):
        self.sw, self.chan, self.user, self.peer = switch, channel, user, peer
        self.mode, self.port = 'exec', None
        self.length, self.changed = 24, False
        self.open = True

    # I/O
    def write(self, text):
        self.chan.sendall(text.replace('\r\n', '\n').replace('\n', '\r\n').encode())

    def getch(self):
        data = self.chan.recv(1)
        if not data:
            raise EOFError
        return data.decode('latin-1')

    def prompt(self):
        name = self.sw.dev.name
        return {'exec': f'{name}#', 'config': f'{name}(config)#', 'if': f'{name}(config-if)#'}[self.mode]

    def readline(self):
        line = ''
        while True:
            ch = self.getch()
            if ch in '\r\n':
                if ch == '\r' and self.chan.recv_ready():
                    nxt = self.chan.recv(1)
                    if nxt not in (b'\n', b'\x00'):
                        line += nxt.decode('latin-1')
                self.write('\n')
                return line
            if ch in '\x7f\x08':
                if line:
                    line = line[:-1]
                    self.write('\b \b')
            elif ch == '\x03':          # Ctrl-C: drop the line
                self.write('^C\n')
                return None
            elif ch == '\x1a':          # Ctrl-Z: end
                self.write('^Z\n')
                return 'end'
            elif ch == '\t' or ch == '\x1b':
                continue
            elif ch >= ' ':
                line += ch
                self.write(ch)

    def page(self, text):
        """Print with the pager: " --More-- " every screen until "terminal length 0"."""
        lines = text.replace('\r\n', '\n').split('\n')
        if not self.length or len(lines) < self.length:
            self.write(text + '\n')
            return
        i, step = 0, self.length - 1
        while i < len(lines):
            self.write('\n'.join(lines[i:i + step]) + '\n')
            i += step
            if i >= len(lines):
                return
            self.write(' --More-- ')
            ch = self.getch()
            self.write('\b' * 10 + ' ' * 10 + '\b' * 10)
            if ch in 'qQ\x03':
                return
            step = 1 if ch in '\r\n' else self.length - 1

    def error(self, line, err):
        if err.message == 'incomplete':
            self.write('% Incomplete command.\n\n')
            return
        words = line.split()
        at = len(self.prompt()) + sum(len(w) + 1 for w in words[:max(err.at, 0)]) + (len(line) - len(line.lstrip()))
        self.write(' ' * at + '^\n')
        self.write("% Invalid command at '^' marker.\n\n" if self.sw.nxos else
                   "% Invalid input detected at '^' marker.\n\n")

    def run(self):
        self.write('\n' + self.prompt())
        while self.open:
            line = self.readline()
            if line is not None and line.strip():
                self.command(line.strip())
            if self.open:
                self.write(self.prompt())

    def command(self, line):
        words = line.split()
        if words[0].lower() == 'do' and self.mode != 'exec' and len(words) > 1:
            words, mode = words[1:], 'exec'
        else:
            mode = self.mode
        patterns = EXEC if mode == 'exec' else CONFIG if mode == 'config' else CONFIG_IF
        try:
            handler, args = match(self.sw, words, patterns)
        except Invalid as err:
            # In an interface, a global command moves back to (config) like IOS does.
            if mode == 'if':
                try:
                    handler, args = match(self.sw, words, CONFIG)
                    self.mode = 'config'
                except Invalid:
                    self.error(line, err)
                    return
            else:
                self.error(line, err)
                return
        handler(self, *args)

    # exec
    def show_run(self):
        self.page(self.sw.running_config())

    def show_start(self):
        if self.sw.saved_at is None and not self.sw._saved_file().exists():
            self.page(self.sw.running_config())
            return
        saved = self.sw._saved()
        now = {n: dict(c) for n, c in self.sw.ports.items()}
        for n, c in (saved.get('ports') or {}).items():
            if n in self.sw.ports:
                self.sw.ports[n].update(c)
        try:
            self.page(self.sw.running_config())
        finally:
            for n, c in now.items():
                self.sw.ports[n].update(c)

    def end(self):
        if self.mode != 'exec':
            self.mode, self.port = 'exec', None
            if self.changed:
                self.sw.configured(self.user, self.peer)
                self.changed = False

    def exit(self):
        if self.mode == 'if':
            self.mode, self.port = 'config', None
        elif self.mode == 'config':
            self.end()
        else:
            self.open = False

    def logout(self):
        self.open = False

    def conf_t(self):
        self.mode = 'config'
        self.write('Enter configuration commands, one per line.  End with CNTL/Z.\n')

    def terminal_length(self, n):
        self.length = n

    def nothing(self, *_):
        pass

    def write_mem(self):
        self.sw.save()
        if self.sw.nxos:
            self.write('[########################################] 100%\nCopy complete.\n')
        else:
            self.write('Building configuration...\n[OK]\n')

    def copy_run_start(self):
        if not self.sw.nxos:
            self.write('Destination filename [startup-config]? ')
            answer = self.readline()
            if answer is None:
                return
        self.write_mem()

    def clear_counters(self, port):
        if self.sw.nxos:
            self.error(f'clear counters {port}', Invalid(2))
            return
        self.write('Clear "show interface" counters on this interface [confirm]')
        ch = self.getch()
        self.write('\n')
        if ch in '\r\ny':
            self.sw.clear(port)

    def clear_counters_nxos(self, port):
        if not self.sw.nxos:
            self.error(f'clear counters interface {port}', Invalid(2))
            return
        self.sw.clear(port)

    # config
    def interface(self, port):
        self.mode, self.port = 'if', port

    def shutdown(self):
        self.sw.set_shut(self.port, True, self.user)
        self.changed = True

    def no_shutdown(self):
        self.sw.set_shut(self.port, False, self.user)
        self.changed = True

    def description(self, text):
        self.sw.set(self.port, descr=text[:240])
        self.changed = True

    def no_description(self):
        self.sw.set(self.port, descr='')
        self.changed = True

    def access_vlan(self, vlan):
        if not 1 <= vlan <= 4094:
            self.error(f'switchport access vlan {vlan}', Invalid(3))
            return
        self.sw.set(self.port, vlan=vlan)
        self.changed = True

    def mode_access(self):
        self.sw.set(self.port, mode='access')
        self.changed = True

    def mode_trunk(self):
        self.sw.set(self.port, mode='trunk')
        self.changed = True

    def portfast(self):
        self.sw.set(self.port, portfast=True)
        self.changed = True


def _out(fn):
    return lambda s, *a: s.page(fn(s.sw, *a))


EXEC = [
    (('show', 'running-config'), Session.show_run),
    (('show', 'startup-config'), Session.show_start),
    (('show', 'version'), _out(lambda sw: sw.version())),
    (('show', 'clock'), _out(lambda sw: '*' + time.strftime('%H:%M:%S.000 UTC %a %b %d %Y', time.gmtime()))),
    (('show', 'inventory'), _out(lambda sw: f'NAME: "1", DESCR: "{sw.dev.model}"\n'
                                            f'PID: {sw.dev.model}  , VID: V01  , SN: {sw.dev.serial}')),
    (('show', 'interfaces', 'status'), _out(lambda sw: sw.status_table())),
    (('show', 'interfaces', 'counters', 'errors'), _out(lambda sw: sw.errors_table())),
    (('show', 'interfaces', IF), _out(lambda sw, p: sw.interface(p))),
    (('show', 'ip', 'interface', 'brief'), _out(lambda sw: sw.ip_brief())),
    (('show', 'mac', 'address-table'), _out(lambda sw: sw.macs())),
    (('show', 'mac', 'address-table', 'interface', IF), _out(lambda sw, p: sw.macs(p))),
    (('terminal', 'length', NUM), Session.terminal_length),
    (('terminal', 'width', NUM), Session.nothing),
    (('configure', 'terminal'), Session.conf_t),
    (('write', 'memory'), Session.write_mem),
    (('copy', 'running-config', 'startup-config'), Session.copy_run_start),
    (('clear', 'counters', 'interface', IF), Session.clear_counters_nxos),
    (('clear', 'counters', IF), Session.clear_counters),
    (('enable',), Session.nothing),
    (('end',), Session.nothing),
    (('exit',), Session.exit),
    (('logout',), Session.logout),
    (('quit',), Session.logout),
]
CONFIG = [
    (('interface', IF), Session.interface),
    (('end',), Session.end),
    (('exit',), Session.exit),
]
CONFIG_IF = [
    (('shutdown',), Session.shutdown),
    (('no', 'shutdown'), Session.no_shutdown),
    (('description', REST), Session.description),
    (('no', 'description'), Session.no_description),
    (('switchport', 'access', 'vlan', NUM), Session.access_vlan),
    (('switchport', 'mode', 'access'), Session.mode_access),
    (('switchport', 'mode', 'trunk'), Session.mode_trunk),
    (('spanning-tree', 'portfast'), Session.portfast),
    (('interface', IF), Session.interface),
    (('end',), Session.end),
    (('exit',), Session.exit),
]


# --- SSH ---------------------------------------------------------------------------------------

class Server(paramiko.ServerInterface):
    def __init__(self, user, password):
        self.user, self.password = user, password
        self.shell = threading.Event()
        self.exec_command = None

    def check_auth_password(self, username, password):
        if username == self.user and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_window_change_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell.set()
        return True

    def check_channel_exec_request(self, channel, command):
        self.exec_command = command.decode('utf-8', 'replace')
        self.shell.set()
        return True


def host_key(state_dir, name):
    """The switch's own key, kept over restarts: Floorbit pins it, and a new one means a new switch."""
    path = pathlib.Path(state_dir) / f'{name}-host-key'
    try:
        return paramiko.RSAKey(filename=str(path))
    except (OSError, paramiko.SSHException):
        key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(path))
        return key


def handle(sock, addr, switch, key, user, password):
    transport = paramiko.Transport(sock)
    transport.local_version = 'SSH-2.0-Cisco-1.25'
    transport.add_server_key(key)
    server = Server(user, password)
    try:
        transport.start_server(server=server)
        chan = transport.accept(30)
        if chan is None or not server.shell.wait(15):
            return
        session = Session(switch, chan, user, addr[0])
        if server.exec_command is not None:
            session.length = 0
            session.command(server.exec_command)
            chan.send_exit_status(0)
        else:
            session.run()
    except (EOFError, OSError, paramiko.SSHException):
        pass
    finally:
        transport.close()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--device', required=True)
    p.add_argument('--listen', default=os.environ.get('LAB_SSH_LISTEN', '0.0.0.0:22'))
    args = p.parse_args(argv)
    dev = LAB.device(args.device)
    if dev.role == 'firewall':
        raise SystemExit('The firewalls have no switch command line.')
    state = os.environ.get('LAB_SSH_STATE', '/tmp/lab-ssh')
    switch = Switch(dev, state, os.environ.get('LAB_SNMP_DATA'))
    key = host_key(state, dev.name)
    user = os.environ.get('LAB_SSH_USER', 'lab')
    password = os.environ.get('LAB_SSH_PASSWORD', 'lab-switch-password')
    host, _, port = args.listen.rpartition(':')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host or '0.0.0.0', int(port)))
    listener.listen(20)
    print(f'{dev.name}: SSH on {args.listen}', flush=True)
    while True:
        sock, addr = listener.accept()
        threading.Thread(target=handle, args=(sock, addr, switch, key, user, password), daemon=True).start()


if __name__ == '__main__':
    main()
