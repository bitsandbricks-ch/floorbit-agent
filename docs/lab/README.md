# The test lab

A hosted Floorbit, two agents and the event network around them, with no event hardware: so the
crew's first hour at the event is a short check, not debugging. Everything here is made up:
locally administered MACs (`02:…`), lab addresses, players called `player-a07`.

## What it simulates

The lab event is one file, [`lab/topology.py`](../../lab/topology.py). Everything else is
generated from it:

| Part | Stands in for | How |
| --- | --- | --- |
| `core-01` | Nexus core (holds the ARP table, routes the seat VLANs) | snmpsim, NX-OS ifIndexes, Q-BRIDGE FDB |
| `dist-01` | Catalyst 4500 distribution (no ARP) | snmpsim, per-VLAN FDB (`public@<vlan>`) |
| `access-a1` … `access-d1` | Catalyst 2960-X access, 24 seats each | snmpsim, built from the MIBs (the real ones aren't running yet) |
| `fw-01`, `fw-02` | OPNsense firewalls | snmpsim, filterlog syslog, a Telegraf push sample |
| `lancache-01` | The LAN cache box, also the DNS server | its access log and the DNS query log |
| `librenms` | LibreNMS | [`lab/librenms_lite`](../../lab/librenms_lite/server.py): polls the simulated devices over SNMP and answers the API calls Floorbit makes |
| `agent-a`, `agent-b` | Two agents at the event | this repository's image |
| `floorbit` | The hosted Floorbit | built from a Floorbit checkout |

96 seats in four blocks of 24, on VLANs 101 to 104. A few are faulty on purpose: B-3 runs half
duplex, C-9 counts CRC errors, D-12 is shut, and a handful are offline. The devices send syslog
(link flaps, MAC flaps, BPDU guard, NX-OS speed changes, firewall filter lines), linkDown/linkUp
traps and NetFlow v9 from the core, on a loop ([`lab/replay`](../../lab/replay/replay.py)).

`python3 lab/snmp/build.py` writes the SNMP data and the seat list (`lab/fixtures/seats.json`);
the unit tests fail when someone changes `topology.py` and forgets to run it.

## Run it

With Docker, from this repository with a Floorbit checkout next to it:

```sh
docker compose -f lab/compose.yml up -d --build --wait
docker compose -f lab/compose.yml run --rm e2e        # the end-to-end tests
# http://localhost:8080, admin / lab-admin-password
docker compose -f lab/compose.yml down -v
```

Failover: `docker compose -f lab/compose.yml stop agent-a`, then
`docker compose -f lab/compose.yml run --rm -e LAB_AGENT_DOWN=agent-a e2e`.

Without Docker (a laptop or a sandbox), every part as a process on 127.0.0.1; needs redis-server,
net-snmp's tools, snmpsim, gunicorn and Floorbit's requirements:

```sh
FLOORBIT_SRC=../floorbit lab/run-local.sh start
lab/run-local.sh test
lab/run-local.sh stop-agent agent-a && LAB_AGENT_DOWN=agent-a lab/run-local.sh test
lab/run-local.sh stop
```

CI runs the Docker lab on every pull request (the `lab` job), normal run and failover.

## What the tests check

[`lab/e2e/test_lab.py`](../../lab/e2e/test_lab.py), against the running lab:

- both agents connected and on the current protocol; monitoring goes through them;
- every switch online with the right port counts, and its temperature;
- every online seat resolves to its own access port: IP to MAC on the core's ARP, MAC to port on
  the access switch's FDB, not the uplinks where the core and distribution see it too;
- port faults show on the seat (half duplex, CRC errors, the shut port);
- a player checks in, and the seat learns its switch port and MAC; history and search by IP,
  MAC and nick;
- syslog from every kind of device arrives once, though both agents send it;
- the NetFlow summary, the captive portal note, the agents' metrics;
- failover: with agent A stopped, lookups and syslog go on through agent B.

The LAN cache numbers and device telemetry are skipped until Floorbit has them
(bitsandbricks-ch/floorbit#660, #661); recorded gNMI and Cisco MDT streams for Telegraf come with
the telemetry plugin.

## Real samples

The lab is only as good as its guess at the real gear. [capture.md](capture.md) lists what to
take from the running core, distribution, firewalls and cache box (read only), and
[`lab/tools/anonymise.py`](../../lab/tools/anonymise.py) strips them before they are shared.
Anonymised walks then replace the generated ones for those devices.

[virtual-devices.md](virtual-devices.md): running real network operating systems in a VM
instead, when a simulation isn't close enough.
