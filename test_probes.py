"""Per-branch tests for the probe functions (red team round 8, 2026-09-27).

test_fleet_health.py covers the digest, the alert flow and the probes changed in
earlier rounds. This file covers every other probe, one test per return branch,
so a refactor that flips a red into a green fails here first.

Run: python3 -m unittest test_probes -v   (stdlib only, no deps)

Nothing here touches the network beyond 127.0.0.1, runs a real `gh`/`aws`/
`launchctl`/`node`/Chrome, or sends a Telegram message: subprocess.run and
urlopen targets are faked, and files live in temp dirs.
"""
import datetime
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import fleet_health as fh


# ── helpers ──────────────────────────────────────────────────────────────────

class _Server:
    """Local HTTP server. routes: path -> (status, body_bytes_or_str, headers)."""

    def __init__(self, routes):
        routes_ = routes

        class H(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                status, body, headers = routes_.get(self.path.split("?")[0],
                                                     (404, b"nope", {}))
                if callable(body):
                    body = body(self)
                if isinstance(body, str):
                    body = body.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _serve

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                self._serve()

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _proc(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _local(hours_ago=0.0, fmt="%Y-%m-%d %H:%M:%S"):
    return (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).strftime(fmt)


def _utc_iso(hours_ago=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Patched(unittest.TestCase):
    """Restores fh.subprocess.run and env vars after every test."""

    def setUp(self):
        self._run = fh.subprocess.run
        self._env = dict(os.environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        fh.subprocess.run = self._run
        os.environ.clear()
        os.environ.update(self._env)
        self.tmp.cleanup()

    def write(self, name, text, age_h=None):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if age_h is not None:
            t = time.time() - age_h * 3600
            os.utime(p, (t, t))
        return str(p)


# ── web_200 ──────────────────────────────────────────────────────────────────

class WebTwoHundred(_Patched):

    def setUp(self):
        super().setUp()
        self.other = _Server({"/login": (200, "vercel login", {})})
        self.srv = _Server({
            "/ok": (200, "<html>Key Critters</html>", {}),
            "/wall": (302, "", {"Location": self.other.base + "/login"}),
            "/gone": (404, "", {}),
        })

    def tearDown(self):
        self.srv.close()
        self.other.close()
        super().tearDown()

    def test_plain_200_is_green(self):
        self.assertEqual(fh.probe_web_200(self.srv.base + "/ok"), (True, "HTTP 200"))

    def test_expect_text_present_is_green(self):
        ok, d = fh.probe_web_200(self.srv.base + "/ok", expect_text="Key Critters")
        self.assertTrue(ok)
        self.assertIn("app shell", d)

    def test_expect_text_missing_is_red(self):
        ok, d = fh.probe_web_200(self.srv.base + "/ok", expect_text="Zap Zone")
        self.assertFalse(ok)
        self.assertIn("placeholder", d)

    def test_redirect_to_another_host_is_red_even_though_it_ends_200(self):
        ok, d = fh.probe_web_200(self.srv.base + "/wall")
        self.assertFalse(ok)
        self.assertIn("off-host", d)

    def test_404_raises_for_the_retry_path(self):
        # urlopen raises HTTPError on 4xx; run_checks turns it into a red after
        # PROBE_ATTEMPTS. Pinned so nobody "fixes" it into a silent green.
        with self.assertRaises(Exception):
            fh.probe_web_200(self.srv.base + "/gone")


# ── local_stamp / file_mtime ────────────────────────────────────────────────

class LocalStampAndMtime(_Patched):

    def test_today_stamp_is_green(self):
        p = self.write("s", datetime.date.today().isoformat())
        self.assertTrue(fh.probe_local_stamp(p, 30)[0])

    def test_old_stamp_is_red_with_days(self):
        p = self.write("s", (datetime.date.today() - datetime.timedelta(days=4)).isoformat())
        ok, d = fh.probe_local_stamp(p, 30)
        self.assertFalse(ok)
        self.assertIn("d ago", d)

    def test_future_stamp_is_red(self):
        p = self.write("s", (datetime.date.today() + datetime.timedelta(days=3)).isoformat())
        ok, d = fh.probe_local_stamp(p, 30)
        self.assertFalse(ok)
        self.assertIn("FUTURE", d)

    def test_garbage_stamp_raises(self):
        p = self.write("s", "not a date")
        with self.assertRaises(ValueError):
            fh.probe_local_stamp(p, 30)

    def test_mtime_fresh_green_stale_red_missing_red(self):
        fresh = self.write("f", "x", age_h=1)
        stale = self.write("g", "x", age_h=50)
        self.assertTrue(fh.probe_file_mtime(fresh, 26)[0])
        ok, d = fh.probe_file_mtime(stale, 26)
        self.assertFalse(ok)
        self.assertIn("stale", d)
        ok, d = fh.probe_file_mtime(str(self.dir / "nope"), 26)
        self.assertFalse(ok)
        self.assertIn("missing", d)


# ── launchd_exit / launchd_running ──────────────────────────────────────────

class Launchd(_Patched):

    LIST = ("PID\tStatus\tLabel\n"
            "-\t0\tcom.jalal.good\n"
            "-\t78\tcom.jalal.bad\n"
            "412\t0\tcom.jalal.daemon\n"
            "-\t0\tcom.jalal.deadd\n")

    def setUp(self):
        super().setUp()
        self._dirs = fh.LAUNCHD_PLIST_DIRS
        fh.LAUNCHD_PLIST_DIRS = [self.dir]
        fh.subprocess.run = lambda *a, **k: _proc(0, self.LIST)

    def tearDown(self):
        fh.LAUNCHD_PLIST_DIRS = self._dirs
        super().tearDown()

    def test_exit_zero_green_nonzero_red(self):
        self.assertEqual(fh.probe_launchd_exit("com.jalal.good"), (True, "last exit 0"))
        self.assertEqual(fh.probe_launchd_exit("com.jalal.bad"), (False, "last exit 78"))

    def test_label_prefix_does_not_match_a_longer_label(self):
        # "com.jalal.dead" must not be satisfied by "com.jalal.deadd".
        ok, d = fh.probe_launchd_exit("com.jalal.dead")
        self.assertFalse(ok)
        self.assertIn("no com.jalal.dead.plist", d)

    def test_not_loaded_but_plist_present_is_red(self):
        self.write("com.jalal.idle.plist", "<plist/>")
        ok, d = fh.probe_launchd_exit("com.jalal.idle")
        self.assertFalse(ok)
        self.assertIn("NOT LOADED", d)

    def test_retired_plist_is_green_but_says_delete_the_row(self):
        self.write("com.jalal.old.plist.retired", "<plist/>")
        ok, d = fh.probe_launchd_exit("com.jalal.old")
        self.assertTrue(ok)
        self.assertIn("RETIRED", d)

    def test_running_needs_a_pid(self):
        self.assertEqual(fh.probe_launchd_running("com.jalal.daemon"), (True, "alive, pid 412"))
        ok, d = fh.probe_launchd_running("com.jalal.deadd")
        self.assertFalse(ok)
        self.assertIn("NOT RUNNING", d)

    def test_launchctl_failure_raises(self):
        fh.subprocess.run = lambda *a, **k: _proc(1, "", "boom")
        with self.assertRaises(RuntimeError):
            fh.probe_launchd_exit("com.jalal.good")
        with self.assertRaises(RuntimeError):
            fh.probe_launchd_running("com.jalal.daemon")


# ── gh_run ───────────────────────────────────────────────────────────────────

class GhRun(_Patched):
    """Fake `gh`: run list returns self.runs, run view --log returns self.logs[id]."""

    def setUp(self):
        super().setUp()
        self.runs, self.logs, self.calls = [], {}, []

        def fake(cmd, *a, **k):
            self.calls.append(cmd)
            if cmd[:3] == ["gh", "run", "list"]:
                return _proc(0, json.dumps(self.runs))
            if "--log-failed" in cmd:
                return _proc(0, "2026-09-27T01:00:00Z Error: boom\n")
            if "--log" in cmd:
                rid = int(cmd[3])
                if rid not in self.logs:
                    return _proc(1, "", "HTTP 410")
                return _proc(0, self.logs[rid])
            raise AssertionError(cmd)
        fh.subprocess.run = fake

    def run_(self, rid, hours_ago, conclusion="success", status="completed",
             event="schedule"):
        return {"databaseId": rid, "createdAt": _utc_iso(hours_ago), "status": status,
                "conclusion": conclusion if status == "completed" else "", "event": event}

    def test_no_runs_is_red(self):
        self.assertEqual(fh.probe_gh_run("r", "w.yml", 26), (False, "no runs found"))

    def test_list_failure_raises(self):
        fh.subprocess.run = lambda *a, **k: _proc(1, "", "rate limited")
        with self.assertRaises(RuntimeError):
            fh.probe_gh_run("r", "w.yml", 26)

    def test_recent_success_green_old_success_red(self):
        self.runs = [self.run_(1, 2)]
        self.assertTrue(fh.probe_gh_run("r", "w.yml", 26)[0])
        self.runs = [self.run_(1, 40)]
        ok, d = fh.probe_gh_run("r", "w.yml", 26)
        self.assertFalse(ok)
        self.assertIn("no run in", d)

    def test_hung_run_is_red(self):
        self.runs = [self.run_(1, 3, status="in_progress"), self.run_(2, 5)]
        ok, d = fh.probe_gh_run("r", "w.yml", 26)
        self.assertFalse(ok)
        self.assertIn("HUNG", d)

    def test_just_started_run_grades_the_previous_finished_one(self):
        self.runs = [self.run_(1, 0.2, status="in_progress"), self.run_(2, 5)]
        self.assertTrue(fh.probe_gh_run("r", "w.yml", 26)[0])

    def test_utctoday_uses_the_utc_date_in_the_evening(self):
        # 6 Oct 2026, 23:00 ET quiet run: gh_run passed the LOCAL date into
        # _expand_dates, which overrode {utctoday}, so the 21:30 ET run's
        # "slot 2026-10-07-AM" was graded against 2026-10-06 and paged red.
        utc = datetime.datetime.now(datetime.timezone.utc).date()
        self.runs = [self.run_(1, 1)]
        self.logs = {1: f"2026-10-07T01:31:00Z slot {utc.isoformat()}-AM; proceeding.\n"}
        real_date = fh.datetime.date

        class LocalYesterday(real_date):
            @classmethod
            def today(cls):
                return utc - datetime.timedelta(days=1)
        fh.datetime.date = LocalYesterday
        try:
            ok, d = fh.probe_gh_run("r", "w.yml", 26, log_grep=r"slot {utctoday}-(?:AM|PM)")
        finally:
            fh.datetime.date = real_date
        self.assertTrue(ok, d)

    def test_failure_with_no_rescue_shows_log_tail(self):
        self.runs = [self.run_(1, 1, "failure")]
        ok, d = fh.probe_gh_run("r", "w.yml", 26)
        self.assertFalse(ok)
        self.assertIn("Error: boom", d)

    def test_failure_rescued_by_earlier_success(self):
        self.runs = [self.run_(1, 1, "failure"), self.run_(2, 3)]
        ok, d = fh.probe_gh_run("r", "w.yml", 26)
        self.assertTrue(ok)
        self.assertIn("blip", d)

    def test_rescue_needs_the_primary_event_when_expect_event_is_set(self):
        self.runs = [self.run_(1, 1, "failure"), self.run_(2, 3, event="schedule")]
        ok, _ = fh.probe_gh_run("r", "w.yml", 26, expect_event="workflow_dispatch")
        self.assertFalse(ok)

    def test_rescue_never_reaches_past_max_age(self):
        self.runs = [self.run_(1, 1, "failure"), self.run_(2, 30)]
        self.assertFalse(fh.probe_gh_run("r", "w.yml", 26)[0])

    def test_no_rescue_row_stays_red(self):
        self.runs = [self.run_(1, 1, "failure"), self.run_(2, 3)]
        ok, d = fh.probe_gh_run("r", "w.yml", 26, no_rescue=True)
        self.assertFalse(ok)
        self.assertIn("no_rescue", d)

    def test_expect_event_missing_from_window_is_red(self):
        self.runs = [self.run_(1, 1, event="schedule"), self.run_(2, 5, event="schedule")]
        ok, d = fh.probe_gh_run("r", "w.yml", 26, expect_event="workflow_dispatch")
        self.assertFalse(ok)
        self.assertIn("PRIMARY trigger", d)

    def test_expect_event_anywhere_in_window_is_green(self):
        self.runs = [self.run_(1, 1, event="schedule"),
                     self.run_(2, 5, event="workflow_dispatch")]
        self.assertTrue(fh.probe_gh_run("r", "w.yml", 26, expect_event="workflow_dispatch")[0])

    def test_log_grep_hit_green(self):
        self.runs = [self.run_(7, 1)]
        self.logs[7] = f"x\t2026Z WROTE {datetime.date.today()}\n"
        ok, d = fh.probe_gh_run("r", "w.yml", 26, log_grep=r"WROTE {today}")
        self.assertTrue(ok)
        self.assertIn("data confirmed", d)

    def test_log_grep_every_pattern_must_match(self):
        self.runs = [self.run_(7, 1)]
        self.logs[7] = "only ALPHA here\n"
        ok, d = fh.probe_gh_run("r", "w.yml", 26, log_grep=["ALPHA", "BETA"])
        self.assertFalse(ok)
        self.assertIn("'BETA'", d)
        self.assertNotIn("'ALPHA'", d)

    def test_log_grep_date_token_rejects_a_week_old_marker(self):
        old = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        self.runs = [self.run_(7, 1)]
        self.logs[7] = f"WROTE {old}\n"
        self.assertFalse(fh.probe_gh_run("r", "w.yml", 26, log_grep=r"WROTE {date}")[0])

    def test_log_grep_unfetchable_log_raises_not_red(self):
        self.runs = [self.run_(7, 1)]          # no self.logs[7] -> rc 1
        with self.assertRaises(RuntimeError):
            fh.probe_gh_run("r", "w.yml", 26, log_grep="X")

    def test_log_grep_earlier_same_window_run_rescues_a_noop(self):
        self.runs = [self.run_(8, 1), self.run_(7, 4)]
        self.logs[8] = "already done, nothing to do\n"
        self.logs[7] = "MARKER\n"
        ok, d = fh.probe_gh_run("r", "w.yml", 26, log_grep="MARKER")
        self.assertTrue(ok)
        self.assertIn("earlier same-window run", d)

    def test_log_grep_earlier_rescue_is_bounded_and_honours_no_rescue(self):
        self.runs = [self.run_(8, 1), self.run_(7, 40)]
        self.logs[8], self.logs[7] = "noop\n", "MARKER\n"
        self.assertFalse(fh.probe_gh_run("r", "w.yml", 26, log_grep="MARKER")[0])
        self.runs = [self.run_(8, 1), self.run_(7, 4)]
        self.assertFalse(fh.probe_gh_run("r", "w.yml", 26, log_grep="MARKER",
                                         no_rescue=True)[0])

    def test_failed_newest_rescued_run_still_needs_its_marker(self):
        self.runs = [self.run_(8, 1, "failure"), self.run_(7, 4)]
        self.logs[7] = "no marker\n"
        self.assertFalse(fh.probe_gh_run("r", "w.yml", 26, log_grep="MARKER")[0])
        self.logs[7] = "MARKER\n"
        ok, d = fh.probe_gh_run("r", "w.yml", 26, log_grep="MARKER")
        self.assertTrue(ok)
        self.assertIn("blip", d)

    def test_flapping_counts_only_the_window(self):
        self.runs = [self.run_(5, 1, "failure"), self.run_(4, 4),
                     self.run_(3, 40, "failure"), self.run_(2, 44, "failure")]
        # Two failures are outside the 26 h window: 1 fail in window <= 1 -> rescue ok.
        self.assertTrue(fh.probe_gh_run("r", "w.yml", 26, rescue_max_failures=1)[0])


# ── planner_backup / log_marker ─────────────────────────────────────────────

class PlannerAndLogMarker(_Patched):

    def test_planner_green_needs_files_and_marker(self):
        t = datetime.date.today().isoformat()
        for n in ("schedule", "plan"):
            self.write(f"b/{t}-{n}.json", "{}")
        log = self.write("l.log", f"PLANNER-BACKUP OK {t}\n")
        self.assertTrue(fh.probe_planner_backup(str(self.dir / "b"),
                                                ["schedule", "plan"], log)[0])

    def test_planner_yesterdays_files_do_not_count(self):
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.write(f"b/{y}-plan.json", "{}")
        log = self.write("l.log", f"PLANNER-BACKUP OK {y}\n")
        ok, d = fh.probe_planner_backup(str(self.dir / "b"), ["plan"], log)
        self.assertFalse(ok)
        self.assertIn("plan", d)

    def test_planner_corrupt_json_is_red(self):
        t = datetime.date.today().isoformat()
        self.write(f"b/{t}-plan.json", "{not json")
        log = self.write("l.log", f"PLANNER-BACKUP OK {t}\n")
        self.assertFalse(fh.probe_planner_backup(str(self.dir / "b"), ["plan"], log)[0])

    def test_planner_marker_or_log_missing_is_red(self):
        t = datetime.date.today().isoformat()
        self.write(f"b/{t}-plan.json", "{}")
        log = self.write("l.log", "PLANNER-BACKUP FAIL plan\n")
        self.assertFalse(fh.probe_planner_backup(str(self.dir / "b"), ["plan"], log)[0])
        ok, d = fh.probe_planner_backup(str(self.dir / "b"), ["plan"], str(self.dir / "x"))
        self.assertFalse(ok)
        self.assertIn("missing", d)

    def test_planner_live_since_grace(self):
        ok, d = fh.probe_planner_backup("/nope", ["plan"], "/nope", live_since="2999-01-01")
        self.assertTrue(ok)
        self.assertIn("grace", d)

    def test_log_marker_date_token_today_or_yesterday(self):
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        old = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
        log = self.write("m.log", f"OK {y}\n")
        self.assertTrue(fh.probe_log_marker(log, r"OK {date}")[0])
        self.assertFalse(fh.probe_log_marker(log, r"OK {today}")[0])
        log = self.write("m.log", f"OK {old}\n")
        self.assertFalse(fh.probe_log_marker(log, r"OK {date}")[0])

    def test_log_marker_missing_file_and_grace(self):
        self.assertFalse(fh.probe_log_marker(str(self.dir / "x"), "OK")[0])
        self.assertTrue(fh.probe_log_marker(str(self.dir / "x"), "OK",
                                            live_since="2999-01-01")[0])


# ── telegram_webhook ────────────────────────────────────────────────────────

class TelegramWebhook(_Patched):
    """Fakes the getWebhookInfo call; the unauth POST goes to a local server."""

    def setUp(self):
        super().setUp()
        self.info = {}
        self._urlopen = fh.urllib.request.urlopen
        real = self._urlopen

        def fake(req, *a, **k):
            url = req.full_url if hasattr(req, "full_url") else req
            if "api.telegram.org" in url:
                self.assertIn("SECRET_TOKEN_VALUE", url)   # uses the env token
                return _Resp(json.dumps({"ok": True, "result": self.info}))
            return real(req, *a, **k)
        fh.urllib.request.urlopen = fake
        os.environ["T_ENV"] = "SECRET_TOKEN_VALUE"
        self.guard = _Server({"/api/hook": (401, "no", {}),
                              "/api/open": (200, "ok", {}),
                              "/api/down": (502, "bad gateway", {})})

    def tearDown(self):
        fh.urllib.request.urlopen = self._urlopen
        self.guard.close()
        super().tearDown()

    def test_missing_token_env_is_red(self):
        del os.environ["T_ENV"]
        self.assertFalse(fh.probe_telegram_webhook("T_ENV", "https://x/api/hook")[0])

    def test_matching_url_green_and_query_secret_never_echoed(self):
        self.info = {"url": "https://bot.example/api/hook?s=GUARDSECRET",
                     "pending_update_count": 0}
        ok, d = fh.probe_telegram_webhook("T_ENV", "https://bot.example/api/hook")
        self.assertTrue(ok)
        self.info["url"] = "https://evil.example/api/hook?s=GUARDSECRET"
        ok, d = fh.probe_telegram_webhook("T_ENV", "https://bot.example/api/hook")
        self.assertFalse(ok)
        self.assertNotIn("GUARDSECRET", d)
        self.assertNotIn("SECRET_TOKEN_VALUE", d)

    def test_empty_webhook_says_deaf(self):
        self.info = {"url": ""}
        ok, d = fh.probe_telegram_webhook("T_ENV", "https://bot.example/api/hook")
        self.assertFalse(ok)
        self.assertIn("deaf", d)

    def test_recent_error_red_old_error_green(self):
        self.info = {"url": "https://bot.example/api/hook",
                     "last_error_message": "Wrong response 500",
                     "last_error_date": int(time.time()) - 3600}
        self.assertFalse(fh.probe_telegram_webhook("T_ENV", "https://bot.example/api/hook")[0])
        self.info["last_error_date"] = int(time.time()) - 3 * 86400
        self.assertTrue(fh.probe_telegram_webhook("T_ENV", "https://bot.example/api/hook")[0])

    def test_guard_401_green_open_red_5xx_raises(self):
        for path, want in (("/api/hook", True), ("/api/open", False)):
            self.info = {"url": self.guard.base + path}
            ok, d = fh.probe_telegram_webhook("T_ENV", self.guard.base + path,
                                              require_guard=True)
            self.assertEqual(ok, want, d)
        self.info = {"url": self.guard.base + "/api/down"}
        with self.assertRaises(RuntimeError):
            fh.probe_telegram_webhook("T_ENV", self.guard.base + "/api/down",
                                      require_guard=True)


class _Resp:
    def __init__(self, body, status=200):
        self._b, self.status = body.encode(), status

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── bot_selftest ─────────────────────────────────────────────────────────────

class BotSelftest(_Patched):

    def setUp(self):
        super().setUp()
        seen = self.seen = {}

        def record(h):
            seen["headers"] = dict(h.headers)
            return json.dumps({"ok": True, "sent": 1})
        self.srv = _Server({
            "/good": (200, record, {}),
            "/apology": (200, json.dumps({"ok": False, "sent": 1, "why": "planner down"}), {}),
            "/silent": (200, json.dumps({"ok": True, "sent": 0}), {}),
            "/html": (200, "<html>old deploy</html>", {}),
            "/denied": (403, "no", {}),
            "/cold": (503, "cold", {}),
        })
        os.environ["S_ENV"] = "hunter2"

    def tearDown(self):
        self.srv.close()
        super().tearDown()

    def test_real_reply_green_and_secret_goes_only_in_the_header(self):
        ok, d = fh.probe_bot_selftest(self.srv.base + "/good", "S_ENV")
        self.assertTrue(ok)
        self.assertEqual(self.seen["headers"].get("X-Selftest"), "1")
        self.assertEqual(self.seen["headers"].get("X-Telegram-Bot-Api-Secret-Token"), "hunter2")
        self.assertNotIn("hunter2", d)

    def test_every_failure_shape(self):
        for path, frag in (("/apology", "planner down"), ("/silent", "no verdict"),
                           ("/html", "non-JSON"), ("/denied", "secret rejected")):
            ok, d = fh.probe_bot_selftest(self.srv.base + path, "S_ENV")
            self.assertFalse(ok, path)
            self.assertIn(frag, d, path)

    def test_5xx_raises_for_retry(self):
        with self.assertRaises(RuntimeError):
            fh.probe_bot_selftest(self.srv.base + "/cold", "S_ENV")

    def test_missing_secret_env_red(self):
        del os.environ["S_ENV"]
        self.assertFalse(fh.probe_bot_selftest(self.srv.base + "/good", "S_ENV")[0])


# ── nuts ─────────────────────────────────────────────────────────────────────

class Nuts(_Patched):

    def good(self):
        return {"unit_test": {"pass": True},
                "download_errors": {},
                "data_quality": {"SPY": {"last_date": _local(24, "%Y-%m-%d")},
                                 "TQQQ": {"last_date": _local(24, "%Y-%m-%d")}},
                "evaluated_at": _local(12, "%Y-%m-%d %H:%M"),
                "final_result": "TQQQ", "final_source": "frontrunners"}

    def grade(self, payload):
        srv = _Server({"/evaluate": (200, json.dumps(payload), {})})
        try:
            return fh.probe_nuts(srv.base + "/evaluate")
        finally:
            srv.close()

    def test_healthy_payload_green(self):
        ok, d = self.grade(self.good())
        self.assertTrue(ok, d)
        self.assertIn("TQQQ", d)

    def test_each_assertion_fails_on_its_own(self):
        cases = [
            ("unit_test", {"pass": False, "expected": 30, "calculated": 70}, "DO NOT TRADE"),
            ("download_errors", {"SPY": "timeout"}, "download_errors"),
            ("data_quality", {}, "no data_quality"),
            ("data_quality", {"SPY": {"last_date": _local(24 * 9, "%Y-%m-%d")},
                              "TQQQ": {"last_date": _local(24, "%Y-%m-%d")}}, "stale prices: SPY"),
            ("evaluated_at", None, "no evaluated_at"),
            ("evaluated_at", _local(120, "%Y-%m-%d %H:%M"), "recompute stalled"),
            ("final_result", "", "final_result empty"),
        ]
        for key, val, frag in cases:
            p = self.good()
            p[key] = val
            ok, d = self.grade(p)
            self.assertFalse(ok, key)
            self.assertIn(frag, d, key)


# ── nuts_radar ───────────────────────────────────────────────────────────────

class NutsRadar(_Patched):

    def setUp(self):
        super().setUp()
        self.cat = {"generated_at": _local(1, "%Y-%m-%d %H:%M"), "items": [1, 2, 3],
                    "missing": []}
        self.srv = _Server({"/": (200, "radar", {}),
                            "/cat.json": (200, lambda h: json.dumps(self.cat), {}),
                            "/gone.json": (404, "", {}),
                            "/down.json": (502, "", {}),
                            "/bad.json": (200, "{nope", {})})
        self.write("repo/job/selfcheck.js", "// fake")
        self.node = _proc(0, "PASS frontrunners\nbook = TQQQ 60 / BIL 40\n")
        fh.subprocess.run = lambda cmd, *a, **k: self.node

    def tearDown(self):
        self.srv.close()
        super().tearDown()

    def grade(self, cat_path="/cat.json"):
        return fh.probe_nuts_radar(self.srv.base + "/", str(self.dir / "repo"),
                                   catalysts_url=self.srv.base + cat_path)

    def test_healthy_green_with_book(self):
        ok, d = self.grade()
        self.assertTrue(ok, d)
        self.assertIn("TQQQ 60 / BIL 40", d)
        self.assertIn("3 catalysts", d)

    def test_selfcheck_failure_is_drift(self):
        self.node = _proc(1, "FAIL ftlt: expected SQQQ got BIL\n")
        ok, d = self.grade()
        self.assertFalse(ok)
        self.assertIn("DRIFTED", d)
        self.assertIn("FAIL ftlt", d)

    def test_missing_selfcheck_red(self):
        os.remove(self.dir / "repo/job/selfcheck.js")
        ok, d = self.grade()
        self.assertFalse(ok)
        self.assertIn("missing", d)

    def test_catalyst_branches(self):
        self.cat = {"generated_at": _local(40, "%Y-%m-%d %H:%M")}
        self.assertIn("stale", self.grade()[1])
        self.cat = {"items": []}
        self.assertIn("never built", self.grade()[1])
        self.assertFalse(self.grade("/gone.json")[0])
        self.assertFalse(self.grade("/bad.json")[0])
        with self.assertRaises(RuntimeError):
            self.grade("/down.json")

    def test_source_issue_is_noted_not_red(self):
        self.cat["missing"] = ["FOMC calendar 404"]
        ok, d = self.grade()
        self.assertTrue(ok)
        self.assertIn("FOMC calendar", d)


# ── one_clock_lambda ─────────────────────────────────────────────────────────

class OneClockLambda(_Patched):
    """Fake aws: --filter-pattern calls return text lines; the unfiltered call
    returns the JSON event stream used to group retries by RequestId."""

    def setUp(self):
        super().setUp()
        self.errors, self.pings, self.dispatches, self.events = [], ["PING OK"] * 5, \
            ["DISPATCH OK repo=a"] * 6, []

        def fake(cmd, *a, **k):
            if "--filter-pattern" in cmd:
                pat = cmd[cmd.index("--filter-pattern") + 1]
                lines = (self.errors if "ERROR" in pat else
                         self.pings if "PING" in pat else self.dispatches)
                return _proc(0, "\n".join(lines))
            return _proc(0, json.dumps(self.events))
        fh.subprocess.run = fake

    def test_healthy_green(self):
        ok, d = fh.probe_one_clock_lambda()
        self.assertTrue(ok)
        self.assertIn("0 errors", d)

    def ev(self, *pairs):
        return [{"t": i, "m": m} for i, m in enumerate(pairs)]

    def test_error_healed_by_retry_of_same_request_is_green(self):
        self.errors = ["[ERROR] HTTPError: HTTP Error 502"]
        self.events = self.ev("START RequestId: aa1", "[ERROR] HTTPError 502",
                              "REPORT RequestId: aa1", "START RequestId: aa1",
                              "DISPATCH OK repo=x", "REPORT RequestId: aa1")
        ok, d = fh.probe_one_clock_lambda()
        self.assertTrue(ok)
        self.assertIn("self-healed", d)

    def test_error_on_every_attempt_is_red(self):
        self.errors = ["[ERROR] HTTPError: HTTP Error 401"]
        self.events = self.ev("START RequestId: bb2", "[ERROR] 401", "REPORT RequestId: bb2",
                              "START RequestId: bb2", "[ERROR] 401", "REPORT RequestId: bb2")
        ok, d = fh.probe_one_clock_lambda()
        self.assertFalse(ok)
        self.assertIn("never recovered", d)

    def test_timeout_counts_as_an_error(self):
        self.errors = ["Task timed out after 30.00 seconds"]
        self.events = self.ev("START RequestId: cc3", "Task timed out after 30.00 seconds",
                              "REPORT RequestId: cc3")
        self.assertFalse(fh.probe_one_clock_lambda()[0])

    def test_too_few_pings_or_dispatches_is_red(self):
        self.pings = ["PING OK"]
        self.assertIn("PING OK", fh.probe_one_clock_lambda()[1])
        self.pings = ["PING OK"] * 5
        self.dispatches = ["DISPATCH OK repo=a"]
        self.assertIn("PAT", fh.probe_one_clock_lambda()[1])

    def test_aws_failure_raises(self):
        fh.subprocess.run = lambda *a, **k: _proc(255, "", "ExpiredToken")
        with self.assertRaises(RuntimeError):
            fh.probe_one_clock_lambda()


# ── rsync_log ────────────────────────────────────────────────────────────────

class RsyncLog(_Patched):

    def block(self, hours_ago=2, code=0, files="31,007 (reg: 26,975, dir: 4,030)", done=True):
        s = _local(hours_ago + 0.1)
        e = _local(hours_ago)
        out = f"=== {s} sync start ===\nNumber of files: {files}\n" \
              f"Number of regular files transferred: 12\n"
        if done:
            out += f"=== {e} sync done (exit {code}) ===\n"
        return out

    def grade(self, text, **kw):
        return fh.probe_rsync_log(self.write("rsync.log", text), min_files=20000, **kw)

    def test_healthy_green(self):
        ok, d = self.grade(self.block())
        self.assertTrue(ok, d)
        self.assertIn("26,975", d)

    def test_only_the_last_block_counts(self):
        ok, d = self.grade(self.block(30) + self.block(2, code=23))
        self.assertFalse(ok)
        self.assertIn("exited 23", d)

    def test_each_failure(self):
        cases = [
            ("", "no rsync run block"),
            (self.block(done=False), "never finished"),
            (self.block(50), "finished 50h ago"),
            (self.block(files="1 (dir: 1)"), "NO regular files"),
            (self.block(files="400 (reg: 300, dir: 100)"), "only 300"),
        ]
        for text, frag in cases:
            ok, d = self.grade(text)
            self.assertFalse(ok, frag)
            self.assertIn(frag, d)

    def test_missing_log_says_unmounted(self):
        ok, d = fh.probe_rsync_log(str(self.dir / "none.log"))
        self.assertFalse(ok)
        self.assertIn("not mounted", d)


# ── log_block ────────────────────────────────────────────────────────────────

class LogBlock(_Patched):
    RE = r"^=== toolcheck (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ==="

    def test_newest_block_graded_alone(self):
        old = f"=== toolcheck {_local(24 * 14)} ===\n24 passed, 0 failed\n"
        new = f"=== toolcheck {_local(24 * 2)} ===\n22 passed, 2 failed\n"
        p = self.write("t.log", old + new)
        ok, d = fh.probe_log_block(p, self.RE, r"\d+ passed, 0 failed", 24 * 8)
        self.assertFalse(ok)
        self.assertIn("marker missing", d)
        p = self.write("t.log", new + f"=== toolcheck {_local(1)} ===\n24 passed, 0 failed\n")
        self.assertTrue(fh.probe_log_block(p, self.RE, r"\d+ passed, 0 failed", 24 * 8)[0])

    def test_stale_block_and_no_block_and_missing(self):
        p = self.write("t.log", f"=== toolcheck {_local(24 * 10)} ===\n24 passed, 0 failed\n")
        self.assertIn("stopped firing", fh.probe_log_block(p, self.RE, "passed", 24 * 8)[1])
        p = self.write("t.log", "random\n")
        self.assertFalse(fh.probe_log_block(p, self.RE, "passed", 24 * 8)[0])
        self.assertFalse(fh.probe_log_block(str(self.dir / "x"), self.RE, "passed", 24)[0])


# ── web_render (fake browser output) ─────────────────────────────────────────

class WebRender(_Patched):

    def setUp(self):
        super().setUp()
        self.out = _proc(0, b"", b"")
        fh.subprocess.run = lambda *a, **k: self.out

    def dom(self, body):
        return ("<html><head><title>Key Critters</title></head><body>"
                + body + "</body></html>" + " " * 300).encode()

    def test_rendered_app_green(self):
        self.out = _proc(0, self.dom("<div>Start typing</div>"), b"")
        self.assertTrue(fh.probe_web_render("http://x", "Start typing")[0])

    def test_text_only_in_head_does_not_count(self):
        self.out = _proc(0, self.dom("<div>blank</div>"), b"")
        self.assertFalse(fh.probe_web_render("http://x", "Key Critters")[0])

    def test_js_error_and_next_crash_and_empty_dom(self):
        self.out = _proc(0, self.dom("<div>Start typing</div>"),
                         b'[1:2:INFO:CONSOLE(3)] "Uncaught TypeError: x is undefined", source: a.js\n')
        ok, d = fh.probe_web_render("http://x", "Start typing")
        self.assertFalse(ok)
        self.assertIn("Uncaught TypeError", d)
        self.out = _proc(0, self.dom("Application error: a client-side exception has occurred"), b"")
        self.assertIn("crashed", fh.probe_web_render("http://x", "Start")[1])
        self.out = _proc(0, b"<html></html>", b"")
        self.assertIn("no rendered page", fh.probe_web_render("http://x", "Start")[1])

    def test_full_chrome_timeout_still_grades_the_dumped_dom(self):
        dom = self.dom("<div>Start typing</div>")

        def hang(*a, **k):
            raise subprocess.TimeoutExpired("chrome", 45, output=dom, stderr=b"")
        fh.subprocess.run = hang
        self.assertTrue(fh.probe_web_render("http://x", "Start typing")[0])


# ── run_checks retry wrapper ─────────────────────────────────────────────────

class RunChecksRetry(_Patched):

    def setUp(self):
        super().setUp()
        self._fleet, self._fns = fh.FLEET, dict(fh.PROBE_FNS)
        self._pause, self._lint = fh.PROBE_RETRY_PAUSE_S, fh.lint_roster
        fh.PROBE_RETRY_PAUSE_S = 0
        fh.lint_roster = lambda fleet=None: []

    def tearDown(self):
        fh.FLEET, fh.PROBE_RETRY_PAUSE_S, fh.lint_roster = self._fleet, self._pause, self._lint
        fh.PROBE_FNS.clear()
        fh.PROBE_FNS.update(self._fns)
        super().tearDown()

    def test_transient_then_success_is_green(self):
        n = {"calls": 0}

        def flaky(**_):
            n["calls"] += 1
            if n["calls"] < 2:
                raise RuntimeError("502")
            return True, "fine"
        fh.PROBE_FNS["flaky"] = flaky
        fh.FLEET = [{"name": "a", "probe": "flaky"}]
        r = fh.run_checks()
        self.assertTrue(r[0]["ok"])
        self.assertEqual(n["calls"], 2)

    def test_always_raising_is_red_after_all_attempts(self):
        def dead(**_):
            raise RuntimeError("boom")
        fh.PROBE_FNS["dead"] = dead
        fh.FLEET = [{"name": "a", "probe": "dead"}]
        r = fh.run_checks()
        self.assertFalse(r[0]["ok"])
        self.assertIn(f"after {fh.PROBE_ATTEMPTS} attempts", r[0]["detail"])

    def test_a_returned_red_is_not_retried(self):
        n = {"calls": 0}

        def red(**_):
            n["calls"] += 1
            return False, "real failure"
        fh.PROBE_FNS["red"] = red
        fh.FLEET = [{"name": "a", "probe": "red"}]
        self.assertFalse(fh.run_checks()[0]["ok"])
        self.assertEqual(n["calls"], 1)

    def test_lint_failure_stops_the_run(self):
        fh.lint_roster = lambda fleet=None: [{"name": "weak row"}]
        with self.assertRaises(SystemExit):
            fh.run_checks()


# ── _telegram_send routing ───────────────────────────────────────────────────

class TelegramSend(_Patched):

    def setUp(self):
        super().setUp()
        self._post, self._urlopen = fh.digest_post, fh.urllib.request.urlopen
        self.sent = []

        def fake_urlopen(req, *a, **k):
            self.sent.append(json.loads(req.data))
            return _Resp("{}")
        fh.urllib.request.urlopen = fake_urlopen
        self._sleep = fh.time.sleep
        fh.time.sleep = lambda s: None
        os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "t", "c"

    def tearDown(self):
        fh.digest_post, fh.urllib.request.urlopen = self._post, self._urlopen
        fh.time.sleep = self._sleep
        super().tearDown()

    def test_silent_goes_to_the_digest_first(self):
        fh.digest_post = lambda *a, **k: True
        self.assertEqual(fh._telegram_send("x", silent=True), "digest")
        self.assertEqual(self.sent, [])

    def test_digest_down_falls_back_to_a_silent_direct_send(self):
        fh.digest_post = lambda *a, **k: False
        self.assertEqual(fh._telegram_send("x", silent=True), "direct")
        self.assertTrue(self.sent[0]["disable_notification"])

    def test_loud_never_uses_the_digest(self):
        fh.digest_post = lambda *a, **k: self.fail("loud alert went to the digest")
        self.assertEqual(fh._telegram_send("x"), "direct")
        self.assertFalse(self.sent[0]["disable_notification"])

    def test_no_creds_or_all_attempts_failing_returns_empty(self):
        fh.digest_post = lambda *a, **k: False

        def boom(*a, **k):
            raise OSError("down")
        fh.urllib.request.urlopen = boom
        self.assertEqual(fh._telegram_send("x"), "")
        del os.environ["TELEGRAM_TOKEN"]
        self.assertEqual(fh._telegram_send("x"), "")


class LoudAlertDenominator(unittest.TestCase):
    """Round 8: the buzz said "N of 71" while the digest said "N of 68" —
    retired rows are not systems in either message."""

    def test_retired_rows_are_not_counted(self):
        sent = []
        orig = fh._telegram_send
        fh._telegram_send = lambda text, silent=False: (sent.append(text), "direct")[1]
        try:
            res = [{"name": "a", "ok": False, "detail": "x"},
                   {"name": "b", "ok": True, "detail": "HTTP 200"},
                   {"name": "old", "ok": True, "detail": "RETIRED — plist is x.plist.retired"}]
            fh.loud_alert(res, now=datetime.datetime(2026, 9, 28, 6, 31), prev={})
        finally:
            fh._telegram_send = orig
        self.assertIn("1 of 2 systems failing", sent[0])


class LoudAlertCheckerTrouble(unittest.TestCase):
    """Round 8: probe errors (checker could not look) are named apart from real reds."""

    def _send(self, res):
        sent = []
        orig = fh._telegram_send
        fh._telegram_send = lambda text, silent=False: (sent.append(text), "direct")[1]
        try:
            fh.loud_alert(res, now=datetime.datetime(2026, 9, 28, 6, 31), prev={})
        finally:
            fh._telegram_send = orig
        return sent[0]

    def test_outage_only_is_not_called_systems_failing(self):
        res = [{"name": f"r{i} (x)", "ok": False,
                "detail": "probe error: RuntimeError: gh run list failed (after 3 attempts)"}
               for i in range(20)]
        msg = self._send(res)
        self.assertNotIn("systems failing", msg)
        self.assertIn("could not look at 20 of 20", msg)

    def test_mixed_counts_real_reds_only(self):
        res = [{"name": "a (x)", "ok": False, "detail": "last run failure"},
               {"name": "b (x)", "ok": False, "detail": "probe error: TimeoutError"},
               {"name": "c", "ok": True, "detail": "ok"}]
        msg = self._send(res)
        self.assertIn("1 of 3 systems failing", msg)
        self.assertIn("Also could not check 1", msg)
        self.assertIn("b", msg.splitlines()[-2])


class CardWindow(unittest.TestCase):
    """notion_health: a LATE card is not a MISSING card (round 8)."""

    def setUp(self):
        import notion_health as nh
        self.nh = nh
        self._open, self._die, self._urlopen = nh._card_window_open, nh.die, nh.urllib.request.urlopen
        self.died = []

        def die(msg):
            self.died.append(msg)
            raise SystemExit(1)
        nh.die = die

    def tearDown(self):
        nh = self.nh
        nh._card_window_open, nh.die, nh.urllib.request.urlopen = self._open, self._die, self._urlopen

    def _check(self, hours_ago, window_open):
        stamp = (datetime.datetime.now() - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")
        self.nh.urllib.request.urlopen = lambda *a, **k: _Resp(json.dumps({"digest_fleet_at": stamp}))
        self.nh._card_window_open = lambda now_utc=None: window_open
        try:
            self.nh.check_card_delivered("2026-09-28 06:31")
        except SystemExit:
            pass
        return not self.died

    def test_todays_card_passes(self):
        self.assertTrue(self._check(2, window_open=True))

    def test_yesterdays_card_waits_while_window_open(self):
        self.assertTrue(self._check(26, window_open=True))

    def test_yesterdays_card_dies_after_window(self):
        self.assertFalse(self._check(26, window_open=False))

    def test_two_day_old_card_dies_even_inside_window(self):
        self.assertFalse(self._check(50, window_open=True))

    def test_window_clock_is_eastern(self):
        utc = datetime.timezone.utc
        self.assertTrue(self.nh._card_window_open(datetime.datetime(2026, 9, 28, 12, 37, tzinfo=utc)))   # 08:37 EDT
        self.assertFalse(self.nh._card_window_open(datetime.datetime(2026, 9, 28, 14, 30, tzinfo=utc)))  # 10:30 EDT
        self.assertTrue(self.nh._card_window_open(datetime.datetime(2026, 12, 1, 14, 30, tzinfo=utc)))   # 09:30 EST


class LogBlockFailGrep(_Patched):
    """Round 8: rubber-band prints `published` even when curves failed."""
    RE = r"^rubber-band run (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)"

    def _block(self, extra=""):
        t = _local(2, "%Y-%m-%dT%H:%M:%S")
        return (f"rubber-band run {t}\n  curve C3: 3266 days to x\n{extra}"
                f"  published → https://gist.githubusercontent.com/u/raw/rb.json\n=== exit 0 ===\n")

    def grade(self, text):
        row = next(r for r in fh.FLEET if r["name"].startswith("rubber-band"))
        return fh.probe_log_block(self.write("rb.log", text), row["block_re"], row["log_grep"],
                                  row["max_age_h"], fail_grep=row["fail_grep"],
                                  weekday_only=row.get("weekday_only", False))

    def test_clean_block_green(self):
        self.assertTrue(self.grade(self._block())[0])

    def test_failed_curve_or_unsent_alert_is_red(self):
        for bad in ("  curve m1: FAILED (401)\n", "  alert (2 changes): NOT sent\n"):
            ok, d = self.grade(self._block(bad))
            self.assertFalse(ok, bad)
            self.assertIn("part failed", d)

    def test_nonzero_exit_is_red(self):
        self.assertFalse(self.grade(self._block().replace("exit 0", "exit 1"))[0])

    def test_failure_in_an_older_block_does_not_count(self):
        old = self._block("  curve m1: FAILED (401)\n").replace(_local(2, "%Y-%m-%dT%H"), _local(30, "%Y-%m-%dT%H"))
        self.assertTrue(self.grade(old + self._block())[0])


class WeekdayAge(unittest.TestCase):
    """10 Oct 2026: rubber-band (weekdays 18:30) was a flat 72 h, so a missed
    Tuesday stayed green until Thursday evening."""

    def ts(self, *a):
        return datetime.datetime(*a).timestamp()

    def test_weekend_is_not_counted(self):
        # Fri 9 Oct 18:30 -> Mon 12 Oct 05:00 = 10.5 weekday hours
        self.assertAlmostEqual(fh._weekday_age_hours(self.ts(2026, 10, 9, 18, 30),
                                                     self.ts(2026, 10, 12, 5, 0)), 10.5)

    def test_missed_tuesday_is_red_by_wednesday_morning(self):
        row = next(r for r in fh.FLEET if r["name"].startswith("rubber-band"))
        self.assertTrue(row.get("weekday_only"))
        # Mon 18:30 -> Wed 05:00 = 34.5 h > limit; Mon 18:30 -> Tue 20:00 = 25.5 h ok
        self.assertGreater(fh._weekday_age_hours(self.ts(2026, 10, 12, 18, 30),
                                                 self.ts(2026, 10, 14, 5, 0)), row["max_age_h"])
        self.assertLess(fh._weekday_age_hours(self.ts(2026, 10, 12, 18, 30),
                                              self.ts(2026, 10, 13, 20, 0)), row["max_age_h"])
        # and a normal Friday -> Monday 06:30 re-check stays green
        self.assertLess(fh._weekday_age_hours(self.ts(2026, 10, 9, 18, 30),
                                              self.ts(2026, 10, 12, 6, 30)), row["max_age_h"])


class TightenedMarkers(unittest.TestCase):
    """Round 8 producer-side fakes: each marker must reject the fake shape."""

    def row(self, prefix):
        return next(r for r in fh.FLEET if r["name"].startswith(prefix))

    def test_am_reads_needs_a_real_parse(self):
        pats = self.row("reddit-scraper (AM Reads")["log_grep"]
        real = ("a\tb\t2026-09-26T18:40:07.9294951Z   Fetching AM Reads post: https://ritholtz.com/2026/09/x/...\n"
                "a\tb\t2026-09-26T18:40:07.9295917Z     Extracted 11 articles (after in-post dedup)\n"
                "a\tb\t2026-09-26T18:40:07.9297891Z Unchanged: data/ritholtz/articles.csv already holds this post (10 articles)\n"
                "a\tb\t2026-09-26T18:40:09.3574046Z NO CHANGE: AM Reads already current\n")
        fake = ("a\tb\t2026-09-26T18:40:07.9294951Z   Fetching AM Reads post: https://ritholtz.com/2026/09/x/...\n"
                "a\tb\t2026-09-26T18:40:07.9295917Z No articles to save\n"
                "a\tb\t2026-09-26T18:40:09.3574046Z NO CHANGE: AM Reads already current\n")
        import re
        self.assertTrue(all(re.search(p, real) for p in pats))
        self.assertFalse(all(re.search(p, fake) for p in pats))

    def test_reddit_browser_failed_scrape_push_is_red(self):
        import re
        pat = self.row("reddit-browser (Mac")["log_grep"]
        self.assertTrue(re.search(pat, "REDDIT PUSHED: 27 files in eb8d61d (scraper exit 0)"))
        self.assertFalse(re.search(pat, "REDDIT PUSHED: 1 files in eb8d61d (scraper exit 1)"))

    def test_sheets_backup_zero_tabs_is_red(self):
        import re
        pat = self.row("sheets-backup")["log_grep"]
        self.assertTrue(re.search(pat, "2026-09-27T11:57:55.1221940Z BACKUP OK: 52 owned tabs, 6 public sources"))
        self.assertFalse(re.search(pat, "2026-09-27T11:57:55.1221940Z BACKUP OK: 0 owned tabs, 6 public sources"))
        # the Actions echo of the workflow source never carries a timestamp before it
        self.assertFalse(re.search(pat, '\x1b[36;1mecho "BACKUP OK: 5 owned tabs, 6 public sources"'))


class DateTokens(unittest.TestCase):
    """Round 8: one expander for every probe; the lint refuses tokens a probe
    would search for literally."""

    def test_expand(self):
        mon = datetime.date(2026, 9, 28)
        self.assertEqual(fh._expand_dates("x {today}", mon), "x 2026-09-28")
        self.assertEqual(fh._expand_dates("{date}", mon), "(?:2026-09-28|2026-09-27)")
        self.assertEqual(fh._expand_dates("{weekday}", mon), "(?:2026-09-28|2026-09-25)")
        self.assertEqual(fh._expand_dates(r"\d{4}", mon), r"\d{4}")   # quantifiers untouched
        self.assertEqual(fh._expand_dates("slot {utctoday}", mon), "slot 2026-09-28")

    def test_log_marker_now_understands_weekday(self):
        import re
        self.assertTrue(re.search(fh._expand_dates("OK {weekday}", datetime.date(2026, 9, 28)),
                                  "OK 2026-09-25"))

    def lint(self, **row):
        base = {"name": "t", "weak_ok": None}
        return fh.lint_roster([dict(base, **row)])

    def test_lint_rejects_unexpanded_tokens(self):
        self.assertTrue(self.lint(probe="log_block", log_grep="OK {date}", block_re="x", max_age_h=1))
        self.assertTrue(self.lint(probe="log_marker", log_grep="OK {yesterday}"))
        self.assertTrue(self.lint(probe="cloudwatch_marker", log_grep="OK {today}", today_only=False))
        bad = self.lint(probe="log_marker", log_grep="OK {yesterday}", weak_ok="excuse")
        self.assertTrue(bad, "weak_ok must not excuse a token that can never match")
        self.assertIn("never expanded", bad[0]["lint_why"])

    def test_lint_accepts_supported_tokens_and_quantifiers(self):
        self.assertFalse(self.lint(probe="log_marker", log_grep=r"OK {weekday} \d{2}"))
        self.assertFalse(self.lint(probe="gh_run", log_grep="OK {today}", repo="r", workflow="w"))
        self.assertFalse(self.lint(probe="cloudwatch_marker", log_grep="REPORT_DELIVERED {today}"))


class WebFreshFailKey(unittest.TestCase):
    """Round 8: health-hub's newest send failing beats a fresh last_tick."""

    def grade(self, payload):
        srv = _Server({"/h": (200, json.dumps(payload), {})})
        try:
            return fh.probe_web_fresh(srv.base + "/h", "send_ok_at", 26,
                                      fail_key="send_error_at", fail_note_key="send_error")
        finally:
            srv.close()

    def test_ok_newer_than_error_green(self):
        self.assertTrue(self.grade({"send_ok_at": _local(1, "%Y-%m-%d %H:%M"),
                                    "send_error_at": _local(5, "%Y-%m-%d %H:%M")})[0])

    def test_error_newer_than_ok_red_with_reason(self):
        ok, d = self.grade({"send_ok_at": _local(5, "%Y-%m-%d %H:%M"),
                            "send_error_at": _local(1, "%Y-%m-%d %H:%M"),
                            "send_error": "403 bot was blocked"})
        self.assertFalse(ok)
        self.assertIn("blocked", d)

    def test_never_failed_green_never_sent_red(self):
        self.assertTrue(self.grade({"send_ok_at": _local(1, "%Y-%m-%d %H:%M")})[0])
        self.assertFalse(self.grade({"send_error_at": _local(1, "%Y-%m-%d %H:%M")})[0])


class DatedLogTail(_Patched):
    """Round 8: dhaka-yearly's per-day run log, graded by its last line."""

    def row(self):
        return next(r for r in fh.FLEET if r["name"].startswith("dhaka-yearly (01:15"))

    def grade(self, last):
        name = f"run-{datetime.date.today():%Y%m%d}.log"
        self.write(name, f"== engine ...\ncheck: 58 plans, 0 problem(s)\n{last}\n")
        r = dict(self.row(), path=str(self.dir / "run-{today_ymd}.log"))
        return fh.probe_log_tail(**r)

    def test_ok_line_green(self):
        self.assertTrue(self.grade("NIGHTLY OK plans=58 problems=0 pushed=0db75b1 notify=ok")[0])

    def test_failed_or_old_format_red(self):
        for last in ("NIGHTLY FAILED: push, notify", "NIGHTLY FAILED: deploy (alert failed)",
                     "NIGHTLY OK plans=0 problems=0 pushed=0db75b1 notify=ok", "== done"):
            self.assertFalse(self.grade(last)[0], last)

    def test_yesterdays_file_does_not_count(self):
        y = datetime.date.today() - datetime.timedelta(days=1)
        self.write(f"run-{y:%Y%m%d}.log", "NIGHTLY OK plans=58 problems=0 pushed=0db75b1 notify=ok\n")
        r = dict(self.row(), path=str(self.dir / "run-{today_ymd}.log"))
        ok, d = fh.probe_log_tail(**r)
        self.assertFalse(ok)
        self.assertIn("missing", d)


class DefensiveNagOutcome(_Patched):
    """Round 8: the nag's last line is its outcome; only failed=0 is green.
    (First draft anchored on leading spaces, which log_tail strips -- a live
    grade caught that false red.)"""

    def grade(self, tail):
        r = next(x for x in fh.FLEET if x["name"].startswith("defensive-nag"))
        p = self.write("dn.log", "defensive-trigger nag 2026-09-27T15:20:05+00:00 mode=INVESTED\n" + tail)
        return fh.probe_log_tail(**dict(r, path=p))

    def test_real_line_green(self):
        self.assertTrue(self.grade("  nag outcome: sent=0 failed=0 pending=none\n")[0])

    def test_failed_send_or_traceback_or_header_only_red(self):
        for tail in ("  send FAILED: GO DEFENSIVE (send returned False)\n"
                     "  nag outcome: sent=0 failed=1 pending=PENDING_DEFENSIVE\n",
                     "Traceback (most recent call last):\n",
                     ""):
            self.assertFalse(self.grade(tail)[0], tail)


class ServedFreshness(unittest.TestCase):
    """2026-10-02: grade what the screen serves (aoife-typing at/generatedAt bug)."""

    def grade(self, items, **kw):
        srv = _Server({"/f": (200, json.dumps({"app": "x", "v": 1, "items": items}), {})})
        try:
            return fh.probe_freshness(srv.base + "/f", **kw)
        finally:
            srv.close()

    def test_served_matches_newest_input_green(self):
        ok, d = self.grade([{"name": "coach mission", "inputAgeH": 10, "servedAgeH": 10, "graceH": 3}])
        self.assertTrue(ok, d)

    def test_the_2_oct_bug_is_red(self):
        # she played 26 h ago; the screen still served the 11 Sep mission (~500 h)
        ok, d = self.grade([{"name": "coach mission", "inputAgeH": 26, "servedAgeH": 505, "graceH": 3}])
        self.assertFalse(ok)
        self.assertIn("coach mission", d)

    def test_inside_grace_green_past_grace_red(self):
        self.assertTrue(self.grade([{"name": "c", "inputAgeH": 1, "servedAgeH": 30, "graceH": 3}])[0])
        self.assertFalse(self.grade([{"name": "c", "inputAgeH": 4, "servedAgeH": 30, "graceH": 3}])[0])

    def test_input_but_nothing_served_red_no_input_green(self):
        self.assertFalse(self.grade([{"name": "c", "inputAgeH": 5, "servedAgeH": None, "graceH": 3}])[0])
        self.assertTrue(self.grade([{"name": "c", "inputAgeH": None, "servedAgeH": None, "graceH": 3}])[0])

    def test_max_age_and_future_stamp_red(self):
        self.assertFalse(self.grade([{"name": "feed", "inputAgeH": None, "servedAgeH": 30,
                                      "graceH": 1, "maxAgeH": 26}])[0])
        self.assertFalse(self.grade([{"name": "c", "inputAgeH": -400000, "servedAgeH": 1, "graceH": 3}])[0])
        self.assertFalse(self.grade([{"name": "stamp", "inputAgeH": None, "servedAgeH": None,
                                      "graceH": 0, "maxAgeH": 3}])[0])

    def test_missing_item_or_bad_shape_red(self):
        self.assertFalse(self.grade([{"name": "other", "inputAgeH": 1, "servedAgeH": 1, "graceH": 3}],
                                    expect_items=["coach mission"])[0])
        self.assertFalse(self.grade([])[0])

    def test_lint_feeds_screen_needs_a_freshness_row(self):
        w = {"name": "w", "repo": "r", "probe": "log_tail", "path": "x", "last_line": "y", "max_age_h": 1,
             "feeds_screen": True}
        f = {"name": "f", "repo": "r", "probe": "freshness", "url": "u"}
        self.assertEqual(len(fh.lint_roster([w])), 1)
        self.assertEqual(fh.lint_roster([w, f]), [])
        side = {"name": "s", "repo": "r", "probe": "web_fresh", "url": "u", "screen_side": True}
        self.assertEqual(fh.lint_roster([w, side]), [])


class PausedIf(unittest.TestCase):
    """2026-10-03: a screen row pauses while its job says it is idle on purpose."""

    def row(self, last):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "run.log")
        with open(path, "w") as f:
            f.write("== start\n" + last + "\n\n")
        return {"name": "x", "paused_if": {"path": path, "last_line": r"^NIGHTLY IDLE: "}}

    def test_idle_last_line_pauses(self):
        self.assertTrue(fh._paused(self.row("NIGHTLY IDLE: no year to search: 2027 trip departed"))
                        .startswith("PAUSED — "))

    def test_ok_line_or_missing_log_does_not_pause(self):
        self.assertIsNone(fh._paused(self.row("NIGHTLY OK plans=3 problems=0 pushed=abc1234 notify=ok")))
        self.assertIsNone(fh._paused({"name": "x", "paused_if": {"path": "/nope/x.log", "last_line": "."}}))
        self.assertIsNone(fh._paused({"name": "x"}))

    def test_idle_only_in_an_earlier_line_does_not_pause(self):
        self.assertIsNone(fh._paused(self.row("NIGHTLY IDLE: x\nNIGHTLY FAILED: engine")))


class PendingStamp(unittest.TestCase):
    """2026-10-03: aoife-gcal-drift stamp."""

    def grade(self, stamp):
        d = tempfile.mkdtemp(); p = os.path.join(d, "s.json")
        with open(p, "w") as f:
            json.dump(stamp, f)
        return fh.probe_pending_stamp(p, 1.25, 25)

    def iso(self, h):
        return (datetime.datetime.now().astimezone() - datetime.timedelta(hours=h)).isoformat()

    def test_in_sync_green_dead_checker_red(self):
        self.assertTrue(self.grade({"checkedAt": self.iso(0.3), "pending": 0, "pendingSinceAt": None})[0])
        self.assertFalse(self.grade({"checkedAt": self.iso(3), "pending": 0, "pendingSinceAt": None})[0])

    def test_pending_inside_grace_green_past_grace_red(self):
        self.assertTrue(self.grade({"checkedAt": self.iso(0.2), "pending": 2, "pendingSinceAt": self.iso(6)})[0])
        self.assertFalse(self.grade({"checkedAt": self.iso(0.2), "pending": 2, "pendingSinceAt": self.iso(26)})[0])

    def test_missing_stamp_red(self):
        self.assertFalse(fh.probe_pending_stamp("/nope/s.json", 1, 1)[0])


if __name__ == "__main__":
    unittest.main()


class WebFreshBypassHeader(unittest.TestCase):
    """bypass_env (2026-09-27): login-protected Vercel sites are read with the
    automation-bypass header; a missing secret is a clear red, never a leak."""

    def _run(self, env, **kw):
        import datetime as dt
        seen = {}
        class R:
            def __init__(self, req): seen["req"] = req
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"updated": dt.datetime.now().isoformat()}).encode()
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(fh.urllib.request, "urlopen", lambda req, timeout=0: R(req)):
            out = fh.probe_web_fresh(url="https://x.vercel.app/data.json",
                                     json_key="updated", max_age_h=26, **kw)
        return out, seen.get("req")

    def test_header_sent_and_secret_not_in_detail(self):
        (ok, detail), req = self._run({"BYP_TEST": "s" * 32}, bypass_env="BYP_TEST")
        self.assertTrue(ok, detail)
        self.assertEqual(req.get_header("X-vercel-protection-bypass"), "s" * 32)
        self.assertNotIn("s" * 32, detail)

    def test_missing_secret_is_red(self):
        os.environ.pop("BYP_MISSING", None)
        (ok, detail), req = self._run({}, bypass_env="BYP_MISSING")
        self.assertFalse(ok)
        self.assertIn("BYP_MISSING not set", detail)
        self.assertIsNone(req)

    def test_no_bypass_sends_no_header(self):
        (ok, _), req = self._run({})
        self.assertTrue(ok)
        self.assertIsNone(req.get_header("X-vercel-protection-bypass"))
