# The agent reuses Floorbit's plugins (LibreNMS, NetBox, health, DNS, Wi-Fi) and its flow
# collector: this stage fetches the Floorbit source of the commit it is tested with (FLOORBIT_REF)
# and keeps only the Python parts, so the image carries no web pages, no React app and no Node.
FROM python:3.11-slim AS floorbit
COPY FLOORBIT_REF /tmp/FLOORBIT_REF
# A source tarball, not git: the slim image has no git, and installing it would mean apt.
RUN python - <<'PY'
import io, pathlib, tarfile, urllib.request
ref = pathlib.Path('/tmp/FLOORBIT_REF').read_text().strip()
url = f'https://github.com/bitsandbricks-ch/floorbit/archive/{ref}.tar.gz'
with urllib.request.urlopen(url, timeout=120) as answer:
    tarfile.open(fileobj=io.BytesIO(answer.read())).extractall('/src', filter='data')
src = next(pathlib.Path('/src').iterdir())
out = pathlib.Path('/out')
out.mkdir()
for name in ('floorbit', 'auth', 'integrations', 'VERSION', 'requirements.txt'):
    (src / name).rename(out / name)
PY

FROM python:3.11-slim
WORKDIR /app
COPY --from=floorbit /out/requirements.txt /tmp/requirements.txt
# gunicorn serves Floorbit's pages; the agent serves none.
RUN grep -v '^gunicorn' /tmp/requirements.txt > /tmp/req.txt \
    && pip install --no-cache-dir -r /tmp/req.txt && rm /tmp/requirements.txt /tmp/req.txt
# The agent's own dependencies, as in pyproject.toml (simple-websocket: the WebSocket link).
RUN pip install --no-cache-dir "requests>=2.31" "simple-websocket>=1.0"
COPY --from=floorbit /out/ /app/
RUN rm /app/requirements.txt
# Telegraf for device telemetry: the Telemetry plugin runs it inside the agent and reads what it
# says about each device (Floorbit's docs/agent.md, "Telegraf"), so there's no sidecar to forget.
# Its static binary only; the version is the one Floorbit's config is tested with.
COPY --from=telegraf:1.40.3 /usr/bin/telegraf /usr/local/bin/telegraf
COPY floorbit_agent/ /app/floorbit_agent/
COPY VERSION /app/floorbit_agent/VERSION

# keepalived for the optional VRRP mode (vrrp.py). The file capabilities let it set the virtual IPs
# while the agent runs as a normal user; the kernel only allows them when the container was given
# NET_ADMIN (compose.vrrp.yml), so without VRRP mode they grant nothing.
# snmp: Telegraf's trap listener looks up each trap's OIDs with net-snmp's snmptranslate; without
# it every linkDown/linkUp is dropped with "snmptranslate not found". Floorbit reads the numeric
# OIDs, so Debian's MIB-less package is enough.
RUN apt-get update \
    && apt-get install -y --no-install-recommends keepalived libcap2-bin snmp \
    && setcap cap_net_admin,cap_net_raw+ep /usr/sbin/keepalived \
    && apt-get purge -y libcap2-bin && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --create-home --home-dir /home/floorbit --shell /usr/sbin/nologin floorbit \
    && mkdir -p /app/state && chown floorbit:floorbit /app/state
VOLUME /app/state
USER floorbit
ENV FLOORBIT_DATA_FILE=/app/state/agent-data.json PYTHONUNBUFFERED=1
# The agent serves no pages: only what its plugins listen on (docs/agent.md, "Ports" in Floorbit).
EXPOSE 5514/udp 2055/udp 1162/udp 57000/tcp 8186/tcp 9520/tcp
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=2 \
    CMD ["python", "-m", "floorbit_agent", "health"]
CMD ["python", "-m", "floorbit_agent"]
