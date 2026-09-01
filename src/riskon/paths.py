"""Filesystem layout for the workstation.

Everything is resolved relative to the repo root so the CLI behaves the same
whether it is run from the repo, from a run directory, or from ``/workspace``
inside the agent image.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

_MARKERS = ("AGENTS.md", "pyproject.toml")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def repo_root() -> Path:
    """Locate the repo root by walking up from the CWD, then from this file."""
    override = os.environ.get("RISKON_HOME")
    if override:
        return Path(override).expanduser().resolve()

    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for candidate in (start, *start.parents):
            if all((candidate / marker).exists() for marker in _MARKERS):
                return candidate

    return Path(__file__).resolve().parents[2]


def _env_dir(var: str, default: Path) -> Path:
    """Honour an env override only when its parent exists.

    The image sets RISKON_RUN_DIR=/workspace/runs, which is right in the agent
    container and wrong on a laptop. Checking the parent lets one image serve
    both without the caller knowing which it is on.
    """
    value = os.environ.get(var)
    if value:
        path = Path(value).expanduser()
        if path.exists() or path.parent.exists():
            return path.resolve()
    return default


def data_dir() -> Path:
    return _env_dir("RISKON_DATA_DIR", repo_root() / "data")


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runs_dir() -> Path:
    return _env_dir("RISKON_RUN_DIR", repo_root() / "runs")


def templates_dir() -> Path:
    return repo_root() / "templates"


def artifacts_dir() -> Path:
    """The delivery counter: the only directory whose contents leave the machine.

    A cloud agent's host collects whatever is under ``<workspace>/artifacts``,
    so this is how a report reaches the person who asked for it. Created on
    demand rather than at import time - a read-only command should not leave an
    empty directory behind.
    """
    return _env_dir("RISKON_ARTIFACTS_DIR", repo_root() / "artifacts")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "run"


def new_run_dir(slug: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = runs_dir() / f"{stamp}-{slugify(slug)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_run() -> Path | None:
    """Resolve the active run: RISKON_RUN, then the pointer file, then latest."""
    env = os.environ.get("RISKON_RUN")
    if env:
        path = Path(env).expanduser().resolve()
        if path.exists():
            return path

    pointer = runs_dir() / ".current"
    if pointer.exists():
        path = Path(pointer.read_text(encoding="utf-8").strip())
        if path.exists():
            return path

    if runs_dir().exists():
        candidates = sorted(p for p in runs_dir().iterdir() if p.is_dir())
        if candidates:
            return candidates[-1]

    return None


def set_current_run(path: Path) -> None:
    runs_dir().mkdir(parents=True, exist_ok=True)
    (runs_dir() / ".current").write_text(str(path.resolve()), encoding="utf-8")
