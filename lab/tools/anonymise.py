#!/usr/bin/env python3
"""Anonymise captures from the event's gear before they leave your machine (docs/lab/capture.md).

The same IP, MAC or name gets the same stand-in in every file, so ARP, FDB, LLDP and logs still
line up. Private IPv4 stays private and keeps its subnet structure; public IPv4 moves to the
documentation ranges. MACs keep their vendor part. Serials, sysContact and sysLocation become
placeholders. --map keeps the mapping between runs: keep that file to yourself.

    python3 lab/tools/anonymise.py --map ~/lab-map.json --out anonymised/ core.snmpwalk show-*.txt
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import secrets
import sys

IPV4 = re.compile(r'(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])')
MAC_COLON = re.compile(r'(?<![0-9A-Fa-f:])([0-9A-Fa-f]{2})([:-])([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})\2'
                       r'([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})\2([0-9A-Fa-f]{2})(?![0-9A-Fa-f:])')
MAC_CISCO = re.compile(r'(?<![0-9A-Fa-f.])([0-9A-Fa-f]{4})\.([0-9A-Fa-f]{4})\.([0-9A-Fa-f]{4})(?![0-9A-Fa-f.])')
HEX6 = re.compile(r'(Hex-STRING: )((?:[0-9A-Fa-f]{2} ){5}[0-9A-Fa-f]{2})(\s*)$')
WALK = re.compile(r'^(\.?[\d.]+) = (.*)$')

# OIDs whose index ends in an IPv4 address or a MAC (snmpwalk -On): rewritten in the index too.
IP_INDEX = ('1.3.6.1.2.1.4.20.1.', '1.3.6.1.2.1.4.22.1.', '1.3.6.1.2.1.4.35.1.', '1.3.6.1.2.1.4.34.1.')
MAC_INDEX = ('1.3.6.1.2.1.17.4.3.1.', '1.3.6.1.2.1.17.7.1.2.2.1.')
# Values that say who or where: placeholders, whatever they were.
PLACEHOLDER = {
    '1.3.6.1.2.1.1.4.0': 'STRING: noc@example.com',                # sysContact
    '1.3.6.1.2.1.1.6.0': 'STRING: Example hall',                   # sysLocation
}
SERIAL_OIDS = ('1.3.6.1.2.1.47.1.1.1.1.11.',)                     # entPhysicalSerialNum
SERIAL_TEXT = re.compile(r'((?:SN|Serial(?: ?[Nn]um(?:ber)?)?|serialnum|Processor board ID|"serialnum")'
                         r'["\s:=]+)([A-Z0-9]{8,14})')
SYSNAME = '1.3.6.1.2.1.1.5.0'
DOC_NETS = [ipaddress.ip_network(n) for n in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')]
PRIVATE = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
KEEP = [ipaddress.ip_network(n) for n in ('0.0.0.0/8', '127.0.0.0/8', '224.0.0.0/4', '255.255.255.255/32',
                                          '169.254.0.0/16')]


def _is_mask(addr):
    bits = int(addr)
    inverted = ~bits & 0xFFFFFFFF
    return bits != 0 and (inverted & (inverted + 1)) == 0


class Anonymiser:
    def __init__(self, state=None, names=None):
        state = state or {}
        self.key = bytes.fromhex(state.get('key') or secrets.token_hex(32))
        self.ips = dict(state.get('ips', {}))
        self.macs = dict(state.get('macs', {}))
        self.serials = dict(state.get('serials', {}))
        self.names = dict(state.get('names', {}))
        self.names.update(names or {})
        self.seen_hosts = set()

    def state(self):
        return {'key': self.key.hex(), 'ips': self.ips, 'macs': self.macs, 'serials': self.serials,
                'names': self.names}

    def _byte(self, label, value):
        return hmac.new(self.key, f'{label}|{value}'.encode(), hashlib.sha256).digest()[0]

    def ip(self, text):
        try:
            addr = ipaddress.IPv4Address(text)
        except ValueError:
            return text
        if any(addr in net for net in KEEP) or _is_mask(addr):
            return text
        if text in self.ips:
            return self.ips[text]
        o = [int(x) for x in text.split('.')]
        if any(addr in net for net in PRIVATE):
            # Prefix-preserving: each octet depends only on the octets before it, so subnets stay
            # subnets. The octets that say "private" stay; so do .0, .1 (the usual gateway) and .255.
            fixed = 1 if o[0] == 10 else 2
            out = o[:fixed]
            for i in range(fixed, 4):
                b = self._byte(f'ip{i}', '.'.join(map(str, o[:i + 1])))
                if i == 1 and o[0] == 172:
                    b = 16 + b % 16
                if i == 3:
                    b = o[3] if o[3] in (0, 1, 255) else 2 + b % 253
                out.append(b)
            new = '.'.join(map(str, out))
        else:
            net = DOC_NETS[self._byte('net', text) % len(DOC_NETS)]
            new = str(net.network_address + 2 + self._byte('host', text) % 252)
        # Two real addresses must not share a stand-in.
        taken = set(self.ips.values())
        while new in taken:
            head, last = new.rsplit('.', 1)
            new = f'{head}.{int(last) % 252 + 2}'
        self.ips[text] = new
        return new

    def mac(self, octets):
        """octets: six ints. Keeps the vendor (OUI) part."""
        key = ':'.join(f'{x:02x}' for x in octets)
        if key in self.macs:
            return [int(x, 16) for x in self.macs[key].split(':')]
        if octets in ([0] * 6, [255] * 6):
            return octets
        tail = list(hmac.new(self.key, f'mac|{key}'.encode(), hashlib.sha256).digest()[:3])
        new = octets[:3] + tail
        self.macs[key] = ':'.join(f'{x:02x}' for x in new)
        return new

    def serial(self, value):
        if value not in self.serials:
            self.serials[value] = f'LAB{len(self.serials) + 1:08d}'
        return self.serials[value]

    # --- text -----------------------------------------------------------------------------

    def text(self, line):
        for real, stand_in in self.names.items():
            line = re.sub(re.escape(real), stand_in, line, flags=re.IGNORECASE)
        line = SERIAL_TEXT.sub(lambda m: m.group(1) + self.serial(m.group(2)), line)
        line = MAC_COLON.sub(self._mac_colon, line)
        line = MAC_CISCO.sub(self._mac_cisco, line)
        line = HEX6.sub(self._mac_hex, line)
        return IPV4.sub(lambda m: self.ip(m.group(0)), line)

    def _mac_colon(self, m):
        new = self.mac([int(m.group(i), 16) for i in (1, 3, 4, 5, 6, 7)])
        upper = m.group(1).isupper()
        return m.group(2).join(f'{x:02X}' if upper else f'{x:02x}' for x in new)

    def _mac_cisco(self, m):
        raw = ''.join(m.group(i) for i in (1, 2, 3))
        new = ''.join(f'{x:02x}' for x in self.mac([int(raw[i:i + 2], 16) for i in range(0, 12, 2)]))
        return f'{new[0:4]}.{new[4:8]}.{new[8:12]}'

    def _mac_hex(self, m):
        new = self.mac([int(x, 16) for x in m.group(2).split()])
        return m.group(1) + ' '.join(f'{x:02X}' for x in new) + m.group(3)

    # --- snmpwalk -ObentU -----------------------------------------------------------------

    def walk_line(self, line):
        m = WALK.match(line.rstrip('\n'))
        if not m:
            return self.text(line)
        oid, value = m.group(1), m.group(2)
        bare = oid.lstrip('.')
        if bare in PLACEHOLDER:
            return f'{oid} = {PLACEHOLDER[bare]}\n'
        if bare == SYSNAME:
            self.seen_hosts.add(value.split(':', 1)[-1].strip().strip('"'))
        if bare.startswith(SERIAL_OIDS):
            kind, _, v = value.partition(': ')
            v = v.strip().strip('"')
            if v:
                value = f'{kind}: {self.serial(v)}'
        oid = self._index(oid)
        value = self.text(value) if not value.startswith('OID: ') else value
        return f'{oid} = {value}\n'

    def _index(self, oid):
        bare = oid.lstrip('.')
        parts = bare.split('.')
        if bare.startswith(IP_INDEX) and len(parts) >= 4:
            ip = '.'.join(parts[-4:])
            if all(p.isdigit() and int(p) < 256 for p in parts[-4:]):
                parts[-4:] = self.ip(ip).split('.')
        elif bare.startswith(MAC_INDEX) and len(parts) >= 6:
            octets = [int(p) for p in parts[-6:]]
            if all(o < 256 for o in octets):
                parts[-6:] = [str(o) for o in self.mac(octets)]
        return ('.' if oid.startswith('.') else '') + '.'.join(parts)

    def file(self, text, walk):
        lines = text.splitlines(keepends=True)
        if walk:
            return ''.join(self.walk_line(line) for line in lines)
        out = ''.join(self.text(line) for line in lines)
        for host in re.findall(r'"host_name":\s*"([^"]+)"|^hostname (\S+)|Device ID:\s*(\S+)', text, re.M):
            self.seen_hosts.update(h for h in host if h)
        return out


def looks_like_walk(text):
    lines = [line for line in text.splitlines()[:20] if line.strip()]
    return bool(lines) and sum(bool(WALK.match(line)) for line in lines) >= len(lines) * 0.8


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('files', nargs='+', type=pathlib.Path)
    p.add_argument('--out', type=pathlib.Path, required=True, help='folder for the anonymised copies')
    p.add_argument('--map', type=pathlib.Path, help='keep the mapping here (private!) to reuse it')
    p.add_argument('--name', action='append', default=[], metavar='REAL=STAND_IN',
                   help='replace a word everywhere (event, venue, crew names), case-insensitive')
    args = p.parse_args(argv)

    names = {}
    for pair in args.name:
        real, sep, stand_in = pair.partition('=')
        if not sep or not real:
            p.error(f'--name wants REAL=STAND_IN, got {pair!r}')
        names[real] = stand_in
    state = json.loads(args.map.read_text()) if args.map and args.map.exists() else None
    anon = Anonymiser(state, names)

    args.out.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        if path.resolve().parent == args.out.resolve():
            continue
        text = path.read_text(errors='replace')
        (args.out / path.name).write_text(anon.file(text, looks_like_walk(text)))
        print(f'{path} -> {args.out / path.name}')

    if args.map:
        args.map.write_text(json.dumps(anon.state(), indent=1))
        os.chmod(args.map, 0o600)
    print(f'{len(anon.ips)} IPs, {len(anon.macs)} MACs, {len(anon.serials)} serials replaced.')
    left = sorted(h for h in anon.seen_hosts if h not in anon.names.values())
    if left:
        print('Hostnames seen (add --name real=stand-in for any that name the event or a person):')
        for host in left:
            print(f'  {host}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
