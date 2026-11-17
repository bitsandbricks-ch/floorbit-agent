# Virtual IPs (VRRP)

Two or more agents can share virtual IPs (VIPs), so whatever the event network points at an agent
(the check-in page and captive API later, syslog and NetFlow now) keeps answering when one agent
machine goes down. Off by default.

With two agents and two VIPs, each agent holds one while both are up (active/active): both do
work, and half the clients are never moved. When one stops, the other holds both:

| What happens | The VIPs move in |
| --- | --- |
| an agent stops (`docker stop`, update, shutdown) | well under a second: keepalived says goodbye |
| an agent machine dies or loses its link | about 3.5 seconds (three missed VRRP adverts) |
| an agent stops answering (its own `/healthz`) | about 3 seconds |
| an agent comes back | 10 to 15 seconds: it waits 10 seconds first, so a flapping agent can't move them back and forth |

These are from `scripts/vrrp_check.py`, which runs two agents with the real keepalived in two
network namespaces (as root: `sudo python scripts/vrrp_check.py`).

The health check asks only the agent itself, never the hosted Floorbit: a VIP on the event network
must stay up while the uplink is away.

## Three ways to run it

### 1. keepalived in the agent image (simplest)

The image has keepalived. The agent writes its config and runs it, and keepalived stops when the
agent stops. On every agent machine:

```sh
# agent.env, the same on every agent except PREFER
FLOORBIT_AGENT_VRRP_VIPS=192.0.2.10/24,192.0.2.11/24
FLOORBIT_AGENT_VRRP_INTERFACE=eth0
FLOORBIT_AGENT_VRRP_PREFER=1        # 2 on the second agent

docker compose -f compose.yml -f compose.vrrp.yml up -d
```

`compose.vrrp.yml` puts the agent on the machine's own network (`network_mode: host`) and gives it
`NET_ADMIN`, which keepalived needs to set the VIPs. The agent still runs as user 10001: only the
keepalived program carries the network rights, and the kernel grants them only when the container
has `NET_ADMIN`. `/metrics` stays on the machine's 127.0.0.1.

| Setting | What |
| --- | --- |
| `FLOORBIT_AGENT_VRRP_VIPS` | the VIPs with their prefix, comma-separated; each is its own VRRP instance |
| `FLOORBIT_AGENT_VRRP_INTERFACE` | the interface they live on, like `eth0` |
| `FLOORBIT_AGENT_VRRP_PREFER` | which VIPs this agent holds while it is up, by position (`1`, `2`, `1,2`) |
| `FLOORBIT_AGENT_VRRP_ID` | the first VIP's virtual router ID, default 51; the next VIPs take 52, 53, ... It must be unique on the VLAN |
| `FLOORBIT_AGENT_VRRP_PEERS` | the other agents' addresses, where the switches drop VRRP multicast: VRRP then goes unicast |

A wrong setting stops the agent at start with what is wrong, so a VIP is never silently missing.

`/metrics` shows `floorbit_agent_vrrp_running` and `floorbit_agent_vrrp_master{vip="…"}` (1 on the
agent that holds it), and the agent logs every change:

```text
[vrrp] keepalived started on eth0 for 192.0.2.10 (preferred), 192.0.2.11
[vrrp] 192.0.2.10: MASTER
[vrrp] 192.0.2.11: BACKUP
```

### 2. keepalived on the machine

When the machine runs keepalived already, leave the agent as it is (`compose.yml` alone) and add
a `vrrp_instance` per VIP to the machine's keepalived. Track the agent with a script that asks
its `/healthz` (any answer, 200 or 503, means it runs):

```text
vrrp_script floorbit_agent {
  script "/usr/bin/curl -s -o /dev/null http://127.0.0.1:9520/healthz"
  interval 2
  fall 2
}
```

and `track_script { floorbit_agent }` in each instance.

### 3. Kubernetes (k3s)

On Kubernetes, the cluster gives out the address instead: run the agents as a Deployment with two
replicas behind a `LoadBalancer` Service, with [kube-vip](https://kube-vip.io) or
[MetalLB](https://metallb.io) (L2 mode) announcing the Service's IP. Don't use VRRP mode in a pod.

## Telemetry

With the Telemetry plugin on, only the agent holding the **first** VIP collects (polls the devices
and sends); the others keep Telegraf listening only and take over when that VIP moves. Point the
devices' traps and model-driven telemetry at the first VIP. Floorbit's docs/telemetry.md, "Two
agents (VRRP)".

## Keep it safe

VRRP has no real authentication: anyone on the VLAN can claim a VIP. Keep the agents and their
VIPs on a management VLAN that attendees can't reach (`docs/threat-model.md`).
