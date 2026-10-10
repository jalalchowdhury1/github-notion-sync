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

    def test_human_fix_row_is_not_named(self):
        # 9 Oct 2026: unpushed work red at noon, pushed 21:14, green at 05:00 = a fix.
        name = "unpushed work (commits only on this Mac)"
        self._slot("12", "2026-10-03 12:00", [{"name": name, "ok": False}])
        self.assertEqual(self._note([{"name": name, "ok": True}]), "")

    def test_all_green_says_nothing(self):
        self._slot("12", "2026-10-03 12:00", [{"name": "x (y)", "ok": True}])
        self.assertEqual(self._note([{"name": "x (y)", "ok": True}]), "")


class UnpushedWork(unittest.TestCase):
    """Real temp git repos: a pushed repo is green, an old unpushed commit is red,
    a fresh one is not, a no-remote repo is counted only, and quiet_red never buzzes."""

    def setUp(self):
        import subprocess
        self.sp = subprocess
        self.root = tempfile.mkdtemp()
        self.env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def _git(self, cwd, *a, date=None):
        env = dict(self.env)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        self.sp.run(["git", "-C", cwd, *a], check=True, capture_output=True, env=env)

    def _repo(self, name, remote=True):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        self._git(d, "init", "-q", "-b", "main")
        self._git(d, "commit", "-q", "--allow-empty", "-m", "first")
        if remote:
            bare = os.path.join(tempfile.mkdtemp(), "r.git")
            self.sp.run(["git", "init", "-q", "--bare", bare], check=True)
            self._git(d, "remote", "add", "origin", bare)
            self._git(d, "push", "-q", "-u", "origin", "main")
        return d

    def test_clean_repo_is_green(self):
        self._repo("clean")
        ok, detail = fh.probe_unpushed_work([self.root])
        self.assertTrue(ok, detail)

    def test_old_unpushed_commit_is_red_and_named(self):
        d = self._repo("forgot")
        self._git(d, "commit", "-q", "--allow-empty", "-m", "x", date="2026-01-01T00:00:00")
        ok, detail = fh.probe_unpushed_work([self.root])
        self.assertFalse(ok)
        self.assertIn("forgot +1", detail)

    def test_fresh_unpushed_commit_is_green(self):
        d = self._repo("today")
        self._git(d, "commit", "-q", "--allow-empty", "-m", "x")
        ok, detail = fh.probe_unpushed_work([self.root])
        self.assertTrue(ok, detail)
        self.assertIn("1 newer than 24h", detail)

    def test_no_remote_repo_is_counted_not_red(self):
        self._repo("local", remote=False)
        ok, detail = fh.probe_unpushed_work([self.root])
        self.assertTrue(ok, detail)
        self.assertIn("1 Mac-only", detail)

    def test_quiet_red_row_never_buzzes(self):
        import datetime
        row = [i for i in fh.FLEET if i.get("quiet_red")][0]
        with mock.patch.object(fh, "_telegram_send", _boom):
            rec = fh.loud_alert([{"name": row["name"], "ok": False, "detail": "x"}],
                                now=datetime.datetime(2026, 10, 4, 6, 30), prev={})
        self.assertEqual(rec, {})


class RunBudget(unittest.TestCase):
    """10 Oct 2026: past RUN_BUDGET_S the remaining rows are filed, not probed."""

    def test_rows_past_the_budget_are_not_probed(self):
        calls = []
        rows = [{"name": "a (x)", "probe": "fake"}, {"name": "b (x)", "probe": "fake"}]
        def fake(**kw):
            calls.append(kw["name"])
            return True, "fine"
        with mock.patch.object(fh, "FLEET", rows), \
             mock.patch.dict(fh.PROBE_FNS, {"fake": fake}), \
             mock.patch.object(fh, "lint_roster", lambda: []), \
             mock.patch.object(fh, "RUN_BUDGET_S", -1):
            res = fh.run_checks()
        self.assertEqual(calls, [])
        self.assertTrue(all(r["detail"].startswith("probe error: run budget exhausted") for r in res))
        self.assertEqual(len(res), 2)
