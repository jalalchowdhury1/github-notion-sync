"""Tests for fleet_health digest logic.

Run: python3 -m unittest test_fleet_health -v   (stdlib only, no deps)
"""
import datetime
import json
import os
import tempfile
import time
import types
import unittest

import fleet_health as fh


def stale_gh(name, repo, days=1.2):
    """A gh_run probe that found NO run at all — the job never started."""
    return {"name": name, "repo": repo, "probe": "gh_run", "ok": False,
            "detail": f"no run in {days}d (limit 24h)\nlast run: https://x/1",
            "cfg": f"probe=gh_run · repo={repo} · max_age_h=24"}


def broken_gh(name, repo):
    """A gh_run probe whose run DID start and then failed — a real repo bug."""
    return {"name": name, "repo": repo, "probe": "gh_run", "ok": False,
            "detail": "last run failure (2h ago)\nrun: https://x/2",
            "cfg": f"probe=gh_run · repo={repo} · max_age_h=24"}


def healthy(name):
    return {"name": name, "repo": None, "probe": "web_200", "ok": True,
            "detail": "HTTP 200", "cfg": "probe=web_200"}


class CorrelatedStaleness(unittest.TestCase):

    def test_stale_probes_in_different_repos_are_reported_as_one_event(self):
        results = [stale_gh("a (daily)", "repo-one"),
                   stale_gh("b (nightly)", "repo-two"),
                   healthy("c")]
        digest = fh.format_digest(results)
        self.assertIn("ONE event", digest)
        self.assertIn("2 repos", digest)

    def test_one_event_banner_names_the_dispatcher_as_the_suspect(self):
        results = [stale_gh("a", "repo-one"), stale_gh("b", "repo-two")]
        self.assertIn("dispatcher", fh.format_digest(results))

    def test_stale_probes_inside_one_repo_do_not_blame_github(self):
        results = [stale_gh("a (daily)", "solo"), stale_gh("a (weekly)", "solo")]
        digest = fh.format_digest(results)
        self.assertIn("ONE event", digest)
        self.assertIn("same repo", digest)
        self.assertNotIn("dispatcher", digest)

    def test_a_single_stale_probe_gets_no_banner(self):
        results = [stale_gh("a", "repo-one"), healthy("c")]
        self.assertNotIn("ONE event", fh.format_digest(results))

    def test_runs_that_started_and_failed_are_not_correlated_staleness(self):
        results = [broken_gh("a", "repo-one"), broken_gh("b", "repo-two")]
        self.assertNotIn("ONE event", fh.format_digest(results))

    def test_banner_survives_when_the_digest_has_to_be_trimmed(self):
        many = []
        for i in range(30):
            r = stale_gh(f"job-{i}", f"repo-{i}")
            r["detail"] += "\n" + ("x" * 900)
            many.append(r)
        digest = fh.format_digest(many)
        self.assertLessEqual(len(digest), fh.TELEGRAM_LIMIT)
        self.assertIn("ONE event", digest)


if __name__ == "__main__":
    unittest.main()


class WebhookGuardProbe(unittest.TestCase):
    """require_guard proves the secret still bites by POSTing WITHOUT one."""

    def _run(self, registered_url, unauth_status):
        import io, json, os, urllib.error
        os.environ["T_TOKEN"] = "x"
        info = {"ok": True, "result": {"url": registered_url, "pending_update_count": 0}}

        def fake_urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, "full_url") else req
            if "getWebhookInfo" in url:
                r = io.BytesIO(json.dumps(info).encode()); r.status = 200
                r.__enter__ = lambda s=r: s; r.__exit__ = lambda s, *a: None
                return r
            self.assertEqual(req.get_method(), "POST")
            self.assertNotIn("?", url)
            self.assertIsNone(req.get_header("X-telegram-bot-api-secret-token"))
            if unauth_status == 200:
                r = io.BytesIO(b"ok"); r.status = 200
                r.__enter__ = lambda s=r: s; r.__exit__ = lambda s, *a: None
                return r
            raise urllib.error.HTTPError(url, unauth_status, "nope", {}, None)

        orig = fh.urllib.request.urlopen
        fh.urllib.request.urlopen = fake_urlopen
        try:
            return fh.probe_telegram_webhook("T_TOKEN", "https://b.vercel.app/api/webhook",
                                             require_guard=True)
        finally:
            fh.urllib.request.urlopen = orig

    def test_header_guarded_bot_with_no_query_passes(self):
        ok, detail = self._run("https://b.vercel.app/api/webhook", 403)
        self.assertTrue(ok, detail)

    def test_query_guarded_bot_still_passes(self):
        ok, detail = self._run("https://b.vercel.app/api/webhook?s=secret", 401)
        self.assertTrue(ok, detail)

    def test_open_endpoint_pages(self):
        ok, detail = self._run("https://b.vercel.app/api/webhook", 200)
        self.assertFalse(ok)
        self.assertIn("guard is GONE", detail)
        self.assertNotIn("secret", detail)

    def test_5xx_on_the_guard_post_is_an_infra_error_not_a_verdict(self):
        with self.assertRaises(RuntimeError):
            self._run("https://b.vercel.app/api/webhook", 503)


class RsyncLogReadTest(unittest.TestCase):
    """probe_rsync_log must never hang the whole fleet run on a blocked read.

    2026-09-13: the first launchd run after the T7 probe shipped sat 27h inside
    open() on /Volumes/T7Files/sync.log — macOS held the read behind a Removable
    Volumes permission prompt nobody was awake to click, so health.json was never
    pushed. A FIFO with no writer blocks open() the same way.
    """

    def _log(self, tmp):
        now = datetime.datetime.now()
        start = (now - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        done = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        path = os.path.join(tmp, "sync.log")
        with open(path, "w") as f:
            f.write(f"=== {start} sync start ===\n"
                    "Number of files: 31,007 (reg: 26,975, dir: 4,030, link: 2)\n"
                    "Number of regular files transferred: 12\n"
                    f"=== {done} sync done (exit 0) ===\n")
        return path

    def test_healthy_log_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, detail = fh.probe_rsync_log(self._log(tmp), min_files=1000)
        self.assertTrue(ok, detail)

    def test_missing_log_reads_as_unmounted(self):
        ok, detail = fh.probe_rsync_log("/nonexistent/T7Files/sync.log")
        self.assertFalse(ok)
        self.assertIn("not mounted", detail)

    def test_a_blocked_read_fails_the_probe_instead_of_hanging_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = os.path.join(tmp, "sync.log")
            os.mkfifo(fifo)
            t0 = time.time()
            ok, detail = fh.probe_rsync_log(fifo, read_timeout_s=1)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("Removable Volumes", detail)


def _iso(minutes_ago):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class GhRunInProgressTest(unittest.TestCase):
    """2026-09-14: a manual 09:30 fleet check landed seconds after One Clock's
    09:30 dispatches and paged two healthy repos as 'likely HUNG'."""

    def _probe(self, runs):
        orig = fh.subprocess.run
        fh.subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout=json.dumps(runs), stderr="")
        try:
            return fh.probe_gh_run("repo", "wf.yml", max_age_h=36)
        finally:
            fh.subprocess.run = orig

    def test_a_run_started_minutes_ago_is_not_called_hung(self):
        ok, detail = self._probe([
            {"status": "in_progress", "conclusion": "", "createdAt": _iso(1),
             "databaseId": 2, "event": "workflow_dispatch"},
            {"status": "completed", "conclusion": "success", "createdAt": _iso(600),
             "databaseId": 1, "event": "schedule"}])
        self.assertTrue(ok, detail)
        self.assertNotIn("HUNG", detail)

    def test_a_run_stuck_for_hours_is_still_hung(self):
        ok, detail = self._probe([
            {"status": "in_progress", "conclusion": "", "createdAt": _iso(180),
             "databaseId": 2, "event": "workflow_dispatch"},
            {"status": "completed", "conclusion": "success", "createdAt": _iso(900),
             "databaseId": 1, "event": "schedule"}])
        self.assertFalse(ok)
        self.assertIn("likely HUNG", detail)


class WeekdayDateTokenTest(unittest.TestCase):
    """hedgelab runs weekdays only; {date} (today|yesterday) paged it every
    Sunday and Monday morning on Friday's perfectly good plan."""

    def test_monday_reaches_back_to_friday(self):
        self.assertEqual(fh._weekday_dates(datetime.date(2026, 9, 14)),
                         ["2026-09-14", "2026-09-11"])

    def test_sunday_reaches_back_to_friday(self):
        self.assertEqual(fh._weekday_dates(datetime.date(2026, 9, 13)),
                         ["2026-09-13", "2026-09-11"])

    def test_tuesday_is_today_or_monday(self):
        self.assertEqual(fh._weekday_dates(datetime.date(2026, 9, 15)),
                         ["2026-09-15", "2026-09-14"])


class TrancheNagSuccessLineTest(unittest.TestCase):
    """tranche-nag's success line grew ' in N message(s)' on 2026-09-12; the row
    kept the old shape and paged two delivered reminders as failures."""

    def _pattern(self):
        return next(r for r in fh.FLEET if r["name"].startswith("tranche-nag"))["last_line"]

    def test_current_success_lines_pass(self):
        for line in ("sent 3160 chars in 1 message(s)",
                     "sent 5200 chars in 2 message(s) plain-fallback",
                     "nothing due (50 rows parsed)"):
            self.assertRegex(line, self._pattern())

    def test_failure_lines_still_fail(self):
        for line in ("nothing due (0 rows parsed)",
                     "PARSE FAIL: 0 rows matched ROW_RE in TRANCHE-EXECUTION.md",
                     "Traceback (most recent call last):"):
            self.assertNotRegex(line, self._pattern())
