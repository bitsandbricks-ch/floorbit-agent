#!/bin/sh
# One simulated device: snmpsim answers SNMP from lab/snmp/data/$DEVICE on UDP 161, and the
# device sends its own syslog, traps and flows (replay.py --device), so they come from its address.
set -e
if [ -d "/lab/snmp/data/$DEVICE" ]; then
  # snmpsim drops to "lab" before it writes its index: give it a folder it may write.
  mkdir -p /tmp/snmpsim && chmod 1777 /tmp/snmpsim
  snmpsim-command-responder --data-dir="/lab/snmp/data/$DEVICE" --cache-dir=/tmp/snmpsim \
    --agent-udpv4-endpoint=0.0.0.0:161 --process-user=lab --process-group=nogroup \
    --log-level=error &
  # A device that can't answer SNMP should stop the lab, not leave LibreNMS polling nothing.
  sleep 3
  kill -0 $! 2>/dev/null || { echo "snmpsim did not start for $DEVICE" >&2; exit 1; }
fi
exec python /lab/replay/replay.py --device "$DEVICE"
