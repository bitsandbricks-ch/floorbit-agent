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
