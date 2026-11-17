#!/usr/bin/env python3
"""Write the simulated switches' SNMP data (snmpsim .snmprec files) from lab/topology.py.

    python3 lab/snmp/build.py            # writes lab/snmp/data/<device>/public*.snmprec, lab/fixtures/seats.json
    python3 lab/snmp/build.py --check    # fails when the committed files are out of date (CI)

One folder per device, served by its own snmpsim container (lab/compose.yml) on its own IP, as on
the event network. The community is the file name: "public", and on the Catalysts "public@<vlan>"
for each VLAN's bridge table, the way Cisco IOS hands out per-VLAN FDBs.

The walks follow the MIBs LibreNMS and Floorbit read: SNMPv2-MIB, IF-MIB, EtherLike-MIB,
ENTITY-MIB, IP-MIB (ARP, only on the core), BRIDGE-MIB and Q-BRIDGE-MIB (FDB), LLDP-MIB,
CISCO-CDP-MIB, CISCO-VTP-MIB, CISCO-PROCESS-MIB, CISCO-MEMORY-POOL-MIB, CISCO-ENVMON-MIB,
CISCO-ENTITY-SENSOR-MIB and HOST-RESOURCES-MIB. Counters move: snmpsim's numeric variation turns
each port's rate into a counter that grows with the clock.
"""
import argparse
import ipaddress
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from topology import INFRA_VLAN, LAB, SEAT_VLAN, SERVER_VLAN

OUT = pathlib.Path(__file__).resolve().parent / 'data'
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / 'fixtures'

# snmprec types
INT, STR, HEX, OID, IPADDR, C32, G32, TICKS, C64 = '2', '4', '4x', '6', '64', '65', '66', '67', '70'
NUMERIC = ':numeric'


def oid_key(oid):
    return tuple(int(x) for x in oid.split('.'))


def mac_hex(mac):
    return mac.replace(':', '').lower()


def mac_oid(mac):
    return '.'.join(str(int(x, 16)) for x in mac.split(':'))


def port_mac(device, port):
    """A made-up burned-in address per port, under the device's own range."""
    base = sum(map(ord, device.name)) % 200
    return f'02:1a:b1:{base:02x}:{(port.if_index >> 8) & 0xff:02x}:{port.if_index & 0xff:02x}'


class Walk:
    def __init__(self):
        self.rows = {}

    def add(self, oid, kind, value):
        self.rows[oid] = (kind, value)

    def counter(self, oid, kind, per_second, initial=0):
        if per_second <= 0:
            self.add(oid, kind, str(int(initial)))
        else:
            wrap = 2 ** 32 if kind == C32 else 2 ** 64
            self.add(oid, kind + NUMERIC, f'initial={int(initial) % wrap},rate={per_second:g},wrap=1')

    def text(self):
        return ''.join(f'{oid}|{kind}|{value}\n' for oid, (kind, value) in sorted(
            self.rows.items(), key=lambda r: oid_key(r[0])))


def system(w, dev):
    w.add('1.3.6.1.2.1.1.1.0', STR, dev.sys_descr)
    w.add('1.3.6.1.2.1.1.2.0', OID, dev.sys_object_id)
    # Up for about three days and counting.
    w.add('1.3.6.1.2.1.1.3.0', TICKS + NUMERIC, 'initial=26000000,rate=100')
    w.add('1.3.6.1.2.1.1.4.0', STR, 'noc@lab.example')
    w.add('1.3.6.1.2.1.1.5.0', STR, dev.hostname if dev.role != 'access' else dev.name)
    w.add('1.3.6.1.2.1.1.6.0', STR, 'Lab hall')
    w.add('1.3.6.1.2.1.1.7.0', INT, '6' if dev.role != 'firewall' else '76')


def interfaces(w, dev):
    w.add('1.3.6.1.2.1.2.1.0', INT, str(len(dev.ports)))
    for p in dev.ports:
        i = p.if_index
        t = '53' if p.kind == 'vlan' else '6'
        bps = p.speed_mbps * 1_000_000
        oper = '1' if p.up else ('2' if p.kind != 'vlan' else '1')
        # Byte rates: about half of the load each way, the player side downloads more.
        down = p.traffic_bps / 8 * 0.8
        up = p.traffic_bps / 8 * 0.2
        w.add(f'1.3.6.1.2.1.2.2.1.1.{i}', INT, str(i))
        w.add(f'1.3.6.1.2.1.2.2.1.2.{i}', STR, p.name)
        w.add(f'1.3.6.1.2.1.2.2.1.3.{i}', INT, t)
        w.add(f'1.3.6.1.2.1.2.2.1.4.{i}', INT, '1500' if p.kind == 'vlan' else '9216')
        w.add(f'1.3.6.1.2.1.2.2.1.5.{i}', G32, str(min(bps, 4294967295)))
        w.add(f'1.3.6.1.2.1.2.2.1.6.{i}', HEX, mac_hex(port_mac(dev, p)))
        w.add(f'1.3.6.1.2.1.2.2.1.7.{i}', INT, '1' if p.admin_up else '2')
        w.add(f'1.3.6.1.2.1.2.2.1.8.{i}', INT, oper)
        w.add(f'1.3.6.1.2.1.2.2.1.9.{i}', TICKS, '1200')
        # The seat side of an access port: ifIn is what the player uploads.
        w.counter(f'1.3.6.1.2.1.2.2.1.10.{i}', C32, up, i * 7919)
        w.counter(f'1.3.6.1.2.1.2.2.1.11.{i}', C32, up / 800, i * 31)
        w.add(f'1.3.6.1.2.1.2.2.1.13.{i}', C32, '0')
        w.counter(f'1.3.6.1.2.1.2.2.1.14.{i}', C32, 3 if p.crc_errors else 0, 1400 if p.crc_errors else 0)
        w.counter(f'1.3.6.1.2.1.2.2.1.16.{i}', C32, down, i * 104729)
        w.counter(f'1.3.6.1.2.1.2.2.1.17.{i}', C32, down / 1200, i * 37)
        w.add(f'1.3.6.1.2.1.2.2.1.19.{i}', C32, '0')
        w.add(f'1.3.6.1.2.1.2.2.1.20.{i}', C32, '0')
        # IF-MIB ifXTable: names, 64-bit counters, the speed in Mbit/s and the description.
        w.add(f'1.3.6.1.2.1.31.1.1.1.1.{i}', STR, _short(p.name))
        w.counter(f'1.3.6.1.2.1.31.1.1.1.6.{i}', C64, up, i * 7919)
        w.counter(f'1.3.6.1.2.1.31.1.1.1.7.{i}', C64, up / 800, i * 31)
        w.counter(f'1.3.6.1.2.1.31.1.1.1.10.{i}', C64, down, i * 104729)
        w.counter(f'1.3.6.1.2.1.31.1.1.1.11.{i}', C64, down / 1200, i * 37)
        w.add(f'1.3.6.1.2.1.31.1.1.1.15.{i}', G32, str(p.speed_mbps))
        w.add(f'1.3.6.1.2.1.31.1.1.1.18.{i}', STR, p.descr)
        if p.kind == 'ethernet':
            # EtherLike-MIB: duplex (2 half, 3 full) and FCS errors, the CRC errors of the CLI.
            w.add(f'1.3.6.1.2.1.10.7.2.1.19.{i}', INT, '2' if p.half_duplex else ('3' if p.up else '1'))
            w.counter(f'1.3.6.1.2.1.10.7.2.1.3.{i}', C32, 3 if p.crc_errors else 0, 1400 if p.crc_errors else 0)


def _short(name):
    """ifName is the short form on Cisco IOS (Gi1/0/1), the long one on NX-OS (Ethernet1/1)."""
    for long, short in (('GigabitEthernet', 'Gi'), ('TenGigabitEthernet', 'Te')):
        if name.startswith(long):
            return short + name[len(long):]
    return name


def entity(w, dev):
    base = '1.3.6.1.2.1.47.1.1.1.1'
    w.add(f'{base}.2.1', STR, f'{dev.model} chassis')
    w.add(f'{base}.5.1', INT, '3')                     # chassis
    w.add(f'{base}.7.1', STR, 'Chassis')
    w.add(f'{base}.11.1', STR, dev.serial)
    w.add(f'{base}.13.1', STR, dev.model)


def health(w, dev):
    if dev.role == 'firewall':
        # HOST-RESOURCES-MIB: one CPU's load and the RAM in 4 KB units.
        w.add('1.3.6.1.2.1.25.3.3.1.2.196608', INT, str(dev.cpu))
        w.add('1.3.6.1.2.1.25.2.3.1.2.1', OID, '1.3.6.1.2.1.25.2.1.2')
        w.add('1.3.6.1.2.1.25.2.3.1.3.1', STR, 'Physical memory')
        w.add('1.3.6.1.2.1.25.2.3.1.4.1', INT, '4096')
        w.add('1.3.6.1.2.1.25.2.3.1.5.1', INT, str((dev.mem_used_mb + dev.mem_free_mb) * 256))
        w.add('1.3.6.1.2.1.25.2.3.1.6.1', INT, str(dev.mem_used_mb * 256))
        return
    # CISCO-PROCESS-MIB: 1- and 5-minute CPU, CISCO-MEMORY-POOL-MIB: the processor pool.
    w.add('1.3.6.1.4.1.9.9.109.1.1.1.1.2.1', INT, '1')
    w.add('1.3.6.1.4.1.9.9.109.1.1.1.1.7.1', G32, str(dev.cpu + 2))
    w.add('1.3.6.1.4.1.9.9.109.1.1.1.1.8.1', G32, str(dev.cpu))
    w.add('1.3.6.1.4.1.9.9.48.1.1.1.2.1', STR, 'Processor')
    w.add('1.3.6.1.4.1.9.9.48.1.1.1.5.1', G32, str(dev.mem_used_mb * 1048576 % 4294967296))
    w.add('1.3.6.1.4.1.9.9.48.1.1.1.6.1', G32, str(dev.mem_free_mb * 1048576 % 4294967296))
    if dev.role == 'core':
        # NX-OS reports temperatures through CISCO-ENTITY-SENSOR-MIB (8 = celsius, 9 = units).
        w.add('1.3.6.1.2.1.47.1.1.1.1.2.21590', STR, 'module-1 FRONT')
        w.add('1.3.6.1.4.1.9.9.91.1.1.1.1.1.21590', INT, '8')
        w.add('1.3.6.1.4.1.9.9.91.1.1.1.1.2.21590', INT, '9')
        w.add('1.3.6.1.4.1.9.9.91.1.1.1.1.3.21590', INT, '0')
        w.add('1.3.6.1.4.1.9.9.91.1.1.1.1.4.21590', INT, str(dev.temp_c))
        w.add('1.3.6.1.4.1.9.9.91.1.1.1.1.5.21590', INT, '1')
    else:
        w.add('1.3.6.1.4.1.9.9.13.1.3.1.2.1', STR, 'Chassis Temperature Sensor')
        w.add('1.3.6.1.4.1.9.9.13.1.3.1.3.1', G32, str(dev.temp_c))
        w.add('1.3.6.1.4.1.9.9.13.1.3.1.4.1', INT, '68')
        w.add('1.3.6.1.4.1.9.9.13.1.3.1.6.1', INT, '1')


def addresses(w, dev, own):
    """ipAddrTable: the device's own addresses, [(ip, ifIndex, mask)]."""
    for ip, if_index, mask in own:
        w.add(f'1.3.6.1.2.1.4.20.1.1.{ip}', IPADDR, ip)
        w.add(f'1.3.6.1.2.1.4.20.1.2.{ip}', INT, str(if_index))
        w.add(f'1.3.6.1.2.1.4.20.1.3.{ip}', IPADDR, mask)


def arp(w, rows):
    """ipNetToMediaTable, [(ifIndex, ip, mac)]: only the core holds one at the event."""
    for if_index, ip, mac in rows:
        idx = f'{if_index}.{ip}'
        w.add(f'1.3.6.1.2.1.4.22.1.1.{idx}', INT, str(if_index))
        w.add(f'1.3.6.1.2.1.4.22.1.2.{idx}', HEX, mac_hex(mac))
        w.add(f'1.3.6.1.2.1.4.22.1.3.{idx}', IPADDR, ip)
        w.add(f'1.3.6.1.2.1.4.22.1.4.{idx}', INT, '3')


def bridge_ports(w, dev):
    """dot1dBasePortIfIndex: bridge port n is the n-th physical port."""
    phys = [p for p in dev.ports if p.kind == 'ethernet']
    w.add('1.3.6.1.2.1.17.1.1.0', HEX, mac_hex(port_mac(dev, phys[0])))
    w.add('1.3.6.1.2.1.17.1.2.0', INT, str(len(phys)))
    numbers = {}
    for n, p in enumerate(phys, start=1):
        w.add(f'1.3.6.1.2.1.17.1.4.1.1.{n}', INT, str(n))
        w.add(f'1.3.6.1.2.1.17.1.4.1.2.{n}', INT, str(p.if_index))
        numbers[p.name] = n
    return numbers


def fdb(w, rows, numbers):
    """dot1dTpFdbTable, [(mac, port name)]: the bridge table of one VLAN context."""
    for mac, port in rows:
        m = mac_oid(mac)
        w.add(f'1.3.6.1.2.1.17.4.3.1.1.{m}', HEX, mac_hex(mac))
        w.add(f'1.3.6.1.2.1.17.4.3.1.2.{m}', INT, str(numbers[port]))
        w.add(f'1.3.6.1.2.1.17.4.3.1.3.{m}', INT, '3')


def qbridge_fdb(w, rows, numbers):
    """dot1qTpFdbPort, [(vlan, mac, port name)]: NX-OS's FDB, all VLANs in one table."""
    for vlan, mac, port in rows:
        w.add(f'1.3.6.1.2.1.17.7.1.2.2.1.2.{vlan}.{mac_oid(mac)}', INT, str(numbers[port]))
        w.add(f'1.3.6.1.2.1.17.7.1.2.2.1.3.{vlan}.{mac_oid(mac)}', INT, '3')


def vlans(w, ids):
    """CISCO-VTP-MIB: which VLANs exist, so a poller knows which public@<vlan> to ask."""
    for vlan in [1, *ids]:
        w.add(f'1.3.6.1.4.1.9.9.46.1.3.1.1.2.1.{vlan}', INT, '1')
        w.add(f'1.3.6.1.4.1.9.9.46.1.3.1.1.4.1.{vlan}', STR, 'default' if vlan == 1 else _vlan(vlan))


def _vlan(vlan):
    return {INFRA_VLAN: 'infra', SERVER_VLAN: 'servers'}.get(vlan) or f'seats-{"ABCD"[vlan - 101]}'


def neighbours(w, dev, numbers):
    """LLDP-MIB on every switch, CISCO-CDP-MIB on the Catalysts and the Nexus."""
    for n, p in enumerate([p for p in dev.ports if p.kind == 'ethernet'], start=1):
        w.add(f'1.0.8802.1.1.2.1.3.7.1.2.{n}', INT, '5')                 # interfaceName
        w.add(f'1.0.8802.1.1.2.1.3.7.1.3.{n}', STR, _short(p.name))
        w.add(f'1.0.8802.1.1.2.1.3.7.1.4.{n}', STR, p.descr or p.name)
    seen = 0
    for p in dev.ports:
        if not p.peer:
            continue
        remote, remote_port = p.peer
        r = LAB.device(remote)
        seen += 1
        local = numbers[p.name]
        idx = f'0.{local}.{seen}'
        w.add(f'1.0.8802.1.1.2.1.4.1.1.4.{idx}', INT, '4')                 # chassis id: MAC
        w.add(f'1.0.8802.1.1.2.1.4.1.1.5.{idx}', HEX, mac_hex(port_mac(r, r.ports[0])))
        w.add(f'1.0.8802.1.1.2.1.4.1.1.6.{idx}', INT, '5')                 # port id: name
        w.add(f'1.0.8802.1.1.2.1.4.1.1.7.{idx}', STR, _short(remote_port))
        w.add(f'1.0.8802.1.1.2.1.4.1.1.8.{idx}', STR, r.port(remote_port).descr)
        w.add(f'1.0.8802.1.1.2.1.4.1.1.9.{idx}', STR, r.hostname if r.role != 'access' else r.name)
        if r.role == 'firewall':
            continue
        cdp = f'{p.if_index}.{seen}'
        w.add(f'1.3.6.1.4.1.9.9.23.1.2.1.1.3.{cdp}', INT, '1')
        w.add(f'1.3.6.1.4.1.9.9.23.1.2.1.1.4.{cdp}', HEX,
              ''.join(f'{int(o):02x}' for o in r.ip.split('.')))
        w.add(f'1.3.6.1.4.1.9.9.23.1.2.1.1.6.{cdp}', STR, r.hostname if r.role != 'access' else r.name)
        w.add(f'1.3.6.1.4.1.9.9.23.1.2.1.1.7.{cdp}', STR, remote_port)
        w.add(f'1.3.6.1.4.1.9.9.23.1.2.1.1.8.{cdp}', STR, f'cisco {r.model}')


def build():
    """{device name: {community: snmprec text}}"""
    out = {}
    core, dist = LAB.core, LAB.dist
    seats_online = [s for s in LAB.seats if s.online]
    fw_macs = LAB.firewall_macs()
    access_uplink = {sw.name: p.name for p in dist.ports if p.peer for sw in LAB.access if p.peer[0] == sw.name}

    # Core: ARP for every seat and server, an SVI per VLAN, the FDB in Q-BRIDGE form.
    w = Walk()
    system(w, core)
    interfaces(w, core)
    entity(w, core)
    health(w, core)
    vlan_index = {int(p.name[4:]): p.if_index for p in core.ports if p.kind == 'vlan'}
    own = [(str(ipaddress.ip_network(f'10.10.{v}.0/24').network_address + 1), idx, '255.255.255.0')
           for v, idx in vlan_index.items()]
    own.append((core.ip, core.port('mgmt0').if_index, '255.255.255.0'))
    addresses(w, core, own)
    rows = [(vlan_index[s.vlan], s.ip, s.mac) for s in seats_online]
    rows += [(vlan_index[SERVER_VLAN], ip, mac) for ip, mac, _ in LAB.servers()]
    rows += [(vlan_index[INFRA_VLAN], f'10.10.{INFRA_VLAN}.{40 + n}', mac)
             for n, mac in enumerate(fw_macs.values(), start=1)]
    arp(w, rows)
    numbers = bridge_ports(w, core)
    q = [(s.vlan, s.mac, 'Ethernet1/49') for s in seats_online]
    q += [(SERVER_VLAN, mac, 'Ethernet1/10') for _, mac, _ in LAB.servers()]
    q += [(INFRA_VLAN, mac, f'Ethernet1/{n}') for n, mac in enumerate(fw_macs.values(), start=1)]
    qbridge_fdb(w, q, numbers)
    neighbours(w, core, numbers)
    out[core.name] = {'public': w.text()}

    # Distribution: no ARP (only the core routes), FDB per VLAN context.
    w = Walk()
    system(w, dist)
    interfaces(w, dist)
    entity(w, dist)
    health(w, dist)
    addresses(w, dist, [(dist.ip, dist.port(f'Vlan{INFRA_VLAN}').if_index, '255.255.255.0')])
    numbers = bridge_ports(w, dist)
    vlans(w, [INFRA_VLAN, *SEAT_VLAN.values()])
    neighbours(w, dist, numbers)
    out[dist.name] = {'public': w.text()}
    for block, vlan in SEAT_VLAN.items():
        v = Walk()
        bridge_ports(v, dist)
        rows = [(s.mac, access_uplink[s.switch]) for s in seats_online if s.block == block]
        rows.append((core.router_mac, 'TenGigabitEthernet1/1'))
        fdb(v, rows, numbers)
        out[dist.name][f'public@{vlan}'] = v.text()

    # Access: MAC tables only. The seat's MAC on its port, the gateway and firewalls on the uplink.
    for sw in LAB.access:
        w = Walk()
        system(w, sw)
        interfaces(w, sw)
        entity(w, sw)
        health(w, sw)
        addresses(w, sw, [(sw.ip, sw.port('Vlan1').if_index, '255.255.255.0')])
        numbers = bridge_ports(w, sw)
        block = 'ABCD'[LAB.access.index(sw)]
        vlans(w, [SEAT_VLAN[block]])
        neighbours(w, sw, numbers)
        out[sw.name] = {'public': w.text()}
        v = Walk()
        bridge_ports(v, sw)
        rows = [(s.mac, s.port) for s in seats_online if s.switch == sw.name]
        rows += [(core.router_mac, 'GigabitEthernet1/0/49')]
        fdb(v, rows, numbers)
        out[sw.name][f'public@{SEAT_VLAN[block]}'] = v.text()

    for fw in LAB.firewalls:
        w = Walk()
        system(w, fw)
        interfaces(w, fw)
        health(w, fw)
        out[fw.name] = {'public': w.text()}
    return out


def seats_json():
    """lab/fixtures/seats.json: Floorbit's JSON seat source, who sits where with which IP."""
    seats = {s.label: {'username': s.nick, 'ip_address': s.ip} for s in LAB.seats if s.online}
    return json.dumps({'_metadata': {'description': 'The lab event (lab/topology.py); made-up players.'},
                       'seats': seats}, indent=1) + '\n'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--check', action='store_true', help='fail when lab/snmp/data is out of date')
    args = p.parse_args(argv)
    stale = []
    outputs = {OUT / device / f'{community}.snmprec': text
               for device, files in build().items() for community, text in files.items()}
    outputs[FIXTURES / 'seats.json'] = seats_json()
    for path, text in outputs.items():
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if stale:
        print('Out of date, run python3 lab/snmp/build.py:\n  ' + '\n  '.join(stale))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
