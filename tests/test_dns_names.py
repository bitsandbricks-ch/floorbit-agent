"""The DNS answers name the seats' HTTPS flows on the agent, and never leave it (floorbit#1600)."""
import json

import pytest

from floorbit_agent import dns_feeds

names = pytest.importorskip('floorbit.flows.names')


class Spec:
    id = name = 'adguard-home'


class Registry:
    def __init__(self, rows):
        self.rows = rows

    def feeders(self, area):
        return [Spec()] if area == 'dns' else []

    def call(self, spec, method, since):
        assert method == 'dns_queries'
        return self.rows


def test_answers_name_flows_here_and_are_not_sent(monkeypatch):
    table = names.Names()
    monkeypatch.setattr(names, 'TABLE', table)
    ts = 1_800_000_000
    rows = [{'ts': ts, 'domain': 'cdn.example.com', 'client': '192.0.2.10', 'blocked': False,
             'answers': [{'address': '198.51.100.7', 'ttl': 300}]}]
    out = dns_feeds(Registry(rows), 0)
    assert table.site_of('192.0.2.10', '198.51.100.7', now=ts + 10) == 'example.com'
    sent = json.dumps(out)
    assert '198.51.100.7' not in sent and '192.0.2.10' not in sent
    assert out[0]['items'][0]['domain'] == 'example.com'


def test_a_broken_table_does_not_stop_dns_analytics(monkeypatch):
    def broken(rows):
        raise RuntimeError('boom')
    monkeypatch.setattr(names, 'learn_rows', broken)
    out = dns_feeds(Registry([{'ts': 1, 'domain': 'example.com', 'client': '192.0.2.10'}]), 0)
    assert out[0]['items'][0]['domain'] == 'example.com'
