"""The lab's made-up event: every switch, port, seat, MAC and IP, in one place.

Everything else in the lab is generated from this (the SNMP data, the replayed logs and flows,
the end-to-end expectations), so a seat's MAC is the same in the core's ARP table, the access
switch's FDB, the syslog lines and the flows. Nothing here is real: names under lab.example,
private IPs, locally administered MACs (02:...).

The hardware follows the event's (docs/lab/README.md): a Nexus core that holds the ARP table, a
Catalyst 4500 distribution that holds none, Catalyst 2960-X access switches (MAC tables only) and
OPNsense firewalls. Model details the event hasn't confirmed yet are marked TODO; the capture
checklist (docs/lab/capture.md) settles them.
"""
from dataclasses import dataclass, field

DOMAIN = 'lab.example'
BLOCKS = 'ABCD'
SEATS_PER_BLOCK = 24
SEAT_VLAN = {b: 101 + i for i, b in enumerate(BLOCKS)}   # one VLAN and /24 per seat block
INFRA_VLAN = 100
SERVER_VLAN = 200


@dataclass
class Port:
    name: str
    if_index: int
    speed_mbps: int
    descr: str = ''
    up: bool = True
    admin_up: bool = True
    half_duplex: bool = False
    crc_errors: bool = False
    kind: str = 'ethernet'          # ethernet | vlan | mgmt
    # (remote device, remote port) for LLDP/CDP and uplink checks
    peer: tuple | None = None
    traffic_bps: int = 0            # rough steady rate; the simulator turns it into moving counters


@dataclass
class Device:
    name: str
    role: str                       # core | distribution | access | firewall
    ip: str                         # management address on the lab network (compose.yml)
    sys_descr: str
    sys_object_id: str
    model: str
    serial: str
    ports: list = field(default_factory=list)
    cpu: int = 10
    mem_used_mb: int = 400
    mem_free_mb: int = 600
    temp_c: int = 35
    router_mac: str = ''

    @property
    def hostname(self):
        return f'{self.name}.{DOMAIN}'

    def port(self, name):
        return next(p for p in self.ports if p.name == name)


@dataclass
class Seat:
    label: str                      # A-7, as Floorbit's plan names seats
    block: str
    number: int
    switch: str
    port: str
    vlan: int
    ip: str
    mac: str
    nick: str
    online: bool


def seat_mac(block, number):
    return f'02:1a:b0:{BLOCKS.index(block) + 10:02x}:00:{number:02x}'


def _nexus(name, ip, mac):
    # NX-OS: Ethernet ports have ifIndex 0x1a000000 + (slot-1)<<..., one 4096 step per port;
    # SVIs 0x9010000 + VLAN; mgmt0 0x5000000. LibreNMS and the CLI agree on these.
    ports = [Port(f'Ethernet1/{n}', 436207616 + (n - 1) * 4096, 100000 if n > 48 else 25000, up=False)
             for n in range(1, 55)]
    ports.append(Port('mgmt0', 83886080, 1000, kind='mgmt'))
    for vlan in (INFRA_VLAN, *SEAT_VLAN.values(), SERVER_VLAN):
        ports.append(Port(f'Vlan{vlan}', 151060480 + vlan, 10000, kind='vlan', descr=_vlan_name(vlan)))
    # TODO(capture): the core's model and NX-OS version are not known yet; this is a Nexus 9300.
    return Device(name, 'core', ip,
                  'Cisco NX-OS(tm) nxos.9.3.12.bin, Software (nxos), Version 9.3(12), RELEASE SOFTWARE '
                  'Copyright (c) 2002-2023 by Cisco Systems, Inc.',
                  '1.3.6.1.4.1.9.12.3.1.3.1812', 'N9K-C93180YC-FX', 'LAB0CORE001', ports,
                  cpu=12, mem_used_mb=6900, mem_free_mb=9100, temp_c=41, router_mac=mac)


def _cat4500(name, ip):
    ports = [Port(f'TenGigabitEthernet1/{n}', n, 10000, up=False) for n in range(1, 9)]
    ports += [Port(f'GigabitEthernet3/{n}', 100 + n, 1000, up=False) for n in range(1, 49)]
    ports.append(Port(f'Vlan{INFRA_VLAN}', 300 + INFRA_VLAN, 1000, kind='vlan', descr=_vlan_name(INFRA_VLAN)))
    # TODO(capture): supervisor and IOS-XE version unknown; a 4507R+E with a Sup8-E here.
    return Device(name, 'distribution', ip,
                  'Cisco IOS Software, IOS-XE Software, Catalyst 4500 L3 Switch  Software (cat4500es8-'
                  'UNIVERSALK9-M), Version 03.11.07.E RELEASE SOFTWARE (fc1)',
                  '1.3.6.1.4.1.9.1.1653', 'WS-C4507R+E', 'LAB0DIST001', ports,
                  cpu=7, mem_used_mb=900, mem_free_mb=3100, temp_c=38)


def _c2960x(name, ip):
    # Stack member 1: Gi1/0/1-48 are 10101-10148, the four SFP uplinks 10149-10152.
    ports = [Port(f'GigabitEthernet1/0/{n}', 10100 + n, 1000, up=False) for n in range(1, 53)]
    ports.append(Port('Vlan1', 1, 1000, kind='vlan'))
    return Device(name, 'access', ip,
                  'Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E9, '
                  'RELEASE SOFTWARE (fc1)',
                  '1.3.6.1.4.1.9.1.1208', 'WS-C2960X-48TS-L', f'LAB0{name[-2:].upper()}00001', ports,
                  cpu=6, mem_used_mb=180, mem_free_mb=90, temp_c=34)


def _opnsense(name, ip):
    ports = [Port('igb0', 1, 1000, descr='uplink'), Port('igb1', 2, 1000, descr='lan')]
    # TODO(capture): which firewall product and version the event runs; OPNsense here.
    return Device(name, 'firewall', ip, 'OPNsense 24.7 FreeBSD 14.1-RELEASE-p3 amd64',
                  '1.3.6.1.4.1.12325.1.1.2.1.1', 'OPNsense', f'LAB0{name.upper().replace("-", "")}', ports,
                  cpu=4, mem_used_mb=1200, mem_free_mb=6800, temp_c=45)


def _vlan_name(vlan):
    if vlan == INFRA_VLAN:
        return 'infra'
    if vlan == SERVER_VLAN:
        return 'servers'
    return f'seats-{BLOCKS[vlan - 101]}'


def _link(a, a_port, b, b_port, bps=0):
    pa, pb = a.port(a_port), b.port(b_port)
    pa.up = pb.up = True
    pa.peer, pb.peer = (b.name, b_port), (a.name, a_port)
    # Both ends say "uplink": Floorbit's uplink pattern (STATS_UPLINK_PATTERN) keeps them out of
    # the seat lookup, as the event's descriptions should.
    pa.descr = f'uplink {b.name} {b_port}'
    pb.descr = f'uplink {a.name} {a_port}'
    pa.traffic_bps = pb.traffic_bps = bps


def build():
    core = _nexus('core-01', '172.31.10.11', '00:22:bd:f8:19:ff')
    dist = _cat4500('dist-01', '172.31.10.12')
    access = [_c2960x(f'access-{b.lower()}1', f'172.31.10.{20 + i}') for i, b in enumerate(BLOCKS)]
    fws = [_opnsense(f'fw-0{n}', f'172.31.10.{40 + n}') for n in (1, 2)]

    _link(core, 'Ethernet1/49', dist, 'TenGigabitEthernet1/1', 2_400_000_000)
    for i, sw in enumerate(access):
        _link(dist, f'GigabitEthernet3/{i + 1}', sw, 'GigabitEthernet1/0/49', 450_000_000)
    for n, fw in enumerate(fws, start=1):
        p = core.port(f'Ethernet1/{n}')
        p.up, p.peer, p.descr, p.speed_mbps = True, (fw.name, 'igb1'), f'{fw.name} lan', 1000
        p.traffic_bps = 900_000_000
    cache = core.port('Ethernet1/10')
    cache.up, cache.descr, cache.speed_mbps, cache.traffic_bps = True, 'lancache-01', 10000, 3_000_000_000

    seats = []
    for b, sw in zip(BLOCKS, access, strict=True):
        for n in range(1, SEATS_PER_BLOCK + 1):
            port = sw.port(f'GigabitEthernet1/0/{n}')
            # Most seats are taken; a few are empty (port down), one in each block came late.
            online = n not in (5, 17, 23)
            port.up = online
            port.descr = f'seat {b}-{n}'
            port.traffic_bps = (40_000_000 if n % 7 else 380_000_000) if online else 0
            seats.append(Seat(f'{b}-{n}', b, n, sw.name, port.name, SEAT_VLAN[b],
                              f'10.10.{SEAT_VLAN[b]}.{10 + n}', seat_mac(b, n), f'player-{b.lower()}{n:02d}',
                              online))
    # The faults the floor plan should show: one seat on half duplex, one with CRC errors, one
    # port shut by the crew.
    access[1].port('GigabitEthernet1/0/3').half_duplex = True
    access[2].port('GigabitEthernet1/0/9').crc_errors = True
    shut = access[3].port('GigabitEthernet1/0/12')
    shut.admin_up = shut.up = False
    shut.traffic_bps = 0
    next(s for s in seats if s.label == 'D-12').online = False    # its MAC aged out of the tables
    return Lab(core, dist, access, fws, seats)


@dataclass
class Lab:
    core: Device
    dist: Device
    access: list
    firewalls: list
    seats: list

    @property
    def switches(self):
        return [self.core, self.dist, *self.access]

    @property
    def devices(self):
        return [*self.switches, *self.firewalls]

    def device(self, name):
        return next(d for d in self.devices if d.name == name)

    def seat(self, label):
        return next(s for s in self.seats if s.label == label)

    def servers(self):
        """Hosts on the server VLAN, as the core's ARP table knows them."""
        return [('10.10.200.10', '02:1a:b0:c0:00:10', 'lancache-01'),
                ('10.10.200.11', '02:1a:b0:c0:00:11', 'dns-01'),
                ('10.10.200.20', '02:1a:b0:c0:00:20', 'game-01')]

    def firewall_macs(self):
        return {fw.name: f'02:1a:b0:f0:00:{n:02x}' for n, fw in enumerate(self.firewalls, start=1)}


LAB = build()


# A Catalyst 9800-CL wireless controller that streams its access points over MDT (Floorbit's
# docs/telemetry.md, "Wireless controllers"). Not an SNMP device here: only its stream is replayed.
# TODO(capture): the event's controller is probably a 9800-CL; its AP names and models are made up.
@dataclass
class AccessPoint:
    name: str
    mac: str
    model: str
    radios: list          # (slot, band enum, channel, width MHz, channel use %, clients)


@dataclass
class Controller:
    name: str
    ip: str
    model: str
    aps: list


WLC = Controller('wlc-01', '172.31.10.30', 'C9800-CL-K9', [
    AccessPoint('ap-hall-a', '02:1a:b0:a0:00:01', 'C9130AXI-E',
                [(0, 'dot11-2-dot-4-ghz-band', 1, 20, 34, 6), (1, 'dot11-5-ghz-band', 36, 80, 52, 21)]),
    AccessPoint('ap-hall-b', '02:1a:b0:a0:00:02', 'C9130AXI-E',
                [(0, 'dot11-2-dot-4-ghz-band', 6, 20, 41, 9), (1, 'dot11-5-ghz-band', 52, 80, 77, 38)]),
    AccessPoint('ap-crew', '02:1a:b0:a0:00:03', 'C9120AXI-E',
                [(1, 'dot11-5-ghz-band', 149, 40, 12, 3)]),
])
