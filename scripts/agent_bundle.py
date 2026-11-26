"""Builds and signs an agent bundle: the agent's code and the Floorbit code it runs (issue #98).

    python scripts/agent_bundle.py build --floorbit ../floorbit --key key.pem --out dist
    python scripts/agent_bundle.py verify dist/floorbit-agent-<version>.tar.gz

``build`` writes ``floorbit-agent-<version>.tar.gz`` and ``floorbit-agent-bundle.json`` (version,
file, sha256, size, image, protocol) to ``--out``; the release workflow attaches both to the release,
and a Floorbit offers the bundle to its agents from there. It refuses a key the image doesn't pin.
The private key lives only in the release workflow's ``agent-signing`` environment.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import floorbit_agent_boot as boot  # noqa: E402

FLOORBIT_PARTS = ('floorbit', 'auth', 'integrations', 'VERSION')


def protocol():
    text = (ROOT / 'floorbit_agent' / '__init__.py').read_text(encoding='utf-8')
    return int(re.search(r'^PROTOCOL = (\d+)', text, re.M).group(1))


def assemble(floorbit: Path, dest: Path, version: str) -> None:
    """The folder a bundle packs: what the Dockerfile puts in /app, the same way."""
    dest.mkdir(parents=True)
    for name in FLOORBIT_PARTS:
        src = floorbit / name
        if src.is_dir():
            shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        else:
            shutil.copy2(src, dest / name)
    shutil.copytree(ROOT / 'floorbit_agent', dest / 'floorbit_agent',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info'))
    (dest / 'floorbit_agent' / 'VERSION').write_text(version + '\n')


def sign(folder: Path, private_pem: bytes, version: str, image: str, proto: int) -> dict:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit('not an Ed25519 private key')
    doc = {'id': boot.BUNDLE_ID, 'version': version, 'image': image, 'protocol': proto,
           'files': boot.file_hashes(folder), 'key_id': boot.key_id(key.public_key())}
    doc['signature'] = base64.b64encode(key.sign(boot.payload(doc))).decode()
    (folder / boot.SIGFILE).write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    return doc


def build(args):
    floorbit = Path(args.floorbit).resolve()
    version = args.version or (ROOT / 'VERSION').read_text().strip()
    image = boot.compute_image_id((ROOT / 'Dockerfile').read_text(), (floorbit / 'requirements.txt').read_text())
    pem = Path(args.key).read_bytes()
    keys = boot.pinned_keys(ROOT)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / 'bundle'
        assemble(floorbit, folder, version)
        doc = sign(folder, pem, version, image, protocol())
        if doc['key_id'] not in keys:
            raise SystemExit(f"key {doc['key_id']} is not pinned in floorbit_agent/keys/: agents would refuse it")
        boot.verify(folder, keys, version=version, image=image)
        data = boot.pack(folder)
    name = f'{boot.BUNDLE_ID}-{version}.tar.gz'
    (out / name).write_bytes(data)
    import hashlib
    meta = {'version': version, 'file': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'image': image, 'protocol': doc['protocol'], 'key_id': doc['key_id']}
    (out / f'{boot.BUNDLE_ID}-bundle.json').write_text(json.dumps(meta, indent=1) + '\n')
    print(f"{out / name}: {len(data) // 1024} KiB, image {image}, signed with {doc['key_id']}")


def verify(args):
    data = Path(args.file).read_bytes()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / 'b'
        boot.unpack(data, folder)
        doc = boot.verify(folder, boot.pinned_keys(ROOT))
    print(f"{args.file}: {doc['version']} for image {doc['image']}, signed with {doc['key_id']}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build')
    b.add_argument('--floorbit', required=True, help='a checkout of Floorbit at FLOORBIT_REF')
    b.add_argument('--key', required=True)
    b.add_argument('--out', default='dist')
    b.add_argument('--version')
    v = sub.add_parser('verify')
    v.add_argument('file')
    args = p.parse_args(argv)
    {'build': build, 'verify': verify}[args.cmd](args)


if __name__ == '__main__':
    main()
