"""Content-bound runtime versions, excluding secrets, user data and tests."""
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re


def runtime_versions(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    paths = sorted((root / 'jobhound').rglob('*.py'))
    paths += [root / name for name in ('run.py', 'requirements.txt', 'pyproject.toml') if (root / name).is_file()]
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    dependencies = {}
    requirements = root / 'requirements.txt'
    if requirements.is_file():
        for line in requirements.read_text(encoding='utf-8').splitlines():
            match = re.match(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)', line)
            if match:
                name = match.group(1).lower()
                try:
                    dependencies[name] = metadata.version(name)
                except metadata.PackageNotFoundError:
                    dependencies[name] = 'missing'
    dependencies['python'] = platform.python_version()
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 'jobhound-runtime-versions/v1', 'code_sha256': digest(files),
            'dependencies_sha256': digest(dependencies), 'files': files, 'dependencies': dependencies}
