"""What a review is given (the diff's facts, from real git) and whether it
did its work (its transcript, in the shape Claude Code 2.1.296 writes: the
tool names and inputs below were read from evaluator sessions on the mini)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from hl import items, merge, review
from hl.common import Refusal, read_json, write_json

from .support import RunCase
from .test_merge import git

def png(width=1280, height=800):
    """The start of a PNG file: its signature and the header chunk that gives its size."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00" + b"\x00" * 400


PNG = png()


def write_transcript(home: Path, session: str, calls, sub_agents=()):
    """A session's transcript, and its sub-agents', as Claude Code lays them
    out. A call is a tool's name and what it was given; a third value is
    what came back from it and was shown to the session (the text of the
    tool result: on the laptop, Claude Code 2.1.296 writes a shell command's
    as one string, and a file read as one string whose lines begin with
    their number and a tab)."""
    project = home / ".claude" / "projects" / "-some-evidence-dir"
    project.mkdir(parents=True, exist_ok=True)

    def lines(tool_calls):
        out = [json.dumps({"type": "user", "message": {"role": "user", "content": "Read the brief and do exactly what it says."}})]
        for i, (name, given, *shown) in enumerate(tool_calls):
            block = {"type": "tool_use", "id": f"toolu_{i}", "name": name, "input": given}
            out.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "…"}, block]}}))
            result = {"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": shown[0] if shown else "ok"}
            out.append(json.dumps({"type": "user", "message": {"role": "user", "content": [result]}}))
        return "\n".join(out) + "\nnot json\n"

    (project / f"{session}.jsonl").write_text(lines(calls))
    for k, tool_calls in enumerate(sub_agents):
        sub = project / session / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"agent-a{k}.jsonl").write_text(lines(tool_calls))


def bash(command, shown=None):
    return ("Bash", {"command": command, "description": "x"}) + (() if shown is None else (shown,))


def navigate(url):
    return ("mcp__playwright__browser_navigate", {"url": url})


READS = [
    bash("cd /loops/p/repos/console/backend/.worktrees/a-1 && git status --short | head -20; git rev-parse HEAD; gh pr checks 1316 --required"),
    bash("git -C /wt diff origin/main...HEAD | cat"),
    bash("git -C /wt log --oneline -5 && gh api repos/o/r/pulls/7 --jq .body"),
    bash("W=/wt; E=/ev; mkdir -p $E; cp $W/README.md $E/; grep -rn 'palette' $W/src | wc -l; sed -n 1,20p $W/app.py"),
    ("Read", {"file_path": "/wt/app.py"}),
]


class RanSomethingTest(RunCase):
    def test_commands_that_only_read_ran_nothing(self):
        for name, given in READS[:4]:
            self.assertFalse(review.ran_something(given["command"]), given["command"])
        for command in (
            "cat a.txt; ls -la b",
            "FOO=1 git --no-pager -C /wt show --stat HEAD",
            "gh pr view 7 --repo o/r --json body --jq .body | head -40",
            "gh -R o/r pr view 5",
            "gh run view 4242 --repo o/r --log-failed | tail -50",
            "time git diff --stat",
            "",
        ):
            self.assertFalse(review.ran_something(command), command)

    def test_a_break_inside_quotes_is_not_a_break_between_commands(self):
        # Found in review: each of these only reads, and its quoted `|`, `&&` or `;` used to leave a fragment that read as a program.
        for command in (
            "rg 'foo|bar' src/",
            'grep -E "def (a|b)" x.py',
            "git log --format='%h|%s'",
            "git log --grep='a && b'",
            "grep -rn 'x; rm -rf y' docs",
            'echo "a && pytest"',
            "sed -n '/^## Criteria/,/^## /p' contract.md | head -20",
        ):
            self.assertFalse(review.ran_something(command), command)

    def test_the_body_of_a_here_document_is_text(self):
        self.assertFalse(review.ran_something("cat > /ev/note.txt <<EOF\npytest tests\ncurl http://127.0.0.1:8401/\nEOF"))
        self.assertFalse(review.ran_something("cat <<-'DONE' > x\n\tnpm run test\n\tDONE\nls"))
        self.assertTrue(review.ran_something("cat <<EOF > x\ntext\nEOF\npytest tests/test_x.py"))

    def test_asking_a_program_its_version_and_changing_things_with_git_or_gh_exercise_nothing(self):
        for command in (
            "python3 --version",
            "node -v && npm --version",
            "git -C /wt checkout -b other",
            "gh api --method PATCH repos/o/r/pulls/7 -F body=@x",
            "gh api -X DELETE repos/o/r/git/refs/heads/x",
            "gh pr merge 7 --squash",
        ):
            self.assertFalse(review.ran_something(command), command)

    def test_a_command_that_runs_anything_counts(self):
        for command in (
            "curl -s http://127.0.0.1:8401/ping",
            "python3 scripts/bytecount.py f; echo $?",
            "cd /wt && .venv/bin/python -m pytest tests/test_x.py -q -p no:cacheprovider",
            "cd /wt/Frontend/react && npm run test -- src/theme",
            "FOO=1 node check.js",
            "git diff | python3 -c 'import sys; print(len(sys.stdin.read()))'",
            "bash ./run.sh",
            "ls; ./scripts/linecount.py README.md",
        ):
            self.assertTrue(review.ran_something(command), command)

    def test_what_a_shell_lets_stand_in_front_of_a_command_is_not_the_command(self):
        # Found in review: each of these runs something, and used to read as running nothing.
        for command in (
            "time pytest tests/test_x.py",
            "timeout 60 make test",
            "gtimeout --signal=KILL 2m npm run build",
            "for f in a b; do pytest $f; done",
            "if curl -sf http://127.0.0.1:3000/; then echo up; fi",
            "while ! curl -sf localhost:8401/healthz; do sleep 1; done",
            "env FOO=1 nice -n 5 node check.js",
            "! pytest -q tests/test_x.py",
            "{ pytest -q; echo done; } > out.txt",
        ):
            self.assertTrue(review.ran_something(command), command)
        for command in ("for f in a b; do echo $f; done", "if test -f x; then cat x; fi", "while read line; do echo $line; done < f"):
            self.assertFalse(review.ran_something(command), command)

    def test_a_command_run_through_another_or_inside_a_substitution_counts(self):
        for command in (
            "ls tests/test_*.py | xargs pytest -q",
            "ls | xargs -n 1 -I {} python {}",
            'find . -name "*.py" -exec python {} \\;',
            "out=$(curl -s localhost:8401/); echo $out",
            'echo "$(pytest -q tests/test_x.py | tail -1)"',
            "echo `node check.js`",
        ):
            self.assertTrue(review.ran_something(command), command)
        for command in ("ls | xargs cat", 'find . -name "*.py" -print', "echo $(git rev-parse HEAD)", 'find . -name "*.log" -exec cat {} \\;'):
            self.assertFalse(review.ran_something(command), command)

    def test_a_flag_that_asks_for_more_output_is_not_a_question_about_the_version(self):
        # Found in review: `pytest -v` read as asking pytest its version, so an honest review ran nothing.
        for command in ("pytest -v", "cd /wt && .venv/bin/python -m pytest -v", "ruff -v"):
            self.assertTrue(review.ran_something(command), command)
        for command in ("node -v", "npm -v", "pytest --version", "python3 -V", "npx -v"):
            self.assertFalse(review.ran_something(command), command)

    def test_a_here_string_is_not_a_here_document(self):
        # Found in review: `<<<` opened a here-document that swallowed every command after it.
        self.assertTrue(review.ran_something('grep -c x <<< "hello"\npytest tests/test_x.py'))
        self.assertFalse(review.ran_something('grep -c x <<< "hello"\nls'))
        self.assertEqual(review.split_heredocs('grep -c x <<< "EOF"\nls\nEOF')[1], [])

    def test_two_angle_brackets_inside_quotes_a_comment_or_arithmetic_open_no_here_document(self):
        # Found in review: `echo "x << y"` swallowed every command after it, so an honest review ran nothing.
        for command in (
            'echo "x << y"\npytest tests/test_x.py',
            "python3 -c 'print(1 << n)'\nn\npytest tests/test_x.py",
            "ls  # see << EOF for the rest\npytest tests/test_x.py",
            "echo $((1 << n))\npytest tests/test_x.py",
            "python3 -c '\nx = 1 << y\n'\nmake check",
            # The quote opened on one line is still open on the next.
            "echo 'first\nx << y\nlast'\npytest tests/test_x.py",
        ):
            self.assertTrue(review.ran_something(command), command)
        # A `#` inside a word is part of the word, and the here-document after it is one.
        self.assertFalse(review.ran_something("cat > /ev/a#b.txt <<EOF\npytest tests\nEOF\nls"))
        # And a real one, after a quoted word on the same line, still holds only text.
        self.assertFalse(review.ran_something("cat 'a b' > /ev/x.txt <<EOF\npytest tests\nEOF\nls"))
        self.assertFalse(review.ran_something('echo "$((2 << 3))"'))
        self.assertFalse(review.ran_something("echo $((1 + 2))"))

    def test_asking_where_a_program_is_does_not_run_it(self):
        # Found in review: `command -v pytest` read as running pytest.
        for command in ("command -v pytest", "command -V node && echo yes", "hash pytest 2>/dev/null", "which pytest; type node"):
            self.assertFalse(review.ran_something(command), command)
        self.assertTrue(review.ran_something("command pytest -q tests/test_x.py"))

    def test_defining_a_function_and_calling_it_runs_what_its_body_runs(self):
        # Found in review: `f() { :; }; f` read as running a program called f.
        for command in ("f() { :; }; f", "function g { echo hi; }; g", "t () {\n  cat x\n}\nt", "function h() { ls; }; h a b"):
            self.assertFalse(review.ran_something(command), command)
        for command in ('t() { pytest "$@"; }; t tests/test_x.py', "function g { npm run test; }; g", "f() { :; }; f; make check"):
            self.assertTrue(review.ran_something(command), command)

    def test_setting_the_shell_up_or_printing_a_file_ran_nothing(self):
        for command in (
            "source .venv/bin/activate",
            ". ./env.sh",
            "bat src/x.py",
            "env",
            "env | sort | head",
            "printenv PATH",
            "zcat log.gz | head",
            "export FOO=1; readonly BAR=2",
        ):
            self.assertFalse(review.ran_something(command), command)
        for command in ("source .venv/bin/activate && pytest tests/test_x.py", "env FOO=1 node check.js", "env -i PATH=/usr/bin python3 x.py"):
            self.assertTrue(review.ran_something(command), command)

    def test_a_script_is_read_into_its_commands(self):
        self.assertEqual(
            review.shell_commands("cd /wt && FOO='a b' pytest \"tests/x y.py\" -k 'a|b'; echo done > out.txt 2>&1 &"),
            [["cd", "/wt"], ["FOO=a b", "pytest", "tests/x y.py", "-k", "a|b"], ["echo", "done", ">", "out.txt", "2>&1"]],
        )
        self.assertEqual(review.shell_commands("a # not b | c\nd"), [["a"], ["d"]])


class ServedCallTest(RunCase):
    def test_the_served_build_is_named_by_its_own_address_and_no_other(self):
        for command in (
            "curl -s http://127.0.0.1:3000/ping",
            "curl -s localhost:3000",
            "curl -sI http://0.0.0.0:3000/x",
            "curl 'http://[::1]:3000/'",
            "PORT=3000; curl http://127.0.0.1:$PORT/x",
            'B=http://localhost:3000 && curl -s "${B}/health"',
            "for p in / /app; do curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000$p; done",
        ):
            self.assertTrue(review.calls_served(command, 3000), command)

    def test_another_port_a_reading_command_and_an_unset_variable_are_not_calls(self):
        # Found in review: `:30001` used to count for a build served on 3000, and a grep that mentioned the address too.
        for command in (
            "curl http://127.0.0.1:30001/",
            "curl http://127.0.0.1:300/",
            "curl http://10.127.0.0.1:3000/",
            "rg 'localhost:3000|x' src",
            "grep -rn '127.0.0.1:3000' /wt",
            "curl http://127.0.0.1:$PORT/",
            "echo http://127.0.0.1:3000/ && cat x",
        ):
            self.assertFalse(review.calls_served(command, 3000), command)


    def test_an_address_set_with_export_or_given_as_two_words_is_still_the_address(self):
        # Found in review: each of these calls the served build, and read as calling nothing.
        for command in (
            "export PORT=3000; curl localhost:$PORT",
            "export BASE=http://127.0.0.1:3000 && curl -s $BASE/x",
            "nc -z localhost 3000",
            "nc -z 127.0.0.1 3000 && echo up",
        ):
            self.assertTrue(review.calls_served(command, 3000), command)
        for command in ("nc -z localhost 30001", "nc -z localhost 300", "echo localhost 3000", "export PORT=3001; curl localhost:$PORT"):
            self.assertFalse(review.calls_served(command, 3000), command)

    def test_a_program_fed_to_an_interpreter_calls_what_its_text_calls(self):
        # Found in review, and common: a reviewer requests the pages from a script on standard input.
        for command in (
            "python3 - <<'PY'\nimport urllib.request\nprint(urllib.request.urlopen('http://127.0.0.1:3000/app').status)\nPY",
            "cd /wt && .venv/bin/python <<EOF\nimport requests\nrequests.get('http://localhost:3000/')\nEOF",
            "node <<'JS'\nfetch('http://127.0.0.1:3000/')\nJS",
            "FOO=1 timeout 60 python3 - <<-'PY'\n\timport requests; requests.get('http://127.0.0.1:3000/')\n\tPY",
        ):
            self.assertTrue(review.calls_served(command, 3000), command)
        for command in (
            # Text written to a file is text, whatever it says.
            "cat > /ev/probe.py <<'PY'\nrequests.get('http://127.0.0.1:3000/')\nPY",
            "python3 - <<'PY'\nprint('http://127.0.0.1:30001/')\nPY",
            "tee notes.md <<EOF\nserved at http://127.0.0.1:3000/\nEOF\npython3 x.py",
        ):
            self.assertFalse(review.calls_served(command, 3000), command)

    def test_a_script_written_to_a_file_and_then_run_calls_what_its_text_calls(self):
        # Found in review: a reviewer that wrote its probe to a file before running it read as calling nothing.
        for command in (
            "cat > /ev/probe.py <<'PY'\nimport requests\nrequests.get('http://127.0.0.1:3000/')\nPY\npython3 /ev/probe.py",
            "cat >probe.py <<'PY'\nrequests.get('http://localhost:3000/')\nPY\ncd /ev && .venv/bin/python probe.py",
            "tee check.js <<'JS' >/dev/null\nfetch('http://127.0.0.1:3000/')\nJS\nnode check.js",
        ):
            self.assertTrue(review.calls_served(command, 3000), command)
        for command in (
            # Written and only read back.
            "cat > /ev/probe.py <<'PY'\nrequests.get('http://127.0.0.1:3000/')\nPY\ncat /ev/probe.py",
            # Another file is run.
            "cat > /ev/probe.py <<'PY'\nrequests.get('http://127.0.0.1:3000/')\nPY\npython3 /ev/other.py",
        ):
            self.assertFalse(review.calls_served(command, 3000), command)
        self.assertEqual(review.written_by("cat > a.py <<EOF\none\nEOF\ncat >> a.py <<EOF\ntwo\nEOF\npython3 - <<EOF\nthree\nEOF"), {"a.py": "one\ntwo\n"})
        # A file written in one call and run in a later one, when the caller keeps what was written.
        written = {}
        self.assertFalse(review.calls_served("cat > p.py <<'PY'\nrequests.get('http://127.0.0.1:3000/')\nPY", 3000, written))
        self.assertTrue(review.calls_served("python3 p.py", 3000, written))
        self.assertFalse(review.calls_served("python3 p.py", 3000))


class ExerciseTest(RunCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def seen(self, calls, port=8401, sub_agents=()):
        write_transcript(self.home, "s-1", calls, sub_agents)
        return review.exercise(review.session_transcripts("s-1"), port)

    def test_a_review_that_only_read_shows_no_exercise(self):
        seen = self.seen(READS)
        self.assertEqual((seen["shell_calls"], seen["ran"], seen["browser_actions"], seen["served_calls"]), (4, 0, 0, 0))

    def test_a_call_on_the_served_build_is_seen_by_its_port(self):
        seen = self.seen(READS + [bash("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8401/ping"), bash("curl -s http://127.0.0.1:9999/")])
        self.assertEqual((seen["ran"], seen["served_calls"]), (2, 1))
        self.assertEqual(self.seen([bash("curl -s http://localhost:8401/")])["served_calls"], 1)
        # A port that is only mentioned by a reading command was not called.
        self.assertEqual(self.seen([bash("grep -rn '127.0.0.1:8401' /wt")])["served_calls"], 0)

    def test_the_browser_on_the_served_build_is_seen_however_the_page_was_opened(self):
        seen = self.seen(
            [
                ("mcp__playwright__browser_resize", {"width": 1280, "height": 800}),
                navigate("http://127.0.0.1:8401/app/alerts"),
                ("mcp__playwright__browser_evaluate", {"function": "() => getComputedStyle(document.body).backgroundColor"}),
                ("mcp__playwright__browser_take_screenshot", {"scale": "css", "filename": "/ev/alerts-dark.png", "type": "png"}),
                ("mcp__playwright__browser_run_code_unsafe", {"code": "async (page) => { await page.goto('http://127.0.0.1:8401/pricing'); await page.screenshot({path: '/ev/p.png'}); }"}),
                navigate("https://example.invalid/"),
            ]
        )
        self.assertEqual((seen["browser_actions"], seen["navigations"], seen["served_pages"]), (5, 3, 2))

    def test_a_script_on_standard_input_that_requests_the_page_is_a_call_on_the_served_build(self):
        script = "python3 - <<'PY'\nimport urllib.request\nprint(urllib.request.urlopen('http://127.0.0.1:8401/app').status)\nPY"
        seen = self.seen(READS + [bash(script)])
        self.assertEqual((seen["ran"], seen["served_calls"]), (1, 1))

    def test_a_probe_written_with_the_file_tool_and_then_run_is_a_call_on_the_served_build(self):
        probe = ("Write", {"file_path": "/ev/check.js", "content": "const r = await fetch('http://127.0.0.1:8401/app'); console.log(r.status)"})
        self.assertEqual(self.seen([probe, bash("cd /ev && node check.js")])["served_calls"], 1)
        # Written and never run, or run before anything named the address, is no call.
        self.assertEqual(self.seen([probe, bash("cat /ev/check.js")])["served_calls"], 0)
        self.assertEqual(self.seen([bash("node check.js"), probe])["served_calls"], 0)
        edited = ("Edit", {"file_path": "/ev/check.js", "old_string": "x", "new_string": "fetch('http://localhost:8401/')"})
        self.assertEqual(self.seen([edited, bash("node /ev/check.js")])["served_calls"], 1)

    # What a page script opens, as a reviewer passes it to the browser: inline, or as a file it names.
    SCRIPT = "async (page) => { await page.goto('http://127.0.0.1:8401/x'); await page.screenshot({path: '/ev/x.png'}); }"
    ELSEWHERE = "async (page) => { await page.goto('http://127.0.0.1:9999/x'); }"

    def run_file(self, filename, action="browser_run_code_unsafe"):
        return (f"mcp__playwright__{action}", {"filename": filename})

    def pages(self, calls):
        seen = self.seen(calls)
        return seen["browser_actions"], seen["navigations"], seen["served_pages"]

    def test_a_browser_script_passed_inline_is_still_read_for_the_page_it_opens(self):
        self.assertEqual(self.pages([("mcp__playwright__browser_run_code_unsafe", {"code": self.SCRIPT})]), (1, 1, 1))
        self.assertEqual(self.pages([("mcp__playwright__browser_run_code", {"code": self.ELSEWHERE})]), (1, 1, 0))

    def test_a_browser_script_passed_as_a_file_is_read_for_the_page_it_opens(self):
        # Found on HCO-34: the reviewer wrote its script to a file and passed the browser only the file's name.
        written = ("Write", {"file_path": "/scratch/looks.js", "content": self.SCRIPT})
        self.assertEqual(self.pages([written, self.run_file("/scratch/looks.js")]), (1, 1, 1))
        # Written by a here-document to a path in a variable, as on HCO-34.
        heredoc = bash(f"S=/scratch; cat > $S/looks.js <<EOF\n{self.SCRIPT}\nEOF")
        self.assertEqual(self.pages([heredoc, self.run_file("/scratch/looks.js")]), (1, 1, 1))
        self.assertEqual(self.pages([bash(f"tee /scratch/t.js >/dev/null <<'JS'\n{self.SCRIPT}\nJS"), self.run_file("/scratch/t.js")]), (1, 1, 1))
        # Only on disk: written some way the transcript does not show.
        on_disk = self.tmp / "looks-run.js"
        on_disk.write_text(self.SCRIPT)
        self.assertEqual(self.pages([self.run_file(str(on_disk), "browser_run_code")]), (1, 1, 1))

    def test_a_browser_script_copied_to_where_the_browser_may_read_it_is_followed(self):
        # HCO-34, both reviews: the browser refused the scratch path, so the script was copied into the evidence
        # directory, run from there, and removed. The copy is gone from disk; the transcript shows what it held.
        made = bash(f"cd /ev; S=/scratch\ncat > $S/looks.js <<EOF\n{self.SCRIPT}\nEOF")
        copied = bash("cd /items/A-1/evidence-2; S=/scratch; mkdir -p .pw && cp $S/looks.js .pw/looks-run.js && ls")
        removed = bash("cd /items/A-1/evidence-2; rm .pw/looks-run.js")
        self.assertEqual(self.pages([made, copied, self.run_file("/items/A-1/evidence-2/.pw/looks-run.js"), removed]), (1, 1, 1))
        self.assertEqual(self.pages([made, bash("cp /scratch/looks.js /ev/into/"), self.run_file("/ev/into/looks.js")]), (1, 1, 1))
        self.assertEqual(self.pages([made, bash("mv /scratch/looks.js /ev/m.js"), self.run_file("/ev/m.js")]), (1, 1, 1))
        # Removed, or moved away, before it was run: nothing was there to run.
        self.assertEqual(self.pages([made, bash("rm -f /scratch/looks.js"), self.run_file("/scratch/looks.js")]), (1, 0, 0))
        self.assertEqual(self.pages([made, bash("mv /scratch/looks.js /ev/m.js"), self.run_file("/scratch/looks.js")]), (1, 0, 0))
        # A copy of a file the transcript does not show is read from disk, where there is none.
        self.assertEqual(self.pages([bash("cp /elsewhere/looks.js /ev/c.js"), self.run_file("/ev/c.js")]), (1, 0, 0))

    def test_a_browser_script_file_is_read_as_it_last_was_before_the_call(self):
        first = ("Write", {"file_path": "/scratch/a.js", "content": self.SCRIPT})
        rewritten = ("Write", {"file_path": "/scratch/a.js", "content": self.ELSEWHERE})
        self.assertEqual(self.pages([first, rewritten, self.run_file("/scratch/a.js")]), (1, 1, 0))
        self.assertEqual(self.pages([rewritten, self.run_file("/scratch/a.js"), first]), (1, 1, 0))
        moved = ("Edit", {"file_path": "/scratch/a.js", "old_string": "127.0.0.1:8401", "new_string": "127.0.0.1:9999"})
        self.assertEqual(self.pages([first, moved, self.run_file("/scratch/a.js")]), (1, 1, 0))
        back = ("Edit", {"file_path": "/scratch/a.js", "old_string": "127.0.0.1:9999", "new_string": "127.0.0.1:8401", "replace_all": True})
        self.assertEqual(self.pages([rewritten, back, self.run_file("/scratch/a.js")]), (1, 1, 1))
        # Rewritten by the shell in a way that does not show the text: what is on disk (here, nothing) is what ran.
        self.assertEqual(self.pages([first, bash("node gen.js > /scratch/a.js"), self.run_file("/scratch/a.js")]), (1, 0, 0))
        self.assertEqual(self.pages([first, bash("echo more >> /scratch/b.js"), self.run_file("/scratch/b.js")]), (1, 0, 0))

    def test_a_browser_script_file_that_opens_no_page_of_the_served_build_is_none(self):
        elsewhere = self.tmp / "elsewhere.js"
        elsewhere.write_text(self.ELSEWHERE)
        self.assertEqual(self.pages([self.run_file(str(elsewhere))]), (1, 1, 0))
        written = ("Write", {"file_path": "/scratch/other.js", "content": "async (page) => { await page.goto('https://example.invalid/'); }"})
        self.assertEqual(self.pages([written, self.run_file("/scratch/other.js")]), (1, 1, 0))
        # A file that names the address but opens nothing is no page either.
        idle = self.tmp / "idle.js"
        idle.write_text("// http://127.0.0.1:8401/x\nasync (page) => {}")
        self.assertEqual(self.pages([self.run_file(str(idle))]), (1, 0, 0))
        # What the transcript shows the file held outranks what is on disk now.
        served = self.tmp / "served.js"
        served.write_text(self.SCRIPT)
        self.assertEqual(self.pages([("Write", {"file_path": str(served), "content": self.ELSEWHERE}), self.run_file(str(served))]), (1, 1, 0))
        # Another file of the same name is another file.
        self.assertEqual(self.pages([("Write", {"file_path": "/a/looks.js", "content": self.SCRIPT}), self.run_file("/b/looks.js")]), (1, 0, 0))

    def test_a_browser_script_file_that_is_missing_or_cannot_be_read_counts_nothing(self):
        big = self.tmp / "big.js"
        big.write_text(self.SCRIPT + " " * review.SCRIPT_FILE_MAX)
        fifo = self.tmp / "pipe.js"
        os.mkfifo(fifo)
        link = self.tmp / "link.js"
        link.symlink_to(self.tmp / "no-such.js")
        # A relative name is resolved by the browser server against a root of its own, not where this runs.
        (self.tmp / "rel.js").write_text(self.SCRIPT)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        for name in (str(self.tmp / "no-such.js"), str(self.tmp), str(big), str(fifo), str(link), "rel.js", "", None, 7, ["/x.js"]):
            self.assertEqual(self.pages([self.run_file(name)]), (1, 0, 0), name)
        # A file at the limit is read.
        edge = self.tmp / "edge.js"
        edge.write_text(self.SCRIPT.ljust(review.SCRIPT_FILE_MAX))
        self.assertEqual(self.pages([self.run_file(str(edge))]), (1, 1, 1))

    def test_a_filename_on_a_browser_call_that_runs_no_script_is_where_it_writes_and_opens_no_page(self):
        # Found in review: a screenshot, snapshot or evaluate call names a file to write its output to, not a script.
        written = ("Write", {"file_path": "/scratch/looks.js", "content": self.SCRIPT})
        on_disk = self.tmp / "looks-out.js"
        on_disk.write_text(self.SCRIPT)
        for prefix in ("mcp__playwright__", "mcp__plugin_playwright_playwright__"):
            for action, given in (
                ("browser_take_screenshot", {"type": "png"}),
                ("browser_snapshot", {}),
                ("browser_evaluate", {"function": "() => document.title"}),
            ):
                for filename in ("/scratch/looks.js", str(on_disk)):
                    call = (prefix + action, dict(given, filename=filename))
                    self.assertEqual(self.pages([written, call]), (1, 0, 0), (prefix, action, filename))

    def test_a_filename_on_a_run_code_call_is_still_the_script_it_runs(self):
        written = ("Write", {"file_path": "/scratch/looks.js", "content": self.SCRIPT})
        for prefix in ("mcp__playwright__", "mcp__plugin_playwright_playwright__"):
            for action in ("browser_run_code", "browser_run_code_unsafe"):
                call = (prefix + action, {"filename": "/scratch/looks.js"})
                self.assertEqual(self.pages([written, call]), (1, 1, 1), (prefix, action))

    def test_a_browser_server_under_another_name_is_still_the_browser(self):
        # Found in review: a Playwright server installed as a plugin names its tools differently.
        seen = self.seen([("mcp__plugin_playwright_playwright__browser_navigate", {"url": "http://127.0.0.1:8401/app"}), ("mcp__github__get_file", {"path": "x"})])
        self.assertEqual((seen["browser_actions"], seen["served_pages"]), (1, 1))

    def test_what_a_sub_agent_ran_counts_as_the_reviewers_own(self):
        seen = self.seen(READS, sub_agents=[[bash("cd /wt && .venv/bin/python -m pytest tests/web -q")], [bash("curl -s http://127.0.0.1:8401/healthz")]])
        self.assertEqual((seen["ran"], seen["served_calls"], seen["sub_agents"]), (2, 1, 2))

    def test_a_session_with_no_transcript_has_none(self):
        self.assertEqual(review.session_transcripts("no-such-session"), [])


class CheckCase(RunCase):
    """An item built and under review, with the reviewer's session in a home of its own."""

    PORT = 8401

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.init()
        self.agree()
        self.build()
        self.grade(session="rev-1")
        self.served()

    def served(self, served=True):
        write_json(self.item_dir() / "serve-1.json", {"served": served, "port": self.PORT if served else None})

    def look(self, *looks):
        path = self.item_dir() / "verdict-1.json"
        data = read_json(path)
        data.update(fix_list=[], looks=list(looks))
        write_json(path, data)

    def picture(self, name="evidence-1/alerts-dark.png", content=PNG):
        path = self.item_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"mode": "dark", "url": f"http://127.0.0.1:{self.PORT}/app/alerts", "screenshot": name, "result": "match", "note": "against the reference"}

    def route(self, look):
        items.route_item(self.run_dir, "A-1", "independent", look, "test")

    def check(self, calls, **kw):
        write_transcript(self.home, "rev-1", calls, **kw)
        return review.check(self.run_dir, "A-1", 1)


class CheckTest(CheckCase):
    def test_a_review_that_ran_nothing_did_not_exercise_the_change(self):
        out = self.check(READS)
        self.assertFalse(out["exercised"])
        self.assertIn("the reviewer ran nothing: 4 shell calls, all of them reading", out["problems"][0])

    def test_a_served_build_that_nothing_called_was_not_exercised(self):
        out = self.check(READS + [bash("cd /wt && python3 -m pytest tests/test_x.py")])
        self.assertFalse(out["exercised"])
        self.assertEqual(out["problems"], ["the build was served on port 8401 and nothing in the review called it"])

    def test_a_call_on_the_served_build_is_an_exercise(self):
        out = self.check(READS + [bash("curl -s http://127.0.0.1:8401/ping")])
        self.assertEqual((out["exercised"], out["problems"]), (True, []))

    def test_where_nothing_is_served_running_the_change_is_the_exercise(self):
        self.served(False)
        out = self.check([bash("cd /wt && python3 scripts/bytecount.py /tmp/f; echo $?")])
        self.assertEqual((out["exercised"], out["served"], out["problems"]), (True, False, []))

    def test_a_port_on_record_for_a_build_that_was_not_served_is_not_a_served_build(self):
        write_json(self.item_dir() / "serve-1.json", {"served": False, "port": self.PORT})
        out = self.check([bash("python3 scripts/bytecount.py f")])
        self.assertEqual((out["served"], out["port"], out["exercised"]), (False, None, True))

    def test_a_reviewer_whose_transcript_is_gone_shows_nothing(self):
        out = review.check(self.run_dir, "A-1", 1)
        self.assertFalse(out["exercised"])
        self.assertIn("transcript (session rev-1) cannot be found", out["problems"][0])

    def test_a_review_that_was_never_started_is_refused(self):
        with self.assertRaises(Refusal):
            review.check(self.run_dir, "A-1", 2)

    def test_an_item_a_person_looks_at_needs_the_look(self):
        self.route(look=True)
        calls = READS + [bash("curl -s http://127.0.0.1:8401/healthz"), navigate("http://127.0.0.1:8401/app/alerts")]
        self.look(self.picture())
        out = self.check(calls)
        self.assertEqual((out["exercised"], out["looked"], out["looks"], out["problems"]), (True, True, 1, []))

    def test_a_look_item_reviewed_without_opening_a_page_is_refused(self):
        self.route(look=True)
        self.look(self.picture())
        out = self.check(READS + [bash("curl -s http://127.0.0.1:8401/app/alerts | grep -c violet")])
        self.assertTrue(out["exercised"])
        self.assertIn("did not look: its transcript shows no page of the served build opened in the browser", out["problems"][0])

    def test_a_look_item_whose_verdict_records_no_look_is_refused(self):
        self.route(look=True)
        self.look()
        out = self.check([navigate("http://127.0.0.1:8401/app/alerts")])
        self.assertIn("did not look: its verdict records no look", out["problems"][0])

    def test_a_look_that_cites_no_real_picture_is_refused(self):
        self.route(look=True)
        fake = self.picture("evidence-1/not-a-picture.png", b"just text, however long it is" * 20)
        missing = {**self.picture(), "screenshot": "evidence-1/never-written.png"}
        self.look(self.picture(), fake, missing)
        out = self.check([navigate("http://127.0.0.1:8401/app/alerts")])
        self.assertIn("2 of the 3 looks its verdict records cite no picture that exists", out["problems"][0])

    def test_a_file_that_only_begins_like_a_picture_is_not_one(self):
        # Found in review: any file that began with a PNG's first eight bytes was taken for a screenshot.
        for content, real in (
            (png(), True),
            (png(80, 24), True),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 400, True),
            (b"\x89PNG\r\n\x1a\n" + b"\x00" * 400, False),
            (png(0, 0), False),
            (png(1, 1), False),
            (png()[:60], False),
            (b"not a picture" * 40, False),
        ):
            path = self.tmp / "p.png"
            path.write_bytes(content)
            self.assertEqual(review._is_image(path), real, content[:30])
        self.assertFalse(review._is_image(self.tmp / "absent.png"))

    def test_a_picture_from_another_review_or_from_before_this_one_is_not_this_reviews_look(self):
        # Found in review: any image over 200 bytes passed, so a screenshot copied from an earlier review would have.
        self.route(look=True)
        elsewhere = self.picture("evidence-0/alerts-dark.png")
        self.look(elsewhere)
        calls = [navigate("http://127.0.0.1:8401/app/alerts")]
        self.assertIn("1 of the 1 looks its verdict records cite no picture that exists", self.check(calls)["problems"][0])
        old = self.picture()
        os.utime(self.item_dir() / old["screenshot"], (1_000_000, 1_000_000))
        self.look(old)
        self.assertIn("1 of the 1 looks", self.check(calls)["problems"][0])

    def test_a_look_item_with_nothing_served_cannot_have_been_looked_at(self):
        self.route(look=True)
        self.served(False)
        self.look(self.picture())
        out = self.check([bash("python3 x.py"), navigate("http://127.0.0.1:8401/")])
        self.assertIn("nothing was served, so there was nothing to open", out["problems"][0])

    def test_a_floor_asks_for_the_look_whatever_the_route_says(self):
        item = items.load_item(self.run_dir, "A-1")
        item["floor"] = ["look"]
        items.save_item(self.run_dir, item)
        out = self.check([bash("curl -s http://127.0.0.1:8401/")])
        self.assertTrue(out["look_required"])
        self.assertEqual(len(out["problems"]), 1)

    def test_an_item_nobody_looks_at_needs_no_look(self):
        self.route(look=False)
        out = self.check([bash("curl -s http://127.0.0.1:8401/")])
        self.assertEqual((out["look_required"], out["looked"], out["problems"]), (False, False, []))


class DiffFactsTest(RunCase):
    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "clone"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "clone", str(self.origin), str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        self.put("src/app.py", "def page():\n    return 'old'\n")
        self.put("tests/test_app.py", "from src import app\n\n\ndef test_page():\n    assert app.page() == 'old'\n    assert app.page()\n")
        self.put("tests/test_gone.py", "def test_gone():\n    assert True\n")
        self.put("Frontend/react/package.json", '{"dependencies": {}}\n')
        self.put("Frontend/react/src/pages/Alerts.test.tsx", "it('renders', () => { expect(1).toBe(1) })\n")
        self.put("web/templates/base.html", "<body class='old'></body>\n")
        self.put("brand/family/SHA256SUMS", "aaa  tokens.json\n")
        self.put("docs/notes.md", "notes\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "push", "origin", "HEAD:main")
        git(self.repo, "checkout", "-b", "hl/run-1/A-1")
        self.tools = merge.Tools(sleep=lambda s: None)

    def put(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def facts(self, **repo_facts):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "A-1: the change")
        return review.diff_facts(self.tools, self.repo, "origin/main", git(self.repo, "rev-parse", "HEAD"), repo_facts)

    def test_the_facts_name_what_a_reviewer_must_not_take_on_trust(self):
        self.put("src/app.py", "def page():\n    return 'new'  # noqa\n")
        self.put("tests/test_app.py", "import pytest\nfrom src import app\n\n\n@pytest.mark.skip(reason='later')\ndef test_page():\n    assert app.page()\n")
        (self.repo / "tests/test_gone.py").unlink()
        self.put("Frontend/react/package.json", '{"dependencies": {"left-pad": "1.3.0"}}\n')
        self.put("Frontend/react/src/pages/Alerts.test.tsx", "it.skip('renders', () => { expect(1).toBe(1) })\n")
        self.put("web/templates/base.html", "<body class='new'></body>\n")
        self.put("brand/family/SHA256SUMS", "bbb  tokens.json\n")
        self.put("src/new_module.py", "X = 1\n")
        facts = self.facts(seen=["web/templates/**", "Frontend/react/src/**/*.tsx", "*.css"], protected=["brand/family/**", ".github/workflows/CICD.yml"])
        self.assertEqual(len(facts["files"]), 8)
        self.assertEqual(facts["tests_deleted"], ["tests/test_gone.py"])
        self.assertEqual(sorted(facts["tests_changed"]), ["Frontend/react/src/pages/Alerts.test.tsx", "tests/test_app.py"])
        # test_app.py lost one of its two assertions; test_gone.py went with its only one; the .tsx line was rewritten (one out, one in).
        self.assertEqual(facts["assertions"], {"added": 1, "removed": 3})
        self.assertEqual(
            sorted((e["path"], e["line"]) for e in facts["switched_off"]),
            [
                ("Frontend/react/src/pages/Alerts.test.tsx", "it.skip('renders', () => { expect(1).toBe(1) })"),
                ("src/app.py", "return 'new'  # noqa"),
                ("tests/test_app.py", "@pytest.mark.skip(reason='later')"),
            ],
        )
        self.assertEqual(facts["manifests"], ["Frontend/react/package.json"])
        self.assertEqual(sorted(facts["seen"]), ["Frontend/react/src/pages/Alerts.test.tsx", "web/templates/base.html"])
        self.assertEqual(facts["protected"], ["brand/family/SHA256SUMS"])
        text = review.facts_text(facts)
        self.assertIn("8 files changed", text)
        self.assertIn("`src/new_module.py` (added)", text)
        self.assertIn("Test files deleted: `tests/test_gone.py`.", text)
        self.assertIn("  - `tests/test_app.py`: `@pytest.mark.skip(reason='later')`", text)
        self.assertIn("Dependency manifests or lockfiles changed: `Frontend/react/package.json`.", text)
        self.assertIn("says a person sees: ", text)
        self.assertIn("says no item may change: `brand/family/SHA256SUMS`.", text)
        self.assertNotIn("the manager has recorded", text)
        # Where the manager exempted this item for a path (`hl-items exempt`), the reviewer is told, so it is not taken for a breach.
        facts["exempted"] = {"brand/family/SHA256SUMS": "the item vendors the {{new}} brand"}
        self.assertIn("- Of those, the manager has recorded that this item alone may change `brand/family/SHA256SUMS` (the item vendors the { {new}} brand).", review.facts_text(facts))

    def test_a_plain_change_has_plain_facts(self):
        self.put("docs/notes.md", "notes, with more\n")
        facts = self.facts(seen=["web/**"], protected=["brand/**"])
        self.assertEqual((facts["files"], facts["insertions"], facts["deletions"]), ([{"status": "M", "path": "docs/notes.md"}], 1, 1))
        for key in ("tests_changed", "tests_deleted", "switched_off", "manifests", "seen", "protected"):
            self.assertEqual(facts[key], [], key)
        self.assertEqual(facts["unmeasured"], {})
        self.assertEqual(review.facts_text(facts), "- 1 files changed, 1 lines added and 1 removed: `docs/notes.md`.")

    def test_a_line_that_looks_like_a_patchs_own_header_is_counted_as_a_line(self):
        # Found in review: a removed SQL comment is `--- note` in a patch and an added `++ x` is `+++ x`; read as
        # the start of a file, neither was counted, and every line after the second lost the file it was in.
        self.put("db/schema.sql", "".join(f"-- note {i}\n" for i in range(30)) + "create table t (id int);\n")
        self.put("tests/test_app.py", "def test_page():\n    assert True\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "base of the change")
        git(self.repo, "push", "origin", "HEAD:main")
        self.put("db/schema.sql", "create table t (id int);\n")
        self.put("tests/test_app.py", "++ marker\nimport pytest\n\n\n@pytest.mark.skip\ndef test_page():\n    assert True\n    assert 1\n")
        facts = self.facts()
        self.assertEqual((facts["insertions"], facts["deletions"], facts["unmeasured"]), (6, 30, {}))
        self.assertEqual(facts["switched_off"], [{"path": "tests/test_app.py", "line": "@pytest.mark.skip"}])
        self.assertEqual(facts["assertions"], {"added": 1, "removed": 0})

    def test_the_lines_of_a_patch_are_read_by_the_counts_in_its_hunks(self):
        patch = "\n".join(
            [
                "diff --git a/m.sql b/m.sql",
                "--- a/m.sql",
                "+++ b/m.sql",
                "@@ -1,2 +1 @@",
                "--- gone",
                "-- also gone",
                "\\ No newline at end of file",
                "+++ b/not-a-file",
                "@@ -9 +8,0 @@ create table t",
                "-last",
                "diff --git a/old.txt b/old.txt",
                "deleted file mode 100644",
                "--- a/old.txt",
                "+++ /dev/null",
                "@@ -1 +0,0 @@",
                "-bye",
                "",
            ]
        )
        self.assertEqual(
            list(review.changed_lines(patch)),
            [("-", "m.sql", "-- gone"), ("-", "m.sql", "- also gone"), ("+", "m.sql", "++ b/not-a-file"), ("-", "m.sql", "last"), ("-", None, "bye")],
        )
        # A hunk that ends before its header said it would: what follows is read as what it is, the next file.
        short = "\n".join(["--- a/x", "+++ b/x", "@@ -1,3 +0,0 @@", "-a", "diff --git a/y b/y", "--- a/y", "+++ b/y", "@@ -1 +1 @@", "-b", "+c"])
        self.assertEqual(list(review.changed_lines(short)), [("-", "x", "a"), ("-", "y", "b"), ("+", "y", "c")])

    def test_a_file_no_count_of_lines_measures_is_named(self):
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40)
        # A repository inside the repository is added as a pointer to one of its commits, as a submodule is.
        inner = self.repo / "vendor" / "lib"
        inner.mkdir(parents=True)
        git(inner, "init", "-q")
        (inner / "lib.py").write_text("x = 1\n" * 500)
        git(inner, "add", "lib.py")
        git(inner, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a library")
        self.put("docs/notes.md", "notes, with more\n")
        facts = self.facts()
        self.assertEqual(facts["unmeasured"], {"logo.png": "a binary file", "vendor/lib": "a submodule's pointer"})
        # The binary file adds no line; the pointer's one line is counted and says nothing.
        self.assertEqual((facts["insertions"], facts["deletions"]), (2, 1))
        self.assertIn("- Changes the line counts above do not show: `logo.png` (a binary file), `vendor/lib` (a submodule's pointer).", review.facts_text(facts))
        # Facts written by a kit before this one have no such list, and read as they did.
        del facts["unmeasured"]
        self.assertNotIn("do not show", review.facts_text(facts))
        # A pointer that is taken out is as little measured as one that is put in.
        git(self.repo, "push", "origin", "HEAD:main")
        shutil.rmtree(self.repo / "vendor")
        self.assertEqual(self.facts()["unmeasured"], {"vendor/lib": "a submodule's pointer"})

    def test_a_change_of_mode_or_of_kind_and_a_file_with_no_line_in_it_are_named(self):
        # Found in review: each of these rode along beside a change of text, counted as no line at all.
        self.put("empty-before.txt", "")
        self.put("bin/run", "#!/bin/sh\n")
        self.put("was-text.dat", "plain text\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "base of the change")
        git(self.repo, "push", "origin", "HEAD:main")
        os.chmod(self.repo / "src/app.py", 0o755)
        (self.repo / "empty-before.txt").unlink()
        self.put("empty-now.txt", "")
        (self.repo / "bin/run").unlink()
        os.symlink("../src/app.py", self.repo / "bin/run")
        (self.repo / "was-text.dat").write_bytes(b"\x00\x01\x02 no longer text\n" * 50)
        self.put("docs/notes.md", "notes, with more\n")
        facts = self.facts()
        self.assertEqual(
            facts["unmeasured"],
            {
                "bin/run": "its mode, 100644 to 120000",
                "empty-before.txt": "deleted with no line in it",
                "empty-now.txt": "added with no line in it",
                "src/app.py": "its mode, 100644 to 100755",
                "was-text.dat": "a binary file",
            },
        )
        # A file that is added or deleted with lines in it, and one whose text alone changed, are measured.
        self.assertEqual([f["path"] for f in facts["files"] if f["path"] not in facts["unmeasured"]], ["docs/notes.md"])
        self.assertEqual(review._unmeasured("000000", "100644", "A", (3, 0)), None)
        self.assertEqual(review._unmeasured("100644", "000000", "D", (0, 3)), None)

    def test_a_name_with_a_tab_a_newline_or_a_space_in_it_is_counted_under_its_own_name(self):
        odd = ("tab\there.py", "line\nbreak.py", "tests/test with space.py", "*.py")
        for name in odd:
            self.put(name, "x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "base of the change")
        git(self.repo, "push", "origin", "HEAD:main")
        for name in odd:
            self.put(name, "import pytest\n\n\n@pytest.mark.skip\ndef test_x():\n    assert True\n")
        facts = self.facts()
        self.assertEqual(sorted(f["path"] for f in facts["files"]), sorted(odd))
        self.assertEqual((facts["insertions"], facts["deletions"], facts["unmeasured"]), (24, 4, {}))
        # A line the diff adds is laid to the file it is in: by its plain name, or as git wrote a name it had to escape.
        self.assertEqual(sorted(entry["path"] for entry in facts["switched_off"]), ["*.py", "line\\nbreak.py", "tab\\there.py", "tests/test with space.py"])
        self.assertEqual(facts["tests_changed"], ["tests/test with space.py"])
        self.assertEqual(facts["assertions"], {"added": 1, "removed": 0})
        # Each file's lines are asked of git by the file's name as it is: `*.py` is one file, not every file that ends so.
        shown = review.fix_files(self.tools, self.repo, "origin/main", "HEAD")
        self.assertEqual([(f["path"], len(f["lines"])) for f in sorted(shown, key=lambda f: f["path"])], [(name, 7) for name in sorted(odd)])

    def test_a_file_that_was_renamed_counts_as_one_removed_and_one_added(self):
        git(self.repo, "mv", "src/app.py", "src/application.py")
        facts = self.facts()
        self.assertEqual(facts["files"], [{"status": "D", "path": "src/app.py"}, {"status": "A", "path": "src/application.py"}])
        self.assertEqual((facts["insertions"], facts["deletions"]), (2, 2))

    def test_the_facts_are_of_the_pull_request_not_of_what_the_base_gained_since(self):
        self.put("docs/notes.md", "branch change\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "A-1")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "--detach", "origin/main")
        self.put("src/other.py", "Y = 2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "another item merged")
        git(self.repo, "push", "origin", "HEAD:main")
        facts = review.diff_facts(self.tools, self.repo, "origin/main", head)
        self.assertEqual([f["path"] for f in facts["files"]], ["docs/notes.md"])

    def test_a_path_that_is_not_plain_ascii_is_matched_as_it_is(self):
        # Found in review: git quotes such a path unless asked not to, and a quoted path matched no pattern.
        self.put("brand/café/tokens.json", "{}\n")
        self.put("docs/with space.md", "x\n")
        facts = self.facts(protected=["brand/café/**"], seen=["docs/*.md"])
        self.assertEqual(sorted(f["path"] for f in facts["files"]), ["brand/café/tokens.json", "docs/with space.md"])
        self.assertEqual((facts["protected"], facts["seen"]), (["brand/café/tokens.json"], ["docs/with space.md"]))

    def test_a_directory_named_plainly_covers_what_is_under_it(self):
        # Found in review: `dist/` and a bare `infra` protected nothing.
        cases = [
            ("brand/family/", "brand/family/SHA256SUMS", True),
            ("brand/family/", "brand/family/desk/tokens.css", True),
            ("brand/family/", "brand/familyx/a", False),
            ("infra", "infra/base/main.tf", True),
            ("infra", "infrastructure/a", False),
            ("infra", "docs/infra", True),
            ("pyproject.toml", "pyproject.toml", True),
            ("pyproject.toml", "sub/pyproject.toml", True),
            ("web/*.html", "web/a/b.html", False),
            ("web/*", "web/a.html", True),
            ("", "anything", False),
            ("/", "anything", False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(bool(review.matching([path], [pattern])), expected, (pattern, path))

    def test_a_pattern_is_read_the_way_an_ignore_file_reads_it(self):
        # Found in review: `brand/*` left `brand/a/b` unprotected, and a bare name covered only a directory at the root.
        cases = [
            ("brand/*", "brand/a/b", True),
            ("web/*", "web/a/b.html", True),
            ("brand/*", "brandy/a", False),
            ("gen", "x/gen/y.py", True),
            ("gen", "x/generated/y.py", False),
            ("infra", "x/infra/y", True),
            ("docs/*.md", "docs/a/b.md", False),
            # A leading slash ties the pattern to the root.
            ("/dist", "dist/a.js", True),
            ("/dist", "web/dist/a.js", False),
            ("/dist/", "dist/a/b.js", True),
            # A class of characters, and the same brackets as characters of a path.
            ("src/[ab]*.py", "src/a1.py", True),
            ("src/[ab]*.py", "src/c1.py", False),
            ("src/[!a]*.py", "src/b.py", True),
            ("src/[!a]*.py", "src/a.py", False),
            ("src/[a-c].py", "src/b.py", True),
            ("src/[a-c].py", "src/d.py", False),
            ("src/[ab]", "src/a/x.py", True),
            ("app/[id]/page.tsx", "app/[id]/page.tsx", True),
            ("app/[id]/**", "app/[id]/x/y.tsx", True),
            ("app/[id]/page.tsx", "app/x/page.tsx", False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(bool(review.matching([path], [pattern])), expected, (pattern, path))

    def test_globs_cross_directories_only_where_they_say_so(self):
        cases = [
            ("web/**", "web/a/b.html", True),
            ("web/**", "website/a.html", False),
            ("web/*.html", "web/a/b.html", False),
            ("web/*.html", "web/a.html", True),
            ("**/*.css", "a/b/c.css", True),
            ("**/*.css", "c.css", True),
            ("*.css", "a/b/c.css", True),
            ("src/**/*.tsx", "src/a.tsx", True),
            ("src/**/*.tsx", "src/x/y/a.tsx", True),
            ("src/**/*.tsx", "src/a.ts", False),
            ("brand/family/SHA256SUMS", "brand/family/SHA256SUMS", True),
            ("brand/family/SHA256SUMS", "brand/family/SHA256SUMS.bak", False),
            ("a?.txt", "ab.txt", True),
            ("a?.txt", "a/b.txt", False),
        ]
        for pattern, path, expected in cases:
            self.assertEqual(bool(review.matching([path], [pattern])), expected, (pattern, path))


class CheckoutTest(RunCase):
    """What a review did to its checkout: its state when the review began against its state when the review is over."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "checkout"
        git(self.tmp, "init", "-q", "-b", "main", str(self.repo))
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        for name in ("a.txt", "b.txt", "c.txt", "run.sh", "old name.txt", *[f"many/{i}.txt" for i in range(10)]):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(f"{name}\n")
        os.symlink("a.txt", self.repo / "link")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.tools = merge.Tools(sleep=lambda s: None)

    def state(self):
        return review.checkout_state(self.tools, self.repo)

    def test_a_clean_checkout_has_nothing_that_differs_and_untracked_files_are_not_its_business(self):
        (self.repo / "scratch.log").write_text("left by a command\n")
        self.assertEqual(self.state(), {"head": self.head, "dirty": {}})

    def test_every_way_a_tracked_file_can_differ_is_seen_and_told_apart(self):
        (self.repo / "a.txt").write_text("edited\n")
        (self.repo / "b.txt").unlink()
        os.chmod(self.repo / "run.sh", 0o755)
        (self.repo / "link").unlink()
        os.symlink("c.txt", self.repo / "link")
        git(self.repo, "mv", "old name.txt", "new\tname.txt")
        first = self.state()
        self.assertEqual(sorted(first["dirty"]), ["a.txt", "b.txt", "link", "new\tname.txt", "run.sh"])
        self.assertEqual((first["dirty"]["b.txt"], first["dirty"]["link"]), ("gone", "link to c.txt"))
        self.assertTrue(first["dirty"]["run.sh"].startswith("runnable "))
        self.assertEqual(self.state(), first)
        # Each of these is the same file differing in another way than before.
        (self.repo / "a.txt").write_text("edited again\n")
        os.chmod(self.repo / "run.sh", 0o644)
        (self.repo / "run.sh").write_text("#!/bin/sh\n")
        (self.repo / "link").unlink()
        os.symlink("a.txt", self.repo / "elsewhere")
        os.symlink("elsewhere", self.repo / "link")
        second = self.state()
        self.assertEqual([path for path in sorted(first["dirty"]) if first["dirty"][path] != second["dirty"].get(path)], ["a.txt", "link", "run.sh"])

    def mark(self, path, *marks):
        """Tell git not to look at a tracked file, with one command for each mark."""
        for mark in marks:
            git(self.repo, "update-index", mark, "--", path)

    def test_a_file_git_was_told_not_to_look_at_counts_as_changed_and_its_content_is_still_told_apart(self):
        # Found in the fourth review: under either mark `git status` lists nothing, so an edit there was in no state at all.
        marks = {"a.txt": ("--skip-worktree",), "b.txt": ("--assume-unchanged",), "c.txt": ("--skip-worktree", "--assume-unchanged"), "run.sh": ("--skip-worktree",)}
        for path, marked in marks.items():
            self.mark(path, *marked)
        self.assertEqual(git(self.repo, "ls-files", "-v", "--", *marks), "S a.txt\nh b.txt\ns c.txt\nS run.sh")
        # The mark alone, on a file that is as the commit has it: nobody can say so while it is there.
        untouched = self.state()
        self.assertEqual(sorted(untouched["dirty"]), sorted(marks))
        self.assertTrue(all(value.startswith("hidden from git, ") for value in untouched["dirty"].values()))
        for path in ("a.txt", "b.txt", "c.txt"):
            (self.repo / path).write_text("edited where git does not look\n")
        (self.repo / "run.sh").unlink()
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        edited = self.state()
        self.assertEqual(sorted(edited["dirty"]), sorted(marks))
        self.assertEqual(edited["dirty"]["run.sh"], "hidden from git, gone")
        self.assertEqual([path for path in sorted(marks) if untouched["dirty"][path] != edited["dirty"][path]], sorted(marks))
        # A file that was plainly changed and is then hidden reads as changed again: it is not the state it was in.
        (self.repo / "old name.txt").write_text("edited in plain sight\n")
        plain = self.state()["dirty"]["old name.txt"]
        self.mark("old name.txt", "--assume-unchanged")
        self.assertEqual(self.state()["dirty"]["old name.txt"], "hidden from git, " + plain)

    def test_what_a_sparse_checkout_left_out_counts_like_any_file_git_does_not_look_at(self):
        # No clone the kit makes is sparse, and a worker can switch one on: nothing is let through for it.
        git(self.repo, "sparse-checkout", "set", "--no-cone", "/*", "!/many/")
        self.assertFalse((self.repo / "many").exists())
        state = self.state()
        self.assertEqual(sorted(state["dirty"]), [f"many/{i}.txt" for i in range(10)])
        self.assertEqual(set(state["dirty"].values()), {"hidden from git, gone"})

    def test_a_submodule_that_differs_is_put_back_by_the_commands_the_refusal_names(self):
        # Found in the fifth review: `git reset --hard HEAD` leaves what is inside a submodule as it is, and the refusal named nothing else.
        def sub(cwd, *args):
            argv = ["git", "-c", "protocol.file.allow=always", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args]
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

        lib = self.tmp / "lib"
        sub(self.tmp, "init", "-q", "-b", "main", str(lib))
        (lib / "lib.py").write_text("x = 1\n")
        sub(lib, "add", "."), sub(lib, "commit", "-q", "-m", "lib")
        sub(self.repo, "submodule", "add", "-q", str(lib), "vendor/lib"), sub(self.repo, "commit", "-q", "-m", "with a submodule")
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.state()["dirty"], {})
        (self.repo / "vendor/lib/lib.py").write_text("x = 2  # rewritten\n")
        (self.repo / "a.txt").write_text("edited\n")
        state = self.state()
        self.assertEqual((sorted(state["dirty"]), state["dirty"]["vendor/lib"]), (["a.txt", "vendor/lib"], "a directory"))
        said = review.begins_changed(state, self.repo, "A-1", head, "main")
        commands = re.findall(r"`(git [^`]+)`", said)
        self.assertEqual(commands, [f"git -C {self.repo} reset --hard HEAD", f"git -C {self.repo} submodule update --init --force --recursive"])
        subprocess.run(commands[0], shell=True, check=True, capture_output=True)
        self.assertEqual(sorted(self.state()["dirty"]), ["vendor/lib"])
        subprocess.run(commands[1], shell=True, check=True, capture_output=True)
        self.assertEqual((self.state()["dirty"], (self.repo / "vendor/lib/lib.py").read_text()), ({}, "x = 1\n"))
        self.assertIsNone(review.begins_changed(self.state(), self.repo, "A-1", head, "main"))

    def test_a_checkout_at_another_commit_with_no_branch_on_record_is_put_back_detached(self):
        (self.repo / "a.txt").write_text("another commit\n")
        git(self.repo, "commit", "-q", "-am", "second")
        said = review.begins_changed(self.state(), self.repo, "A-1", self.head, None)
        self.assertIn(f"Put the checkout back on that commit (`git -C {self.repo} checkout --detach {self.head}`; it leaves untracked files alone)", said)
        # Where the commit under review is not known, only the files are asked about.
        self.assertIsNone(review.begins_changed(self.state(), self.repo, "A-1", None, None))
        self.assertIsNone(review.begins_changed(None, self.repo, "A-1", self.head, "main"))

    def test_the_files_the_spec_names_as_rewritten_by_the_repository_are_told_from_the_rest(self):
        (self.repo / "a.txt").write_text("edited\n")
        (self.repo / "many/3.txt").write_text("edited\n")
        (self.repo / "run.sh").write_text("edited\n")
        state = self.state()
        self.assertEqual(review.unnamed_changes(state, None), ["a.txt", "many/3.txt", "run.sh"])
        self.assertEqual(review.unnamed_changes(state, ["many/", "*.sh"]), ["a.txt"])
        self.assertEqual(review.unnamed_changes(None, ["many/"]), [])

    def test_where_there_is_no_checkout_to_read_there_is_no_state(self):
        for nowhere in (None, "", self.tmp / "gone", self.tmp):
            self.assertIsNone(review.checkout_state(self.tools, nowhere), nowhere)

    def test_a_problem_is_what_changed_between_the_start_and_the_end(self):
        (self.repo / "a.txt").write_text("rewritten before the review began\n")
        (self.repo / "b.txt").write_text("rewritten before the review began\n")
        (self.repo / "c.txt").write_text("rewritten before the review began\n")
        before = self.state()
        self.assertEqual(review.checkout_read(before, self.state(), self.head), {"checkout": [], "dirty_before": ["a.txt", "b.txt", "c.txt"]})
        (self.repo / "b.txt").write_text("rewritten again\n")
        git(self.repo, "checkout", "--", "c.txt")
        (self.repo / "run.sh").write_text("newly edited\n")
        read = review.checkout_read(before, self.state(), self.head)
        self.assertEqual(read, {"checkout": ["tracked files were changed in the checkout during the review: b.txt, c.txt, run.sh"], "dirty_before": ["a.txt"]})
        # With no state noted at its start, a review is held to a clean checkout, as every review once was.
        strict = review.checkout_read(None, self.state(), self.head)
        self.assertEqual(strict, {"checkout": ["tracked files were changed in the checkout during the review: a.txt, b.txt, run.sh"], "dirty_before": []})
        # No checkout at the end: nothing to say of it.
        self.assertEqual(review.checkout_read(before, None, self.head), {"checkout": [], "dirty_before": []})

    def test_a_checkout_at_another_commit_and_a_long_list_of_files(self):
        for i in range(10):
            (self.repo / f"many/{i}.txt").write_text("edited\n")
        read = review.checkout_read({"head": self.head, "dirty": {}}, self.state(), "f" * 40)
        self.assertEqual(
            read["checkout"],
            [
                f"the checkout moved during the review: it is at {self.head[:12]}, the commit under review is ffffffffffff",
                "tracked files were changed in the checkout during the review: " + ", ".join(f"many/{i}.txt" for i in range(8)),
            ],
        )
