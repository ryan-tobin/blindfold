"""The enclave image installs engine/requirements.lock: it must pin what pyproject.toml pins."""

import re
import tomllib

from blindfold import load

ENGINE = load.REPO_ROOT / "engine"


def test_lock_matches_pyproject_and_every_package_has_a_hash():
    deps = tomllib.loads((ENGINE / "pyproject.toml").read_text())["project"]["dependencies"]
    lock = (ENGINE / "requirements.lock").read_text()
    blocks = re.findall(r"^([a-z0-9-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}.*\n)+)", lock, re.M)
    pinned = {name: version for name, version, _ in blocks}
    assert len(blocks) == lock.count("=="), "every package needs at least one --hash"
    for dep in deps:
        name, version = dep.split("==")
        assert pinned.get(name) == version, f"{name}: run `make lock`"
