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
| `fw-01`, `fw-02` | OPNsense firewalls | snmpsim, filterlog syslog, their own Telegraf's push |
| `lancache-01` | The LAN cache box, also the DNS server | its access log and the DNS query log |
| `librenms` | LibreNMS | [`lab/librenms_lite`](../../lab/librenms_lite/server.py): polls the simulated devices over SNMP and answers the API calls Floorbit makes |
| `agent-a`, `agent-b` | Two agents at the event | this repository's image |
| `floorbit` | The hosted Floorbit | Floorbit's image, or built from a Floorbit checkout |

96 seats in four blocks of 24, on VLANs 101 to 104. A few are faulty on purpose: B-3 runs half
duplex, C-9 counts CRC errors, D-12 is shut, and a handful are offline. The devices send syslog
(link flaps, MAC flaps, BPDU guard, NX-OS speed changes, firewall filter lines), linkDown/linkUp
traps and NetFlow v9 from the core, on a loop ([`lab/replay`](../../lab/replay/replay.py)).

Every switch also answers SSH on port 22, as Catalyst IOS (the access and distribution switches)
or NX-OS (the core), with the login `lab` / `lab-switch-password`
([`lab/switch_ssh`](../../lab/switch_ssh/server.py)). It knows what Floorbit sends a switch and
what a tech types, with IOS's abbreviations: `show running-config`, `show interfaces status`,
`show interface Gi1/0/5`, `show mac address-table`, `show version`, `conf t` > `int gi1/0/5` >
`shut` / `no shut` / `description`, `clear counters` (it asks `[confirm]`, as IOS does),
`write memory`, and the pager until `terminal length 0`. A shut port does what it does at the
event: its SNMP says so (LibreNMS and Telegraf see it), the switch logs `%LINK-5-CHANGED` and
`%SYS-5-CONFIG_I` and sends a linkDown trap, and the running config has `shutdown` under it.
Without `write memory`, a restarted switch comes back with its last saved config.

`python3 lab/snmp/build.py` writes the SNMP data and the seat list (`lab/fixtures/seats.json`);
the unit tests fail when someone changes `topology.py` and forgets to run it.

## Run it

With Docker, from this repository. Every image comes ready-made from GHCR, so nothing is built and
no Floorbit checkout is needed:

```sh
docker compose -f lab/compose.yml up -d --wait
docker compose -f lab/compose.yml run --rm e2e        # the end-to-end tests
# http://localhost:8080, admin / lab-admin-password
docker compose -f lab/compose.yml down -v
```

| Image | Built by | Tags | Platforms |
| --- | --- | --- | --- |
| `ghcr.io/bitsandbricks-ch/floorbit-lab` | this repository, `lab/Dockerfile` (the `Lab image` workflow) | `latest`, the short commit | amd64, arm64 |
| `ghcr.io/bitsandbricks-ch/floorbit-agent` | this repository, `Dockerfile` (the `Image` workflow) | `latest`, the short commit, the version | amd64 |
| `ghcr.io/bitsandbricks-ch/floorbit` | bitsandbricks-ch/floorbit | `latest`, the short commit, the version | amd64 |

On a Mac with an Apple chip Docker Desktop runs the amd64 images emulated, a little slower.

`latest` is the newest from each repository's main branch; `docker compose -f lab/compose.yml pull`
fetches it again. `LAB_IMAGE`, `LAB_AGENT_IMAGE` and `LAB_FLOORBIT_IMAGE` pick another tag.

When you change `lab/` or the agent, build instead, with a Floorbit checkout next to this
repository (or `LAB_FLOORBIT_SRC` pointing at one), as CI does:

```sh
docker compose -f lab/compose.yml up -d --build --wait
```

One simulated device on its own, say to point a Telegraf or a LibreNMS of your own at it
(SNMP v2c, community `public`; `DEVICE` is any device in `lab/topology.py`):

```sh
docker run --rm -p 1161:161/udp -e DEVICE=access-a1 ghcr.io/bitsandbricks-ch/floorbit-lab sh /lab/device.sh
snmpwalk -v2c -c public 127.0.0.1:1161 1.3.6.1.2.1.31.1.1.1.1    # ifName
```

Failover: `docker compose -f lab/compose.yml stop agent-a`, then
`docker compose -f lab/compose.yml run --rm -e LAB_AGENT_DOWN=agent-a e2e`. The other bad days
are under [Drills](#drills).

### Your own agent with the simulated hall

[`lab/senders.yml`](../../lab/senders.yml) runs an agent of yours with every simulated device
around it, for a Floorbit you test with (it gets made-up devices and players). The agent connects
to your Floorbit as at an event; the devices keep their lab addresses and send it syslog (5514),
traps (1162), NetFlow (2055), the core's gNMI/MDT stream and the firewalls' push (8186, with the
agent's own ingest token), and answer SNMP (v2c, `public`):

```sh
cp agent.env.example lab/agent.env       # FLOORBIT_URL and FLOORBIT_AGENT_KEY
docker compose -f lab/senders.yml up -d
```

The switches answer SSH there too (`lab` / `lab-switch-password`), for SSH port actions, switch
commands, config backups and the terminal from your Floorbit.

Then, in Floorbit: turn on the syslog (port 5514), NetFlow (port 2055) and Telemetry plugins, and
add the devices to the inventory with the addresses in `lab/senders.yml`, in a group with Telemetry
on and the SNMP v2c community `public`. What each device needs on top (`telemetry.drivers`,
`telemetry.mdt.node`, `telemetry.snmp.vlans`) is what `telemetry()` in `lab/e2e/setup.py` sets.

Without Docker (a laptop or a sandbox), every part as a process on 127.0.0.1; needs redis-server,
net-snmp's tools, snmpsim with pysmi (`pip install snmpsim==1.2.2 pysmi==2.0.0`, as in `lab/Dockerfile`:
snmpsim imports pysmi without declaring it), gunicorn and Floorbit's requirements:

```sh
FLOORBIT_SRC=../floorbit lab/run-local.sh start
lab/run-local.sh test
lab/run-local.sh stop-agent agent-a && LAB_AGENT_DOWN=agent-a lab/run-local.sh test
lab/run-local.sh stop
```

`lab/run-local.sh down <part>...` and `up <part>...` stop and start a device (by its name),
`librenms` or `floorbit`, for the drills.

The switches' SSH needs their lab addresses on the machine, since the agent logs in to private
addresses only: add them once (`sudo ip addr add 172.31.10.11/32 dev lo`, and `.12`, `.20` to
`.23`) and start with `LAB_SSH=1`. They answer on port 2222; without it the SSH tests skip.

Plugin settings are the same for every agent, so on one machine only agent A runs syslog, NetFlow
the check-in note and the front door (they would take the same ports); the failover's syslog test needs the
Docker lab.

The setup step (`lab/e2e/setup.py`) sets up the agents' plugins in Floorbit, as the crew would at
an event: the lab LibreNMS, syslog and NetFlow from the hall, the check-in note and the attendees'
front door (check-in takes the seat PC's address only from there). It draws the hall, plans a seat
range per block on the firewalls (blocks A and B on fw-01, C and D on fw-02, each with the other as
its backup), and sets every device up for telemetry in the inventory. The agents get only
their URL and key.

CI runs the Docker lab on every pull request (the `lab` job), normal run, the drills and failover,
built from the branch. After a merge to main, the `Lab image` workflow pushes the lab image and runs the lab once
more from the pulled images only.

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
- the NetFlow summary, the note on phones, the agents' metrics;
- failover: with agent A stopped, lookups and syslog go on through agent B;
- SSH to the switches through the agents, with the login stored in Floorbit's inventory
  ([`lab/e2e/test_switch_ssh.py`](../../lab/e2e/test_switch_ssh.py)): the show templates on IOS
  and NX-OS, a config backup without the switch's secrets, a seat port shut (the switch's log,
  LibreNMS, the next backup and its diff see it) and turned on again, a bounce, Clear port counters,
  and the browser terminal with the pager;
- telemetry: every device reports to agent A's collector; Telegraf's SNMP polls show a seat
  port's traffic, the shut port and the distribution's CPU; its MAC tables and the core's ARP feed
  the IP and MAC history; a firewall's own Telegraf push lands on that firewall;
- in a real browser ([`lab/e2e/test_attendee_browser.py`](../../lab/e2e/test_attendee_browser.py),
  Playwright and Chromium; skipped without): a fresh agent answers attendees on one port with nothing
  set by hand; a seat PC checks in through the front door and the note on phones goes away; the crew
  signs in at the front door, picks a row on the seat walk and walks a seat without leaving the page;
  and no agent logged a stack trace. CI runs it from the runner and keeps the screenshots
  (`lab-browser-screenshots`). Locally: `pip install playwright`, `playwright install chromium`, then
  `LAB_SHOTS=/tmp/shots lab/run-local.sh test lab/e2e/test_attendee_browser.py`.

The LAN cache numbers are skipped until Floorbit has them (bitsandbricks-ch/floorbit#660).

## Drills

Bad days, in [`lab/e2e/test_drills.py`](../../lab/e2e/test_drills.py): the lab stops a part, the
tests check what the crew sees, and it comes back. Each runs on its own with `LAB_DRILL`, in this
order (as CI's `lab` job runs them):

| `LAB_DRILL` | Stopped first | Checked |
| --- | --- | --- |
| `devices-down` | `fw-01`, `access-c1` (`LAB_DOWN`) | fw-01's ranges move to fw-02 and the plan's firewall layer marks their seats; access-c1 is offline and on the Problems page; the rest of the hall carries on |
| `recovered` | (started again) | every range back on its own firewall; access-c1 online and its seats resolving |
| `librenms-down` | `librenms` | Floorbit says monitoring is away; not hearing about a firewall moves no range; the pages answer and Telegraf's numbers keep coming |
| `hosted-down` | `floorbit` | the check-in note keeps its last answer; the front door says Floorbit is away; the agents notice and keep the syslog on disk |
| `hosted-back` | (started again, `LAB_HOSTED_AWAY="from-to"`) | the syslog from the outage arrives with its own times; the agents' backlog drains |

```sh
docker compose -f lab/compose.yml stop fw-01 access-c1
docker compose -f lab/compose.yml run --rm -e LAB_DRILL=devices-down e2e
docker compose -f lab/compose.yml start fw-01 access-c1
docker compose -f lab/compose.yml run --rm -e LAB_DRILL=recovered e2e
```

A device down or back shows after two status polls in a row: the lab's Floorbit polls every 15 s
(`STATUS_MONITOR_INTERVAL`), an event's every 60 s by default, so there it takes up to about three
minutes.

### High availability: failover times

[`lab/ha_drill.py`](../../lab/ha_drill.py) times what the crew notices when one of the two agents
goes away (Floorbit issues #1405 and #1441). It runs on the machine that runs the lab, since it
stops and starts the containers, and CI's `lab` job runs it after the other drills, with the two
agents splitting the devices between them. By hand:

```sh
docker compose -f lab/compose.yml up -d --wait
python3 lab/ha_drill.py --report ha-drill.md --json ha-drill.json
```

| Step | What it does | Checked (budget) |
| --- | --- | --- |
| the primary is killed | `docker compose kill` (the machine dies) | the other agent is primary on the Local agents page (30 s), a seat lookup answers (45 s), new syslog arrives (60 s), the other agent's front door answers (5 s) |
| it comes back | `start` | it reconnects and does **not** take the lease back for a minute |
| the standby stops for 30 s | `stop`, `start` | the primary stays where it is |
| the hosted Floorbit is away for 60 s | `stop floorbit`, `start` | both agents reconnect, nobody changes role |
| the primary is stopped cleanly | `stop` | the other agent is primary, lookups answer |

Through every step it samples the group twice a second, and it fails if the group ever shows two
primaries. The report is one Markdown table of the measured seconds. VRRP's own times (a VIP moving
in 1 to 4 s) come from `sudo python scripts/vrrp_check.py` ([docs/vrrp.md](../vrrp.md)): the lab's
agents share one Docker network without VRRP, so the drill measures Floorbit's lease and reaches
each agent's front door at its own address, as the "both addresses in DNS" setup does.

## Telemetry

Setup turns on Floorbit's Telemetry plugin and sets every lab device up for it in the inventory, as
the crew would: the Telemetry feature and an SNMP v2c login for all devices, MAC tables per seat VLAN on the Catalysts, Cisco
model-driven telemetry by node name on the core, the firewalls' own push. Only agent A collects
(without VRRP both agents would count as the collector), and the Telegraf inside agent A's image
runs the config it writes, as at an event; the devices send their traps to agent A. The core's replay also streams gNMI and Cisco MDT counters for its ports, and the
firewalls push CPU, memory and pf states, both to agent A's port 8186 with its ingest token.
Telemetry's "Who polls the switches" stays on Auto, as at an event with LibreNMS: LibreNMS polls the
ports and tables, Telemetry keeps traps, facts and streams. The Telegraf polling tests switch it to
Telemetry while they run, so both paths are tested.

`lab/run-local.sh` hands the agents the Telegraf on the PATH (or `TELEGRAF=/path/to/telegraf`) to run
themselves; without it the tests that need it skip. Traps stay off locally: every device is 127.0.0.1 there, so
the agent couldn't tell them apart.

## Real samples

The lab is only as good as its guess at the real gear. [capture.md](capture.md) lists what to
take from the running core, distribution, firewalls and cache box (read only), and
[`lab/tools/anonymise.py`](../../lab/tools/anonymise.py) strips them before they are shared.
Anonymised walks then replace the generated ones for those devices.

[virtual-devices.md](virtual-devices.md): running real network operating systems in a VM
instead, when a simulation isn't close enough.
