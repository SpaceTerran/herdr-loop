"""The herdr command line as function calls, and the record a worker leaves
in its item directory.

A herdr command's exit status says whether it worked. Most print one JSON
object, `{"result": …}` on success and `{"error": {"code", "message"}}` on
failure; some (`pane run`) succeed without printing anything. Nothing here
interprets what an agent's state means; callers do.

How a program is laid out in herdr, in the operator's words (2026-10-11):
the manager works in one tab of a workspace; a set of work that is all
related gets a workspace of its own; agents that run at the same time are
tabs inside that workspace. So a run has one workspace, and every worker and
served build of the run is a tab in it.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

from .common import Refusal, read_json, write_json

class HerdrError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")

    @property
    def not_found(self) -> bool:
        return "not_found" in self.code


class Reply(dict):
    """What a command answered. Reading a field herdr did not send raises
    HerdrError rather than KeyError, so the caller's own cleanup still runs."""

    def __init__(self, data: dict, command: str):
        super().__init__(data)
        self.command = command

    def __getitem__(self, key):
        try:
            value = super().__getitem__(key)
        except KeyError:
            sent = ", ".join(sorted(self)) or "nothing"
            raise HerdrError("unexpected_reply", f"herdr {self.command} answered without `{key}` (it sent: {sent})") from None
        return Reply(value, self.command) if isinstance(value, dict) else value


class Herdr:
    def __init__(self, binary: str | None = None, env=None, timeout: float = 60):
        self.bin = binary or shutil.which("herdr")
        if not self.bin:
            raise Refusal("herdr is not available (pass --herdr)")
        self.env = dict(os.environ if env is None else env)
        self.timeout = timeout

    def call(self, *args: str, timeout: float | None = None) -> Reply:
        command = " ".join(args[:2])
        try:
            proc = subprocess.run(
                [self.bin, *args], env=self.env, capture_output=True, text=True, timeout=timeout or self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise HerdrError("timeout", f"herdr {command} gave no answer in {exc.timeout:.0f}s") from exc
        data = None
        for stream in (proc.stdout, proc.stderr):
            try:
                data = json.loads(stream)
                break
            except ValueError:
                continue
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            raise HerdrError(str(data["error"].get("code", "error")), str(data["error"].get("message", "")))
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())[:300]
            raise HerdrError(f"exit_{proc.returncode}", f"herdr {command}: {detail}")
        # Exit 0 is the success. A command that printed no JSON object answered with no fields.
        result = data.get("result", data) if isinstance(data, dict) else {}
        return Reply(result if isinstance(result, dict) else {}, command)

    def text(self, *args: str) -> str:
        """A command that prints the pane's text instead of JSON (`pane read`, `agent read`)."""
        try:
            proc = subprocess.run([self.bin, *args], env=self.env, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise HerdrError("timeout", f"herdr {' '.join(args[:2])} gave no answer in {exc.timeout:.0f}s") from exc
        if proc.returncode != 0:
            raise HerdrError(f"exit_{proc.returncode}", (proc.stderr.strip() or proc.stdout.strip())[:300])
        return proc.stdout

    def workspaces(self) -> list[dict]:
        return self.call("workspace", "list").get("workspaces", [])

    def find_workspace(self, label: str) -> str | None:
        """The id of the workspace with this label, or None."""
        return next((w["workspace_id"] for w in self.workspaces() if w.get("label") == label), None)

    def create_workspace(self, label: str, cwd) -> dict:
        """A new workspace. Herdr opens it with one tab holding one pane, a shell in `cwd`."""
        made = self.call("workspace", "create", "--label", label, "--cwd", str(cwd), "--no-focus")
        return {
            "workspace": made["workspace"]["workspace_id"],
            "pane": made["root_pane"]["pane_id"],
            "tab": made.get("tab", {}).get("tab_id"),
        }

    def agent(self, name: str) -> dict | None:
        """What herdr knows about a named agent, or None when it has no such agent."""
        try:
            return self.call("agent", "get", name)["agent"]
        except HerdrError as error:
            if error.not_found:
                return None
            raise


# ---------------------------------------------------------------- worker records


def worker_path(item_dir, phase: str, n: int) -> Path:
    return Path(item_dir) / f"{phase}-{n}.worker.json"


def write_worker(item_dir, phase: str, n: int, record: dict) -> Path:
    path = worker_path(item_dir, phase, n)
    write_json(path, record)
    return path


def read_worker(item_dir, phase: str, n: int) -> dict:
    path = worker_path(item_dir, phase, n)
    if not path.exists():
        raise Refusal(f"{path} does not exist: no worker was started for {phase} {n} (hl-spawn writes it)")
    return read_json(path)


def claude_homes() -> list[Path]:
    """The Claude configuration directories a session's transcript can be
    in: the one this process was started with (`CLAUDE_CONFIG_DIR`), the
    default `~/.claude`, and every `~/.claude-<name>` beside it, which is
    where a program's `claude_config_dir` must be. A manager, its workers and
    the scripts they run need not share one: the sessions of a program that
    moved to another sign-in are on both sides of the move."""
    home = Path(os.path.expanduser("~"))
    named = [Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()] if os.environ.get("CLAUDE_CONFIG_DIR") else []
    homes: list[Path] = []
    for path in [*named, home / ".claude", *sorted(Path(p) for p in glob.glob(str(home / ".claude-*")))]:
        if path not in homes and path.is_dir():
            homes.append(path)
    return homes


def claude_transcript(session: str) -> Path | None:
    """A Claude session's transcript. The directory name is derived from the
    session's working directory by a rule Claude Code owns, so find the file
    by its session id instead of rebuilding the name, in each directory a
    Claude session may have been started with (`claude_homes`). A session id
    is unique, so the first found is the one."""
    for home in claude_homes():
        found = glob.glob(str(home / "projects" / "*" / f"{glob.escape(session)}.jsonl"))
        if found:
            return Path(found[0])
    return None


def claude_transcripts(session: str) -> list[Path]:
    """A Claude session's transcript and those of its sub-agents, which
    Claude Code keeps beside it under `<session>/subagents/`. Empty when the
    session has no transcript."""
    main = claude_transcript(session) if session else None
    if main is None:
        return []
    return [main] + sorted((main.parent / session / "subagents").glob("*.jsonl"))
