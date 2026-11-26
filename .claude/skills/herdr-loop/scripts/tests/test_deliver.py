"""A wait's event as the manager's next message (the velocity review's change 8, its second part): `hl-wait --deliver`
writes the whole event to a file and types one short line into the manager's pane, and `wait_delivery: prompt`
tells the manager to wait that way. Without either, waits are as they were."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from hl import spawn, stage, wait
from hl.common import read_json, write_json

from . import support, test_stage, test_wait

MANAGER = "canary-mgr-2"


def done_event(run_dir, **extra):
    return {
        "event": "done",
        "item": "A-1",
        "phase": "agree",
        "n": 2,
        "agent": "canary-a1-a2",
        "file": f"{run_dir}/items/A-1/agree-2.done",
        "result": f"{run_dir}/items/A-1/agree-2.md",
        "word": "REVISE",
        "next": f"hl-items --run-dir {run_dir} contract --id A-1 --from-amended agree-2.md",
        "waited_s": 412,
        **extra,
    }


class EventLineTest(support.unittest.TestCase):
    def test_the_line_says_what_happened_and_names_the_file_that_holds_it_whole(self):
        whole = Path("/loop/runs/r/waits/20261119T220000Z-1-done.json")
        line = wait.event_line(done_event("/loop/runs/r"), whole)
        self.assertTrue(line.startswith('hl-wait {"event":"done","item":"A-1","phase":"agree","n":2,'))
        self.assertIn('"word":"REVISE"', line)
        self.assertIn('"next":"in the file"', line)
        for left_out in ("canary-a1-a2", "agree-2.done", "agree-2.md", "--from-amended"):
            self.assertNotIn(left_out, line)
        self.assertTrue(line.endswith(f" (whole: {whole})"))
        self.assertLessEqual(len(line), spawn.MAX_PROMPT)

    def test_checks_are_named_with_their_bucket_and_only_those_not_green(self):
        event = {"event": "checks", "item": "A-1", "pr": 7, "passed": False, "head_sha": "abc", "checks_file": "/x/checks-1.md", "checks": [
            {"name": "full-ci", "bucket": "fail"}, {"name": "secret-scan", "bucket": "pass"}, {"name": "Platform CI", "bucket": "skipping"}, {"name": "ownership-guard", "bucket": "cancel"},
        ]}
        said = json.loads(wait.event_line(event, Path("/w.json"))[len("hl-wait "):].rsplit(" (whole: ", 1)[0])
        self.assertEqual(said["checks"], ["full-ci=fail", "ownership-guard=cancel"])
        self.assertNotIn("checks_file", said)

    def test_a_long_event_is_cut_to_fit_and_still_ends_with_its_file(self):
        event = {"event": "error", "source": "gh", "detail": "x" * 2000, "item": "A-1", "checks": [{"name": f"check-{k}", "bucket": "fail"} for k in range(60)]}
        whole = Path("/Users/you/loops/helpdesk-catalog/runs/hd-vb-tests/waits/20261119T220000Z-12345-error.json")
        line = wait.event_line(event, whole)
        self.assertLessEqual(len(line), spawn.MAX_PROMPT)
        self.assertTrue(line.endswith(f" (whole: {whole})"))
        self.assertNotIn("x" * 200, line)


class DeliverTest(test_wait.WaitCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.herdr = support.FakeHerdr()
        self.herdr.agents[MANAGER] = {"name": MANAGER, "pane_id": "w1:p1", "agent_status": "idle"}
        write_json(self.root / "watchdog.json", {"manager": {"agent": MANAGER, "pane": "w1:p1", "session": None}})

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = wait.main(list(argv), herdr=self.herdr, sleep=lambda s: None)
        return code, json.loads(out.getvalue())

    def test_the_whole_event_is_kept_and_its_line_is_typed_into_the_manager(self):
        event = done_event(self.run_dir)
        told = wait.deliver(self.root, self.run_dir, event, herdr=self.herdr, sleep=lambda s: None)
        self.assertEqual(told["delivered"], MANAGER)
        self.assertEqual(read_json(told["whole"]), event)
        self.assertEqual(Path(told["whole"]).parent, self.run_dir / "waits")
        self.assertEqual(self.herdr.taken[MANAGER], [told["line"]])
        self.assertEqual(told["line"], wait.event_line(event, Path(told["whole"])))
        self.assertEqual(sorted(p.name for p in (self.run_dir / "waits").iterdir()), [Path(told["whole"]).name])

    def test_a_manager_that_cannot_be_reached_leaves_the_file_and_says_why(self):
        (self.root / "watchdog.json").unlink()
        told = wait.deliver(self.root, self.run_dir, done_event(self.run_dir), herdr=self.herdr, sleep=lambda s: None)
        self.assertIn("undelivered", told)
        self.assertTrue(Path(told["whole"]).exists())
        # Said beside the event too, since a detached wait's answer goes nowhere.
        self.assertEqual(Path(told["whole"]).with_suffix(".undelivered").read_text(), told["undelivered"] + "\n")
        write_json(self.root / "watchdog.json", {"manager": {"agent": MANAGER, "session": None}})
        self.herdr.agents[MANAGER]["agent_status"] = "blocked"
        told = wait.deliver(self.root, self.run_dir, done_event(self.run_dir), herdr=self.herdr, sleep=lambda s: None)
        self.assertIn("blocked", told["undelivered"])
        self.assertEqual(self.herdr.taken, {})

    def test_deliver_needs_the_root(self):
        code, said = self.main("--run-dir", str(self.run_dir), "--max-min", "0", "--deliver")
        self.assertEqual((code, said["event"]), (2, "refused"))
        self.assertEqual(self.herdr.taken, {})

    def test_a_timer_is_delivered_and_exits_as_before(self):
        code, said = self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--max-min", "0", "--deliver")
        self.assertEqual((code, said["event"], said["delivered"]), (0, "time", MANAGER))
        self.assertTrue(self.herdr.taken[MANAGER][0].startswith('hl-wait {"event":"time"'))

    def test_a_refusal_is_delivered_too(self):
        code, said = self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--for", "not-a-watch", "--deliver")
        self.assertEqual((code, said["event"], said["delivered"]), (2, "refused", MANAGER))
        self.assertIn('"event":"refused"', self.herdr.taken[MANAGER][0])

    def test_a_crash_is_delivered_as_failed_and_raises_as_before_without_deliver(self):
        with mock.patch("hl.wait.wait", side_effect=RuntimeError("the disk went away")):
            code, said = self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--max-min", "0", "--deliver")
            self.assertEqual((code, said["event"]), (1, "failed"))
            self.assertIn("the disk went away", said["error"])
            self.assertIn('"event":"failed"', self.herdr.taken[MANAGER][0])
            with self.assertRaises(RuntimeError):
                self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--max-min", "0")

    def test_an_event_that_was_not_delivered_exits_3(self):
        self.herdr.agents[MANAGER]["agent_status"] = "blocked"
        code, said = self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--max-min", "0", "--deliver")
        self.assertEqual((code, said["event"]), (3, "time"))
        self.assertTrue(Path(said["whole"]).exists())

    def test_without_deliver_nothing_is_typed_or_written(self):
        code, said = self.main("--root", str(self.root), "--run-dir", str(self.run_dir), "--max-min", "0")
        self.assertEqual((code, said["event"]), (0, "time"))
        self.assertNotIn("delivered", said)
        self.assertEqual(self.herdr.taken, {})
        self.assertFalse((self.run_dir / "waits").exists())


class WaitDeliveryStageTest(test_stage.StageCase):
    def prompt(self):
        return (self.root / "MANAGER-PROMPT.md").read_text()

    def test_by_default_the_prompt_says_nothing_of_delivery(self):
        self.stage()
        self.assertNotIn("--deliver", self.prompt())
        self.assertIn("read \"The program's Claude seat\" there, at once.\n", self.prompt())

    def test_prompt_tells_the_manager_to_wait_with_deliver_detached(self):
        self.spec["settings"]["wait_delivery"] = "prompt"
        self.write_spec()
        self.stage()
        said = stage.WAIT_BY_PROMPT.replace("{{ROOT}}", str(self.root))
        self.assertIn("read \"The program's Claude seat\" there, at once." + said + "\n", self.prompt())
        self.assertIn(f"nohup hl-wait --deliver --root {self.root}", said)

    def test_a_value_outside_the_two_stops_the_stage(self):
        self.spec["settings"]["wait_delivery"] = "monitor"
        self.write_spec()
        self.assertIn("settings: `wait_delivery` is one of task, prompt, got 'monitor'", "; ".join(self.refusal()))
        self.assertFalse((self.root / "program.json").exists())


if __name__ == "__main__":
    support.unittest.main()
