"""--quiet must grade and print, and never send, publish, alert or lock (3 Oct 2026)."""
import os
import tempfile
import unittest
from unittest import mock

import fleet_health as fh


def _boom(*a, **k):
    raise AssertionError("quiet mode must not call this")


class QuietMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.quiet = os.path.join(self.tmp, "health-quiet.json")

    def _run(self, results):
        with mock.patch.object(fh, "run_checks", return_value=results), \
             mock.patch.object(fh, "lint_roster", return_value=[]), \
             mock.patch.object(fh, "_telegram_send", _boom), \
             mock.patch.object(fh, "publish", _boom), \
             mock.patch.object(fh, "loud_alert", _boom), \
             mock.patch.object(fh, "QUIET_FILE", self.quiet), \
             mock.patch.object(fh, "LOCK_FILE", os.path.join(self.tmp, "lock")):
            rc = fh.quiet_main(["--quiet"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "lock")))
        self.assertTrue(os.path.exists(self.quiet))
        return rc

    def test_all_green_exits_0_and_sends_nothing(self):
        self.assertEqual(self._run([{"name": "a", "ok": True, "detail": "fine"}]), 0)

    def test_red_exits_1_and_still_sends_nothing(self):
        self.assertEqual(self._run([{"name": "a", "ok": False, "detail": "stale"}]), 1)


if __name__ == "__main__":
    unittest.main()


class OffHoursNote(unittest.TestCase):
    """Rows red at a scheduled off-hours quiet run but green in the morning get one
    digest line; rows red now, stale files and manual quiet runs do not."""

    def setUp(self):
        import datetime, json
        self.dt, self.json = datetime, json
        self.tmp = tempfile.mkdtemp()
        self.now = datetime.datetime(2026, 10, 4, 5, 0)

    def _slot(self, hh, checked, rows):
        with open(os.path.join(self.tmp, f"health-quiet-{hh}.json"), "w") as f:
            self.json.dump({"checked": checked, "results": rows}, f)

    def _note(self, results):
        with mock.patch.object(fh, "QUIET_SLOT_GLOB", os.path.join(self.tmp, "health-quiet-*.json")):
            return fh.offhours_note(results, self.now)

    def test_red_last_night_green_now_is_named(self):
        self._slot("23", "2026-10-03 23:00", [{"name": "dhaka-yearly (01:15 run)", "ok": False}])
        note = self._note([{"name": "dhaka-yearly (01:15 run)", "ok": True}])
        self.assertIn("dhaka-yearly (red at 23:00)", note)

    def test_red_now_is_not_repeated(self):
        self._slot("23", "2026-10-03 23:00", [{"name": "x (y)", "ok": False}])
        self.assertEqual(self._note([{"name": "x (y)", "ok": False}]), "")

    def test_old_slot_file_is_ignored(self):
        self._slot("12", "2026-10-02 12:00", [{"name": "x (y)", "ok": False}])
        self.assertEqual(self._note([{"name": "x (y)", "ok": True}]), "")

    def test_all_green_says_nothing(self):
        self._slot("12", "2026-10-03 12:00", [{"name": "x (y)", "ok": True}])
        self.assertEqual(self._note([{"name": "x (y)", "ok": True}]), "")
