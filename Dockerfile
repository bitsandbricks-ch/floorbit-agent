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
COPY floorbit_agent/ /app/floorbit_agent/
COPY VERSION /app/floorbit_agent/VERSION

RUN useradd --system --uid 10001 --create-home --home-dir /home/floorbit --shell /usr/sbin/nologin floorbit \
    && mkdir -p /app/state && chown floorbit:floorbit /app/state
VOLUME /app/state
USER floorbit
ENV FLOORBIT_DATA_FILE=/app/state/agent-data.json PYTHONUNBUFFERED=1
# The agent serves no pages: nothing to expose but the optional syslog and flow ports.
EXPOSE 5514/udp 2055/udp
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=2 \
    CMD ["python", "-m", "floorbit_agent", "health"]
CMD ["python", "-m", "floorbit_agent"]
