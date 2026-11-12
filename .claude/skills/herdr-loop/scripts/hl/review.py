"""What a review is given, and whether it did its work.

    diff_facts  what a pull request's diff touches, computed from git and
                never taken from the builder: the files, the tests among
                them, checks switched off or loosened, dependency manifests,
                paths a person sees, paths no item may change
    exercise    what the reviewer's own transcript shows it did: commands
                that ran something, calls on the served build, a browser on
                the served pages

What `exercise` can and cannot say: it is a floor. It catches a review that
ran nothing, never called the build that was served for it, or never opened
a page it was required to look at. It cannot show that a review was
thorough, and it will pass a command that ran something beside the point.
A check it cannot see through (a test runner that takes the served address
from its own config file) reads as no call, and that review is run again.

A review that reads the code is still only a review if it exercised the
change. Reading `git diff` proves nothing about what the build does, so a
shell call is not enough by itself: a command counts when it runs something,
and where the build is served, something has to have called it.

Nothing here judges the change. The facts go into the reviewer's brief, and
the reading of its transcript goes to the merge gate, which refuses a pass
that did not exercise the change.

A delta confirm (`hl/confirm.py`) is read the same way, and for two things
more. Its transcript must show the reviewer was shown the fix as the fix's
own diff: for every file the fix changes, one of the results its own
commands printed back must hold the line git prints for that file in a
diff of exactly these two commits, and with it every line the fix adds to
the file or removes from it. And where the failed review looked at pages,
the confirm must have looked at each of them again.

`checkout_state` and `checkout_read` are what a review did to its checkout:
the tracked files that differed from the commit when the review began, and
what had changed by the time it was over.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from . import herdr as herdr_mod, items
from .common import Refusal, read_json

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|e2e|specs?)/|(^|/)test_[^/]+\.py$|(^|/)conftest\.py$|_test\.(py|go|rb)$|\.(test|spec)\.[cm]?[jt]sx?$"
)
MANIFEST_RE = re.compile(
    r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|pyproject\.toml|poetry\.lock|uv\.lock"
    r"|Pipfile|Pipfile\.lock|go\.mod|go\.sum|Cargo\.toml|Cargo\.lock|Gemfile|Gemfile\.lock|requirements[^/]*\.txt|\.nvmrc|\.python-version)$"
)
# A line added by the diff that turns a check off or loosens one. Each is
# legitimate somewhere; the reviewer is shown them and decides.
SWITCHED_OFF_RE = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail|importorskip)\(|@unittest\.(skip|skipIf|skipUnless|expectedFailure)"
    r"|\b(it|test|describe)\.(skip|todo|only|failing|fixme)\b|\bx(it|describe|test)\("
    r"|#\s*(pragma:\s*no cover|noqa|type:\s*ignore)|eslint-disable|@ts-(ignore|expect-error|nocheck)|istanbul ignore"
    r"|--no-verify|continue-on-error:\s*true|\|\|\s*true\b|--passWithNoTests|-k\s+['\"]?not\b|--deselect|--ignore[= ]"
)
ASSERTION_RE = re.compile(r"\bassert\b|\bexpect\(|\bself\.assert\w+\(|\bassert[A-Z]\w*\(|\.should\b|\bt\.(Error|Fatal)")
SHOWN = 40


def _glob_re(pattern: str, rooted: bool = False, classes: bool = True) -> re.Pattern:
    """`**` crosses directories; `*`, `?` and a `[…]` class do not. A pattern
    with no slash matches the name in any directory, unless it is rooted."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif classes and pattern[i] == "[" and "]" in pattern[i + 2 :]:
            end = pattern.index("]", i + 2)
            inner = pattern[i + 1 : end]
            negate = inner[:1] in ("!", "^")
            out.append("[" + ("^/" if negate else "") + re.escape(inner[1:] if negate else inner).replace("\\-", "-") + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    body = "".join(out)
    return re.compile(("(?:.*/)?" if "/" not in pattern and not rooted else "") + body + r"\Z")


def _covers(pattern: str, path: str) -> bool:
    """Whether a spec's pattern covers a path, read the way an ignore file
    is: a pattern that matches a directory covers everything under it. So
    `brand/family/`, `infra` and `brand/*` each cover what lies below what
    they match, and a `protected` entry written any of those ways does not
    protect nothing. A leading slash ties the pattern to the root."""
    pattern = pattern.strip()
    if not pattern:
        return False
    rooted = pattern.startswith("/")
    bare = pattern.strip("/")
    # A bracket is a class of characters, and it is also a character some paths hold (`app/[id]/page.tsx`): either reading covers.
    readings = [_glob_re(bare, rooted=rooted)] + ([_glob_re(bare, rooted=rooted, classes=False)] if "[" in bare else [])
    parts = path.split("/")
    return any(regex.match("/".join(parts[:k])) for regex in readings for k in range(1, len(parts) + 1))


def matching(paths: list[str], patterns) -> list[str]:
    return [path for path in paths if any(_covers(p, path) for p in patterns or [])]


def diff_facts(tools, checkout, base_ref: str, head: str, repo_facts: dict | None = None) -> dict:
    """What the change from the merge base to `head` touches. `repo_facts` is
    the repository's entry in program.json: `seen` and `protected` are glob
    lists its spec declares."""
    repo_facts = repo_facts or {}
    merge_base = tools.git(["merge-base", base_ref, head], checkout).stdout.strip()
    # -z: git otherwise quotes a path that is not plain ASCII, and a quoted path matches no glob.
    raw = tools.git(["diff", "--raw", "--no-abbrev", "--no-renames", "-z", merge_base, head], checkout).stdout.split("\0")
    # Each file is `:<mode before> <mode after> <blob before> <blob after> <status>`, then its path.
    entries = [(raw[i].split(), raw[i + 1]) for i in range(0, len(raw) - 1, 2) if raw[i + 1]]
    files = [{"status": meta[4][:1], "path": path} for meta, path in entries]
    paths = [f["path"] for f in files]
    tests = [f for f in files if TEST_PATH_RE.search(f["path"])]

    # The lines are git's own count, file by file. Where that count is not the size of a file's change, the
    # file is named with why: no number of lines says what such a change is.
    counts: dict = {}
    for counted in tools.git(["diff", "--numstat", "--no-renames", "-z", merge_base, head], checkout).stdout.split("\0"):
        if counted:
            added, removed, path = counted.split("\t", 2)
            counts[path] = None if added == "-" else (int(added), int(removed))
    insertions, deletions = (sum(lines[k] for lines in counts.values() if lines) for k in (0, 1))
    unmeasured = {path: why for (meta, path) in entries for why in [_unmeasured(meta[0].lstrip(":"), meta[1], meta[4][:1], counts.get(path))] if why}

    switched_off: list[dict] = []
    removed_asserts = added_asserts = 0
    patch = tools.git(["-c", "core.quotePath=false", "diff", "--no-color", "--no-renames", "-U0", merge_base, head], checkout).stdout
    for sign, current, text in changed_lines(patch):
        if sign == "+":
            if current and SWITCHED_OFF_RE.search(text):
                switched_off.append({"path": current, "line": text.strip()[:200]})
            if current and TEST_PATH_RE.search(current) and ASSERTION_RE.search(text):
                added_asserts += 1
        # A removed line's file is the one the hunk is in; a deleted file has no "+++ b/" name.
        elif ASSERTION_RE.search(text) and (current is None or TEST_PATH_RE.search(current)):
            removed_asserts += 1
    return {
        "base": merge_base,
        "head": head,
        "files": files,
        "insertions": insertions,
        "deletions": deletions,
        "unmeasured": dict(sorted(unmeasured.items())),
        "tests_changed": [f["path"] for f in tests if f["status"] != "D"],
        "tests_deleted": [f["path"] for f in tests if f["status"] == "D"],
        "assertions": {"added": added_asserts, "removed": removed_asserts},
        "switched_off": switched_off,
        "manifests": [p for p in paths if MANIFEST_RE.search(p)],
        "seen": matching(paths, repo_facts.get("seen")),
        "protected": matching(paths, repo_facts.get("protected")),
    }


def _unmeasured(before: str, after: str, status: str, lines) -> str | None:
    """Why a count of lines is not the size of one file's change, or None
    when it is. `before` and `after` are the file's modes, `lines` what git
    counted as added and removed, or None where it counted nothing."""
    if GITLINK in (before, after):
        return "a submodule's pointer"
    if lines is None:
        return "a binary file"
    if status in "AD":
        return None if any(lines) else ("added with no line in it" if status == "A" else "deleted with no line in it")
    return f"its mode, {before} to {after}" if before != after else None


def _named(name: str) -> str | None:
    """The file a patch's `+++` line names, None for one the patch deletes.
    Git ends a name that holds a space with a tab, and puts one that holds a
    tab, a newline or a quote between quotes with those escaped: the name
    is kept as git wrote it, without the quotes."""
    name = name.rstrip("\t")
    if name == "/dev/null":
        return None
    name = name[1:-1] if name.startswith('"') and name.endswith('"') else name
    return name[2:] if name.startswith("b/") else name


def changed_lines(patch: str):
    """The lines a patch made with `-U0` adds and removes, each as its sign,
    the file it is in (None for a file the patch deletes) and its text.

    Read by the counts in each hunk's header and not by how a line begins:
    a line of the change may itself begin `--- ` or `+++ ` (a removed SQL
    comment is `--- note` in a patch), and that is a line, not the start of
    a file."""
    current = None
    old = new = 0
    for line in patch.split("\n"):
        if (old or new) and line.startswith(("+", "-", "\\")):
            if line.startswith("-") and old:
                old -= 1
                yield "-", current, line[1:]
            elif line.startswith("+") and new:
                new -= 1
                yield "+", current, line[1:]
            # What is left is `\ No newline at end of file`, which belongs to the line before it.
            continue
        old = new = 0
        hunk = _HUNK_RE.match(line)
        if hunk:
            old, new = (1 if count is None else int(count) for count in hunk.groups())
        elif line.startswith("+++ "):
            current = _named(line[4:])


def facts_text(facts: dict) -> str:
    """The facts as a reviewer reads them."""

    def listed(values, each=lambda v: f"`{v}`") -> str:
        shown = ", ".join(each(v) for v in values[:SHOWN])
        return shown + (f", and {len(values) - SHOWN} more" if len(values) > SHOWN else "")

    lines = [
        f"{len(facts['files'])} files changed, {facts['insertions']} lines added and {facts['deletions']} removed: "
        + listed(facts["files"], lambda f: f"`{f['path']}`" + ("" if f["status"] == "M" else f" ({_STATUS.get(f['status'], f['status'])})"))
        + "."
    ]
    if facts["tests_deleted"]:
        lines.append(f"Test files deleted: {listed(facts['tests_deleted'])}.")
    if facts["tests_changed"]:
        counts = facts["assertions"]
        lines.append(
            f"Test files added or changed: {listed(facts['tests_changed'])}. In test files the diff adds {counts['added']} "
            f"lines that assert something and removes {counts['removed']}."
        )
    elif facts["assertions"]["removed"]:
        lines.append(f"The diff removes {facts['assertions']['removed']} lines that assert something.")
    if facts["switched_off"]:
        lines.append("Lines the diff adds that switch a check off or loosen one:")
        lines += [f"  - `{entry['path']}`: `{entry['line']}`" for entry in facts["switched_off"][:SHOWN]]
    if facts.get("unmeasured"):
        lines.append("Changes the line counts above do not show: " + listed(list(facts["unmeasured"].items()), lambda named: f"`{named[0]}` ({named[1]})") + ".")
    if facts["manifests"]:
        lines.append(f"Dependency manifests or lockfiles changed: {listed(facts['manifests'])}.")
    if facts["seen"]:
        lines.append(f"Changed paths this repository's spec says a person sees: {listed(facts['seen'])}.")
    if facts["protected"]:
        lines.append(f"Changed paths this repository's spec says no item may change: {listed(facts['protected'])}.")
    if facts.get("exempted"):
        named = [f"`{path}` ({' '.join(str(why or '').split()).replace('{{', '{ {')})" for path, why in facts["exempted"].items()]
        lines.append(f"Of those, the manager has recorded that this item alone may change {', '.join(named)}.")
    return "\n".join(f"- {line}" if not line.startswith("  ") else line for line in lines)


_STATUS = {"A": "added", "D": "deleted", "M": "changed", "T": "type changed"}


# ---------------------------------------------------------------- exercise

# Programs that only read, or only move files about. A command that starts
# with one of these exercised nothing; neither did any `git` or `gh` command,
# whatever it did. Anything else ran something.
READING = frozenset(
    """cat ls head tail less more grep egrep fgrep rg ag fd wc cut sort uniq tr echo printf pwd cd pushd popd test [ [[ true false
    mkdir touch cp mv rm ln chmod tee jq yq sed awk diff cmp comm file stat du df tree which type date sleep wait basename dirname
    realpath readlink export set unset local declare typeset readonly read shift exit return break continue : open xxd od strings
    column nl paste rev fold md5 md5sum shasum sha256sum base64 source . bat eza exa view zcat gzcat hexdump env printenv hash git gh""".split()
)
# Programs that take their program from a here-document or a string: what that text calls, the command calls.
INTERPRETERS = frozenset({"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "bash", "sh", "zsh"})
DECLARERS = frozenset({"export", "local", "declare", "typeset", "readonly"})
# For these, -v asks the version; for most programs it means "say more".
V_IS_VERSION = frozenset({"node", "npm", "npx", "yarn", "pnpm", "bun", "deno"})
# Words a shell allows in front of a command, which are not the command.
PREFIXES = frozenset({"time", "if", "then", "else", "elif", "do", "while", "until", "!", "{", "exec", "command", "builtin", "noglob", "nohup", "nice", "sudo", "caffeinate"})
TIMEOUTS = frozenset({"timeout", "gtimeout"})
# Words that open or close a compound and run nothing themselves.
NOT_COMMANDS = frozenset({"for", "select", "case", "function", "done", "fi", "esac", "}", "in"})
ONLY_ASKS = frozenset({"--version", "-V", "--help", "-h", "version", "help"})
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
BROWSER_RE = re.compile(r"^mcp__(?:.*_)?playwright(?:_.*)?__(browser_\w+)$")
BROWSER_PASSIVE = ("browser_resize", "browser_close", "browser_install", "browser_tabs", "browser_console_messages", "browser_network_requests")
# The browser calls whose `filename` names a script they run; on any other, a `filename` is where the call writes its output.
BROWSER_SCRIPT_FILE = ("browser_run_code", "browser_run_code_unsafe")
IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
# The start of a hunk in a patch, with how many lines it removes and adds (one, where git leaves the count out).
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# The line of a file's patch that names the file's blob before and after, each cut as short as git likes.
_INDEX_RE = re.compile(r"^index ([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})(?: \d+)?$")
# What the file tool, and `cat -n`, put in front of each line they print: its number.
_NUMBERED_RE = re.compile(r"^\s*\d+[\t\u2192]")
# Where one file's part of a diff begins, as git prints it.
SECTION = "diff --git "
# How the digest of a file begins when git was told not to look at the file, and what takes each such mark off.
HIDDEN = "hidden from git, "
UNMARK = ("--no-skip-worktree", "--no-assume-unchanged")
# What a path holds when it is a directory git tracks as one thing: a submodule.
DIRECTORY = "a directory"
# A submodule's entry in a tree: its change is one line in a diff, and any amount of code.
GITLINK = "160000"


def shell_commands(script: str) -> list[list[str]]:
    """The simple commands of a shell script, each as its words.

    Quotes are honoured: a `|`, `;` or `&&` inside them is part of a word,
    not a break between commands. A command substitution, in `$( )` or in
    backticks, is a command of its own. The body of a here-document is text,
    not commands. This is not a shell: it reads enough to say which programs
    a command line ran."""
    commands: list[list[str]] = []

    def parse(text: str, i: int, closer: str | None) -> int:
        words: list[str] = []
        word: list[str] | None = None

        def end_word():
            nonlocal word
            if word is not None:
                words.append("".join(word))
                word = None

        def end_command():
            end_word()
            if words:
                commands.append(list(words))
                words.clear()

        n = len(text)
        while i < n:
            c = text[i]
            if closer and text.startswith(closer, i):
                end_command()
                return i + len(closer)
            if c == "\\" and i + 1 < n:
                word = (word or []) + [text[i + 1]]
                i += 2
            elif c == "'":
                j = text.find("'", i + 1)
                j = n if j < 0 else j
                word = (word or []) + [text[i + 1 : j]]
                i = j + 1
            elif c == '"':
                word = word or []
                i += 1
                while i < n and text[i] != '"':
                    if text[i] == "\\" and i + 1 < n:
                        word.append(text[i + 1])
                        i += 2
                    elif text.startswith("$((", i):
                        j = text.find("))", i)
                        i = n if j < 0 else j + 2
                    elif text.startswith("$(", i):
                        i = parse(text, i + 2, ")")
                    elif text[i] == "`":
                        i = parse(text, i + 1, "`")
                    else:
                        word.append(text[i])
                        i += 1
                i += 1
            elif text.startswith("$((", i):
                # Arithmetic: it computes, and runs nothing.
                j = text.find("))", i)
                i = n if j < 0 else j + 2
                word = word or []
            elif text.startswith("$(", i):
                # What it prints becomes part of the outer command; what it runs is a command of its own.
                i = parse(text, i + 2, ")")
                word = word or []
            elif c == "`" and closer != "`":
                i = parse(text, i + 1, "`")
                word = word or []
            elif c == "#" and word is None:
                while i < n and text[i] != "\n":
                    i += 1
            elif c in ";\n" or text.startswith("&&", i) or text.startswith("||", i):
                end_command()
                i += 2 if text[i : i + 2] in ("&&", "||") else 1
            elif c == "|":
                end_command()
                i += 1
            elif c == "&" and not (i + 1 < n and text[i + 1] == ">") and not (i > 0 and text[i - 1] == ">"):
                end_command()
                i += 1
            elif c in "()":
                end_command()
                i += 1
            elif c.isspace():
                end_word()
                i += 1
            else:
                word = (word or []) + [c]
                i += 1
        end_command()
        return i

    parse(split_heredocs(script or "")[0], 0, None)
    return commands


def _heredoc_marks(line: str, quote: str | None) -> tuple[list[str], str | None]:
    """The here-documents a line opens, read as a shell reads the line: a `<<`
    inside quotes, a comment or arithmetic opens nothing. `quote` is the
    quote the lines before left open; the one this line leaves open is
    returned with the markers."""
    marks: list[str] = []
    i, n, at_word_start = 0, len(line), True
    while i < n:
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\":
            i, at_word_start = i + 2, False
        elif c in "'\"":
            quote, i, at_word_start = c, i + 1, False
        elif c == "#" and at_word_start:
            break
        elif line.startswith("((", i):
            j = line.find("))", i)
            i = n if j < 0 else j + 2
        elif line.startswith("<<", i):
            # A here-string (`<<<`) does not match: no name follows its first two brackets.
            found = _HEREDOC_RE.match(line, i)
            if found:
                marks.append(found.group(2))
            i = found.end() if found else i + 2
            at_word_start = False
        else:
            at_word_start = c in " \t;|&("
            i += 1
    return marks, quote


def split_heredocs(script: str) -> tuple[str, list[tuple[str, str]]]:
    """A script without the bodies of its here-documents, and each body with
    the line that opened it."""
    lines = script.split("\n")
    kept, bodies, i, quote = [], [], 0, None
    while i < len(lines):
        opener = lines[i]
        kept.append(opener)
        marks, quote = _heredoc_marks(opener, quote)
        i += 1
        for mark in marks:
            body = []
            while i < len(lines) and lines[i].strip() != mark:
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append((opener, "\n".join(body)))
    return "\n".join(kept), bodies


# A function a script defines: `name() {` or `function name {`. Its body is read where it is defined.
_FUNCTION_RE = re.compile(r"(?:^|[;&|\n({])\s*(?:function\s+([A-Za-z_][\w-]*)\s*(?:\(\s*\))?|([A-Za-z_][\w-]*)\s*\(\s*\))\s*\{")


def script_commands(script: str) -> list[list[str]]:
    """The commands a script runs, with the variables it set filled in and
    without the calls to functions it defined itself: what such a function
    runs was read where it was defined."""
    text = split_heredocs(script or "")[0]
    defined = {a or b for a, b in _FUNCTION_RE.findall(text)}
    return [words for words in _expanded(shell_commands(script)) if not (words and words[0] in defined)]


def written_by(script: str) -> dict[str, str]:
    """The files a script writes from a here-document, by name, with the text."""
    out: dict[str, str] = {}
    for opener, body in split_heredocs(script or "")[1]:
        for words in shell_commands(opener):
            found = _program(words)
            if found is None:
                continue
            targets = [w.lstrip(">") or (words[k + 1] if k + 1 < len(words) else "") for k, w in enumerate(words) if w.startswith(">")]
            if found[0] == "tee":
                targets += [a for a in found[1] if not a.startswith(("-", "<", ">"))]
            for target in targets:
                if target:
                    out[Path(target).name] = out.get(Path(target).name, "") + body + "\n"
    return out


def _program(words: list[str]) -> tuple[str, list[str]] | None:
    """The program a command runs and its arguments, past what a shell lets
    stand in front of it. None when the words run nothing."""
    words = list(words)
    while words:
        first = words[0]
        if first == "command" and len(words) > 1 and words[1] in ("-v", "-V"):
            # It asks where a program is; it does not run it.
            return None
        if first == "function":
            words = words[2:]
        elif _ASSIGN_RE.match(first) or first in PREFIXES or (first.startswith("-") and len(words) > 1):
            words = words[1:]
        elif Path(first).name in TIMEOUTS:
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[0])):
                words = words[1:]
        else:
            break
    if not words or words[0] in NOT_COMMANDS:
        return None
    # `.` (the shell's own `source`) has no name as a path.
    return Path(words[0]).name or words[0], words[1:]


def _runs(words: list[str]) -> bool:
    found = _program(words)
    if found is None:
        return False
    program, args = found
    if program == "xargs":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            # -I and -n take a value.
            rest = rest[2:] if rest[0] in ("-I", "-n", "-P", "-L", "-d", "-s") else rest[1:]
        return _runs(rest)
    if program == "find":
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in args:
                return _runs(args[args.index(flag) + 1 :])
        return False
    if program == "env":
        # `env FOO=1 node x.js` runs node; `env` alone prints.
        return _runs([a for a in args if not a.startswith("-")])
    if program in READING:
        return False
    if args and all(a in ONLY_ASKS or (a == "-v" and program in V_IS_VERSION) for a in args):
        return False
    return True


def _expanded(commands: list[list[str]]) -> list[list[str]]:
    """The commands with `$NAME` replaced where the same script assigned it,
    so `PORT=8401; curl 127.0.0.1:$PORT/` reads as the call it makes."""
    known: dict[str, str] = {}
    out = []
    for words in commands:
        seen = [_VAR_RE.sub(lambda m: known.get(m.group(1) or m.group(2), m.group(0)), w) for w in words]
        # `PORT=8401 …`, and `export PORT=8401`.
        for w in seen[1:] if seen and seen[0] in DECLARERS else seen:
            m = _ASSIGN_RE.match(w)
            if not m:
                break
            known[m.group(1)] = m.group(2)
        out.append(seen)
    return out


def ran_something(command: str) -> bool:
    """Whether a shell command line executed anything beyond reading."""
    return any(_runs(words) for words in script_commands(command))


def _origin_re(port: int) -> re.Pattern:
    # `host:port`, and `host port` as two words (`nc -z localhost 8401`).
    return re.compile(rf"(?<![\w.-])(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(?::|\s+){int(port)}(?!\d)")


def calls_served(command: str, port: int, written: dict[str, str] | None = None) -> bool:
    """Whether a command that ran something named the served build's address:
    in its own words, in a here-document it fed to an interpreter, or in a
    file the session wrote and this command runs (a script that requests a
    page is the reviewer calling the build). `written` is those files so
    far, by name; what this command writes from a here-document is added."""
    origin = _origin_re(port)
    files = written if written is not None else {}
    for name, text in written_by(command).items():
        files[name] = files.get(name, "") + text
    for words in script_commands(command):
        if _runs(words) and (origin.search(" ".join(words)) or any(origin.search(files.get(Path(w).name, "")) for w in words[1:])):
            return True
    for opener, body in split_heredocs(command or "")[1]:
        fed = any((_program(words) or ("", []))[0] in INTERPRETERS for words in shell_commands(opener))
        if fed and origin.search(body):
            return True
    return False


def _records(path: Path):
    """Each message of a transcript that holds a list of blocks: tool calls, and what came back from them."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                yield [block for block in content if isinstance(block, dict)]


def shown_results(transcript: Path) -> list:
    """What a session's own tool calls printed back to it: for each result,
    its lines in the sections a diff has, one for each file. A section
    begins at git's `diff --git` line and runs to the next; what comes
    before the first is one too. This is what the session was shown, as
    its transcript holds it (the text of each tool result, in the shape
    Claude Code 2.1.296 writes). A command whose output went to a file or
    to nowhere showed nothing, and neither did the part of an output that
    was cut off. A line is kept without what trails it, and one the file
    tool or `cat -n` numbered without its number, so a patch written to a
    file and then read counts as the patch."""
    results = []
    for blocks in _records(transcript):
        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            parts = [content] if isinstance(content, str) else [part.get("text") for part in content or [] if isinstance(part, dict)]
            sections: list = [set()]
            for text in parts:
                for line in str(text or "").split("\n"):
                    line = _NUMBERED_RE.sub("", line, count=1).rstrip()
                    if line.startswith(SECTION):
                        sections.append(set())
                    sections[-1].add(line)
            results.append(sections)
    return results


def fix_files(tools, checkout, start: str, head: str) -> list[dict]:
    """The change from `start` to `head`, file by file: the file's blob at
    each of the two commits, and every line the change adds to it or
    removes from it as a diff prints the line (its sign, then its text).
    Raises RuntimeError when git cannot read the two commits."""
    raw = tools.git(["diff", "--raw", "--no-abbrev", "--no-renames", "-z", start, head], checkout).stdout.split("\0")
    files = []
    for i in range(0, len(raw) - 1, 2):
        meta, path = raw[i].split(), raw[i + 1]
        # One file at a time, by its name as it is: nothing is read from a patch's own headers.
        argv = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0", start, head, "--", f":(literal){path}"]
        lines = [sign + text.rstrip() for sign, _, text in changed_lines(tools.git(argv, checkout).stdout)]
        files.append({"path": path, "before": meta[2], "after": meta[3], "lines": lines})
    return files


def moved_files(tools, checkout, start: str, head: str) -> list[str]:
    """The files of a change that git, asked to look for both, reads as
    renamed or copied: each as `old to new`. A diff that follows a rename
    prints one `index` line and the lines that differ between the two
    names; the fix as `fix_files` reads it, with no rename followed, is
    one file deleted and one added. Raises RuntimeError when git cannot
    read the two commits."""
    raw = tools.git(["diff", "--raw", "-z", "-M", "-C", start, head], checkout).stdout.split("\0")
    moved = []
    while len(raw) > 1:
        # A renamed or copied file is `:<modes> <blobs> R<score>` or `C<score>`, then the name it had, then its name now.
        if raw[0].split()[4][:1] in ("R", "C"):
            moved.append(f"`{raw[1]}` to `{raw[2]}`")
            raw = raw[3:]
        else:
            raw = raw[2:]
    return moved


def shows(result: list, file: dict) -> bool:
    """Whether one tool result holds a file's part of the fix as the fix's
    own diff: a section of it (`shown_results`) with git's
    `index <blob before>..<blob after>` line for the file, which no diff of
    any other pair of commits prints for it, and in that same section
    every line the fix adds to the file or removes from it. Without the
    first, the same lines in a diff of something else would count: a fix
    that repeats a line is otherwise shown by the diff that first added
    it. Held to one section, because the `index` line of this diff and the
    lines of another may come back in one result (`git diff A B | head -3;
    git diff C D`). A section with two `index` lines is not one file's
    part of one diff, and shows nothing."""
    for section in result:
        found = [m for m in (_INDEX_RE.match(line) for line in section) if m]
        named = len(found) == 1 and file["before"].startswith(found[0].group(1)) and file["after"].startswith(found[0].group(2))
        if named and all(line in section for line in file["lines"]):
            return True
    return False


def hidden_files(tools, worktree) -> list[str]:
    """The tracked files of a checkout that git has been told not to look
    at: marked assume-unchanged or skip-worktree (`git update-index`, or a
    sparse checkout, which sets the second on every file it leaves out).
    `git status` leaves such a file out whatever was done to it. So the
    mark itself counts as a change wherever a checkout is held to its
    commit, whatever the file holds: while it is there, no script and no
    worker's own `git status` can say the file is as the commit has it.
    Nothing is left out for a sparse checkout: no clone the kit makes is
    one, and a worker can switch one on."""
    listed = tools.git(["ls-files", "-v", "-z"], worktree).stdout.split("\0")
    # `-v` tags each file: `S` for skip-worktree, a small letter for assume-unchanged, `s` for both.
    return sorted({entry[2:] for entry in listed if entry[:1] == "S" or entry[:1].islower()})


def checkout_state(tools, worktree) -> dict | None:
    """Where a checkout is, and which of its tracked files differ from that
    commit, each with a digest of what it holds now. A file git was told
    not to look at (`hidden_files`) is one of them, and its digest says
    so. None when there is no checkout to read."""
    if not worktree or not Path(worktree).is_dir():
        return None
    at = tools.git(["rev-parse", "HEAD"], worktree, check=False)
    status = tools.git(["status", "--porcelain=v1", "-z", "--untracked-files=no"], worktree, check=False)
    if at.returncode != 0 or status.returncode != 0:
        return None
    dirty = {}
    entries = status.stdout.split("\0")
    while entries:
        entry = entries.pop(0)
        if len(entry) > 3:
            if entry[0] in "RC":
                # A rename or a copy in the index names the path it came from next.
                entries.pop(0)
            dirty[entry[3:]] = _holds(Path(worktree) / entry[3:])
    for path in hidden_files(tools, worktree):
        dirty[path] = HIDDEN + _holds(Path(worktree) / path)
    return {"head": at.stdout.strip(), "dirty": dirty}


def begins_changed(state: dict | None, worktree, item_id: str, head: str | None = None, branch: str | None = None, tools=None) -> str | None:
    """Why a review may not begin on a checkout in this state: it is at
    another commit than the one under review (`head`), or tracked files in
    it differ from the commit it is at. Whoever left it so (a review before
    this one that switched commits to compare, or edited a file; a hand),
    the reviewer would run a build that is not the commit, and the read at
    the end would take the files for something that was so before the
    review. Asked before the build is served. The commands it names put
    the checkout back, in the order they are to be run: `branch` is the
    item's own, which the checkout is put back on. One case is not put
    back: a checkout that has gone on from the commit under review (the
    base merged in, a commit pushed) is a new head, and `hl-flow built`
    takes it up."""
    if not state:
        return None
    moved = bool(head) and state["head"] != head
    paths = sorted(state["dirty"])
    if not paths and not moved:
        return None
    # Asked of git: where the checkout is at what the item's branch holds on the remote, the branch has gone on (its base
    # merged in and pushed), and putting it back would take the item behind its own pull request. A commit that
    # was made here and never pushed is not that: `hl-flow built` would refuse it, and the checkout is put back.
    on_remote = tools.git(["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"], worktree, check=False).stdout.strip() if moved and branch and tools is not None else None
    ahead = on_remote == state["head"]
    shown = ", ".join(paths[:8]) + (f" and {len(paths) - 8} more" if len(paths) > 8 else "")
    hidden = [path for path in paths if state["dirty"][path].startswith(HIDDEN)]
    marks = ""
    commands = []
    if hidden:
        # A sparse checkout is switched off first. `reset --hard` leaves a file under skip-worktree as it is, and git clears one mark for each command, never both.
        named = ", ".join(hidden[:8]) + (f" and {len(hidden) - 8} more" if len(hidden) > 8 else "")
        marks = (
            f" Git was told not to look at {len(hidden)} of them ({named}: marked assume-unchanged or skip-worktree, by hand or by a sparse checkout, "
            "which a loop's worktree may not be), and such a file counts as changed whatever it holds."
        )
        commands.append(f"`git -C {worktree} sparse-checkout disable`")
        commands += [f"`git -C {worktree} ls-files -z | git -C {worktree} update-index {clear} -z --stdin`" for clear in UNMARK]
    if paths:
        commands.append(f"`git -C {worktree} reset --hard HEAD`")
    if any(state["dirty"][path].endswith(DIRECTORY) for path in paths):
        # A submodule: the reset above leaves what is inside it as it is.
        commands.append(f"`git -C {worktree} submodule update --init --force --recursive`")
    if ahead:
        commands.append(f"`hl-flow built --id {item_id}`")
    elif moved:
        commands.append(f"`git -C {worktree} checkout -B {branch} {head}`" if branch else f"`git -C {worktree} checkout --detach {head}`")
    way = ", ".join(commands[:-1]) + (", then " if commands[:-1] else "") + commands[-1]
    differ = f"tracked files in it differ from {'the commit it is at' if moved else 'that commit'} ({shown})"
    elsewhere = f"it is at {state['head'][:12]}, {'which is what its branch holds on the remote, ' if ahead else ''}and the commit under review is {str(head)[:12]}"
    if ahead:
        put = "The branch has gone on since its build was accepted (its base merged in, or a commit pushed): take the new head up"
    else:
        put = f"Put the {'checkout back on that commit' if moved else 'files back as the commit has them'}"
    return (
        f"the checkout of {item_id} is not the commit under review: {'; '.join(([elsewhere] if moved else []) + ([differ] if paths else []))}.{marks} A review is of what is "
        f"committed, so none was started and none is spent. {put} ({way}{'; it leaves untracked files alone' if paths or not ahead else ''}) "
        "and run this again. What an earlier review's read found is on its attempt, and this does not change it"
    )


def unnamed_changes(state: dict | None, rewrites) -> list[str]:
    """The tracked files that differ from the commit as a review's reviewer
    is about to start, and that the program's spec does not name as paths
    the repository rewrites by itself (`rewrites`, patterns read as an
    ignore file reads them). Whatever is here was written by the build's
    own Serve or Ready line, or by the setup run again: the reviewer would
    be given a build that is not the commit."""
    paths = sorted(state["dirty"]) if state else []
    named = set(matching(paths, rewrites))
    return [path for path in paths if path not in named]


def _holds(path: Path) -> str:
    """What a file is now, as one value: enough to say whether it changed."""
    if path.is_symlink():
        return "link to " + os.readlink(path)
    if path.is_dir():
        return DIRECTORY
    if not path.is_file():
        return "gone"
    return ("runnable " if os.access(path, os.X_OK) else "") + hashlib.sha256(path.read_bytes()).hexdigest()


def checkout_read(before: dict | None, after: dict | None, head: str | None, worktree=None) -> dict:
    """What a review did to its checkout, read when the review is over:
    `after` against `before`, the state noted when the review began.

    A problem is what changed while the review ran: a checkout that is not
    at the commit under review, and a tracked file that differs from that
    commit in another way than it did at the start (newly, differently, or
    no longer). A file that already differed when the review began and is
    as it was then is named apart: something before the review rewrote it,
    and no review can be asked to answer for that. A review with no state
    noted at its start is held to a clean checkout. A checkout on record
    (`worktree`) that cannot be read counts as one that changed."""
    if after is None:
        # No checkout on record: nothing to read. One on record that cannot be read has not been shown to be as it
        # was, and that is what goes on the attempt: made again later, it would read clean.
        gone = [f"the checkout ({worktree}) could not be read when the review was over, so nothing shows it was left as it was found"] if worktree else []
        return {"checkout": gone, "dirty_before": []}
    was = (before or {}).get("dirty") or {}
    changed = sorted(path for path in {*was, *after["dirty"]} if was.get(path) != after["dirty"].get(path))
    problems = []
    if after["head"] != head:
        problems.append(f"the checkout moved during the review: it is at {after['head'][:12]}, the commit under review is {str(head)[:12]}")
    if changed:
        problems.append("tracked files were changed in the checkout during the review: " + ", ".join(changed[:8]))
    return {"checkout": problems, "dirty_before": sorted(path for path in after["dirty"] if path not in changed)}


def _tool_calls(path: Path):
    for blocks in _records(path):
        for block in blocks:
            if block.get("type") == "tool_use":
                yield str(block.get("name", "")), block.get("input") if isinstance(block.get("input"), dict) else {}


def session_transcripts(session: str) -> list[Path]:
    """A Claude session's transcript and those of its sub-agents."""
    main = herdr_mod.claude_transcript(session)
    if main is None:
        return []
    return [main] + sorted((main.parent / session / "subagents").glob("*.jsonl"))


# The most of a browser script on disk that is read for the page it opens. A
# script is a few KB; a file larger than this is not read at all.
SCRIPT_FILE_MAX = 256 * 1024
# A word that sends a command's output to a file: `>`, `>>`, `>|`, `1>`, `&>`.
_REDIRECT_RE = re.compile(r"^(\d*|&)(>>|>\|?)(.*)$", re.DOTALL)


def _absolute(name, cwd: str | None = None) -> str | None:
    """`name` as an absolute path, a relative one taken from `cwd`. None when
    that cannot be told: no name, a variable left unfilled, a relative name
    with no directory known."""
    if not isinstance(name, str) or not name or "$" in name or "\0" in name:
        return None
    if not os.path.isabs(name):
        if not cwd:
            return None
        name = os.path.join(cwd, name)
    return os.path.normpath(name)


def _redirects(args: list[str]) -> tuple[list[str], list[tuple[str, str, str]]]:
    """A command's arguments apart from its redirections to files, and those
    redirections as (descriptor, operator, target). A here-document's word
    and a descriptor duplicated (`2>&1`) are neither."""
    plain, out, skip = [], [], False
    for k, word in enumerate(args):
        if skip:
            skip = False
            continue
        if word.startswith("<"):
            # `<<EOF`, `<< EOF`, `< in.txt`: input, never an argument.
            skip = word in ("<", "<<", "<<-", "<<<")
            continue
        found = _REDIRECT_RE.match(word)
        if not found:
            plain.append(word)
            continue
        target = found.group(3)
        if not target:
            target, skip = (args[k + 1] if k + 1 < len(args) else ""), True
        if target and not target.startswith("&"):
            out.append((found.group(1), found.group(2), target))
    return plain, out


def shell_writes(command: str, files: dict[str, str]) -> None:
    """Bring `files` (absolute path to the text the session last put there)
    up to date with what a shell command line writes: `cat` or `tee` from a
    here-document or from files already known, a redirection to a file, a
    copy or move (`cp`, `mv`), a removal (`rm`). Variables the line set are
    filled in, and a relative name is taken from the directory a `cd` on the
    same line moved to. A file written in a way that does not tell what it
    holds is dropped from `files`, so it is read from disk instead; nothing
    here guesses at its text."""
    bodies = [body for _, body in split_heredocs(command or "")[1]]
    commands = _expanded(shell_commands(command))
    marks = sum(1 for words in commands for w in words if w.startswith("<<") and not w.startswith("<<<"))
    # Each here-document is fed to the command that opened it, in order. Where
    # the two cannot be paired one to one, no body is taken for any of them.
    queue = bodies if marks == len(bodies) else []
    cwd = None
    for words in commands:
        fed = None
        for w in words:
            if w.startswith("<<") and not w.startswith("<<<"):
                fed = queue.pop(0) if queue else None
        found = _program(words)
        if found is None:
            continue
        program, args = found
        plain, redirects = _redirects(args)
        names = [a for a in plain if not a.startswith("-")]
        if program == "cd":
            cwd = _absolute(names[0], cwd) if names else None
            continue
        # What the command prints: known only for `cat` (and `tee`) of a here-document or of files already known.
        printed = None
        if program in ("cat", "tee") and fed is not None and (program == "tee" or not names):
            printed = fed
        elif program == "cat" and names and fed is None:
            sources = [files.get(_absolute(n, cwd) or "") for n in names]
            printed = "".join(sources) if all(s is not None for s in sources) else None
        targets = list(redirects)
        if program == "tee":
            targets += [("", ">>" if "-a" in plain or "--append" in plain else ">", n) for n in names]
        for fd, op, target in targets:
            where = _absolute(target, cwd)
            if where is None:
                continue
            text = printed if fd in ("", "1", "&") else None
            # Added to a file whose text is not known, the whole is not known either.
            before = files.get(where) if op == ">>" else ""
            if text is None or before is None:
                files.pop(where, None)
            else:
                files[where] = before + text + "\n"
        if program in ("cp", "mv") and len(names) >= 2:
            *sources, dest = names
            into = dest.endswith("/") or len(sources) > 1
            for source in sources:
                src = _absolute(source, cwd)
                dst = _absolute(os.path.join(dest, os.path.basename(source)) if into else dest, cwd)
                if dst is None:
                    continue
                if src is not None and src in files:
                    files[dst] = files[src]
                else:
                    files.pop(dst, None)
                if program == "mv" and src is not None:
                    files.pop(src, None)
        elif program == "rm":
            for name in names:
                where = _absolute(name, cwd)
                if where is not None:
                    files.pop(where, None)


def _file_edited(files: dict[str, str], tool: str, given: dict) -> None:
    """Bring `files` up to date with a file tool's call: `Write` sets the
    text, an edit changes the text already known. An edit to a text that is
    not known, or that does not hold what it replaces, drops the file, so it
    is read from disk instead."""
    where = _absolute(given.get("file_path"))
    if where is None:
        return
    if tool == "Write":
        files[where] = str(given.get("content") or "")
        return
    edits = given.get("edits") if tool == "MultiEdit" else [given]
    text = files.get(where)
    for edit in edits if isinstance(edits, list) else [None]:
        old = edit.get("old_string") if isinstance(edit, dict) else None
        if text is None or not isinstance(old, str) or not old or old not in text:
            files.pop(where, None)
            return
        new = str(edit.get("new_string") or "")
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    files[where] = text


def _file_text(path: str) -> str:
    """The text of a regular file of at most SCRIPT_FILE_MAX bytes on disk.
    Empty for anything else: missing, unreadable, larger, or not a regular
    file (a FIFO is opened without waiting on it, then refused)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except (OSError, ValueError):
        return ""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > SCRIPT_FILE_MAX:
            return ""
        data, chunk = b"", b"-"
        while chunk and len(data) <= SCRIPT_FILE_MAX:
            chunk = os.read(fd, SCRIPT_FILE_MAX + 1 - len(data))
            data += chunk
    except OSError:
        return ""
    finally:
        os.close(fd)
    return "" if len(data) > SCRIPT_FILE_MAX else data.decode("utf-8", errors="replace")


def script_file(filename, files: dict[str, str]) -> str:
    """The text of the script a browser call names as a `filename` instead
    of passing its code: what the session's transcript shows it last put at
    that path, or else what is on disk there now. Empty when the name is not
    an absolute path (the browser server resolves a relative one against a
    root of its own, which cannot be told here) or nothing can be read."""
    where = _absolute(filename)
    if where is None:
        return ""
    return files[where] if where in files else _file_text(where)


def exercise(transcripts: list[Path], port: int | None = None) -> dict:
    """What the transcripts show was done. `port` is the served build's, when one was served.

    A browser call that runs a script from a file (`filename`) is read for
    that file's text, as an inline script is read for its `code`. Only the
    run_code calls are: on any other, `filename` is where the call writes its
    output, and naming it opens no page."""
    origin = _origin_re(port) if port else None
    out = {"shell_calls": 0, "ran": 0, "browser_actions": 0, "navigations": 0, "served_calls": 0, "served_pages": 0, "sub_agents": max(len(transcripts) - 1, 0)}
    for path in transcripts:
        # What the session wrote to files, by name: a probe written with the file tool and then run is a call like any other.
        written: dict[str, str] = {}
        # And by absolute path, the text each file last held: what a browser call that names a script file ran.
        files: dict[str, str] = {}
        for name, given in _tool_calls(path):
            browser = BROWSER_RE.match(name)
            if name in ("Write", "Edit", "MultiEdit"):
                _file_edited(files, name, given)
            if name in ("Write", "Edit"):
                target = Path(str(given.get("file_path", ""))).name
                if target:
                    written[target] = written.get(target, "") + str(given.get("content") or given.get("new_string") or "") + "\n"
            elif name == "Bash":
                shell_writes(str(given.get("command", "")), files)
                out["shell_calls"] += 1
                command = str(given.get("command", ""))
                called = bool(port) and calls_served(command, port, written)
                if ran_something(command):
                    out["ran"] += 1
                    if called:
                        out["served_calls"] += 1
            elif browser:
                action = browser.group(1)
                if action in BROWSER_PASSIVE:
                    continue
                out["browser_actions"] += 1
                text = json.dumps(given)
                if action in BROWSER_SCRIPT_FILE and "filename" in given:
                    text += "\n" + script_file(given.get("filename"), files)
                opened = action == "browser_navigate" or "page.goto(" in text or ".goto(" in text
                if opened:
                    out["navigations"] += 1
                    if origin and origin.search(text):
                        out["served_pages"] += 1
    return out


def _stamp_epoch(stamp: str | None) -> float | None:
    from datetime import datetime, timezone

    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if stamp else None
    except ValueError:
        return None


def _is_image(path: Path) -> bool:
    """A PNG with a header that gives it a size, or a JPEG: a file that only begins like a picture is not one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
        size = path.stat().st_size
    except OSError:
        return False
    if head.startswith(IMAGE_MAGIC[0]):
        return len(head) == 24 and head[12:16] == b"IHDR" and min(int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) >= 16 and size > 200
    return head.startswith(IMAGE_MAGIC[1]) and size > 200


def _page(look: dict) -> tuple[str, str]:
    """A look as the mode and the page it was of, whatever host and port the page was served on."""
    url = urlsplit(str(look.get("url", "")))
    return str(look.get("mode", "")).strip().lower(), (url.path.rstrip("/") or "/") + (f"?{url.query}" if url.query else "")


def recorded_looks(item_dir, n: int) -> list[dict]:
    """The looks the verdict of review `n` records."""
    try:
        data = json.loads((Path(item_dir) / f"verdict-{n}.json").read_text())
    except (OSError, ValueError):
        return []
    looks = data.get("looks") if isinstance(data, dict) else None
    return [look for look in looks if isinstance(look, dict)] if isinstance(looks, list) else []


def check(run_dir, item_id: str, n: int, tools=None, checkout=None) -> dict:
    """Read review `n` of an item against what its transcript shows.

    `problems` is empty when the review exercised the change the way the
    item's route requires. A served build must have been called. An item a
    person looks at must have been opened in the browser on the served
    build, and every look the verdict records must cite a picture that
    exists.

    A delta confirm is held to the same, and its transcript must show the
    fix was printed to its reviewer: the fix is taken from git in
    `checkout` with `tools`, and each file's part of it must be in one
    result of the reviewer's own session, as this fix's diff (`shows`).
    That it was printed is all this says: nothing here can tell whether it
    was read. What a sub-agent was shown, the reviewer was not. The pages
    the failed review looked at are pages a person looks at whatever the
    route says: the confirm must have looked at each again, in the same
    mode."""
    item = items.load_item(run_dir, item_id)
    d = items.item_dir(run_dir, item_id)
    entry = next(
        (e for e in items.load_attempts(run_dir, item_id)["log"] if e["phase"] == "grade" and e["n"] == n and not e.get("voided")), None
    )
    if entry is None:
        raise Refusal(f"{item_id} has no review {n}")
    transcripts = session_transcripts(entry.get("session") or "")
    serve_path = d / f"serve-{entry['build']}.json"
    served = read_json(serve_path) if serve_path.exists() else {}
    port = served.get("port") if served.get("served") else None
    confirming = entry.get("kind") == items.CONFIRM
    looked_before = sorted({_page(look) for look in recorded_looks(d, entry.get("confirms"))}) if confirming else []
    look_required = bool((item.get("route") or {}).get("look")) or "look" in (item.get("floor") or []) or bool(looked_before)
    out = {"review": n, "session": entry.get("session"), "served": bool(port), "port": port, "look_required": look_required, "problems": []}
    if not transcripts:
        out["problems"].append(f"the reviewer's transcript (session {entry.get('session')}) cannot be found, so nothing shows the change was exercised")
        return {**out, "exercised": False, "looked": False}
    seen = exercise(transcripts, port)
    out.update(seen)
    if confirming:
        start, head = str(entry.get("from_sha")), str(entry.get("head_sha"))
        try:
            files = [f for f in fix_files(tools, checkout, start, head) if f["lines"]] if tools is not None else None
        except RuntimeError:
            files = None
        results = shown_results(transcripts[0])
        unshown = [f["path"] for f in files or [] if not any(shows(result, f) for result in results)]
        out.update(fix_files=len(files or []), fix_files_shown=len(files or []) - len(unshown))
        if files is None:
            out["problems"].append(f"the fix between {start[:12]} and {head[:12]} could not be taken from git, so nothing shows it was printed to the reviewer")
        elif not files:
            out["problems"].append(f"the fix between {start[:12]} and {head[:12]} adds and removes no line: there is nothing a reviewer could have been shown")
        elif unshown:
            out["problems"].append(
                f"the reviewer's own commands showed it the fix's diff of {len(files) - len(unshown)} of the {len(files)} files the fix changes "
                f"between {start[:12]} and {head[:12]} (not of `{unshown[0]}`: no output holds, in one file's part of a diff, that file's `index` "
                "line of this diff and every line the fix adds or removes): nothing shows the whole fix was printed to the reviewer"
            )
    out["exercised"] = seen["ran"] > 0 or seen["browser_actions"] > 0
    if not out["exercised"]:
        out["problems"].append(
            f"the reviewer ran nothing: {seen['shell_calls']} shell calls, all of them reading (git, gh, cat, grep), and no browser action. "
            "A review exercises the change"
        )
    elif port and not (seen["served_calls"] or seen["served_pages"]):
        out["exercised"] = False
        out["problems"].append(f"the build was served on port {port} and nothing in the review called it")

    looks = []
    try:
        data = json.loads((d / f"verdict-{n}.json").read_text())
        looks = data.get("looks") if isinstance(data, dict) and isinstance(data.get("looks"), list) else []
    except (OSError, ValueError):
        pass
    pictures = [look.get("screenshot") for look in looks if isinstance(look, dict) and isinstance(look.get("screenshot"), str)]
    # A picture counts when this review took it: in this review's own evidence directory, and not older than the review.
    began = _stamp_epoch(entry.get("at"))
    own = (d / f"evidence-{n}").resolve()
    real = [p for p in pictures if _is_image(d / p) and own in (d / p).resolve().parents and (began is None or (d / p).stat().st_mtime >= began - 2)]
    out["looks"] = len(looks)
    out["looked"] = bool(port) and seen["served_pages"] > 0 and bool(looks) and len(real) == len(looks)
    if look_required and not out["looked"]:
        if not port:
            why = "nothing was served, so there was nothing to open"
        elif not seen["served_pages"]:
            why = "its transcript shows no page of the served build opened in the browser"
        elif not looks:
            why = "its verdict records no look"
        else:
            why = f"{len(looks) - len(real)} of the {len(looks)} looks its verdict records cite no picture that exists"
        out["problems"].append(f"this item changes something a person looks at, and the review did not look: {why}")
    missed = [page for page in looked_before if page not in {_page(look) for look in looks if isinstance(look, dict)}]
    if missed:
        shown = ", ".join(f"{path} ({mode})" for mode, path in missed)
        out["problems"].append(f"review {entry.get('confirms')} looked at pages this confirm did not look at again: {shown}")
    return out
