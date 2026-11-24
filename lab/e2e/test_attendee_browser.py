"""Attendees and the crew in a real browser, as at the event (agent #64): a fresh agent with nothing
set by hand, its one attendee port, a seat PC checking in, the check-in note on phones, the crew's
seat walk-through, and an agent log without stack traces.

Needs Playwright for Python and a Chromium (``pip install playwright`` and ``playwright install
chromium``, or LAB_CHROMIUM pointing at one); skipped without. The browser asks through the front
door as a reverse proxy would, naming the seat's address in X-Forwarded-For, so the lab's proxy
address must be in the front door's trusted proxies (setup.py ``--proxies``).

LAB_SHOTS: a folder for the screenshots (the pull request's). LAB_AGENT_LOGS: the agents' log files,
comma-separated (run-local.sh sets them), or the CI step greps ``docker compose logs`` itself.
"""
import os
import pathlib
import re

import pytest
import requests
from conftest import eventually

sync_api = pytest.importorskip('playwright.sync_api')

DOOR = os.environ.get('LAB_FRONT_DOOR', '').rstrip('/')
SHOTS = os.environ.get('LAB_SHOTS', '')


def short(port):
    from test_lab import short as s
    return s(port)


@pytest.fixture(scope='module')
def browser():
    if not DOOR:
        pytest.skip('LAB_FRONT_DOOR is not set')
    with sync_api.sync_playwright() as p:
        exe = os.environ.get('LAB_CHROMIUM') or None
        b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


def shot(page, name):
    if SHOTS:
        pathlib.Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(pathlib.Path(SHOTS) / f'{name}.png'))


def seat_browser(browser, seat, phone=False):
    """A browser at this seat: the front door sees it as the seat's own address."""
    size = {'width': 390, 'height': 844} if phone else {'width': 1280, 'height': 860}
    return browser.new_context(viewport=size, device_scale_factor=2,
                               extra_http_headers={'X-Forwarded-For': seat.ip})


def note(seat):
    r = requests.get(f'{DOOR}/.well-known/captive-portal', headers={'X-Forwarded-For': seat.ip}, timeout=10)
    r.raise_for_status()
    assert r.headers['Content-Type'].startswith('application/captive+json')
    return r.json()


def test_a_fresh_agent_answers_attendees_on_one_port(fb):
    """Nobody turned the front door on or gave it an address: it is on by default."""
    plugins = {p['id']: p for p in fb.get('/api/settings/plugins')['plugins']}
    assert 'captive-note' not in plugins
    assert plugins['front-door']['enabled'], plugins['front-door']
    r = requests.get(f'{DOOR}/', timeout=30)
    assert r.status_code == 200 and '<html' in r.text.lower(), r.status_code
    assert requests.get(f'{DOOR}/.well-known/captive-portal', timeout=10).status_code == 200


def test_a_seat_pc_checks_in_in_a_browser_and_the_note_goes(fb, lab, browser):
    seat = next(s for s in lab.seats if s.online and s.label.startswith('C-') and s.label not in ('C-1',))
    eventually(lambda: note(seat)['captive'] is True, timeout=120, what='the check-in note before checking in')
    ctx = seat_browser(browser, seat)
    page = ctx.new_page()
    page.goto(f'{DOOR}/', wait_until='networkidle')
    assert page.get_by_role('heading', name='Seat check-in').first.is_visible()
    shot(page, 'checkin-page')
    pin = page.get_by_label('PIN from your seat card')
    if pin.count():
        page.get_by_role('button', name='No card at your seat? Type the seat number instead').click()
    page.get_by_label('Seat number').fill(seat.label)
    page.get_by_label('Your nickname').fill(seat.nick)
    page.get_by_role('button', name='Check in', exact=True).click()
    page.get_by_text("You're checked in at seat").wait_for(timeout=30_000)
    shot(page, 'checked-in')
    ctx.close()

    def verified():
        row = next((c for c in fb.get('/api/checkins')['checkins'] if c['seat'] == seat.label), None)
        return row if row and row['wired'] == 'verified' else None
    row = eventually(verified, what='the check-in port lookup')
    assert row['port_seen'] == {'switch': seat.switch, 'port': short(seat.port)}
    eventually(lambda: note(seat)['captive'] is False, timeout=150, what='the note after checking in')


def test_the_crew_walks_a_seat_from_the_walk_page(fb, lab, browser):
    """The crew laptop opens the walk page through the agent, picks a row and walks a seat:
    no setting, no code to copy, no trip to another address."""
    ctx = browser.new_context(viewport={'width': 1280, 'height': 900}, device_scale_factor=2)
    page = ctx.new_page()
    page.goto(f'{DOOR}/seat-walk', wait_until='networkidle')       # sends the crew to sign in first
    page.locator('input[name=username]').fill('admin')
    page.locator('input[type=password]').fill(os.environ.get('LAB_ADMIN_PASSWORD', 'lab-admin-password'))
    page.keyboard.press('Enter')
    page.wait_for_url(f'{DOOR}/seat-walk', timeout=30_000)
    # Not networkidle: the page keeps a live-updates stream open.
    page.get_by_label('Rows').first.wait_for(timeout=30_000)
    assert not page.get_by_text("doesn't know where laptops").count()
    assert not page.get_by_text('No laptop in the hall has reached a local agent yet').count()
    page.get_by_label('Rows').first.click()
    page.get_by_role('option').filter(has_text=re.compile(r'^D\b')).first.click()     # block D's first row
    page.keyboard.press('Escape')
    start = page.get_by_role('button', name='Start at')
    start.wait_for()
    label = start.inner_text().replace('Start at', '').strip()
    seat = lab.seat(label)
    if not seat.online:
        pytest.skip(f'{label} has no laptop online in the lab')
    # The laptop is now plugged in at that seat: the front door sees it there.
    page.set_extra_http_headers({'X-Forwarded-For': seat.ip})
    start.click()
    shot(page, 'walk-plug-in')
    # No button: the page looks the laptop up by itself once it is plugged in.
    page.get_by_text(f'{label} is on').wait_for(timeout=90_000)
    shot(page, 'walk-found')
    assert page.url.startswith(DOOR), page.url               # never left for another address
    ctx.close()
    rows = [o['value'] for o in fb.get('/api/options/rows')['options'] if o.get('group') == 'D']
    kept = fb.get(f"/api/seat-walk?rows={','.join(rows)}")
    port = {s['seat']: s['port'] for s in kept['seats']}[label]
    assert port['port'] == short(seat.port) and port['source'] == 'walk'
    assert fb.s.delete(f'{fb.url}/api/seat-walk/{label}', timeout=30).status_code == 200


def test_the_agents_log_has_no_stack_traces():
    files = [f for f in os.environ.get('LAB_AGENT_LOGS', '').split(',') if f]
    if not files:
        pytest.skip('LAB_AGENT_LOGS is not set (CI greps docker compose logs itself)')
    for f in files:
        text = pathlib.Path(f).read_text(errors='replace')
        assert 'Traceback' not in text, f'{f} has a stack trace:\n' + text[text.index('Traceback') - 400:][:2000]
