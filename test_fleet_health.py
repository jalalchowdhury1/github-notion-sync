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
from pathlib import Path

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

class TestLogTailGrace(unittest.TestCase):
    """probe_log_tail grace window: a job mid-retry-chain is not a failure
    (aoife-typing false alarm 2026-09-19, probed 4 min before `wrote coach`)."""
    RE = r"^\d{4}-\d\d-\d\dT[\d:.]+Z wrote coach: "
    MID_RETRY = [
        "2026-09-19T10:20:12Z 2026-09-17: focus kyh",
        "2026-09-19T10:24:12Z   nemotron -> timeout",
        "2026-09-19T10:34:34Z wrote coach: Bubble Train (fallback)",
        "2026-09-19T10:35:43Z 2026-09-17: focus kyh",
        "2026-09-19T10:39:43Z   nemotron -> timeout",
        "2026-09-19T10:30:33Z   free router fallback: deepseek",
    ]

    def _log(self, lines, age_min=0):
        p = os.path.join(tempfile.mkdtemp(), "coach.log")
        Path(p).write_text("\n".join(lines) + "\n")
        if age_min:
            t = time.time() - age_min * 60
            os.utime(p, (t, t))
        return p

    def test_mid_retry_passes_inside_grace(self):
        ok, detail = fh.probe_log_tail(self._log(self.MID_RETRY), self.RE, 2, grace_min=16)
        self.assertTrue(ok, detail)
        self.assertIn("mid-run", detail)

    def test_mid_retry_fails_without_grace(self):
        ok, detail = fh.probe_log_tail(self._log(self.MID_RETRY), self.RE, 2)
        self.assertFalse(ok)
        self.assertIn("not a success", detail)

    def test_no_success_in_window_fails_inside_grace(self):
        lines = [f"2026-09-19T10:{m:02d}:00Z ERROR boom" for m in range(20, 34)]
        ok, detail = fh.probe_log_tail(self._log(lines), self.RE, 2, grace_min=16)
        self.assertFalse(ok)

    def test_success_outside_grace_age_still_fails(self):
        ok, _ = fh.probe_log_tail(self._log(self.MID_RETRY, age_min=20), self.RE, 2, grace_min=16)
        self.assertFalse(ok)

class TestRosterGuards(unittest.TestCase):
    """Rows that exist because of a real silent failure must stay in the roster."""

    def _row(self, prefix):
        return next(r for r in fh.FLEET if r["name"].startswith(prefix))

    def test_alerts_bot_webhook_row_watches_the_digest_tap_endpoint(self):
        # 2026-09-19: @TweetSyn_bot's webhook came back empty; every card button died.
        r = self._row("alerts bot (digest-tap webhook")
        self.assertEqual(r["probe"], "telegram_webhook")
        self.assertEqual(r["token_env"], "TELEGRAM_TOKEN")
        self.assertTrue(r["expect_url"].endswith("/api/defensive"))
        self.assertTrue(r.get("require_guard"))

    def test_aoife_typing_log_tail_has_a_retry_grace_window(self):
        # 2026-09-19: probed 4 min before the cycle's `wrote coach` line and paged.
        r = self._row("aoife-typing")
        self.assertEqual(r["probe"], "log_tail")
        self.assertGreaterEqual(r.get("grace_min", 0), 15)

    def test_reddit_browser_rows_grade_the_run_and_every_list(self):
        # 2026-09-26: Reddit moved to a Mac job; GitHub's RSS backup hides a dead Mac.
        import re
        run = self._row("reddit-browser (Mac")
        self.assertEqual(run["probe"], "log_block")
        self.assertIsNone(run["repo"])
        good = "== 2026-09-26 07:35:01 start\nBROWSER SAVED: 26 of 26 lists\nREDDIT PUSHED: 27 files in 3bcaddd (scraper exit 0)\n"
        m = re.search(run["block_re"], good, re.M)
        self.assertEqual(m.group(1), "2026-09-26 07:35:01")
        self.assertRegex(good, run["log_grep"])
        self.assertRegex("NO REDDIT CHANGES (scraper exit 0)", run["log_grep"])
        self.assertNotRegex("NO REDDIT CHANGES (scraper exit 1)", run["log_grep"])
        self.assertNotRegex("REDDIT PUSHED: 0 files", run["log_grep"])
        site = self._row("reddit-browser (live site")
        self.assertEqual((site["probe"], site["rows_key"], site["json_key"]), ("web_fresh", "reddit_lists", "checked"))
        self.assertIsNone(site["repo"])

    def test_every_telegram_webhook_row_names_a_token_and_a_path(self):
        for r in fh.FLEET:
            if r["probe"] != "telegram_webhook":
                continue
            self.assertTrue(r.get("token_env"), r["name"])
            self.assertTrue(r.get("expect_url", "").startswith("https://"), r["name"])



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


class RetrySlotDigestModeTest(unittest.TestCase):
    """2026-09-15: already_ran_today() used to accept telegram=="sent", which a
    Silent-digest hand-off also set even though the card isn't delivered until
    its 06:50-07:30 autoFlush window — so the 6:30 retry slot skipped instead
    of re-checking, and a problem that self-healed between 5:00 and 6:30 (the
    mental-models 6 AM backstop is exactly this) reached Jalal's card still
    showing the stale 5:00 red."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        self._orig = fh.HEALTH_FILE
        fh.HEALTH_FILE = self.path
        # publish() runs git commit + push. With the real subprocess.run this
        # test committed and pushed whatever was staged in the repo (it did,
        # 2026-09-26: "Fleet health: 15:08"). Record the calls instead.
        self.git_calls = []
        self._orig_run = fh.subprocess.run
        fh.subprocess.run = lambda cmd, *a, **k: (self.git_calls.append(cmd),
                                                  types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]

    def tearDown(self):
        fh.HEALTH_FILE = self._orig
        fh.subprocess.run = self._orig_run
        os.remove(self.path)

    def _write(self, checked, telegram_mode):
        with open(self.path, "w") as f:
            json.dump({"checked": checked, "telegram": "sent" if telegram_mode else "failed",
                      "telegram_mode": telegram_mode, "results": []}, f)

    def test_digest_handoff_does_not_skip_the_retry(self):
        today = datetime.date.today().strftime("%Y-%m-%d %H:%M")
        self._write(today, "digest")
        self.assertFalse(fh.already_ran_today(),
                         "a queued-not-yet-delivered digest hand-off must still retry")

    def test_direct_send_skips_the_retry(self):
        today = datetime.date.today().strftime("%Y-%m-%d %H:%M")
        self._write(today, "direct")
        self.assertTrue(fh.already_ran_today())

    def test_failed_send_still_retries(self):
        today = datetime.date.today().strftime("%Y-%m-%d %H:%M")
        self._write(today, "")
        self.assertFalse(fh.already_ran_today())

    def test_yesterdays_direct_send_does_not_count_for_today(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        self._write(yesterday, "direct")
        self.assertFalse(fh.already_ran_today())

    def test_publish_keeps_the_sent_failed_contract_for_the_dead_mac_watchdog(self):
        """notion_health.py dies on telegram != "sent" — must not regress."""
        fh.publish([], "digest")
        self.assertEqual(json.load(open(self.path))["telegram"], "sent")
        fh.publish([], "direct")
        self.assertEqual(json.load(open(self.path))["telegram"], "sent")
        fh.publish([], "")
        self.assertEqual(json.load(open(self.path))["telegram"], "failed")

    def test_publish_commits_only_health_json(self):
        """A bare `git commit` swept other staged files into the fleet commit."""
        fh.publish([], "digest")
        commit = next(c for c in self.git_calls if "commit" in c)
        self.assertEqual(commit[-2:], ["--", "health.json"])


class ParseStampTest(unittest.TestCase):
    """2026-09-26: a zone suffix was cut off, so UTC stamps read as Mac-local
    and ages came out 4-5 h too young ("-4h old")."""

    def test_utc_and_offset_stamps_become_local(self):
        want = datetime.datetime(2026, 9, 26, 19, 10, 16,
                                 tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None)
        for raw in ("2026-09-26T19:10:16Z", "2026-09-26T19:10:16+00:00",
                    "2026-09-26T15:10:16-0400", "2026-09-26T15:10:16-04:00"):
            self.assertEqual(fh._parse_stamp(raw), want, raw)

    def test_zoneless_stamps_stay_local(self):
        self.assertEqual(fh._parse_stamp("2026-09-26 06:50"), datetime.datetime(2026, 9, 26, 6, 50))
        self.assertEqual(fh._parse_stamp("2026-09-26T00:36"), datetime.datetime(2026, 9, 26, 0, 36))
        self.assertEqual(fh._parse_stamp("2026-09-26"), datetime.datetime(2026, 9, 26))

    def test_fresh_utc_stamp_is_not_negative(self):
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = fh._age_hours(fh._parse_stamp(now).timestamp())
        self.assertGreaterEqual(age, -0.01)
        self.assertLess(age, 0.1)


class RetiredLaunchdRowTest(unittest.TestCase):
    """2026-09-17: a launchd job is retired by `launchctl unload` + renaming
    its plist to `<label>.plist.retired`, never by deleting it. The roster
    still carried a row for com.jalal.supervisor two days after it was
    retired that way, and the digest paged "1 of 64 FAILING — job not
    loaded" for a job Jalal had shut down on purpose. The probes must tell
    retired apart from actually-broken."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_dirs = fh.LAUNCHD_PLIST_DIRS
        fh.LAUNCHD_PLIST_DIRS = [Path(self.tmp.name)]
        self._orig_run = fh.subprocess.run

    def tearDown(self):
        fh.LAUNCHD_PLIST_DIRS = self._orig_dirs
        fh.subprocess.run = self._orig_run
        self.tmp.cleanup()

    def _mock_launchctl(self, stdout_lines):
        fh.subprocess.run = lambda *a, **k: types.SimpleNamespace(
            returncode=0, stdout="\n".join(stdout_lines), stderr="")

    def _touch(self, name):
        open(os.path.join(self.tmp.name, name), "w").close()

    # -- probe_launchd_running --------------------------------------------

    def test_label_present_with_pid_is_unchanged(self):
        self._mock_launchctl(["1234\t0\tcom.jalal.keepawake"])
        ok, detail = fh.probe_launchd_running("com.jalal.keepawake")
        self.assertTrue(ok)
        self.assertEqual(detail, "alive, pid 1234")

    def test_absent_label_with_retired_plist_is_ok_and_labelled_retired(self):
        self._mock_launchctl(["-\t0\tcom.jalal.other"])
        self._touch("com.jalal.supervisor.plist.retired")
        ok, detail = fh.probe_launchd_running("com.jalal.supervisor")
        self.assertTrue(ok, detail)
        self.assertTrue(detail.startswith("RETIRED"), detail)

    def test_absent_label_with_live_plist_says_not_loaded(self):
        self._mock_launchctl(["-\t0\tcom.jalal.other"])
        self._touch("com.jalal.stuck.plist")
        ok, detail = fh.probe_launchd_running("com.jalal.stuck")
        self.assertFalse(ok)
        self.assertIn("launchctl load", detail)

    def test_absent_label_with_no_plist_at_all_says_deleted(self):
        self._mock_launchctl(["-\t0\tcom.jalal.other"])
        ok, detail = fh.probe_launchd_running("com.jalal.ghost")
        self.assertFalse(ok)
        self.assertIn("job deleted", detail)

    # -- probe_launchd_exit (same not-loaded branch) -----------------------

    def test_launchd_exit_absent_label_with_retired_plist_is_ok(self):
        self._mock_launchctl(["-\t0\tcom.jalal.other"])
        self._touch("com.jalal.supervisor.plist.retired")
        ok, detail = fh.probe_launchd_exit("com.jalal.supervisor")
        self.assertTrue(ok, detail)
        self.assertTrue(detail.startswith("RETIRED"), detail)


class RetiredRowDigestTest(unittest.TestCase):
    """format_digest must surface a retired row honestly instead of either
    hiding it or paging it as a failure."""

    def _retired(self, name):
        return {"name": name, "repo": None, "probe": "launchd_running", "ok": True,
                "detail": "RETIRED — plist is com.jalal.supervisor.plist.retired; "
                          "delete this row from fleet_health.py",
                "cfg": "probe=launchd_running · label=com.jalal.supervisor"}

    def _failing(self, name):
        return {"name": name, "repo": "some-repo", "probe": "web_200", "ok": False,
                "detail": "HTTP 500", "cfg": "probe=web_200 · repo=some-repo"}

    def test_failure_still_fails_and_retired_is_grouped_separately(self):
        results = [self._failing("broken-thing"), self._retired("supervisor"),
                   healthy("c")]
        digest = fh.format_digest(results)
        self.assertIn("❌ broken-thing", digest)
        self.assertIn("🗂 retired (1): supervisor", digest)
        self.assertIn("delete this row", digest)
        # totals exclude the retired row: 2 counted (broken-thing, c), 1 bad
        self.assertIn("1 of 2 FAILING", digest)
        self.assertNotIn("supervisor", digest.split("🗂 retired")[0])

    def test_retired_only_day_is_not_reported_as_all_healthy(self):
        results = [self._retired("supervisor"), healthy("c")]
        digest = fh.format_digest(results)
        self.assertIn("0 of 1 FAILING", digest)
        self.assertIn("1 retired", digest)
        self.assertNotIn("🚨", digest)

    def test_all_healthy_with_no_retired_rows_is_unchanged(self):
        results = [healthy("a"), healthy("b")]
        digest = fh.format_digest(results)
        self.assertEqual(digest, f"✅ Fleet check {datetime.date.today().isoformat()} — all 2 systems healthy")

    def _annotate_from_prior_failure(self, new_result):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        orig = fh.HEALTH_FILE
        fh.HEALTH_FILE = path
        try:
            json.dump({"checked": "2026-09-15 05:00",
                       "results": [{"name": "supervisor", "ok": False,
                                    "detail": "job not loaded"}]},
                      open(path, "w"))
            return fh.annotate_history([new_result])
        finally:
            fh.HEALTH_FILE = orig
            os.remove(path)

    def test_recovered_detection_does_not_count_a_retired_row(self):
        recovered = self._annotate_from_prior_failure(self._retired("supervisor"))
        self.assertEqual(recovered, [])

    def test_recovered_detection_still_fires_for_a_genuine_fix(self):
        """Contrast case: a real fix (ok=True, ordinary detail) for the same
        prior failure must still show up as recovered — proves the retired
        exclusion isn't just swallowing every case."""
        genuine_fix = {"name": "supervisor", "repo": None, "probe": "launchd_running",
                       "ok": True, "detail": "alive, pid 999", "cfg": "probe=launchd_running"}
        recovered = self._annotate_from_prior_failure(genuine_fix)
        self.assertIn("supervisor", recovered)
