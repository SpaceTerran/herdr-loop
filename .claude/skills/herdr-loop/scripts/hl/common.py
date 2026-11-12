"""Shared helpers: atomic JSON state files, the clock, and the refusal type."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def absolute(path: str) -> str:
    """A path given on a command line, made absolute against the directory
    the command was run from: the argument type of every `--root`,
    `--run-dir` and `--repo-dir`. The scripts hand these paths on to places
    with another working directory (a serve script run in the worktree's
    pane, `git -C <clone>`), where a relative one names something else. A
    path that is already absolute is kept as it was typed: symlinks are not
    resolved."""
    return os.path.abspath(path)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data) -> None:
    """Write-then-rename, so a crash never leaves a half-written state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text: str) -> None:
    """Write-then-rename, as for a state file: a reader finds the whole text or the file as it was."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def emit(data) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


class Refusal(Exception):
    """A gate said no. Carries every reason, so one run names all of them."""

    def __init__(self, reasons):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))
