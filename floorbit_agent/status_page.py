"""A page for a quick look at the agent from the event network: ``GET /`` on the metrics address
(``FLOORBIT_AGENT_METRICS``, 9520). The same numbers the hosted Local agents page shows, and each
plugin's state, with no hosted Floorbit needed: is it connected, does Telegraf deliver, which device
fails and why.

Open without a login, like ``/healthz``, so it is built
to give nothing away and do nothing: only GET, no forms, no scripts (a Content-Security-Policy
that allows none), no settings, keys or tokens (anything shaped like a Floorbit token is masked
even in a plugin's message), no request data echoed back. ``GET /status.json`` answers the same as
JSON.
"""
import html
import json
import re
import time

# Floorbit agent keys and tokens (fba_...), and anything after "password"/"token"/"key"/"community".
SECRETS = re.compile(r'fba_[A-Za-z0-9_-]+|((?:password|passwd|token|secret|community|key)\s*[=:]\s*)\S+', re.I)
SECURITY_HEADERS = {
    'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
}

STYLE = '''
:root {
  --bg:#f6f7f5;
  --card:#fff;
  --text:#16181a;
  --dim:#6b7177;
  --line:#e2e5e2;
  --ok:#0e9f6e;
  --warn:#c27803;
  --down:#d63c3c;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#111315; --card:#1a1d20; --text:#e8eaec; --dim:#8b9197; --line:#2a2e33; }
}
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui, sans-serif; }
main { max-width:960px; margin:0 auto; padding:20px 16px 40px; }
h1 { font-size:20px; margin:0 0 4px; } .dim { color:var(--dim); font-size:13px; }
.state {
  margin:14px 0;
  padding:10px 14px;
  border-radius:8px;
  background:var(--card);
  border-left:4px solid var(--down);
}
.state.ok { border-left-color:var(--ok); }
table { border-collapse:collapse; background:var(--card); border-radius:8px; width:100%; margin-bottom:16px; }
th, td { text-align:left; padding:6px 12px; border-bottom:1px solid var(--line); font-weight:normal; }
th { color:var(--dim); width:45%; } td { font-variant-numeric:tabular-nums; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); }
.card {
  background:var(--card);
  border-radius:8px;
  padding:12px 14px;
  border:1px solid var(--line);
  overflow-wrap:anywhere;
}
.card h2 { font-size:15px; margin:0 0 6px; display:flex; align-items:center; gap:8px; }
.card h2 small { margin-left:auto; color:var(--dim); font-weight:normal; }
.bit { width:10px; height:10px; background:var(--ok); }
.warning .bit { background:var(--warn); }
.error .bit { background:var(--down); }
.card p { margin:4px 0; }
.warning p:first-of-type { color:var(--warn); }
.error p:first-of-type { color:var(--down); }
a { color:var(--ok); }
.numbers { color:var(--dim); font-size:13px; } ul { padding-left:18px; font-size:13px; }
'''


def _clean(text):
    return SECRETS.sub(lambda m: (m.group(1) or '') + '***', str(text))

REFRESH = 10      # seconds; the page reloads itself

LABELS = {
    'up_seconds': 'Running for', 'reconnects': 'Reconnects', 'link_drops': 'Link drops',
    'calls_busy': 'Calls refused as busy', 'answers_failed': 'Answers that failed',
    'syslog_received': 'Syslog lines in', 'syslog_sent': 'Syslog lines sent', 'syslog_dropped': 'Syslog dropped',
    'syslog_backlog': 'Syslog waiting', 'flows_packets': 'NetFlow packets',
    'front_door_passed': 'Front door passed', 'front_door_failed': 'Front door failed',
    'captive_shown': 'Captive note shown', 'captive_done': 'Captive note done',
}
# Telemetry's numbers in words, the rest as the plugin names them.
PLUGIN_LABELS = {
    'points': 'points received', 'devices': 'devices set up', 'devices_heard': 'devices heard',
    'devices_failing': 'devices failing', 'series': 'series', 'sent': 'sends to Floorbit',
    'telegraf_quiet_seconds': 's since Telegraf posted', 'telegraf_restarts': 'Telegraf restarts',
    'refused_writes': 'posts refused', 'bad_lines': 'lines not understood', 'backlog': 'waiting to send',
}


def status(agent, health):
    """What the page shows, as data: health (from /healthz), the agent's report, its plugins."""
    try:
        report = agent.report()
    except Exception as exc:  # noqa: BLE001  # the page still says what it can
        report = {'error': f'{type(exc).__name__}: {exc}'}
    plugins = report.pop('plugins', {}) if isinstance(report, dict) else {}
    shown = {}
    for pid, p in (plugins or {}).items():
        if not isinstance(p, dict):
            continue
        # Only what the page needs: never the plugin's settings or the logins it is waiting on.
        shown[str(pid)] = {'status': p.get('status'), 'running': bool(p.get('running')),
                           'message': _clean(p.get('message') or ''),
                           'metrics': {str(k): v for k, v in (p.get('metrics') or {}).items()
                                       if isinstance(v, (int, float))},
                           'errors': [{'at': x.get('at'), 'error': _clean(x.get('error') or '')}
                                      for x in (p.get('errors') or [])[-5:] if isinstance(x, dict)]}
    agent_numbers = {k: v for k, v in (report or {}).items() if k in LABELS and isinstance(v, (int, float))}
    if isinstance(report, dict) and report.get('error'):
        agent_numbers['error'] = _clean(report['error'])
    return {'at': time.time(), 'version': agent.version, 'health': health, 'agent': agent_numbers, 'plugins': shown}


def _duration(seconds):
    seconds = int(seconds or 0)
    for size, unit in ((86400, 'd'), (3600, 'h'), (60, 'min')):
        if seconds >= size:
            return f'{seconds // size} {unit}'
    return f'{seconds} s'


def _number(v):
    return f'{v:,.0f}' if isinstance(v, (int, float)) and float(v).is_integer() else str(v)


def render(data):
    e = html.escape
    health = data['health'] or {}
    ok = health.get('ok')
    link = health.get('link') or 'none'
    if ok:
        head = f'Connected to Floorbit ({ "WebSocket" if link == "websocket" else "long poll" })'
    else:
        head = 'Not connected to Floorbit: ' + (health.get('reason') or 'no answer')
    rows = []
    for key, value in (data['agent'] or {}).items():
        if key in LABELS and not isinstance(value, (dict, list)):
            shown = _duration(value) if key == 'up_seconds' else _number(value)
            rows.append(f'<tr><th>{e(LABELS[key])}</th><td>{e(shown)}</td></tr>')
    cards = []
    for pid, p in sorted((data['plugins'] or {}).items()):
        state = p.get('status') or 'ok'
        if not p.get('running'):
            state = 'error'
        numbers = ' · '.join(f'{_number(v)} {PLUGIN_LABELS.get(k, k.replace("_", " "))}'
                             for k, v in sorted((p.get('metrics') or {}).items()))
        errors = ''.join(f'<li>{e(time.strftime("%H:%M:%S", time.localtime(x.get("at") or 0)))} '
                         f'{e(str(x.get("error") or ""))}</li>'
                         for x in (p.get('errors') or [])[-5:] if isinstance(x, dict))
        cards.append(
            f'<section class="card {e(state)}"><h2><span class="bit"></span>{e(pid)}'
            f'<small>{e(state if p.get("running") else "not running")}</small></h2>'
            + (f'<p>{e(p.get("message") or "")}</p>' if p.get('message') else '')
            + (f'<p class="numbers">{e(numbers)}</p>' if numbers else '')
            + (f'<details><summary>Last errors</summary><ul>{errors}</ul></details>' if errors else '')
            + '</section>')
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH}">
<title>Floorbit agent</title>
<style>{STYLE}</style></head><body><main>
<h1>Floorbit agent {e(str(data.get("version") or ""))}</h1>
<div class="dim">Updated {e(time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(data["at"])))}, every {REFRESH} s ·
<a href="/status.json">JSON</a> · <a href="/metrics">metrics</a></div>
<div class="state{" ok" if ok else ""}">{e(head)}</div>
<div class="grid">{"".join(cards) or '<p class="dim">No plugins run on this agent.</p>'}</div>
<table style="margin-top:16px">{"".join(rows)}</table>
</main></body></html>
'''


def as_json(data):
    return json.dumps(data, default=str) + '\n'
