"""A program's Claude seats: the readings, the move, the pause and the resume."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hl import program, seat
from hl.common import read_json, write_json

HOUR = 3600


class SeatCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = str(self.home / ".claude-a")
        self.b = str(self.home / ".claude-b")
        for home, email in ((self.a, "a@example.invalid"), (self.b, "b@example.invalid")):
            Path(home).mkdir()
            write_json(Path(home) / ".claude.json", {"oauthAccount": {"emailAddress": email}})
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.settings = {
            "program": "demo",
            "base": "main",
            "worker_claude_ref": "-",
            "gh_token_ref": "op://Loop/gh/password",
            "cursor_model": "composer",
            "claude_config_dir": self.a,
            "claude_seats": [self.a, self.b],
        }
        write_json(self.root / "program.json", self.settings)
        self.now = time.time()

    def cfg(self):
        return program.load(self.root)

    def usage(self, home, five=10.0, week=10.0, five_resets=2 * HOUR, week_resets=3 * 24 * HOUR, observed=0, session="s1"):
        """What the status line writes for one session on a seat, its resets given from now."""
        (Path(home) / seat.USAGE_DIR).mkdir(exist_ok=True)
        write_json(
            Path(home) / seat.USAGE_DIR / f"{session}.json",
            {
                "observed_at": int(self.now - observed),
                "rate_limits": {
                    "five_hour": {"used_percentage": five, "resets_at": int(self.now + five_resets)},
                    "seven_day": {"used_percentage": week, "resets_at": int(self.now + week_resets)},
                },
            },
        )

    def step(self, refused=None):
        return seat.step(self.cfg(), self.now, refused)

    def act(self):
        decision = self.step()
        seat.apply(self.cfg(), decision, self.now)
        return decision


class ReadingTest(SeatCase):
    def test_a_seat_with_no_reading_is_unknown_and_never_full(self):
        entry = seat.reading(self.a, self.now, 99)
        self.assertEqual((entry["known"], entry["full"]), (False, False))

    def test_either_window_at_the_threshold_makes_the_seat_full_until_its_reset(self):
        self.usage(self.a, five=99, week=40, five_resets=HOUR)
        entry = seat.reading(self.a, self.now, 99)
        self.assertTrue(entry["full"])
        self.assertEqual(entry["open_at"], int(self.now + HOUR) + seat.RESET_MARGIN_S)
        self.usage(self.a, five=20, week=99.4, week_resets=5 * HOUR)
        self.assertEqual(seat.reading(self.a, self.now, 99)["open_at"], int(self.now + 5 * HOUR) + seat.RESET_MARGIN_S)

    def test_both_windows_full_open_when_the_later_one_resets(self):
        self.usage(self.a, five=100, week=99, five_resets=HOUR, week_resets=30 * HOUR)
        self.assertEqual(seat.reading(self.a, self.now, 99)["open_at"], int(self.now + 30 * HOUR) + seat.RESET_MARGIN_S)

    def test_a_window_whose_reset_has_passed_counts_as_empty_after_the_margin(self):
        self.usage(self.a, five=100, week=50, five_resets=-60)
        self.assertTrue(seat.reading(self.a, self.now, 99)["full"], "still inside the margin")
        self.usage(self.a, five=100, week=50, five_resets=-seat.RESET_MARGIN_S - 1)
        entry = seat.reading(self.a, self.now, 99)
        self.assertFalse(entry["full"])
        self.assertEqual(entry["windows"]["five_hour"]["used"], 0.0)

    def test_a_usage_limit_refusal_makes_the_seat_full_whatever_the_reading_says(self):
        self.usage(self.a, five=40, week=60)
        entry = seat.reading(self.a, self.now, 99, {self.a: self.now + 2 * HOUR})
        self.assertTrue(entry["full"])
        self.assertEqual(entry["open_at"], self.now + 2 * HOUR + seat.RESET_MARGIN_S)

    def test_a_reading_that_is_not_json_is_unknown(self):
        (Path(self.a) / seat.USAGE_DIR).mkdir()
        (Path(self.a) / seat.USAGE_DIR / "s1.json").write_text("{half")
        self.assertFalse(seat.reading(self.a, self.now, 99)["known"])


class MergeTest(SeatCase):
    """Each session writes its own reading, as of its own last request."""

    def test_an_idle_session_drawn_again_never_takes_the_seat_back_down(self):
        self.usage(self.a, week=99.2, observed=60, session="working")
        self.usage(self.a, week=94, session="idle")
        entry = seat.reading(self.a, self.now, 99)
        self.assertEqual(entry["windows"]["seven_day"]["used"], 99.2)
        self.assertTrue(entry["full"])
        self.assertEqual(entry["observed_at"], seat.stamp(int(self.now)))

    def test_a_later_window_outranks_an_old_one_whatever_its_use(self):
        # A session idle since the last 5-hour window still holds that window's 97 %.
        self.usage(self.a, five=97, five_resets=-10 * 60, session="idle")
        self.usage(self.a, five=12, five_resets=4 * HOUR + 50 * 60, session="working")
        self.assertEqual(seat.reading(self.a, self.now, 99)["windows"]["five_hour"]["used"], 12.0)

    def test_reset_times_a_few_minutes_apart_are_one_window(self):
        self.usage(self.a, week=99.5, week_resets=3 * 24 * HOUR, session="one")
        self.usage(self.a, week=96, week_resets=3 * 24 * HOUR + 5 * 60, session="two")
        self.assertEqual(seat.reading(self.a, self.now, 99)["windows"]["seven_day"]["used"], 99.5)

    def test_a_window_left_out_after_its_reset_counts_as_empty(self):
        # At a reset every session draws again, without the window that reset (seen live 2026-11-04 15:30Z).
        self.usage(self.a, five=99.5, five_resets=-3 * 60, week=93, session="before")
        write_json(
            Path(self.a) / seat.USAGE_DIR / "after.json",
            {"observed_at": int(self.now), "rate_limits": {"seven_day": {"used_percentage": 94, "resets_at": int(self.now + 3 * 24 * HOUR)}}},
        )
        entry = seat.reading(self.a, self.now, 99)
        self.assertEqual((entry["full"], entry["windows"]["five_hour"]["used"], entry["windows"]["seven_day"]["used"]), (False, 0.0, 94.0))

    def test_a_reading_of_some_other_shape_is_left_out(self):
        self.usage(self.a, week=40, session="good")
        write_json(Path(self.a) / seat.USAGE_DIR / "odd.json", {"observed_at": 1, "rate_limits": "none"})
        self.assertEqual(seat.reading(self.a, self.now, 99)["windows"]["seven_day"]["used"], 40.0)


class PruneTest(SeatCase):
    def test_readings_whose_every_window_has_reset_are_removed(self):
        self.usage(self.a, five_resets=-HOUR, week_resets=-HOUR, session="spent")
        self.usage(self.a, five_resets=-HOUR, week_resets=HOUR, session="week-left")
        self.assertEqual(seat.prune(self.a, self.now), ["spent.json"])
        self.assertEqual(sorted(p.name for p in (Path(self.a) / seat.USAGE_DIR).iterdir()), ["week-left.json"])

    def test_a_reading_with_no_reset_time_goes_once_it_is_old(self):
        (Path(self.a) / seat.USAGE_DIR).mkdir()
        path = Path(self.a) / seat.USAGE_DIR / "bare.json"
        write_json(path, {"observed_at": 1, "rate_limits": {}})
        self.assertEqual(seat.prune(self.a, self.now), [])
        old = self.now - seat.STALE_READING_S - 1
        os.utime(path, (old, old))
        self.assertEqual(seat.prune(self.a, self.now), ["bare.json"])

    def test_a_seat_with_no_readings_has_nothing_to_prune(self):
        self.assertEqual(seat.prune(self.b, self.now), [])

    def test_when_every_reading_has_reset_the_newest_is_kept_and_says_the_seat_is_open(self):
        self.usage(self.a, five=40, week=99, five_resets=-3 * HOUR, week_resets=-2 * HOUR, observed=5 * HOUR, session="older")
        self.usage(self.a, five=28, week=99, five_resets=-HOUR, week_resets=-HOUR, observed=2 * HOUR, session="newest")
        self.assertEqual(seat.prune(self.a, self.now), ["older.json"])
        self.assertEqual(seat.prune(self.a, self.now), [])
        entry = seat.reading(self.a, self.now, 99)
        self.assertTrue(entry["known"])
        self.assertFalse(entry["full"])

    def test_the_kept_reading_goes_once_a_newer_one_is_drawn(self):
        self.usage(self.a, week=99, five_resets=-HOUR, week_resets=-HOUR, observed=HOUR, session="spent")
        self.assertEqual(seat.prune(self.a, self.now), [])
        self.usage(self.a, week=3, session="fresh")
        self.assertEqual(seat.prune(self.a, self.now), ["spent.json"])

    def test_a_seat_whose_readings_all_reset_resumes_a_pause(self):
        self.usage(self.a, week=99, week_resets=72 * HOUR)
        self.usage(self.b, five=28, week=99, five_resets=-HOUR, week_resets=3 * HOUR)
        self.assertEqual(self.act()["action"], "pause")
        self.now += 3 * HOUR + seat.RESET_MARGIN_S + 1
        for home in (self.a, self.b):
            seat.prune(home, self.now)
        decision = self.act()
        self.assertEqual((decision["action"], decision["to"]), ("resume", self.b))


class StepTest(SeatCase):
    def test_a_seat_with_room_stays(self):
        self.usage(self.a, week=98.9)
        self.usage(self.b, week=10)
        self.assertEqual(self.step()["action"], "stay")

    def test_a_full_seat_moves_the_program_to_the_open_one(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=60)
        decision = self.act()
        self.assertEqual((decision["action"], decision["from"], decision["to"]), ("move", self.a, self.b))
        self.assertIn("a@example.invalid is full (5-hour window 10 %, week 99 %)", decision["why"])
        self.assertIn("moves to b@example.invalid", decision["why"])
        # Every Claude session started from now on comes up on the new seat.
        self.assertEqual(program.claude_home(self.cfg()), self.b)
        self.assertEqual(read_json(self.root / "seat.json")["home"], self.b)

    def test_the_five_hour_window_alone_moves_the_program(self):
        self.usage(self.a, five=99.5, week=20)
        self.usage(self.b, five=5, week=90)
        self.assertEqual(self.step()["action"], "move")

    def test_the_program_does_not_move_back_while_the_seat_it_moved_to_has_room(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=60)
        self.act()
        self.usage(self.a, week=99, week_resets=-HOUR)
        self.assertEqual(self.step()["action"], "stay")
        self.assertEqual(program.claude_home(self.cfg()), self.b)

    def test_a_seat_with_no_reading_is_never_moved_to(self):
        self.usage(self.a, week=99, week_resets=5 * HOUR)
        decision = self.step()
        self.assertEqual(decision["action"], "pause")
        self.assertIn("b@example.invalid no reading", seat.explain(decision))

    def test_every_seat_full_pauses_until_the_first_one_opens(self):
        self.usage(self.a, week=99, week_resets=40 * HOUR)
        self.usage(self.b, five=99, week=70, five_resets=3 * HOUR)
        decision = self.act()
        self.assertEqual(decision["action"], "pause")
        self.assertEqual(decision["until"], int(self.now + 3 * HOUR) + seat.RESET_MARGIN_S)
        self.assertEqual(read_json(self.root / "seat.json")["paused_until"], int(decision["until"]))
        self.assertIn("paused until", decision["why"])
        # Still paused while nothing has reset; the seat in use does not change.
        self.assertEqual(self.step()["action"], "paused")
        self.assertEqual(program.claude_home(self.cfg()), self.a)

    def test_a_pause_resumes_on_the_seat_with_the_most_room_once_one_opens(self):
        self.usage(self.a, week=99, week_resets=40 * HOUR)
        self.usage(self.b, five=99, week=70, five_resets=3 * HOUR)
        self.act()
        self.now += 3 * HOUR + seat.RESET_MARGIN_S + 1
        decision = self.act()
        self.assertEqual((decision["action"], decision["to"]), ("resume", self.b))
        state = read_json(self.root / "seat.json")
        self.assertIsNone(state["paused_until"])
        self.assertEqual(program.claude_home(self.cfg()), self.b)
        self.assertEqual([h["action"] for h in state["history"]], ["pause", "resume"])

    def test_resuming_prefers_the_seat_with_more_of_its_week_left(self):
        self.usage(self.a, five=99, week=30, five_resets=HOUR)
        self.usage(self.b, five=99, week=80, five_resets=HOUR)
        self.act()
        self.now += HOUR + seat.RESET_MARGIN_S + 1
        self.assertEqual(self.step()["to"], self.a)

    def test_a_program_with_one_seat_pauses_when_it_is_full(self):
        write_json(self.root / "program.json", {**self.settings, "claude_seats": None})
        self.usage(self.a, week=99.9)
        self.assertEqual(self.step()["action"], "pause")

    def test_a_program_on_the_default_sign_in_has_nothing_to_decide(self):
        write_json(self.root / "program.json", {**self.settings, "claude_config_dir": None, "claude_seats": None})
        self.assertEqual(self.step()["action"], "none")
        self.assertIsNone(program.claude_home(self.cfg()))

    def test_a_threshold_of_its_own_is_used(self):
        write_json(self.root / "program.json", {**self.settings, "seat_full_percent": 90})
        self.usage(self.a, week=91)
        self.usage(self.b, week=10)
        self.assertEqual(self.step()["action"], "move")

    def test_the_record_keeps_a_bounded_history(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=99)
        for _ in range(seat.HISTORY + 5):
            seat.apply(self.cfg(), {**self.step(), "action": "pause", "until": self.now + HOUR}, self.now)
        self.assertEqual(len(read_json(self.root / "seat.json")["history"]), seat.HISTORY)


class NoticeTest(SeatCase):
    def test_nothing_to_act_on_while_the_manager_is_on_the_programs_seat(self):
        self.assertIsNone(seat.notice(self.cfg(), own=self.a))

    def test_a_manager_left_on_the_old_seat_is_told_to_move(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=50)
        self.act()
        told = seat.notice(self.cfg(), own=self.a)
        self.assertEqual((told["action"], told["from"], told["to"]), ("move", self.a, self.b))
        self.assertIsNone(seat.notice(self.cfg(), own=self.b))

    def test_a_pause_is_told_to_every_manager(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=99)
        self.act()
        for own in (self.a, self.b):
            told = seat.notice(self.cfg(), own=own)
            self.assertEqual(told["action"], "pause")
            self.assertTrue(told["until"].endswith("Z"))

    def test_the_manager_s_own_seat_is_its_environment(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=50)
        self.act()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.a}):
            self.assertEqual(seat.notice_for(self.root)["action"], "move")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.b}):
            self.assertIsNone(seat.notice_for(self.root))

    def test_without_its_environment_the_manager_s_seat_is_the_one_the_watchdog_started_it_on(self):
        self.usage(self.a, week=99)
        self.usage(self.b, week=50)
        self.act()
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertIsNone(seat.notice_for(self.root), "no environment and no record: nothing to go on")
            write_json(self.root / "watchdog.json", {"manager": {"session": "s", "home": self.a}})
            self.assertEqual(seat.notice_for(self.root)["action"], "move")
            write_json(self.root / "watchdog.json", {"manager": {"session": "s", "home": self.b}})
            self.assertIsNone(seat.notice_for(self.root))

    def test_a_root_without_a_program_has_nothing_to_tell(self):
        self.assertIsNone(seat.notice_for(self.tmp / "nowhere"))


class CliTest(SeatCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = seat.main(["--root", str(self.root), *argv])
        return code, json.loads(out.getvalue())

    def test_status_shows_each_seat_and_what_would_happen_and_writes_nothing(self):
        self.usage(self.a, five=3, week=94)
        self.usage(self.b, five=78, week=85)
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual((out["seat"], out["account"], out["decision"]["action"]), (self.a, "a@example.invalid", "stay"))
        self.assertEqual([s["account"] for s in out["seats"]], ["a@example.invalid", "b@example.invalid"])
        self.assertEqual(out["seats"][0]["seven_day"]["used"], 94.0)
        # The rule tried at other thresholds on the same readings.
        moved = self.run_cli("status", "--full-percent", "90")[1]["decision"]
        self.assertEqual((moved["action"], moved["to"]), ("move", self.b))
        paused = self.run_cli("status", "--full-percent", "80")[1]["decision"]
        self.assertEqual(paused["action"], "pause")
        self.assertIn("until", paused)
        self.assertFalse((self.root / "seat.json").exists())


if __name__ == "__main__":
    unittest.main()
