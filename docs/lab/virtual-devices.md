# Virtual devices (optional)

The lab's switches are SNMP recordings: they answer like the real thing, but nothing changes
unless the replay says so. For what a recording can't show (telemetry streams, the CLI over SSH,
a real firewall's API), run the vendor's own virtual image next to the lab. None of these are
needed for CI.

They need a machine with nested virtualisation and some RAM; each is free to download with an
account at the vendor. Put them on a bridge to the lab network (172.31.10.0/24 in
`lab/compose.yml`) and add them to the lab LibreNMS with `LAB_DEVICES`, or point a real LibreNMS
at them.

## Nexus 9000v (core)

The closest to the event's core: NX-OS, the same ifIndexes and MIBs, plus NX-API, gNMI and model
driven telemetry.

- Image: Cisco's Nexus 9000v (`nexus9300v` or `nexus9500v`), for KVM or VirtualBox; about 10 GB
  RAM and 4 vCPUs.
- Turn on what Floorbit and the agent use:

  ```text
  feature nxapi
  feature grpc
  feature telemetry
  snmp-server community lab-ro ro
  logging server 172.31.10.31 use-vrf management
  logging server 172.31.10.32 use-vrf management
  ```

- gNMI (port 50051) and dial-out telemetry feed Telegraf once the telemetry plugin is in
  (bitsandbricks-ch/floorbit#661).

## Catalyst on CML-Free (access and distribution)

Cisco Modeling Labs Free runs up to five nodes. IOSvL2 behaves like a Catalyst access switch
(per-VLAN FDB, CDP, the IOS syslog format); there is no 2960-X or 4500 image, so the model
strings differ.

- Two or three IOSvL2 nodes with an external connector bridged to the lab network.
- `snmp-server community lab-ro RO`, `logging host 172.31.10.31`, `logging host 172.31.10.32`.
- Don't count on NetFlow from IOSvL2: flows stay with the replay.

## OPNsense VM (firewalls)

The real firewall: its filter log over syslog, the Telegraf plugin push, NetFlow, and its API.

- The OPNsense amd64 ISO in any hypervisor, 2 GB RAM; one interface on the lab network.
- System > Settings > Logging > Remote: the two agents, UDP 5514.
- Reporting > NetFlow: version 9 to an agent, port 2055.
- The `os-telegraf` plugin, output to the agent or straight to the telemetry store.

Use example values for every key and community; nothing from the event goes in a lab file.
