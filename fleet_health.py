#!/usr/bin/env python3
"""Daily fleet health check — verifies every scheduled automation ACTUALLY
produced data (not just green checkmarks — the 2026-07 CarMax incident ran
green for 17 days while writing nothing).

Runs ON THE MAC (launchd com.jalal.fleet-health, daily 5:00 AM with a 6:30 AM
retry slot — everything settled before wake-up) because only the Mac can see
all three worlds: local launchd stamps, GitHub Actions (gh CLI), and the live
sites. Outputs:
  1. The digest — handed SILENTLY to health-hub's Silent digest (the morning
     card's "🛠 Fleet health" button replays it; direct silent Telegram if the
     hand-off fails). ONE line when all is healthy; otherwise a full
     diagnostic block per failure, meant to be pasted verbatim into Claude.
  2. loud_alert() — from 06:25 only, ONE buzzing Telegram naming the rows
     still red after the 06:30 re-check, once per row per day. Rows the
     checker could not look at ("probe error") are listed apart.
  3. health.json committed+pushed — the cloud watchdog (notion_health.py via
     health.yml) stamps Notion and alerts if the Mac goes quiet or the
     morning card stops going out.

Reliability rules (the full reference is AGENTS.md §1):
  - Probes RAISE on infrastructure errors (network blips, gh/aws/launchctl
    failures, 5xx) and those get retried 3x with a pause. A probe that
    RETURNS False is real signal and is never retried.
  - lint_roster() refuses to run (exit 3 + 🚨) on a liveness-only row without
    weak_ok, a marker that matches anything, or a date token the probe
    cannot expand.
  - cli(): any crash or refusal sends a 🚨 and exits 3; run_health.sh alerts
    on any other nonzero exit (e.g. a SyntaxError before cli() exists).
  - --retry-slot (the 06:30 run) re-grades in digest mode (the default), and
    skips only after a direct send with no unbuzzed reds; a lock whose pid is
    alive makes it back off. --force (run_health.sh) re-grades by day.
  - NOT a dry run: `python3 fleet_health.py` sends and pushes. Grade without
    side effects via run_checks() (AGENTS.md §1.5).
  - Failures carry failing_since across days, and the first healthy digest
    after a failure says what recovered.

Stdlib only (+ the gh CLI and git, both already on the Mac).
"""

import datetime
import json
import os
import re
import html as _html
import tempfile
import glob
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_FILE = os.path.join(REPO_DIR, "health.json")
LOCK_FILE = os.path.join(REPO_DIR, ".fleet_health.lock")
GH_USER = "jalalchowdhury1"
PROBE_ATTEMPTS = 3          # total tries for probes that raise (infra errors)
PROBE_RETRY_PAUSE_S = 20
LOCK_STALE_S = 2 * 3600     # a lock older than this is a crashed run, ignore
TELEGRAM_LIMIT = 4000       # hard cap is 4096; leave headroom for encoding

# Where launchd plists live on this Mac — tests monkeypatch this list to point
# at a temp dir instead of touching the real LaunchAgents folder.
LAUNCHD_PLIST_DIRS = [Path.home() / "Library/LaunchAgents", Path("/Library/LaunchDaemons")]

# ── probe implementations ───────────────────────────────────────────────────
# Contract: return (ok, detail). Raise on infrastructure trouble (gets
# retried); return False only for genuine data-level failure.

def _read(path) -> str:
    """Whole text file, handle closed (bare open().read() leaked one per probe)."""
    with open(path, errors="replace") as f:
        return f.read()


DATE_TOKENS = ("{today}", "{date}", "{weekday}", "{utctoday}")
# Probes whose log_grep expands DATE_TOKENS. Any other probe would search for the
# literal text "{date}" and could never match -- lint_roster refuses that.
TOKEN_PROBES = {"gh_run", "log_marker", "cloudwatch_marker"}


def _expand_dates(pattern, today=None):
    """{today} = today only; {date} = today|yesterday; {weekday} = today|the
    previous weekday. One helper for every probe (round 8: each probe used to
    expand its own subset, and {weekday} outside gh_run stayed literal).
    {utctoday} = the UTC date, for jobs that label their output by UTC date: the
    23:00 ET quiet run (3 Oct 2026) is already tomorrow in UTC, so {today} paged a
    healthy financial-dashboard-history. A caller-supplied `today` stands in for it."""
    utc = today or datetime.datetime.now(datetime.timezone.utc).date()
    today = today or datetime.date.today()
    yday = today - datetime.timedelta(days=1)
    return (pattern.replace("{today}", today.isoformat())
            .replace("{utctoday}", utc.isoformat())
            .replace("{weekday}", f"(?:{'|'.join(_weekday_dates(today))})")
            .replace("{date}", f"(?:{today.isoformat()}|{yday.isoformat()})"))


def _age_hours(ts: float) -> float:
    return (datetime.datetime.now().timestamp() - ts) / 3600


def _weekday_dates(today: datetime.date) -> list:
    """Today plus the most recent weekday before it — the `{weekday}` log_grep token,
    for weekday-only jobs whose newest output on a Sunday or Monday morning is Friday's."""
    prev = today - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    return [today.isoformat(), prev.isoformat()]


_ZONED_STAMP = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d(?:\.\d+)?)?)\s*(Z|[+-]\d\d:?\d\d)$")


def _parse_stamp(raw):
    """"YYYY-MM-DD HH:MM" (or the same with a T) — and bare "YYYY-MM-DD".

    A date-only stamp parses as MIDNIGHT, so the age it reports is up to a day
    older than the truth. That is the safe direction (stricter, never laxer),
    but it means max_age_h for a date-only feed must budget an extra ~24 h.

    A stamp that names its zone ("…Z", "…+00:00", "…-0400") is converted to
    Mac-local time. Until 2026-09-26 the zone was cut off, so a UTC stamp read
    as local and every age came out 4-5 h too young (reddit-browser's live row
    said "-4h old"). Stamps without a zone are Mac-local, as before.
    """
    s = str(raw).strip().replace("T", " ")
    m = _ZONED_STAMP.match(s)
    if m:
        zone = m.group(2)
        zone = "+00:00" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
        aware = datetime.datetime.fromisoformat(f"{m.group(1).replace(' ', 'T')}{zone}")
        return aware.astimezone().replace(tzinfo=None)
    for fmt, n in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp {raw!r}")


def probe_web_fresh(url, json_key, max_age_h, rows_key=None, min_rows=None,
                    fail_key=None, fail_note_key=None, bypass_env=None, **_):
    """Fetch JSON and check a timestamp field is recent (data-level freshness).

    rows_key: grade the OLDEST per-row stamp under data[rows_key] instead of a
    top-level field. Use it whenever the job rewrites its output file on every
    run regardless of outcome — a top-level `updated` then measures only that
    the job WOKE UP, which is exactly how dhaka-hotels published a green row
    through five nights of zero scraped rates (2026-08-11 → 08-16: `updated`
    said today, every row's `checked` said Aug 10). The rule this file already
    states for launchd_exit — never grade a proxy for the thing you care about
    — applies just as much to a timestamp the job stamps unconditionally.

    fail_key (round 8): a top-level stamp of the job's newest FAILURE. Red when it
    is newer than json_key's stamp — the last thing the job tried did not work
    (health-hub: send_error_at vs send_ok_at; a blocked bot kept last_tick fresh).
    fail_note_key names a short reason field to quote.

    bypass_env (2026-09-27): env var holding a Vercel "Protection Bypass for
    Automation" secret, for sites behind Vercel Authentication (dhaka-flights was
    made login-only so the family's trip dates stop being public). Sent as the
    x-vercel-protection-bypass header; the value never appears in a detail line."""
    req = urllib.request.Request(url)
    if bypass_env:
        secret = os.environ.get(bypass_env)
        if not secret:
            return False, f"{bypass_env} not set (see run_health.sh)"
        req.add_header("x-vercel-protection-bypass", secret)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    # min_rows {list_key: floor} (red team 2026-09-27): a fresh top-level stamp on
    # a half-empty scrape is still a false green -- dhaka-flights published
    # flights=0 on 2026-08-23 with a brand-new `updated`. Floors sit far below
    # every healthy night, so only a broken scrape trips them.
    short = [f"{k}={len(data.get(k) or [])} (min {n})"
             for k, n in (min_rows or {}).items() if len(data.get(k) or []) < n]
    if short:
        return False, "fresh stamp but a scrape came back short: " + ", ".join(short)
    if rows_key:
        rows = data.get(rows_key) or []
        stamps = [str(row.get(json_key, "")) for row in rows
                  if isinstance(row, dict) and row.get(json_key)]
        if not stamps:
            return False, f"no {json_key!r} stamp on any {rows_key} row"
        raw = min(stamps, key=lambda s: _parse_stamp(s).timestamp())
        label = f"oldest of {len(stamps)} {rows_key}"
    else:
        if not data.get(json_key):
            return False, f"no {json_key!r} in the response"
        raw = str(data.get(json_key, ""))
        label = "data"
    ts = _parse_stamp(raw).timestamp()
    age = _age_hours(ts)
    if age < -1:        # red team 2026-09-27: a future stamp passed every age check
        return False, f"{label} stamp is {-age:.0f}h in the FUTURE ({json_key}={raw!r}) — clock/zone bug"
    ok = age <= max_age_h
    if ok and fail_key and data.get(fail_key):
        f_raw = str(data[fail_key])
        if _parse_stamp(f_raw).timestamp() > ts:
            why = str(data.get(fail_note_key) or "")[:90] if fail_note_key else ""
            return False, (f"newest attempt FAILED: {fail_key}={f_raw} is after "
                           f"{json_key}={raw}" + (f" ({why})" if why else ""))
    return ok, f"{label} {age:.0f}h old" + ("" if ok else
                                            f" (limit {max_age_h}h, raw {json_key}={raw!r})")


def probe_web_200(url, expect_text=None, bypass_env=None, **_):
    """HTTP 200 from the host we asked, optionally carrying expect_text.

    Red team round 4 (2026-09-12): urlopen FOLLOWS redirects, including to other
    hosts, and reports the final status. Verified: google.com -> www.google.com
    comes back 200. So a site that got Vercel Deployment Protection switched on
    -- a 302 into the vercel.com login page -- read HTTP 200 and stayed green
    while nobody could open it. The final URL must stay on the requested host.
    expect_text proves the right app answered, not a placeholder or a login wall.
    Still liveness: it cannot prove the page RUNS (a runtime JS error serves 200).
    """
    req = urllib.request.Request(url, method="GET")
    if bypass_env:                      # login-only Vercel site: see probe_web_fresh
        secret = os.environ.get(bypass_env)
        if not secret:
            return False, f"{bypass_env} not set (see run_health.sh)"
        req.add_header("x-vercel-protection-bypass", secret)
    with urllib.request.urlopen(req, timeout=30) as r:
        want = urllib.parse.urlsplit(url).netloc
        got = urllib.parse.urlsplit(r.url).netloc
        if got != want:
            return False, f"HTTP {r.status} but redirected off-host to {got} (login wall?)"
        if r.status != 200:
            return False, f"HTTP {r.status}"
        if expect_text:
            body = r.read(500_000).decode("utf-8", "replace")
            if expect_text not in body:
                return False, f"HTTP 200 but {expect_text!r} not in page — wrong app or placeholder"
        return True, f"HTTP {r.status}" + (" + app shell" if expect_text else "")


def probe_local_stamp(path, max_age_h, **_):
    """Stamp file contains an ISO date written on success."""
    date = _read(os.path.expanduser(path)).strip()
    ts = datetime.datetime.strptime(date, "%Y-%m-%d").timestamp()
    age = _age_hours(ts)
    if age < -24:       # date-only = midnight, so "today" is 0..24 h old; beyond = future
        return False, f"stamp {date} is in the FUTURE — clock bug"
    ok = age <= max_age_h
    return ok, f"last success {date}" + ("" if ok else f" ({age/24:.1f}d ago)")


def probe_file_mtime(path, max_age_h, **_):
    """Freshness by file mtime, for jobs that write a log rather than a date stamp.
    Complements `launchd_exit`, which cannot see this: a job that exits 0 without
    doing anything (e.g. t7-drive-sync's deliberate `[ -d /Volumes/T7Files ] || exit 0`
    when the drive is unplugged) looks identical to a successful run."""
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return False, f"{path} missing (volume unmounted?)"
    age = _age_hours(os.path.getmtime(p))
    ok = age <= max_age_h
    return ok, (f"written {age:.0f}h ago" if ok
                else f"stale: last written {age/24:.1f}d ago (limit {max_age_h}h)")


def _plist_state(label) -> str:
    """"live" / "retired" / "missing" for a launchd label's plist on this Mac.

    Retiring a job here is done by `launchctl unload` + renaming its plist to
    `<label>.plist.retired` rather than deleting it (see
    com.jalal.supervisor.plist.retired) — so a label absent from `launchctl
    list` is not automatically a failure. This is what probe_launchd_exit and
    probe_launchd_running check before reporting "not loaded" as broken.
    """
    if any((d / f"{label}.plist").exists() for d in LAUNCHD_PLIST_DIRS):
        return "live"
    if any((d / f"{label}.plist.retired").exists() for d in LAUNCHD_PLIST_DIRS):
        return "retired"
    return "missing"


def _not_loaded_result(label):
    """(ok, detail) for a label missing from `launchctl list` — retired on
    purpose, still installed but not loaded, or actually gone. See
    _plist_state's docstring for the retirement convention."""
    state = _plist_state(label)
    if state == "retired":
        return True, (f"RETIRED — plist is {label}.plist.retired; "
                       "delete this row from fleet_health.py")
    if state == "live":
        found = next((d for d in LAUNCHD_PLIST_DIRS if (d / f"{label}.plist").exists()),
                     LAUNCHD_PLIST_DIRS[0])
        return False, (f"plist present but NOT LOADED — launchctl load "
                        f"{found / (label + '.plist')}")
    return False, (f"no {label}.plist in ~/Library/LaunchAgents — "
                    "job deleted? restore the plist or delete this row")


def probe_launchd_exit(label, **_):
    """launchctl list: second column = last exit status (0 = clean)."""
    p = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                       timeout=15)
    if p.returncode != 0:
        raise RuntimeError(f"launchctl list failed: {p.stderr.strip()[:120]}")
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            code = parts[1]
            return code == "0", f"last exit {code}"
    return _not_loaded_result(label)


def probe_launchd_running(label, **_):
    """An always-on daemon is actually ALIVE right now, not merely registered.

    `launchd_exit` is the wrong tool for a KeepAlive daemon: its second column
    is the LAST exit status, which stays "0" long after the process is gone, so
    a dead daemon reads exactly like a healthy one. The first column is the
    live PID, or "-" when nothing is running. That is the only column that
    distinguishes the two, so this probe reads that one.

    Added 2026-09-12. keepawake is the reason: it is a bare `caffeinate -s`
    holding the Mac out of sleep, and if it dies every overnight job in this
    roster silently stops happening. Nothing here noticed that, which made it
    the highest-leverage missing probe in the fleet.
    """
    p = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                       timeout=15)
    if p.returncode != 0:
        raise RuntimeError(f"launchctl list failed: {p.stderr.strip()[:120]}")
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            pid, code = parts[0], parts[1]
            if pid == "-":
                return False, f"NOT RUNNING (loaded, last exit {code})"
            return True, f"alive, pid {pid}"
    return _not_loaded_result(label)


# `--log-failed` keeps the failed job's *housekeeping* steps too, and those run
# last — so a naive tail shows git-credential cleanup instead of the error. This
# bit me on 2026-08-05: the leasehackr digest tailed 8 lines of `git config
# --unset` and hid the RuntimeError that actually explained the failure.
_CLEANUP_MARKER = re.compile(r"Post job cleanup|Cleaning up orphan processes")
_ERROR_SIGNATURE = re.compile(
    r"Traceback|RuntimeError|Exception|AssertionError|\bFAILED\b|fatal:|"
    r"Killed|OOM|No such file|Permission denied|Error:", re.I)
# Always the last line of a failed step and never informative on its own.
_GENERIC_ERROR = re.compile(r"##\[error\]Process completed with exit code")
_LOG_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# GitHub renders a step's *echoed source* in cyan-bold. Those lines are the
# script, not its output, so `echo "Error: ..."` in a step that passed must
# never be mistaken for the cause of the failure.
_ECHOED_CMD = re.compile(r"\x1b\[36;1m")


def _failed_log_tail(repo, run_id, max_chars=1200, keep=14):
    """The failed step's real error — bounded, best-effort.

    Drops the post-job cleanup block, strips the ISO timestamp off each line
    (pure noise that ate most of the character budget), and keeps the last
    `keep` lines. If the run's first error signature scrolled off the top of
    that window, it is prepended so the digest never loses the actual cause.
    """
    try:
        p = subprocess.run(
            ["gh", "run", "view", str(run_id), "-R", f"{GH_USER}/{repo}",
             "--log-failed"],
            capture_output=True, text=True, timeout=120)
        # (is_echoed_source, cleaned_text) per line — the flag has to be read
        # off the raw line, before the ANSI colours are stripped for display.
        entries = [(_ECHOED_CMD.search(raw) is not None,
                    _ANSI.sub("", _LOG_TIMESTAMP.sub("", raw)).strip())
                   for raw in (l.split("\t")[-1].strip()
                               for l in p.stdout.splitlines() if l.strip())]
        cut = next((i for i, (_, l) in enumerate(entries)
                    if _CLEANUP_MARKER.search(l)), len(entries))
        entries = entries[:cut] or entries    # all-cleanup log → keep it anyway
        lines = [l for _, l in entries]
        window = lines[-keep:]
        cause = next((l for echoed, l in entries
                      if not echoed and _ERROR_SIGNATURE.search(l)
                      and not _GENERIC_ERROR.search(l)), None)
        if cause and cause not in window:
            window = [cause, "..."] + window
        tail = "\n".join(window)
        return tail[-max_chars:]
    except Exception:                        # noqa: BLE001 — log tail is a bonus
        return ""


def probe_gh_run(repo, workflow, max_age_h, log_grep=None, expect_event=None,
                 no_rescue=False, rescue_max_failures=None, **_):
    """Latest workflow run: recent + successful; optionally grep the log for
    data-level markers proving real work happened, not just a green exit.

    `log_grep` is one regex or a list of them — ALL must match. Prefer markers
    that stay true on a legitimately quiet day: assert the pipeline ran ("across
    N regions"), not that the count was nonzero, or a slow source hands you a
    false alarm at 5 AM.

    A `{date}` in a pattern expands to today|yesterday, same as
    `probe_log_marker`. Use it whenever the marker carries the date of the data
    it produced: an unpinned `date=\\d{4}-\\d{2}-\\d{2}` proves only that the
    job printed A date, so a pipeline whose cron quietly stopped keeps passing
    on yesterday's marker until the run itself ages out of max_age_h.
    `{weekday}` widens that to today|the previous weekday, for weekday-only jobs.

    `expect_event` (One Clock, 2026-08-29) names the trigger that SHOULD be
    firing this workflow — "workflow_dispatch" for anything whose primary is now
    an AWS EventBridge schedule. Without it, an EventBridge that silently stops
    is INVISIBLE here: GitHub's demoted backstop cron still runs the job, the
    run is recent and green, and this probe reports healthy while the thing we
    migrated to is dead. That is exactly how the financial-telegram-bot Lambda
    stayed broken for two months behind a GHA backstop. A mismatch is reported
    as a WARN-style failure naming both events — the job itself is fine, the
    primary trigger is not.
    """
    p = subprocess.run(
        ["gh", "run", "list", "-R", f"{GH_USER}/{repo}", "--workflow", workflow,
         "--limit", "5", "--json", "status,conclusion,createdAt,databaseId,event"],
        capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"gh run list failed: {p.stderr.strip()[:150]}")
    runs = json.loads(p.stdout or "[]")
    if not runs:
        return False, "no runs found"
    run = runs[0]
    _rescue_note = None
    ts = datetime.datetime.strptime(run["createdAt"][:16], "%Y-%m-%dT%H:%M")
    age = _age_hours(ts.replace(tzinfo=datetime.timezone.utc).timestamp())
    # A run that started under an hour ago is just RUNNING, not hung. 2026-09-14: a
    # manual 09:30 fleet check landed seconds after One Clock's 09:30 dispatches and
    # paged two healthy repos as "likely HUNG". Grade the newest finished run instead.
    done = [r for r in runs[1:] if r.get("status") == "completed"]
    if run.get("status") != "completed" and age < 1 and done:
        run = done[0]
        ts = datetime.datetime.strptime(run["createdAt"][:16], "%Y-%m-%dT%H:%M")
        age = _age_hours(ts.replace(tzinfo=datetime.timezone.utc).timestamp())
    run_url = f"https://github.com/{GH_USER}/{repo}/actions/runs/{run['databaseId']}"
    # An in-progress run has conclusion "" — without this branch it rendered as
    # "last run  (1h ago)", which reads like a failure and hides the real story.
    # A hang needs the opposite remedy from a failure (cancel + find the wedged
    # step, vs read the log tail), and its log is not fetchable until it ends.
    if run.get("status") != "completed":
        return False, (f"still running {age:.0f}h (normal runs finish in minutes)"
                       f" — likely HUNG; cancel it to release the runner and"
                       f" expose the wedged step's log\nrun: {run_url}")
    if run["conclusion"] != "success":
        # One Clock made a job's newest run and its REAL run two different things.
        # With an AWS primary plus a demoted GitHub backstop, a redundant backstop
        # run can fail on its own (transient API blip) minutes-to-hours AFTER the
        # primary already did the work — newest-run grading then reports red on a
        # pipeline that is fine. Observed 2026-08-29: AWS stamped Notion at 15:27,
        # the 4h-late backstop cron hit a Notion TimeoutError at 17:03, row red.
        #
        # So: a failure is only downgraded when the work provably happened anyway —
        # a SUCCESS inside max_age_h, and (when expect_event is set) via the
        # PRIMARY trigger specifically. A genuinely broken job has no such success
        # and stays red, which is the case this must not soften.
        #
        # no_rescue (2026-09-12, red team). The rescue assumes every run of the
        # workflow does the SAME work. True for a redundant trigger, FALSE for a
        # workflow whose runs are distinct jobs -- financial-dashboard-history
        # takes an AM and a PM snapshot, and a failed PM was greening off the AM
        # run's log. Rows whose runs are not interchangeable must say so.
        rescue = None
        # rescue_max_failures (red team 2026-09-27): for a job whose runs are
        # separate cycles (reddit-backup every 3 h, the 30-min trading signal),
        # "an earlier run succeeded" also describes a job failing every OTHER
        # run. Past this many failed runs in the window it is flapping, not a
        # blip, and no rescue applies.
        if rescue_max_failures is not None:
            in_window = [r for r in runs if r.get("status") == "completed"
                         and _age_hours(datetime.datetime.strptime(r["createdAt"][:16], "%Y-%m-%dT%H:%M")
                                        .replace(tzinfo=datetime.timezone.utc).timestamp()) <= max_age_h]
            fails = sum(1 for r in in_window if r.get("conclusion") != "success")
            if fails > rescue_max_failures:
                tail = _failed_log_tail(repo, run["databaseId"])
                return False, (f"{fails} of the last {len(in_window)} runs failed — flapping, "
                               f"not a one-off blip\nrun: {run_url}"
                               + (f"\n{tail}" if tail else ""))
        if no_rescue:
            tail = _failed_log_tail(repo, run["databaseId"])
            return False, (f"last run {run['conclusion']} ({age:.0f}h ago); no_rescue "
                           f"-- an earlier run does different work here\nrun: {run_url}"
                           + (f"\n{tail}" if tail else ""))
        for r in runs[1:]:
            if r.get("conclusion") != "success":
                continue
            r_age = _age_hours(datetime.datetime.strptime(r["createdAt"][:16], "%Y-%m-%dT%H:%M")
                               .replace(tzinfo=datetime.timezone.utc).timestamp())
            if r_age > max_age_h:
                continue
            if expect_event and r.get("event") != expect_event:
                continue
            rescue = (r, r_age)
            break
        if rescue:
            r, r_age = rescue
            note = (f"newest run ({run.get('event', '?')}) {run['conclusion']} "
                    f"{age:.0f}h ago, but the {r.get('event', '?')} run {r_age:.0f}h "
                    f"ago succeeded — redundant-trigger blip, work was done")
            if log_grep:
                run, run_url, age = r, (f"https://github.com/{GH_USER}/{repo}"
                                        f"/actions/runs/{r['databaseId']}"), r_age
                _rescue_note = note
            else:
                return True, note
        else:
            detail = f"last run {run['conclusion']} ({age:.0f}h ago)\nrun: {run_url}"
            tail = _failed_log_tail(repo, run["databaseId"])
            if tail:
                detail += f"\nlog tail:\n{tail}"
            return False, detail
    if age > max_age_h:
        return False, (f"no run in {age/24:.1f}d (limit {max_age_h}h)"
                       f"\nlast run: {run_url}")
    # Trigger-provenance check. Deliberately placed AFTER age/conclusion: a job
    # that is failing or stale is the bigger story, and this must not mask it.
    #
    # Judge the WINDOW, not just the newest run. GitHub's demoted backstop cron
    # frequently lands hours late, so on a perfectly healthy day the newest run
    # is often the backstop even though EventBridge fired on time (measured
    # 2026-08-29: AWS ran mental-models at 04:10, GitHub's cron straggled in at
    # 11:36). Testing the newest run alone false-alarms constantly. What we
    # actually want to know is: did the primary fire AT ALL inside the window?
    if expect_event:
        seen = [r for r in runs
                if r.get("event") == expect_event
                and r.get("conclusion") == "success"
                and _age_hours(datetime.datetime.strptime(r["createdAt"][:16], "%Y-%m-%dT%H:%M")
                               .replace(tzinfo=datetime.timezone.utc).timestamp()) <= max_age_h]
        if not seen:
            events = ", ".join(sorted({r.get("event", "?") for r in runs})) or "none"
            return False, (
                f"no '{expect_event}' run in {max_age_h}h (saw: {events}) — the job "
                f"itself is fine, but its PRIMARY trigger (AWS EventBridge, One "
                f"Clock) has not fired; GitHub's demoted backstop cron is carrying "
                f"it. Check `aws scheduler get-schedule --name one-clock-*` and the "
                f"gh-dispatcher Lambda logs.\nlatest run: {run_url}")
    if log_grep:
        patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
        # {today} pins to today alone, no buffer — same token probe_cloudwatch_marker
        # uses. Added here 2026-09-12 (red team round 3) for rows where the
        # today|yesterday buffer is too loose to mean anything. Safe because this
        # monitor runs at 05:00 ET, when the local date and the UTC date agree;
        # it would NOT be safe if fleet-health ever ran between 20:00 (19:00 once DST
        # ends) and midnight.
        # {weekday} = today|the previous weekday, for weekday-only jobs (hedgelab):
        # {date}'s one-day buffer paged Friday's good plan every Sunday and Monday.
        # No `today` argument (6 Oct 2026): passing the local date overrode
        # {utctoday}, so the 23:00 ET quiet run still graded a UTC-labelled slot
        # against the local date and paged a healthy financial-dashboard-history.
        patterns = [_expand_dates(p) for p in patterns]
        lp = subprocess.run(
            ["gh", "run", "view", str(run["databaseId"]), "-R",
             f"{GH_USER}/{repo}", "--log"],
            capture_output=True, text=True, timeout=120)
        log = lp.stdout
        # A log we could not FETCH is not a log with missing markers. Ignoring the
        # return code turns any GitHub API wobble (5xx, rate limit, logs still
        # finalising, 410 on expired logs) into an empty string, so every pattern
        # "misses" and a perfectly healthy pipeline reports failure — which the
        # digest treats as real signal and never retries. Raise so this takes the
        # infra-retry path (PROBE_ATTEMPTS) instead.
        if lp.returncode != 0 or not log.strip():
            raise RuntimeError(
                f"could not fetch run log (rc={lp.returncode}): "
                f"{(lp.stderr or '').strip()[:120] or 'empty log'}")
        missing = [pat for pat in patterns if not re.search(pat, log)]
        if not missing:
            return True, (f"success {age:.0f}h ago, data confirmed"
                          + (f" ({_rescue_note})" if _rescue_note else ""))
        # The latest run's log lacks the marker, but some workflows (e.g.
        # mental-models) fire more than once a day — a manual dispatch does
        # the real work, then the schedule trigger fires later, sees today's
        # output already committed, and no-ops green with no marker. That's
        # not a failure, so before crying wolf, check whether an EARLIER run
        # from today's same freshness window already proved the work happened.
        # Bounded to max_age_h so this can't reach back into a stale prior day
        # and paper over an actually-broken "latest" run.
        #
        # no_rescue gates THIS path too (red team, 2026-09-12). Same assumption,
        # quieter failure: the newest run is GREEN, so nothing looks wrong -- only
        # its own log lacks the marker, and a sibling run supplies it. hedgelab
        # DEPENDS on this fallback (duplicate guard, see its row) and so does not
        # set no_rescue; dashboard-history's AM/PM runs are distinct and does.
        for cand in ([] if no_rescue else runs[1:]):
            if cand.get("status") != "completed" or cand.get("conclusion") != "success":
                continue
            cand_ts = datetime.datetime.strptime(cand["createdAt"][:16], "%Y-%m-%dT%H:%M")
            if _age_hours(cand_ts.replace(tzinfo=datetime.timezone.utc).timestamp()) > max_age_h:
                continue
            clp = subprocess.run(
                ["gh", "run", "view", str(cand["databaseId"]), "-R",
                 f"{GH_USER}/{repo}", "--log"],
                capture_output=True, text=True, timeout=120)
            if clp.returncode != 0 or not clp.stdout.strip():
                continue  # an older run's log being unfetchable isn't infra trouble worth raising for
            cand_missing = [pat for pat in patterns if not re.search(pat, clp.stdout)]
            if not cand_missing:
                cand_url = f"https://github.com/{GH_USER}/{repo}/actions/runs/{cand['databaseId']}"
                return True, (f"success {age:.0f}h ago, data confirmed in earlier "
                              f"same-window run: {cand_url}")
        return False, (f"run green but data marker missing "
                       f"({', '.join(repr(m) for m in missing)})"
                       f"\nrun: {run_url}")
    return True, (f"success {age:.0f}h ago"
                  + (f" ({_rescue_note})" if _rescue_note else ""))


def _valid_json(path):
    try:
        json.loads(_read(path))
        return True
    except Exception:                        # noqa: BLE001 — missing/corrupt both mean "not there"
        return False


def probe_planner_backup(dest_dir, names, log_path, live_since=None, **_):
    """Nightly Drive snapshot of the aoifes-schedule KV blobs (schedule +
    plan) — scripts/planner-backup.sh in the "Aoife's Schedule" repo,
    launchd com.jalal.aoife-planner-backup at 3:40 AM, well before this
    5:00 AM check.

    Two independent checks, same "assert the artifact, not the exit code"
    rule as everywhere else in this file: (a) each of `names`' dated JSON
    files under `dest_dir` exists for TODAY or YESTERDAY (a one-day buffer —
    same pattern as dhaka-hotels — for a run that lands late) and parses,
    and (b) the script's own "PLANNER-BACKUP OK <date>" marker for one of
    those two dates appears in `log_path`. The marker only prints when BOTH
    endpoints round-tripped, so a stale file left over from a prior success
    can't pass on its own.

    live_since: /api/plan-get is not deployed on the live site until this
    date — every night before it, the script's plan half legitimately FAILs
    and never prints OK (verified manually 2026-08-17). Alerting on that
    would page for a known, dated, one-sided gap instead of a real failure,
    so this probe reports healthy without checking anything before
    `live_since`.
    """
    today = datetime.date.today()
    if live_since and today.isoformat() < live_since:
        return True, f"pre-launch grace period — alerting starts {live_since}"
    # Today only (red team 2026-09-27): the job runs 03:40, before this check,
    # and a yesterday arm let a failed night pass on the previous snapshot.
    candidates = [today]
    dest = os.path.expanduser(dest_dir)
    missing = [name for name in names
               if not any(_valid_json(os.path.join(dest, f"{d.isoformat()}-{name}.json"))
                          for d in candidates)]
    if missing:
        return False, (f"missing/unparseable snapshot(s): {', '.join(missing)} "
                       f"(checked {today} in {dest_dir})")
    log = os.path.expanduser(log_path)
    try:
        text = _read(log)
    except FileNotFoundError:
        return False, f"{log_path} missing"
    pattern = r"PLANNER-BACKUP OK (" + "|".join(d.isoformat() for d in candidates) + ")"
    if not re.search(pattern, text):
        return False, f"no {pattern!r} match in {log_path}"
    return True, "snapshot files present + OK marker in log"


def probe_log_marker(log_path, log_grep, live_since=None, **_):
    """A local (launchd) log carries the job's own dated success marker for
    TODAY or YESTERDAY.

    The Mac-side twin of `probe_gh_run`'s `log_grep`, and the same rule:
    assert the marker the pipeline PRINTS WHEN IT WORKED, never that the job
    woke up — a job that ran and failed still writes a line, still touches the
    file's mtime, and still exits 0 where the wrapper swallows the status.

    `log_grep` is one regex or a list (ALL must match). A `{date}` in a pattern
    expands to an alternation of today and yesterday — the one-day buffer
    `probe_planner_backup` uses, and load-bearing for any job whose slots do
    not all land before this 5:00 AM check.

    live_since: nothing to grade before the job's first full day; report
    healthy rather than page for a log that does not exist yet.
    """
    today = datetime.date.today()
    if live_since and today.isoformat() < live_since:
        return True, f"pre-launch grace period — alerting starts {live_since}"
    log = os.path.expanduser(log_path)
    try:
        text = _read(log)
    except FileNotFoundError:
        return False, f"{log_path} missing"
    dates = "|".join(d.isoformat() for d in
                     (today, today - datetime.timedelta(days=1)))
    patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
    # {today} = today only (red team 2026-09-27): for a job that runs BEFORE this
    # 05:00 check, {date}'s yesterday arm let a job that failed this morning
    # pass on yesterday's marker until the next day's check.
    missing = [p for p in patterns
               if not re.search(_expand_dates(p, today), text)]
    if missing:
        return False, (f"no {', '.join(repr(m) for m in missing)} match "
                       f"in {log_path} (checked {dates})")
    return True, f"marker present in {log_path}"


def probe_telegram_webhook(token_env, expect_url, require_guard=False, **_):
    """A Telegram bot's webhook still points where we think it does.

    Added 2026-08-24 after voices-bot went silently deaf: deactivating the old
    n8n `MAIN` workflow made n8n call deleteWebhook on @MainJ_bot, which wiped
    the Vercel registration. The function was still deployed and still returned
    200 on a GET, so a web_200 probe would have stayed green while every
    message the bot received went nowhere. This is the probe that catches it.

    Also surfaces Telegram's own last_error_message — the earliest warning that
    a deployed-but-erroring function is dropping updates.
    """
    token = os.environ.get(token_env)
    if not token:
        return False, f"{token_env} not set (see run_health.sh)"
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as r:
        body = json.load(r)
    if not body.get("ok"):
        return False, f"getWebhookInfo failed: {str(body)[:120]}"
    info = body.get("result", {})
    got = info.get("url") or ""
    pending = info.get("pending_update_count", 0)
    err = info.get("last_error_message")
    # Compare scheme+host+path ONLY. aoife-milestones-bot and aoife-school-bot
    # guard their webhook with a shared secret in the query string (?s=...) and
    # THIS REPO IS PUBLIC — putting the full URL in the roster would publish the
    # guard. The query is not what we are verifying anyway; that the hook still
    # points at the right function is. Never echo the query back in an error
    # message either, for the same reason.
    g = urllib.parse.urlsplit(got)
    e = urllib.parse.urlsplit(expect_url)
    if (g.scheme, g.netloc, g.path) != (e.scheme, e.netloc, e.path):
        return False, (f"webhook is {got.split('?')[0] or '(EMPTY — bot is deaf)'}, "
                       f"expected {expect_url}")
    # ...but a guard that silently VANISHES leaves the endpoint open to anyone,
    # so prove it still bites: POST one bare update with NO secret (no ?s=, no
    # X-Telegram-Bot-Api-Secret-Token header) and require a 401/403. Until
    # 2026-09-07 this asserted `?s=` was in the registered URL; milestones-bot
    # then moved its secret into Telegram's header (the query form leaked into
    # every Vercel access-log line) and the probe paged for a day on a bot that
    # was MORE locked down, not less. Test the door, not the shape of the key.
    if require_guard:
        status = _unauth_post_status(f"{g.scheme}://{g.netloc}{g.path}")
        if status >= 500:
            # A 5xx says nothing about the guard — the function is erroring or
            # cold. Raise so run_checks() takes the infra-retry path instead of
            # paging "guard is GONE" on a transient.
            raise RuntimeError(f"unauth POST got HTTP {status} — cannot judge the guard")
        if status not in (401, 403):
            return False, (f"webhook guard is GONE — an unauthenticated POST got "
                           f"HTTP {status}, expected 401/403; the endpoint accepts "
                           f"anyone's updates")
    detail = f"webhook registered, {pending} pending"
    # Telegram keeps the LAST error forever, even after later deliveries worked
    # (red team 2026-09-27), so one old blip would pin the row red. Only an error
    # from the last 24 h counts; a missing date is treated as recent.
    err_age_h = (time.time() - info["last_error_date"]) / 3600 if info.get("last_error_date") else 0
    if err and err_age_h <= 24:
        return False, f"{detail}, last_error={err!r} ({err_age_h:.0f}h ago)"
    return True, detail


def _unauth_post_status(url):
    """HTTP status for a secret-less Telegram-shaped POST. Never sends a token."""
    req = urllib.request.Request(url, method="POST", data=b'{"update_id":0}',
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def probe_nuts(url, max_data_age_d=5, max_eval_age_h=96, **_):
    """NUTS /evaluate — the SIGNAL behind trading-algorithm- and voices-bot /nuts.

    Why this probe has to exist (added 2026-08-25): trading-algorithm- reads this
    endpoint and alerts ONLY on a holding change, so silence is its normal
    output. If NUTS freezes, /evaluate keeps returning HTTP 200 with a stale
    cached body, the consumer sees no change and stays quiet, and its green
    gh_run row proves only that the Action woke up. A frozen signal was
    therefore indistinguishable from a quiet market, on the model behind a live
    ~$178k Composer symphony. `web_200` would have reproduced exactly that blind
    spot, which is why this grades the payload instead.

    Plain GET only. NEVER add ?force=true — a bare GET returns the cached body
    the website shows, at zero cost to NUTS (see reference-nuts-algo).

    Four data-level assertions, most-serious first:
      1. unit_test.pass — NUTS's own RSI self-check. Per reference-nuts-algo,
         if this fails the standing instruction is DO NOT TRADE, so it is a
         hard failure here even when everything else looks fine.
      2. download_errors empty — a partial price fetch silently changes which
         branch wins.
      3. data_quality[*].last_date — the last COMPLETED trading day, which is
         the freshness signal that actually matters: it catches a signal being
         computed on stale prices.
      4. evaluated_at — backstop for a wholly dead EventBridge cron.

    THRESHOLDS (measured 2026-08-25, against a 5 AM check and NUTS's last
    recompute of a session at ~16:35 ET). Both limits are graded on the plain
    calendar rather than a market calendar, so both must absorb the longest
    legitimate quiet stretch:

        scenario                          eval_age_h   data_age_d
        normal weekday (Mon → Tue 5am)          12.4          1.2
        long weekend   (Fri → Mon 5am)          60.4          3.2
        Monday holiday (Fri → Tue 5am)          84.4          4.2
        Good Friday    (Thu → Mon 5am)          84.4          4.2

    The first draft used 4 d / 80 h, which sits BELOW the holiday row — it would
    have false-alarmed on every Monday holiday and Good Friday, ~6 times a year,
    which is how a digest gets ignored. 5 d / 96 h clears the worst legitimate
    case with real slack. The cost is one extra day before a total outage is
    called, and that is cheap: an outage persists, so the next morning catches
    it, while assertions 1 and 2 are time-independent and catch the genuinely
    dangerous silent-corruption cases on the very first run.

    Deliberately NOT graded: final_result's VALUE. Which ticker NUTS holds is a
    trading decision, not a health fact — trading-algorithm- owns alerting on
    that. This only asserts the field is populated at all.
    """
    with urllib.request.urlopen(url, timeout=45) as r:
        if r.status != 200:
            return False, f"HTTP {r.status}"
        data = json.loads(r.read().decode())

    ut = data.get("unit_test") or {}
    if not ut.get("pass"):
        return False, (f"unit_test FAILED (expected {ut.get('expected')!r}, "
                       f"calculated {ut.get('calculated')!r}) — DO NOT TRADE")

    errs = data.get("download_errors")
    if errs:
        return False, f"download_errors: {json.dumps(errs)[:200]}"

    dq = data.get("data_quality") or {}
    dates = {t: v.get("last_date") for t, v in dq.items()
             if isinstance(v, dict) and v.get("last_date")}
    if not dates:
        return False, "no data_quality[*].last_date in payload"
    worst_t = min(dates, key=lambda t: _parse_stamp(dates[t]).timestamp())
    worst = dates[worst_t]
    data_age_d = _age_hours(_parse_stamp(worst).timestamp()) / 24
    if data_age_d > max_data_age_d:
        return False, (f"stale prices: {worst_t} last_date={worst} "
                       f"({data_age_d:.1f}d, limit {max_data_age_d}d)")

    raw_eval = data.get("evaluated_at")
    if not raw_eval:
        return False, "no evaluated_at in payload"
    eval_age_h = _age_hours(_parse_stamp(raw_eval).timestamp())
    if eval_age_h > max_eval_age_h:
        return False, (f"recompute stalled: evaluated_at={raw_eval} "
                       f"({eval_age_h:.0f}h, limit {max_eval_age_h}h)")

    holding = data.get("final_result")
    if not holding:
        return False, f"final_result empty (final_source={data.get('final_source')!r})"

    return True, (f"{holding} via {data.get('final_source')} · unit_test pass · "
                  f"prices {worst} ({data_age_d:.1f}d, {len(dates)} tickers) · "
                  f"eval {eval_age_h:.0f}h old")


def probe_nuts_radar(url, repo_dir, catalysts_url=None, max_cat_age_h=27, **_):
    """nuts-radar — grade the TREE SHAPE, not the page returning 200.

    The radar answers "if this condition crosses, what does the book become?"
    by re-walking a copy of NUTS's tree shape held in `assets/tree.js`. That
    copy is the whole risk: if NUTS's trees are ever edited, a stale shape
    would keep confidently reporting the OLD destinations — the same class of
    silent divergence that had trading-algorithm- reporting BIL (cash) while
    NUTS was TQQQ (3x long). The page defends itself by self-checking on every
    load and hiding all consequences on mismatch, but nobody is looking at the
    page at 5 AM, so `web_200` here would be pure decoration.

    So this runs the repo's OWN checker, `job/selfcheck.js`, which replays
    assets/tree.js against a live /evaluate and asserts it reproduces NUTS's
    own frontrunners / ftlt / blackswan results. Exit 1 = the shape has drifted.
    Running the repo's script rather than reimplementing the walk here is
    deliberate: a third copy of the tree would be a third thing to drift.

    Plain GET inside the script — never ?force=true (see reference-nuts-algo).

    Three assertions:
      1. the site serves 200 (transport — cheap, and it is the delivery path
         for the 6 AM Telegram link)
      2. selfcheck.js exits 0 (the tree shape still matches NUTS)
      3. catalysts.json freshness. The builder runs at 04:30 — deliberately
         BEFORE this 5:00 check, so a failed build is caught the same morning
         rather than ~23 h later. (It was 05:45 first, which put it after the
         check and made the 6:30 retry slot useless for it, since fleet_health
         exits early once the day's digest has gone out.) A healthy file is
         ~30 min old at check time; 27 h is the limit, which fails a single
         missed night.
         A null `generated_at` is now a FAILURE, not a shrug — it means the
         file was never built by job/build_catalysts.py.

    Deliberately NOT a launchd_exit probe: exit 0 would only prove the wrapper
    woke up. Grading catalysts.json grades the thing that matters.
    """
    ok, detail = probe_web_200(url)
    if not ok:
        return False, f"site: {detail}"

    script = os.path.join(os.path.expanduser(repo_dir), "job", "selfcheck.js")
    if not os.path.exists(script):
        return False, f"selfcheck.js missing at {script}"
    try:
        p = subprocess.run(["node", script], capture_output=True, text=True,
                           timeout=90, cwd=os.path.dirname(script))
    except FileNotFoundError:
        raise RuntimeError("node not on PATH for fleet-health")
    if p.returncode != 0:
        fails = [ln.strip() for ln in p.stdout.splitlines() if "FAIL" in ln]
        return False, ("TREE SHAPE DRIFTED from NUTS — consequences on the "
                       "radar are hidden until assets/tree.js is re-derived "
                       "from NUTS/backend/trees/. " + (" · ".join(fails)
                       or (p.stderr or "").strip()[:200]))
    book = next((ln.split("=", 1)[1].strip() for ln in p.stdout.splitlines()
                 if ln.strip().startswith("book =")), "?")

    cat = "catalysts not live yet"
    if catalysts_url:
        try:
            with urllib.request.urlopen(catalysts_url, timeout=30) as r:
                cj = json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                return False, f"catalysts.json unreadable: {exc}"
            raise RuntimeError(f"catalysts.json fetch failed: {exc}") from exc   # infra retry
        except Exception as exc:                       # noqa: BLE001
            return False, f"catalysts.json unreadable: {exc}"
        gen = cj.get("generated_at")
        if not gen:
            return False, "catalysts.json has no generated_at — never built by job/"
        age = _age_hours(_parse_stamp(gen).timestamp())
        if age > max_cat_age_h:
            return False, (f"catalysts stale: generated_at={gen} "
                           f"({age:.0f}h, limit {max_cat_age_h}h) — the 05:45 "
                           f"build has not landed")
        miss = cj.get("missing") or []
        cat = f"{len(cj.get('items') or [])} catalysts ({age:.0f}h old)"
        if miss:
            cat += f" · {len(miss)} source issue(s): {miss[0][:90]}"

    return True, f"tree shape matches NUTS · book {book} · {cat}"


def probe_one_clock_lambda(log_group="/aws/lambda/gh-dispatcher",
                           ping_window_min=75, min_pings=2,
                           dispatch_window_h=26, min_dispatches=3, **_):
    """Health of the One Clock dispatch machinery itself (EventBridge Scheduler
    -> gh-dispatcher Lambda), independent of any single job.

    The expect_event checks on individual gh_run probes tell you a given job's
    primary went quiet, one job at a time, up to a day late. This probe watches
    the shared machinery directly, so one red row names the real culprit:

      1. Any ERROR/Traceback in the Lambda log inside 24h -> red with the tail,
         UNLESS Lambda's own async-invoke retry re-ran the SAME RequestId and
         that later attempt had no error (a one-off transient, e.g. GitHub's
         API returning a 502, self-healed with no human action). Still red
         for the case that actually needs a human: every attempt for that
         RequestId erroring (a revoked/rotated-but-not-updated GH_DISPATCH_PAT
         — urlopen raises on the 401 — a bad deploy, or a Lambda timeout that
         keeps recurring), in ONE place.
      2. PING OK count in the last `ping_window_min` minutes (health-hub tick,
         cron(1/15 ...) = 5 expected per 75 min; >= `min_pings` tolerates
         transients). Proves scheduler->Lambda->URL end to end, every hour of
         the day -- this is the canary, because it is the only One Clock leg
         that fires often enough to grade freshly at 5 AM.
      3. DISPATCH OK count across `dispatch_window_h` -> proves the PAT leg
         (mental-models 00:10 ET + >=4 daytime monitors land inside any 26h).

    Uses the `aws` CLI as claude-ops (CloudWatchLogsReadOnly); run_health.sh
    puts /usr/local/bin on PATH. An aws-CLI failure RAISES (infra retry path),
    the same convention as probe_gh_run's log fetch: an unreadable log is not
    a log with missing markers.

    RED-TEAM FIX 2026-09-22. A single transient GitHub-side 502 (not a PAT or
    deploy problem — a bad PAT gives 401/403, not 502) flagged this probe red
    even though Lambda's built-in async-invoke retry reused the SAME RequestId
    54s later and succeeded (`DISPATCH OK repo=ynab-budget-brief`). Verified in
    CloudWatch: both `START RequestId: aa6ab1ea-...` blocks share one id; the
    first ends in `[ERROR] HTTPError: HTTP Error 502`, the second in a clean
    REPORT. Self-healed noise like that must not page a human.
    """
    req_id_re = re.compile(r"RequestId:\s*([a-f0-9-]+)")

    def _logs(minutes_back, pattern):
        start = int((time.time() - minutes_back * 60) * 1000)
        p = subprocess.run(
            ["aws", "logs", "filter-log-events", "--log-group-name", log_group,
             "--start-time", str(start), "--filter-pattern", pattern,
             "--query", "events[].message", "--output", "text"],
            capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError(f"aws logs failed: {p.stderr.strip()[:150]}")
        out = p.stdout.strip()
        return [l for l in out.splitlines() if l.strip()] if out else []

    def _events(minutes_back):
        start = int((time.time() - minutes_back * 60) * 1000)
        p = subprocess.run(
            ["aws", "logs", "filter-log-events", "--log-group-name", log_group,
             "--start-time", str(start),
             "--query", "events[].{t:timestamp,m:message}", "--output", "json"],
            capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise RuntimeError(f"aws logs failed: {p.stderr.strip()[:150]}")
        out = p.stdout.strip()
        return json.loads(out) if out else []

    errors = _logs(24 * 60, "?ERROR ?Traceback ?\"Task timed out\"")
    if errors:
        # Group the day's events into per-RequestId retry attempts so a
        # transient that Lambda already retried into a success doesn't page.
        events = sorted(_events(24 * 60), key=lambda e: e["t"])
        attempts = {}  # RequestId -> [had_error, had_error, ...] in order
        cur_id, cur_error = None, False
        for e in events:
            msg = e["m"]
            if msg.startswith("START RequestId:"):
                m = req_id_re.search(msg)
                cur_id, cur_error = (m.group(1) if m else None), False
            elif "[ERROR]" in msg or "Task timed out" in msg:
                cur_error = True
            elif msg.startswith("REPORT RequestId:"):
                m = req_id_re.search(msg)
                rid = m.group(1) if m else cur_id
                if rid:
                    attempts.setdefault(rid, []).append(cur_error)
        # Red only for a RequestId where every attempt in the window errored
        # (retries exhausted or none happened yet) — a real, unresolved fault.
        unresolved = [rid for rid, tries in attempts.items()
                      if tries and all(tries)]
        if unresolved:
            return False, (f"{len(errors)} Lambda error line(s) in 24h, "
                           f"{len(unresolved)} RequestId never recovered on "
                           f"retry — first: {errors[0][:180]}\n(check "
                           f"GH_DISPATCH_PAT validity and the gh-dispatcher "
                           f"deploy; log group {log_group})")
    pings = _logs(ping_window_min, '"PING OK"')
    if len(pings) < min_pings:
        return False, (f"only {len(pings)} PING OK in {ping_window_min}m "
                       f"(expect ~{ping_window_min // 15}) — EventBridge "
                       f"Scheduler or the Lambda is not firing; check "
                       f"`aws scheduler list-schedules --name-prefix one-clock`")
    dispatches = _logs(dispatch_window_h * 60, '"DISPATCH OK"')
    if len(dispatches) < min_dispatches:
        return False, (f"only {len(dispatches)} DISPATCH OK in "
                       f"{dispatch_window_h}h (expect >=5) — the workflow_dispatch "
                       f"leg (PAT) is failing while pings still pass; the "
                       f"GH_DISPATCH_PAT has likely been revoked or lost repos")
    err_note = (f"{len(errors)} error line(s) self-healed on retry"
                if errors else "0 errors")
    return True, (f"{len(pings)} pings/{ping_window_min}m, "
                  f"{len(dispatches)} dispatches/{dispatch_window_h}h, {err_note}")


def probe_rsync_log(log_path, max_age_h=36, min_files=1, read_timeout_s=60, **_):
    """The LAST rsync run in an append-only backup log actually copied a real tree.

    Replaces launchd_exit on the T7 backup (2026-09-12). Three independent things
    can go wrong here and an exit status sees only one of them:

      1. The job never ran            -> no run block dated inside max_age_h.
      2. rsync errored mid-transfer   -> `sync done (exit 23)`, which has happened
         twice in this log, not exit 0.
      3. The Google-Drive source never mounted -> rsync walks an EMPTY tree, copies
         nothing, and exits 0. This is the failure that matters most: it is
         completely silent, and it is discovered on the one day you actually need
         to restore.

    RED-TEAM FIX 2026-09-12. The first version of this probe asserted
    `Number of files >= 1` and was a FALSE GREEN, verified against rsync 3.4.4:

        $ rsync -a --stats empty_src/ dst/   # mount point exists, volume gone
        Number of files: 1 (dir: 1)          # <- the source dir counts as a file
        exit 0

    Only a source directory that does not EXIST gives `0` + exit 23. An unmounted
    volume whose mount point survives - the common macOS shape, and the exact T7
    case - gives 1 and exit 0, and the old floor passed it.

    So the assertion is now a BASELINE on the `reg:` sub-count, not a floor on the
    total. `Number of files: 31,007 (reg: 26,975, dir: 4,030, link: 2)` is the live
    shape; an empty tree prints `1 (dir: 1)` with no `reg:` group at all, so a
    missing `reg:` group is itself the unmounted signal. `min_files` is per-row and
    deliberately well below the true count (deleting a big folder must not page),
    but far above what a partial or absent mount can produce.

    Grades only the FINAL run block. The log is append-only, so an unscoped regex
    would cheerfully match a healthy run from last March and call the backup well.

    A missing/unreadable log is itself a failure: it means the T7 volume is not
    mounted, which is exactly the state where nothing is being backed up.
    """
    path = os.path.expanduser(log_path)
    # Read on a daemon thread with a deadline, so a blocked open() cannot freeze
    # the whole run. 2026-09-13: the first launchd run after this probe shipped sat
    # 27h inside open() — macOS held the read behind a Removable Volumes permission
    # prompt for python3.14 (terminal runs pass on iTerm/tmux's own grant) that
    # nobody was awake to click, so health.json was never pushed and the Actions
    # watchdog paged a day later. A brew python upgrade re-arms that prompt. The
    # read stays in THIS process on purpose: the launchd TCC log attributes it to
    # python3.14 itself, the binary holding the grant; a child like /bin/cat would
    # be a separate request whose attribution was never verified.
    box = {}

    def _read():
        try:
            with open(path, errors="replace") as f:
                box["text"] = f.read()
        except OSError as e:
            box["err"] = e

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(read_timeout_s)
    if reader.is_alive():
        return False, (f"reading {log_path} blocked >{read_timeout_s}s — most likely "
                       f"a macOS permission prompt: allow "
                       f"{os.path.realpath(sys.executable)} under System Settings › "
                       f"Privacy & Security › Files & Folders › Removable Volumes")
    if isinstance(box.get("err"), FileNotFoundError):
        return False, f"{log_path} unreachable — T7 volume not mounted?"
    if "err" in box:
        return False, f"{log_path} unreadable: {box['err']}"
    text = box["text"]

    starts = list(re.finditer(r"^=== (\d{4}-\d\d-\d\d) ([\d:]+) sync start ",
                              text, re.M))
    if not starts:
        return False, "no rsync run block found in log"
    last = starts[-1]
    block = text[last.start():]

    done = re.search(r"^=== (\d{4}-\d\d-\d\d) ([\d:]+) sync done \(exit (\d+)\) ===",
                     block, re.M)
    if not done:
        return False, f"run started {last.group(1)} {last.group(2)} never finished"

    stamp = f"{done.group(1)} {done.group(2)}"
    code = int(done.group(3))
    try:
        ts = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return False, f"unparseable finish stamp {stamp!r}"
    age = _age_hours(ts)
    if age > max_age_h:
        return False, f"last backup finished {age:.0f}h ago (limit {max_age_h}h)"
    if code != 0:
        return False, f"rsync exited {code} at {stamp} (23 = partial transfer)"

    nfiles = re.search(r"^Number of files: ([\d,]+)(?: \(([^)]*)\))?", block, re.M)
    if not nfiles:
        return False, f"no rsync --stats block in the {stamp} run"
    count = int(nfiles.group(1).replace(",", ""))
    # The `reg:` sub-count is the only number that means "real files were walked".
    # No `reg:` group at all => the tree held nothing but directories => unmounted.
    reg = re.search(r"reg: ([\d,]+)", nfiles.group(2) or "")
    if not reg:
        return False, (f"rsync saw {count} entries but NO regular files at {stamp} "
                       f"— source tree empty/unmounted")
    reg_n = int(reg.group(1).replace(",", ""))
    if reg_n < min_files:
        return False, (f"rsync saw only {reg_n:,} regular files at {stamp} "
                       f"(baseline {min_files:,}) — partial or unmounted source")

    moved = re.search(r"^Number of regular files transferred: ([\d,]+)", block, re.M)
    moved_n = int(moved.group(1).replace(",", "")) if moved else -1
    return True, (f"{stamp}, {reg_n:,} regular files seen "
                  f"(baseline {min_files:,}), {moved_n:,} transferred, {age:.0f}h ago")


def _last_expected_tick(active_window, now=None):
    """Latest UTC time a job ticking inside active_window ("HH:MM", "HH:MM",
    UTC, may wrap past midnight) should have run by now: now itself while
    inside the window, else the most recent window end. UTC because the
    schedules are UTC crons -- a local window drifts an hour at every DST flip.
    `now` is an aware UTC datetime."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    (fh_, fm), (th, tm) = (map(int, t.split(":")) for t in active_window)
    mins, start, end = now.hour * 60 + now.minute, fh_ * 60 + fm, th * 60 + tm
    inside = (start <= mins <= end) if start <= end else (mins >= start or mins <= end)
    since_open = (mins - start) % 1440
    if inside and since_open > 20:           # just opened: the first tick may not be logged yet
        return now
    last_end = now.replace(hour=th, minute=tm, second=0, microsecond=0)
    return last_end if last_end <= now else last_end - datetime.timedelta(days=1)


def _cloudwatch_last_outcome(log_group, log_grep, max_age_h, fail_grep=None,
                             active_window=None, slack_min=20):
    """See probe_cloudwatch_marker(today_only=False).

    active_window ("10:00", "23:55"): the newest good outcome must be within
    slack_min of the last tick the schedule owed -- a Lambda whose schedule
    stopped at 18:00 would otherwise pass on its 17:xx ticks the next morning."""
    start_ms = int((time.time() - max_age_h * 3600) * 1000)
    p = subprocess.run(
        ["aws", "logs", "filter-log-events", "--log-group-name", log_group,
         "--start-time", str(start_ms),
         "--query", "events[].[timestamp,message]", "--output", "text"],
        capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"aws logs failed: {p.stderr.strip()[:150]}")
    events = []                                  # [ms, text] per log event
    for line in (p.stdout or "").splitlines():
        head = line.split("\t", 1)
        if len(head) == 2 and head[0].strip().isdigit() and len(head[0].strip()) >= 12:
            events.append([int(head[0].strip()), head[1]])
        elif events:
            events[-1][1] += "\n" + line
    if not events:
        return False, f"no log events in {log_group} for {max_age_h}h — the Lambda did not run"
    patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
    good = [ms for ms, t in events if any(re.search(pat, t) for pat in patterns)]
    bad = [(ms, t) for ms, t in events if fail_grep and re.search(fail_grep, t)]
    if not good:
        why = f"; newest failure: {bad[-1][1].strip()[:90]!r}" if bad else ""
        return False, (f"{len(events)} events in {max_age_h}h but no success outcome "
                       f"({', '.join(repr(m) for m in patterns)}){why}")
    age = _age_hours(good[-1] / 1000)
    if active_window:
        owed = _last_expected_tick(active_window)
        newest = datetime.datetime.fromtimestamp(good[-1] / 1000, datetime.timezone.utc)
        if newest < owed - datetime.timedelta(minutes=slack_min):
            return False, (f"newest good outcome {newest:%m-%d %H:%M} UTC but the schedule "
                           f"owed a tick by {owed:%m-%d %H:%M} UTC — the Lambda stopped firing")
    # Two failures after the newest good, not one (round 8): a lone Lambda
    # timeout on the night's LAST tick (03:55 UTC) has no later tick to heal it
    # until 14:00 UTC, so it pinned the row red -- and buzzing -- all morning.
    # A job that is really broken fails every tick and piles up far more.
    since = [b for b in bad if b[0] > good[-1]]
    if len(since) >= 2:
        return False, (f"latest runs are failing ({len(since)} since the last good): "
                       f"{since[-1][1].strip()[:90]!r} "
                       f"({_age_hours(since[-1][0] / 1000):.1f}h ago; last good outcome {age:.1f}h ago)")
    if since:
        return True, (f"{len(good)} good outcomes in {max_age_h}h, newest {age:.1f}h ago; "
                      f"1 failure since (one-off, not repeated): {since[-1][1].strip()[:60]!r}")
    return True, f"{len(good)} good outcomes in {max_age_h}h, newest {age:.1f}h ago, no failure since"


def probe_cloudwatch_marker(log_group, log_grep, max_age_h=30, today_only=True,
                            fail_grep=None, active_window=None, **_):
    """A Lambda's own success marker in CloudWatch, for work that leaves NO trace
    on GitHub.

    Added 2026-09-12 for financial-telegram-bot's daily report — the weakest row
    on the board while being one of the most important jobs on it.

    Why gh_run cannot do this one: the GitHub workflow is a 30-minute BACKSTOP
    that defers to the AWS Lambda. On a normal day its entire log is "Lambda
    already delivered today — skipping runner send (no double-report)". There is
    no marker to grep because the workflow deliberately did nothing, and grading
    it green proved only that the backstop correctly stood down. The actual proof
    that a report reached Jalal's phone is REPORT_DELIVERED, in CloudWatch.

    Nor can the skip-line be asserted instead: on the one day the Lambda fails,
    the backstop DOES send and prints something else, so a probe keyed to the
    skip-line would go red exactly when the safety net worked.

    Same conventions as probe_gh_run: `log_grep` is one regex or a list and ALL
    must match; `{date}` expands to today|yesterday. An aws-CLI failure RAISES
    (infra retry path) rather than reporting a missing marker — an unreadable log
    is not a log without markers. Runs as claude-ops, CloudWatchLogsReadOnly.

    today_only=False + fail_grep (2026-09-27, for the ynab-nag Lambda, which
    ticks every 5 min from 10:00 to 23:55 and so has NO events "today" at 05:00):
    grade the whole max_age_h window, and fail when the NEWEST event matching
    fail_grep is later than the newest event matching log_grep -- the job's
    latest runs all broke (a KV outage returns "kv-error" every tick and sends
    nothing) even though healthy ticks from earlier are still in the window.
    """
    if not today_only:
        return _cloudwatch_last_outcome(log_group, log_grep, max_age_h, fail_grep,
                                        active_window=active_window)
    start_ms = int((time.time() - max_age_h * 3600) * 1000)
    p = subprocess.run(
        ["aws", "logs", "filter-log-events",
         "--log-group-name", log_group,
         "--start-time", str(start_ms),
         "--query", "events[].[timestamp,message]", "--output", "text"],
        capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"aws logs failed: {p.stderr.strip()[:150]}")
    raw = p.stdout or ""
    # Keep the WIDE read window (it distinguishes "log group unreachable" from
    # "the Lambda produced nothing today"), but correlate the markers to TODAY's
    # events. Red team round 3: with --query events[].message the timestamps were
    # discarded and every pattern was matched against 30h of concatenated text, so
    # two markers that must describe the SAME run could be satisfied by two
    # different runs a day apart. Nothing here was a live false green -- the
    # {today} pin and the explicit ok=false check closed the reachable paths --
    # but this is the only proof the daily report was delivered, and it should
    # not depend on a second assertion to stay honest.
    _today_d = datetime.date.today()
    _today_lines, _cur_is_today = [], False
    for line in raw.splitlines():
        head = line.split("\t", 1)
        if len(head) == 2 and head[0].strip().isdigit() and len(head[0].strip()) >= 12:
            ev_ms = int(head[0].strip())
            _cur_is_today = (datetime.date.fromtimestamp(ev_ms / 1000) == _today_d)
            if _cur_is_today:
                _today_lines.append(head[1])
        elif _cur_is_today:
            _today_lines.append(line)          # continuation of a multi-line event
    text = "\n".join(_today_lines)
    if not raw.strip():
        return False, f"no log events in {log_group} for {max_age_h}h"
    if not text.strip():
        return False, (f"{log_group} has events in the last {max_age_h}h but NONE "
                       f"from today ({_today_d}) — the Lambda did not run")

    today = datetime.date.today()
    dates = "|".join(d.isoformat() for d in
                     (today, today - datetime.timedelta(days=1)))

    # {today} vs {date}: {date} carries the usual today|yesterday buffer, which
    # every row whose slot can land after the 5 AM check needs. This job does NOT
    # need it — the Lambda fires 04:15 and the check runs 05:00 — and taking the
    # buffer anyway costs a full day of blindness: with a 30 h read window, a day
    # where the Lambda never ran leaves YESTERDAY's 04:15 lines sitting 24 h 45 m
    # back, inside the window, satisfying "yesterday", and the row reports a
    # report that was never sent. {today} pins to today alone so a missed day
    # fails the same morning; the wide read window is kept so a late-but-delivered
    # report is still found.
    patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
    missing = [pat for pat in patterns
               if not re.search(_expand_dates(pat, today), text)]
    if missing:
        return False, (f"delivery marker missing in {max_age_h}h of CloudWatch "
                       f"({', '.join(repr(m) for m in missing)})")

    # ok=false means the Lambda ran and FAILED to deliver — the exact state a
    # green Lambda invocation hides. Surface errors=N without failing on it: a
    # partial report still reached him, and paging on one bad section would
    # train him to ignore this row.
    # The NEWEST REPORT_DELIVERED line decides (red team 2026-09-27): a failed
    # first attempt followed by a successful retry delivered the report.
    last_rd = re.findall(r"REPORT_DELIVERED ok=(true|false)", text)
    if last_rd and last_rd[-1] == "false":
        return False, "REPORT_DELIVERED ok=false — Lambda ran but did not deliver"
    errs = re.findall(r"REPORT_DELIVERED ok=true sections=(\d+) errors=(\d+)", text)
    if errs:
        sections, errors = errs[-1]
        note = f"delivered, {sections} sections"
        if int(errors):
            note += f", {errors} section error(s) — report went but incomplete"
        return True, note
    return True, "delivery marker confirmed"


def probe_log_block(log_path, block_re, log_grep, max_age_h, fail_grep=None, **_):
    """Assert markers inside the LAST run block of an append-only log.

    Added 2026-09-12 for toolcheck, which had no probe at all. The generic
    problem it solves: `probe_log_marker` pins {date} to today|yesterday, which
    is right for a daily job and useless for a WEEKLY one — toolcheck runs
    Sundays, so on a Tuesday there is no today/yesterday marker to find and the
    row would page every week. Dropping the date pin instead is worse: the log is
    append-only, so a bare "24 passed, 0 failed" would match a healthy run from
    August and report a toolbox that has been broken for a month as fine.

    So: isolate the newest block, check ITS timestamp against max_age_h, and
    require every marker inside that block only.

    `block_re` must capture a parseable "YYYY-MM-DD HH:MM:SS" as group 1.
    `fail_grep` (round 8): a line the producer prints on a partial failure that
    does not stop it printing its success marker; a match in the block is red.
    """
    path = os.path.expanduser(log_path)
    try:
        text = _read(path)
    except FileNotFoundError:
        return False, f"{log_path} missing"
    blocks = list(re.finditer(block_re, text, re.M))
    if not blocks:
        return False, "no run block found in log"
    last = blocks[-1]
    try:
        ts = datetime.datetime.strptime(last.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()
    except (ValueError, IndexError):
        return False, f"unparseable block stamp {last.group(0)[:60]!r}"
    age = _age_hours(ts)
    if age > max_age_h:
        return False, (f"last run {age/24:.1f}d ago "
                       f"(limit {max_age_h/24:.1f}d) — job has stopped firing")
    block = text[last.start():]
    patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
    missing = [pat for pat in patterns if not re.search(pat, block, re.M)]
    if missing:
        tail = " / ".join(l for l in block.strip().splitlines()[-2:])
        return False, (f"last run {age/24:.1f}d ago but marker missing "
                       f"({', '.join(repr(m) for m in missing)}) — tail: {tail[:100]}")
    hit = re.search(fail_grep, block, re.M) if fail_grep else None
    if hit:
        line = block[block.rfind("\n", 0, hit.start()) + 1:].split("\n", 1)[0].strip()
        return False, f"last run {age/24:.1f}d ago finished, but part failed: {line[:110]!r}"
    return True, f"clean run {age/24:.1f}d ago"


def probe_log_tail(path, last_line, max_age_h, grace_min=0, grace_lines=12, **_):
    """Fresh log whose LAST non-empty line is a success shape.

    For a job that prints a one-line outcome but no timestamp (tranche-nag:
    `sent N chars` or `nothing due`). mtime proves it ran; the last line proves
    the run ENDED in a success state rather than a traceback. Added red team
    round 4, 2026-09-12, replacing a file_mtime row whose waiver claimed the job
    "prints no success marker" -- which was not true.

    grace_min: a job that logs a retry chain before its outcome (aoife-typing:
    up to ~14 min of model timeouts, then `wrote coach`) is mid-run whenever the
    file was written < grace_min ago. Then a success line anywhere in the last
    grace_lines lines proves the previous cycle finished; only a tail with no
    success at all in that window fails (false alarm 2026-09-19: probed 4 min
    before the cycle's `wrote coach` line).
    """
    # {today_ymd} (round 8): for a job that writes one log per day, e.g.
    # dhaka-yearly's data/run-20260928.log.
    path = path.replace("{today_ymd}", datetime.date.today().strftime("%Y%m%d"))
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return False, f"{path} missing"
    age = _age_hours(os.path.getmtime(p))
    if age > max_age_h:
        return False, f"stale: last written {age/24:.1f}d ago (limit {max_age_h}h)"
    lines = [l for l in _read(p).splitlines() if l.strip()]
    last = lines[-1].strip() if lines else ""
    if not re.search(last_line, last):
        if grace_min and age * 60 <= grace_min:
            hit = next((l.strip() for l in reversed(lines[-grace_lines:])
                        if re.search(last_line, l.strip())), None)
            # Red team 2026-09-27: a 15-min job keeps the file young forever, so
            # grace was ALWAYS on and any old success in the last 12 lines passed
            # while short error cycles piled up behind it. When the lines carry
            # stamps, the hit may be no older than one grace window plus a cycle,
            # measured against the NEWEST line (the file's mtime proves that is now).
            def _stamp(line):
                m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?", line)
                try:
                    return _parse_stamp(m.group(1) + (m.group(2) or "")) if m else None
                except ValueError:
                    return None
            t_hit = hit and _stamp(hit)
            t_new = max((t for t in map(_stamp, lines[-grace_lines:]) if t), default=None)
            if t_hit and t_new and (t_new - t_hit).total_seconds() / 60 > 2 * grace_min + 15:
                hit = None
            if hit:
                return True, (f"written {age*60:.0f}min ago, mid-run; "
                              f"last finished {hit[:40]!r}")
        return False, f"written {age:.0f}h ago but last line is not a success: {last[:90]!r}"
    return True, f"written {age:.0f}h ago, ended {last[:40]!r}"


def probe_bot_selftest(url, secret_env, **_):
    """A synthetic message through the bot's REAL handler produced a real reply.

    Red team 2026-09-12. telegram_webhook proves the hook points at the function
    and the guard bites, but every bot answers Telegram 200 even when its handler
    raised or fell back to an apology ("brain's lagging", "the planner isn't
    answering", a caught error) -- so a bot that could not reply stayed green.

    Each bot's webhook takes `X-Selftest: 1` plus its webhook secret (header only,
    never ?s=). It then runs one fixed, read-only message (zinger: a text;
    voices: /nuts; milestones: /recent; school: preview today; health-hub:
    /status) through the real handler with every Bot API call captured instead
    of sent, and answers {"ok", "sent", "why"} -- a verdict and a count, never
    the reply text. Nobody is messaged and nothing is written.

    The secret comes from the environment (run_health.sh reads it by name from
    each bot's .env). THIS REPO IS PUBLIC: never put a secret in the roster and
    never echo one in a detail string.
    """
    secret = os.environ.get(secret_env)
    if not secret:
        return False, f"{secret_env} not set (see run_health.sh)"
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Content-Type": "application/json", "X-Selftest": "1",
        "X-Telegram-Bot-Api-Secret-Token": secret})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code >= 500:                    # cold start / platform blip: infra retry path
            raise RuntimeError(f"selftest answered HTTP {e.code}") from e
        return False, f"selftest answered HTTP {e.code}" + (" (secret rejected)" if e.code in (401, 403) else "")
    except ValueError:
        return False, "selftest answered non-JSON (old deploy without the selftest?)"
    if body.get("ok") is True and int(body.get("sent") or 0) >= 1:
        return True, f"synthetic message got a real reply ({body['sent']} captured, nothing sent)"
    return False, f"selftest failed: {str(body.get('why') or 'no verdict')[:100]}"


def _headless_browser():
    """chrome-headless-shell exits by itself after --dump-dom (about 1 s); full
    Chrome 153 dumps the DOM and then never exits, so it is only the fallback."""
    shells = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    if shells:
        return shells[-1], []
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", ["--headless=new"]


def probe_web_render(url, expect_text, **_):
    """Render the page in a headless browser and prove the app RAN.

    Red team 2026-09-12: web_200 proved the host served an app shell, and a game
    whose JavaScript throws still serves that shell with a 200. This runs the JS
    (8 s of virtual time) and fails on an uncaught error in the console, on Next's
    "Application error: a client-side exception" page, or when expect_text (body
    text, <head> excluded) is missing -- a login wall or a blank page.
    """
    exe, extra = _headless_browser()
    with tempfile.TemporaryDirectory() as prof:
        cmd = [exe, *extra, "--disable-gpu", "--no-first-run", f"--user-data-dir={prof}",
               "--enable-logging=stderr", "--v=0", "--virtual-time-budget=8000", "--dump-dom", url]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=45)
            dom, log = p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:       # full Chrome: DOM already dumped
            dom, log = e.stdout or b"", e.stderr or b""
    dom = dom.decode("utf-8", "replace")
    log = log.decode("utf-8", "replace")
    if len(dom) < 200:
        return False, f"no rendered page ({len(dom)} bytes from {os.path.basename(exe)})"
    if "Application error: a client-side exception" in dom:
        return False, "the app crashed in the browser (Next.js client exception page)"
    errs = [l for l in log.splitlines() if "CONSOLE" in l and "Uncaught" in l]
    if errs:
        m = re.search(r'"(Uncaught[^"]{0,110})', errs[0])
        return False, f"JS error on load: {m.group(1) if m else errs[0][-110:]}"
    body = re.sub(r"<head\b.*?</head>|<(script|style)\b[^>]*>.*?</\1>", " ", dom, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", body)))
    if expect_text not in text:
        return False, f"rendered, but {expect_text!r} is not on the page (wrong app, login wall or blank)"
    return True, f"rendered with no JS errors, {expect_text!r} on the page"


def probe_freshness(url, expect_items=None, bypass_env=None, **_):
    """Served-data freshness, contract v1 (2026-10-02): grade what the SCREEN serves.

    Born from aoife-typing: its coach wrote a fresh mission every 15 min (log_tail
    green), but the app read `at` while the writer stamped `generatedAt`, so the
    screen served the 11 Sep mission for three weeks. A writer-side row cannot see
    that; this row reads the app's GET /api/freshness, which reports ages ONLY:
      {"app", "v": 1, "items": [{"name", "inputAgeH", "servedAgeH", "graceH", "maxAgeH"?}]}
    inputAgeH = hours since the newest input the served thing should reflect;
    servedAgeH = hours since the input the served thing actually reflects.
    The app reports raw ages and THIS function judges, so a bug in the app's own
    logic cannot mark itself green. Red when an input is past its grace and the
    served copy is older than it, or when servedAgeH passes maxAgeH.
    expect_items: names that must be present (a renamed or dropped item is red).
    bypass_env: as probe_web_fresh, for login-only Vercel sites."""
    req = urllib.request.Request(url)
    if bypass_env:
        secret = os.environ.get(bypass_env)
        if not secret:
            return False, f"{bypass_env} not set (see run_health.sh)"
        req.add_header("x-vercel-protection-bypass", secret)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    items = data.get("items")
    if data.get("v") != 1 or not isinstance(items, list) or not items:
        return False, f"not a freshness v1 reply: {str(data)[:120]}"
    names = {str(i.get("name")) for i in items if isinstance(i, dict)}
    missing = sorted(set(expect_items or ()) - names)
    if missing:
        return False, f"freshness item(s) missing: {', '.join(missing)}"
    bad, good = [], []
    for i in items:
        name, inp, srv = i.get("name"), i.get("inputAgeH"), i.get("servedAgeH")
        grace, cap = float(i.get("graceH") or 0), i.get("maxAgeH")
        if (inp is not None and float(inp) < -1) or (srv is not None and float(srv) < -1):
            bad.append(f"{name}: stamp in the FUTURE (input {inp}h, served {srv}h) — clock/field bug")
        elif inp is not None and float(inp) > grace and (srv is None or float(srv) > float(inp) + 0.25):
            bad.append(f"{name}: screen serves data {'(none)' if srv is None else f'{float(srv):.0f}h'} old, "
                       f"newest input {float(inp):.0f}h old (grace {grace:g}h)")
        elif cap is not None and srv is None:
            # moneymap 2026-10-02: a capped feed serving NOTHING (stamp key gone) is not fresh
            bad.append(f"{name}: nothing served (limit {float(cap):g}h)")
        elif cap is not None and float(srv) > float(cap):
            bad.append(f"{name}: served data {float(srv):.0f}h old (limit {float(cap):g}h)")
        else:
            good.append(f"{name} {'nothing yet' if srv is None else f'{float(srv):.1f}h'}")
    return (not bad), ("; ".join(bad) if bad else "screen fresh: " + ", ".join(good))


def probe_pending_stamp(path, max_check_age_h, pending_grace_h, **_):
    """A Mac drift checker's JSON stamp (2026-10-03, aoife-gcal-drift):
    {"checkedAt": ISO, "pending": n, "pendingSinceAt": ISO|null}. Red when the
    checker itself stopped (checkedAt too old -- it leaves the stamp untouched on
    failure) or when a change has sat unsynced past pending_grace_h."""
    p = os.path.expanduser(path)
    try:
        data = json.loads(_read(p))
    except Exception as e:                  # noqa: BLE001
        return False, f"stamp unreadable: {type(e).__name__}"
    chk = data.get("checkedAt")
    if not chk:
        return False, "no checkedAt in stamp"
    age = _age_hours(datetime.datetime.fromisoformat(chk).timestamp())
    if age > max_check_age_h:
        return False, f"drift checker last ran {age:.1f}h ago (limit {max_check_age_h}h)"
    n = int(data.get("pending") or 0)
    if n and data.get("pendingSinceAt"):
        lag = _age_hours(datetime.datetime.fromisoformat(data["pendingSinceAt"]).timestamp())
        if lag > pending_grace_h:
            return False, f"{n} change(s) unsynced for {lag:.1f}h (grace {pending_grace_h}h)"
        return True, f"{n} change(s) pending {lag:.1f}h (inside {pending_grace_h}h grace)"
    return True, f"in sync, checked {age:.1f}h ago"


def _git(repo, *args, timeout=20):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                          text=True, timeout=timeout)


def probe_unpushed_work(roots, max_age_h=24, ignore=None, **_):
    """Commits that exist ONLY on this Mac (2026-10-03). For every git repo under
    `roots` with a remote: the CURRENT branch's commits not on its upstream (or, with
    no upstream, not on any remote). Red when any such commit is older than max_age_h
    -- if the disk dies, that work is gone. Purely local and read-only: no fetch, no
    push. Side branches are ignored on purpose (abandoned experiments are not "forgot
    to push"); repos with no remote at all are counted, not graded (Time Machine
    covers them hourly). `ignore` = {repo_dir_name: "why"}."""
    ignore = ignore or {}
    now = time.time()
    old, fresh, mac_only = [], 0, 0
    for root in roots:
        root = os.path.expanduser(root)
        single = os.path.isdir(os.path.join(root, ".git"))
        cands = [root] if single else sorted(
            os.path.join(root, d) for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d, ".git")))
        for repo in cands:
            name = os.path.basename(repo.rstrip("/"))
            if name in ignore:
                continue
            if single:                           # ~/.local/bin reads better than "bin"
                name = repo.replace(os.path.expanduser("~"), "~", 1)
            if not _git(repo, "remote").stdout.strip():
                mac_only += 1
                continue
            if not _git(repo, "branch", "--show-current").stdout.strip():
                continue                         # detached HEAD: nothing to push
            up = _git(repo, "rev-parse", "--abbrev-ref", "@{u}")
            rng = [f"{up.stdout.strip()}..HEAD"] if up.returncode == 0 \
                else ["HEAD", "--not", "--remotes"]
            stamps = [int(t) for t in
                      _git(repo, "log", "--format=%ct", *rng).stdout.split() if t.isdigit()]
            if not stamps:
                continue
            age = (now - min(stamps)) / 3600
            if age > max_age_h:
                old.append((age, f"{name} +{len(stamps)} ({age / 24:.0f}d)"))
            else:
                fresh += 1
    tail = f"; {fresh} newer than {max_age_h}h; {mac_only} Mac-only repos (no remote, Time Machine)"
    if old:
        old.sort(reverse=True)
        return False, (f"{len(old)} repo(s) hold commits only on this Mac >{max_age_h}h: "
                       + ", ".join(t for _, t in old) + tail)
    return True, "every current branch is on GitHub" + tail


PROBE_FNS = {"web_fresh": probe_web_fresh, "web_200": probe_web_200,
             "web_render": probe_web_render, "bot_selftest": probe_bot_selftest,
             "one_clock_lambda": probe_one_clock_lambda,
             "local_stamp": probe_local_stamp, "launchd_exit": probe_launchd_exit,
             "file_mtime": probe_file_mtime, "gh_run": probe_gh_run,
             "launchd_running": probe_launchd_running,
             "planner_backup": probe_planner_backup,
             "log_marker": probe_log_marker,
             "telegram_webhook": probe_telegram_webhook,
             "nuts": probe_nuts,
             "nuts_radar": probe_nuts_radar,
             "rsync_log": probe_rsync_log,
             "cloudwatch_marker": probe_cloudwatch_marker,
             "freshness": probe_freshness,
             "pending_stamp": probe_pending_stamp,
             "unpushed_work": probe_unpushed_work,
             "log_block": probe_log_block}

# ── the fleet roster ────────────────────────────────────────────────────────
# repo: GitHub repo name for the Notion row (None = not a repo, Telegram-only).
# To retire a launchd job: `launchctl unload` it and rename its plist to
# `<label>.plist.retired` (never delete the plist) — the launchd_exit/
# launchd_running probes then report the row as retired instead of paging as
# "job not loaded" (2026-09-17, after com.jalal.supervisor's row false-alarmed
# for two days past its retirement).
PROBE_FNS["log_tail"] = probe_log_tail

FLEET = [
    {"name": "dhaka-flights (nightly trip tracker)", "feeds_screen": True, "screen_side": True, "repo": "dhaka-flights",
     "probe": "web_fresh", "url": "https://dhaka-flights.vercel.app/data.json", "bypass_env": "DHAKA_VERCEL_BYPASS",
     "json_key": "updated", "max_age_h": 36,
     # healthy nights 22 Aug-27 Sep: ticket1 2-8, ticket2 9-10, sg 35-61,
     # flights 196-254 (flights=0 on 08-23 was the broken scrape this catches)
     "min_rows": {"ticket1_options": 1, "ticket2_options": 5, "sg_tickets": 20, "flights": 100}},
    # com.jalal.dhaka-hotels (launchd, 5:00 AM, run_hotel_rates.sh) — the award
    # -points research behind the trip site's Stays table. Rostered 2026-08-06;
    # schedule_snapshot has always listed it, the fleet never watched it.
    # Deliberately NOT launchd_exit: the wrapper `exit 0`s when a flight run is
    # still holding the browser session, so standing down looks exactly like a
    # successful refresh — the same trap as the T7 backup. Reading the PUBLISHED
    # file instead covers the whole chain (scrape → write → commit → push).
    # TIMING NOTE: this job fires at 5:00 AM — the SAME MINUTE as
    # com.jalal.fleet-health — so the probe usually reads YESTERDAY's file.
    # `updated` is date-only (parses as midnight), which puts a normal morning
    # at ~29 h and a genuinely missed night at ~53 h; 36 sits between them.
    # repo is None ON PURPOSE even though the job lives in dhaka-flights:
    # notion_health.py stamps one row per repo and the LAST result wins, so a
    # healthy hotel refresh would paint the flights row ✅ while the flight
    # tracker was down. Telegram carries both entries independently.
    # Graded on the OLDEST row's `checked`, NOT the top-level `updated`:
    # run_hotel_rates.py rewrites and pushes hotel_rates.json every night even
    # when it scraped nothing, so `updated` proves only that the job ran. That
    # is how 2026-08-11 → 08-16 stayed ✅ while the Browserbase free tier was
    # dry and not one rate had refreshed. `checked` is per-row and is stamped
    # ONLY on a real scrape (tests: test_build_keeps_previous_rate_when_scrape
    # _fails), so it is the field that actually means "we have fresh data".
    # max_age_h 96, not 36: a MISS on one property is normal and self-heals,
    # and both stamps are date-only (they parse as midnight, so a normal
    # morning already reads ~29 h). 96 fires on ~3 dead nights, not on one.
    {"name": "dhaka-hotels (nightly award-rate research)", "feeds_screen": True, "screen_side": True, "screen_repo": "dhaka-flights", "repo": None,
     "probe": "web_fresh",
     "url": "https://dhaka-flights.vercel.app/hotel_rates.json", "bypass_env": "DHAKA_VERCEL_BYPASS",
     "json_key": "checked", "rows_key": "rows", "max_age_h": 96},
    {"name": "carmax-scraper (nightly car picks)", "repo": "carmax-scraper",
     "probe": "local_stamp", "path": "~/PycharmProjects/carmax-scraper/.last_success_date",
     "max_age_h": 36},
    # com.jalal.mac-audit (launchd, 3:10 AM + 4:10 AM retry) — nightly security
    # posture diff of the Mac itself (repo: mac-audit, private).
    # This job is SILENT BY DESIGN: it Telegrams only when the posture changed,
    # so "no message" proves nothing on its own. That makes this probe the only
    # thing separating "quiet because healthy" from "quiet because dead" — which
    # is exactly the failure mode the 2026-08-06 audit found across the fleet.
    # state/last_success is written LAST and ONLY after a run that could actually
    # see the machine (<= MAX_BLIND collectors unavailable), so a green tick here
    # proves the collectors ran — not merely that the wrapper woke up and exited 0.
    # 24 h: the job stamps at ~03:10 and this check runs at 05:00, so a healthy
    # morning reads ~1.8 h while the FIRST missed night already reads ~25.8 h.
    {"name": "mac-audit (nightly security posture)", "repo": "mac-audit",
     "probe": "local_stamp", "path": "~/PycharmProjects/mac-audit/state/last_success",
     "max_age_h": 24},
    # Two separate workflows against the same source — the Daily snapshot and
    # the cumulative Historical sheet. They failed together 2026-08-04/05 but
    # only the Daily one was rostered, so the digest under-reported it as a
    # single failure. Both markers assert the 7-region fan-out ran rather than
    # that deals were found: whole regions legitimately sit at zero deals.
    #
    # max_age_h 48 -> 24 on 2026-08-06: the crons moved 07:04/07:06 -> 03:54/03:56
    # UTC that day (commit ce56a69). This repo runs 2.0-3.6 h behind its cron
    # (8 days measured), so runs now land ~05:54-07:30 UTC and are 1.5-3.1 h old
    # at the 09:00 UTC check — a MISSED day shows 25.5-27.1 h, which 48 slept
    # through. 24 catches the first miss and still tolerates 5 h of GitHub
    # lateness beyond anything observed.
    {"name": "leasehackr-scraper (daily deals)", "repo": "leasehackr-scraper",
     # 26, not 24 (3 Oct 2026): a 23:56 start left ~0 slack for a check run at
     # 23:59. A missed night is still red at the next 05:00 check (age >= 29 h).
     "probe": "gh_run", "workflow": "daily_scraper.yml", "max_age_h": 26,
     # 2 Oct 2026: AWS One Clock (23:56 ET) is the primary start. The job's
     # 12 h dedupe guard makes a second same-night run (AWS retry or GH
     # backstop) print the skip line instead -- that run is healthy too.
     "log_grep": [r"Found [1-9]\d* unique deal cards across [1-9]\d* regions"
                  r"|dedupe guard: a successful run in the last 12 h, skipping",
                  r"Scraped [1-9]\d* deals total"
                  r"|dedupe guard: a successful run in the last 12 h, skipping"],
     "expect_event": "workflow_dispatch"},
    {"name": "leasehackr-scraper (historical sheet)", "repo": "leasehackr-scraper",
     # 26, not 24 (3 Oct 2026): daily 23:54 start, same reasoning as the daily row.
     "probe": "gh_run", "workflow": "weekly_scraper.yml", "max_age_h": 26,
     "log_grep": [r"Found [1-9]\d* unique deal cards across [1-9]\d* regions",
                  r"refreshed the dashboard with [1-9]\d* sorted deals"],
     # 2 Oct 2026: AWS One Clock (23:54 ET) is the primary start; red when only
     # the GH backstop ran, i.e. the AWS schedule went quiet.
     "expect_event": "workflow_dispatch"},
    # 48 -> 36 on the three daily entries below (2026-08-06). Their runs land
    # AFTER the 09:00 UTC check, so the freshest run the check can ever see is
    # yesterday's — 17-23 h old on a good day, 41-47 h after ONE missed day.
    # 48 therefore needed TWO consecutive misses to alarm; 36 catches the first
    # while leaving 13-19 h of slack over the worst measured run time.
    # ── One Clock (AWS EventBridge primary triggers, 2026-08-29) ────────────
    # The dispatch machinery itself. One red row here names the real culprit
    # when several expect_event rows would otherwise go red one by one.
    {"name": "one-clock (EventBridge->Lambda dispatcher)", "repo": "one-clock",
     "probe": "one_clock_lambda"},
    # The dead-man's-switch's own dead-man's-switch: health.yml (GH side)
    # watches this Mac; this row watches health.yml back, and expect_event
    # confirms AWS (one-clock-notion-health, 12:37 UTC) is what fires it —
    # mutual watching, so neither side can die silently.
    # Marker added 2026-09-12. The job prints its own dated confirmation after the
    # Notion write, so {date} is pinnable — a green run that wrote nothing fails.
    {"name": "github-notion-sync (daily health stamp)", "feeds_screen": True, "screen_repo": "voices-bot", "repo": "github-notion-sync",
     "probe": "gh_run", "workflow": "health.yml", "max_age_h": 36,
     "log_grep": r"Notion updated: [1-9]\d* rows.*checked {date}",
     "expect_event": "workflow_dispatch"},
    # AAII (2026-09-27): sentiment-scraper + its Google Sheet are RETIRED (the
    # sheet's service-account key had leaked). The dashboard now reads aaii.com
    # itself (Substack backup) at /api/aaii; the morning bot reads that API.
    # as_of is AAII's weekly survey date (Wednesdays), so 240 h = one missed week.
    {"name": "financial-dashboard /api/aaii (AAII weekly, fresh)", "repo": "financial-telegram-bot",
     "probe": "web_fresh", "url": "https://financial-telegram-bot-beryl.vercel.app/api/aaii",
     "json_key": "as_of", "max_age_h": 240},
    # Red whenever the Substack backup is carrying the data: a dead primary
    # source must not hide behind a working fallback.
    {"name": "financial-dashboard /api/aaii (primary = aaii.com)", "repo": "financial-telegram-bot",
     "probe": "web_200", "url": "https://financial-telegram-bot-beryl.vercel.app/api/aaii",
     "expect_text": "\"source\":\"aaii.com\"",
     "weak_ok": "asserts WHICH source served; the /api/aaii freshness row above is the data check"},
    # ynab-budget-brief: cron 11:00 UTC, actually runs 12:00-13:40 (10 days).
    # Since the 2026-08-19 quota redesign the run sends TWO messages (Eating
    # Out, then Aoife+Nabila). Each marker is printed only AFTER its
    # send_telegram() returns — together they prove both messages were
    # actually delivered, not merely that Python exited 0.
    {"name": "ynab-budget-brief (7am budget brief)", "repo": "ynab-budget-brief",
     "probe": "gh_run", "workflow": "daily_brief.yml", "max_age_h": 36,
     "log_grep": [r"Sent eating-out brief:", r"Sent family brief:"],
     "expect_event": "workflow_dispatch"},
    # Markers added 2026-09-12, as a PAIR. The slot line carries the date and
    # which half of the day it is; the append line carries the metric count. Both
    # must match: the slot alone proves only that the job picked a slot, and the
    # append alone could be a stale re-run of yesterday's slot.
    {"name": "financial-dashboard-history (2x-daily snapshots)", "feeds_screen": True, "screen_repo": "financial-telegram-bot", "repo": "financial-dashboard-history",
     "probe": "gh_run", "workflow": "scraper.yml", "max_age_h": 36,
     # no_rescue + either/or (red team, 2026-09-12). The AM and PM runs are
     # DIFFERENT snapshots, so the earlier-run fallbacks above were greening a
     # missing PM off the AM run's slot line. With the fallbacks off, the row must
     # be provable from the newest run's OWN log -- and there are two legitimate
     # ways it proves its slot: it appended, or its dedupe guard found the slot
     # already done by an earlier run and correctly skipped the append.
     "no_rescue": True,
     # {today}, not {date} (red team round 3). The job labels its slots by UTC
     # date (a run at 01:30 UTC prints slot <UTC-today>-AM, which is 21:30 the
     # previous evening ET). Combined with the today|yesterday buffer and
     # (?:AM|PM), ONE marker accepted FOUR different slot strings -- so three of
     # the four snapshots in the window could be missing and the row stayed green.
     # Pinned to today, the alternation is safe: at the 05:00 ET check only the
     # AM slot can exist yet, so a missed AM now goes red the same morning.
     # {utctoday} (4 Oct 2026): the job labels slots by UTC date, and the 23:00 ET
     # quiet run is past midnight UTC -- {today} paged the healthy 21:30 ET run.
     "log_grep": [r"slot {utctoday}-(?:AM|PM)",
                  # carried_forward capped under half of the 38 metrics (producer-side red
                  # team 2026-09-12): with every fetcher down, apply_fallbacks copies the
                  # last row forward and still prints "successfully appended". Normal is
                  # 0-3 (last 20 appends).
                  # 2026-09-27: the producer now caps each carry at 4 runs and prints
                  # stale=N (columns it had to leave blank); any stale column is red.
                  r"(?:Data successfully appended to Google Sheet\. \(\d+ metrics; carried_forward=(?:1[0-8]|[0-9]), na_remaining=\d+, stale=0\)"
                  r"|dedupe guard: slot {utctoday}-(?:AM|PM) already has a successful run)"],
     "expect_event": "workflow_dispatch"},
    # vix-fear-greed: RETIRED + ARCHIVED 2026-08-29, probe deliberately removed.
    # Its whole job was writing the FEAR/GREED tag into the VIX sheet's cell C2.
    # That computation now lives in financial-telegram-bot
    # (dashboard/lib/vixFearGreed.js, cascade CBOE -> FRED -> C2), and both the
    # dashboard and the Telegram brief read it from /api/sheets instead of the
    # cell. The tag is therefore covered by the financial-telegram-bot probes
    # below; a probe here would only ever fail on an archived repo.
    # ── added 2026-08-06 after a GitHub-wide Actions outage took down hedgelab
    # and trading-algorithm- for hours and the digest said NOTHING: neither repo
    # was rostered. Both have an `if: failure()` Telegram step, which is useless
    # for exactly this failure mode — when a runner is never acquired the job
    # never starts, so no in-workflow step can alert. Only an external probe can.
    #
    # max_age_h on the two weekday-only entries is deliberately 72, not 48: the
    # last Friday run is ~60-64h old by the time the Monday 5am ET (09:00 UTC)
    # check runs, and both repos' Monday crons fire AFTER it. 48 would page every
    # Monday morning.
    # Marker added 2026-09-12: the committed result file for today. NOTE the trap
    # this relies on probe_gh_run's earlier-run fallback to survive — hedgelab has
    # a duplicate guard, so the NEWEST green run of the day is often just
    # "Today's plan already committed — skipping duplicate." with no marker at
    # all. The fallback re-checks earlier runs inside max_age_h and finds the one
    # that did the work. Grading the latest run alone would page on a healthy day.
    {"name": "hedgelab (noon hedge check)", "repo": "hedgelab",
     "probe": "gh_run", "workflow": "daily.yml", "max_age_h": 72,
     "log_grep": r"results/daily/{weekday}\.json",  # weekday-only: Sun/Mon see Friday's
     # AWS one-clock-hedgelab-daily (12:10 ET) is the primary since 27 Sep 2026.
     "expect_event": "workflow_dispatch"},
    # Rebuilt 2026-08-24 to read NUTS's /evaluate instead of its own drifted
    # tree (it had been reporting BIL while NUTS was TQQQ). It is now SILENT
    # unless the holding changed, so silence in Telegram is indistinguishable
    # from death — this probe is the ONLY liveness signal, which is precisely
    # why the bot has no daily heartbeat message.
    #
    # The marker matches BOTH shapes on purpose: a quiet run prints
    # "NUTS-SIGNAL OK unchanged=…", a change prints "NUTS-SIGNAL CHANGED …".
    # One `|` covering both, never two patterns that can't both hold (the
    # reddit-scraper lesson). Neither line can print unless the NUTS fetch
    # succeeded AND its RSI unit test passed — main.py exits 1 first otherwise
    # — so this cannot go green on stale or untrusted numbers. Conclusion-only
    # (what this entry was until now) would have stayed green through the
    # entire BIL-vs-TQQQ divergence.
    {"name": "trading-algorithm- (30-min signal)", "repo": "trading-algorithm-",
     "probe": "gh_run", "workflow": "trading_alert.yml", "max_age_h": 72,
     # date={weekday} (red team 2026-09-27): with max_age_h 72 alone, a job that
     # died at Tuesday's open still read green Wed + Thu off Monday's last run.
     # main.py stamps date= on both lines; weekday-only, so {weekday} not {date}.
     "log_grep": r"NUTS-SIGNAL (?:OK unchanged=|CHANGED ).*date={weekday}",
     "rescue_max_failures": 2,          # 3+ of the last 5 half-hourly runs red = broken
     "expect_event": "workflow_dispatch"},
    # reddit-scraper's commit step is `git commit … || exit 0`, so a run that
    # scrapes nothing still exits GREEN having written nothing — conclusion-only
    # was blind to it. The workflow has TWO legitimate shapes (03:00 scrape,
    # 09:00 retry that dedupes itself), so each marker is an either/or:
    #   1. the guard step actually PRINTED its decision, and
    #   2. the push landed at least one data file (or the run was the legitimate
    #      no-op). It used to grep "Google News Aggregator", a banner printed when
    #      that scraper STARTS -- proof of nothing (producer-side red team 2026-09-12).
    # `[^$\n]+` is load-bearing: Actions echoes the step's source in the log, so
    # a plain "already updated today" would match `echo "…($LAST)…"` on every
    # run and never fail. Excluding `$` keeps the echoed source out.
    # reddit-backup (added 2026-09-26): reddit_backup.yml refills lists the Mac
    # left stale (RSS, 6 per run, every 3 h). With nothing stale it still reads
    # one list and prints "GITHUB REDDIT CHECK: rss ok", so a backup that stopped
    # reaching Reddit from GitHub turns red BEFORE the day the Mac dies. Listed
    # before the daily-data row on purpose: same repo, notion_health keeps the
    # LAST result per repo, and Telegram carries both.
    {"name": "reddit-backup (GitHub RSS every 3 h)", "feeds_screen": True, "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "reddit_backup.yml", "max_age_h": 8,
     "rescue_max_failures": 1,
     "log_grep": [r"REDDIT BACKUP: refreshed \d+ of \d+",
                  r"GITHUB REDDIT CHECK: rss ok|REDDIT BACKUP: refreshed [1-9]"]},
    {"name": "reddit-scraper (daily data)", "feeds_screen": True, "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "daily_scrape.yml", "max_age_h": 36,
     "log_grep": [r"last data/ commit: [^$\n]+ — proceeding"
                  r"|data/ already updated today \([^$\n]+\) — skipping",
                  r"DATA PUSHED: [1-9]\d* data files"
                  r"|data/ already updated today \([^$\n]+\) — skipping",
                  # 2026-09-27: Google News used to swallow every error and exit 0
                  # while Ritholtz/Trung still pushed (26 Sep). It now prints this.
                  r"GOOGLE NEWS: saved [1-9]\d* articles"
                  r"|data/ already updated today \([^$\n]+\) — skipping"]},
    # reddit-browser (added 2026-09-26): Reddit blocks GitHub's IPs, so the top
    # lists now come from launchd com.jalal.reddit-browser (07:35 + 19:35), which
    # drives a headless Chromium on the Mac and pushes the CSVs. The daily_scrape
    # row above stays green through a dead Mac job, because GitHub's RSS backup
    # refills stale lists (with ranks instead of upvotes). Hence two rows:
    #   1. the run itself: every run opens "== YYYY-MM-DD HH:MM:SS start"; a good
    #      one ends "REDDIT PUSHED: N files" or, when Reddit had nothing new,
    #      "NO REDDIT CHANGES (scraper exit 0)". A scrape that saved nothing says
    #      "(scraper exit 1)" and fails the grep. 26 h = two missed runs.
    #      reddit-scraper tests/test_robustness.py pins these exact patterns.
    #   2. the data, on the LIVE site: /api/status lists, per list, when the Mac
    #      last REACHED its real page (`checked`; saved or, if too short to save,
    #      not). The OLDEST is graded, so one list the Mac keeps failing to reach
    #      turns it red while the rest refresh; a quiet sub (r/lifehacks, 7 posts
    #      in Sep 2026) doesn't. max_age_h 36 = real hours (_parse_stamp converts
    #      these UTC "…Z" stamps since 2026-09-26): graded at 05:00/06:30, two
    #      missed 12-hourly runs read ~33-35 h (green), three ~45-47 h (red).
    #      Not graded: `reddit_missing` (lists never saved).
    # repo None on both: notion_health stamps one row per repo, last result wins.
    {"name": "reddit-browser (Mac 07:35/19:35 Reddit lists)", "feeds_screen": True, "screen_repo": "reddit-scraper", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/reddit-browser.log",
     "block_re": r"^== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) start",
     # "(scraper exit 0)" on BOTH arms (round 8): reddit-browser.sh prints
     # REDDIT PUSHED with the scraper's real exit code, and a failed scrape still
     # pushes the one reddit_browser.json it touched ("checked" stamps).
     "log_grep": r"REDDIT PUSHED: [1-9]\d* files in \w+ \(scraper exit 0\)|NO REDDIT CHANGES \(scraper exit 0\)",
     "max_age_h": 26},
    {"name": "reddit-browser (live site: every Reddit list fresh)", "repo": None,
     "probe": "web_fresh", "url": "https://reddit-scraper-lyart.vercel.app/api/status",
     "rows_key": "reddit_lists", "json_key": "checked", "max_age_h": 36},
    #   3. the backups (added 2026-09-26): each Mac run ends with
    #      "METHOD CHECK: page ok · json ok · rss ok" only if the page method saved
    #      at least half the lists AND both backups (Reddit's JSON and RSS through
    #      the same browser) just fetched a real list. Rows 1-2 stay green while the
    #      Mac quietly runs on a backup; this one doesn't. reddit-scraper
    #      tests/test_reddit_backups.py pins the exact line.
    {"name": "reddit-browser (backup methods: page, json, rss all work)", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/reddit-browser.log",
     "block_re": r"^== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) start",
     "log_grep": r"METHOD CHECK: page ok · json ok · rss ok",
     "max_age_h": 26},
    # financial-telegram-bot is the owner's most important repo and was entirely
    # unrostered. Its own health-check Telegrams on warn/critical, but nothing
    # watched whether that health check still RUNS — a monitor that dies is
    # indistinguishable from a healthy fleet. Roster the monitor itself.
    # 2026-09-12: SPLIT INTO TWO ROWS, because "the report was delivered" and "the
    # backstop still works" are different facts and used to be conflated into one
    # green row that proved neither. See probe_cloudwatch_marker's docstring.
    {"name": "financial-telegram-bot (report DELIVERED)", "repo": "financial-telegram-bot",
     "probe": "cloudwatch_marker",
     "log_group": "/aws/lambda/financial-telegram-report", "max_age_h": 30,
     "log_grep": [r"REPORT_DELIVERED ok=true",
                  r"Report sent at {today} \d\d:\d\d:\d\d"]},
    # REPORT_DELIVERED means "stored for the digest card", not "reached Telegram"
    # (red team 2026-09-12). health-hub stamps a sender only after Telegram accepts
    # a card carrying it. At 05:00 this grades YESTERDAY's card (sent 06:50-10:00 ET,
    # so 19-22 h old); 30 h covers a late card and the 25-hour DST day.
    {"name": "financial-telegram-bot (report reached Telegram in the digest card)", "repo": "financial-telegram-bot",
     "probe": "web_fresh", "url": "https://jalal-health.vercel.app/api/health",
     "json_key": "digest_report_at", "max_age_h": 30},
    # Liveness-only ON PURPOSE, and this is the declared reason the lint wants:
    # this workflow's healthy output is "I did nothing, the Lambda had it". There
    # is no success marker to assert, and asserting the skip-line would go RED on
    # the one day the backstop actually saves the report. Delivery is proven by
    # the CloudWatch row above; this row only answers "is the safety net loaded".
    {"name": "financial-telegram-bot (backstop workflow alive)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "daily_report.yml", "max_age_h": 36,
     "weak_ok": "backstop: healthy output is a no-op; delivery proven in CloudWatch"},
    # Marker added 2026-09-12. "Overall: ok" is printed only after all 17 endpoint
    # checks pass; a green run where an endpoint was unhealthy prints "Overall:
    # degraded" and now fails the row instead of passing it.
    {"name": "financial-telegram-bot (self-health monitor)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "health-check.yml", "max_age_h": 36,
     "log_grep": r"Overall: ok",
     "expect_event": "workflow_dispatch"},
    # The BACKUP needs two probes because neither failure mode implies the other.
    # `launchd_exit` alone was a probe that could essentially never fail: the script
    # ends with `tail … && mv`, so it used to report mv's status and discard rsync's
    # (fixed 2026-08-06 to `exit $rc`), AND it deliberately `exit 0`s when the T7 is
    # unmounted — so an unplugged drive read "last exit 0 ✅" every morning while
    # nothing was backed up. mtime catches "not running / drive gone"; exit status
    # catches "ran but rsync errored". A silently dead backup is the worst class of
    # failure here: it is only discovered when you need to restore.
    # 2026-09-12: these were TWO weak rows — file_mtime ("the log was touched")
    # and launchd_exit ("rsync exited 0"). Neither could see the failure that
    # actually matters: if the Google-Drive source does not mount, rsync walks an
    # empty tree, copies nothing, touches the log, and exits 0. Both rows stayed
    # green. probe_rsync_log grades the final run block instead and asserts a
    # non-zero file count, so an unmounted source now FAILS.
    {"name": "T7 Google-Drive backup (real tree copied)", "repo": None,
     # 3 Oct 2026: reads the script's MIRROR in ~/.local/state, not the T7 copy.
     # Every brew python upgrade is a new binary path, macOS re-asks the Removable
     # Volumes permission, and the read hung on a prompt nobody could answer remotely
     # while the backup itself was fine. Same run blocks, written by the backup script.
     # An unmounted T7 still fails: the script exits before writing a fresh block.
     "probe": "rsync_log", "log_path": "~/.local/state/t7-sync/sync.log",
     # Live reg-count has sat at 26,968-26,975 across every run in the log.
     # 20,000 is a ~26% floor: deleting a large folder must not page, but an
     # unmounted source (reg: absent) or a half-mounted one cannot reach it.
     # Raised from the old `>= 1` floor, which a red team proved was a false
     # green -- rsync prints `Number of files: 1 (dir: 1)`, exit 0, on an
     # empty tree whose mount point still exists.
     "min_files": 20000,
     "max_age_h": 36,
     "weak_ok": None},
    {"name": "zinger-bot (Telegram bot on Vercel)", "repo": "zinger-bot",
     "probe": "web_200", "url": "https://zinger-bot.vercel.app",
     "weak_ok": "root serves even when the handler is dead; the telegram_webhook row is the real check"},
    # The web_200 above probes the ROOT, which a dead handler still serves — a
    # gap noted in AGENTS.md and closed here 2026-08-25. Same two-independent-
    # deaths reasoning as voices-bot: the function can stop serving, OR Telegram
    # can stop pointing at it, and neither probe sees the other's failure.
    {"name": "zinger-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "ZINGER_BOT_TOKEN",
     "expect_url": "https://zinger-bot.vercel.app/api/webhook"},
    {"name": "zinger-bot (synthetic message gets a real reply)", "repo": None,
     "probe": "bot_selftest", "url": "https://zinger-bot.vercel.app/api/webhook", "secret_env": "ZINGER_WEBHOOK_SECRET"},
    {"name": "aoife-math (daily game site)", "repo": "aoife-math",
     "probe": "web_render", "url": "https://aoife-math.vercel.app",
     "expect_text": "Try 1 of 2",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    # nafis-mortgage (site) row DELETED 2026-10-03: Jalal no longer uses it ("a one
    # time thing"); its GitHub repo no longer exists. Code stays on the Mac.
    # Rostered 2026-08-25 during a coverage audit: aoife-math/columns/frameworks
    # were watched while these three equally-live sisters were not — coverage by
    # accident of when each was built, not by risk. aoife-puzzles matters most:
    # it is the WISC-V prep game, and a broken level reads to Jalal as a real
    # weakness in Aoife rather than a bug (see feedback-puzzle-validity-sacred).
    {"name": "aoife-puzzles (site)", "repo": "aoife-puzzles",
     "probe": "web_render", "url": "https://aoife-puzzles.vercel.app",
     "expect_text": "Aoife Puzzles",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    # aoife-columns / frameworks / order / algebra — ROWS RETIRED 2026-09-27: Aoife
    # stopped using them; repos archived on GitHub, Vercel projects removed.
    # backbench — ROW RETIRED 2026-09-12. It read `web_200` on backbench.vercel.app
    # and reported "daily trading brief: OK" because the static site answers. There
    # is no GitHub repo (404), no launchd job, and the local folder was retired the
    # same day. No brief has been sent. A green row for a job that does not exist is
    # worse than no row: it spends your trust. If backbench is ever revived, roster
    # it on the marker the brief PRINTS when it sends, not on the site responding.
    # Added 2026-08-17 alongside the Aoife's Planner rebuild. live_since is a
    # deliberate grace period: /api/plan-get (the plan-half endpoint the
    # backup script fetches) only goes live 2026-08-18, so the script's plan
    # half legitimately FAILs — and prints no OK marker — every night before
    # that (confirmed manually 2026-08-17: schedule half wrote a valid JSON
    # snapshot, plan half FAILed cleanly with no bogus file, script exited
    # nonzero). Alerting on that known gap would page for nothing; see
    # probe_planner_backup's docstring for the two independent checks.
    {"name": "aoife-planner-backup (nightly KV snapshot)", "repo": "aoifes-schedule",
     "probe": "planner_backup",
     "dest_dir": "~/Library/CloudStorage/GoogleDrive-jalal.chowdhury@gmail.com/My Drive/Aoife Planner Backups",
     "names": ["schedule", "plan"],
     "log_path": "~/Library/Logs/aoife-planner-backup.log",
     "live_since": "2026-08-18"},
    # Added 2026-08-18 with the aoife-school-bot deploy (launchd
    # com.jalal.aoife-school-bot-tick, scripts/tick.sh, every 30 min
    # 07:00–21:30 ET — the fleet's one deliberately daytime job).
    # A today-or-YESTERDAY window, not a max_age_h: this check runs at 5:00 AM
    # and the tick's first slot of the day is 07:00, so the freshest marker is
    # ALWAYS yesterday's 21:30 line. Grading age would be grading how long ago
    # 9:30 PM was.
    # `TICK OK` specifically, NEVER a bare `TICK`: a tick that cannot reach the
    # planner still answers HTTP 200 (by design — a 500 would strand it until
    # the next slot) and writes `TICK FAIL <date> planner-unreachable`, which
    # is exactly the silent-bot failure this probe exists to catch. See the
    # bot repo's AGENTS.md §5.
    # Trigger Board (added 2026-09-09): 07:00 + 18:00 launchd com.jalal.trigger-board
    # → ~/concierge/triggers/board/run.sh. run.sh prints `BOARD OK <slot> <date>`
    # only on exit 0; the 05:00 check sees yesterday's evening marker via {date}.
    # errors=0 (round 8): board.py used to turn a failed gym/meds/downloads fetch
    # into "trigger not firing" -- the card said "nothing to do" and BOARD OK was
    # written. run.sh now appends errors=<n> (FETCH ERRORS count); only 0 is green.
    {"name": "trigger-board (07:00 + 18:00 must-do nag)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/trigger-board.log",
     "log_grep": r"BOARD OK \w+ {date} errors=0\b",
     "live_since": "2026-09-10"},
    # job-reaper (added 2026-09-15): launchd com.jalal.job-reaper runs every 5 min
    # → ~/.local/bin/job-reaper.py, which stops scheduled jobs hung past their cap
    # (one hung run blocks every later slot of that job). Each full pass ends with
    # `JOB-REAPER OK watched=N running=N reaped=N`. block_re skips manual `dry-run`
    # passes, and max_age_h=1 turns "the reaper itself stopped firing" red.
    {"name": "job-reaper (5-min hung-job stopper)", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/job-reaper.log",
     "block_re": r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) JOB-REAPER OK watched=\d+ running=\d+ reaped=\d+$",
     "log_grep": r"JOB-REAPER OK",
     "max_age_h": 1},
    {"name": "aoife-school-bot (30-min Telegram tick)", "repo": "aoife-school-bot",
     "probe": "log_marker",
     "log_path": "~/Library/Logs/aoife-school-bot-tick.log",
     "log_grep": r"TICK OK {date}",
     "live_since": "2026-08-19"},
    # The tick above proves the OUTBOUND half (the Mac pushing previews). It says
    # nothing about the INBOUND half: Jalal replying to the bot. Those die
    # independently — the tick keeps writing TICK OK while an unhooked bot
    # silently swallows every reply. Rostered 2026-08-25.
    # repo: None on purpose, so a healthy sibling row cannot paint over it in
    # Notion (last-row-wins trap).
    {"name": "aoife-school-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "SCHOOL_BOT_TOKEN",
     "expect_url": "https://aoife-school-bot.vercel.app/api/webhook",
     "require_guard": True},
    {"name": "aoife-school-bot (synthetic preview gets a real reply)", "repo": None,
     "probe": "bot_selftest", "url": "https://aoife-school-bot.vercel.app/api/webhook", "secret_env": "SCHOOL_WEBHOOK_SECRET"},
    # aoife-milestones-bot had NO probe of any kind before 2026-08-25 — the only
    # live service in the fleet that was entirely unwatched. It is voice-driven
    # and write-through (voice → draft → ✓ → Sheet → Doc + Notion), so a deaf bot
    # loses milestones Jalal believes were recorded; the Sheet is master and it
    # simply stops gaining rows, which looks exactly like a quiet week.
    {"name": "aoife-milestones-bot (function serving)", "repo": "aoife-milestones-bot",
     "probe": "web_200", "url": "https://aoife-milestones-bot.vercel.app/api/webhook",
     "weak_ok": "paired with the telegram_webhook row; two independent deaths"},
    {"name": "aoife-milestones-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "MILESTONES_BOT_TOKEN",
     "expect_url": "https://aoife-milestones-bot.vercel.app/api/webhook",
     "require_guard": True},
    {"name": "aoife-milestones-bot (synthetic /recent gets a real reply)", "repo": None,
     "probe": "bot_selftest", "url": "https://aoife-milestones-bot.vercel.app/api/webhook", "secret_env": "MILESTONES_WEBHOOK_SECRET"},
    # notebooklm-drip — RETIRED 2026-09-12. The drip finished its job: the 41
    # notebooks are populated and Jalal does not need it nightly any more, but
    # wants it available later. com.jalal.notebooklm-drip is unloaded and its
    # plist parked in ~/Library/LaunchAgents/_disabled-2026-09-12/. This row is
    # kept commented rather than deleted so reviving the job is one un-comment
    # here plus `launchctl bootstrap` on the parked plist — a deleted row would
    # mean a revived job silently runs unwatched.
    # {"name": "notebooklm-drip (nightly Gemini Notebook drip)", "repo": None,
    #  "probe": "log_marker",
    #  "log_path": "~/PycharmProjects/notebooklm-library/drip.log",
    #  "log_grep": r"=== drip {date}[\s\S]*?FINISHED types_still_open="},
    # Added 2026-08-18 with the Google Calendar sync (launchd
    # com.jalal.aoife-gcal-sync, scripts/gcal-sync/run.sh, 04:10 daily — after
    # the 03:40 planner backup, before this 05:00 check, so TODAY's marker is
    # the one that should be there and the {date} alternation is just slack).
    # `GCAL-SYNC OK` specifically, NEVER a bare `GCAL-SYNC`: the job prints
    # `GCAL-SYNC WAITING <calendar-api-disabled|calendar-not-shared-yet|
    # write-permission>` — one per owner setup step — and EXITS 0 while any of
    # them is pending, so exit code and file mtime both say "healthy" for a sync
    # that has never published a single event. Those WAITING states are exactly
    # what live_since covers: not an error tonight, but if it is still WAITING
    # on 2026-08-20 the owner needs telling, and this probe is what tells him.
    # All three cleared on 2026-08-18 and the first real sync published 12
    # events that evening, so this could have been tightened to 08-19 — it is
    # NOT, deliberately: that evening's manual kickstart wrote an OK marker
    # dated 08-18 into this very log, and the probe's today|yesterday window
    # would let it satisfy an 08-19 check even if the 04:10 run had failed.
    # Starting at 08-20 means the first graded morning can only be satisfied by
    # a marker the scheduled job actually produced.
    # repo is None ON PURPOSE: aoifes-schedule already owns the
    # aoife-planner-backup row, and notion_health.py stamps one row per repo
    # with the LAST result winning — a green backup would paint over a red
    # calendar sync (the same trap documented on dhaka-hotels). Telegram
    # carries both entries independently.
    {"name": "aoife-gcal-sync (nightly Google Calendar publish)", "feeds_screen": True, "screen_repo": "aoife-calendar", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/aoife-gcal-sync.log",
     "log_grep": r"GCAL-SYNC OK {today}",          # runs 04:10, before the check
     "live_since": "2026-08-20"},
    # Added 2026-08-28 after GitHub's cron dropped mental-models entirely that
    # morning (schedule is best-effort; observed +31m/+34m/+11h14m/never).
    # launchd com.jalal.mental-models-backstop, 6:00 AM: if
    # results/daily/$TODAY.json is absent from the repo, dispatch daily.yml.
    # NOTE the workflow's skip-guard is `if: github.event_name == 'schedule'`
    # ONLY — a dispatch bypasses it, so the script's artifact check IS the
    # dedupe guard (the guard still stops a LATE cron arriving after a backstop
    # dispatch). Runs AFTER this 05:00 check, so the {date} today|yesterday window
    # is what grades it: yesterday's marker at 5 AM proves the backstop is
    # alive; a dead backstop surfaces the following morning, the fleet-wide
    # buffer. OK|DISPATCHED both count — either way the backstop RAN; ERROR
    # lines deliberately don't match. This row watches the BACKSTOP; the
    # mental-models gh_run row above still watches the primary path, so a
    # backstop-only week still shows up there as late/odd runs.
    # repo is None: mental-models already owns the gh_run row and
    # notion_health.py's last-result-wins would let this green paint over a
    # red primary.
    {"name": "mental-models-backstop (6 AM cron-miss dispatcher)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/mental-models-backstop.log",
     "log_grep": r"MM-BACKSTOP (OK|DISPATCHED) date={date}",
     # 08-30, not 08-29: today's markers came from manual test runs; the first
     # scheduled 6 AM run is 08-29, so 08-30 is the first morning where only a
     # scheduled marker can satisfy the window (the gcal-sync lesson above).
     "live_since": "2026-08-30"},
    # Added 2026-08-24 with the daily-trackers deploy (launchd
    # com.jalal.daily-trackers, run_daily.sh, 3:30 AM + 4:30/5:30 AM retries —
    # widened from 4:30/5:30 to the fleet-standard 3-slot ladder the same
    # evening).
    # Appends five metric rows (Zillow Zestimate, Redfin estimate, MND 30-yr
    # mortgage rate, USD-CAD, USD-BDT) to the "Automa Data" Google Sheet that
    # the Daily Trackers spreadsheet reads live. `TRACKERS_OK 5/5`
    # specifically: a partial night prints `TRACKERS_FAIL [...]` instead
    # (per-metric isolation still writes what it can), and a bounds-rejected
    # value is a FAIL by design — a wrong number in that sheet is worse than a
    # missing one. The 3:30 marker exists well before the 5:00 check; if only
    # the 5:30 retry succeeds, the 6:30 slot sees it (the {date} window is
    # extra slack, not the mechanism).
    {"name": "daily-trackers (nightly Automa Data sheet update)", "repo": "daily-trackers",
     "probe": "log_marker",
     "log_path": "~/PycharmProjects/daily-trackers/cron.log",
     # {today}: slots 3:30/4:30 precede the check; a 5:30-only success is
     # re-graded green by the 6:30 retry slot.
     "log_grep": r"TRACKERS_OK 5/5 date={today}",
     "live_since": "2026-08-25"},
    # sheets-backup: nightly git snapshot of every sheet feeding the finance
    # dashboard + Automa Data (cron 06:10 UTC, ~3 h old at the 09:00 check; one
    # missed night reads ~27 h, so 24 catches it same-morning). The marker is
    # printed only after every fetch passed sanity_check, so a garbage/partial
    # night cannot paint the row green. Marker shape is anchored by a unit test
    # in sheets-backup — change both sides together.
    # 28, not 24 (DST replay 2026-09-12). The 06:10 UTC cron really lands 10:29-11:11
    # UTC, AFTER the 05:00 check, so the check always grades the previous run. At
    # 05:00 EST (10:00 UTC) that age is ~23.5 h, and one early run (before 10:00 UTC)
    # next to a normal one tips 24 h into a false red. Detection is not delayed: a
    # missed run is still red at the next morning's check either way.
    {"name": "sheets-backup (nightly sheets -> git)", "repo": "sheets-backup",
     "probe": "gh_run", "workflow": "backup.yml", "max_age_h": 28,
     # [1-9] (round 8): "0 owned tabs" is a run that backed up nothing.
     "log_grep": r"\dZ BACKUP OK: [1-9]\d* owned tabs, [1-9]\d* public sources",
     # AWS one-clock-sheets-backup (06:10 UTC) is the primary since 2 Oct 2026.
     "expect_event": "workflow_dispatch"},
    # ── ported off n8n 2026-08-24 ───────────────────────────────────────────
    # mental-models: cron 05:10 UTC (00:10 EST / 01:10 EDT), so by the 09:00 UTC
    # check a good night's run is ~4 h old and ONE missed night reads ~28 h.
    # 24 catches the first miss with ~20 h of slack over anything observed.
    #
    # The marker is printed ONLY when the brief was delivered AND the rotation
    # index advanced — `--dry-run` and `--no-telegram` deliberately cannot
    # print it. That matters here because dry_run is a workflow_dispatch input:
    # without that rule, running a manual test would paint the row ✅ while the
    # nightly cron was dead. index=A->B in the marker is the proof of real work;
    # a run that sent a message but did not advance is NOT a healthy night.
    {"name": "mental-models (nightly 3 models)", "repo": "mental-models",
     "probe": "gh_run", "workflow": "daily.yml", "max_age_h": 24,
     # date={date}, not an unpinned \d{4}-..: on 2026-08-28 GitHub's cron never
     # fired at all, yet the previous day's runs were still inside max_age_h, so
     # an any-date marker reported healthy while no brief had been sent.
     "log_grep": r"MENTAL-MODELS OK [1-3]/3 date={date} index=\d+->\d+",
     # Primary trigger is one-clock-mental-models (EventBridge 00:10 ET). The
     # Mac 6 AM backstop ALSO dispatches, so a workflow_dispatch alone does not
     # prove AWS fired — but a `schedule` run means BOTH AWS and the Mac missed
     # and GitHub's demoted cron covered. Pair with the MM-BACKSTOP marker above.
     "expect_event": "workflow_dispatch"},
    # voices-bot is a WEBHOOK, not a cron — there is no scheduled run to grade,
    # so "did it run" is the wrong question. The two ways it dies silently are
    # (a) the Vercel function stops serving and (b) Telegram stops pointing at
    # it. Neither probe catches the other's failure, so both are rostered:
    # a dead function still has a valid webhook registration, and an unhooked
    # bot still serves 200 on a GET.
    {"name": "voices-bot (function serving)", "repo": "voices-bot",
     "probe": "web_200", "url": "https://voices-bot.vercel.app/api/webhook",
     "weak_ok": "paired with the telegram_webhook row; two independent deaths"},
    # (b) — the one that actually happened, twice, on 2026-08-24.
    {"name": "voices-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "VOICES_BOT_TOKEN",
     "expect_url": "https://voices-bot.vercel.app/api/webhook"},
    {"name": "voices-bot (synthetic /nuts gets a real reply)", "repo": None,
     "probe": "bot_selftest", "url": "https://voices-bot.vercel.app/api/webhook", "secret_env": "VOICES_WEBHOOK_SECRET"},
    # NUTS — rostered 2026-08-25 after Jalal spotted it missing. It is the
    # highest-consequence thing in the fleet (it models a live ~$178k Composer
    # symphony) and was the ONLY unwatched link in a chain whose watched end was
    # reporting green: trading-algorithm- ✅ + voices-bot ✅ both sit downstream
    # of this endpoint, and both go quiet — not red — when it freezes.
    # Two probes because they fail independently, same reasoning as voices-bot:
    # the API is what the consumers actually read, while the site is what Jalal
    # reads. A dead Vercel frontend leaves the API serving perfectly, and a
    # frozen API leaves the frontend rendering a stale page at HTTP 200.
    {"name": "NUTS (trading signal API)", "feeds_screen": True, "screen_side": True, "repo": "NUTS",
     "probe": "nuts",
     "url": "https://ju9t7h8903.execute-api.us-east-1.amazonaws.com/evaluate"},
    # nuts-sooty, NOT nuts.vercel.app — that is an unrelated old app that would
    # serve a cheerful 200 forever (see reference-nuts-algo).
    {"name": "NUTS (visualizer site)", "repo": None,
     "probe": "web_200", "url": "https://nuts-sooty.vercel.app",
     "weak_ok": "site only; the SIGNAL is graded by the nuts probe row"},
    # nuts-radar's risk is not uptime, it is a stale copy of NUTS's tree shape
    # silently reporting the wrong consequences — so this runs the repo's own
    # selfcheck.js against live /evaluate. See probe_nuts_radar.
    {"name": "nuts-radar (catalyst board)", "feeds_screen": True, "screen_side": True, "repo": "nuts-radar",
     "probe": "nuts_radar", "url": "https://nuts-radar.vercel.app",
     "repo_dir": "~/PycharmProjects/nuts-radar",
     "catalysts_url": "https://nuts-radar.vercel.app/catalysts.json"},
    # health-hub (Jalal's health app, 2026-08-26): the tick loop IS the product —
    # med nags, eating-window alerts and fridge prompts all ride on it, and a
    # web_200 would stay green with the scheduler dead. /api/health exposes
    # last_tick, stamped at the END of every tick pass, so this proves the loop
    # completed recently. Primary trigger: com.jalal.health-tick every 5 min;
    # backstop: hourly GH tick.yml. max_age_h=3 tolerates a dead Mac (backstop
    # hourly, GH cron up to ~90 min late) yet pages the morning both are gone.
    {"name": "health-hub (tick loop fresh)", "feeds_screen": True, "repo": "health-hub",
     "probe": "web_fresh", "url": "https://jalal-health.vercel.app/api/health",
     "json_key": "last_tick", "max_age_h": 3},
    # Same two-independent-deaths reasoning as the other webhook bots; repo None
    # so a healthy tick row can't paint over a deaf bot in Notion.
    # Round 8: last_tick stayed fresh while every tgSend failed (each caught and
    # only console-logged -- a blocked bot or wrong chat id reached nobody).
    # tick.js now records send outcomes; red when none succeeded in 30 h or the
    # newest attempt failed. 30 h: sends land most days from 06:50 (card) to the
    # 18:00 target message, so the longest normal gap is ~13 h.
    {"name": "health-hub (Telegram sends actually delivered)", "repo": None,
     "probe": "web_fresh", "url": "https://jalal-health.vercel.app/api/health",
     "json_key": "send_ok_at", "max_age_h": 30,
     "fail_key": "send_error_at", "fail_note_key": "send_error"},
    {"name": "health-hub (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "HEALTH_BOT_TOKEN",
     "expect_url": "https://jalal-health.vercel.app/api/telegram",
     "require_guard": True},
    {"name": "health-hub (synthetic /status gets a real reply)", "repo": None,
     "probe": "bot_selftest", "url": "https://jalal-health.vercel.app/api/telegram", "secret_env": "HEALTH_WEBHOOK_SECRET"},
    # The ALERTS bot (@TweetSyn_bot, TELEGRAM_TOKEN via the Dhaka flights .env)
    # owns the Silent-digest card buttons: a tap is a callback to /api/defensive.
    # 2026-09-19 its webhook came back EMPTY mid-morning (registered at the 06:50
    # card, gone by 08:45; no script on the Mac calls deleteWebhook) and every
    # button died silently. The tick re-registers it every 5 min; this row is
    # the alarm if that ever stops working.
    {"name": "alerts bot (digest-tap webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "TELEGRAM_TOKEN",
     "expect_url": "https://jalal-health.vercel.app/api/defensive",
     "require_guard": True},

    # ── 2026-09-12 coverage audit ───────────────────────────────────────────
    # A full inventory of every launchd job and cloud cron found ten live jobs
    # the roster had never watched. They were unwatched by accident of when
    # each was built, not by risk — the same finding as the 2026-08-25 audit.
    # The three daemons come FIRST because everything above them depends on
    # the Mac being awake and reachable.

    # keepawake is `caffeinate -s`. If it dies the Mac sleeps and EVERY
    # overnight row in this roster stops happening — the single highest-
    # leverage probe the fleet was missing. launchd_running, not launchd_exit:
    # see that function's docstring for why the exit column lies here.
    # toolcheck — rostered 2026-09-12. It was NOT redundant with fleet health, as
    # first suspected: fleet health grades the JOBS, toolcheck grades the TOOLS the
    # jobs are built on (24 CLI checks: arm64 brew, keys in the right .env, uv,
    # node, vercel). A broken tool surfaces through fleet health only later, as
    # whichever job happened to need it failing for a reason that reads unrelated.
    # Weekly (Sundays 04:40), hence log_block with an 8-day limit: one missed
    # Sunday reads ~8d and pages, a normal Saturday reads ~6d and does not.
    # "0 failed" is asserted, not just "it ran" — toolcheck's whole output is a
    # pass/fail count, so a run that found 20 broken tools must NOT read green.
    {"name": "toolcheck (weekly CLI toolbox physical)", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/toolcheck.log",
     "block_re": r"^===== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+\(exit \d+\) =====",
     "log_grep": r"[1-9]\d* passed, 0 failed",
     "max_age_h": 192},
    {"name": "keepawake (Mac stays awake for overnight jobs)", "repo": None,
     "probe": "launchd_running", "label": "com.jalal.keepawake"},
    # The always-on `claude remote-control` session. Its death is invisible
    # until Jalal tries to reach the Mac from his phone and cannot.
    {"name": "claude-concierge (remote session alive)", "repo": None,
     "probe": "launchd_running", "label": "com.jalal.claude-concierge"},
    # The blackout guard is RunAtLoad with NO interval — it fires on boot or
    # login, so on a week with no reboot it correctly never runs. Grading a
    # dated marker would therefore page on a perfectly healthy quiet week.
    # Assert only that it is still REGISTERED and last exited clean; that is
    # the whole failure mode worth catching (an unloaded guard is a silent
    # return to the blackout nights it exists to prevent).
    {"name": "overnight-blackout-check (guard registered)", "repo": None,
     "probe": "launchd_exit", "label": "com.jalal.overnight-blackout-check",
     "weak_ok": "RunAtLoad with no interval — a dated marker would page every no-reboot week"},

    # aoife-typing coach: every 15 min, and `--quiet` sends nothing to stdout,
    # which is why the launchd .out.log is empty and looked dead. The real log
    # is written by the script itself and carries an ISO stamp per run.
    {"name": "aoife-typing (15-min coach loop)", "repo": "aoife-typing",
     # log_tail, not "any line stamped today" (producer-side red team 2026-09-12):
     # coach.mjs stamps EVERY log() call, so "ERROR <stack>", "no state yet" (state
     # fetch broken) and "no APP_KEY" all matched as proof. It fires at :05/:20/
     # :35/:50 around the clock, so the newest line must be a FINISHED state: a
     # mission written, one already built, or a round still inside its hour.
     "probe": "log_tail", "path": "~/Library/Logs/aoife-typing-coach.log",
     "last_line": r"^\d{4}-\d\d-\d\dT[\d:.]+Z (?:wrote coach: |\d{4}-\d\d-\d\d: "
                  r"(?:mission already built$|last round \d+ min ago \S+ waiting for the hour$))",
     "max_age_h": 2,
     # A cycle logs up to ~14 min of free-model timeouts before `wrote coach`;
     # a probe inside that window must look past the retry lines (2026-09-19).
     "grace_min": 16,
     "feeds_screen": True},
    # ...and the screen side (2026-10-02): the mission the app SERVES must come
    # from her newest finished day. The row above stayed green for the three
    # weeks the screen served the 11 Sep mission (`at` vs `generatedAt`).
    {"name": "aoife-typing (screen serves the newest coach mission)", "repo": "aoife-typing",
     "probe": "freshness", "url": "https://aoife-typing.vercel.app/api/freshness",
     "expect_items": ["coach mission"]},
    # later-jar (2026-10-02): every tab reads the cached YNAB copy (jar:ynab:cache,
    # 5-min refresh). Red when the screen is stuck on a copy > 1 h old (YNAB down).
    {"name": "later-jar (screen serves a fresh YNAB copy)", "repo": "later-jar",
     "probe": "freshness", "url": "https://later-jar.vercel.app/api/freshness",
     "expect_items": ["ynab-jars"]},
    # aoife-puzzles (2026-10-02): position, rematch queue and avoid-list are all
    # recomputed from her raw sessions on each request; this proves the serving
    # helpers still see her newest session.
    {"name": "aoife-puzzles (screen sees her newest session)", "repo": "aoife-puzzles",
     "probe": "freshness", "url": "https://aoife-puzzles.vercel.app/api/freshness",
     "expect_items": ["position", "rematches", "avoidList"]},
    # moneymap-jalal (2026-10-02): the plan the site serves vs the plan files on the
    # Mac (stamped to KV every 30 min by com.jalal.moneymap-input-stamp).
    {"name": "moneymap-jalal (served plan = newest plan files)", "repo": None, "screen_repo": "moneymap-jalal",
     "probe": "freshness", "url": "https://moneymap-jalal.vercel.app/api/freshness",
     "expect_items": ["plan", "plan-input-stamp"]},
    # health-hub (2026-10-02): the morning digest card vs the newest sender item,
    # and the clean-streak chip recomputed each ET day. Rewrite -> /api/health?fresh=1.
    {"name": "health-hub (digest card + streak served fresh)", "repo": "health-hub",
     "probe": "freshness", "url": "https://jalal-health.vercel.app/api/freshness",
     "expect_items": ["digest-card", "clean-streak"]},
    # financial dashboard (2026-10-02): the Rubber Band card vs the newest NYSE close,
    # and the history sheet behind "What moved" / tap charts (2 runs/day).
    {"name": "financial-dashboard (rubber band + history served fresh)", "repo": "financial-telegram-bot",
     "probe": "freshness", "url": "https://financial-telegram-bot-beryl.vercel.app/api/freshness",
     "expect_items": ["rubber-band", "history-sheet"]},
    # Daily Reader (2026-10-02): every tab graded through get_data(), the page's own
    # path. The reddit-browser web_fresh row reads the Mac's `checked` stamp, which
    # stays fresh even when list_problem() rejects the new list and the old one shows.
    {"name": "reddit-scraper (live site: every tab serves fresh data)", "repo": "reddit-scraper",
     "probe": "freshness", "url": "https://reddit-scraper-lyart.vercel.app/api/freshness",
     "expect_items": ["reddit-monthly", "reddit-yearly", "news", "am-reads", "am-reads-check",
                      "satpost", "github-trending"]},
    # voices-bot (2026-10-02): the /status reply reads health.json (this repo's own
    # run). aoife-milestones-bot: Doc + Notion mirrors vs the newest Sheet row.
    {"name": "voices-bot (/status serves a fresh fleet file)", "repo": "voices-bot",
     "probe": "freshness", "url": "https://voices-bot.vercel.app/api/freshness",
     "expect_items": ["fleet-status"]},
    {"name": "aoife-milestones-bot (Doc/Notion/recap reflect the Sheet)", "repo": "aoife-milestones-bot",
     "probe": "freshness", "url": "https://aoife-milestones-bot.vercel.app/api/freshness",
     "expect_items": ["doc-mirror", "notion-mirror", "monthly-recap"]},
    # Tranche tab on nuts-radar (2026-10-03): the page reads an encrypted gist, so the
    # screen side is graded on the Mac. ~/.local/bin/tranche-fresh-check.py (launchd
    # com.jalal.tranche-fresh, :00/:30) compares the gist (updated_at + ciphertext
    # hash, never decrypted) with a content hash of TRANCHE-EXECUTION.md +
    # INSTRUMENTS.json. A STALE line, or no line for 1.5 h, is red.
    {"name": "nuts-radar Tranche tab (gist reflects the newest plan)", "repo": None,
     "screen_side": True, "screen_repo": "nuts-radar-tranche",
     "probe": "log_tail", "path": "~/Library/Logs/tranche-fresh.log",
     "last_line": r"^\S+ TRANCHE FRESH OK inputAgeH=", "max_age_h": 1.5},
    # Aoife's Google Calendar copy (2026-10-03): gcal-drift dry-runs the sync at
    # :15/:45 (read-only scope) and stamps pending changes. A daytime planner edit
    # clears in 30 min, a late one by 04:10 (~7 h), a hand edit on the calendar by
    # the next 04:10 (~24 h) -> 25 h grace.
    {"name": "aoife calendar (Google copy matches the planner)", "repo": None,
     "screen_side": True, "screen_repo": "aoife-calendar",
     "probe": "pending_stamp", "path": "~/.local/state/aoife-gcal-drift.json",
     "max_check_age_h": 1.25, "pending_grace_h": 25},
    # Work that lives only on this Mac (2026-10-03): on 2-3 Oct six repos had
    # commits never pushed (later-jar 8 days). quiet_red: shows in the morning card
    # but never buzzes -- a reminder, not an outage. Never pushes anything itself:
    # a push to main DEPLOYS several of these repos, so pushing is Jalal's call.
    {"name": "unpushed work (commits only on this Mac)", "repo": None,
     "probe": "unpushed_work", "quiet_red": True,
     "roots": ["~/PycharmProjects", "~/.local/bin", "~/concierge"], "max_age_h": 24,
     "ignore": {"nafis-mortgage": "retired one-off (3 Oct 2026); GitHub repo gone, kept on the Mac only"}},

    # financial-telegram-bot's two LOCAL launchd jobs. The two existing
    # financial-telegram-bot rows grade the cloud daily report and the
    # self-health monitor; neither sees these, on the highest-stakes
    # automation in the fleet.
    {"name": "defensive-nag (hourly risk prompt)", "repo": None,
     # log_tail (red team 2026-09-27): the {date} marker is a UTC date, so a nag
     # that died at 20:20 still passed ~33 h later. Runs hourly 08:20-22:20 local
     # into one log (stdout+stderr); at 05:00 the last write is ~6.7 h old and at
     # the 06:30 re-check ~8.2 h. 11 h, not 9 (round 8): the night clocks go back
     # adds a real hour, and 9 h false-redded -- LOUDLY -- that one morning a year.
     # Still catches a missed evening; a traceback ends the log on a non-indented,
     # non-marker line.
     "probe": "log_tail", "path": "~/Library/Logs/defensive-nag.log",
     # Round 8: every run now ENDS with "  nag outcome: sent=N failed=N pending=X"
     # (a failed reminder used to leave no trace). Only failed=0 is green.
     "last_line": r"^nag outcome: sent=\d+ failed=0 pending=\w+$", "max_age_h": 11},
    # rubber-band is weekdays 18:30 ONLY, so a Monday 05:00 check is looking at
    # Friday evening — ~58 h. A dated marker would page every Monday. mtime at
    # 72 h clears the weekend and still catches a genuine multi-day stall.
    # Was file_mtime with weak_ok "prints no success marker". Not true (red team
    # round 4): every run prints `published -> <gist raw URL>` after the gist
    # write succeeds. Graded on the newest run block only; 72h covers Fri 18:30
    # to the Monday 05:00 check.
    {"name": "rubber-band (weekday evening check)", "feeds_screen": True, "screen_repo": "financial-telegram-bot", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/rubber-band.log",
     "block_re": r"^rubber-band run (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)",
     # Round 8: `published` prints even when every Composer curve failed
     # (fetch_all_curves never throws; each leg logs "curve X: FAILED") and when
     # a colour-change alert logs "NOT sent". The wrapper's `=== exit 0 ===` also
     # covers the defensive-trigger evaluate that runs after the publish.
     "log_grep": [r"^\s*published → https://gist\.githubusercontent\.com/",
                  r"^=== exit 0 ===$"],
     "fail_grep": r"curve \S+: FAILED|alert \(\d+ changes\): NOT sent",
     "max_age_h": 72},

    # The tranche pair. 07:12 publish is the one Jalal would actually notice
    # missing. nag prints no date ("sent N chars" / "nothing due"), so it gets
    # mtime; publish stamps every line and gets the stronger dated marker.
    # tranche-nag retired 2026-09-23 (Jalal: no Tranche alerts any more; plist → .plist.retired).
    {"name": "tranche-publish (07:12 board publish)", "feeds_screen": True, "screen_repo": "nuts-radar-tranche", "repo": None,
     # log_block, not log_marker (producer-side red team 2026-09-12): the
     # "tranche.json: N steps" line prints BEFORE the gist upload, so a failed
     # publish still left a matching marker, and "0 steps" matched too. The newest
     # run block must now show a non-empty feed AND the upload that followed it.
     "probe": "log_block", "log_path": "~/Library/Logs/tranche-publish.log",
     "block_re": r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) tranche\.json: \d+ steps",
     "log_grep": [r"tranche\.json: [1-9]\d* steps", r"^gist updated \(encrypted\):"],
     "max_age_h": 30},

    # aoife-reads was the one Aoife site with no uptime probe while its eight
    # siblings all had one.
    {"name": "aoife-reads (site)", "repo": "aoife-reads",
     "probe": "web_render", "url": "https://aoife-reads.vercel.app",
     "expect_text": "Find Your Reading Powers",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    # money-moves is deliberately NOT rostered: it has no reachable public URL.
    # money-moves.vercel.app 404s (no production alias assigned) and the
    # deployment URL 302s into Vercel SSO. There is nothing a probe could
    # asserted that would mean "Jalal can open his tax page". Fix the alias
    # first, then add a web_200 row here.

    # ── coverage audit 2026-09-27: live jobs that had NO row ────────────────
    # ynab-nag Lambda: since 26 Sep EVERY family budget nag (Later Jar,
    # NAG_MODE=app) is sent by this Lambda, every 5 min 10:00-23:55 ET via
    # EventBridge one-clock-ynab-nag -- not by a GitHub run, so no gh_run row
    # could see it. Each tick prints "[app-nag] <outcome>". A KV outage returns
    # "kv-error" on every tick and sends nothing while the Lambda stays green, so
    # the newest failure must not be newer than the newest good outcome. It has
    # no events "today" at 05:00, hence today_only=False over a 10 h window
    # (reaches back to ~19:00 the previous evening).
    {"name": "ynab-nag Lambda (family budget nags, every 5 min)", "repo": None,
     "probe": "cloudwatch_marker", "log_group": "/aws/lambda/ynab-nag",
     # active_window is the EventBridge cron itself, cron(0/5 14-23,0-3), in
     # UTC -- so it stays right across DST (10:00-23:55 EDT, 09:00-22:55 EST).
     "today_only": False, "max_age_h": 14, "active_window": ("14:00", "03:55"),
     "log_grep": r"\[app-nag\] (?:checked|before-first-slot|quiet|reminded|no-reminder|waiting|muted|sent-\d+)\s*$",
     "fail_grep": r"\[app-nag\] kv-error|Task timed out|\[ERROR\]|Budget brief failed|Traceback"},
    # dhaka-yearly (launchd 01:15): nightly.sh deploys only after the engine, the
    # rule check AND the page test pass, and undoes data.json otherwise -- so the
    # LIVE `updated_tz` moves only on a fully good night. 26 h: a run finishing
    # ~02:00 is ~3 h old at 05:00; one missed night reads ~27 h.
    # The run itself (round 8): the live site's updated_tz is also freshened by
    # manual daytime runs, and nightly.sh used to `|| true` a failed push and a
    # failed notify. Every run now ENDS with one line (engine/nightly_line.py);
    # manual runs append to the same dated file, so its last line is the latest
    # run. 01:15 launchd -> ~3-5 h old at 05:00/06:30.
    # One Clock dead-man (2026-09-27): every 5 min the Mac pushes a one-shot AWS
    # alarm 45 min ahead; if the Mac goes quiet the alarm fires a Telegram alert
    # from the cloud. This row catches the heartbeat job itself failing.
    {"name": "mac-heartbeat (One Clock dead-man switch)", "repo": "one-clock",
     "probe": "launchd_exit", "label": "com.jalal.mac-heartbeat",
     "weak_ok": "exit code only; the real check is the cloud alarm itself, which fires within 45 min of the beats stopping"},
    {"name": "dhaka-yearly (01:15 nightly run ended OK)", "feeds_screen": True, "screen_repo": "dhaka-yearly", "repo": None,
     "probe": "log_tail", "path": "~/PycharmProjects/dhaka-yearly/data/run-{today_ymd}.log",
     # NIGHTLY IDLE (2026-09-27): after the booked departure no year is left to
     # search (6-31 Jan 2027), so the run deliberately does nothing.
     "last_line": r"^(?:NIGHTLY OK plans=[1-9]\d* problems=0 pushed=[0-9a-f]{7} notify=(?:ok|none)"
                  r"|NIGHTLY IDLE: no year to search: .+)$",
     # 24, not 8 (3 Oct 2026): the dated filename + required last line already pin
     # this to TONIGHT's run; 8 h turned any off-schedule fleet run after ~09:45
     # into a false loud alert for a run that had ended OK.
     "max_age_h": 24},
    {"name": "dhaka-yearly (nightly trip plans, live site)", "screen_side": True, "screen_repo": "dhaka-yearly", "repo": None,
     "probe": "web_fresh", "url": "https://dhaka-yearly.vercel.app/data.json",
     "json_key": "updated_tz", "max_age_h": 26,
     # Between trips the run prints NIGHTLY IDLE and stops refreshing on purpose
     # (from ~6 Jan 2027 until the next year is watched): PAUSED, not red.
     "paused_if": {"path": "~/PycharmProjects/dhaka-yearly/data/run-{today_ymd}.log",
                   "last_line": r"^NIGHTLY IDLE: "}},
    # AM Reads (reddit-scraper am_reads.yml, 07:45 ET + two backup crons). Both
    # outcome lines are anchored on the log's own "…Z " timestamp: the echoed
    # workflow source also contains "NO CHANGE: AM Reads already current" (the
    # Actions echo trap), but there it follows `echo "`, never the stamp.
    {"name": "reddit-scraper (AM Reads morning list)", "feeds_screen": True, "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "am_reads.yml", "max_age_h": 30,
     # Round 8: "Fetching AM Reads post" prints BEFORE the fetch, so a fetch that
     # failed (returns []) still matched it, and the empty run then said NO CHANGE.
     # Both new markers print only after a real parse / a real CSV check.
     "log_grep": [r"\dZ\s+Extracted [1-9]\d* articles",
                  r"\dZ (?:Unchanged: data/ritholtz/articles\.csv already holds this post|Saved data/ritholtz/articles\.csv) \([1-9]\d* articles\)",
                  r"\dZ (?:NO CHANGE: AM Reads already current|AM READS PUSHED: [0-9a-f]{7})"]},
    # llm-balance-check (launchd 08:10) is the only warning before the paid
    # OpenRouter lane runs dry. A blank balance (API/key broken) prints
    # "openrouter=" with no number and fails the row. Runs after the 05:00
    # check, so {date}.
    {"name": "llm-balance-check (daily OpenRouter balance warning)", "repo": None,
     "probe": "log_marker", "log_path": "~/Library/Logs/llm-balance-check.log",
     # sent=none|200 (round 8): the script logs whether the warning actually went
     # out; sent=notoken / sent=000 / sent=4xx is a warning that never arrived.
     "log_grep": r"{date} \d\d:\d\d:\d\d openrouter=\d[\d.]* sent=(?:none|200)\b"},
]


def _cfg_line(item) -> str:
    """One-line probe config so a failure block is self-describing."""
    keys = ("probe", "repo", "workflow", "url", "path", "label",
            "json_key", "max_age_h", "log_grep", "dest_dir", "names",
            "log_path", "live_since", "token_env", "expect_url")
    def fmt(v):
        return " + ".join(v) if isinstance(v, list) else v
    return " · ".join(f"{k}={fmt(item[k])}"
                      for k in keys if item.get(k) is not None)


def _paused(item):
    """paused_if (2026-10-03): {"path": log, "last_line": regex}. When the job's own
    newest log line says it is deliberately idle, a screen row that would go red
    on old data reports PAUSED instead (dhaka-yearly between trips: the nightly run
    prints NIGHTLY IDLE and stops refreshing data.json on purpose). The writer's
    own row still grades the job; this only stops a false red on the screen side.
    Returns the detail string, or None when not paused."""
    cfg = item.get("paused_if")
    if not cfg:
        return None
    path = os.path.expanduser(cfg["path"].replace(
        "{today_ymd}", datetime.date.today().strftime("%Y%m%d")))
    try:
        lines = [ln.strip() for ln in _read(path).splitlines() if ln.strip()]
    except Exception:                       # noqa: BLE001 -- no log = not paused
        return None
    if lines and re.search(cfg["last_line"], lines[-1]):
        return f"PAUSED — job is idle on purpose: {lines[-1][:120]}"
    return None


def run_checks() -> list:
    # The lint runs HERE, not only in main(). A red team (2026-09-12) pointed out
    # that a programmatic caller doing `from fleet_health import run_checks` would
    # bypass main() entirely and execute an unwaived liveness-only row -- which is
    # the exact route by which a backbench-shaped false green re-enters the board.
    # The guard belongs on the door the rows actually walk through.
    bad = lint_roster()
    if bad:
        names = ", ".join(i["name"] for i in bad)
        raise SystemExit(f"roster lint failed (see lint_roster): {names}")
    results = []
    for item in FLEET:
        fn = PROBE_FNS[item["probe"]]
        err = ""
        paused = _paused(item)
        for attempt in range(1, 0 if paused else PROBE_ATTEMPTS + 1):
            try:
                ok, detail = fn(**item)
                break
            except Exception as e:           # noqa: BLE001 — infra error: retry
                err = f"probe error: {type(e).__name__}: {e}"[:300]
                if attempt < PROBE_ATTEMPTS:
                    print(f"  … {item['name']}: {err} (attempt {attempt}, retrying)")
                    time.sleep(PROBE_RETRY_PAUSE_S)
        else:                                # all attempts raised
            ok, detail = False, f"{err} (after {PROBE_ATTEMPTS} attempts)"
        if paused:
            ok, detail = True, paused
        results.append({"name": item["name"], "repo": item.get("repo"),
                        "probe": item["probe"], "ok": ok, "detail": detail,
                        "cfg": _cfg_line(item)})
        print(f"  {'✅' if ok else '❌'} {item['name']} — {detail.splitlines()[0]}")
    return results


# ── reporting ───────────────────────────────────────────────────────────────

def _is_retired(r) -> bool:
    """A launchd row the probe found deliberately retired (see _plist_state) —
    ok=True, but not a real "healthy" result: it still needs its roster row
    deleted, so the digest must not fold it into either the healthy count or
    a "recovered" banner."""
    return bool(r.get("ok")) and str(r.get("detail", "")).startswith("RETIRED — ")


def annotate_history(results) -> list:
    """Carry failing_since across days (before health.json is overwritten) and
    return the systems that failed last run but are healthy now.

    A row that flips from failing to RETIRED is not a recovery — the job was
    deliberately shut down, not fixed — so it is excluded here too.
    """
    try:
        prev = json.loads(_read(HEALTH_FILE))
    except Exception:                        # noqa: BLE001 — first run ever
        return []
    prev_date = prev.get("checked", "")[:10]
    prev_bad = {r["name"]: r.get("failing_since") or prev_date
                for r in prev.get("results", []) if not r.get("ok")}
    today = datetime.date.today().isoformat()
    for r in results:
        if not r["ok"]:
            r["failing_since"] = prev_bad.get(r["name"], today)
    return sorted(n.split(" (")[0] for n in prev_bad
                  if any(r["name"] == n and r["ok"] and not _is_retired(r)
                         for r in results))


def _size(lines) -> int:
    return sum(len(l) + 1 for l in lines)


def _failure_block(r, today, budget=None) -> list:
    """The lines for ONE failure. The name (and 'failing since') are the head
    and are never dropped; everything after it — config line, detail, log tail —
    is filled in until `budget` chars run out.

    Trimming has to happen HERE, per failure, not on the finished digest: a
    naive tail-chop of the whole message drops the LAST failure blocks and the
    healthy-systems line entirely, so a 4-repo outage reads as a 2-repo one.
    Losing a log tail costs a paste-into-Claude round trip; losing a failure
    name means the owner never learns that system is down.
    """
    head = [f"❌ {r['name']}"]
    since = r.get("failing_since")
    if since and since != today:
        head.append(f"   ⏳ failing since {since}")
    rest = [f"   [{r['cfg']}]"] + [f"   {dl}" for dl in r["detail"].splitlines()]
    if budget is None:
        return head + rest + [""]
    mark = "   …(detail trimmed — full text in health.json)"
    out, used = list(head), _size(head)
    for line in rest:
        if used + len(line) + 1 > budget:
            if used + len(mark) + 1 <= budget:   # the marker must fit too
                out.append(mark)
            break
        out.append(line)
        used += len(line) + 1
    return out + [""]


# A gh_run probe that never found a run at all — the workflow did not START,
# as opposed to starting and failing. Anchored to the exact string probe_gh_run
# emits for that case.
_NEVER_STARTED = re.compile(r"^no run in ")


def correlated_note(results) -> list:
    """Several workflows going stale AT ONCE is ONE event, not N separate bugs.

    On 2026-08-27 GitHub silently dropped ~9 h of scheduled events across every
    repo in the account. The digest reported that as three unrelated repo
    failures — three wrong debugging sessions — because each probe only ever
    sees its own system. Nothing here is wrong per probe; what was missing is
    the sentence that ties them together, so name the shape before the blocks.

    Only *never started* counts. A run that started and failed has a real,
    per-repo cause, and lumping those together would send the reader looking
    for a fleet outage that isn't there.
    """
    stale = [r for r in results
             if not r["ok"] and r.get("probe") == "gh_run"
             and _NEVER_STARTED.match(r["detail"])]
    if len(stale) < 2:
        return []
    repos = sorted({r["repo"] for r in stale if r.get("repo")})
    if len(repos) < 2:
        where = repos[0] if repos else "one repo"
        return [f"\u26a0\ufe0f {len(stale)} workflows in the same repo ({where}) never "
                f"started \u2014 ONE event, not {len(stale)} bugs. Debug them together.", ""]
    return [f"\u26a0\ufe0f {len(stale)} workflows across {len(repos)} repos never started "
            f"\u2014 ONE event, not {len(stale)} bugs. Suspect GitHub's cron dispatcher "
            f"before the repos: check the newest event=schedule run ACROSS all "
            f"repos first. If that is hours old it is GitHub, and recovery is "
            f"`gh workflow run <wf>` per missed workflow.", ""]


def format_digest(results, recovered=()) -> str:
    """One line when all healthy; full paste-to-Claude blocks when not.

    Every failing system is named no matter how many fail at once (a fleet-wide
    outage is exactly when the digest must not eat its own tail), and the
    "✅ the other N healthy" line always survives, so the message states the
    scope of the damage even when the detail had to be cut.

    Retired launchd rows (ok=True, detail starts "RETIRED — ", see
    _is_retired) are real signal too — a deleted-but-not-yet-removed roster
    row — but they are not failures and not "healthy" either: they are pulled
    out of both the healthy count and the "N of M FAILING" totals and listed
    in their own "🗂 retired" line so the nag to delete the row survives even
    on an otherwise all-clear day.
    """
    today = datetime.date.today().isoformat()
    retired = [r for r in results if _is_retired(r)]
    retired_names = {r["name"] for r in retired}
    counted = [r for r in results if r["name"] not in retired_names]
    bad = [r for r in counted if not r["ok"]]

    if not bad and not retired:
        text = f"✅ Fleet check {today} — all {len(counted)} systems healthy"
        if recovered:
            text += f" · recovered: {', '.join(recovered)}"
        return text

    def _retired_names() -> str:
        return ", ".join(r["name"].split(" (")[0] for r in retired)

    if not bad:
        # Nothing is actually broken — the only news is a retired row that
        # still needs its roster entry deleted. No 🚨: that emoji is reserved
        # for a real failure, or the nag trains Jalal to ignore the alarm.
        text = (f"🗂 Fleet check {today} — 0 of {len(counted)} FAILING · "
                f"{len(retired)} retired: {_retired_names()} — delete "
                f"{'this row' if len(retired) == 1 else 'these rows'}")
        if recovered:
            text += f" · recovered: {', '.join(recovered)}"
        return text

    header = f"🚨 FLEET CHECK {today}: {len(bad)} of {len(counted)} FAILING"
    if retired:
        header += f" · {len(retired)} retired"
    footer = []
    if recovered:
        footer.append(f"💚 recovered today: {', '.join(recovered)}")
    ok_full = [r["name"] for r in counted if r["ok"]]
    shorts = [n.split(" (")[0] for n in ok_full]
    # Keep the qualifier when one repo has several probes, otherwise two rows
    # collapse to "leasehackr-scraper, leasehackr-scraper" and read as a dupe.
    ok_names = [s if shorts.count(s) == 1 else full
                for s, full in zip(shorts, ok_full)]
    if ok_names:
        footer.append(f"✅ the other {len(ok_names)} healthy: {', '.join(ok_names)}")
    footer += ["", "Paste this whole message to Claude to debug "
                   "(fleet_health.py in github-notion-sync)."]

    blocks = [_failure_block(r, today) for r in bad]
    retired_block = []
    if retired:
        plural = "this row" if len(retired) == 1 else "these rows"
        retired_block = [f"🗂 retired ({len(retired)}): {_retired_names()} "
                          f"— delete {plural}", ""]
    # Header + footer (+ the retired line, which is never trimmed — it is one
    # short line) are reserved first; whatever is left is split evenly across
    # the failures, and blocks that come in under their share hand the slack
    # back to the big ones (usually a gh_run block carrying a log tail).
    note = correlated_note(results)
    room = TELEGRAM_LIMIT - len(header) - 1 - _size(note) - _size(footer) - _size(retired_block)
    if bad and sum(_size(b) for b in blocks) > room:
        share = max(0, room // len(bad))
        slack = sum(share - _size(b) for b in blocks if _size(b) < share)
        big = [i for i, b in enumerate(blocks) if _size(b) >= share]
        budget = share + (slack // len(big) if big else 0)
        blocks = [b if _size(b) < share else _failure_block(bad[i], today, budget)
                  for i, b in enumerate(blocks)]
    body = [l for b in blocks for l in b] + retired_block
    if _size(body) > room:
        # Pathological (dozens of failures at once): fall back to the roll call
        # of what is down. The names are the last thing to go, and the footer
        # is never touched. The retired line is the first thing to go — a
        # cleanup nag matters less than a live failure name.
        body = [f"❌ {r['name']}" for r in bad]
        while body and _size(body) > room:
            body.pop()
            if body:
                body[-1] = "…(+ more — full list in health.json)"
    return "\n".join([header, ""] + note + body + footer)


def digest_post(item_id, text, parse_mode="HTML", photo=None, caption=None) -> bool:
    """Hand an overnight message to the health-hub Silent digest instead of the chat
    (one 07:00 card, a button per item; a tap replays the full message — 11 Sep 2026).
    True = stored; False = caller sends to Telegram as before. Needs DIGEST_URL + DIGEST_KEY."""
    url, key = os.environ.get("DIGEST_URL"), os.environ.get("DIGEST_KEY")
    if not url or not key:
        return False
    body = json.dumps({"id": item_id, "text": text, "parse_mode": parse_mode,
                       "photo": photo, "caption": caption}).encode()
    req = urllib.request.Request(f"{url}?k={urllib.parse.quote(key)}", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode() or "{}").get("ok") is True
    except Exception as e:  # noqa: BLE001
        print(f"digest hand-off failed ({e}); sending directly")
        return False


def _telegram_send(text, silent=False) -> str:
    """Plain-text send (no parse_mode — log excerpts would break Markdown),
    3 attempts. silent=True = no buzz (the 05:00 digest; alerts stay loud).

    Returns "digest" (queued in health-hub's Silent digest — NOT yet in front
    of Jalal; the card itself goes out 06:50-07:30, see WINDOW in
    health-hub/lib/digest.js), "direct" (a real Telegram send — genuinely
    delivered), or "" on failure. already_ran_today() cares about this
    distinction (2026-09-15): a "digest" hand-off must not skip the 6:30 retry,
    or a problem that self-heals between 5:00 and 6:30 (e.g. the mental-models
    6 AM backstop) reaches Jalal's card still showing yesterday's — well,
    this morning's — stale red.
    """
    if silent and digest_post("fleet", text, ""):   # Silent digest card first (11 Sep 2026)
        print("handed to the Silent digest (health-hub)")
        return "digest"
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(no Telegram creds — digest not sent)")
        return ""
    for attempt in range(1, 4):
        try:
            body = json.dumps({"chat_id": chat, "text": text,
                               "disable_web_page_preview": True,
                               "disable_notification": bool(silent)}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"Telegram digest: HTTP {r.status}")
                return "direct"
        except Exception as e:               # noqa: BLE001
            print(f"WARN: Telegram send attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(15)
    return ""


LOUD_FROM = datetime.time(6, 25)   # the 05:00 run stays silent; 06:30 re-checks and buzzes


def loud_alert(results, now=None, prev=None):
    """One BUZZING message when systems are still red after the 6:30 re-check.

    Red team 2026-09-27: the digest goes to health-hub's Silent card, whose button
    reads "🛠 Fleet health" whether the fleet is green or on fire -- a red fleet
    was indistinguishable from a healthy one unless he tapped. Owner decision
    2026-09-27: reds are loud. Safety rules: never before LOUD_FROM (the 05:00
    run must not wake anyone; 06:30 re-checks first, so self-healed rows never
    buzz), and once per failing name per day (a manual re-run repeats nothing).
    Returns the record to store in health.json, or the previous one unchanged.
    """
    now = now or datetime.datetime.now()
    today = now.date().isoformat()
    if prev is None:
        try:
            prev = json.loads(_read(HEALTH_FILE)).get("loud") or {}
        except Exception:                    # noqa: BLE001
            prev = {}
    already = set(prev.get("names", [])) if prev.get("date") == today else set()
    # quiet_red rows (2026-10-03) show in the digest but never buzz.
    quiet = {i["name"] for i in FLEET if i.get("quiet_red")}
    bad = [r for r in results if not r.get("ok") and r["name"] not in quiet]
    new = [r for r in bad if r["name"] not in already]
    if not new or now.time() < LOUD_FROM:
        return prev if prev.get("date") == today else {}
    # Same denominator as format_digest: retired launchd rows are not systems.
    live = [r for r in results if not _is_retired(r)]
    # Round 8: a GitHub/AWS outage at 06:30 makes every gh_run/aws row raise 3x
    # and come back "probe error: ...". Those say the CHECKER could not look,
    # not that the system is down; lumping them in sent "20 of 20 failing" --
    # the message most likely to teach him to ignore the buzz.
    real = [r for r in bad if not str(r.get("detail", "")).startswith("probe error:")]
    blind = [r for r in bad if r not in real]
    lines = ([f"🚨 Fleet check: {len(real)} of {len(live)} systems failing"] if real else
             [f"⚠️ Fleet check could not look at {len(blind)} of {len(live)} systems"])
    for r in real[:12]:
        since = r.get("failing_since")
        lines.append(f"• {r['name']}" + (f" (since {since})" if since and since != today else ""))
    if len(real) > 12:
        lines.append(f"• …and {len(real) - 12} more")
    if blind:
        lines.append(f"{'Also c' if real else 'C'}ould not check {len(blind)} "
                     f"(GitHub/AWS/network trouble, not the systems themselves): "
                     + ", ".join(r["name"].split(" (")[0] for r in blind[:6])
                     + (" …" if len(blind) > 6 else ""))
    lines.append("Full detail: tap 🛠 Fleet health in the morning card, or paste this to Claude.")
    if _telegram_send("\n".join(lines)) != "direct":
        print("WARN: loud alert not delivered")
        return prev if prev.get("date") == today else {}
    print(f"loud alert sent for {len(new)} new failing system(s)")
    return {"date": today, "names": sorted(already | {r["name"] for r in bad})}


def publish(results, telegram_mode: str, loud=None) -> None:
    payload = {"checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               # "sent"/"failed": unchanged contract — notion_health.py's Dead-Mac
               # watchdog dies on anything but "sent". telegram_mode is the new,
               # separate signal already_ran_today() actually needs: "digest" vs
               # "direct" (see _telegram_send).
               "telegram": "sent" if telegram_mode else "failed",
               "telegram_mode": telegram_mode or None,
               "loud": loud or {},
               "results": results}
    with open(HEALTH_FILE, "w") as f:
        json.dump(payload, f, indent=1)
    def git(*args):
        return subprocess.run(["git", "-C", REPO_DIR] + list(args),
                              capture_output=True, text=True, timeout=60)
    git("add", "health.json")
    # "-- health.json" commits ONLY that file. A bare commit took whatever else
    # was staged: on 2026-09-26 a test run swept a session's staged roster edit
    # into "Fleet health: 15:08" and pushed it.
    c = git("commit", "-m", f"Fleet health: {payload['checked']}", "--", "health.json")
    if c.returncode == 0:
        p = git("push")
        print("health.json pushed" if p.returncode == 0
              else f"WARN: push failed: {p.stderr.strip()[:150]}")
    elif "nothing to commit" not in c.stdout:
        print(f"WARN: commit failed: {c.stderr.strip()[:150]}")


def already_ran_today() -> bool:
    """True if today's check completed AND actually reached Jalal.

    2026-09-15: this used to accept telegram=="sent", which a Silent-digest
    hand-off also sets — but a hand-off only QUEUES the item; the health-hub
    card isn't delivered until its 06:50-07:30 autoFlush window. Treating that
    as "delivered" let a 5:00 finding that self-heals by 6:30 (mental-models'
    6 AM backstop is exactly this) reach the card still showing the stale red,
    because the retry slot skipped instead of re-checking and overwriting it.
    Only a "direct" send (no digest configured, or its hand-off failed) is
    truly delivered already; "digest" must still fall through to a re-check.
    """
    try:
        h = json.loads(_read(HEALTH_FILE))
        today = datetime.date.today().isoformat()
        # A direct send at 05:00 is delivered, but it was SILENT: while rows are
        # still red and no loud alert went out today, the 6:30 slot must re-check.
        unbuzzed = (any(not r.get("ok") for r in h.get("results", []))
                    and (h.get("loud") or {}).get("date") != today)
        return (h.get("checked", "")[:10] == today
                and h.get("telegram_mode") == "direct" and not unbuzzed)
    except Exception:                        # noqa: BLE001
        return False


def _lock_is_fresh() -> bool:
    """A lock only backs the retry slot off while its writer is still ALIVE.

    Red team 2026-09-27: job-reaper SIGKILLs a hung 05:00 run at ~05:30, the
    finally-block never runs, and the orphaned lock (1.5 h old at 06:30, under
    LOCK_STALE_S) made the retry slot log "backing off" and exit 0 -- no digest
    that day, nothing loud until the cloud watchdog hours later. The pid inside
    the lock decides; age is only the fallback for an unreadable lock."""
    try:
        age = time.time() - os.path.getmtime(LOCK_FILE)
    except OSError:
        return False
    try:
        pid = int(_read(LOCK_FILE).strip())
    except (OSError, ValueError):
        return age < LOCK_STALE_S
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                         # writer is dead: the lock is an orphan
    except PermissionError:
        pass                                 # alive, just not ours to signal
    return age < LOCK_STALE_S


# ── roster lint ─────────────────────────────────────────────────────────────
# Probes that assert LIVENESS, not WORK. Each is fine in the right place and
# useless in the wrong one, and the wrong one is invisible: a green row reads
# identically either way.
#
# Why this exists (2026-09-12): the roster carried a row called "backbench
# (daily trading brief)" that probed web_200 on a static site. It reported OK
# every morning for weeks. There was no repo, no scheduled job, and no brief had
# ever been sent. Nothing in the file flagged it, because choosing a weak probe
# looked exactly like choosing a strong one.
#
# So a weak probe is now a DECISION SOMEONE HAD TO TYPE. Add `weak_ok` with the
# reason liveness is genuinely sufficient for that row, or the monitor refuses to
# start. This cannot catch a wrong reason — it can only make the choice visible,
# which is the whole failure mode it is aimed at.
LIVENESS_ONLY = {"web_200", "launchd_exit", "file_mtime"}


def lint_roster(fleet=None):
    """Every liveness-only row must say why liveness is enough. Returns offenders."""
    offenders = []
    for item in (FLEET if fleet is None else fleet):
        pats = item.get("log_grep") or []
        pats = [pats] if isinstance(pats, str) else list(pats)
        # Red team 2026-09-27: log_grep=[""] or "." passed the lint and matches
        # any log -- a marker that cannot miss is decoration.
        vacuous = any(re.search(re.sub(r"\{[a-z]+\}", "", p), probe_text)
                      for p in pats for probe_text in ("", "zz"))
        weak = (item["probe"] in LIVENESS_ONLY or vacuous
                or (item["probe"] == "gh_run" and not item.get("log_grep")))
        # Round 8: a token this probe does not expand (or a typo like {yesterday})
        # is searched for literally and can never match -- a permanent false red
        # at best, and no weak_ok excuses it.
        tokens = {t for p in pats for t in re.findall(r"\{[a-z]+\}", p)}
        if item["probe"] == "cloudwatch_marker" and item.get("today_only") is False:
            bad_tok = tokens               # _cloudwatch_last_outcome expands none
        else:
            bad_tok = tokens - set(DATE_TOKENS) if item["probe"] in TOKEN_PROBES else tokens
        if bad_tok:
            item = dict(item, lint_why=f"date token {sorted(bad_tok)} is never expanded "
                                       f"by probe {item['probe']!r}")
            offenders.append(item)
        elif weak and not item.get("weak_ok"):
            offenders.append(item)
        # 2026-10-02 (aoife-typing): a writer that feeds a screen needs a row that
        # reads the SCREEN side too -- a green writer proved nothing for 3 weeks.
        # A web_fresh/nuts row that fetches the SAME file the page renders may
        # stand in, marked screen_side=True (dhaka-flights data.json, 2026-10-02).
        # screen_repo: the app a row's writer feeds / its screen check belongs to,
        # when `repo` is None or a different repo (Notion stamping uses `repo`).
        elif item.get("feeds_screen") and not any(
                (r["probe"] == "freshness" or r.get("screen_side"))
                and (r.get("screen_repo") or r.get("repo"))
                == (item.get("screen_repo") or item.get("repo"))
                for r in (FLEET if fleet is None else fleet)):
            offenders.append(dict(item, lint_why="feeds_screen but no 'freshness'/screen_side row "
                                                 f"for {item.get('screen_repo') or item.get('repo')!r}"))
    return offenders


def main(argv=()) -> None:
    now = datetime.datetime.now()
    bad = lint_roster()
    if bad:
        print("=== ROSTER LINT FAILED — refusing to run ===")
        for item in bad:
            print(f"  {item['name']}: " + (item.get("lint_why") or
                  f"probe {item['probe']!r} only proves liveness (or its log_grep "
                  f"matches anything). Give it a real success marker, or add "
                  f'weak_ok="<why liveness is enough>".'))
        raise SystemExit(2)
    if "--retry-slot" in argv:
        if already_ran_today():
            print(f"=== {now:%Y-%m-%d %H:%M} retry slot: already ran today — skipping ===")
            return
        if _lock_is_fresh():
            print(f"=== {now:%Y-%m-%d %H:%M} retry slot: earlier run still in "
                  f"progress (lock) — backing off ===")
            return
    print(f"=== Fleet health check {now:%Y-%m-%d %H:%M} ===")
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    try:
        results = run_checks()
        recovered = annotate_history(results)
        text = format_digest(results, recovered)
        note = offhours_note(results, now)
        if note:
            text += "\n\n" + note
        mode = _telegram_send(text, silent=True)   # 05:00 digest — no buzz (11 Sep 2026)
        loud = loud_alert(results)
        publish(results, mode, loud)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    print("=== Done ===")


def cli(argv) -> int:
    """Run main() and turn EVERY failure into an alert. Returns the exit code:
    0 ok, 3 = failed AND already alerted (run_health.sh alerts on any other
    nonzero code, e.g. a SyntaxError before this function exists)."""
    try:
        main(argv)
        return 0
    except SystemExit as e:
        # Red team 2026-09-27: the roster lint exits via SystemExit(2), which
        # `except Exception` never sees -- a bad roster edit stopped every
        # morning check with no alert at all. A clean exit (0/None) stays quiet.
        if e.code in (0, None):
            return 0
        _telegram_send(f"🚨 fleet_health.py REFUSED TO RUN (exit {e.code}) — the "
                       "checker is down, not the fleet. Most likely the roster "
                       "lint: a liveness-only row without weak_ok. See "
                       "health.log. Paste this to Claude to debug.")
        return 3
    except Exception:                        # noqa: BLE001 — die LOUDLY
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        _telegram_send("🚨 fleet_health.py itself CRASHED — the checker is "
                       f"down, not the fleet:\n{tb[-1500:]}\n"
                       "Paste this to Claude to debug.")
        return 3


QUIET_FILE = os.path.join(REPO_DIR, "health-quiet.json")
# Scheduled off-hours runs (launchd com.jalal.fleet-quiet, 12:00 + 23:00) pass
# --slot and write one file per hour, so the 23:00 run cannot erase the noon one.
QUIET_SLOT_GLOB = os.path.join(REPO_DIR, "health-quiet-*.json")


def offhours_note(results, now=None, max_age_h=20) -> str:
    """One digest line naming rows that were RED at an off-hours quiet run but are
    GREEN in this morning run -- the signature of a check that only passes at certain
    hours (3 Oct 2026: dhaka-yearly's 8 h window paged a manual 10:24 run). Rows red
    now are already in the failure list, so they are not repeated here."""
    import glob
    now = now or datetime.datetime.now()
    green_now = {r["name"] for r in results if r.get("ok")}
    hits = {}
    for path in sorted(glob.glob(QUIET_SLOT_GLOB)):
        try:
            q = json.loads(_read(path))
            at = datetime.datetime.strptime(q["checked"], "%Y-%m-%d %H:%M")
        except Exception:                    # noqa: BLE001 — a bad file is no evidence
            continue
        if (now - at).total_seconds() > max_age_h * 3600:
            continue
        for r in q.get("results", []):
            if not r.get("ok") and r["name"] in green_now:
                hits.setdefault(r["name"].split(" (")[0], []).append(f"{at:%H:%M}")
    if not hits:
        return ""
    names = ", ".join(f"{n} (red at {'/'.join(t)})" for n, t in sorted(hits.items()))
    return ("⏰ Green now but RED at an off-hours check — the check may only pass at "
            f"certain hours: {names}"[:400] + ". Paste this to Claude.")


def quiet_main(argv=()) -> int:
    """`--quiet`: grade every row and print the verdicts, NOTHING else (3 Oct 2026).

    A manual daytime re-run used to go through main(): it overwrote the morning
    digest item, rewrote + pushed health.json and sent a loud 🚨 to the phone --
    on 3 Oct a 10:24 test run paged Jalal for a row that was only red because of
    the hour. This path never calls _telegram_send, publish, loud_alert or the
    lock, and writes its verdicts to health-quiet.json (gitignored) instead.
    annotate_history only READS health.json. Exit 0 = all green, 1 = some red."""
    bad = lint_roster()
    if bad:
        for item in bad:
            print(f"LINT: {item['name']}: {item.get('lint_why') or 'liveness-only row'}")
        return 2
    now = datetime.datetime.now()
    results = run_checks()
    annotate_history(results)
    slot = "--slot" in argv
    with open(os.path.join(REPO_DIR, f"health-quiet-{now:%H}.json") if slot
              else QUIET_FILE, "w") as f:
        json.dump({"checked": now.strftime("%Y-%m-%d %H:%M"), "quiet": True,
                   "results": results}, f, indent=1)
    red = [r for r in results if not r.get("ok")]
    print(f"=== QUIET fleet check {now:%Y-%m-%d %H:%M}: "
          f"{len(results) - len(red)} of {len(results)} healthy — nothing sent ===")
    for r in red:
        print(f"  ❌ {r['name']} — {r.get('detail', '')}")
    return 1 if red else 0


if __name__ == "__main__":
    if "--quiet" in sys.argv[1:]:
        sys.exit(quiet_main(sys.argv[1:]))
    sys.exit(cli(sys.argv[1:]))
