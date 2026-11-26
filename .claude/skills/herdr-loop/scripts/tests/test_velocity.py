"""How long a passed item waits to merge and what a merged item costs, as a save reports it (`velocity`): the
numbers read from the run files, the transcript tallies, what is kept so a transcript is read once, the read's time
budget, and that nothing here fails a save. Transcripts are files written here; `find` stands in for the search of the
Claude homes."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hl import items, velocity
from hl.common import read_json, write_json

PROGRAM = {"program": "p", "base": "main", "worker_claude_ref": "-", "gh_token_ref": "op://x/y/z", "cursor_model": "composer", "manager_model": "claude-opus-5-5"}

BACKLOG = "\n\n---\n\n".join(
    f"## T-{n} — Item {n}\n\n**Scope.** Thing {n}.\n\n**Accept.**\n1. It works.\n\n**Verify.** `true`" for n in range(1, 9)
)


def build(n, at, effort="high", engine="claude", session=None):
    return {"phase": "build", "build": n, "n": n, "at": at, "effort": effort, "engine": engine, **({"session": session} if session else {})}


def grade(n, build_n, at, effective=None, read_at=None, confirm=False, session=None):
    entry = {"phase": "grade", "n": n, "build": build_n, "at": at, "engine": "claude", **({"session": session} if session else {})}
    if effective:
        entry["read"] = {"at": read_at or at, "effective": effective}
    if confirm:
        entry["kind"] = items.CONFIRM
    return entry


def assistant(message_id, at, usage, model="claude-sonnet-5-5", bash=(), sidechain=False):
    content = [{"type": "tool_use", "id": b, "name": "Bash", "input": {"command": "hl-flow save"}} for b in bash]
    record = {"type": "assistant", "timestamp": at, "message": {"id": message_id, "model": model, "usage": usage, "content": content}}
    if sidechain:
        record["isSidechain"] = True
    return json.dumps(record)


def usage(inp=0, write=0, read=0, out=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": out}


# The line Claude Code writes each time it compacts a conversation (2.1.329, as read on the mini).
COMPACTED = json.dumps({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted", "isMeta": False, "level": "info", "compactMetadata": {"trigger": "auto", "preTokens": 205393, "postTokens": 8087}})


class VelocityCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        write_json(self.root / "program.json", PROGRAM)
        self.backlog = self.root / "T-ITEMS.md"
        self.backlog.write_text(BACKLOG)
        self.transcripts = self.root / "home" / "projects" / "-worktrees"
        self.transcripts.mkdir(parents=True)
        self.manager = self.root / "home" / "projects" / "-root"
        self.manager.mkdir(parents=True)
        self.clock = [0.0]

    def run_of(self, name, repo="owner/repo", policy=None):
        run_dir = self.root / "runs" / name
        items.init_run(run_dir, name, repo, [str(self.backlog)], policy=policy)
        return run_dir

    def item(self, run_dir, item_id, log, merged_at=None, reviews=None):
        if merged_at:
            item = items.load_item(run_dir, item_id)
            item["status"] = "merged"
            item["merged_at"] = merged_at
            items.save_item(run_dir, item)
        folder = items.item_dir(run_dir, item_id)
        write_json(folder / "attempts.json", {"log": log})
        for n, record in (reviews or {}).items():
            write_json(folder / f"review-{n}.json", record)

    def transcript(self, session, lines, folder=None):
        path = (folder or self.transcripts) / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return path

    def find(self, session):
        path = self.transcripts / f"{session}.jsonl"
        if not path.exists():
            return []
        return [path] + sorted((self.transcripts / session / "subagents").glob("*.jsonl"))

    def facts(self, **kw):
        kw.setdefault("find", self.find)
        kw.setdefault("manager_dir", self.manager)
        kw.setdefault("clock", lambda: self.clock[0])
        kw.setdefault("now", lambda: velocity._epoch("2026-11-19T12:00:00Z"))
        return velocity.facts({**velocity.program.DEFAULTS, **PROGRAM}, self.root, **kw)


class FromTheRunFiles(VelocityCase):
    def test_the_wait_runs_from_the_last_passing_review_to_the_merge(self):
        run = self.run_of("r1")
        # T-1: passed at 10:00, merged 10:20. T-2: failed, rebuilt, passed at 10:00 and again at 11:00 after CI, merged 12:00.
        self.item(run, "T-1", [build(1, "2026-11-19T09:00:00Z"), grade(1, 1, "2026-11-19T09:30:00Z", "PASS", "2026-11-19T10:00:00Z")], merged_at="2026-11-19T10:20:00Z")
        self.item(
            run,
            "T-2",
            [
                build(1, "2026-11-19T08:00:00Z"),
                grade(1, 1, "2026-11-19T08:30:00Z", "FAIL"),
                build(2, "2026-11-19T09:00:00Z"),
                grade(2, 2, "2026-11-19T09:30:00Z", "PASS", "2026-11-19T10:00:00Z"),
                grade(3, 2, "2026-11-19T10:30:00Z", "PASS", "2026-11-19T11:00:00Z", confirm=True),
                {**grade(4, 2, "2026-11-19T10:40:00Z", "PASS"), "voided": True},
            ],
            merged_at="2026-11-19T12:00:00Z",
            reviews={1: {"effective": "FAIL", "criteria_in_question": ["3"]}},
        )
        # T-3: its only review said INCONCLUSIVE in its record, not in the log, and then it passed.
        self.item(
            run,
            "T-3",
            [build(1, "2026-11-19T07:00:00Z"), grade(1, 1, "2026-11-19T07:30:00Z"), grade(2, 1, "2026-11-19T08:00:00Z", "PASS")],
            merged_at="2026-11-19T09:00:00Z",
            reviews={1: {"effective": "INCONCLUSIVE"}},
        )
        # Not merged: not in the window.
        self.item(run, "T-4", [build(1, "2026-11-19T07:00:00Z")])
        out = self.facts()
        self.assertEqual(out["window"], {"merged": 3, "since": "2026-11-19T09:00:00Z"})
        self.assertEqual(out["merge_wait_minutes"], {"median": 60.0, "p90": 60.0, "items": 3})
        self.assertEqual((out["reviews_per_item"], out["delta_confirms"], out["rebuilt"]), (2.0, 1, 0.33))
        self.assertEqual(out["non_pass_reviews"], {"reviews": 2, "inconclusive": 1, "criterion_questioned": 1})
        self.assertEqual(out["errors"], [])

    def test_only_the_last_merges_count_and_a_train_is_left_out(self):
        run = self.run_of("r1")
        train = self.run_of("r2", repo="owner/trained", policy={"review": "code", "merge": "train", "train": {"branch": "hl-train/p", "item_checks": []}})
        for n in range(1, 5):
            self.item(run, f"T-{n}", [grade(1, 1, f"2026-11-19T0{n}:00:00Z", "PASS")], merged_at=f"2026-11-19T0{n}:{10 * n}:00Z")
        self.item(train, "T-1", [grade(1, 1, "2026-11-19T09:00:00Z", "PASS")], merged_at="2026-11-19T09:59:00Z")
        with mock.patch.object(velocity, "WINDOW", 3):
            out = self.facts()
        # T-4, T-3, T-2: 40, 30 and 20 minutes.
        self.assertEqual((out["window"]["merged"], out["merge_wait_minutes"]), (3, {"median": 30.0, "p90": 40.0, "items": 3}))

    def test_first_builds_count_what_their_first_review_said_newest_first(self):
        run = self.run_of("r1")
        self.item(run, "T-1", [build(1, "2026-11-19T01:00:00Z", "medium"), grade(1, 1, "2026-11-19T01:30:00Z", "PASS")])
        self.item(run, "T-2", [build(1, "2026-11-19T02:00:00Z", "medium"), grade(1, 1, "2026-11-19T02:30:00Z", "FAIL"), build(2, "2026-11-19T03:00:00Z")])
        # The verdict from the review's record when the log has none.
        self.item(run, "T-3", [build(1, "2026-11-19T03:00:00Z", "high"), grade(1, 1, "2026-11-19T03:30:00Z")], reviews={1: {"effective": "PASS"}})
        # A first review given back does not count: the one after it does.
        self.item(run, "T-7", [build(1, "2026-11-19T00:30:00Z", "high"), {**grade(1, 1, "2026-11-19T00:40:00Z", "FAIL"), "voided": True}, grade(2, 1, "2026-11-19T00:50:00Z", "PASS")])
        # No review yet, and a Cursor build: neither counts.
        self.item(run, "T-4", [build(1, "2026-11-19T04:00:00Z")])
        self.item(run, "T-5", [build(1, "2026-11-19T05:00:00Z", engine="cursor"), grade(1, 1, "2026-11-19T05:30:00Z", "PASS")])
        # The oldest, past the sample.
        self.item(run, "T-6", [build(1, "2026-11-18T01:00:00Z", "high"), grade(1, 1, "2026-11-18T01:30:00Z", "FAIL")])
        with mock.patch.object(velocity, "FIRST_BUILDS", 4):
            first = self.facts()["first_builds"]
        self.assertEqual(
            first, {"builds": 4, "since": "2026-11-19T00:30:00Z", "first_review_passed": 0.75, "rebuilt": 0.25, "efforts": {"high": 2, "medium": 2}}
        )

    def test_merges_on_a_delta_confirm_count_with_how_their_deploys_came_out(self):
        run = self.run_of("r1")
        train = self.run_of("r2", repo="owner/trained", policy={"review": "code", "merge": "train", "train": {"branch": "hl-train/p", "item_checks": []}})
        failed_then_confirmed = [grade(1, 1, "2026-11-19T01:00:00Z", "FAIL"), build(2, "2026-11-19T01:10:00Z"), grade(2, 2, "2026-11-19T01:30:00Z", "PASS", confirm=True)]
        red_then_confirmed = [grade(1, 1, "2026-11-19T02:00:00Z", "PASS"), build(2, "2026-11-19T02:10:00Z"), {**grade(2, 2, "2026-11-19T02:30:00Z", "PASS", confirm=True), "red_checks": [{"name": "ci"}]}]
        self.item(run, "T-1", failed_then_confirmed, merged_at="2026-11-19T01:40:00Z")
        self.item(run, "T-2", red_then_confirmed, merged_at="2026-11-19T02:40:00Z")
        # The verdict from the confirm's record; a review given back after it does not count.
        self.item(run, "T-3", [grade(1, 1, "2026-11-19T03:00:00Z", "FAIL"), grade(2, 2, "2026-11-19T03:30:00Z", confirm=True), {**grade(3, 2, "2026-11-19T03:35:00Z", "PASS"), "voided": True}],
                  merged_at="2026-11-19T03:40:00Z", reviews={2: {"effective": "PASS"}})
        # Merged on a full review after a confirm that failed, on a review in full, not merged, and on a train: none counts.
        self.item(run, "T-4", [grade(1, 1, "2026-11-19T04:00:00Z", "FAIL"), grade(2, 2, "2026-11-19T04:20:00Z", "FAIL", confirm=True), grade(3, 3, "2026-11-19T04:40:00Z", "PASS")], merged_at="2026-11-19T04:50:00Z")
        self.item(run, "T-5", [grade(1, 1, "2026-11-19T05:00:00Z", "PASS")], merged_at="2026-11-19T05:10:00Z")
        self.item(run, "T-6", failed_then_confirmed)
        self.item(train, "T-1", red_then_confirmed, merged_at="2026-11-19T06:00:00Z")
        for item_id, said in (("T-1", "verified"), ("T-2", "fixed-forward")):
            item = items.load_item(run, item_id)
            item["fields"]["verify"] = said
            items.save_item(run, item)
        self.assertEqual(
            self.facts()["confirm_merges"],
            {"items": 3, "since": "2026-11-19T01:40:00Z", "red_checks": 1, "verified": 1, "fixed_forward": 1, "verify_failed": 0, "unchecked": 1},
        )
        with mock.patch.object(velocity, "CONFIRM_MERGES", 2):
            self.assertEqual(self.facts()["confirm_merges"]["since"], "2026-11-19T02:40:00Z")

    def test_the_windows_deploy_checks_are_counted_as_they_came_out(self):
        run = self.run_of("r1")
        for n, said in enumerate(("verified", "verified", "fixed-forward", "verify-failed", None), start=1):
            self.item(run, f"T-{n}", [grade(1, 1, f"2026-11-19T0{n}:00:00Z", "PASS")], merged_at=f"2026-11-19T0{n}:10:00Z")
            if said:
                item = items.load_item(run, f"T-{n}")
                item["fields"]["verify"] = said
                items.save_item(run, item)
        # Not merged: not in the window, whatever its Verify says.
        self.item(run, "T-6", [grade(1, 1, "2026-11-19T07:00:00Z", "PASS")])
        self.assertEqual(self.facts()["verify"], {"verified": 2, "fixed_forward": 1, "verify_failed": 1, "unchecked": 1})
        with mock.patch.object(velocity, "WINDOW", 2):
            self.assertEqual(self.facts()["verify"], {"verified": 0, "fixed_forward": 0, "verify_failed": 1, "unchecked": 1})

    def test_page_walks_count_as_started_walked_closed_and_given_back_with_the_repairs_they_gave(self):
        run = self.run_of("r1")
        walk = lambda n, **kw: {"phase": "walk", "n": n, "at": "2026-11-19T05:00:00Z", **kw}
        # T-1: walked and closed. T-2: given back once, then walking now. T-3: done with no walk, closed.
        self.item(run, "T-1", [grade(1, 1, "2026-11-19T01:00:00Z", "PASS"), walk(1)], merged_at="2026-11-19T01:10:00Z")
        self.item(run, "T-2", [grade(1, 1, "2026-11-19T02:00:00Z", "PASS"), walk(1, voided={"why": "no browser"}), walk(2)], merged_at="2026-11-19T02:10:00Z")
        self.item(run, "T-3", [grade(1, 1, "2026-11-19T03:00:00Z", "PASS"), walk(1)], merged_at="2026-11-19T03:10:00Z")
        t1, t2, t3 = (items.item_dir(run, f"T-{n}") for n in (1, 2, 3))
        (t1 / "walk-1.md").write_text("# Walk\n")
        write_json(t1 / "walk-1.worker.json", {"agent": "a", "closed_at": "2026-11-19T06:00:00Z"})
        write_json(t2 / "walk-2.worker.json", {"agent": "b"})
        write_json(t3 / "walk-1.worker.json", {"agent": "c", "closed_at": "2026-11-19T06:30:00Z"})
        # Two repairs from the walk of a file, and one from the contract audit, which is not a walk's.
        for n, what in ((4, "the page walk of T-ITEMS.md"), (5, "The page walk of T-ITEMS.md, its list page"), (6, "the closing audit of T-ITEMS.md")):
            item = items.load_item(run, f"T-{n}")
            item["origin"] = {"kind": "audit", "what": what}
            items.save_item(run, item)
        self.assertEqual(self.facts()["walks"], {"started": 3, "walked": 1, "closed": 2, "voided": 1, "repairs": 2})

    def test_delivered_waits_count_since_the_window_opened_and_those_never_delivered_apart(self):
        run = self.run_of("r1")
        self.item(run, "T-1", [grade(1, 1, "2026-11-19T09:00:00Z", "PASS")], merged_at="2026-11-19T10:00:00Z")
        waits = run / "waits"
        for name in ("20261119T093000Z-1-done.json", "20261119T103000Z-2-done.json", "20261119T110000Z-3-time.json", "20261119T113000Z-4-checks.json", "notes.json"):
            write_json(waits / name, {"event": "done"})
        (waits / "20261119T110000Z-3-time.undelivered").write_text("the manager's pane is blocked\n")
        # Before the window's oldest merge (10:00): not counted. A file not named for its time: not counted.
        self.assertEqual(self.facts()["waits"], {"delivered": 2, "undelivered": 1})

    def test_nothing_merged_yet_gives_nulls_and_reads_no_transcript(self):
        out = self.facts(find=lambda s: self.fail("no transcript is looked for"))
        self.assertEqual(out["window"], {"merged": 0, "since": None})
        self.assertEqual(out["merge_wait_minutes"], {"median": None, "p90": None, "items": 0})
        self.assertIsNone(out["reviews_per_item"])
        self.assertEqual(out["confirm_merges"], {"items": 0, "since": None, "red_checks": 0, "verified": 0, "fixed_forward": 0, "verify_failed": 0, "unchecked": 0})
        self.assertEqual(out["verify"], {"verified": 0, "fixed_forward": 0, "verify_failed": 0, "unchecked": 0})
        self.assertEqual(out["walks"], {"started": 0, "walked": 0, "closed": 0, "voided": 0, "repairs": 0})
        self.assertEqual(out["waits"], {"delivered": 0, "undelivered": 0})
        self.assertNotIn("tokens_per_item", out)


class Tally(VelocityCase):
    def test_a_request_counts_once_with_its_longest_reply_and_a_sidechain_not_at_all(self):
        path = self.transcript(
            "s1",
            [
                assistant("m1", "2026-11-19T10:00:01Z", usage(10, 1000, 0, 5)),
                assistant("m1", "2026-11-19T10:00:02Z", usage(10, 1000, 0, 50), bash=["b1"]),
                '{"type": "user", "timestamp": "2026-11-19T10:00:03Z"}',
                assistant("m2", "2026-11-19T11:00:00Z", usage(20, 100, 1010, 10), bash=["b2", "b3"]),
                assistant("m3", "2026-11-19T11:00:05Z", usage(9, 9, 9000, 9), sidechain=True),
                COMPACTED,
                json.dumps({**json.loads(COMPACTED), "isSidechain": True}),
                "not json",
            ],
        )
        t = velocity.tally(path)
        # m1: 10 + 1250 + 0 + 250 = 1510; m2: 20 + 125 + 101 + 50 = 296.
        self.assertEqual(
            t,
            {
                "model": "claude-sonnet-5-5",
                "turns": 2,
                "peak": 1130,
                "bash": 3,
                "compactions": 1,
                "weighted": 1806,
                "hours": {"2026-11-19T10": [1, 1, 1510], "2026-11-19T11": [1, 2, 296]},
            },
        )


class FromTheTranscripts(VelocityCase):
    def setUp(self):
        super().setUp()
        self.run = self.run_of("r1")
        # Merged at 10:00 and 12:00: the window opens at 10:00.
        self.item(
            self.run,
            "T-1",
            [build(1, "2026-11-19T09:00:00Z", session="old-build"), grade(1, 1, "2026-11-19T09:40:00Z", "PASS", session="old-grade")],
            merged_at="2026-11-19T10:00:00Z",
        )
        self.item(
            self.run,
            "T-2",
            [
                build(1, "2026-11-19T10:05:00Z", session="b1"),
                grade(1, 1, "2026-11-19T10:40:00Z", "FAIL", session="g1"),
                build(2, "2026-11-19T11:00:00Z", session="b2"),
                grade(2, 2, "2026-11-19T11:30:00Z", "PASS", session="g2"),
            ],
            merged_at="2026-11-19T12:00:00Z",
        )
        self.transcript("old-build", [assistant("x", "2026-11-19T09:10:00Z", usage(out=1000))])
        self.transcript("b1", [assistant("a", "2026-11-19T10:10:00Z", usage(100, 0, 0, 0)), assistant("b", "2026-11-19T10:20:00Z", usage(300, 0, 0, 0))])
        self.transcript("b2", [assistant("a", "2026-11-19T11:10:00Z", usage(200, 0, 0, 0))])
        self.transcript("g1", [assistant("a", "2026-11-19T10:45:00Z", usage(50, 0, 0, 0))])
        # g2's own transcript and a sub-agent beside it.
        self.transcript("g2", [assistant("a", "2026-11-19T11:35:00Z", usage(50, 0, 0, 0))])
        self.transcript("agent-1", [assistant("a", "2026-11-19T11:36:00Z", usage(100, 0, 0, 0))], folder=self.transcripts / "g2" / "subagents")
        # The manager: Opus with commands, an hour of it before the window; a Sonnet session it started; its sub-agent.
        self.transcript(
            "mgr",
            [
                assistant("m0", "2026-11-19T09:30:00Z", usage(1000, 0, 0, 0), model="claude-opus-5-5", bash=["c0"]),
                assistant("m1", "2026-11-19T10:30:00Z", usage(400, 0, 0, 0), model="claude-opus-5-5", bash=["c1", "c2"]),
                assistant("m2", "2026-11-19T11:30:00Z", usage(400, 0, 0, 0), model="claude-opus-5-5", bash=["c3"]),
            ],
            folder=self.manager,
        )
        self.transcript("side", [assistant("s", "2026-11-19T10:50:00Z", usage(200, 0, 0, 0), bash=["d1"])], folder=self.manager)
        self.transcript("agent-2", [assistant("a", "2026-11-19T10:55:00Z", usage(100, 0, 0, 0), bash=["d2"])], folder=self.manager / "mgr" / "subagents")
        # A worker session started in the root: counted once, as the worker it is.
        self.transcript("g1", [assistant("a", "2026-11-19T10:45:00Z", usage(50, 0, 0, 0))], folder=self.manager)
        # Files written before the window opened are not read at all.
        old = self.transcript("before", [assistant("z", "2026-11-19T08:00:00Z", usage(9999, 0, 0, 0), model="claude-opus-5-5")], folder=self.manager)
        os.utime(old, (velocity._epoch("2026-11-19T08:00:00Z"),) * 2)

    def test_tokens_turns_and_commands_since_the_window_opened_per_merged_item(self):
        out = self.facts()
        # Builds since 10:00: b1 (peak 300, 2 turns) and b2 (200, 1).
        self.assertEqual(out["builds"], {"sessions": 2, "peak_context_p90": 300, "turns_median": 1.5, "compacted": 0.0, "compactions_max": 0})
        # Workers: b1 400 + b2 200 + g1 50 + g2 50 + its sub-agent 100 = 800, over 2 merged items.
        # Manager from 10:00: Opus 800, the Sonnet session 200, the sub-agent 100 = 1100.
        self.assertEqual(out["tokens_per_item"], {"workers": 400, "manager": 550, "total": 950, "worker_sessions": 4})
        # Commands and turns are the manager model's own: c1, c2, c3 and m1, m2.
        self.assertEqual(out["manager_per_item"], {"commands": 1.5, "turns": 1.0})
        self.assertEqual((out["unread"], out["errors"]), (0, []))

    def test_builds_that_compacted_are_counted(self):
        # b2 compacted twice (`builder_autocompact`); b1 never did. A review that compacts is not a build.
        self.transcript("b2", [assistant("a", "2026-11-19T11:10:00Z", usage(200, 0, 0, 0)), COMPACTED, assistant("b", "2026-11-19T11:20:00Z", usage(90, 0, 0, 0)), COMPACTED])
        self.transcript("g1", [assistant("a", "2026-11-19T10:45:00Z", usage(50, 0, 0, 0)), COMPACTED])
        builds = self.facts()["builds"]
        self.assertEqual((builds["compacted"], builds["compactions_max"]), (0.5, 2))

    def test_a_transcript_is_read_once_and_again_only_when_it_changed(self):
        self.facts()
        kept = read_json(self.root / velocity.CACHE)
        self.assertEqual(kept["version"], velocity.VERSION)
        self.assertIn("session:b1", kept["sessions"])
        self.assertFalse(any("before.jsonl" in k for k in kept["sessions"]))
        with mock.patch.object(velocity, "tally", wraps=velocity.tally) as read, mock.patch.object(self, "find", wraps=self.find) as found:
            self.facts(find=found)
            self.assertEqual((read.call_count, found.call_count), (0, 0))
            with open(self.transcripts / "b2.jsonl", "a") as fh:
                fh.write(assistant("b", "2026-11-19T11:20:00Z", usage(500, 0, 0, 0)) + "\n")
            out = self.facts(find=found)
            self.assertEqual([c.args[0].name for c in read.call_args_list], ["b2.jsonl"])
        self.assertEqual(out["builds"]["peak_context_p90"], 500)

    def test_a_read_past_its_budget_counts_what_it_left_and_uses_what_was_kept(self):
        self.facts()
        with open(self.transcripts / "b2.jsonl", "a") as fh:
            fh.write(assistant("b", "2026-11-19T11:20:00Z", usage(500, 0, 0, 0)) + "\n")
        # The budget is spent as soon as the read starts.
        ticks = iter([0.0])
        out = self.facts(clock=lambda: next(ticks, 10.0**6))
        # Every file is checked; only b2 changed, and it is not read again: its kept tally stands.
        self.assertEqual((out["unread"], out["builds"]["peak_context_p90"]), (1, 300))
        self.assertEqual(self.facts()["builds"]["peak_context_p90"], 500)

    def test_a_kept_file_that_cannot_be_read_is_read_again_whole(self):
        (self.root / velocity.CACHE).write_text("{not json")
        out = self.facts()
        self.assertEqual((out["errors"], out["tokens_per_item"]["workers"]), ([], 400))

    def test_a_session_named_twice_counts_once_and_a_later_sub_agent_is_found_without_a_search(self):
        self.assertEqual(self.facts()["tokens_per_item"]["workers"], 400)
        # b1 resumed: the same session in a second entry, and a sub-agent it started since.
        attempts = items.item_dir(self.run, "T-2") / "attempts.json"
        log = read_json(attempts)["log"]
        write_json(attempts, {"log": log + [{**log[0], "at": "2026-11-19T10:30:00Z"}]})
        self.transcript("agent-9", [assistant("a", "2026-11-19T10:31:00Z", usage(200, 0, 0, 0))], folder=self.transcripts / "b1" / "subagents")
        with mock.patch.object(self, "find", wraps=self.find) as found:
            out = self.facts(find=found)
        self.assertEqual(found.call_count, 0)
        # 800 + 200, over 2 merged items; b1 is one build.
        self.assertEqual((out["tokens_per_item"]["workers"], out["tokens_per_item"]["worker_sessions"], out["builds"]["sessions"]), (500, 4, 2))

    def test_a_session_with_no_transcript_is_kept_as_missing_only_once_it_is_old(self):
        self.item(self.run, "T-3", [build(1, "2026-11-19T11:58:00Z", session="young"), build(2, "2026-11-19T10:30:00Z", session="gone")])
        self.facts()
        kept = read_json(self.root / velocity.CACHE)["sessions"]
        self.assertEqual(kept.get("session:gone"), {"missing": True})
        self.assertNotIn("session:young", kept)

    def test_no_manager_transcripts_found_says_null_not_zero(self):
        out = self.facts(manager_dir=None)
        self.assertEqual((out["tokens_per_item"]["manager"], out["tokens_per_item"]["total"], out["manager_per_item"]), (None, None, None))
        self.assertEqual(out["tokens_per_item"]["workers"], 400)


class NeverFailsASave(VelocityCase):
    def test_a_program_that_cannot_be_read_or_a_read_that_breaks_is_said(self):
        (self.root / "program.json").write_text("{")
        self.assertEqual(list(velocity.facts_or_said(self.root)), ["errors"])
        write_json(self.root / "program.json", PROGRAM)
        with mock.patch.object(velocity, "facts", side_effect=RuntimeError("disk gone")):
            self.assertEqual(velocity.facts_or_said(self.root), {"errors": ["could not be read: disk gone"]})



if __name__ == "__main__":
    unittest.main()
