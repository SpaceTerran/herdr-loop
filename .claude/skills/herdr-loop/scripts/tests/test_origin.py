"""Where an item came from (`**Origin.**`): what the parser reads and refuses, what a run records of it, and what a
save counts of it (`velocity.origins`): the window's items by origin, and the repairs and audit findings written
since the window opened, per 100 merged items."""

from __future__ import annotations

from hl import backlog, items, program
from hl.common import Refusal, read_json, write_json

from . import test_velocity


def section(item_id, origin=None):
    line = f"\n\n{origin}" if origin else ""
    return f"## {item_id} — Item {item_id}\n\n**Scope.** A thing.\n\n**Accept.**\n1. It works.{line}\n"


def parse_one(origin):
    return backlog.parse_backlog(section("O-1", origin))[0].origin


class OriginParseTest(test_velocity.unittest.TestCase):
    def test_each_kind_is_read_with_what_it_names(self):
        self.assertEqual(parse_one("**Origin.** new"), {"kind": "new", "what": None})
        self.assertEqual(parse_one("**Origin.** New."), {"kind": "new", "what": None})
        self.assertEqual(parse_one("**Origin.** new: the catalog's next page"), {"kind": "new", "what": "the catalog's next page"})
        self.assertEqual(parse_one("**Origin.** repair: HD-1102, its empty state"), {"kind": "repair", "what": "HD-1102, its empty state"})
        self.assertEqual(parse_one("**Origin.** Audit: the closing audit of HD-ITEMS-41.md"), {"kind": "audit", "what": "the closing audit of HD-ITEMS-41.md"})
        self.assertEqual(parse_one("**Origin.** rule: Decision 6"), {"kind": "rule", "what": "Decision 6"})

    def test_an_item_without_the_line_has_no_origin_on_record(self):
        self.assertIsNone(parse_one(None))

    def test_a_line_that_does_not_say_where_by_the_rules_is_refused_by_name(self):
        for line in (
            "**Origin.**",
            "**Origin.** fix: HD-1",
            "**Origin.** repair",
            "**Origin.** repair: the button on the plans page",
            "**Origin.** audit",
            "**Origin.** audit:",
            "**Origin.** rule: ",
            "**Origin.** new work",
        ):
            with self.assertRaisesRegex(backlog.BacklogError, r"O-1: malformed \*\*Origin\.\*\* line", msg=line):
                parse_one(line)

    def test_a_run_records_each_items_origin(self):
        with test_velocity.tempfile.TemporaryDirectory() as tmp:
            path = test_velocity.Path(tmp) / "O-ITEMS.md"
            path.write_text(section("O-1", "**Origin.** repair: HD-7") + "\n" + section("O-2"))
            run_dir = test_velocity.Path(tmp) / "runs" / "r1"
            items.init_run(run_dir, "r1", "owner/repo", [str(path)])
            self.assertEqual(items.load_item(run_dir, "O-1")["origin"], {"kind": "repair", "what": "HD-7"})
            self.assertIsNone(items.load_item(run_dir, "O-2")["origin"])


class RequiredOriginTest(test_velocity.unittest.TestCase):
    def setUp(self):
        self._tmp = test_velocity.tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = test_velocity.Path(self._tmp.name)

    def file(self, name, *origins):
        path = self.tmp / f"{name}-ITEMS.md"
        path.write_text("\n---\n\n".join(section(f"{name}-{n}", origin) for n, origin in enumerate(origins, start=1)))
        return str(path)

    def policy(self, **settings):
        return program.run_policy({**program.DEFAULTS, **settings}, "owner/repo")

    def test_a_program_that_says_nothing_requires_no_origin(self):
        self.assertNotIn("item_origin", self.policy())
        run_dir = self.tmp / "runs" / "r1"
        items.init_run(run_dir, "r1", "owner/repo", [self.file("O", None)], policy=self.policy())
        self.assertIsNone(items.load_item(run_dir, "O-1")["origin"])

    def test_required_refuses_a_file_with_an_item_that_says_nothing_and_writes_nothing(self):
        policy = self.policy(item_origin="required")
        self.assertEqual(policy["item_origin"], "required")
        run_dir = self.tmp / "runs" / "r1"
        with self.assertRaises(Refusal) as caught:
            items.init_run(run_dir, "r1", "owner/repo", [self.file("O", "**Origin.** new", None, None)], policy=policy)
        said = str(caught.exception)
        self.assertIn("`item_origin: required`", said)
        self.assertIn("O-2, O-3", said)
        self.assertNotIn("O-1", said)
        self.assertFalse((run_dir / "run.json").exists())
        self.assertFalse((run_dir / "items").exists())

    def test_required_takes_a_file_whose_items_all_say_and_the_run_keeps_the_setting(self):
        run_dir = self.tmp / "runs" / "r1"
        run = items.init_run(run_dir, "r1", "owner/repo", [self.file("O", "**Origin.** new", "**Origin.** rule: Decision 7")], policy=self.policy(item_origin="required"))
        self.assertEqual(items.option(run, "item_origin"), "required")
        self.assertEqual(items.options_on(run), {"item_origin": "required"})

    def test_a_run_made_before_the_setting_is_taken_up_again_as_it_was(self):
        path = self.file("O", None)
        run_dir = self.tmp / "runs" / "r1"
        items.init_run(run_dir, "r1", "owner/repo", [path], policy=self.policy())
        run = items.init_run(run_dir, "r1", "owner/repo", [path], policy=self.policy(item_origin="required"))
        self.assertEqual(items.option(run, "item_origin"), "optional")

    def test_a_value_that_is_not_one_of_the_two_is_refused(self):
        with self.assertRaisesRegex(Refusal, r"item_origin 'always' is not one of optional, required"):
            items.check_policy({"item_origin": "always"})


class OriginFactsTest(test_velocity.VelocityCase):
    def run_with(self, name, created, origins):
        """A run made at `created` from a file whose items say `origins` (None: no line), in order."""
        path = self.root / f"{name}-ITEMS.md"
        path.write_text("\n---\n\n".join(section(f"{name.upper()}-{n}", origin) for n, origin in enumerate(origins, start=1)))
        run_dir = self.root / "runs" / name
        items.init_run(run_dir, name, "owner/repo", [str(path)])
        run = read_json(run_dir / "run.json")
        write_json(run_dir / "run.json", {**run, "created": created})
        return run_dir

    def merge(self, run_dir, item_id, at):
        self.item(run_dir, item_id, [test_velocity.grade(1, 1, at, "PASS")], merged_at=at)

    def test_nothing_merged_is_nothing_measured(self):
        out = self.facts()["origins"]
        self.assertEqual(out["merged"], {"new": 0, "repair": 0, "audit": 0, "rule": 0, "unrecorded": 0})
        self.assertIsNone(out["repair_share"])
        self.assertIsNone(out["found_per_100_merged"])
        self.assertEqual(out["repaired"], {"items": 0, "by": {}})

    def test_the_windows_items_are_counted_by_origin_and_the_repairs_found_since_per_100_merged(self):
        old = self.run_with("a", "2026-11-18T08:00:00Z", ["**Origin.** new", "**Origin.** repair: HD-1", None, "**Origin.** new"])
        for n, at in enumerate(("2026-11-19T09:00:00Z", "2026-11-19T09:10:00Z", "2026-11-19T09:20:00Z", "2026-11-19T09:30:00Z"), start=1):
            self.merge(old, f"A-{n}", at)
        # Written after the window's oldest merge: one repair, one audit finding, one new item, one that says nothing.
        self.run_with("b", "2026-11-19T09:05:00Z", ["**Origin.** repair: A-1", "**Origin.** audit: the closing audit of a-ITEMS.md"])
        self.run_with("c", "2026-11-19T09:40:00Z", ["**Origin.** new", None])
        # Written before it opened: not counted as found since.
        self.run_with("d", "2026-11-19T08:00:00Z", ["**Origin.** repair: A-2"])
        out = self.facts()["origins"]
        self.assertEqual(out["merged"], {"new": 2, "repair": 1, "audit": 0, "rule": 0, "unrecorded": 1})
        self.assertEqual(out["repair_share"], 0.33)
        self.assertEqual(out["written"], {"items": 4, "unrecorded": 1, "repair": 1, "audit": 1})
        self.assertEqual(out["found_per_100_merged"], 50.0)

    def test_the_windows_items_a_repair_names_are_listed_with_the_repairs_that_name_them(self):
        old = self.run_with("a", "2026-11-18T08:00:00Z", ["**Origin.** new", "**Origin.** new", "**Origin.** new"])
        self.merge(old, "A-1", "2026-11-19T09:00:00Z")
        self.merge(old, "A-2", "2026-11-19T09:10:00Z")
        # A-3 is not merged: a repair that names it is not one of the window's.
        self.run_with("b", "2026-11-19T09:30:00Z", ["**Origin.** repair: A-2, its empty state", "**Origin.** repair: A-2 and A-1", "**Origin.** repair: A-3"])
        self.run_with("c", "2026-11-19T09:40:00Z", ["**Origin.** audit: the closing audit of A-1", "**Origin.** new"])
        self.assertEqual(self.facts()["origins"]["repaired"], {"items": 2, "by": {"A-1": ["B-2"], "A-2": ["B-1", "B-2"]}})

    def test_a_program_that_writes_no_origins_reads_as_unmeasured_not_as_clean(self):
        old = self.run_with("a", "2026-11-18T08:00:00Z", [None, None])
        self.merge(old, "A-1", "2026-11-19T09:00:00Z")
        self.run_with("b", "2026-11-19T10:00:00Z", [None])
        out = self.facts()["origins"]
        self.assertEqual(out["merged"]["unrecorded"], 1)
        self.assertIsNone(out["repair_share"])
        self.assertEqual(out["written"], {"items": 1, "unrecorded": 1, "repair": 0, "audit": 0})
        self.assertIsNone(out["found_per_100_merged"])

    def test_an_item_recorded_before_origins_existed_reads_as_unrecorded(self):
        old = self.run_with("a", "2026-11-18T08:00:00Z", ["**Origin.** new"])
        item = items.load_item(old, "A-1")
        item.pop("origin")
        items.save_item(old, item)
        self.merge(old, "A-1", "2026-11-19T09:00:00Z")
        self.assertEqual(self.facts()["origins"]["merged"]["unrecorded"], 1)


if __name__ == "__main__":
    test_velocity.unittest.main()
