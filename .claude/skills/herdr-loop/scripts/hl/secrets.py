"""The program's secrets, read from their store once and kept in memory.

    get <name>     print one secret and nothing else; it is for `$(…)`
    run -- <cmd…>  run a command with the loop's GitHub token in its environment
    notify         tell the operator on the program's ntfy topic
    budget         what a 1Password service account has left of its daily limit,
                   and whether to hold; it says plainly when it does not apply
    status         which secrets are held and how old, and the counters; never a value
    forget         drop everything held, after a secret was rotated
    stop           end the keeper; the next request starts another
    serve          the keeper itself; started on demand, not by hand

The names are fixed and the references are the program's own: `gh` is
`gh_token_ref` in program.json, `claude` is `worker_claude_ref`, `ntfy` is
`ntfy_password_ref`. A `worker_claude_ref` of `-` names no token (Claude
workers run on the program's seat), and `claude` is then refused unread.

A reference is read with the program's `secrets_reader` (a command as a list
of words, the reference added as its last), or else with the kit's own
`hl-read-secret`, which takes `file:`, `op://`, `keychain:` and `env:`
references. A reader prints the value on standard output; when it fails, the
first line it printed on standard error is the reason given.

Why this exists (measured 2026-10-13, on a program whose secrets were all
op:// references read through a 1Password service account). The service
account allows 1,000 requests a day for the whole account. The first real
program made about 100 an hour: its manager read the GitHub token on almost
every shell call, about 40 an hour, because its shell keeps no variable
between calls, and every worker read two tokens in its own pane. After ten
hours the limit was reached, and for the next fifteen nothing that needs a
secret worked: no worker, no merge, and no notice to the operator, whose
channel's password is a secret too. Nothing had warned beforehand. A store
with no such limit still gains: each read is then one process, or one network
call, fewer, and a store that is down stops nothing that is already held.

So one keeper process per loop root reads each secret the first time it is
asked for and answers from memory afterwards. It listens on a Unix socket in
the root that only its own user can open, and what it holds is never written
to a file, logged, or put on a command line. A value is read again once it
is `secrets_ttl_s` old (twelve hours unless program.json says otherwise). A
read that fails is remembered for ten minutes and answered with the same
words without reading again: a failed read may still count against the
limit, and a storm of callers must not spend what is left of it. Nothing
here tries a read a second time by itself.

A value past its lifetime that cannot be read again stays in use, and is
replaced only by a read that succeeds: the store being down or limited must
not stop a program that holds working secrets, and a secret that was revoked
fails where it is used. A first read that fails gives nothing.

The keeper is started by the first caller that finds none, in a session of
its own, and ends by itself after six hours without a request. Two callers
that start one at the same moment end with one: a keeper holds a lock on
`secrets.lock` for as long as it lives, and one that does not get the lock
stands down as soon as the holder answers. A socket left by a keeper that
died is replaced by the next one, which knows it is stale because it holds
that lock. A caller starts a keeper only when nothing listens, which is
before anything was sent; a request that was sent and not answered is never
sent again by itself, because the keeper that took it may have asked the
store.

The scripts use the keeper where program.json says `"secrets": "keeper"`.
With `"direct"`, or without the line, they do what the kit first did: a
manager exports GH_TOKEN itself, a worker pane reads with `hl-read-secret`
each time a worker starts, and the watchdog reads with the program's reader.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import http.client
import json
import math
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import program, reap
from .common import Refusal, absolute, emit, read_json, utc_now

# What a caller may ask for, and the setting in program.json that names its reference.
NAMES = {"gh": "gh_token_ref", "claude": "worker_claude_ref", "ntfy": "ntfy_password_ref"}
# The kit's own reader, beside the entry points; the reference is its one argument. A program's `secrets_reader`
# takes its place.
READ_SECRET = Path(__file__).resolve().parent.parent / "hl-read-secret"
READ_TIMEOUT_S = 60
# The values `secrets` may have in program.json. `direct` (the default) is the kit's first behaviour.
MODES = ("keeper", "direct")
# Under this many requests left for the day on the service account, reads through it are about to stop.
ACCOUNT_LOW = 50
# How long a failed read is answered from memory, and how long a keeper nobody asks stays.
FAILURE_TTL_S = 10 * 60
IDLE_EXIT_S = 6 * 60 * 60
# A Unix socket's path is at most 104 bytes on macOS, the last of them a terminator.
SOCK_PATH_MAX = 103
# How long a caller waits for a keeper it started, and for an answer: two reads may be ahead of it.
START_WAIT_S = 15
# How often one caller starts a keeper before it says none can be had, and how long a new keeper waits for
# one that is leaving to let go of the lock.
MAX_STARTS = 3
LOCK_WAIT_S = 3
ASK_TIMEOUT_S = 2 * READ_TIMEOUT_S + 30
REQUEST_WAIT_S = 5
# What a keeper is not started with: it holds the program's own, read by reference.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# Where `op` is looked for when the account's limits are read; nothing else of the caller's PATH is given to it.
OP_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
ENTRY = Path(__file__).resolve().parent.parent / "hl-secrets"


class Unreachable(Refusal):
    """No keeper could be reached or started, and nothing was sent to one:
    nothing was asked of the store. A keeper that took a request and did not
    answer it is a plain Refusal, because that one may have asked."""


class _NotThere(Exception):
    """Nothing is listening on the socket."""


# ---------------------------------------------------------------- where things are


def _abs(root) -> Path:
    # As program.load spells it, so that every caller of one root works out one socket path.
    return Path(str(root)).resolve()


def sock_path(root) -> Path:
    path = _abs(root) / "secrets.sock"
    size = len(os.fsencode(str(path)))
    if size > SOCK_PATH_MAX:
        raise Unreachable(
            f"the loop root is too long for the keeper's socket: {path} is {size} bytes, and a Unix socket's path may be "
            f"at most {SOCK_PATH_MAX}. Use a shorter root"
        )
    return path


def _lock_path(root) -> Path:
    return _abs(root) / "secrets.lock"


def _log_path(root) -> Path:
    return _abs(root) / "secrets.log"


def _is_loop_root(root) -> bool:
    return (_abs(root) / "program.json").is_file()


def _log(root, line: str) -> None:
    """What the keeper did, for whoever looks later. Never a value."""
    try:
        fd = os.open(str(_log_path(root)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(f"{utc_now()} pid={os.getpid()} {line}\n")
    except OSError:
        pass


def in_use(cfg: dict) -> bool:
    """Whether this program's scripts get their secrets from the keeper."""
    mode = cfg.get("secrets") or "direct"
    if mode not in MODES:
        raise Refusal(f"program.json: secrets is {mode!r}; it is `keeper` or `direct`")
    return mode == "keeper"


def configured_reader(cfg: dict) -> list[str] | None:
    """The program's own reader (`secrets_reader`), or None for the kit's
    `hl-read-secret`. Raises Refusal when the setting is not a command."""
    reader = cfg.get("secrets_reader")
    if reader is not None and not (isinstance(reader, list) and reader and all(isinstance(part, str) for part in reader)):
        raise Refusal("program.json: secrets_reader is a command as a list of words, or left out")
    return reader


# ---------------------------------------------------------------- one read


def read_secret(ref: str, argv: list[str] | None = None) -> tuple[str | None, str | None]:
    """One read from the store. Returns the value and None, or None and what
    was said instead: the first line the reader printed on standard error.
    `argv` is the reader, with the reference added as its last argument;
    without one it is the kit's `hl-read-secret`."""
    try:
        # A session of its own, so that giving up on it ends everything it started.
        proc = subprocess.Popen(
            [*(argv or [str(READ_SECRET)]), ref], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
    except OSError as error:
        return None, f"the reader could not be run: {error}"
    try:
        out, err = proc.communicate(timeout=READ_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Killing the reader alone would leave a tool it started (`op`, say) asking the store with nobody waiting for the answer.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.communicate(timeout=REQUEST_WAIT_S)
        except subprocess.TimeoutExpired:
            pass
        return None, f"the read did not answer within {READ_TIMEOUT_S} seconds"
    try:
        value = out.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None, f"the read printed something that is not text (exit {proc.returncode})"
    if proc.returncode == 0 and value:
        # A read that worked says nothing, whatever the reader printed beside it.
        return value, None
    # The reader's first line is its reason; whatever follows is detail nobody is shown.
    said = next((" ".join(line.split()) for line in err.decode("utf-8", "replace").splitlines() if line.strip()), "")
    if value:
        # A reader that printed something and then failed: whatever it printed is not shown to anyone.
        said = said.replace(value, "[withheld]")
    return None, said[:300] or f"the read printed nothing (exit {proc.returncode})"


# ---------------------------------------------------------------- what is held


class Keeper:
    """What is held, and the rules for giving it out. There is no socket
    here: `handle` takes a request and returns its answer."""

    def __init__(self, root, reader=read_secret, clock=time.monotonic, wall=time.time):
        self.root = str(_abs(root))
        self.reader = reader
        # Lifetimes and the ten minutes are counted on a clock that cannot be set back; `wall` only says what
        # time of day a moment on it was.
        self.clock = clock
        self.wall = wall
        self.held: dict[str, dict] = {}
        # By name, then by reference: a reference that is changed and changed back finds its own failure still
        # remembered. `named` is the reference each name has now.
        self.failed: dict[str, dict[str, dict]] = {}
        self.named: dict[str, str] = {}
        # How each request ended: a read (some of them failed), a value within its lifetime, a value kept past
        # it, or a failure remembered.
        self.counts = {"reads": 0, "failed_reads": 0, "from_memory": 0, "past_lifetime": 0, "refused_from_memory": 0}
        self.last_read: dict | None = None
        self.since = utc_now()
        self.stopping = False

    def handle(self, request: dict) -> dict:
        op = request.get("op") if isinstance(request, dict) else None
        if op == "get":
            return self.get(str(request.get("name")))
        if op == "status":
            return self.status()
        if op == "forget":
            return self.forget()
        if op == "stop":
            self.stopping = True
            return {"ok": True, "stopped": True, "pid": os.getpid()}
        return {"ok": False, "error": f"the keeper does not know the request {op!r}"}

    def get(self, name: str) -> dict:
        if name not in NAMES:
            return {"ok": False, "error": f"`{name}` is not a secret a program has: {', '.join(NAMES)}"}
        # Read each time: a reference or a lifetime changed in program.json holds from the next request.
        try:
            cfg = program.load(self.root)
        except Exception as broken:
            # Whatever is wrong with the file, nothing is read on its word.
            return {"ok": False, "error": f"the program's settings could not be read: {broken}"}
        ref = cfg.get(NAMES[name])
        if not ref or not isinstance(ref, str):
            return {"ok": False, "error": f"program.json names no reference for `{name}` (`{NAMES[name]}`)"}
        if name == "claude" and ref == program.SIGNED_IN:
            # Not a reference: the store is not asked for it, and no failed read is counted or remembered.
            return {"ok": False, "error": "program.json names no Claude token (`worker_claude_ref` is `-`): Claude workers run on the program's seat"}
        ttl = cfg.get("secrets_ttl_s")
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            # json reads NaN and Infinity as numbers. No age is less than NaN, so with it every request would be a read.
            return {"ok": False, "error": f"program.json: secrets_ttl_s is {ttl!r}; it is a number of seconds above 0"}
        try:
            reader = configured_reader(cfg)
        except Refusal as refusal:
            return {"ok": False, "error": str(refusal)}
        now = self.clock()
        self.named[name] = ref
        kept = self.held.get(name)
        if kept and kept["ref"] != ref:
            # What was read for another reference is never the answer for this one.
            kept = None
        if kept and now - kept["at"] < ttl:
            self.counts["from_memory"] += 1
            return {"ok": True, "value": kept["value"]}
        failure = self.failed.get(name, {}).get(ref)
        if failure and now - failure["at"] < FAILURE_TTL_S:
            if kept:
                # Past its lifetime, and less than ten minutes ago the store would not give it again.
                self.counts["past_lifetime"] += 1
                return {"ok": True, "value": kept["value"]}
            self.counts["refused_from_memory"] += 1
            ago = int(now - failure["at"])
            return {
                "ok": False,
                "error": f"{failure['error']} (the answer of {ago} s ago: a failed read is not tried again for {FAILURE_TTL_S // 60} minutes)",
            }
        self.counts["reads"] += 1
        try:
            value, said = self.reader(ref, reader)
        except Exception as broke:
            # Counted and remembered as a failed read: whether the store was asked before it broke is not known.
            value, said = None, f"the reader broke ({type(broke).__name__})"
        now = self.clock()
        # How the last read went, for `budget`: whether reads work is what a program goes by.
        self.last_read = {"name": name, "ok": bool(value), "at": utc_now()}
        if not value:
            self.counts["failed_reads"] += 1
            error = f"the reader did not give `{name}` ({ref}): {said or 'it said nothing'}"
            self.failed.setdefault(name, {})[ref] = {"error": error, "at": now}
            if kept:
                # The store being down or limited does not take a working secret away. It is replaced by the
                # next read that succeeds; one that was revoked fails where it is used.
                kept.setdefault("past_since", kept["at"] + ttl)
                kept["error"] = error
                self.counts["past_lifetime"] += 1
                _log(self.root, f"read {name}: failed: {said or 'it said nothing'}; the one read {int(now - kept['at'])} s ago stays in use")
                return {"ok": True, "value": kept["value"]}
            self.held.pop(name, None)
            _log(self.root, f"read {name}: failed: {said or 'it said nothing'}")
            return {"ok": False, "error": error}
        self.held[name] = {"value": value, "at": now, "ref": ref}
        _log(self.root, f"read {name}: held")
        return {"ok": True, "value": value}

    def status(self) -> dict:
        now = self.clock()
        return {
            "ok": True,
            "running": True,
            "pid": os.getpid(),
            "since": self.since,
            "held": {name: self._shown(kept, now) for name, kept in sorted(self.held.items())},
            "last_read": self.last_read,
            # What the reference each name has now last failed with, while that is still remembered.
            "failed": {
                name: {"age_s": int(now - failure["at"]), "error": failure["error"]}
                for name in sorted(self.failed)
                for failure in [self.failed[name].get(self.named.get(name))]
                if failure and now - failure["at"] < FAILURE_TTL_S
            },
            **self.counts,
        }

    def _shown(self, kept: dict, now: float) -> dict:
        """What `status` says of one held secret: its age, and when it is in
        use past its lifetime, since when and what the last read said."""
        shown: dict = {"age_s": int(now - kept["at"])}
        if "past_since" in kept:
            since = self.wall() - (now - kept["past_since"])
            shown["past_lifetime_since"] = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            shown["last_error"] = kept["error"]
        return shown

    def forget(self) -> dict:
        dropped = {"ok": True, "forgot": sorted(self.held), "failures_dropped": sorted(name for name, by_ref in self.failed.items() if by_ref)}
        self.held.clear()
        self.failed.clear()
        _log(self.root, f"forgot: {', '.join(dropped['forgot'] + dropped['failures_dropped']) or 'nothing was held'}")
        return dropped


# ---------------------------------------------------------------- the keeper process


def peer_uid(conn) -> int | None:
    """The user at the other end of a connection, as the kernel says. None
    when it cannot be told, which is treated as somebody else."""
    try:
        if sys.platform == "darwin":
            # LOCAL_PEERCRED on SOL_LOCAL gives a struct xucred: a version, then the uid.
            return struct.unpack_from("=II", conn.getsockopt(0, 0x001, 76))[1]
        if hasattr(socket, "SO_PEERCRED"):
            # struct ucred: pid, uid, gid.
            return struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[1]
    except (OSError, struct.error):
        return None
    return None


def _recv_line(conn) -> bytes:
    data = b""
    while not data.endswith(b"\n") and len(data) < 65536:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def _answer(conn, keeper: Keeper) -> None:
    conn.settimeout(REQUEST_WAIT_S)
    uid = peer_uid(conn)
    line = _recv_line(conn)
    if not line:
        # Someone looked whether a keeper listens, and left.
        return
    if uid != os.getuid():
        _log(keeper.root, f"refused a connection from uid {uid}")
        reply = {"ok": False, "error": "this keeper answers only the user it runs as"}
    else:
        try:
            request = json.loads(line)
        except ValueError:
            request = None
        try:
            reply = keeper.handle(request) if request is not None else {"ok": False, "error": "the request was not one JSON object on one line"}
        except Exception as broke:
            # A fault in one request does not end the keeper: what it holds would all be read again.
            _log(keeper.root, f"a request broke the keeper's handling ({type(broke).__name__}); it goes on")
            reply = {"ok": False, "error": f"the keeper could not handle that request ({type(broke).__name__})"}
    # Whose keeper this is. A caller that reached it by a path of another root does not take the answer.
    reply["root"] = keeper.root
    conn.sendall(json.dumps(reply).encode() + b"\n")


def _drain(sock, keeper: Keeper) -> None:
    """Answer whoever connected before the socket's path was removed. A
    keeper that leaves drops nobody: a caller that was dropped could not
    tell whether its request had been read, and so could not ask again."""
    sock.settimeout(0)
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            try:
                _answer(conn, keeper)
            except OSError:
                pass


def _bind(path: Path) -> socket.socket:
    """Listen at `path`, which is this process's to replace: it holds the lock."""
    try:
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise Refusal(f"{path} exists and is not a socket; it was not removed")
        os.unlink(str(path))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Made with its final mode, so there is no moment at which another user could connect.
    old = os.umask(0o177)
    try:
        sock.bind(str(path))
    finally:
        os.umask(old)
    sock.listen(64)
    return sock


def serve(root, idle_s: float = IDLE_EXIT_S, foreground: bool = False) -> int:
    root = _abs(root)
    path = sock_path(root)
    if not _is_loop_root(root):
        raise Refusal(f"{root} holds no program.json: it is not a loop root, and no keeper serves it")
    lock = os.open(str(_lock_path(root)), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            pass
        if _listening(path):
            # Another keeper holds this root and answers: this one is not needed.
            return 0
        if time.monotonic() >= deadline:
            # Neither leaving nor answering: a keeper whose socket was removed under it, or one that is stuck.
            raise Refusal(f"another keeper (pid {_lock_pid(root)}) holds {_lock_path(root)} and nothing answers on {path}")
        time.sleep(0.02)
    sock = _bind(path)
    if not foreground:
        # The caller that started this waits for the line below, and by then the socket is listening.
        if os.fork():
            os._exit(0)
        os.setsid()
    os.ftruncate(lock, 0)
    os.write(lock, f"{os.getpid()}\n".encode())

    def leave(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)
    # A pane or a session that closes takes nothing with it.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    keeper = Keeper(root)
    _log(root, f"started: answers on {path}, leaves after {idle_s:g} s without a request")
    last = time.monotonic()
    # Whoever connected before a keeper leaves of its own accord is still answered; one that is killed answers nobody.
    why, drain = "stopped by a signal", False
    try:
        while not keeper.stopping:
            left = last + idle_s - time.monotonic()
            if left <= 0:
                why, drain = f"no request for {idle_s:g} s", True
                break
            sock.settimeout(left)
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            last = time.monotonic()
            with conn:
                try:
                    _answer(conn, keeper)
                except OSError:
                    # A caller that went away. What it asked for stays held for the next one.
                    pass
        else:
            why, drain = "asked to stop", True
    except Exception as broke:
        why = f"broke ({type(broke).__name__})"
        raise
    finally:
        # Still under the lock, which goes when this process does: no other keeper can have bound here yet.
        try:
            os.unlink(str(path))
        except OSError:
            pass
        if drain:
            _drain(sock, keeper)
        sock.close()
        _log(root, f"left: {why}")
    return 0


# ---------------------------------------------------------------- callers


def _listening(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(REQUEST_WAIT_S)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def _exchange(path: Path, request: dict, timeout: float) -> dict:
    """One request and its answer. `_NotThere` means nothing listens and
    nothing was sent; every other failure is after the request went out."""
    try:
        found = os.lstat(str(path))
    except FileNotFoundError:
        raise _NotThere() from None
    if found.st_uid != os.getuid():
        raise Refusal(f"{path} belongs to another user (uid {found.st_uid}); nothing was asked of it")
    if not stat.S_ISSOCK(found.st_mode):
        # A link there would be followed to whatever it points at. Nothing is sent; a keeper that is started
        # finds the same thing and refuses to remove it.
        raise _NotThere()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except socket.timeout:
            raise Refusal(f"the keeper at {path} did not take a connection within {timeout:g} seconds") from None
        except OSError:
            raise _NotThere() from None
        try:
            sock.sendall(json.dumps(request).encode() + b"\n")
            data = _recv_line(sock)
        except socket.timeout:
            raise Refusal(f"the keeper at {path} did not answer within {timeout:g} seconds") from None
        except OSError:
            data = b""
    if not data:
        raise Refusal(
            f"the keeper at {path} went without answering a request that had been sent. It may have read it and asked "
            "the store, so nothing is asked a second time by itself: run the command again"
        )
    try:
        reply = json.loads(data)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise Refusal(f"what answers at {path} is not a keeper")
    theirs = reply.pop("root", None)
    if theirs != str(path.parent):
        raise Refusal(f"what answers at {path} is not this root's keeper (it answers for {theirs!r}); nothing it said was taken")
    return reply


def _start(root: Path) -> None:
    """Start a keeper for this root: its own session, no terminal, none of
    the caller's tokens. It returns once the keeper listens, or has found
    another one already there."""
    # Nor a worker's mark: a keeper a worker's pane started is the program's, and `hl-spawn close` stops what carries it.
    env = {key: value for key, value in os.environ.items() if key not in TOKEN_VARS and key != reap.VAR}
    try:
        log = os.open(str(_log_path(root)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError as error:
        raise Unreachable(f"a keeper could not be started for {root}: {error}") from error
    try:
        proc = subprocess.Popen(
            [sys.executable or "/usr/bin/python3", str(ENTRY), "--root", str(root), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=str(root),
            env=env,
            start_new_session=True,
        )
    except OSError as error:
        raise Unreachable(f"a keeper could not be started for {root}: {error}") from error
    finally:
        os.close(log)
    try:
        code = proc.wait(timeout=START_WAIT_S)
    except subprocess.TimeoutExpired:
        raise Unreachable(f"a keeper was started for {root} and was not listening after {START_WAIT_S} seconds; {_log_path(root)} may say why") from None
    if code != 0:
        raise Unreachable(f"a keeper could not start for {root} (exit {code}); {_log_path(root)} says why")


def ask(root, request: dict, start: bool = False, timeout: float | None = None) -> dict:
    """One request to the root's keeper. With `start`, a keeper is started
    when nothing listens, a few times at most: the one that was there may
    be leaving, and starting one asks nothing of the store."""
    root = _abs(root)
    path = sock_path(root)
    timeout = timeout or ASK_TIMEOUT_S
    for started in range(MAX_STARTS + 1):
        try:
            return _exchange(path, request, timeout)
        except _NotThere:
            if not start:
                raise Unreachable(f"no keeper is running for {root}") from None
        if not _is_loop_root(root):
            # Nothing is made in a directory that is not a program's: no lock, no log, no process.
            raise Unreachable(f"{root} holds no program.json: it is not a loop root, and no keeper was started")
        if started < MAX_STARTS:
            _start(root)
    raise Unreachable(f"{MAX_STARTS} keepers were started for {root} and none answered; {_log_path(root)} may say why")


def get(root, name: str) -> str:
    """The secret called `name`, from the root's keeper. Raises Refusal with
    what the reader said when it cannot be had."""
    reply = ask(root, {"op": "get", "name": name}, start=True)
    if not reply.get("ok"):
        raise Refusal(str(reply.get("error") or f"the keeper gave no reason for refusing `{name}`"))
    return str(reply["value"])


def _lock_pid(root) -> int | None:
    try:
        return int(_lock_path(root).read_text().strip())
    except (OSError, ValueError):
        return None


def status(root) -> dict:
    try:
        reply = ask(root, {"op": "status"})
    except Refusal as gone:
        # No keeper, or one that does not answer: the pid in its lock file is where to look.
        return {"running": False, "detail": str(gone), "last_pid": _lock_pid(root)}
    reply.pop("ok", None)
    return reply


def forget(root) -> dict:
    try:
        reply = ask(root, {"op": "forget"})
    except Unreachable:
        return {"running": False, "forgot": [], "failures_dropped": []}
    reply.pop("ok", None)
    return reply


def stop(root) -> dict:
    path = sock_path(root)
    try:
        reply = ask(root, {"op": "stop"})
    except Unreachable:
        return {"running": False, "stopped": False}
    # It removes its socket as it leaves.
    deadline = time.monotonic() + 5
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return {"stopped": True, "pid": reply.get("pid")}


# ---------------------------------------------------------------- the operator's channel


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A notice goes to the address in program.json or nowhere. urllib
    would carry the request's headers, the password among them, to wherever
    a redirect pointed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_notice(cfg: dict, password: str, title: str, body: str, priority: str | None = None) -> str | None:
    """Post to the program's ntfy topic. Returns None when it went out, and
    why not otherwise."""
    title = " ".join(title.split())
    try:
        title.encode("ascii")
    except UnicodeEncodeError:
        # ntfy reads a header that is not plain ASCII in this form.
        title = "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="
    token = base64.b64encode(f"{cfg['ntfy_user']}:{password}".encode()).decode()
    headers = {"Title": title, "Authorization": f"Basic {token}"}
    if priority:
        headers["Priority"] = priority
    try:
        request = urllib.request.Request(cfg["ntfy_url"], data=body.encode(), headers=headers)
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=20):
            return None
    except (OSError, ValueError, http.client.HTTPException) as error:
        return f"{cfg['ntfy_url']} did not take it: {str(error)[:200]}"


def notify(root, title: str, body: str, priority: str | None = None) -> dict:
    cfg = program.load(root)
    if not cfg.get("ntfy_url") or not cfg.get("ntfy_user"):
        raise Refusal("program.json names no ntfy_url and ntfy_user: this program has no channel to the operator")
    why = post_notice(cfg, get(root, "ntfy"), title, body, priority)
    if why:
        raise Refusal(f"the operator was not told: {why}")
    return {"notified": True, "title": title}


# ---------------------------------------------------------------- the account's limits
#
# Only a 1Password service account has a daily request limit that `op` can be asked about. `budget` answers for
# one: where the program names no op:// reference, or no service account token is to be found, it says plainly
# that it does not apply, still says whether the keeper's last read worked, and does not fail, so that a
# manager's start-of-session step never stops on it.


def _op_refs(root) -> list[str] | None:
    """The program's references that `hl-read-secret` reads from 1Password,
    or None when its settings cannot be read (then nobody can say none)."""
    try:
        data = read_json(_abs(root) / "program.json")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return [data[key] for key in NAMES.values() if isinstance(data.get(key), str) and data[key].startswith("op://")]


def _account_token() -> str | None:
    """The service account's own token, which `op` needs to say what is left
    of its limits. From this environment, or from a zsh on the host, whose
    startup files may set it whether or not they export it. Held for one
    call and never shown."""
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
    if token:
        return token
    try:
        proc = subprocess.run(
            ["/bin/zsh", "-c", 'print -rn -- "${OP_SERVICE_ACCOUNT_TOKEN-}"'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def parse_limits(text: str) -> list[dict] | None:
    """`op service-account ratelimit` as rows, or None when it is not the
    table this was written against: a header line, then one line per limit,
    columns two or more spaces apart."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    columns = [name.lower() for name in re.split(r"\s{2,}", lines[0])]
    if not {"type", "limit", "used", "remaining"} <= set(columns):
        return None
    rows = []
    for line in lines[1:]:
        cells = re.split(r"\s{2,}", line)
        if len(cells) != len(columns):
            return None
        row: dict = dict(zip(columns, cells))
        for key in ("limit", "used", "remaining"):
            if not row[key].isdigit():
                return None
            row[key] = int(row[key])
        rows.append(row)
    return rows


def budget(root) -> tuple[dict | None, str]:
    """What the service account has left, without reading a secret: the
    rows and the text `op` printed. The rows are None when the text could
    not be read. Where there is no service account to ask about, the answer
    says that it does not apply, and why."""
    refs = _op_refs(root)
    if refs == []:
        return {
            "applies": False,
            "why_not": "program.json names no op:// reference: this program reads nothing from 1Password, and no service account's daily limit binds it",
        }, ""
    token = _account_token()
    if not token:
        return {
            "applies": False,
            "why_not": (
                "OP_SERVICE_ACCOUNT_TOKEN is not in this environment and a zsh on this host does not set it: op:// references are read with "
                "whatever other sign-in `op` has, and the daily limit this reports is a service account's"
            ),
        }, ""
    op = shutil.which("op", path=OP_PATH)
    if not op:
        raise Refusal(f"`op` is not in {OP_PATH}")
    # `op` is given an empty home and nothing else of this environment: with the user's own it turns to the
    # desktop app, and waits for a person.
    home = tempfile.mkdtemp(prefix="hl-budget-")
    try:
        proc = subprocess.run(
            [op, "service-account", "ratelimit"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=READ_TIMEOUT_S,
            env={"OP_SERVICE_ACCOUNT_TOKEN": token, "HOME": home, "PATH": OP_PATH},
        )
    except subprocess.TimeoutExpired:
        raise Refusal(f"`op service-account ratelimit` did not answer within {READ_TIMEOUT_S} seconds") from None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    text = proc.stdout.replace(token, "[withheld]")
    rows = parse_limits(text)
    if rows is None and not text.strip():
        said = " ".join(proc.stderr.replace(token, "[withheld]").split())[:300]
        raise Refusal(f"`op service-account ratelimit` printed no counters (exit {proc.returncode}): {said or 'it said nothing'}")
    if rows is None:
        return None, text
    # The limit that stopped the first program is the account's, counted over a day.
    account = next((row for row in rows if row["type"].lower() == "account"), None)
    return {
        "applies": True,
        "account_is": "the service account's daily request allowance, shared by everything that reads through it",
        "limits": rows,
        "account_remaining": account["remaining"] if account else None,
    }, text


def reads_and_hold(state: dict, remaining: int | None) -> dict:
    """What a program goes by: whether the keeper's reads work, and, where a
    service account's counter applies (`remaining` is not None), whether it
    is about to stop them. `state` is the keeper's status."""
    last = state.get("last_read")
    out = {"reads": {"last_read": last, "failed_reads": state.get("failed_reads")}}
    if last and not last["ok"]:
        return {**out, "hold": True, "why": f"the keeper's last read ({last['name']}) failed"}
    if remaining is not None and remaining < ACCOUNT_LOW:
        return {**out, "hold": True, "why": f"the service account has {remaining} requests left for the day (under {ACCOUNT_LOW} is low), and reads through it are about to stop"}
    if not last:
        return {**out, "hold": False, "why": "the keeper has read nothing yet: hold only when a read fails"}
    return {**out, "hold": False, "why": f"the keeper's last read ({last['name']}) worked"}


# ---------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hl-secrets", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", type=absolute, required=True, help="the program's loop root")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("get", help="print one secret and nothing else; for `$(…)`, never a terminal")
    s.add_argument("name", choices=sorted(NAMES))
    s.add_argument("--tagged", action="store_true", help="print `ok <value>` or `no <reason>`, so one capture tells a shell which it got (hl-env.sh)")
    s = sub.add_parser("run", help="run a command with GH_TOKEN set to the loop's token, and exit as it exits")
    s.add_argument("command", nargs=argparse.REMAINDER, help="after `--`: the command and its arguments")
    s = sub.add_parser("notify", help="post to the program's ntfy topic")
    s.add_argument("--title", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--priority", default="default")
    sub.add_parser("budget", help="what a 1Password service account has left of its daily limit, and whether to hold; reads no secret")
    sub.add_parser("status", help="what is held, how old, and the counters; never a value")
    sub.add_parser("forget", help="drop everything held, after a secret was rotated")
    sub.add_parser("stop", help="end the keeper")
    s = sub.add_parser("serve", help="the keeper itself; started on demand, not by hand")
    s.add_argument("--idle-s", type=float, default=IDLE_EXIT_S, help="leave after this long without a request")
    s.add_argument("--foreground", action="store_true", help="stay the process that was started")
    return p


def _refused(reasons: list[str]) -> int:
    emit({"refused": reasons})
    print(f"hl-secrets: refused: {'; '.join(reasons)}", file=sys.stderr)
    return 2


def _get(root, name: str, tagged: bool) -> int:
    if sys.stdout.isatty():
        raise Refusal("this would print a secret on a terminal; `get` is for `$(…)`, and `run -- <command>` gives a command the GitHub token")
    try:
        value = get(root, name)
    except Refusal as refusal:
        if not tagged:
            raise
        sys.stdout.write("no " + " ".join(str(refusal).split()) + "\n")
        return 2
    sys.stdout.write(("ok " if tagged else "") + value + "\n")
    return 0


def _run(root, command: list[str]) -> int:
    command = command[1:] if command[:1] == ["--"] else command
    if not command:
        raise Refusal("run needs a command after `--`")
    env = {**os.environ, "GH_TOKEN": get(root, "gh")}
    try:
        # In place of this process: its exit status and its signals are the command's own.
        os.execvpe(command[0], command, env)
    except OSError as error:
        print(f"hl-secrets: {command[0]}: {error.strerror or error}", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "serve":
            return serve(args.root, idle_s=args.idle_s, foreground=args.foreground)
        if args.cmd == "get":
            return _get(args.root, args.name, args.tagged)
        if args.cmd == "run":
            return _run(args.root, args.command)
        if args.cmd == "notify":
            emit(notify(args.root, args.title, args.body, args.priority))
        elif args.cmd == "budget":
            parsed, text = budget(args.root)
            if parsed is None:
                sys.stdout.write(text)
            else:
                emit({**parsed, **reads_and_hold(status(args.root), parsed.get("account_remaining"))})
        elif args.cmd == "status":
            emit(status(args.root))
        elif args.cmd == "forget":
            emit(forget(args.root))
        else:
            emit(stop(args.root))
    except Refusal as refusal:
        if args.cmd in ("get", "run", "serve"):
            # Nothing on standard output: a `$(…)` around this must come back empty.
            print(f"hl-secrets: refused: {refusal}", file=sys.stderr)
            if args.cmd == "serve" and _is_loop_root(args.root):
                _log(args.root, f"refused: {refusal}")
            return 2
        return _refused(refusal.reasons)
    return 0


if __name__ == "__main__":
    sys.exit(main())
