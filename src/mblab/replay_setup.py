"""Install replay dependencies without copying them into MazeBench itself."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mblab.official import official_runtime_root


def replay_project_root(repo_root: Path) -> Path:
    development = repo_root / "replay"
    installed = Path(sys.prefix) / "share" / "mazebench-control-center" / "replay"
    return development if (development / "package.json").is_file() else installed


def setup_replay(repo_root: Path) -> dict[str, Any]:
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to install replay dependencies")
    project = replay_project_root(repo_root)
    if not (project / "package-lock.json").is_file():
        raise RuntimeError(f"replay lockfile is missing: {project}")
    subprocess.run(
        [
            npm,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--prefix",
            str(project),
        ],
        check=True,
    )
    runtime = official_runtime_root()
    runtime_modules = runtime / "node_modules"
    runtime_modules.mkdir(exist_ok=True)
    links = {}
    for package in ("playwright-core", "three"):
        source = project / "node_modules" / package
        target = runtime_modules / package
        if not source.is_dir():
            raise RuntimeError(f"npm did not install {package}")
        if target.is_symlink():
            target.unlink()
        if not target.exists():
            target.symlink_to(source, target_is_directory=True)
            state = "linked"
        else:
            # Respect a real package directory from MazeBench or an older local
            # setup rather than deleting software we do not own.
            state = "existing-runtime-package"
        links[package] = {"source": str(source), "target": str(target), "state": state}
    return {"project": str(project), "runtime": str(runtime), "packages": links}
