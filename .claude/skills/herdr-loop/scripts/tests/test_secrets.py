"""hl-secrets: the keeper, its callers, and the scripts that use it.

Nothing here reads a real secret store. The reader is a small script that
counts its own calls and is named in each test root's program.json
(`secrets_reader`); `op` is a stand-in on a path of the test's own; and every
process a test starts has an empty home and a PATH whose first `op` and
`security` are guards that refuse and say they were reached, so a test that
forgot its reader would fail and not spend a request.

The values the reader gives are worked out from the reference, here and in
the reader alike, so no file holds one. Every test ends by stopping its
keeper and searching its loop root for them.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from hl import flow, merge, program, secrets as keeper, spawn, train, verify, wait, watchdog
from hl.common import Refusal, write_json

from .support import KIT, SpawnCase

HL_SECRETS = KIT / "scripts" / "hl-secrets"
HL_ENV = KIT / "scripts" / "hl-env.sh"
ZSH = shutil.which("zsh")

GH_REF = "op://v/gh/password"
CLAUDE_REF = "op://v/claude/password"
NTFY_REF = "op://v/ntfy/password"
SAID = "[ERROR] 2026/10/13 09:14:02 Too many requests. Your service account has been rate-limited"

# A stand-in for the program's reader. It writes the reference it was asked for beside itself, one line a call, and whether
# any of the caller's tokens reached it. What it prints is worked out from the reference and a salt.
READER = r'''#!/usr/bin/env python3
import hashlib, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
def setting(name):
    path = os.path.join(here, name)
    return open(path).read().strip() if os.path.exists(path) else ""
ref = sys.argv[1]
tokens = [k for k in ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if os.environ.get(k)]
with open(os.path.join(here, "reads.log"), "a") as fh:
    fh.write(ref + " " + ",".join(tokens) + "\n")
if setting("delay"):
    time.sleep(float(setting("delay")))
if setting("mode") == "fail" or "missing" in ref:
    sys.stderr.write("%s\n")
    sys.exit(1)
if "empty" in ref:
    sys.exit(0)
print("tok-" + hashlib.sha256((setting("salt") + ref).encode()).hexdigest()[:32])
''' % SAID

# A stand-in for `op`. It answers `service-account ratelimit` from files beside itself and writes down how it
# was called: never the token, only whether it was the one expected. A shell script, so that nothing but the
# shell's own three variables is added to the environment it reports.
OP = r"""#!/bin/sh
here=$(dirname "$0")
{
  printf 'argv=%s\n' "$*"
  [ "$OP_SERVICE_ACCOUNT_TOKEN" = "$(cat "$here/expect")" ] && echo token=expected || echo token=other
  printf 'home=%s\n' "$HOME"
  printf 'home_entries=%s\n' "$(ls -A "$HOME" | wc -l | tr -d ' ')"
  printf 'path=%s\n' "$PATH"
  printf 'others=%s\n' "$(env | sed 's/=.*//' | grep -v -x -e OP_SERVICE_ACCOUNT_TOKEN -e HOME -e PATH -e PWD -e OLDPWD -e SHLVL -e _ | sort | tr '\n' ' ')"
  printf 'stdin=%s\n' "$(cat)"
} > "$here/seen"
cat "$here/stderr" >&2
cat "$here/stdout"
exit "$(cat "$here/rc")"
"""

# What stands first on every test's PATH as `op` and `security`: the kit's own reader would reach them for an op:// or a
# keychain: reference when a test forgot its reader. They refuse, and leave a mark the test's cleanup looks for.
GUARD = """#!/bin/sh
echo "$(basename "$0") $*" >> "$(dirname "$0")/reached"
echo "a test reached $(basename "$0"): no test reads a real secret store" >&2
exit 1
"""

TABLE = """TYPE       ACTION        LIMIT    USED    REMAINING    RESET
token      write         100      0       100          N/A
token      read          1000     212     788          41 minutes from now
account    read_write    1000     846     154          9 hours from now
"""


def fake(ref: str, salt: str = "") -> str:
    """What the stand-in reader prints for a reference."""
    return "tok-" + hashlib.sha256((salt + ref).encode()).hexdigest()[:32]


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class SecretsCase(unittest.TestCase):
    """A loop root whose program.json names the stand-in reader, an empty
    home, and nothing of the caller's tokens in the environment."""

    SETTINGS: dict = {}

    def setUp(self):
        # A socket's path is short. The usual temp directory fits; where it would not, /tmp does.
        usual = Path(tempfile.gettempdir()).resolve()
        short = len(str(usual)) + len("/tmpxxxxxxxx/root/secrets.sock") <= keeper.SOCK_PATH_MAX
        self._tmp = tempfile.TemporaryDirectory(dir=None if short else "/tmp")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.reader = self.bin / "reader"
        self.reader.write_text(READER)
        home = self.tmp / "home"
        home.mkdir()
        self.guard = self.tmp / "guard"
        self.guard.mkdir()
        for name in ("op", "security"):
            (self.guard / name).write_text(GUARD)
            (self.guard / name).chmod(0o755)
        env = {
            "HOME": str(home),
            "ZDOTDIR": str(home),
            "OP_SERVICE_ACCOUNT_TOKEN": "ops_stand-in-for-the-account",
            "PATH": f"{self.guard}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in keeper.TOKEN_VARS:
            os.environ.pop(name, None)
        self.settings()
        self.roots = [self.root]
        self.addCleanup(self.no_real_store_was_reached)
        self.addCleanup(self.nothing_secret_is_left)
        self.addCleanup(self.stop_keepers)

    def settings(self, root=None, **changed):
        base = {
            "program": "canary",
            "base": "main",
            "worker_claude_ref": CLAUDE_REF,
            "gh_token_ref": GH_REF,
            "ntfy_password_ref": NTFY_REF,
            "ntfy_user": "loop-notices",
            "cursor_model": "m",
            "secrets": "keeper",
            "secrets_reader": [sys.executable, str(self.reader)],
        }
        data = {**base, **self.SETTINGS, **changed}
        write_json((root or self.root) / "program.json", {k: v for k, v in data.items() if v is not None})

    def set(self, name: str, value: str) -> None:
        """Change how the stand-in reader behaves: `mode`, `delay`, `salt`."""
        (self.bin / name).write_text(value)

    def reads(self) -> list[str]:
        log = self.bin / "reads.log"
        return [line.split(" ")[0] for line in log.read_text().splitlines()] if log.exists() else []

    def cli(self, *argv, root=None, env=None, timeout=60):
        return subprocess.run(
            [sys.executable, str(HL_SECRETS), "--root", str(root or self.root), *argv],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
        )

    def status(self, root=None) -> dict:
        return json.loads(self.cli("status", root=root).stdout)

    def started(self, root=None) -> int:
        log = (root or self.root) / "secrets.log"
        return sum(1 for line in log.read_text().splitlines() if " started: " in line) if log.exists() else 0

    def stop_keepers(self):
        """Every keeper a test started is stopped by it, and seen to be gone:
        a keeper holds its root's lock for as long as it lives."""
        left = []
        for root in self.roots:
            lock = root / "secrets.lock"
            if not lock.exists():
                continue
            keeper.stop(root)
            fd = os.open(str(lock), os.O_RDWR)
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            pid = keeper._lock_pid(root)
                            left.append(pid)
                            if pid:
                                os.kill(pid, signal.SIGKILL)
                            break
                        time.sleep(0.02)
            finally:
                os.close(fd)
        self.assertEqual(left, [], "a keeper was still running after it was told to stop")

    def no_real_store_was_reached(self):
        reached = self.guard / "reached"
        self.assertFalse(reached.exists(), reached.read_text() if reached.exists() else "")

    def nothing_secret_is_left(self):
        values = [fake(ref, salt).encode() for ref in (GH_REF, CLAUDE_REF, NTFY_REF) for salt in ("", "rotated")]
        found = []
        for root in self.roots:
            for path in sorted(root.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    found += [str(path.relative_to(root)) for value in values if value in data]
        self.assertEqual(found, [], "a file under the loop root holds a secret")

    def kill(self, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"pid {pid} did not die")


# ---------------------------------------------------------------- the rules, without a process


class RulesTest(SecretsCase):
    """`Keeper.handle` with a reader that is a function and a clock that is
    moved by hand."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.asked: list[str] = []
        self.says: dict[str, tuple] = {}
        # One hand-moved clock for both: what is counted, and what time of day it is.
        self.keeper = keeper.Keeper(self.root, reader=self.read, clock=self.clock, wall=self.clock)

    def read(self, ref, argv):
        self.asked.append(ref)
        return self.says.get(ref, (fake(ref), None))

    def get(self, name="gh") -> dict:
        return self.keeper.handle({"op": "get", "name": name})

    def test_the_keeper_notes_how_its_last_read_went(self):
        self.assertIsNone(self.keeper.status()["last_read"])
        self.get()
        state = self.keeper.status()
        self.assertEqual((state["last_read"]["name"], state["last_read"]["ok"], state["failed_reads"]), ("gh", True, 0))
        self.assertEqual(sorted(state["last_read"]), ["at", "name", "ok"])
        self.keeper.forget()
        self.says[GH_REF] = (None, "rate-limited")
        self.get()
        state = self.keeper.status()
        self.assertEqual((state["last_read"]["ok"], state["failed_reads"]), (False, 1))

    def test_one_read_serves_every_later_request(self):
        answers = [self.get() for _ in range(25)]
        self.assertEqual({a["value"] for a in answers}, {fake(GH_REF)})
        self.assertEqual(self.asked, [GH_REF])
        self.assertEqual((self.keeper.counts["reads"], self.keeper.counts["from_memory"]), (1, 24))

    def test_each_name_is_its_own_reference_and_its_own_read(self):
        self.assertEqual(self.get("gh")["value"], fake(GH_REF))
        self.assertEqual(self.get("claude")["value"], fake(CLAUDE_REF))
        self.assertEqual(self.get("ntfy")["value"], fake(NTFY_REF))
        self.get("gh"), self.get("claude")
        self.assertEqual(self.asked, [GH_REF, CLAUDE_REF, NTFY_REF])

    def test_a_value_is_kept_for_its_lifetime_and_read_again_after_it(self):
        self.get()
        self.clock.t += 12 * 3600 - 1
        self.get()
        self.assertEqual(self.asked, [GH_REF])
        self.clock.t += 1
        self.assertEqual(self.get()["value"], fake(GH_REF))
        self.assertEqual(self.asked, [GH_REF, GH_REF])

    def test_the_lifetime_is_the_programs_setting(self):
        self.settings(secrets_ttl_s=90)
        self.get()
        self.clock.t += 89
        self.get()
        self.assertEqual(len(self.asked), 1)
        self.clock.t += 1
        self.get()
        self.assertEqual(len(self.asked), 2)

    def test_the_defaults_are_twelve_hours_held_ten_minutes_failed_and_six_hours_idle(self):
        self.assertEqual(program.load(self.root)["secrets_ttl_s"], 12 * 3600)
        self.assertEqual((keeper.FAILURE_TTL_S, keeper.IDLE_EXIT_S), (10 * 60, 6 * 3600))
        self.assertEqual(keeper._parser().parse_args(["--root", "r", "serve"]).idle_s, 6 * 3600)
        # And a program that says nothing about secrets does not use the keeper.
        self.assertEqual(program.DEFAULTS["secrets"], "direct")

    def test_a_lifetime_that_is_not_a_time_is_refused_before_anything_is_read(self):
        for bad in (0, -5, "12h", True):
            self.settings(secrets_ttl_s=bad)
            answer = self.get()
            self.assertFalse(answer["ok"])
            self.assertIn("secrets_ttl_s", answer["error"])
        self.assertEqual(self.asked, [])

    def test_a_failed_read_is_answered_from_memory_for_ten_minutes_and_not_read_again(self):
        self.says[GH_REF] = (None, SAID)
        first = self.get()
        self.assertEqual(first, {"ok": False, "error": f"the reader did not give `gh` ({GH_REF}): {SAID}"})
        self.clock.t += 599
        again = [self.get() for _ in range(40)]
        self.assertEqual(self.asked, [GH_REF])
        for answer in again:
            self.assertFalse(answer["ok"])
            self.assertIn(first["error"], answer["error"])
            self.assertIn("not tried again for 10 minutes", answer["error"])
        self.assertEqual(self.keeper.counts, {"reads": 1, "failed_reads": 1, "from_memory": 0, "past_lifetime": 0, "refused_from_memory": 40})
        self.assertEqual(sorted(self.keeper.status()["failed"]), ["gh"])
        # Ten minutes on it is no longer listed as failed; it is read once more, and what that read says is the answer.
        self.says[GH_REF] = (fake(GH_REF), None)
        self.clock.t += 1
        self.assertEqual(self.keeper.status()["failed"], {})
        self.assertEqual(self.get(), {"ok": True, "value": fake(GH_REF)})
        self.assertEqual(self.asked, [GH_REF, GH_REF])
        self.assertEqual(self.keeper.status()["failed"], {})

    def test_a_failure_of_one_secret_does_not_stand_in_the_way_of_another(self):
        self.says[CLAUDE_REF] = (None, SAID)
        self.assertFalse(self.get("claude")["ok"])
        self.assertEqual(self.get("gh"), {"ok": True, "value": fake(GH_REF)})

    def test_a_read_that_gives_nothing_is_a_failed_read(self):
        self.says[GH_REF] = ("", None)
        answer = self.get()
        self.assertEqual(answer, {"ok": False, "error": f"the reader did not give `gh` ({GH_REF}): it said nothing"})
        self.get()
        self.assertEqual((self.asked, self.keeper.counts["failed_reads"]), ([GH_REF], 1))

    def test_a_value_past_its_lifetime_stays_in_use_when_it_cannot_be_read_again(self):
        self.get()
        self.clock.t += 12 * 3600
        self.says[GH_REF] = (None, SAID)
        # The read at its lifetime fails. The caller gets the value there was, and that cost one read.
        self.assertEqual(self.get(), {"ok": True, "value": fake(GH_REF)})
        self.assertEqual(self.asked, [GH_REF, GH_REF])
        # For ten minutes nothing more is read, and the value goes on being given.
        self.clock.t += 599
        for _ in range(20):
            self.assertEqual(self.get(), {"ok": True, "value": fake(GH_REF)})
        self.assertEqual(self.asked, [GH_REF, GH_REF])
        self.assertEqual(self.keeper.counts, {"reads": 2, "failed_reads": 1, "from_memory": 0, "past_lifetime": 21, "refused_from_memory": 0})
        # Ten minutes after the failed read it is tried once more. Still refused, still the value.
        self.clock.t += 1
        self.assertEqual(self.get(), {"ok": True, "value": fake(GH_REF)})
        self.assertEqual((len(self.asked), self.keeper.counts["failed_reads"], self.keeper.counts["past_lifetime"]), (3, 2, 22))

    def test_status_says_a_value_is_past_its_lifetime_since_when_and_why_and_never_the_value(self):
        self.get("claude")
        read_at = self.clock.t
        self.get()
        self.clock.t += 12 * 3600 + 90
        self.says[GH_REF] = (None, SAID)
        self.get()
        self.clock.t += 30
        since = datetime.fromtimestamp(read_at + 12 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status = self.keeper.handle({"op": "status"})
        self.assertEqual(
            status["held"],
            {
                # Nobody has asked for this one since its lifetime ended: it is old, and nothing has failed.
                "claude": {"age_s": 12 * 3600 + 120},
                "gh": {"age_s": 12 * 3600 + 120, "past_lifetime_since": since, "last_error": f"the reader did not give `gh` ({GH_REF}): {SAID}"},
            },
        )
        self.assertEqual(status["failed"]["gh"]["age_s"], 30)
        self.assertEqual(status["past_lifetime"], 1)
        self.assertNotIn(fake(GH_REF), json.dumps(status))
        # The next failed read is what `last_error` then says; since when does not move.
        self.clock.t += 570
        self.says[GH_REF] = (None, "the server is not reachable")
        self.get()
        shown = self.keeper.status()["held"]["gh"]
        self.assertEqual((shown["past_lifetime_since"], shown["last_error"]), (since, f"the reader did not give `gh` ({GH_REF}): the server is not reachable"))
        # A failure leaves the failed list after its ten minutes; the value is still marked.
        self.clock.t += 600
        status = self.keeper.status()
        self.assertEqual((status["failed"], status["held"]["gh"]["past_lifetime_since"]), ({}, since))

    def test_a_read_that_succeeds_replaces_a_value_kept_past_its_lifetime(self):
        self.get()
        self.clock.t += 12 * 3600
        self.says[GH_REF] = (None, SAID)
        self.get()
        self.clock.t += 600
        self.says[GH_REF] = (fake(GH_REF, "rotated"), None)
        self.assertEqual(self.get(), {"ok": True, "value": fake(GH_REF, "rotated")})
        status = self.keeper.status()
        self.assertEqual((status["held"], status["failed"]), ({"gh": {"age_s": 0}}, {}))
        # And its lifetime starts with that read.
        self.clock.t += 12 * 3600 - 1
        self.get()
        self.assertEqual((len(self.asked), self.keeper.counts["from_memory"]), (3, 1))

    def test_forget_drops_a_value_kept_past_its_lifetime_too(self):
        self.get()
        self.clock.t += 12 * 3600
        self.says[GH_REF] = (None, SAID)
        self.assertTrue(self.get()["ok"])
        self.assertEqual(self.keeper.handle({"op": "forget"}), {"ok": True, "forgot": ["gh"], "failures_dropped": ["gh"]})
        # Nothing is held now, so the read that fails is a first read, and gives nothing.
        self.assertEqual(self.get(), {"ok": False, "error": f"the reader did not give `gh` ({GH_REF}): {SAID}"})
        self.assertEqual(self.keeper.status()["held"], {})

    def test_a_value_read_for_one_reference_is_not_kept_for_another(self):
        self.get()
        self.settings(gh_token_ref="op://v/gh-new/password")
        self.says["op://v/gh-new/password"] = (None, SAID)
        for age in (0, 12 * 3600):
            self.clock.t += age
            answer = self.get()
            self.assertEqual(answer, {"ok": False, "error": f"the reader did not give `gh` (op://v/gh-new/password): {SAID}"})
        self.assertEqual((self.keeper.status()["held"], self.keeper.counts["past_lifetime"]), ({}, 0))
        # The same when the value for the first reference had already run past its lifetime.
        self.settings()
        self.keeper.handle({"op": "forget"})
        self.assertEqual(self.get()["value"], fake(GH_REF))
        self.clock.t += 12 * 3600
        self.settings(gh_token_ref="op://v/gh-new/password")
        self.assertEqual(self.get(), {"ok": False, "error": f"the reader did not give `gh` (op://v/gh-new/password): {SAID}"})
        self.assertEqual((self.keeper.status()["held"], self.keeper.counts["past_lifetime"]), ({}, 0))

    def test_a_reference_changed_in_the_settings_is_read_and_the_old_value_is_not_given_for_it(self):
        self.get()
        self.settings(gh_token_ref="op://v/gh-new/password")
        self.assertEqual(self.get()["value"], fake("op://v/gh-new/password"))
        self.assertEqual(self.asked, [GH_REF, "op://v/gh-new/password"])

    def test_a_failure_remembered_for_one_reference_is_not_the_answer_for_another(self):
        self.says[GH_REF] = (None, SAID)
        self.get()
        self.settings(gh_token_ref="op://v/gh-new/password")
        self.assertTrue(self.get()["ok"])
        # And once it has been read, nothing is listed as failed.
        self.assertEqual(self.keeper.status()["failed"], {})

    def test_forget_drops_what_is_held_and_what_failed(self):
        self.says[CLAUDE_REF] = (None, SAID)
        self.get("gh"), self.get("claude")
        self.assertEqual(self.keeper.handle({"op": "forget"}), {"ok": True, "forgot": ["gh"], "failures_dropped": ["claude"]})
        self.get("gh"), self.get("claude")
        self.assertEqual(self.asked, [GH_REF, CLAUDE_REF, GH_REF, CLAUDE_REF])

    def test_status_names_what_is_held_with_its_age_and_the_counters_and_no_value(self):
        self.says[CLAUDE_REF] = (None, SAID)
        self.get("gh"), self.get("gh"), self.get("claude"), self.get("claude")
        self.clock.t += 75
        status = self.keeper.handle({"op": "status"})
        self.assertEqual(status["held"], {"gh": {"age_s": 75}})
        self.assertEqual(status["failed"]["claude"]["age_s"], 75)
        self.assertIn(SAID, status["failed"]["claude"]["error"])
        counted = {k: status[k] for k in ("reads", "failed_reads", "from_memory", "refused_from_memory")}
        self.assertEqual(counted, {"reads": 2, "failed_reads": 1, "from_memory": 1, "refused_from_memory": 1})
        self.assertRegex(status["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertNotIn(fake(GH_REF), json.dumps(status))

    def test_a_name_no_program_has_is_refused(self):
        answer = self.get("aws")
        self.assertFalse(answer["ok"])
        self.assertIn("`aws` is not a secret a program has", answer["error"])
        self.assertEqual(self.asked, [])

    def test_a_name_without_a_reference_is_refused(self):
        self.settings(ntfy_password_ref=None)
        answer = self.get("ntfy")
        self.assertIn("names no reference for `ntfy`", answer["error"])
        self.assertEqual(self.asked, [])

    def test_a_claude_ref_of_dash_names_no_token_and_nothing_is_read_or_counted(self):
        self.settings(worker_claude_ref="-")
        answer = self.get("claude")
        self.assertFalse(answer["ok"])
        self.assertIn("names no Claude token (`worker_claude_ref` is `-`)", answer["error"])
        self.assertEqual((self.asked, self.keeper.counts["reads"], self.keeper.counts["failed_reads"]), ([], 0, 0))
        self.assertEqual((self.keeper.status()["last_read"], self.keeper.status()["failed"]), (None, {}))
        self.assertEqual(self.get("gh")["value"], fake(GH_REF))

    def test_settings_that_cannot_be_read_are_said_and_nothing_is_read(self):
        (self.root / "program.json").unlink()
        answer = self.get()
        self.assertIn("program.json does not exist", answer["error"])
        self.assertEqual((self.asked, self.keeper.counts["failed_reads"]), ([], 0))

    def test_a_lifetime_that_is_not_a_finite_number_is_refused_and_reads_nothing(self):
        # json reads NaN and Infinity. Against NaN every age compares false, which made every request a read.
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.settings(secrets_ttl_s=bad)
            for _ in range(5):
                answer = self.get()
                self.assertFalse(answer["ok"])
                self.assertIn("secrets_ttl_s", answer["error"])
        self.assertEqual((self.asked, self.keeper.counts["reads"]), ([], 0))

    def test_a_reference_that_goes_back_and_forth_does_not_get_a_failing_store_asked_each_time(self):
        other = "op://v/gh-other/password"
        self.says[GH_REF] = self.says[other] = (None, SAID)
        for n in range(6):
            self.settings(gh_token_ref=other if n % 2 else GH_REF)
            self.assertFalse(self.get()["ok"])
        # Each reference failed once, and each failure is remembered for its own ten minutes.
        self.assertEqual(self.asked, [GH_REF, other])
        self.assertEqual((self.keeper.counts["failed_reads"], self.keeper.counts["refused_from_memory"]), (2, 4))
        self.assertEqual(sorted(self.keeper.status()["failed"]), ["gh"])
        self.clock.t += 600
        self.get()
        self.settings(gh_token_ref=GH_REF)
        self.get()
        self.assertEqual(self.asked, [GH_REF, other, other, GH_REF])

    def test_lifetimes_are_counted_on_a_clock_that_cannot_be_set_back(self):
        self.assertIs(keeper.Keeper(self.root).clock, time.monotonic)
        counted, wall = Clock(), Clock()
        counted.t = 5000.0
        kept = keeper.Keeper(self.root, reader=self.read, clock=counted, wall=wall)
        get = lambda: kept.handle({"op": "get", "name": "gh"})  # noqa: E731
        self.says[GH_REF] = (None, SAID)
        get()
        # The wall clock is set back an hour. Ten minutes later the failed read is still tried again.
        wall.t -= 3600
        counted.t += 600
        self.says[GH_REF] = (fake(GH_REF), None)
        self.assertTrue(get()["ok"])
        self.assertEqual(len(self.asked), 2)
        # And a value still ends its lifetime twelve hours after it was read.
        wall.t -= 3600
        counted.t += 12 * 3600
        self.says[GH_REF] = (None, "the server is not reachable")
        self.assertTrue(get()["ok"])
        self.assertEqual(len(self.asked), 3)
        # What `status` prints is a time of day: the wall clock's now, less how long ago the lifetime ended.
        counted.t += 100
        since = datetime.fromtimestamp(wall.t - 100, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(kept.status()["held"]["gh"]["past_lifetime_since"], since)

    def test_a_reader_that_breaks_is_a_failed_read_and_is_not_asked_again(self):
        def broken(ref, argv):
            self.asked.append(ref)
            raise RuntimeError("the reader's own fault, with whatever it had in hand")

        self.keeper.reader = broken
        first = self.get()
        self.assertEqual(first, {"ok": False, "error": f"the reader did not give `gh` ({GH_REF}): the reader broke (RuntimeError)"})
        self.assertFalse(self.get()["ok"])
        self.assertEqual((self.asked, self.keeper.counts["failed_reads"], self.keeper.counts["refused_from_memory"]), ([GH_REF], 1, 1))

    def test_settings_that_are_not_an_object_or_a_reference_that_is_not_a_word_read_nothing(self):
        (self.root / "program.json").write_text("[1, 2]\n")
        self.assertIn("the program's settings could not be read", self.get()["error"])
        self.settings(gh_token_ref=5)
        self.assertIn("names no reference for `gh`", self.get()["error"])
        self.assertEqual((self.asked, self.keeper.counts["reads"]), ([], 0))

    def test_a_reader_that_is_not_a_command_is_refused(self):
        self.settings(secrets_reader="hl-read-secret")
        answer = self.get()
        self.assertIn("secrets_reader", answer["error"])
        self.assertEqual(self.asked, [])

    def test_the_programs_reader_is_the_one_asked(self):
        self.keeper.reader = lambda ref, argv: (" ".join(argv), None)
        self.assertEqual(self.get()["value"], f"{sys.executable} {self.reader}")

    def test_a_request_it_does_not_know_is_refused(self):
        for request in ({"op": "dump"}, {}, ["get", "gh"], "get"):
            self.assertFalse(self.keeper.handle(request)["ok"])

    def test_only_the_same_user_is_answered(self):
        self.get()
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b'{"op": "get", "name": "gh"}\n')
        with mock.patch("hl.secrets.peer_uid", return_value=os.getuid() + 1):
            keeper._answer(ours, self.keeper)
        answer = json.loads(theirs.recv(65536))
        self.assertEqual(answer, {"ok": False, "error": "this keeper answers only the user it runs as", "root": str(self.root)})
        self.assertIn(f"refused a connection from uid {os.getuid() + 1}", (self.root / "secrets.log").read_text())

    def test_a_caller_whose_user_cannot_be_told_is_not_answered(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b'{"op": "get", "name": "gh"}\n')
        with mock.patch("hl.secrets.peer_uid", return_value=None):
            keeper._answer(ours, self.keeper)
        self.assertFalse(json.loads(theirs.recv(65536))["ok"])
        self.assertEqual(self.asked, [])

    def test_a_request_that_breaks_the_keeper_is_answered_and_the_keeper_goes_on(self):
        self.get()
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b'{"op": "get", "name": "gh"}\n')
        with mock.patch("hl.secrets.peer_uid", return_value=os.getuid()), mock.patch.object(self.keeper, "handle", side_effect=RuntimeError("a fault")):
            keeper._answer(ours, self.keeper)
        self.assertEqual(json.loads(theirs.recv(65536)), {"ok": False, "error": "the keeper could not handle that request (RuntimeError)", "root": str(self.root)})
        self.assertIn("a request broke the keeper's handling (RuntimeError); it goes on", (self.root / "secrets.log").read_text())
        # What it held, it still holds.
        self.assertEqual((self.get()["value"], self.asked), (fake(GH_REF), [GH_REF]))

    def test_someone_who_only_looked_whether_it_listens_is_not_answered_and_not_logged(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        theirs.close()
        with mock.patch("hl.secrets.peer_uid", return_value=None):
            self.assertIsNone(keeper._answer(ours, self.keeper))
        self.assertFalse((self.root / "secrets.log").exists())

    def test_whoever_connected_before_a_keeper_left_is_still_answered(self):
        path = self.root / "secrets.sock"
        listening = keeper._bind(path)
        self.addCleanup(listening.close)
        callers = []
        for name in ("gh", "claude"):
            caller = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(caller.close)
            caller.settimeout(10)
            caller.connect(str(path))
            caller.sendall(json.dumps({"op": "get", "name": name}).encode() + b"\n")
            callers.append(caller)
        # The keeper is leaving: its path is gone, and nobody new can connect.
        os.unlink(str(path))
        keeper._drain(listening, self.keeper)
        # Every answer says whose keeper gave it.
        mine = {"ok": True, "root": str(self.root)}
        self.assertEqual([json.loads(c.recv(65536)) for c in callers], [{**mine, "value": fake(GH_REF)}, {**mine, "value": fake(CLAUDE_REF)}])

    def test_a_request_that_is_not_json_is_refused_and_the_keeper_goes_on(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b"get gh\n")
        with mock.patch("hl.secrets.peer_uid", return_value=os.getuid()):
            keeper._answer(ours, self.keeper)
        self.assertIn("not one JSON object", json.loads(theirs.recv(65536))["error"])


class ReaderTest(SecretsCase):
    def read(self, ref):
        return keeper.read_secret(ref, [sys.executable, str(self.reader)])

    def test_a_read_gives_the_value_without_its_newline(self):
        self.assertEqual(self.read(GH_REF), (fake(GH_REF), None))

    def test_a_read_that_worked_says_nothing_whatever_the_reader_printed_beside_it(self):
        script = self.bin / "chatty"
        script.write_text("import sys\nprint('a warning of some kind', file=sys.stderr)\nprint('the-value')\n")
        self.assertEqual(keeper.read_secret(GH_REF, [sys.executable, str(script)]), ("the-value", None))

    def test_only_the_readers_first_line_on_stderr_is_the_reason(self):
        script = self.bin / "wordy"
        script.write_text("#!/bin/sh\necho >&2\necho '  hl-read-secret: op read failed (exit 1):   [ERROR] no such item' >&2\necho 'a second line of detail' >&2\nexit 1\n")
        script.chmod(0o755)
        self.assertEqual(keeper.read_secret(GH_REF, [str(script)]), (None, "hl-read-secret: op read failed (exit 1): [ERROR] no such item"))

    def test_a_failed_read_gives_what_was_said(self):
        self.assertEqual(self.read("op://v/missing/password"), (None, SAID))

    def test_a_read_that_prints_nothing_says_so(self):
        self.assertEqual(self.read("op://v/empty/password"), (None, "the read printed nothing (exit 0)"))

    def test_what_a_failing_reader_printed_is_withheld_from_the_error(self):
        script = self.bin / "leaky"
        script.write_text("#!/bin/sh\necho half-a-token\necho 'could not finish after half-a-token' >&2\nexit 3\n")
        script.chmod(0o755)
        self.assertEqual(keeper.read_secret(GH_REF, [str(script)]), (None, "could not finish after [withheld]"))

    def test_output_that_is_not_text_is_a_failed_read_and_not_a_fault(self):
        script = self.bin / "binary"
        script.write_text("#!/bin/sh\nprintf '\\377\\376tok'\nexit 0\n")
        script.chmod(0o755)
        self.assertEqual(keeper.read_secret(GH_REF, [str(script)]), (None, "the read printed something that is not text (exit 0)"))
        script.write_text("#!/bin/sh\nprintf 'no \\377 such item\\n' >&2\nexit 1\n")
        self.assertEqual(keeper.read_secret(GH_REF, [str(script)]), (None, "no \ufffd such item"))

    def test_a_reader_that_cannot_be_run_is_a_failed_read(self):
        value, said = keeper.read_secret(GH_REF, [str(self.bin / "not-there")])
        self.assertIsNone(value)
        self.assertIn("could not be run", said)

    def test_a_reader_that_does_not_answer_is_a_failed_read(self):
        self.set("delay", "5")
        with mock.patch.object(keeper, "READ_TIMEOUT_S", 0.3):
            self.assertEqual(self.read(GH_REF), (None, "the read did not answer within 0.3 seconds"))

    def test_a_reader_that_is_given_up_on_is_ended_with_whatever_it_started(self):
        script = self.bin / "spawning"
        pidfile = self.bin / "grandchild.pid"
        script.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nwait\n")
        script.chmod(0o755)

        def end_it():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

        self.addCleanup(end_it)
        with mock.patch.object(keeper, "READ_TIMEOUT_S", 0.5):
            self.assertEqual(keeper.read_secret(GH_REF, [str(script)]), (None, "the read did not answer within 0.5 seconds"))
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        alive = True
        while alive and time.monotonic() < deadline:
            # A killed child of this process stays a zombie until it is waited for; `ps` says which it is.
            state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            alive = bool(state) and not state.startswith("Z")
            time.sleep(0.05)
        self.assertFalse(alive, "what the reader started was left running")

    def test_without_a_reader_of_its_own_a_program_is_read_with_the_kits_hl_read_secret(self):
        with mock.patch("hl.secrets.subprocess.Popen") as started:
            started.return_value.communicate.return_value = (b"v", b"")
            started.return_value.returncode = 0
            self.assertEqual(keeper.read_secret(GH_REF), ("v", None))
        self.assertEqual(started.call_args[0][0], [str(KIT / "scripts" / "hl-read-secret"), GH_REF])
        self.assertEqual(keeper.READ_SECRET, KIT / "scripts" / "hl-read-secret")

    def test_the_kits_reader_reads_a_file_reference_end_to_end(self):
        token = self.tmp / "gh-token"
        token.write_text(fake(GH_REF) + "\n")
        token.chmod(0o600)
        self.assertEqual(keeper.read_secret(f"file:{token}"), (fake(GH_REF), None))
        token.chmod(0o644)
        value, said = keeper.read_secret(f"file:{token}")
        self.assertIsNone(value)
        self.assertRegex(said, r"^hl-read-secret: .*/gh-token: group or others may use it \(mode -rw-r--r--")
        token.unlink()


# ---------------------------------------------------------------- the keeper as a process


class KeeperTest(SecretsCase):
    def test_many_gets_cost_one_read(self):
        got = [self.cli("get", "gh") for _ in range(6)]
        self.assertEqual({(p.returncode, p.stdout, p.stderr) for p in got}, {(0, fake(GH_REF) + "\n", "")})
        self.assertEqual(self.reads(), [GH_REF])
        status = self.status()
        self.assertEqual((status["reads"], status["from_memory"], status["failed_reads"]), (1, 5, 0))
        self.assertEqual(self.started(), 1)

    def test_two_secrets_cost_two_reads(self):
        for _ in range(3):
            self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF))
            self.assertEqual(self.cli("get", "claude").stdout.strip(), fake(CLAUDE_REF))
        self.assertEqual(self.reads(), [GH_REF, CLAUDE_REF])

    def test_the_lifetime_in_the_settings_is_the_one_a_running_keeper_holds_to(self):
        self.settings(secrets_ttl_s=1.5)
        keeper.get(self.root, "gh")
        keeper.get(self.root, "gh")
        self.assertEqual(len(self.reads()), 1)
        time.sleep(1.6)
        self.assertEqual(keeper.get(self.root, "gh"), fake(GH_REF))
        self.assertEqual(len(self.reads()), 2)

    def test_a_program_goes_on_with_the_secret_it_holds_while_the_store_refuses(self):
        self.settings(secrets_ttl_s=1.5)
        self.assertEqual(keeper.get(self.root, "gh"), fake(GH_REF))
        self.set("mode", "fail")
        time.sleep(1.6)
        for _ in range(3):
            got = self.cli("get", "gh")
            self.assertEqual((got.returncode, got.stdout, got.stderr), (0, fake(GH_REF) + "\n", ""))
        shown = self.cli("status")
        status = json.loads(shown.stdout)
        self.assertRegex(status["held"]["gh"]["past_lifetime_since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(status["held"]["gh"]["last_error"], f"the reader did not give `gh` ({GH_REF}): {SAID}")
        self.assertEqual((status["reads"], status["failed_reads"], status["past_lifetime"]), (2, 1, 3))
        self.assertNotIn(fake(GH_REF), shown.stdout)
        self.assertIn("stays in use", (self.root / "secrets.log").read_text())
        # A secret it never held is still refused: a first read that fails gives nothing.
        first = self.cli("get", "claude")
        self.assertEqual((first.returncode, first.stdout), (2, ""))

    def test_a_failed_read_is_said_once_by_the_reader_and_then_from_memory(self):
        self.set("mode", "fail")
        got = [self.cli("get", "gh") for _ in range(5)]
        for p in got:
            self.assertEqual((p.returncode, p.stdout), (2, ""))
            self.assertIn(f"the reader did not give `gh` ({GH_REF}): {SAID}", p.stderr)
        self.assertEqual(self.reads(), [GH_REF])
        status = self.status()
        self.assertEqual((status["reads"], status["failed_reads"], status["refused_from_memory"]), (1, 1, 4))
        self.assertIn(SAID, status["failed"]["gh"]["error"])
        # The account answers again; the keeper still does not ask it, and says why.
        self.set("mode", "")
        again = self.cli("get", "gh")
        self.assertEqual(again.returncode, 2)
        self.assertIn("a failed read is not tried again for 10 minutes", again.stderr)
        self.assertEqual(self.reads(), [GH_REF])

    def test_callers_that_all_find_no_keeper_end_with_one_and_one_read(self):
        self.set("delay", "0.4")
        argv = [sys.executable, str(HL_SECRETS), "--root", str(self.root), "get", "gh"]
        procs = [subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        got = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
        self.assertEqual(set(got), {(fake(GH_REF) + "\n", "", 0)})
        self.assertEqual(self.reads(), [GH_REF])
        self.assertEqual(self.started(), 1)
        status = self.status()
        self.assertEqual((status["reads"], status["from_memory"]), (1, 7))

    def test_a_dead_keepers_socket_is_replaced(self):
        self.cli("get", "gh")
        first = self.status()["pid"]
        self.kill(first)
        self.assertTrue(stat.S_ISSOCK(os.lstat(str(self.root / "secrets.sock")).st_mode), "the dead keeper's socket is still there")
        self.assertEqual(self.status()["running"], False)
        self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF))
        second = self.status()["pid"]
        self.assertNotEqual(first, second)
        # What the dead one held died with it.
        self.assertEqual((self.reads(), self.started()), ([GH_REF, GH_REF], 2))

    def test_the_socket_is_the_users_alone(self):
        self.cli("get", "gh")
        info = os.lstat(str(self.root / "secrets.sock"))
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_uid), (0o600, os.getuid()))
        for name in ("secrets.lock", "secrets.log"):
            self.assertEqual(stat.S_IMODE(os.lstat(str(self.root / name)).st_mode), 0o600, name)

    def test_a_socket_that_is_another_users_is_not_asked(self):
        self.cli("get", "gh")
        with mock.patch("hl.secrets.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(Refusal) as caught:
                keeper.get(self.root, "gh")
        self.assertIn("belongs to another user", str(caught.exception))

    def test_status_shows_no_value(self):
        self.cli("get", "gh"), self.cli("get", "claude"), self.cli("get", "gh")
        shown = self.cli("status")
        status = json.loads(shown.stdout)
        self.assertEqual(sorted(status["held"]), ["claude", "gh"])
        for name in ("gh", "claude"):
            self.assertLess(status["held"][name]["age_s"], 30)
        self.assertEqual((status["running"], status["reads"], status["from_memory"]), (True, 2, 1))
        for value in (fake(GH_REF), fake(CLAUDE_REF)):
            self.assertNotIn(value, shown.stdout + shown.stderr)

    def test_forget_makes_the_next_get_a_read_and_a_rotated_secret_the_one_given(self):
        self.cli("get", "gh"), self.cli("get", "claude")
        self.set("salt", "rotated")
        self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF), "still the one that was read")
        forgot = json.loads(self.cli("forget").stdout)
        self.assertEqual(forgot, {"forgot": ["claude", "gh"], "failures_dropped": []})
        self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF, "rotated"))
        self.assertEqual(self.reads(), [GH_REF, CLAUDE_REF, GH_REF])

    def test_forget_stop_and_status_start_nothing(self):
        self.assertEqual(json.loads(self.cli("forget").stdout), {"running": False, "forgot": [], "failures_dropped": []})
        self.assertEqual(json.loads(self.cli("stop").stdout), {"running": False, "stopped": False})
        self.assertEqual(self.status()["running"], False)
        self.assertEqual((self.started(), (self.root / "secrets.sock").exists()), (0, False))

    def test_stop_ends_the_keeper_and_takes_its_socket(self):
        self.cli("get", "gh")
        pid = self.status()["pid"]
        self.assertEqual(json.loads(self.cli("stop").stdout), {"stopped": True, "pid": pid})
        self.assertFalse((self.root / "secrets.sock").exists())
        self.assertIn("left: asked to stop", (self.root / "secrets.log").read_text())

    def test_a_keeper_that_was_just_stopped_is_replaced_at_once(self):
        for n in range(1, 5):
            self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF))
            self.assertTrue(json.loads(self.cli("stop").stdout)["stopped"])
            self.assertEqual(self.cli("get", "gh").returncode, 0)
            self.assertEqual(len(self.reads()), 2 * n)
            self.cli("stop")

    def test_it_leaves_by_itself_when_nobody_asks(self):
        proc = subprocess.Popen(
            [sys.executable, str(HL_SECRETS), "--root", str(self.root), "serve", "--foreground", "--idle-s", "0.6"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.kill)
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertFalse((self.root / "secrets.sock").exists())
        log = (self.root / "secrets.log").read_text()
        self.assertIn("leaves after 0.6 s without a request", log)
        self.assertIn("left: no request for 0.6 s", log)

    def test_a_request_keeps_it_from_leaving(self):
        proc = subprocess.Popen(
            [sys.executable, str(HL_SECRETS), "--root", str(self.root), "serve", "--foreground", "--idle-s", "2"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not (self.root / "secrets.sock").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = keeper.status(self.root)["pid"]
        for _ in range(5):
            time.sleep(0.7)
            self.assertEqual(keeper.status(self.root).get("pid"), pid, "it left with a request every 0.7 s and 2 s to idle")
        self.assertEqual(proc.wait(timeout=20), 0)
        self.assertEqual(self.started(), 1)

    def test_it_is_started_with_none_of_the_callers_tokens(self):
        env = {**os.environ, "GH_TOKEN": "keyring-admin", "GITHUB_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "signed-in", "ANTHROPIC_API_KEY": "key"}
        self.assertEqual(self.cli("get", "gh", env=env).returncode, 0)
        self.assertEqual((self.bin / "reads.log").read_text(), GH_REF + " \n")

    def test_a_root_too_long_for_a_socket_is_refused_and_nothing_is_started(self):
        long_root = self.tmp / ("r" * 90)
        long_root.mkdir()
        self.settings(root=long_root)
        with self.assertRaises(keeper.Unreachable):
            # Nothing was asked of anything, which is what lets the watchdog read for itself in such a root.
            keeper.get(long_root, "gh")
        got = self.cli("get", "gh", root=long_root)
        self.assertEqual((got.returncode, got.stdout), (2, ""))
        self.assertIn("too long for the keeper's socket", got.stderr)
        self.assertIn(f"at most {keeper.SOCK_PATH_MAX}", got.stderr)
        self.assertEqual(sorted(p.name for p in long_root.iterdir()), ["program.json"])
        served = self.cli("serve", "--foreground", root=long_root)
        self.assertEqual(served.returncode, 2)
        self.assertEqual(self.reads(), [])

    def test_the_longest_root_that_fits_is_served(self):
        fits = self.tmp / ("r" * (keeper.SOCK_PATH_MAX - len(str(self.tmp)) - len("//secrets.sock")))
        fits.mkdir()
        self.roots.append(fits)
        self.settings(root=fits)
        self.assertEqual(len(str(fits / "secrets.sock")), keeper.SOCK_PATH_MAX)
        self.assertEqual(self.cli("get", "gh", root=fits).stdout.strip(), fake(GH_REF))

    def test_something_at_the_sockets_path_that_is_not_a_socket_is_left_alone(self):
        (self.root / "secrets.sock").write_text("not a socket\n")
        got = self.cli("get", "gh")
        self.assertEqual((got.returncode, got.stdout), (2, ""))
        self.assertIn("could not start", got.stderr)
        self.assertEqual((self.root / "secrets.sock").read_text(), "not a socket\n")
        self.assertIn("exists and is not a socket", (self.root / "secrets.log").read_text())
        self.assertEqual(self.reads(), [])
        (self.root / "secrets.sock").unlink()

    def test_a_keeper_whose_socket_was_removed_is_named_and_not_doubled(self):
        self.cli("get", "gh")
        pid = self.status()["pid"]
        (self.root / "secrets.sock").unlink()
        with mock.patch.object(keeper, "MAX_STARTS", 1):
            with self.assertRaises(keeper.Unreachable) as caught:
                keeper.get(self.root, "gh")
        self.assertIn("could not start", str(caught.exception))
        self.assertIn(f"another keeper (pid {pid}) holds", (self.root / "secrets.log").read_text())
        self.assertEqual(self.status(), {"running": False, "detail": f"no keeper is running for {self.root}", "last_pid": pid})
        self.assertEqual(self.started(), 1)
        self.kill(pid)

    def test_a_caller_starts_a_keeper_a_few_times_and_then_says_none_can_be_had(self):
        with mock.patch.object(keeper, "_exchange", side_effect=keeper._NotThere()), mock.patch.object(keeper, "_start") as start:
            with self.assertRaises(keeper.Unreachable) as caught:
                keeper.get(self.root, "gh")
        self.assertEqual(start.call_count, keeper.MAX_STARTS)
        self.assertIn("none answered", str(caught.exception))

    def stand_in(self, *answers):
        """Something that listens where the keeper would and gives each
        caller the next of `answers`: bytes to send, or None to hang up."""
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.bind(str(self.root / "secrets.sock"))
        other.listen(8)
        self.addCleanup(lambda: (self.root / "secrets.sock").unlink())

        def answer():
            for said in answers:
                try:
                    conn, _ = other.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(4096)
                    if said is not None:
                        conn.sendall(said)

        threading.Thread(target=answer, daemon=True).start()

    def test_a_keeper_that_does_not_answer_is_not_waited_for_without_end_and_not_replaced(self):
        self.stand_in()
        with mock.patch.object(keeper, "ASK_TIMEOUT_S", 0.3), mock.patch.object(keeper, "_start") as start:
            with self.assertRaises(Refusal) as caught:
                keeper.get(self.root, "gh")
            self.assertEqual(keeper.status(self.root), {"running": False, "detail": f"the keeper at {self.root / 'secrets.sock'} did not answer within 0.3 seconds", "last_pid": None})
        self.assertIn("did not answer within 0.3 seconds", str(caught.exception))
        # It may be asking the store this moment: that is not "no keeper", and nobody reads in its place.
        self.assertNotIsInstance(caught.exception, keeper.Unreachable)
        start.assert_not_called()

    def test_a_request_that_was_sent_and_not_answered_is_not_sent_again(self):
        self.stand_in(None)
        with mock.patch.object(keeper, "_start") as start:
            with self.assertRaises(Refusal) as caught:
                keeper.get(self.root, "gh")
        self.assertIn("went without answering a request that had been sent. It may have read it and asked the store", str(caught.exception))
        self.assertNotIsInstance(caught.exception, keeper.Unreachable)
        start.assert_not_called()

    def test_what_answers_with_something_else_is_not_taken_for_a_keeper(self):
        self.stand_in(b"hello\n", b"[1, 2]\n")
        for _ in range(2):
            with self.assertRaises(Refusal) as caught:
                keeper.get(self.root, "gh")
            self.assertIn("is not a keeper", str(caught.exception))
            self.assertNotIsInstance(caught.exception, keeper.Unreachable)

    def send(self, request: dict) -> socket.socket:
        caller = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(caller.close)
        caller.settimeout(30)
        caller.connect(str(self.root / "secrets.sock"))
        caller.sendall(json.dumps(request).encode() + b"\n")
        return caller

    def test_a_keeper_told_to_stop_first_answers_whoever_was_already_waiting(self):
        keeper.ask(self.root, {"op": "status"}, start=True)
        self.set("delay", "0.6")
        # While it reads for the first caller, a stop arrives, and behind the stop another request.
        first = self.send({"op": "get", "name": "gh"})
        time.sleep(0.15)
        stop = self.send({"op": "stop"})
        behind = self.send({"op": "get", "name": "claude"})
        self.assertEqual(json.loads(first.recv(65536)), {"ok": True, "value": fake(GH_REF), "root": str(self.root)})
        self.assertTrue(json.loads(stop.recv(65536))["stopped"])
        self.assertEqual(json.loads(behind.recv(65536)), {"ok": True, "value": fake(CLAUDE_REF), "root": str(self.root)})
        self.assertEqual(self.reads(), [GH_REF, CLAUDE_REF])

    def test_one_root_named_two_ways_is_one_socket_and_one_keeper(self):
        link = self.tmp / "link"
        link.symlink_to(self.root)
        self.assertEqual(keeper.sock_path(link), keeper.sock_path(self.root))
        self.assertEqual(self.cli("get", "gh", root=link).stdout.strip(), fake(GH_REF))
        self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF))
        self.assertEqual((self.reads(), self.started()), ([GH_REF], 1))

    def test_a_link_where_the_socket_should_be_is_not_followed_to_another_roots_keeper(self):
        other = self.tmp / "other"
        other.mkdir()
        self.roots.append(other)
        self.settings(root=other, gh_token_ref="op://v/gh-other/password")
        self.assertEqual(self.cli("get", "gh", root=other).stdout.strip(), fake("op://v/gh-other/password"))
        (self.root / "secrets.sock").symlink_to(other / "secrets.sock")
        self.addCleanup(lambda: (self.root / "secrets.sock").unlink())
        got = self.cli("get", "gh")
        self.assertEqual((got.returncode, got.stdout), (2, ""))
        self.assertIn("could not start", got.stderr)
        self.assertIn("exists and is not a socket", (self.root / "secrets.log").read_text())
        self.assertEqual(self.reads(), ["op://v/gh-other/password"])

    def test_an_answer_from_another_roots_keeper_is_not_taken(self):
        answers = ({"ok": True, "value": "theirs", "root": str(self.tmp / "other")}, {"ok": True, "value": "theirs"})
        self.stand_in(*(json.dumps(said).encode() + b"\n" for said in answers))
        for _ in answers:
            with self.assertRaises(Refusal) as caught:
                keeper.get(self.root, "gh")
            self.assertIn("is not this root's keeper", str(caught.exception))
            self.assertNotIsInstance(caught.exception, keeper.Unreachable)

    def test_a_keeper_waits_for_one_that_is_leaving_to_let_go_of_the_lock(self):
        """A keeper that is leaving has removed its socket and still holds
        the lock for a moment. This process plays it."""
        leaving = os.open(str(self.root / "secrets.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(leaving, fcntl.LOCK_EX)
        timer = threading.Timer(0.6, os.close, [leaving])
        timer.start()
        self.addCleanup(timer.join)
        self.assertEqual(self.cli("get", "gh").stdout.strip(), fake(GH_REF))
        self.assertEqual(self.started(), 1)

    def test_a_log_that_cannot_be_written_means_no_keeper_and_says_so(self):
        (self.root / "secrets.log").mkdir()
        with self.assertRaises(keeper.Unreachable) as caught:
            keeper.get(self.root, "gh")
        self.assertIn("a keeper could not be started", str(caught.exception))
        (self.root / "secrets.log").rmdir()

    def test_a_directory_that_is_no_loop_root_gets_no_keeper_and_nothing_is_made_in_it(self):
        for place in (self.tmp / "nowhere", self.bin):
            before = sorted(p.name for p in place.iterdir()) if place.exists() else None
            with self.assertRaises(keeper.Unreachable) as caught:
                keeper.get(place, "gh")
            self.assertIn("holds no program.json: it is not a loop root, and no keeper was started", str(caught.exception))
            served = self.cli("serve", "--foreground", root=place)
            self.assertEqual((served.returncode, served.stdout), (2, ""))
            self.assertIn("it is not a loop root, and no keeper serves it", served.stderr)
            self.assertEqual(sorted(p.name for p in place.iterdir()) if place.exists() else None, before)


# ---------------------------------------------------------------- the command line


class CliTest(SecretsCase):
    def test_get_prints_the_value_and_nothing_else(self):
        got = self.cli("get", "claude")
        self.assertEqual((got.returncode, got.stdout, got.stderr), (0, fake(CLAUDE_REF) + "\n", ""))

    def test_get_tagged_says_which_it_got_in_one_capture(self):
        got = self.cli("get", "gh", "--tagged")
        self.assertEqual((got.returncode, got.stdout, got.stderr), (0, f"ok {fake(GH_REF)}\n", ""))
        self.settings(worker_claude_ref="op://v/missing/password")
        got = self.cli("get", "claude", "--tagged")
        self.assertEqual((got.returncode, got.stderr), (2, ""))
        self.assertEqual(got.stdout, f"no the reader did not give `claude` (op://v/missing/password): {SAID}\n")

    def test_get_will_not_print_to_a_terminal(self):
        out = io.StringIO()
        out.isatty = lambda: True
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = keeper.main(["--root", str(self.root), "get", "gh"])
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("would print a secret on a terminal", err.getvalue())
        self.assertEqual(self.reads(), [])

    def test_a_name_that_is_not_one_of_the_three_is_refused(self):
        got = self.cli("get", "aws")
        self.assertEqual((got.returncode, got.stdout), (2, ""))
        self.assertEqual(self.started(), 0)

    def test_run_gives_the_command_the_token_and_the_caller_nothing(self):
        show = "import os, sys; print(os.environ.get('GH_TOKEN')); print(len(sys.argv))"
        got = self.cli("run", "--", sys.executable, "-c", show)
        self.assertEqual((got.returncode, got.stdout, got.stderr), (0, f"{fake(GH_REF)}\n1\n", ""))
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertEqual(self.reads(), [GH_REF])

    def test_run_replaces_a_token_the_caller_had(self):
        got = self.cli("run", "--", sys.executable, "-c", "import os; print(os.environ['GH_TOKEN'])", env={**os.environ, "GH_TOKEN": "keyring-admin"})
        self.assertEqual(got.stdout, fake(GH_REF) + "\n")

    def test_run_exits_as_the_command_exits_and_passes_its_arguments_as_they_are(self):
        got = self.cli("run", "--", "/bin/sh", "-c", 'printf "%s|" "$@"; exit 7', "sh", "pr", "list", "--json", "a b")
        self.assertEqual((got.returncode, got.stdout), (7, "pr|list|--json|a b|"))

    def test_run_without_a_command_or_with_one_that_is_not_there(self):
        got = self.cli("run", "--")
        self.assertEqual(got.returncode, 2)
        self.assertIn("run needs a command", got.stderr)
        got = self.cli("run", "--", "hl-no-such-command")
        self.assertEqual((got.returncode, got.stdout), (127, ""))
        self.assertIn("hl-no-such-command", got.stderr)

    def test_run_starts_nothing_when_the_token_cannot_be_had(self):
        self.set("mode", "fail")
        marker = self.tmp / "ran"
        got = self.cli("run", "--", "/usr/bin/touch", str(marker))
        self.assertEqual((got.returncode, got.stdout), (2, ""))
        self.assertIn(SAID, got.stderr)
        self.assertFalse(marker.exists())


class NtfyCase(SecretsCase):
    """A stand-in for the ntfy server, which keeps what it was sent."""

    def serve(self, status=200):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                seen.append({**{k: self.headers.get(k) for k in ("Authorization", "Title", "Priority")}, "body": body, "path": self.path})
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.settings(ntfy_url=f"http://127.0.0.1:{server.server_port}/loop-topic")
        return seen

    def basic(self, value=None):
        return "Basic " + base64.b64encode(f"loop-notices:{value or fake(NTFY_REF)}".encode()).decode()


class NotifyTest(NtfyCase):
    def test_notify_posts_to_the_programs_topic_with_the_kept_password(self):
        seen = self.serve()
        for n in (1, 2, 3):
            got = self.cli("notify", "--title", f"canary: file {n} complete", "--body", "Nine items merged and verified.")
            self.assertEqual((got.returncode, json.loads(got.stdout)), (0, {"notified": True, "title": f"canary: file {n} complete"}))
            self.assertNotIn(fake(NTFY_REF), got.stdout + got.stderr)
        self.assertEqual(
            seen[0],
            {"Authorization": self.basic(), "Title": "canary: file 1 complete", "Priority": "default", "body": "Nine items merged and verified.", "path": "/loop-topic"},
        )
        self.assertEqual((len(seen), self.reads()), (3, [NTFY_REF]))

    def test_notify_takes_a_priority_and_a_title_that_is_not_plain_ascii(self):
        seen = self.serve()
        title = "canary: deploy red \u2014 HD-07"
        got = self.cli("notify", "--title", title, "--body", "Read the run.", "--priority", "high")
        self.assertEqual(got.returncode, 0)
        self.assertEqual(seen[0]["Priority"], "high")
        self.assertEqual(seen[0]["Title"], "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?=")

    def test_a_server_that_refuses_is_said_without_the_password(self):
        self.serve(status=403)
        got = self.cli("notify", "--title", "t", "--body", "b")
        self.assertEqual(got.returncode, 2)
        self.assertIn("the operator was not told", json.loads(got.stdout)["refused"][0])
        self.assertIn("403", got.stdout)
        self.assertNotIn(fake(NTFY_REF), got.stdout + got.stderr)
        self.assertNotIn(self.basic(), got.stdout + got.stderr)

    def test_an_address_that_is_not_one_is_a_refusal_and_not_a_fault(self):
        self.settings(ntfy_url="ntfy.example/loop-topic")
        got = self.cli("notify", "--title", "t", "--body", "b")
        self.assertEqual(got.returncode, 2)
        self.assertIn("ntfy.example/loop-topic did not take it", json.loads(got.stdout)["refused"][0])
        self.assertNotIn("Traceback", got.stderr)

    def test_the_password_does_not_follow_a_redirect_to_somewhere_else(self):
        """A 302 to a post is followed as a get, with the request's headers: the password among them."""
        arrived = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                arrived.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args):
                pass

        class Redirecting(Elsewhere):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/taken")
                self.end_headers()

        elsewhere = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)
        first = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
        for server in (elsewhere, first):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
            self.addCleanup(thread.join)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.settings(ntfy_url=f"http://127.0.0.1:{first.server_port}/loop-topic")
        got = self.cli("notify", "--title", "t", "--body", "b")
        self.assertEqual(got.returncode, 2)
        self.assertIn("did not take it", json.loads(got.stdout)["refused"][0])
        self.assertIn("302", got.stdout)
        self.assertEqual(arrived, [], "the request, and the password with it, went where a redirect pointed")

    def test_a_program_without_a_channel_or_a_password_is_refused(self):
        got = self.cli("notify", "--title", "t", "--body", "b")
        self.assertIn("no channel to the operator", json.loads(got.stdout)["refused"][0])
        self.serve()
        self.set("mode", "fail")
        got = self.cli("notify", "--title", "t", "--body", "b")
        self.assertEqual(got.returncode, 2)
        self.assertIn(SAID, json.loads(got.stdout)["refused"][0])


class BudgetTest(SecretsCase):
    def setUp(self):
        super().setUp()
        self.op = self.bin / "op"
        self.op.write_text(OP)
        self.op.chmod(0o755)
        self.answer(TABLE)
        (self.bin / "expect").write_text(os.environ["OP_SERVICE_ACCOUNT_TOKEN"])
        patcher = mock.patch.object(keeper, "OP_PATH", f"{self.bin}:/usr/bin:/bin")
        patcher.start()
        self.addCleanup(patcher.stop)

    def answer(self, stdout="", stderr="", rc=0):
        for name, text in (("stdout", stdout), ("stderr", stderr), ("rc", str(rc))):
            (self.bin / name).write_text(text)

    def budget(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = keeper.main(["--root", str(self.root), "budget"])
        return code, out.getvalue(), err.getvalue()

    def seen(self) -> dict:
        return dict(line.split("=", 1) for line in (self.bin / "seen").read_text().splitlines())

    def held(self, **last_read):
        """`budget` with a keeper whose last read went this way, and what it then says to hold."""
        failed = 0 if last_read.get("ok", True) else 1
        state = {"running": True, "failed_reads": failed, "last_read": {"name": "gh", "at": "2026-10-13T00:00:00Z", **last_read} if last_read else None}
        with mock.patch.object(keeper, "status", return_value=state):
            code, out, _ = self.budget()
        answer = json.loads(out)
        self.assertEqual(
            (code, answer["applies"], answer["account_is"]), (0, True, "the service account's daily request allowance, shared by everything that reads through it")
        )
        self.assertEqual(answer["reads"], {"last_read": state["last_read"], "failed_reads": failed})
        return answer["account_remaining"], answer["hold"], answer["why"]

    def test_reads_that_work_hold_the_program_only_once_the_counter_is_low(self):
        self.assertEqual(self.held(ok=True), (154, False, "the keeper's last read (gh) worked"))
        self.answer(TABLE.replace("154", "49"))
        self.assertEqual(self.held(ok=True), (49, True, "the service account has 49 requests left for the day (under 50 is low), and reads through it are about to stop"))
        self.answer(TABLE.replace("154", "50"))
        self.assertEqual(self.held(ok=True)[:2], (50, False))
        self.answer(TABLE.replace("154", "0"))
        self.assertEqual(self.held(ok=True)[:2], (0, True))

    def test_a_read_that_failed_holds_the_program_whatever_the_counter_says(self):
        self.assertEqual(self.held(ok=False), (154, True, "the keeper's last read (gh) failed"))

    def test_before_any_read_nothing_is_held_unless_the_counter_is_low(self):
        self.assertEqual(self.held(), (154, False, "the keeper has read nothing yet: hold only when a read fails"))
        # With no keeper running the answer is the same: nothing says a read failed.
        code, out, _ = self.budget()
        self.assertEqual((code, json.loads(out)["hold"], json.loads(out)["reads"]["last_read"]), (0, False, None))
        self.answer(TABLE.replace("154", "3"))
        self.assertEqual(self.held()[:2], (3, True))

    def test_a_program_that_reads_nothing_from_1password_is_told_plainly_that_it_does_not_apply(self):
        self.settings(gh_token_ref="file:~/.config/herdr-loop/gh-token", worker_claude_ref="-", ntfy_password_ref="keychain:loop-ntfy")
        code, out, err = self.budget()
        answer = json.loads(out)
        self.assertEqual((code, err, answer["applies"], answer["hold"]), (0, "", False, False))
        self.assertIn("program.json names no op:// reference", answer["why_not"])
        self.assertNotIn("account_remaining", answer)
        self.assertFalse((self.bin / "seen").exists(), "op was asked about an account the program does not use")
        # Whether the keeper's last read worked still decides whether to hold.
        failed = {"running": True, "failed_reads": 1, "last_read": {"name": "gh", "ok": False, "at": "2026-10-13T00:00:00Z"}}
        with mock.patch.object(keeper, "status", return_value=failed):
            answer = json.loads(self.budget()[1])
        self.assertEqual((answer["applies"], answer["hold"], answer["why"]), (False, True, "the keeper's last read (gh) failed"))
        # One op:// reference among them is enough for it to apply.
        self.settings(ntfy_password_ref=NTFY_REF, worker_claude_ref="-", gh_token_ref="file:~/x")
        self.assertTrue(json.loads(self.budget()[1])["applies"])

    def test_the_counters_come_back_as_json_with_the_accounts_remaining_requests(self):
        code, out, err = self.budget()
        self.assertEqual((code, err), (0, ""))
        answer = json.loads(out)
        self.assertEqual(answer["account_remaining"], 154)
        self.assertEqual(
            answer["limits"][2],
            {"type": "account", "action": "read_write", "limit": 1000, "used": 846, "remaining": 154, "reset": "9 hours from now"},
        )
        self.assertEqual([row["remaining"] for row in answer["limits"]], [100, 788, 154])

    def test_op_is_asked_the_one_thing_with_an_empty_home_and_nothing_else_of_the_environment(self):
        os.environ["GH_TOKEN"] = "loop-token"
        self.budget()
        seen = self.seen()
        self.assertEqual((seen["argv"], seen["token"]), ("service-account ratelimit", "expected"))
        self.assertEqual((seen["path"], seen["others"], seen["stdin"]), (f"{self.bin}:/usr/bin:/bin", "", ""))
        self.assertEqual(seen["home_entries"], "0")
        self.assertNotEqual(seen["home"], os.environ["HOME"])
        self.assertFalse(Path(seen["home"]).exists(), "the home made for the call is removed after it")

    def test_it_reads_no_secret_and_starts_no_keeper(self):
        self.budget()
        self.assertEqual((self.reads(), self.started(), (self.root / "secrets.sock").exists()), ([], 0, False))

    def test_it_needs_no_settings(self):
        (self.root / "program.json").unlink()
        self.assertEqual(self.budget()[0], 0)

    def test_counters_printed_with_a_failing_exit_are_still_given(self):
        self.answer(TABLE, stderr="[ERROR] rate limit exceeded\n", rc=1)
        code, out, _ = self.budget()
        self.assertEqual((code, json.loads(out)["account_remaining"]), (0, 154))

    def test_output_that_is_not_the_table_is_printed_as_it_came(self):
        odd = ("Requests left today: 154\n", "TYPE  LIMIT\naccount  1000\n", TABLE.replace("846", "many"), TABLE + "Counters are approximate.\n", "TYPE  LIMIT  USED  REMAINING\n")
        for text in odd:
            self.answer(text)
            self.assertEqual(self.budget(), (0, text, ""))

    def test_a_table_without_the_accounts_row_says_so(self):
        self.answer("\n".join(TABLE.splitlines()[:3]) + "\n")
        answer = json.loads(self.budget()[1])
        self.assertEqual((answer["account_remaining"], len(answer["limits"])), (None, 2))

    def test_op_saying_nothing_is_a_refusal_with_what_it_said(self):
        self.answer("", stderr="[ERROR] 2026/10/13 could not reach the server\n", rc=1)
        code, out, _ = self.budget()
        self.assertEqual(code, 2)
        self.assertIn("printed no counters (exit 1): [ERROR] 2026/10/13 could not reach the server", json.loads(out)["refused"][0])

    def test_the_accounts_token_is_never_printed(self):
        token = os.environ["OP_SERVICE_ACCOUNT_TOKEN"]
        self.answer(f"debug: authenticated with {token}\n")
        code, out, err = self.budget()
        self.assertEqual((code, out), (0, "debug: authenticated with [withheld]\n"))
        self.answer("", stderr=f"bad token {token}\n", rc=1)
        code, out, err = self.budget()
        self.assertNotIn(token, out + err)
        self.assertIn("bad token [withheld]", out)

    @unittest.skipUnless(ZSH, "needs zsh")
    def test_a_token_only_a_zsh_on_the_host_sets_is_found(self):
        token = os.environ.pop("OP_SERVICE_ACCOUNT_TOKEN")
        (Path(os.environ["ZDOTDIR"]) / ".zshenv").write_text(f"OP_SERVICE_ACCOUNT_TOKEN={token}\n")
        self.assertEqual(self.budget()[0], 0)
        self.assertEqual(self.seen()["token"], "expected")

    def test_no_service_account_token_anywhere_is_told_plainly_that_it_does_not_apply_and_op_is_not_run(self):
        del os.environ["OP_SERVICE_ACCOUNT_TOKEN"]
        code, out, err = self.budget()
        answer = json.loads(out)
        self.assertEqual((code, err, answer["applies"], answer["hold"]), (0, "", False, False))
        self.assertIn("OP_SERVICE_ACCOUNT_TOKEN is not in this environment and a zsh on this host does not set it", answer["why_not"])
        self.assertEqual(answer["why"], "the keeper has read nothing yet: hold only when a read fails")
        self.assertFalse((self.bin / "seen").exists())

    def test_a_host_without_op_is_a_refusal(self):
        self.op.unlink()
        with mock.patch.object(keeper, "OP_PATH", str(self.bin)):
            code, out, _ = self.budget()
        self.assertEqual(code, 2)
        self.assertIn("`op` is not in", json.loads(out)["refused"][0])

    def test_an_op_that_does_not_answer_is_a_refusal(self):
        self.op.write_text("#!/bin/sh\nexec sleep 5\n")
        with mock.patch.object(keeper, "READ_TIMEOUT_S", 0.3):
            code, out, _ = self.budget()
        self.assertEqual(code, 2)
        self.assertIn("did not answer within 0.3 seconds", json.loads(out)["refused"][0])


# ---------------------------------------------------------------- the scripts that use it


class LoopTokenTest(SecretsCase):
    def test_a_token_in_the_environment_is_used_as_it_is_and_nothing_is_asked(self):
        os.environ["GH_TOKEN"] = "the-callers-own"
        merge.require_loop_token(self.root)
        merge.require_loop_token()
        self.assertEqual(os.environ["GH_TOKEN"], "the-callers-own")
        self.assertEqual((self.reads(), self.started()), ([], 0))

    def test_without_one_the_roots_keeper_gives_it_to_this_process(self):
        merge.require_loop_token(self.root)
        self.assertEqual(os.environ["GH_TOKEN"], fake(GH_REF))
        # Tools copies the environment when it is built, which is why the check comes first.
        self.assertEqual(merge.Tools(git="/usr/bin/git", gh="/usr/bin/true").env["GH_TOKEN"], fake(GH_REF))
        merge.require_loop_token(self.root)
        self.assertEqual(self.reads(), [GH_REF])

    def test_without_the_variable_and_without_a_root_it_refuses_as_it_did(self):
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token()
        self.assertIn("GH_TOKEN is not set", str(caught.exception))
        self.assertNotIn("GH_TOKEN", os.environ)

    def test_a_program_that_does_not_say_keeper_is_refused_as_before_and_nothing_is_started(self):
        for mode in (None, "direct"):
            self.settings(secrets=mode)
            with self.assertRaises(Refusal) as caught:
                merge.require_loop_token(self.root)
            self.assertIn("GH_TOKEN is not set", str(caught.exception))
        self.assertEqual((self.reads(), self.started(), (self.root / "secrets.lock").exists()), ([], 0, False))

    def test_a_directory_that_is_no_program_is_no_root(self):
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token(self.tmp)
        # The refusal there always was, and nothing about settings that such a directory was never meant to have.
        self.assertIn("GH_TOKEN is not set: run this through", str(caught.exception))
        self.assertNotIn("program.json does not exist", str(caught.exception))

    def test_a_setting_that_is_neither_is_refused(self):
        self.settings(secrets="keepr")
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token(self.root)
        self.assertIn("secrets is 'keepr'; it is `keeper` or `direct`", str(caught.exception))

    def test_settings_that_cannot_be_read_are_a_refusal_for_the_token_and_not_a_fault(self):
        (self.root / "program.json").write_text("{ this is not json\n")
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token(self.root)
        self.assertIn("GH_TOKEN is not set", str(caught.exception))
        self.settings(base="{{BASE}}")
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token(self.root)
        self.assertIn("GH_TOKEN is not set", str(caught.exception))
        self.assertIn("unfilled placeholders", str(caught.exception))
        self.assertEqual((self.reads(), self.started()), ([], 0))

    def test_a_token_that_cannot_be_read_is_a_refusal_that_says_what_the_reader_said(self):
        self.set("mode", "fail")
        with self.assertRaises(Refusal) as caught:
            merge.require_loop_token(self.root)
        self.assertIn("GH_TOKEN is not set, and the program's keeper did not give the loop's token", str(caught.exception))
        self.assertIn(SAID, str(caught.exception))
        self.assertNotIn("GH_TOKEN", os.environ)

    def _main(self, module, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(argv)
        return code, json.loads(out.getvalue())

    def test_each_command_line_that_reaches_github_gets_the_token_from_its_root(self):
        """Each then stops at the next thing it needs, the pinned binaries,
        which this root does not have: the token is no longer what is missing."""
        run_dir = self.root / "runs" / "run-1"
        cases = (
            (flow, ["--root", str(self.root), "--run-dir", str(run_dir), "prepare", "--id", "A-1"]),
            (verify, ["--root", str(self.root), "--run-dir", str(run_dir), "--repo-dir", str(self.tmp), "--id", "A-1"]),
            (merge, ["--root", str(self.root), "--run-dir", str(run_dir), "--repo-dir", str(self.tmp), "--id", "A-1"]),
            (train, ["--root", str(self.root), "--repo", "owner/repo", "status"]),
        )
        for module, argv in cases:
            os.environ.pop("GH_TOKEN", None)
            code, out = self._main(module, argv)
            self.assertEqual(code, 2, module.__name__)
            self.assertIn("hl-claim pin", out["refused"][0], module.__name__)
            self.assertEqual(os.environ.get("GH_TOKEN"), fake(GH_REF), module.__name__)
        self.assertEqual(self.reads(), [GH_REF])

    def test_hl_merge_without_root_finds_the_program_its_run_directory_lies_in(self):
        run_dir = self.root / "runs" / "run-1"
        run_dir.mkdir(parents=True)
        argv = ["--run-dir", str(run_dir), "--repo-dir", str(self.tmp), "--git", "/usr/bin/git", "--gh", "/usr/bin/true", "--release-own"]
        with mock.patch("hl.merge.release_own", return_value=[]) as released:
            self.assertEqual(self._main(merge, argv), (0, {"released": []}))
        self.assertEqual(released.call_args[0][0].env["GH_TOKEN"], fake(GH_REF))

    def test_hl_wait_gets_the_token_when_it_watches_checks_or_a_train(self):
        run_dir = self.root / "runs" / "run-1"
        for watched in (["--checks", "A-1"], ["--train", "owner/repo"]):
            os.environ.pop("GH_TOKEN", None)
            with mock.patch("hl.wait.items.load_item"), mock.patch("hl.wait.claim.check_pins", return_value={"bins": {"git": "/usr/bin/git", "gh": "/usr/bin/true"}}):
                with mock.patch("hl.wait.wait", return_value={"event": "time"}) as waited:
                    code, out = self._main(wait, ["--root", str(self.root), "--run-dir", str(run_dir), *watched])
            self.assertEqual((code, out), (0, {"event": "time"}), watched)
            self.assertEqual(waited.call_args[0][4].env["GH_TOKEN"], fake(GH_REF), watched)
        self.assertEqual(self.reads(), [GH_REF])

    def test_hl_wait_without_a_root_still_needs_the_variable(self):
        run_dir = self.root / "runs" / "run-1"
        with mock.patch("hl.wait.items.load_item"):
            code, out = self._main(wait, ["--run-dir", str(run_dir), "--checks", "A-1", "--gh", "/usr/bin/true"])
        self.assertEqual(code, 2)
        self.assertIn("GH_TOKEN is not set", out["refused"][0])


@unittest.skipUnless(ZSH, "needs zsh")
class EnvTest(SecretsCase):
    """hl-env.sh, sourced by a real zsh that has no startup files."""

    # The exit status, whether each variable is set, and whether it is the value the keeper holds. Never a value.
    REPORT = (
        'echo "rc=$?"; echo "claude=${CLAUDE_CODE_OAUTH_TOKEN:+set} gh=${GH_TOKEN:+set} api=${ANTHROPIC_API_KEY:+set} memory=${CLAUDE_CODE_DISABLE_AUTO_MEMORY:-}"; '
        '[ "$GH_TOKEN" = "$WANT_GH" ] && echo gh-is-the-kept-one; [ "$CLAUDE_CODE_OAUTH_TOKEN" = "$WANT_CLAUDE" ] && echo claude-is-the-kept-one; '
        'echo "left=${hl_env_dir:-}$(command -v hl_env)"'
    )

    def setUp(self):
        super().setUp()
        # hl-env exports this one, and the report says whether it is set: a caller's own, as in a loop's pane, would read as hl-env's.
        os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)

    def source(self, *args, before="", script=HL_ENV):
        line = f"{before}\n. {script} {' '.join(args)}\n{self.REPORT}\n"
        env = {**os.environ, "WANT_GH": fake(GH_REF), "WANT_CLAUDE": fake(CLAUDE_REF)}
        proc = subprocess.run([ZSH, "-f", "-c", line], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
        for value in (fake(GH_REF), fake(CLAUDE_REF)):
            self.assertNotIn(value, proc.stdout + proc.stderr, "a secret was printed")
        return proc.stdout.strip().splitlines()

    def test_with_the_root_both_tokens_come_from_the_keeper(self):
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root))
        self.assertEqual(
            out,
            ["hl-env ok ab12cd34-ready", "rc=0", "claude=set gh=set api= memory=1", "gh-is-the-kept-one", "claude-is-the-kept-one", "left="],
        )
        self.assertEqual(self.reads(), [CLAUDE_REF, GH_REF])

    def test_a_second_and_a_third_worker_cost_no_read(self):
        for nonce in ("ab12cd34", "0000beef", "12345678"):
            self.assertEqual(self.source(CLAUDE_REF, GH_REF, nonce, str(self.root))[0], f"hl-env ok {nonce}-ready")
        self.assertEqual(self.reads(), [CLAUDE_REF, GH_REF])

    def test_a_cursor_worker_gets_no_claude_token_and_none_is_read(self):
        out = self.source("-", GH_REF, "ab12cd34", str(self.root))
        self.assertEqual(out, ["hl-env ok ab12cd34-ready", "rc=0", "claude= gh=set api= memory=1", "gh-is-the-kept-one", "left="])
        self.assertEqual(self.reads(), [GH_REF])

    def test_a_claude_worker_of_a_program_on_the_signed_in_account_gets_only_the_github_token(self):
        self.settings(worker_claude_ref="-")
        out = self.source("-", GH_REF, "ab12cd34", str(self.root))
        self.assertEqual(out, ["hl-env ok ab12cd34-ready", "rc=0", "claude= gh=set api= memory=1", "gh-is-the-kept-one", "left="])
        self.assertEqual(self.reads(), [GH_REF])

    def test_a_claude_token_the_keeper_cannot_give_fails_closed_with_what_the_reader_said(self):
        self.settings(worker_claude_ref="op://v/missing/password")
        out = self.source("op://v/missing/password", GH_REF, "ab12cd34", str(self.root))
        self.assertEqual(
            out,
            [
                f"hl-env FAILED ab12cd34: no Claude token from the keeper: the reader did not give `claude` (op://v/missing/password): {SAID}",
                "rc=1",
                "claude= gh= api= memory=",
                "left=",
            ],
        )
        # The GitHub token was not asked for: the first failure ends it.
        self.assertEqual(self.reads(), ["op://v/missing/password"])

    def test_a_github_token_the_keeper_cannot_give_exports_nothing_at_all(self):
        self.settings(gh_token_ref="op://v/missing/password")
        out = self.source(CLAUDE_REF, "op://v/missing/password", "ab12cd34", str(self.root))
        self.assertEqual(out[0], f"hl-env FAILED ab12cd34: no GitHub token from the keeper: the reader did not give `gh` (op://v/missing/password): {SAID}")
        self.assertEqual(out[1:], ["rc=1", "claude= gh= api= memory=", "left="])

    def test_the_failure_line_is_the_one_the_spawn_waits_for(self):
        self.set("mode", "fail")
        out = self.source("-", GH_REF, "ab12cd34", str(self.root))
        self.assertRegex(out[0], r"^hl-env (ok ab12cd34-ready|FAILED ab12cd34:)")

    def test_tokens_already_in_the_shell_are_dropped_before_the_keeper_is_asked(self):
        self.set("mode", "fail")
        before = "export GH_TOKEN=keyring-admin CLAUDE_CODE_OAUTH_TOKEN=signed-in ANTHROPIC_API_KEY=key"
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), before=before)
        self.assertEqual(out[1:], ["rc=1", "claude= gh= api= memory=", "left="])
        self.assertEqual((self.bin / "reads.log").read_text(), CLAUDE_REF + " \n")

    def test_a_keeper_that_gives_no_answer_fails_closed(self):
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.tmp / "nowhere"))
        self.assertRegex(out[0], r"^hl-env FAILED ab12cd34: no Claude token from the keeper: /.*/nowhere holds no program\.json: it is not a loop root, and no keeper was started$")
        self.assertEqual(out[1:], ["rc=1", "claude= gh= api= memory=", "left="])
        broken = self.tmp / "kit"
        broken.mkdir()
        shutil.copy(HL_ENV, broken / "hl-env.sh")
        # `ok` with nothing after it is not a token.
        (broken / "hl-secrets").write_text("#!/bin/sh\necho 'ok '\necho 'Traceback' >&2\nexit 1\n")
        (broken / "hl-secrets").chmod(0o755)
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), script=broken / "hl-env.sh")
        self.assertEqual(out, ["hl-env FAILED ab12cd34: no Claude token from the keeper: hl-secrets gave no answer", "rc=1", "claude= gh= api= memory=", "left="])
        out = self.source("-", GH_REF, "ab12cd34", str(self.root), script=broken / "hl-env.sh")
        self.assertEqual(out[0], "hl-env FAILED ab12cd34: no GitHub token from the keeper: hl-secrets gave no answer")

    def test_a_kit_without_hl_secrets_beside_it_fails_closed(self):
        alone = self.tmp / "kit"
        alone.mkdir()
        shutil.copy(HL_ENV, alone / "hl-env.sh")
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), script=alone / "hl-env.sh")
        self.assertEqual(out, [f"hl-env FAILED ab12cd34: {alone}/hl-secrets is not there to ask", "rc=1", "claude= gh= api= memory=", "left="])

    def test_a_shell_that_refuses_unset_parameters_still_gets_its_line(self):
        out = self.source("env:WANT_CLAUDE", "env:WANT_GH", "ab12cd34", before="setopt nounset")
        self.assertEqual(out[:2], ["hl-env ok ab12cd34-ready", "rc=0"])
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), before="setopt nounset")
        self.assertEqual(out[:2], ["hl-env ok ab12cd34-ready", "rc=0"])
        out = self.source(before="setopt nounset")
        self.assertEqual(out[:2], ["hl-env FAILED: usage: hl-env.sh <claude-ref|-> <gh-ref> <nonce> [<loop root>]", "rc=1"])

    def test_a_shell_that_exits_on_a_failure_still_prints_the_line_the_spawn_waits_for(self):
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), before="set -e")
        self.assertEqual(out[:2], ["hl-env ok ab12cd34-ready", "rc=0"])
        self.cli("forget")
        self.set("mode", "fail")
        # The failed read is where such a shell stops; the line it leaves is the one that says why.
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34", str(self.root), before="set -e")
        self.assertEqual(out, [f"hl-env FAILED ab12cd34: no Claude token from the keeper: the reader did not give `claude` ({CLAUDE_REF}): {SAID}"])
        out = self.source("-", GH_REF, "ab12cd34", str(self.root), before="set -e")
        self.assertEqual(out, [f"hl-env FAILED ab12cd34: no GitHub token from the keeper: the reader did not give `gh` ({GH_REF}): {SAID}"])

    def test_hl_secrets_is_found_beside_the_file_however_it_was_named(self):
        out = self.source("-", GH_REF, "ab12cd34", str(self.root), before=f"cd {HL_ENV.parent}", script="./hl-env.sh")
        self.assertEqual(out[0], "hl-env ok ab12cd34-ready")
        link = self.tmp / "linked-kit"
        link.symlink_to(HL_ENV.parent)
        out = self.source("-", GH_REF, "ab12cd34", str(self.root), script=link / "hl-env.sh")
        self.assertEqual(out[0], "hl-env ok ab12cd34-ready")

    def test_without_the_root_it_reads_with_hl_read_secret_as_it_always_did_and_asks_no_keeper(self):
        # `env:` references, which the kit's reader reads from the pane's own environment: the values the keeper would give.
        out = self.source("env:WANT_CLAUDE", "env:WANT_GH", "ab12cd34")
        self.assertEqual(out, ["hl-env ok ab12cd34-ready", "rc=0", "claude=set gh=set api= memory=1", "gh-is-the-kept-one", "claude-is-the-kept-one", "left="])
        out = self.source("env:WANT_CLAUDE", "env:NOT_SET_ANYWHERE", "ab12cd34")
        self.assertEqual(out, ["hl-env FAILED ab12cd34: no GitHub token: hl-read-secret: env:NOT_SET_ANYWHERE is not set", "rc=1", "claude= gh= api= memory=", "left="])
        # The program's references are op:// ones, and the reader is the kit's, not the program's: `op` here is the
        # test's guard, which refuses, and the pane fails closed with what it said.
        out = self.source(CLAUDE_REF, GH_REF, "ab12cd34")
        self.assertEqual(out[0], "hl-env FAILED ab12cd34: no Claude token: hl-read-secret: op read failed (exit 1): a test reached op: no test reads a real secret store")
        (self.guard / "reached").unlink()
        self.assertEqual((self.reads(), self.started()), ([], 0))


class SpawnLineTest(SpawnCase):
    """What `hl-spawn` types into a worker's pane."""

    def typed(self):
        self.start()
        return self.herdr.called("pane", "run")[0][3]

    def program(self, **changed):
        path = self.root / "program.json"
        write_json(path, {**json.loads(path.read_text()), **changed})

    def test_a_program_that_uses_the_keeper_gives_the_pane_its_root(self):
        self.program(secrets="keeper")
        self.assertRegex(self.typed(), rf"hl-env\.sh op://Loop/worker-claude/password op://Loop/loop-gh/password [0-9a-f]{{8}} {re.escape(str(self.root))}$")

    def test_a_program_that_does_not_types_what_the_kit_always_typed(self):
        self.assertRegex(self.typed(), r"hl-env\.sh op://Loop/worker-claude/password op://Loop/loop-gh/password [0-9a-f]{8}$")

    def test_a_root_with_a_space_in_it_is_one_word(self):
        self.program(secrets="keeper")
        cfg = {**spawn.program.load(self.root), "root": "/Users/you/loops/two words"}
        herdr = mock.Mock()
        herdr.call.return_value = {"matched_line": "hl-env ok ab12cd34-ready"}
        with mock.patch("hl.spawn.secrets.token_hex", return_value="ab12cd34"):
            spawn._load_secrets(herdr, "w1:1", cfg, "cursor")
        self.assertTrue(herdr.call.call_args_list[0][0][3].endswith(" ab12cd34 '/Users/you/loops/two words'"))

    def test_a_setting_that_is_neither_stops_the_spawn(self):
        self.program(secrets="yes")
        with self.assertRaises(Refusal) as caught:
            self.start()
        self.assertIn("it is `keeper` or `direct`", str(caught.exception))


class WatchdogNoticeTest(NtfyCase):
    """The watchdog's notice, with the password from the keeper."""

    def cfg(self):
        return watchdog.program.load(self.root)

    def test_the_password_comes_from_the_keeper_and_is_read_once(self):
        seen = self.serve()
        system = watchdog.System()
        with mock.patch.object(watchdog.System, "secret") as direct:
            for n in (1, 2, 3):
                self.assertTrue(system.notify(self.cfg(), f"canary: manager started ({n})", "because"))
        direct.assert_not_called()
        self.assertEqual([s["Authorization"] for s in seen], [self.basic()] * 3)
        self.assertEqual((seen[0]["Title"], seen[0]["body"], seen[0]["Priority"]), ("canary: manager started (1)", "because", None))
        self.assertEqual(self.reads(), [NTFY_REF])

    def test_a_keeper_that_cannot_be_reached_leaves_the_watchdog_reading_it_itself(self):
        seen = self.serve()
        (self.root / "secrets.sock").write_text("not a socket\n")
        with mock.patch.object(watchdog.System, "secret", return_value=("read-by-the-watchdog", None)) as direct:
            self.assertTrue(watchdog.System().notify(self.cfg(), "canary: manager started", "because"))
        # With the program's own reader.
        direct.assert_called_once_with(NTFY_REF, [sys.executable, str(self.reader)])
        self.assertEqual(seen[0]["Authorization"], self.basic("read-by-the-watchdog"))
        (self.root / "secrets.sock").unlink()

    def test_a_read_the_store_refused_is_not_tried_again_from_the_watchdog_and_the_log_says_why(self):
        self.serve()
        self.set("mode", "fail")
        with mock.patch.object(watchdog.System, "secret") as direct:
            self.assertFalse(watchdog.System().notify(self.cfg(), "canary: watchdog halted", "three starts"))
            self.assertFalse(watchdog.System().notify(self.cfg(), "canary: watchdog halted", "three starts"))
        direct.assert_not_called()
        self.assertEqual(self.reads(), [NTFY_REF])
        log = (self.root / "watchdog.log").read_text()
        self.assertIn(f"not notified (canary: watchdog halted): the reader did not give `ntfy` ({NTFY_REF}): {SAID}", log)

    def test_a_server_that_cannot_be_reached_is_logged_and_nothing_is_raised(self):
        self.settings(ntfy_url="http://127.0.0.1:9/loop-topic")
        self.assertFalse(watchdog.System().notify(self.cfg(), "canary: manager started", "because"))
        log = (self.root / "watchdog.log").read_text()
        self.assertIn("not notified (canary: manager started): http://127.0.0.1:9/loop-topic did not take it", log)
        self.assertNotIn(fake(NTFY_REF), log)

    def test_a_keeper_that_took_the_request_and_did_not_answer_is_not_read_around(self):
        self.serve()
        gone = Refusal("the keeper took the request and went without answering it")
        with mock.patch("hl.watchdog.keeper.get", side_effect=gone), mock.patch.object(watchdog.System, "secret") as direct:
            self.assertFalse(watchdog.System().notify(self.cfg(), "canary: manager started", "because"))
        direct.assert_not_called()
        self.assertIn("not notified (canary: manager started): the keeper took the request and went", (self.root / "watchdog.log").read_text())

    def test_nothing_that_goes_wrong_in_a_notice_reaches_the_tick(self):
        seen = self.serve()
        with mock.patch("hl.watchdog.keeper.get", side_effect=RuntimeError("a fault nobody planned for")):
            self.assertFalse(watchdog.System().notify(self.cfg(), "canary: manager started", "because"))
        with mock.patch("hl.watchdog.keeper.post_notice", side_effect=RuntimeError(f"a fault that names {fake(NTFY_REF)}")):
            self.assertFalse(watchdog.System().notify(self.cfg(), "canary: watchdog halted", "three starts"))
        log = (self.root / "watchdog.log").read_text()
        self.assertIn("not notified (canary: manager started): RuntimeError: a fault nobody planned for", log)
        self.assertIn("not notified (canary: watchdog halted): RuntimeError: a fault that names [withheld]", log)
        self.assertEqual(seen, [])

    def test_a_setting_that_is_neither_does_not_stop_a_tick(self):
        self.serve()
        self.settings(secrets="keepr", ntfy_url="http://127.0.0.1:9/x")
        with mock.patch.object(watchdog.System, "secret") as direct:
            self.assertFalse(watchdog.System().notify(self.cfg(), "t", "b"))
        direct.assert_not_called()
        self.assertIn("it is `keeper` or `direct`", (self.root / "watchdog.log").read_text())

    def test_a_program_that_does_not_use_the_keeper_is_read_as_before(self):
        seen = self.serve()
        self.settings(secrets=None, ntfy_url=self.cfg()["ntfy_url"])
        with mock.patch.object(watchdog.System, "secret", return_value=("read-by-the-watchdog", None)) as direct:
            self.assertTrue(watchdog.System().notify(self.cfg(), "t", "b"))
        direct.assert_called_once_with(NTFY_REF, [sys.executable, str(self.reader)])
        self.assertEqual(seen[0]["Authorization"], self.basic("read-by-the-watchdog"))
        self.assertEqual((self.reads(), self.started()), ([], 0))

    def test_without_the_keeper_the_watchdog_reads_with_the_programs_reader_once_per_notice(self):
        seen = self.serve()
        self.settings(secrets="direct", ntfy_url=self.cfg()["ntfy_url"])
        for n in (1, 2):
            self.assertTrue(watchdog.System().notify(self.cfg(), f"t{n}", "b"))
        self.assertEqual([s["Authorization"] for s in seen], [self.basic()] * 2)
        self.assertEqual((self.reads(), self.started()), ([NTFY_REF, NTFY_REF], 0))

    def test_without_a_reader_of_the_programs_own_the_watchdog_reads_with_hl_read_secret(self):
        seen = self.serve()
        os.environ["LOOP_NTFY_PASSWORD"] = fake(NTFY_REF)
        self.settings(secrets="direct", secrets_reader=None, ntfy_password_ref="env:LOOP_NTFY_PASSWORD", ntfy_url=self.cfg()["ntfy_url"])
        self.assertTrue(watchdog.System().notify(self.cfg(), "t", "b"))
        self.assertEqual(seen[0]["Authorization"], self.basic())
        # One it cannot read is logged with the reader's own line.
        del os.environ["LOOP_NTFY_PASSWORD"]
        self.assertFalse(watchdog.System().notify(self.cfg(), "t", "b"))
        self.assertIn(
            "not notified (t): the reader did not give the ntfy password (env:LOOP_NTFY_PASSWORD): hl-read-secret: env:LOOP_NTFY_PASSWORD is not set",
            (self.root / "watchdog.log").read_text(),
        )

    def test_a_reader_that_is_not_a_command_is_logged_and_nothing_is_read(self):
        self.serve()
        self.settings(secrets="direct", secrets_reader="hl-read-secret", ntfy_url=self.cfg()["ntfy_url"])
        with mock.patch.object(watchdog.System, "secret") as direct:
            self.assertFalse(watchdog.System().notify(self.cfg(), "t", "b"))
        direct.assert_not_called()
        self.assertIn("not notified (t): program.json: secrets_reader is a command as a list of words", (self.root / "watchdog.log").read_text())

    def test_no_password_reference_is_logged(self):
        self.settings(ntfy_password_ref=None, ntfy_url="http://127.0.0.1:9/x")
        self.assertFalse(watchdog.System().notify(self.cfg(), "t", "b"))
        self.assertIn("names no ntfy_password_ref", (self.root / "watchdog.log").read_text())


if __name__ == "__main__":
    unittest.main()
