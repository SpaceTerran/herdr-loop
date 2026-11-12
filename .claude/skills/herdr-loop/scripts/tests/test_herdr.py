"""The wrapper against a stand-in binary that prints what herdr 0.9.0 printed
on the mini on 2026-10-11, byte for byte where it matters: the replies below
were copied from that host."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from hl.herdr import Herdr, HerdrError

WORKSPACE_CREATED = (
    '{"id":"cli:workspace:create","result":{"root_pane":{"agent_status":"unknown","cwd":"/Users/you/loops/spike",'
    '"focused":false,"foreground_cwd":"/Users/you/loops/spike","pane_id":"wB:p1","revision":0,"tab_id":"wB:t1",'
    '"terminal_id":"term_65d97df8ba58f17","workspace_id":"wB"},"tab":{"agent_status":"unknown","focused":false,'
    '"label":"1","number":1,"pane_count":1,"tab_id":"wB:t1","workspace_id":"wB"},"type":"workspace_created",'
    '"workspace":{"active_tab_id":"wB:t1","agent_status":"unknown","focused":false,"label":"hl-probe","number":9,'
    '"pane_count":1,"tab_count":1,"workspace_id":"wB"}}}'
)
WORKSPACE_LIST = (
    '{"id":"cli:workspace:list","result":{"type":"workspace_list","workspaces":['
    '{"label":"New-Looprunner","pane_count":1,"tab_count":1,"workspace_id":"w8"},'
    '{"label":"hl-probe","pane_count":1,"tab_count":1,"workspace_id":"wB"}]}}'
)
PANE_CLOSED = '{"id":"cli:pane:close","result":{"type":"ok"}}'
PANE_NOT_FOUND = '{"error":{"code":"pane_not_found","message":"pane not found"},"id":"cli:pane:process_info"}'

# argv[1:3] -> (stdout, stderr, exit status)
ANSWERS = {
    "workspace create": (WORKSPACE_CREATED, "", 0),
    "workspace list": (WORKSPACE_LIST, "", 0),
    "pane run": ("", "", 0),
    "pane send-keys": ("", "", 0),
    "pane close": (PANE_CLOSED, "", 0),
    "pane process-info": (PANE_NOT_FOUND, "", 1),
    "tab rename": ("renamed\n", "", 0),
    "server stop": ("", "herdr: no server is running\n", 3),
    "pane list": ("[1, 2]", "", 0),
}

FAKE = """#!/usr/bin/env python3
import json, sys
out, err, code = json.loads({answers!r}).get(" ".join(sys.argv[1:3]), ("", "fake herdr: unexpected", 64))
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
"""


class CallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        binary = Path(tmp.name) / "herdr"
        binary.write_text(FAKE.format(answers=json.dumps(ANSWERS)))
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        self.herdr = Herdr(str(binary), env={})

    def test_a_command_that_succeeds_without_printing_anything_is_a_success(self):
        self.assertEqual(self.herdr.call("pane", "run", "wB:p2", "printf ok"), {})
        self.assertEqual(self.herdr.call("pane", "send-keys", "wB:p2", "ctrl+c"), {})

    def test_the_result_is_read_from_inside_the_cli_envelope(self):
        self.assertEqual(self.herdr.call("pane", "close", "wB:p2"), {"type": "ok"})

    def test_an_error_object_raises_with_herdrs_own_code(self):
        with self.assertRaises(HerdrError) as caught:
            self.herdr.call("pane", "process-info", "--pane", "wB:p2")
        self.assertEqual((caught.exception.code, caught.exception.not_found), ("pane_not_found", True))

    def test_a_failure_that_prints_no_json_raises_with_the_exit_status_and_what_was_said(self):
        with self.assertRaises(HerdrError) as caught:
            self.herdr.call("server", "stop")
        self.assertEqual(caught.exception.code, "exit_3")
        self.assertIn("no server is running", caught.exception.message)

    def test_exit_0_with_text_that_is_not_a_json_object_is_a_success_with_no_fields(self):
        self.assertEqual(self.herdr.call("tab", "rename", "wB:t1", "run-1"), {})
        self.assertEqual(self.herdr.call("pane", "list"), {})

    def test_reading_a_field_herdr_did_not_send_is_a_herdr_error_not_a_key_error(self):
        with self.assertRaises(HerdrError) as caught:
            self.herdr.call("pane", "run", "wB:p2", "x")["process_info"]
        self.assertEqual(caught.exception.code, "unexpected_reply")
        self.assertIn("herdr pane run answered without `process_info` (it sent: nothing)", str(caught.exception))
        with self.assertRaises(HerdrError) as caught:
            self.herdr.call("workspace", "create", "--label", "x")["workspace"]["id"]
        self.assertIn("answered without `id`", str(caught.exception))

    def test_a_new_workspace_gives_its_id_and_the_pane_and_tab_it_opened_with(self):
        made = self.herdr.create_workspace("hl-probe", "/Users/you/loops/spike")
        self.assertEqual(made, {"workspace": "wB", "pane": "wB:p1", "tab": "wB:t1"})

    def test_a_workspace_is_found_by_its_label(self):
        self.assertEqual(self.herdr.find_workspace("hl-probe"), "wB")
        self.assertIsNone(self.herdr.find_workspace("Help desk portal"))


if __name__ == "__main__":
    unittest.main()
