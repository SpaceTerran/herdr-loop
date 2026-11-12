"""Finding a wait on a clock, a reading or another run's end: in text, in a
backlog file, in the ledger's Gates table, and across a program's open runs.
The texts are the ones that held the help-desk catalog program's streams on
2026-11-10, and the product wording around them that must not be taken for a wait."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hl import clocks, items

SCRIPTS = Path(__file__).resolve().parents[1]

# HD-LAMBDA-LOCK-ITEMS.md, HD-N47-*-ITEMS.md and HD-418 as the manager wrote them on 2026-11-09 and 11-10.
HELD_HEADER = """# Help desk: fixes noted at an operator note — hd-n47-fix

The run is itself held until run `hd-lambda-lock` closes (HD-418 is not merged at `f1e2d3c`).
HD-478 also waits for HD-418's drain reading (2026-11-10 about 16:42Z).
"""
HD_418 = """## HD-418 — If the Renovate queue does not drain in a day, the help desk raises its own pull-request limits

**Scope.** Whether this item is built at all is G-RENOVATE-QUEUE's to say.

**Accept.**
1. `jq .prConcurrentLimit renovate.json` prints `10`.
"""
# Product wording from the same program's items: a drain that is a feature, a lifetime, a reminder's timing.
PRODUCT = """## HD-361 — The search-index drain takes a bounded share of a turn

**Scope.** The drain never cuts a turn. While a backlog drains, `jobs` runs only in a turn the drain did not cut.
A token is valid for 24 hours. The reminder goes once, 3 days after the send back, and the auto-resolve 7 days after.
See `docs/ops/invoke-dlq-drain.md`. Built once `HD-63` is complete.

**Accept.**
1. `grep -c 'drain' docs/desk/jobs.md` prints `4`.

**Verify.** `curl -fsS https://desk.example.invalid/health --max-time 24`
Wait. 60
"""


class FoundTest(unittest.TestCase):
    def kinds(self, text, kinds=clocks.ALL):
        return [kind for kind, _ in clocks.found(text, kinds)]

    def test_the_waits_that_held_the_help_desk_catalog_are_found(self):
        self.assertEqual(self.kinds(HELD_HEADER), ["edge", "wait"])
        self.assertEqual(self.kinds(HD_418), ["wait"])
        self.assertEqual(self.kinds("5. **The drain reading, 24 hours after the first Renovate run after HD-417**"), ["wait", "clock"])

    def test_the_operators_examples_are_each_found(self):
        # Two operator notes, and the kit change they asked for.
        for text, kind in (
            ("read the queue again after 24 h", "clock"),
            ("the count, a day later", "clock"),
            ("G-ID-RETIRE: 7 days after it", "clock"),
            ("next read 11-11", "wait"),
            ("Next read ~2026-11-03T22:20Z", "wait"),
            ("the apply at T+24h", "wait"),
            ("merge after tonight's backup", "wait"),
            ("a 48 h soak", "wait"),
            ("the old pool stays until 11-15T20:20Z", "wait"),
            ("whether the queue then drains", "wait"),
            ("if the backlog hasn't cleared", "wait"),
            ("`app-console` is made when `app-backend` is complete", "edge"),
            ("after run hd-engine has completed", "edge"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.kinds(text), [kind])

    def test_a_products_own_drain_lifetimes_and_an_items_dependency_are_not_waits(self):
        self.assertEqual(clocks.found("The search-index drain takes a bounded share. A token is valid for 24 hours."), [])
        self.assertEqual(clocks.found("See `docs/ops/invoke-dlq-drain.md`. Built once `HD-63` is complete."), [])
        # A length of time is the product's timing as often as a wait: it is not looked for in an item's text.
        self.assertEqual(self.kinds("The reminder goes 3 days after the send back.", clocks.IN_ITEMS), [])
        self.assertEqual(self.kinds("The reminder goes 3 days after the send back."), ["clock"])

    def test_each_place_is_said_once_with_its_line_around_it(self):
        line = "x " * 80 + "HD-478 also waits for HD-418's drain reading" + " y" * 80
        (kind, says), = clocks.found(line + "\nand a second line")
        self.assertEqual(kind, "wait")
        self.assertIn("drain reading", says)
        self.assertTrue(says.startswith("…") and says.endswith("…") and len(says) <= clocks.SAYS_CHARS + 2)
        self.assertNotIn("second line", says)
        # Two patterns on the same words are one place.
        self.assertEqual(len(clocks.found("If the queue does not drain in a day")), 1)


class BacklogTest(unittest.TestCase):
    def test_a_files_header_and_its_items_are_said_apart(self):
        hits = clocks.backlog_hits(HELD_HEADER + "\n---\n\n" + HD_418 + "\n---\n\n" + PRODUCT, "HD-N47-FIX-ITEMS.md")
        self.assertEqual(
            [(where, kind) for where, kind, _ in hits],
            [("HD-N47-FIX-ITEMS.md header", "edge"), ("HD-N47-FIX-ITEMS.md header", "wait"), ("HD-418", "wait")],
        )

    def test_a_verify_wait_is_a_deadline_from_the_merge_and_never_found(self):
        self.assertEqual(clocks.in_item(PRODUCT), [])
        self.assertEqual(clocks.in_item(PRODUCT.replace("--max-time 24", "&& sleep 86400 # soak")), [])

    def test_a_file_with_no_items_is_all_header_and_one_that_does_not_parse_still_has_its_header_read(self):
        self.assertEqual([w for w, _, _ in clocks.backlog_hits("Merge after tonight.\n", "NOTES.md")], ["NOTES.md header"])
        broken = HELD_HEADER + "\n## X-1 — no scope\n\n**Accept.**\n1. x.\n"
        self.assertEqual([w for w, _, _ in clocks.backlog_hits(broken, "X.md")], ["X.md header", "X.md header"])

    def test_remarks_say_where_what_and_why(self):
        (remark,) = clocks.remarks([("HD-418", "wait", "If the queue does not drain in a day")])
        self.assertEqual(remark, f'HD-418 {clocks.REMARK["wait"]} ("If the queue does not drain in a day")')


LEDGER = """# Ledger

## Now

Next read of the pool at 22:20Z.

## Gates

Conditions between files that no item holds.

| Gate | Before | Result | When | How it was checked |
|---|---|---|---|---|
| G-ID-POOL-DONE (read 1) | HD-226 | **not passed** | 2026-11-01T22:15Z | 4/10 legacy emails in v2. Next read ~2026-11-03T22:20Z; passes at 2026-11-15. |
| G-ID-POOL-DONE | HD-226 | **passed by the operator** | 2026-11-10T15:55Z | An operator note. |
| G-RENOVATE-QUEUE | HD-418 | open | | The drain reading, 24 hours after the first Renovate run. |
| G-SUPERADMIN | HD-60 | stopped before the invite; asked | | No clock here. |
| `G-DNS` | HD-70 | **done**; restore the TTL after 2 days | | |
| broken row with | two cells |

## Decisions

| # | Item | Question |
|---|---|---|
| 1 | HD-1 | Wait 24 h? |
"""


class GatesTest(unittest.TestCase):
    def test_a_gate_still_open_with_a_wait_is_listed_and_one_settled_by_its_last_row_is_not(self):
        self.assertEqual(
            clocks.gate_rows(LEDGER),
            [{"gate": "G-RENOVATE-QUEUE", "result": "open", "kind": "wait", "says": "The drain reading, 24 hours after the first Renovate run."}],
        )

    def test_a_reading_row_left_last_keeps_its_gate_open(self):
        text = LEDGER.replace("| G-ID-POOL-DONE | HD-226 | **passed by the operator** | 2026-11-10T15:55Z | An operator note. |\n", "")
        self.assertEqual([(g["gate"], g["result"], g["kind"]) for g in clocks.gate_rows(text)][0], ("G-ID-POOL-DONE", "not passed", "wait"))

    def test_a_ledger_with_no_gates_table_has_no_gates(self):
        self.assertEqual(clocks.gate_rows("# Ledger\n\n## Now\n\nAfter 24 h.\n"), [])

    def test_the_ledger_is_read_as_utf8_whatever_the_locale(self):
        # Read in a Latin-1 locale's encoding, É (C3 89) is two characters, Ã and a control character that ends a
        # gate's name: the clocks fact would name an open gate G-Ã, which the archive, reading UTF-8, never wrote.
        # ✅ (E2 9C 85) holds U+0085, which must not cut G1's passed row in two either.
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "LEDGER.md"
            ledger.write_bytes(
                "## Gates\n\n| Gate | Before | Result | When | How |\n|---|---|---|---|---|\n"
                "| G1 | HD-1 | open, next reading 11-02 | 11-01 | wait 24h for the soak |\n"
                "| G1 | ✅ HD-1 | passed | 11-02 | read again |\n"
                "| G-ÉTÉ | HD-2 | open, next reading 11-02 | 11-01 | wait 24h for the soak |\n".encode()
            )
            code = "import json, sys; from hl import clocks; print(json.dumps(clocks.program_clocks(sys.argv[1], sys.argv[2])))"
            env = {**os.environ, "LC_ALL": "en_US.ISO8859-1", "LANG": "en_US.ISO8859-1", "PYTHONUTF8": "0"}
            run = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code, tmp, str(ledger)], cwd=SCRIPTS, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            out = json.loads(run.stdout)
            self.assertEqual((out["runs"], out["items"], [g["gate"] for g in out["gates"]]), ([], [], ["G-ÉTÉ"]))


class TableCellsTest(unittest.TestCase):
    def test_a_row_is_split_as_a_person_reads_it(self):
        cases = {
            "| G-A | x | passed | t | y |": ["G-A", "x", "passed", "t", "y"],
            # a gate whose check quoted `grep -E 'x|done'` was read as done
            "| G-A | `grep -E 'x|done'` | open | t |": ["G-A", "`grep -E 'x|done'`", "open", "t"],
            "| G-A | a \\| b | open |": ["G-A", "a \\| b", "open"],
            "| G-A | don`t | open |": ["G-A", "don`t", "open"],
            "| G-A | ``a ` | b`` | open |": ["G-A", "``a ` | b``", "open"],
            "| G-A | b |  |": ["G-A", "b", ""],
            "| G-A | b | no closing pipe": ["G-A", "b", "no closing pipe"],
            # a backtick written `\``, opens no code span
            "| G-A | a \\` b | open | c \\` d |": ["G-A", "a \\` b", "open", "c \\` d"],
        }
        for line, cells in cases.items():
            self.assertEqual(clocks.table_cells(line), cells, line)
        self.assertEqual(clocks.plain_cells("| G-A | `a|b` | c \\| d |"), ["G-A", "`a", "b`", "c \\| d"])

    def test_an_open_gate_with_a_pipe_in_its_code_stays_open(self):
        text = "## Gates\n\n| Gate | Before | Result | When | How |\n|---|---|---|---|---|\n| G-OPEN | `a|done` | open | | The drain reading, 24 hours after the first run. |\n"
        self.assertEqual([g["gate"] for g in clocks.gate_rows(text)], ["G-OPEN"])

    def test_a_row_that_reads_two_ways_is_never_settled_and_never_left_out(self):
        header = ["| Gate | Before | Result | When | How |", "|---|---|---|---|---|"]
        # A backtick left open would join the rest of the row into one cell. Split at every `|`, the row has the
        # header's five cells and says open: the earlier passed row does not settle the gate.
        stray = clocks.gate_table(header + ["| G-A | x | passed | t | y |", "| G-A | after `hd-x merges | open | t | ran `grep x` |"])
        self.assertEqual(clocks.gate_result(stray["G-A"]), "open")
        self.assertFalse(clocks.gate_settled(stray["G-A"]))
        # The two readings disagree on whether it is settled, or the row has two cells: it cannot be said. Taking
        # the reading with the header's five cells would read the third row as settled: split wrongly, it has five.
        for row in ("| G-B | `x|y` | passed |", "| G-B | passed |", "| G-B | `grep -cE 'IDX|deleted' log` reads 0 | open | t |"):
            odd = clocks.gate_table(header + ["| G-B | x | passed | t | y |", row])
            self.assertEqual(len(odd["G-B"]), 2, row)
            self.assertTrue(clocks.gate_unclear(odd["G-B"]), row)
            self.assertFalse(clocks.gate_settled(odd["G-B"]), row)
        # Rows written with fewer or more cells than the header, read alike both ways, are read as they are.
        plain = clocks.gate_table(header + ["| G-C | x | passed |", "| G-D | x | passed | t | y | z |"])
        self.assertTrue(clocks.gate_settled(plain["G-C"]) and clocks.gate_settled(plain["G-D"]))
        # A two-column legend beside the table names no gate: no row of its words has three cells.
        legend = clocks.gate_table(["| Word | Meaning |", "|---|---|", "| passed | done and read |", ""] + header + ["| G-E | x | open | t | y |"])
        self.assertEqual(list(legend), ["G-E"])
        # An unclear row keeps its text after the marker: a wait it names is still found.
        waits = clocks.gate_rows("## Gates\n\n" + "\n".join(header + ["| G-W | `a|soak 2 days` | passed | t | y |"]) + "\n")
        self.assertEqual([(g["gate"], g["kind"]) for g in waits], [("G-W", "wait")])


class ProgramClocksTest(unittest.TestCase):
    """The `clocks` fact across a loop root's runs and its ledger."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "root"
        self.held = self.tmp / "HD-N47-FIX-ITEMS.md"
        self.held.write_text(HELD_HEADER + "\n---\n\n" + HD_418 + "\n---\n\n" + PRODUCT)
        self.run_dir = self.root / "runs" / "hd-n47-fix"
        items.init_run(self.run_dir, "hd-n47-fix", "owner/desk", [str(self.held)])
        self.ledger = self.tmp / "LEDGER.md"
        self.ledger.write_text(LEDGER)

    def status(self, item_id, status):
        item = items.load_item(self.run_dir, item_id)
        item["status"] = status
        items.save_item(self.run_dir, item)

    def test_an_open_runs_header_its_items_and_the_open_gates_are_named(self):
        out = clocks.program_clocks(self.root, self.ledger)
        (run,) = out["runs"]
        self.assertEqual((run["run"], run["file"], run["kind"]), ("hd-n47-fix", "HD-N47-FIX-ITEMS.md", "edge"))
        self.assertTrue(run["says"].startswith("The run is itself held until run `hd-lambda-lock` closes"))
        self.assertEqual([(i["item"], i["kind"]) for i in out["items"]], [("HD-418", "wait")])
        self.assertEqual([g["gate"] for g in out["gates"]], ["G-RENOVATE-QUEUE"])
        self.assertNotIn("errors", out)

    def test_a_parked_item_is_still_named_and_a_run_with_nothing_left_is_not(self):
        self.status("HD-418", "parked")
        self.assertEqual([i["item"] for i in clocks.program_clocks(self.root, None)["items"]], ["HD-418"])
        self.status("HD-418", "merged")
        self.status("HD-361", "skipped")
        self.assertEqual(clocks.program_clocks(self.root, None), {"runs": [], "items": [], "gates": []})

    def test_the_items_text_is_read_as_the_run_froze_it(self):
        # A run's items never change once it is made; the file beside it may, and its header is read from there.
        self.held.write_text(PRODUCT)
        out = clocks.program_clocks(self.root, None)
        self.assertEqual(([r["run"] for r in out["runs"]], [i["item"] for i in out["items"]]), ([], ["HD-418"]))

    def test_a_read_that_fails_is_said_and_the_rest_is_still_read(self):
        self.held.unlink()
        self.ledger.unlink()
        out = clocks.program_clocks(self.root, self.ledger)
        self.assertEqual([i["item"] for i in out["items"]], ["HD-418"])
        self.assertEqual(len(out["errors"]), 2)
        self.assertTrue(out["errors"][0].startswith("hd-n47-fix: ") and out["errors"][1].startswith("the ledger: "))


if __name__ == "__main__":
    unittest.main()
