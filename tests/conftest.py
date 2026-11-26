"""The agent reuses Floorbit's code: put a checkout of it (FLOORBIT_SRC, by default ../floorbit, the
commit in FLOORBIT_REF) on the path. Tests never talk to a real Floorbit, LibreNMS or NetBox."""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLOORBIT = pathlib.Path(os.environ.get("FLOORBIT_SRC") or ROOT.parent / "floorbit").resolve()
if (FLOORBIT / "floorbit" / "__init__.py").is_file():
    sys.path.insert(0, str(FLOORBIT))
sys.path.insert(0, str(ROOT))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_owner_list_from_iptoasn(monkeypatch):
    """The flow collector fetches the network owner list once a day (Floorbit's floorbit/flows/owners.py);
    never in tests. Floorbit before that file has nothing to stop."""
    try:
        from floorbit.flows import owners
    except ImportError:
        return

    def refuse(path):
        raise OSError("no network in tests")
    monkeypatch.setattr(owners, "_download", refuse)
