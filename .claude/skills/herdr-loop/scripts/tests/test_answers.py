"""What the manager read by hand, now in the answers it already gets (the velocity review's change 8): an item's
Scope and markers in `hl-items next`, `hl-items show` and `hl-flow prepare`; the files an item holds in
`hl-items show`; the word a premise check or a correction ends on in `hl-wait`'s done event; and the ledger's
headings and Now in `hl-claim check`."""

from __future__ import annotations

import contextlib
import io
import json
from unittest import mock

from hl import backlog, claim, items
from hl.common import write_json

from . import support, test_wait

ITEM = """## HD-1 — An item

**Scope.** What is there now.
It goes on.

It becomes. Two.

This item may write: `a.py`, `b.py`.

**Accept.** Every command runs from the root.
1. One.

**Depends.** none.

**Floor.** sonnet: because.

**Verify.** `true`
Wait. 60

**Size.** M (carried).
"""


def cli(main, *argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, json.loads(out.getvalue())


class ItemFieldsTest(support.unittest.TestCase):
    def test_the_scope_is_read_whole_and_each_other_marker_by_its_name(self):
        fields = backlog.item_fields(ITEM)
        self.assertEqual(fields["scope"], "What is there now.\nIt goes on.\n\nIt becomes. Two.\n\nThis item may write: `a.py`, `b.py`.")
        self.assertEqual(fields["markers"], {"Depends": "none.", "Floor": "sonnet: because.", "Size": "M (carried)."})

    def test_an_item_with_only_the_required_markers_has_no_others(self):
        self.assertEqual(backlog.item_fields("## X-1 — x\n\n**Scope.** s\n\n**Accept.**\n1. a\n"), {"scope": "s", "markers": {}})


class ItemAnswersTest(support.RunCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_next_names_the_item_with_its_scope_and_markers(self):
        code, said = cli(items.main, "--run-dir", str(self.run_dir), "next")
        self.assertEqual((code, said["state"], said["next"]), (0, "ready", "A-1"))
        self.assertEqual(said["item"]["id"], "A-1")
        self.assertEqual(said["item"]["title"], "First item")
        self.assertEqual(said["item"]["scope"], "Build the first thing.\n\nIt has a second paragraph.")
        self.assertEqual(said["item"]["markers"], {})

    def test_show_gives_the_text_and_the_files_the_item_holds(self):
        self.write("A-1", "correction-1.md", "STANDS\n")
        code, said = cli(items.main, "--run-dir", str(self.run_dir), "show", "--id", "A-1")
        self.assertEqual(code, 0)
        self.assertEqual(said["text"]["scope"], "Build the first thing.\n\nIt has a second paragraph.")
        self.assertIn("correction-1.md", said["files"])
        self.assertIn("item.md", said["files"])
        self.assertEqual(said["files"], sorted(said["files"]))

    def test_a_run_with_nothing_ready_names_no_item(self):
        for item_id in items.load_run(self.run_dir)["order"]:
            item = items.load_item(self.run_dir, item_id)
            items.save_item(self.run_dir, {**item, "status": "merged"})
        code, said = cli(items.main, "--run-dir", str(self.run_dir), "next")
        self.assertEqual((code, said["state"], said["next"]), (0, "complete", None))
        self.assertNotIn("item", said)


class RulingTest(test_wait.WaitCase):
    def done(self, phase, n, text):
        self.worker(phase=phase, n=n)
        self.write("A-1", f"{phase}-{n}.md", text)
        self.write("A-1", f"{phase}-{n}.done", "done\n")
        return self.wait(test_wait.ScriptedHerdr(test_wait.info()), (f"A-1:{phase}-{n}",))

    def test_a_premise_check_that_revised_says_so_and_how_to_install_it(self):
        event = self.done("agree", 2, "Criterion 2 is stale.\n\n1. One.\n\nREVISE\n")
        self.assertEqual((event["event"], event["word"]), ("done", "REVISE"))
        self.assertEqual(event["result"], str(self.item_dir() / "agree-2.md"))
        self.assertEqual(event["next"], f"hl-items --run-dir {self.run_dir} contract --id A-1 --from-amended agree-2.md")

    def test_a_premise_check_that_agreed_says_so_and_needs_nothing_installed(self):
        event = self.done("agree", 2, "Every criterion holds.\n\nAGREED\n")
        self.assertEqual(event["word"], "AGREED")
        self.assertNotIn("next", event)

    def test_a_correction_says_its_word(self):
        event = self.done("correction", 1, "Criterion 2 is right.\n\nSTANDS\n")
        self.assertEqual(event["word"], "STANDS")

    def test_a_result_with_no_final_word_says_null(self):
        self.assertIsNone(self.done("agree", 2, "I ran out of time.\n")["word"])

    def test_a_build_is_done_as_before(self):
        self.worker()
        self.write("A-1", "build-1.done", "done\n")
        event = self.wait(test_wait.ScriptedHerdr(test_wait.info()))
        self.assertEqual(sorted(event), sorted(["event", "item", "phase", "n", "agent", "file", "waited_s"]))


class LedgerMapTest(support.unittest.TestCase):
    def setUp(self):
        self._tmp = support.tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = support.Path(self._tmp.name)
        self.ledger = self.root / "LEDGER.md"

    def test_the_headings_and_now_are_given(self):
        write_json(self.root / "program.json", {"ledger": str(self.ledger)})
        self.ledger.write_text("# Ledger: p\n\n## Now (rewrite this section)\n\n- Stream a: HD-9 in review.\n- Stream b: idle.\n\n## Decisions\n\n| 1 | x |\n")
        said = claim.ledger_map(self.root)
        self.assertEqual(said["headings"], [[3, "## Now (rewrite this section)"], [8, "## Decisions"]])
        self.assertEqual(said["now"], "- Stream a: HD-9 in review.\n- Stream b: idle.")
        self.assertEqual(said["file"], str(self.ledger))

    def test_a_ledger_without_now_has_none(self):
        write_json(self.root / "program.json", {"ledger": str(self.ledger)})
        self.ledger.write_text("# Ledger\n\n## Decisions\n")
        self.assertIsNone(claim.ledger_map(self.root)["now"])

    def test_a_ledger_that_cannot_be_read_is_said_and_the_check_still_answers(self):
        self.assertIn("error", claim.ledger_map(self.root))
        write_json(self.root / "program.json", {"ledger": str(self.root / "gone.md")})
        self.assertIn("could not be read", claim.ledger_map(self.root)["error"])
        with mock.patch("hl.claim.check_pins", return_value={"ok": True, "bins": {}, "updated": []}), mock.patch("hl.claim.emit") as emitted:
            code = claim.main(["--root", str(self.root), "check"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(emitted.call_args[0][0]), ["bins", "ledger", "ok", "updated"])


if __name__ == "__main__":
    support.unittest.main()
