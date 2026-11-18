"""FLOORBIT_AGENT_KEY_FILE: the key a self-hosted Floorbit writes for the agent next to it."""
import os

import floorbit_agent


def test_the_key_comes_from_the_file(tmp_path):
    path = tmp_path / 'local-agent.key'
    path.write_text('fba_10ca1000_example-key-for-tests-only\n')
    assert floorbit_agent.key_from_file(str(path)) == 'fba_10ca1000_example-key-for-tests-only'


def test_it_waits_for_floorbit_to_write_it(tmp_path):
    path = tmp_path / 'local-agent.key'
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            path.write_text('fba_10ca1000_example-key-for-tests-only')
    assert floorbit_agent.key_from_file(str(path), wait=60, sleep=sleep).startswith('fba_10ca1000_')
    assert len(naps) == 3


def test_no_file_no_key(tmp_path):
    assert floorbit_agent.key_from_file('') == ''
    assert floorbit_agent.key_from_file(str(tmp_path / 'missing'), wait=0) == ''


def test_main_takes_the_file_and_hands_the_key_to_the_plugins(tmp_path, monkeypatch):
    path = tmp_path / 'local-agent.key'
    path.write_text('fba_10ca1000_example-key-for-tests-only')
    monkeypatch.setenv('FLOORBIT_AGENT_KEY', '')     # restored after the test
    monkeypatch.setenv('FLOORBIT_AGENT_KEY_FILE', str(path))
    monkeypatch.setenv('FLOORBIT_URL', 'http://floorbit.example.com')   # refused: not https
    monkeypatch.setenv('FLOORBIT_AGENT_INSECURE', '0')
    assert floorbit_agent.main(['run']) == 2
    assert os.environ['FLOORBIT_AGENT_KEY'] == 'fba_10ca1000_example-key-for-tests-only'
