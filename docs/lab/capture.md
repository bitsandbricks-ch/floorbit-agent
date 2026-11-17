# What to capture from the event's gear

The [test lab](README.md) runs Floorbit and the agent against simulated switches, firewalls and a
LAN cache. It's only as good as its samples. This list says what to take from the gear that is
**running now**, so the lab looks like the real event and the on-site shakedown stays short.

The access switches (Catalyst 2960-X) aren't running, so the lab builds them from the MIBs
(synthetic walks with real port counts). Skip them here. Everything below is for the core (Nexus),
the distribution (Catalyst 4500), the firewalls and the LAN cache box, **if they are up**. Take what
you can; every item stands on its own.

## Ground rules

- **Read-only.** Every command here only shows or walks. Don't enable a feature, add an SNMP view,
  point syslog somewhere new or change a config to get a sample. If something below needs that
  (gNMI, query logging), it says so: skip it unless the crew agrees.
- **Gentle.** SNMP walks are spread out (`-Cr20`, a pause between subtrees) so the switch's control
  plane doesn't notice. Walk one device at a time.
- **No secrets.** None of these commands print passwords, keys or SNMP communities. Don't add
  `show running-config` or `show snmp host`: both print communities. Type the community into the
  prompt (`read -s`), not the command line, so it stays out of your shell history.
- **Anonymise before it goes anywhere** (see the end). Raw files stay on your machine.

On the laptop (Linux or macOS) you need net-snmp's `snmpbulkwalk` (`apt install snmp` or
`brew install net-snmp`) and, for gNMI only, [gnmic](https://gnmic.openconfig.net).

## 1. Core (Nexus)

Holds the ARP table, so it matters most. From the CLI (SSH, a read-only account is enough), each
into its own file. The `| json` forms are exactly what NX-API answers, so they double as NX-API
fixtures.

```text
show version
show version | json
show inventory
show module
show feature
show interface brief
show interface | json
show interface counters errors
show lldp neighbors detail | json
show cdp neighbors detail
show vlan brief
show ip arp summary vrf all
show ip arp vrf all | json
show mac address-table count
show mac address-table | json
show system resources | json
show environment | json
show vpc brief
show hsrp brief
show vrrp
show snmp trap
show running-config telemetry
show telemetry control database
show flow exporter
show flow record
show flow monitor
show sflow
show logging last 300
show clock
```

Some of these answer "invalid command" or "feature not enabled" (vPC, HSRP, telemetry, NetFlow):
that answer is useful too, keep it.

SNMP, from the laptop, on the management network:

```sh
read -p "Host: " H; read -s -p "Community: " C; echo
OIDS="1.3.6.1.2.1.1 1.3.6.1.2.1.2 1.3.6.1.2.1.31 1.3.6.1.2.1.47 1.3.6.1.2.1.4.20 1.3.6.1.2.1.4.22
1.3.6.1.2.1.4.35 1.3.6.1.2.1.17 1.0.8802.1.1.2 1.3.6.1.4.1.9.9.23 1.3.6.1.4.1.9.9.46
1.3.6.1.2.1.10.7 1.3.6.1.4.1.9.9.109 1.3.6.1.4.1.9.9.48 1.3.6.1.4.1.9.9.221 1.3.6.1.2.1.99
1.3.6.1.4.1.9.9.91 1.3.6.1.4.1.9.9.13 1.3.6.1.4.1.9.9.117 1.3.6.1.2.1.68 1.3.6.1.4.1.9.9.106"
for oid in $OIDS; do
  snmpbulkwalk -v2c -c "$C" -ObentU -Cr20 -t 5 "$H" "$oid"; sleep 1
done > core.snmpwalk 2> core.snmpwalk.err
```

That's system, interfaces, entities, IP addresses and ARP, bridge and FDB, LLDP, CDP, VLANs,
Ethernet errors, CPU, memory, sensors, fans and power, VRRP and HSRP. `-ObentU` is the format the
simulator reads. If the core speaks SNMPv3 only, swap `-v2c -c "$C"` for
`-v3 -l authPriv -u <user> -a SHA -A "$A" -x AES -X "$X"` (again read with `read -s`).

**gNMI / model-driven telemetry, only if already on.** `show feature` lists `grpc` and
`telemetry`. If both say enabled, record two minutes of a subscription (subscribing changes
nothing on the switch):

```sh
gnmic -a "$H:50051" -u <read-only user> --ask-password --skip-verify --encoding json \
  subscribe --mode stream --stream-mode sample --sample-interval 10s \
  --path /interfaces/interface/state/counters --path /system/cpus \
  --format event > core-gnmi.json &
sleep 120; kill %1
```

If they're off, skip it: the lab has a recorded stream built from the YANG models, and turning gRPC
on is a config change for another day.

## 2. Distribution (Catalyst 4500)

Holds no ARP (only the core does), but its FDB and uplinks matter.

```text
show version
show module
show inventory
show interfaces status
show interfaces counters errors
show interfaces description
show cdp neighbors detail
show lldp neighbors detail
show vlan brief
show mac address-table count
show ip arp | count Internet
show etherchannel summary
show spanning-tree summary
show processes cpu sorted | exclude 0.00
show environment all
show power
show flow exporter
show flow record
show logging
show clock
```

SNMP: the same loop as the core, into `dist.snmpwalk`. Catalyst IOS keeps the bridge table per
VLAN, so the plain walk of `1.3.6.1.2.1.17` shows only VLAN 1. Add one walk per VLAN that has seats
(the VLAN numbers are in `show vlan brief`):

```sh
for vlan in 10 20 30; do        # your seat VLANs
  snmpbulkwalk -v2c -c "$C@$vlan" -ObentU -Cr20 "$H" 1.3.6.1.2.1.17 > "dist-vlan$vlan.snmpwalk"; sleep 2
done
```

## 3. Firewalls

First, which product and version (OPNsense or pfSense, and whether all ten are the same). On one
firewall's shell (console option 8, or SSH), as a read-only check:

```sh
opnsense-version 2>/dev/null || cat /etc/version
pfctl -si                       # state table and counters
pfctl -ss | wc -l               # number of states only, no addresses
netstat -ibn                    # interface counters
pkg info | grep -iE 'telegraf|softflowd|netflow|ntopng|snmp|frr'
tail -n 300 /var/log/filter/latest.log 2>/dev/null || tail -n 300 /var/log/filter.log
tail -n 200 /var/log/system/latest.log 2>/dev/null || tail -n 200 /var/log/system.log
```

If SNMP is on, walk it like the core with these subtrees: `1.3.6.1.2.1.1 1.3.6.1.2.1.2
1.3.6.1.2.1.31 1.3.6.1.2.1.25 1.3.6.1.4.1.12325.1.200` (system, interfaces, host resources, pf).
Into `fw1.snmpwalk`.

Screenshots (no shell needed): **System > Gateways** (or the gateways widget), **Reporting >
NetFlow** settings (which version it exports, if any) and, if the Telegraf plugin is installed, its
**Services > Telegraf** output page. Crop out public addresses, or anonymise them as below.

## 4. LAN cache box (cache and DNS)

```sh
docker ps --format 'table {{.Image}}\t{{.Names}}\t{{.Status}}'
grep -E '^(CACHE_|UPSTREAM_DNS|USE_GENERIC|LANCACHE_IP|DNS_BIND_IP|CACHE_DOMAINS)' .env   # in the lancache folder
df -h
tail -n 1000 "$CACHE_ROOT/logs/access.log" > lancache-access.log
tail -n 200  "$CACHE_ROOT/logs/error.log"  > lancache-error.log
docker logs --tail 1000 lancache-dns > lancache-dns.log 2>&1
```

(`$CACHE_ROOT` is from `.env`; the container names may differ, `docker ps` shows them.) If
lancache-dns doesn't log queries, keep what it does log; turning query logging on is a config
change, so skip it. If DNS runs on something else (Unbound, BIND on the host, Pi-hole), say which,
and copy the last 1,000 lines of its query log if one exists.

## 5. Anonymise, then send

The lab ships a script that rewrites addresses consistently across all your files: the same IP or
MAC gets the same stand-in everywhere, so ARP, FDB and logs still line up.

```sh
python3 lab/tools/anonymise.py --map ~/lab-map.json --out anonymised/ *.snmpwalk *.txt *.json *.log
```

- IPv4: private addresses stay private and keep their subnets (a seat VLAN stays one subnet);
  public ones become documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`).
- MACs keep the vendor part (the first three bytes) and get new last three bytes.
- Serial numbers, `sysContact` and `sysLocation` become placeholders.
- Hostnames and other words: add `--name real=stand-in` for each (the event name, the venue,
  crew names in descriptions). The script prints the hostnames it saw, so you can check.
- `--map` keeps the mapping, so a second batch gets the same stand-ins. **Keep that file to
  yourself**: it turns the anonymised files back into real ones.

Read through the output once before you share it (look for names in interface descriptions and
log lines). Then put the `anonymised/` folder in the project files, or attach it to the lab
pull request, and say which device each file came from.

## Questions the samples answer

These are open in the project notes; the samples settle them without guessing:

- The Nexus model and NX-OS version (`show version`), and whether NX-API, gRPC and telemetry are on.
- The 4500's supervisor and IOS or IOS-XE version.
- Which firewall product and version, and whether it exports NetFlow.
- Which DNS server the cache box runs, and its log format.
- The syslog timestamp format (`show clock` against a few log lines).
- How LibreNMS knows the switches: by IP address or by name (the hostname column on its device
  list). Floorbit finds a switch in LibreNMS by the name or the address it has in its inventory,
  so the two have to agree; the lab tries both (`LAB_HOSTNAMES=ip`).
