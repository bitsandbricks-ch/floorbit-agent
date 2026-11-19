#!/bin/sh
# The lab without Docker, for a laptop or a sandbox: every part as a process on 127.0.0.1.
# Needs: redis-server, net-snmp's snmpbulkwalk/snmptrap, Python with Floorbit's requirements,
# this repository's agent installed (pip install -e .), snmpsim and gunicorn. Usage:
#
#   FLOORBIT_SRC=../floorbit lab/run-local.sh start     # start everything, set up the hall
#   lab/run-local.sh test                               # the end-to-end tests
#   lab/run-local.sh stop
#
# Ports: Floorbit 5000 (and its own syslog 5514), lab LibreNMS 18000, devices UDP 11161-11168, agents: syslog 5524,
# NetFlow 2065, captive 8908, front door 8443 (agent A only), metrics 9520/9521, telemetry 8186/8187,
# Redis 6390. State in $LAB_STATE (/tmp/floorbit-lab).
set -e
LAB=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$LAB")
STATE=${LAB_STATE:-/tmp/floorbit-lab}
FLOORBIT_SRC=$(cd "${FLOORBIT_SRC:-$REPO/../floorbit}" && pwd)
PY=${PYTHON:-python3}
DEVICES="core-01 dist-01 access-a1 access-b1 access-c1 access-d1 fw-01 fw-02"

start() {
  rm -rf "$STATE"      # a fresh lab every time: new keys, an empty plan
  mkdir -p "$STATE/floorbit" "$STATE/keys" "$STATE/lancache"
  redis-server --port 6390 --save "" --daemonize yes --logfile "$STATE/redis.log"
  n=0; spec=""
  for d in $DEVICES; do
    n=$((n + 1))
    # snmpsim drops to "nobody" before it writes its index: give it a folder it may write.
    mkdir -p "$STATE/snmpsim-$d" && chmod 1777 "$STATE/snmpsim-$d"
    snmpsim-command-responder --data-dir="$LAB/snmp/data/$d" --cache-dir="$STATE/snmpsim-$d" \
      --agent-udpv4-endpoint="127.0.0.1:1116$n" --process-user=nobody --process-group=nogroup \
      --log-level=error > "$STATE/snmpsim-$d.log" 2>&1 &
    spec="$spec,$d=127.0.0.1:1116$n"
  done
  LAB_DEVICES=${spec#,} LAB_POLL_SECONDS=15 LAB_LISTEN=127.0.0.1:18000 LAB_TOKEN=lab-librenms-token \
    $PY "$LAB/librenms_lite/server.py" > "$STATE/librenms.log" 2>&1 &

  (cd "$FLOORBIT_SRC" && env REDIS_HOST=127.0.0.1 REDIS_PORT=6390 SECRET_KEY=lab-only-not-a-secret \
    FLOORBIT_DATA_FILE="$STATE/floorbit/data.json" CHECKIN_FILE="$STATE/floorbit/checkins.json" \
    FLOWS_DIR="$STATE/floorbit/flows" SEAT_SOURCE_JSON_PATH="$LAB/fixtures/seats.json" \
    AUTH_ADMIN_USER=admin AUTH_ADMIN_PASSWORD=lab-admin-password SYSLOG_ENABLED=true SYSLOG_ALLOW=127.0.0.1 \
    CHECKIN_ENABLED=true CHECKIN_TRUST_PROXY=true CAPTIVE_ENABLED=true PUBLIC_URL=http://127.0.0.1:5000 \
    $PY -m gunicorn --bind 127.0.0.1:5000 --workers 1 --worker-class gthread --threads 32 app:app \
    > "$STATE/floorbit.log" 2>&1 &)
  # Both agents on one machine can't take the same ports: only agent A runs syslog, NetFlow, the
  # note and the front door here, so the failover run has no syslog (the Docker lab has it on both).
  $PY "$LAB/e2e/setup.py" --url http://127.0.0.1:5000 --out "$STATE/keys" --password lab-admin-password \
    --librenms-url http://127.0.0.1:18000 --syslog-port 5524 --flows-port 2065 --captive 127.0.0.1:8908 \
    --front-door 127.0.0.1:8443 --captive-proxies 127.0.0.1 --listeners-on agent-a --telemetry-on agent-a --snmp-devices "${spec#,}" \
    agent-a agent-b

  # Telegraf runs inside each agent, as in the image: the one on the PATH (or TELEGRAF=...).
  # Traps stay off here: every device is 127.0.0.1, so a trap couldn't say which one it is.
  telegraf=$(command -v "${TELEGRAF:-telegraf}" || true)
  [ -n "$telegraf" ] || echo "No telegraf here (set TELEGRAF): the telemetry tests will skip what it polls."
  i=0
  for a in agent-a agent-b; do
    mkdir -p "$STATE/$a"
    (cd "$STATE/$a" && exec env PYTHONPATH="$FLOORBIT_SRC:$REPO" FLOORBIT_URL=http://127.0.0.1:5000 \
      FLOORBIT_AGENT_INSECURE=1 FLOORBIT_AGENT_KEY="$(cat "$STATE/keys/$a.key")" \
      FLOORBIT_DATA_FILE="$STATE/$a/agent-data.json" FLOWS_DIR="$STATE/$a/flows" \
      FLOORBIT_AGENT_METRICS=127.0.0.1:$((9520 + i)) FLOORBIT_AGENT_LAN_URL=http://127.0.0.1:8908 \
      FLOORBIT_TELEMETRY_PORT=$((8186 + i)) FLOORBIT_TELEGRAF_BIN="$telegraf" \
      $PY -m floorbit_agent) > "$STATE/$a.log" 2>&1 &
    echo $! > "$STATE/$a.pid"
    i=$((i + 1))
  done
  # Everything comes from 127.0.0.1 here, so one replay sends for all devices.
  LAB_SYSLOG_TARGETS=127.0.0.1:5524 LAB_FLOW_TARGETS=127.0.0.1:2065 LAB_LANCACHE_DIR="$STATE/lancache" \
    LAB_PUSH_URL=http://127.0.0.1:8186/write LAB_STREAM_URL=http://127.0.0.1:8186/write \
    LAB_TELEMETRY_TOKEN_FILE="$STATE/agent-a/plugins/telemetry/ingest-token" \
    $PY "$LAB/replay/replay.py" > "$STATE/replay.log" 2>&1 &
  echo "Lab up: http://127.0.0.1:5000 (admin / lab-admin-password), logs in $STATE"
}

e2e() {
  command -v "${TELEGRAF:-telegraf}" > /dev/null || export LAB_TELEGRAF=0
  LAB_FLOORBIT_URL=http://127.0.0.1:5000 LAB_ADMIN_PASSWORD=lab-admin-password \
    LAB_AGENT_METRICS=agent-a=http://127.0.0.1:9520 \
    LAB_CAPTIVE=http://127.0.0.1:8908 LAB_FRONT_DOOR=http://127.0.0.1:8443 $PY -m pytest -q "$LAB/e2e" "$@"
}

stop() {
  pkill -f "$LAB/replay/replay.py" || true
  pkill -f "telegraf --config $STATE/" || true   # the agents' own, should one outlive them
  pkill -f "floorbit_agent" || true
  pkill -f "$LAB/librenms_lite/server.py" || true
  pkill -f "snmpsim-command-responder --data-dir=$LAB" || true
  pkill -f "gunicorn --bind 127.0.0.1:5000" || true
  redis-cli -p 6390 shutdown nosave 2>/dev/null || true
}

case "$1" in
  start) start ;;
  test) shift; e2e "$@" ;;
  stop) stop ;;
  stop-agent) kill "$(cat "$STATE/$2.pid")" ;;     # failover: lab/run-local.sh stop-agent agent-a
  *) echo "usage: $0 start|test|stop|stop-agent <name>"; exit 2 ;;
esac
