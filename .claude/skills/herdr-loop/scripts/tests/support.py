"""Fixtures shared by the tests: a small backlog, and an item driven to the
point where it has been graded."""

from __future__ import annotations

import json
import re
import signal
import stat
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from hl import items, spawn
from hl.reap import Proc
from hl.common import write_json
from hl.herdr import Herdr, HerdrError, Reply

KIT = Path(__file__).resolve().parents[2]

BACKLOG_A = """# Stream A

Human-only preamble. Nothing here reaches a model.

---

## A-1 — First item

**Scope.** Build the first thing.

It has a second paragraph.

**Accept.**
1. `make test` exits 0.
2. `grep -c first out.txt` prints
   `1`.

**Verify.** `true`
Wait. 5

---

## A-2 — Second item

**Scope.** Build on the first.

**Accept.**
1. It works with the first.

**Depends.** A-1.
"""

BACKLOG_B = """## B-1 - Third item, hyphen heading

**Scope.** Needs the second item and one from another program.

**Accept.**
1. One.
2. Two.
3. Three.

**Depends.** A-2 and X-9.
"""


# A stand-in for `gh` that answers from the JSON file named by HL_FAKE_GH
# and records every call it receives.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["HL_FAKE_GH"]
state = json.load(open(path))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def done(code=0):
    json.dump(state, open(path, "w"))
    sys.exit(code)
if args[:2] == ["pr", "view"]:
    fields = args[args.index("--json") + 1].split(",")
    print(json.dumps({k: state["pr"].get(k) for k in fields}))
    done()
if args[:2] == ["pr", "checks"]:
    if state.get("checks") is None:
        sys.stderr.write("no required checks reported on the 'main' branch\n")
        done(1)
    print(json.dumps(state["checks"]))
    done(state.get("checks_rc", 0))
if args[:2] == ["pr", "diff"]:
    # What GitHub says for a pull request of more than 300 files. The kit reads the diff from the clone instead.
    sys.stderr.write("could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of files (300).\n")
    done(1)
if args[:2] == ["pr", "list"]:
    print(json.dumps(state.get("pr_list", [{"number": state["pr"]["number"]}])))
    done()
if args[:1] == ["api"] and args[1].endswith("/protection/required_status_checks"):
    # What the base branch's protection requires; none named means GitHub's 404 for an unprotected branch.
    if state.get("protection_error"):
        sys.stderr.write(state["protection_error"] + "\n")
        done(1)
    if state.get("required_names") is None:
        sys.stderr.write("gh: Branch not protected (HTTP 404)\n")
        done(1)
    print(json.dumps(state["required_names"]))
    done()
if args[:1] == ["api"] and "/rules/branches/" in args[1]:
    print(json.dumps(state.get("ruleset_names", [])))
    done()
if args[:1] == ["api"]:
    sys.stdout.write(state.get("merged_message", "A-1: add the feature (#7)\n"))
    done()
if args[:2] == ["pr", "merge"]:
    if state.get("merge_rc", 0):
        sys.stderr.write("GraphQL: Pull request is not mergeable\n")
        done(state["merge_rc"])
    state["pr"]["state"] = "MERGED"
    state["pr"]["mergeCommit"] = {"oid": "f" * 40}
    done()
sys.stderr.write("fake gh: unexpected " + " ".join(args) + "\n")
done(64)
'''


def allow_exemptions(run_dir, *patterns) -> None:
    """A run created when its repository's spec named these patterns under `exemptable`."""
    path = Path(run_dir) / "run.json"
    run = json.loads(path.read_text())
    run["policy"] = {**(run.get("policy") or {}), "exemptable": list(patterns)}
    write_json(path, run)


def install_fake_gh(directory) -> Path:
    path = Path(directory) / "gh"
    path.write_text(FAKE_GH)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# What `agent read` showed on the mini on 2026-10-12. Between the trust dialog
# and Claude's first screen there is only the shell's own line; then the input
# box, a rule with the marked line under it.
STARTING_SCREEN = (
    "you@loop-host run-1-A-1 % claude --dangerously-skip-permissions --model claude-sonnet-5-5 --effort low --session\n"
    "-id f964b51c-5995-4101-9c3d-bc08aeacf458\n"
)
RULE = "\u2500" * 80
STALLED = "agent prompt produced no observed working or blocked state within 5000 ms; current status is idle"


AGENTS_PATH_ERROR = "Error: --agents takes a JSON object, or a file path only with --print (-p). Pass the agents as inline JSON, or run with -p to read them from a file."


class FakeHerdr:
    """Herdr as an object: keeps workspaces, panes and agents the way the
    real one answered on the mini, and records every call. `fail` maps the
    first two words of a command to a list of errors to raise, one per call;
    None in the list lets that call through.

    Commands whose reply was never seen, or was seen to be empty (`pane run`
    prints nothing), answer with no fields here, so nothing can lean on them.

    A prompt behaves as the real one did. `agent prompt` answers ok whether
    or not the text reached the agent; only with `--wait` does it say that
    no work began. After the folder-trust dialog is answered the agent is
    called idle and ready at once, but its input box is drawn only at the
    `box_at`-th look at its screen, and a prompt sent before that is lost.
    `fates` decides what becomes of the next prompts, one entry each: "lost"
    (nothing arrives), "typed" (the text arrives and the Enter does not),
    "stuck" (typed, and Enter never starts it), "dialog" (nothing arrives
    and the agent stops on a dialog), "unseen" (taken, though herdr saw no
    work begin and still calls the agent idle), "late" (taken, and herdr
    says working only after it has answered that none began), or None
    (taken). `taken` is what each agent was really
    given; `on_take(name, text)` runs when one is."""

    def __init__(self):
        self.calls = []
        self.fail = {}
        self.agents = {}
        self.pane_text = {}
        self.panes = {}
        self.spaces = []
        self.tabs = {}
        self.screen = ""
        self.keys = []
        self.trust_dialog = False
        self.secrets_say = None
        self.home_say = None
        self.shells_say = None
        self.box_at = 2
        self.fates = []
        self.on_take = None
        self.taken = {}
        self.typed = {}
        self._starting = {}
        self._n = 0

    workspaces = Herdr.workspaces
    find_workspace = Herdr.find_workspace
    create_workspace = Herdr.create_workspace

    def called(self, *words):
        return [c for c in self.calls if c[: len(words)] == words]

    def labels(self):
        return [w["label"] for w in self.spaces]

    def open_space(self, workspace_id, label):
        """A workspace that was there before the test's subject ran."""
        self.spaces.append({"workspace_id": workspace_id, "label": label})

    def _flag(self, args, name):
        return args[args.index(name) + 1]

    def tab_panes(self):
        """Panes opened as tabs, leaving out the pane each workspace opened with."""
        return {pane: record for pane, record in self.panes.items() if not record.get("first")}

    def _pane(self, workspace, cwd, label, first=False):
        self._n += 1
        pane, tab = f"{workspace}:p{self._n}", f"{workspace}:t{self._n}"
        self.panes[pane] = {"shell_pid": 5000 + self._n, "processes": [], "cwd": cwd, "tab": tab, "first": first}
        self.tabs[tab] = label
        return {"root_pane": {"pane_id": pane, "tab_id": tab}, "tab": {"tab_id": tab, "label": label}}

    def call(self, *args, timeout=None):
        self.calls.append(args)
        queued = self.fail.get(args[:2])
        if queued:
            error = queued.pop(0)
            if error:
                raise error
        return Reply(self._answer(*args), " ".join(args[:2]))

    def _answer(self, *args):
        key = args[:2]
        if key == ("workspace", "list"):
            return {"workspaces": list(self.spaces), "type": "workspace_list"}
        if key == ("workspace", "create"):
            taken = {w["workspace_id"] for w in self.spaces} | {pane.split(":")[0] for pane in self.panes}
            made = {"workspace_id": next(f"w{k}" for k in range(1, 99) if f"w{k}" not in taken), "label": self._flag(args, "--label")}
            self.spaces.append(made)
            # A workspace opens with one tab holding one pane.
            first = self._pane(made["workspace_id"], self._flag(args, "--cwd"), "1", first=True)
            return {"workspace": made, **first, "type": "workspace_created"}
        if key == ("tab", "create"):
            workspace = self._flag(args, "--workspace")
            if workspace not in {w["workspace_id"] for w in self.spaces}:
                raise HerdrError("workspace_not_found", f"no workspace {workspace}")
            return {**self._pane(workspace, self._flag(args, "--cwd"), self._flag(args, "--label")), "type": "tab_created"}
        if key == ("tab", "rename"):
            if args[2] not in self.tabs:
                raise HerdrError("tab_not_found", f"tab {args[2]} not found")
            self.tabs[args[2]] = args[3]
            return {"tab": {"tab_id": args[2], "label": args[3]}, "type": "tab_info"}
        if key == ("pane", "run"):
            self.panes[args[2]].setdefault("ran", []).append(args[3])
            return {}
        if key == ("pane", "wait-output"):
            shells = re.fullmatch(r"hl-shells \(ok ([0-9a-f]+)-ready\|FAILED \1\)", self._flag(args, "--regex"))
            if shells:
                # The line that has the worker's shells write themselves down; `shells_say` is what it printed instead.
                return {"type": "output_matched", "matched_line": (self.shells_say or "hl-shells ok {nonce}-ready").format(nonce=shells.group(1))}
            home = re.fullmatch(r"hl-home \(ok ([0-9a-f]+)-ready\|FAILED \1:\)", self._flag(args, "--regex"))
            if home:
                # The line that sets the program's Claude sign-in; `home_say` is what it printed instead of the ready line.
                return {"type": "output_matched", "matched_line": (self.home_say or "hl-home ok {nonce}-ready").format(nonce=home.group(1))}
            # hl-env.sh prints one of two lines; `secrets_say` is what it printed instead of the ready line.
            nonce = re.fullmatch(r"hl-env \(ok ([0-9a-f]+)-ready\|FAILED \1:\)", self._flag(args, "--regex")).group(1)
            return {"type": "output_matched", "matched_line": (self.secrets_say or "hl-env ok {nonce}-ready").format(nonce=nonce)}
        if key == ("agent", "start"):
            name, kind, pane = args[2], self._flag(args, "--kind"), self._flag(args, "--pane")
            argv = list(args[args.index("--") + 1 :])
            self._n += 1
            given = argv[argv.index("--agents") + 1] if "--agents" in argv else "{}"
            if kind == "claude" and not given.lstrip().startswith("{") and not {"-p", "--print"} & set(argv):
                # What Claude Code 2.1.296 does in a terminal: it prints this and exits 1, and Herdr waits out its timeout.
                self.pane_text[pane] = AGENTS_PATH_ERROR + "\n"
                raise HerdrError("timeout", "timed out waiting for agent startup")
            info = {"name": name, "pane_id": pane, "state_change_seq": 1, "agent_status": "idle", "interactive_ready": True}
            self.panes[pane]["processes"] = [{"argv0": "claude" if kind == "claude" else "cursor-agent", "pid": 7000 + self._n}]
            if self.trust_dialog:
                info.update(agent_status="blocked", interactive_ready=False, launch_pending=True)
                self.agents[name] = info
                raise HerdrError("agent_not_ready", f"agent {name} is blocked during startup and is not ready for prompts")
            self.agents[name] = info
            return {"agent": info, "argv": argv, "type": "agent_started"}
        if key == ("agent", "send-keys"):
            self.keys.append(args[3:])
            info = self.agents[args[2]]
            if info.get("launch_pending"):
                # The dialog is answered: herdr says idle and ready, and Claude has drawn nothing yet.
                info.update(agent_status="idle", interactive_ready=True, launch_pending=False)
                self._starting[args[2]] = self.box_at - 1
            elif args[3:] == ("enter",) and args[2] in self.typed and not self._starting.get(args[2]):
                text, stuck = self.typed[args[2]]
                if not stuck:
                    del self.typed[args[2]]
                    self._take(args[2], text)
            return {"type": "ok"}
        if key == ("agent", "wait"):
            info = self.agents[args[2]]
            wanted = {args[i + 1] for i, word in enumerate(args) if word == "--until"}
            if info["agent_status"] not in wanted:
                raise HerdrError("timeout", "timed out waiting for agent status")
            return {"agent": dict(info), "type": "agent_info"}
        if key == ("agent", "prompt"):
            if args[2] not in self.agents:
                raise HerdrError("agent_not_found", f"no agent named {args[2]}")
            info = self.agents[args[2]]
            if info["agent_status"] == "blocked":
                raise HerdrError("agent_blocked", f"agent {args[2]} is blocked; nothing was sent")
            fate = self.fates.pop(0) if self.fates else None
            if self._starting.get(args[2]):
                fate = "lost"
            if fate in ("typed", "stuck"):
                self.typed[args[2]] = (args[3], fate == "stuck")
            elif fate == "dialog":
                info["agent_status"] = "blocked"
            elif fate != "lost":
                self._take(args[2], args[3], seen=fate != "unseen")
            if fate and "--wait" in args:
                raise HerdrError("agent_prompt_stalled", STALLED)
            return {"agent": dict(info), "type": "agent_prompted"}
        if key == ("pane", "process-info"):
            pane = self.panes.get(self._flag(args, "--pane"))
            if pane is None:
                raise HerdrError("pane_not_found", "no such pane")
            return {"process_info": {"shell_pid": pane["shell_pid"], "foreground_processes": pane["processes"]}}
        if key == ("pane", "close"):
            if args[2] not in self.panes:
                raise HerdrError("pane_not_found", "no such pane")
            self.tabs.pop(self.panes.pop(args[2]).get("tab"), None)
            self.agents = {k: v for k, v in self.agents.items() if v["pane_id"] != args[2]}
            # A workspace goes when its last pane does.
            workspace = args[2].split(":")[0]
            if not any(pane.startswith(workspace + ":") for pane in self.panes):
                self.spaces = [w for w in self.spaces if w["workspace_id"] != workspace]
            return {"type": "ok"}
        if key == ("pane", "send-keys"):
            self.keys.append(args[3:])
            return {}
        raise AssertionError(f"unexpected herdr call: {args}")

    def _take(self, name, text, seen=True):
        self.taken.setdefault(name, []).append(text)
        if seen:
            self.agents[name]["agent_status"] = "working"
        if self.on_take:
            self.on_take(name, text)

    def agent(self, name):
        return self.agents.get(name)

    def text(self, *args):
        self.calls.append(args)
        if args[:2] == ("pane", "read") and args[2] in self.pane_text:
            return self.pane_text[args[2]]
        name = args[2] if args[:2] == ("agent", "read") else None
        if name in self._starting:
            if self._starting[name]:
                self._starting[name] -= 1
                return STARTING_SCREEN
            return self._claude_screen(name)
        if name in self.typed or name in self.taken:
            return self._claude_screen(name)
        return self.screen

    def _claude_screen(self, name):
        def wrapped(text):
            return "\n  ".join(text[i : i + 76] for i in range(0, len(text), 76))

        said = "".join(f"\u276f {wrapped(text)}\n\n\u23fa ok\n\n" for text in self.taken.get(name, []))
        box = wrapped(self.typed[name][0]) if name in self.typed else 'Try "fix lint errors"'
        return f"{said}{RULE}\n\u276f\u00a0{box}\n{RULE}\n  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)\n"


class RunCase(unittest.TestCase):
    """A temp directory holding the two backlog files and an empty run dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.file_a = self.tmp / "A-ITEMS.md"
        self.file_b = self.tmp / "B-ITEMS.md"
        self.file_a.write_text(BACKLOG_A)
        self.file_b.write_text(BACKLOG_B)
        self.run_dir = self.tmp / "run"

    def init(self, *files):
        return items.init_run(self.run_dir, "run-1", "owner/repo", [str(f) for f in files or (self.file_a, self.file_b)])

    def item_dir(self, item_id="A-1") -> Path:
        return items.item_dir(self.run_dir, item_id)

    def write(self, item_id, name, text):
        path = self.item_dir(item_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def agree(self, item_id="A-1", session="eval-agree"):
        """Install the Accept list as the contract and have it agreed."""
        items.contract_from_accept(self.run_dir, item_id)
        entry = items.start_phase(self.run_dir, item_id, "agree", session=session)
        self.write(item_id, f"agree-{entry['n']}.md", "Every criterion holds against main.\n\nAGREED\n")
        return entry

    def build(self, item_id="A-1", session="build-1"):
        entry = items.start_phase(self.run_dir, item_id, "build", session=session, engine="claude")
        self.write(item_id, f"build-{entry['n']}.md", "**Serve.** `true`\n**Ready.** COMMAND `true`\n")
        return entry

    def grade(self, item_id="A-1", session="eval-1", head="a" * 40, verdict="PASS", results=None):
        entry = items.start_phase(self.run_dir, item_id, "grade", session=session, head_sha=head)
        count = items.load_item(self.run_dir, item_id)["contract"]["criteria"]
        results = results or ["pass"] * count
        self.write(
            item_id,
            f"verdict-{entry['n']}.json",
            json.dumps(
                {
                    "verdict": verdict,
                    "criteria": [
                        {"n": i + 1, "result": r, "evidence": [], "note": "seen"} for i, r in enumerate(results)
                    ],
                    "findings": [],
                    "how_run": "make test",
                }
            ),
        )
        return entry


class FakeProcs:
    """A process table. `me` runs in session 890, under its tool shell 890,
    the manager 880, its pane shell 870 and the Herdr server 700. A process
    ends on the first signal listed for it in `ends_on` (TERM by default);
    one listed with nothing ends on none."""

    def __init__(self, rows=(), envs=None, ends_on=None):
        manager = [Proc(900, 890, 501, 10.0, "python3 hl-spawn close"), Proc(890, 880, 501, 10.0, "/bin/zsh -c hl-spawn close"),
                   Proc(880, 870, 501, 5.0, "claude --model claude-opus-5-5"), Proc(870, 700, 501, 4.0, "-zsh"), Proc(700, 1, 501, 1.0, "herdr server"),
                   Proc(1, 0, 0, 0.0, "/sbin/launchd")]
        self.rows = {p.pid: p for p in [*manager, *rows]}
        self.sessions = {900: 890, 890: 890, 880: 870, 870: 870, 700: 700, 1: 1}
        self.envs = envs or {}
        self.ends_on = ends_on or {}
        self.killed = []
        self.looked = 0
        self.t = 0.0

    def add(self, pid, session, command, start=100.0, ppid=1, uid=501, zombie=False):
        self.rows[pid] = Proc(pid, ppid, uid, start, command, zombie)
        self.sessions[pid] = session

    def table(self):
        self.looked += 1
        return list(self.rows.values())

    def session(self, pid):
        return self.sessions.get(pid) if pid in self.rows else None

    def environ(self, pid):
        return self.envs.get(pid, [])

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        if pid in self.rows and sig in self.ends_on.get(pid, (signal.SIGTERM,)):
            del self.rows[pid]

    def me(self):
        return 900

    def uid(self):
        return 501

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeEnvs:
    def __init__(self, absent=()):
        self.absent = set(absent)
        self.asked = []

    def has(self, pid, name):
        self.asked.append((pid, name))
        return name not in self.absent


class SpawnCase(RunCase):
    """A loop root with the real kit linked in, one item with an agreed
    contract, and herdr replaced by an object."""

    def setUp(self):
        super().setUp()
        self.root = (self.tmp / "root").resolve()
        (self.root / ".claude" / "skills").mkdir(parents=True)
        (self.root / ".claude" / "skills" / "herdr-loop").symlink_to(KIT)
        write_json(
            self.root / "program.json",
            {
                "program": "canary",
                "base": "main",
                "worker_claude_ref": "op://Loop/worker-claude/password",
                "gh_token_ref": "op://Loop/loop-gh/password",
                "cursor_model": "composer-2.5[fast=false]",
            },
        )
        self.run_dir = self.root / "runs" / "run-1"
        self.init()
        self.agree()
        self.worktree = self.root / "repos" / "main" / "scratch" / ".worktrees" / "run-1-A-1"
        self.worktree.mkdir(parents=True)
        items.set_fields(self.run_dir, "A-1", ["branch=hl/run-1/A-1", f"worktree={self.worktree}", "port=8401"])
        self.herdr = FakeHerdr()
        self.envs = FakeEnvs()
        patcher = mock.patch("hl.spawn.claim.check_pins", return_value={"ok": True, "bins": {"herdr": "/bin/herdr"}})
        self.check_pins = patcher.start()
        self.addCleanup(patcher.stop)

    def opts(self, *extra, phase="build", engine="claude"):
        argv = ["--root", str(self.root), "--run-dir", str(self.run_dir), "start", "--id", "A-1"]
        args = spawn._parser().parse_args(argv + ["--phase", phase, "--engine", engine, *extra])
        args.sets = spawn._sets(args.sets, args.note_file)
        return args

    def start(self, *extra, phase="build", engine="claude", item_id="A-1"):
        opts = self.opts(*extra, phase=phase, engine=engine)
        return spawn.start(
            self.root, self.run_dir, item_id, phase, engine, self.herdr, opts, envs=self.envs, sleep=lambda s: None
        )

    def attempts(self):
        return items.load_attempts(self.run_dir, "A-1")

    def ship(self, *item_ids, verify="verified"):
        """Items as hl-merge and the deploy check leave them: merged, and verified."""
        for item_id in item_ids or ("A-1", "A-2", "B-1"):
            item = items.load_item(self.run_dir, item_id)
            item["status"] = "merged"
            if verify:
                item["fields"]["verify"] = verify
            items.save_item(self.run_dir, item)

    def agent_argv(self, n=-1):
        call = self.herdr.called("agent", "start")[n]
        return list(call[call.index("--") + 1 :])

    def finish_build(self, n=1):
        """What a builder leaves, then the manager's close."""
        self.write("A-1", f"build-{n}.md", "**Serve.** `PORT=8401 python3 app.py`\n**Ready.** URL `http://127.0.0.1:8401/`\n")
        self.write("A-1", f"build-{n}.done", "")
        spawn.close(self.run_dir, "A-1", "build", n, self.herdr, sleep=lambda s: None)
        items.set_fields(self.run_dir, "A-1", ["pr=7", "pr_url=https://example.invalid/pull/7", "head_sha=" + "a" * 40])

    def finish_grade(self, n=1, verdict="FAIL"):
        self.write("A-1", f"verdict-{n}.json", json.dumps({"verdict": verdict}))
        self.write("A-1", f"grade-{n}.done", "")
        spawn.close(self.run_dir, "A-1", "grade", n, self.herdr, sleep=lambda s: None)
