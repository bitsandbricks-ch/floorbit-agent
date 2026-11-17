#!/bin/sh
# One simulated device: snmpsim answers SNMP from lab/snmp/data/$DEVICE on UDP 161, and the
# device sends its own syslog, traps and flows (replay.py --device), so they come from its address.
set -e
if [ -d "/lab/snmp/data/$DEVICE" ]; then
  snmpsim-command-responder --data-dir="/lab/snmp/data/$DEVICE" --cache-dir=/tmp/snmpsim \
    --agent-udpv4-endpoint=0.0.0.0:161 --process-user=lab --process-group=nogroup \
    --log-level=error &
fi
exec python /lab/replay/replay.py --device "$DEVICE"
