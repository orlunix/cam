#!/usr/bin/env python3
"""Create a deterministic, credential-free Server binary + SDK release."""
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tarfile


def package(binary, destination, target):
    root = Path(__file__).resolve().parents[3]
    version = re.search(r'^version = "([^"]+)"',
                        (root / 'components/cam-router/crates/cam-server/Cargo.toml').read_text(), re.M).group(1)
    entries = [('cam-server', Path(binary))]
    for directory in ['camc_pkg', 'cam/adapters/configs']:
        for path in sorted((root / 'src' / directory).rglob('*')):
            if path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts and path.suffix in ('.py', '.toml', '.md'):
                entries.append(('sdk/' + path.relative_to(root / 'src').as_posix(), path))
    destination = Path(destination)
    with tarfile.open(str(destination), 'w', format=tarfile.USTAR_FORMAT) as archive:
        for name, path in entries:
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o700 if name == 'cam-server' else 0o600
            archive.addfile(info, io.BytesIO(data))
    data = destination.read_bytes()
    manifest = dict(version=version, target=target, size=len(data), sha256=hashlib.sha256(data).hexdigest())
    Path(str(destination) + '.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    package(*sys.argv[1:])
