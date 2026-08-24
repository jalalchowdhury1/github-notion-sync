#!/usr/bin/env python3
"""Daily fleet health check — verifies every scheduled automation ACTUALLY
produced data (not just green checkmarks — the 2026-07 CarMax incident ran
green for 17 days while writing nothing).

Runs ON THE MAC (launchd com.jalal.fleet-health, daily 5:00 AM with a 6:30 AM
retry slot — everything settled before wake-up) because only the Mac can see
all three worlds: local launchd stamps, GitHub Actions (gh CLI), and the live
sites. Outputs:
  1. Telegram digest — ONE line when everything is healthy; when anything
     fails, a full diagnostic block per failure (probe config, run URL,
     failed-log tail) meant to be pasted verbatim into Claude to debug.
  2. health.json committed+pushed to this repo — the daily GitHub Action
     (health.yml) then stamps the results into the Notion repos table and
     fails loudly if health.json goes stale (dead-Mac watchdog).

Reliability rules:
  - Probes RAISE on infrastructure errors (network blips, gh/launchctl
    failures) and those get retried 3x with a pause — a transient hiccup at
    5 AM must not page as a fake ❌. A probe that RETURNS False is real
    signal (stale data, red run) and is never retried.
  - Telegram sends are plain text (no Markdown — log excerpts full of _*[
    used to be able to 400 the whole digest) and retried 3x.
  - If this script itself crashes, a 🚨 panic Telegram is sent before exiting
    nonzero; the cloud watchdog catches a fully dead Mac within 2 days.
  - --retry-slot (the 6:30 AM run) exits early if today's digest already went
    out, so a healthy day gets exactly one message; a lock file makes it back
    off if the 5 AM run is somehow still going.
  - Failures carry a failing_since date across days (new breakage vs ongoing
    saga), and the first healthy digest after a failure says what recovered.

Stdlib only (+ the gh CLI and git, both already on the Mac).
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_FILE = os.path.join(REPO_DIR, "health.json")
LOCK_FILE = os.path.join(REPO_DIR, ".fleet_health.lock")
GH_USER = "jalalchowdhury1"
PROBE_ATTEMPTS = 3          # total tries for probes that raise (infra errors)
PROBE_RETRY_PAUSE_S = 20
LOCK_STALE_S = 2 * 3600     # a lock older than this is a crashed run, ignore
TELEGRAM_LIMIT = 4000       # hard cap is 4096; leave headroom for encoding

# ── probe implementations ───────────────────────────────────────────────────
# Contract: return (ok, detail). Raise on infrastructure trouble (gets
# retried); return False only for genuine data-level failure.

def _age_hours(ts: float) -> float:
    return (datetime.datetime.now().timestamp() - ts) / 3600


def _parse_stamp(raw):
    """"YYYY-MM-DD HH:MM" (or the same with a T) — and bare "YYYY-MM-DD".

    A date-only stamp parses as MIDNIGHT, so the age it reports is up to a day
    older than the truth. That is the safe direction (stricter, never laxer),
    but it means max_age_h for a date-only feed must budget an extra ~24 h.
    """
    s = str(raw).strip().replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp {raw!r}")


def probe_web_fresh(url, json_key, max_age_h, rows_key=None, **_):
    """Fetch JSON and check a timestamp field is recent (data-level freshness).

    rows_key: grade the OLDEST per-row stamp under data[rows_key] instead of a
    top-level field. Use it whenever the job rewrites its output file on every
    run regardless of outcome — a top-level `updated` then measures only that
    the job WOKE UP, which is exactly how dhaka-hotels published a green row
    through five nights of zero scraped rates (2026-08-11 → 08-16: `updated`
    said today, every row's `checked` said Aug 10). The rule this file already
    states for launchd_exit — never grade a proxy for the thing you care about
    — applies just as much to a timestamp the job stamps unconditionally."""
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    if rows_key:
        rows = data.get(rows_key) or []
        stamps = [str(row.get(json_key, "")) for row in rows
                  if isinstance(row, dict) and row.get(json_key)]
        if not stamps:
            return False, f"no {json_key!r} stamp on any {rows_key} row"
        raw = min(stamps, key=lambda s: _parse_stamp(s).timestamp())
        label = f"oldest of {len(stamps)} {rows_key}"
    else:
        raw = str(data.get(json_key, ""))
        label = "data"
    ts = _parse_stamp(raw).timestamp()
    age = _age_hours(ts)
    ok = age <= max_age_h
    return ok, f"{label} {age:.0f}h old" + ("" if ok else
                                            f" (limit {max_age_h}h, raw {json_key}={raw!r})")


def probe_web_200(url, **_):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        ok = r.status == 200
        return ok, f"HTTP {r.status}"


def probe_local_stamp(path, max_age_h, **_):
    """Stamp file contains an ISO date written on success."""
    date = open(os.path.expanduser(path)).read().strip()
    ts = datetime.datetime.strptime(date, "%Y-%m-%d").timestamp()
    age = _age_hours(ts)
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
    return False, "job not loaded"


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


def probe_gh_run(repo, workflow, max_age_h, log_grep=None, **_):
    """Latest workflow run: recent + successful; optionally grep the log for
    data-level markers proving real work happened, not just a green exit.

    `log_grep` is one regex or a list of them — ALL must match. Prefer markers
    that stay true on a legitimately quiet day: assert the pipeline ran ("across
    N regions"), not that the count was nonzero, or a slow source hands you a
    false alarm at 5 AM.
    """
    p = subprocess.run(
        ["gh", "run", "list", "-R", f"{GH_USER}/{repo}", "--workflow", workflow,
         "--limit", "1", "--json", "status,conclusion,createdAt,databaseId"],
        capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"gh run list failed: {p.stderr.strip()[:150]}")
    runs = json.loads(p.stdout or "[]")
    if not runs:
        return False, "no runs found"
    run = runs[0]
    run_url = f"https://github.com/{GH_USER}/{repo}/actions/runs/{run['databaseId']}"
    ts = datetime.datetime.strptime(run["createdAt"][:16], "%Y-%m-%dT%H:%M")
    age = _age_hours(ts.replace(tzinfo=datetime.timezone.utc).timestamp())
    # An in-progress run has conclusion "" — without this branch it rendered as
    # "last run  (1h ago)", which reads like a failure and hides the real story.
    # A hang needs the opposite remedy from a failure (cancel + find the wedged
    # step, vs read the log tail), and its log is not fetchable until it ends.
    if run.get("status") != "completed":
        return False, (f"still running {age:.0f}h (normal runs finish in minutes)"
                       f" — likely HUNG; cancel it to release the runner and"
                       f" expose the wedged step's log\nrun: {run_url}")
    if run["conclusion"] != "success":
        detail = f"last run {run['conclusion']} ({age:.0f}h ago)\nrun: {run_url}"
        tail = _failed_log_tail(repo, run["databaseId"])
        if tail:
            detail += f"\nlog tail:\n{tail}"
        return False, detail
    if age > max_age_h:
        return False, (f"no run in {age/24:.1f}d (limit {max_age_h}h)"
                       f"\nlast run: {run_url}")
    if log_grep:
        patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
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
        if missing:
            return False, (f"run green but data marker missing "
                           f"({', '.join(repr(m) for m in missing)})"
                           f"\nrun: {run_url}")
        return True, f"success {age:.0f}h ago, data confirmed"
    return True, f"success {age:.0f}h ago"


def _valid_json(path):
    try:
        json.load(open(path))
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
    candidates = [today, today - datetime.timedelta(days=1)]
    dest = os.path.expanduser(dest_dir)
    missing = [name for name in names
               if not any(_valid_json(os.path.join(dest, f"{d.isoformat()}-{name}.json"))
                          for d in candidates)]
    if missing:
        return False, (f"missing/unparseable snapshot(s): {', '.join(missing)} "
                       f"(checked {candidates[1]} & {candidates[0]} in {dest_dir})")
    log = os.path.expanduser(log_path)
    try:
        text = open(log).read()
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
        text = open(log, errors="replace").read()
    except FileNotFoundError:
        return False, f"{log_path} missing"
    dates = "|".join(d.isoformat() for d in
                     (today, today - datetime.timedelta(days=1)))
    patterns = [log_grep] if isinstance(log_grep, str) else list(log_grep)
    missing = [p for p in patterns
               if not re.search(p.replace("{date}", f"(?:{dates})"), text)]
    if missing:
        return False, (f"no {', '.join(repr(m) for m in missing)} match "
                       f"in {log_path} (checked {dates})")
    return True, f"marker present in {log_path}"


PROBE_FNS = {"web_fresh": probe_web_fresh, "web_200": probe_web_200,
             "local_stamp": probe_local_stamp, "launchd_exit": probe_launchd_exit,
             "file_mtime": probe_file_mtime, "gh_run": probe_gh_run,
             "planner_backup": probe_planner_backup,
             "log_marker": probe_log_marker}

# ── the fleet roster ────────────────────────────────────────────────────────
# repo: GitHub repo name for the Notion row (None = not a repo, Telegram-only).
FLEET = [
    {"name": "dhaka-flights (nightly trip tracker)", "repo": "dhaka-flights",
     "probe": "web_fresh", "url": "https://raw.githubusercontent.com/jalalchowdhury1/dhaka-flights/main/site/data.json",
     "json_key": "updated", "max_age_h": 36},
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
    {"name": "dhaka-hotels (nightly award-rate research)", "repo": None,
     "probe": "web_fresh",
     "url": "https://raw.githubusercontent.com/jalalchowdhury1/dhaka-flights/main/site/hotel_rates.json",
     "json_key": "checked", "rows_key": "rows", "max_age_h": 96},
    {"name": "carmax-scraper (nightly car picks)", "repo": "carmax-scraper",
     "probe": "local_stamp", "path": "~/PycharmProjects/carmax-scraper/.last_success_date",
     "max_age_h": 36},
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
     "probe": "gh_run", "workflow": "daily_scraper.yml", "max_age_h": 24,
     "log_grep": [r"unique deal cards across \d+ regions",
                  r"Scraped \d+ deals total"]},
    {"name": "leasehackr-scraper (historical sheet)", "repo": "leasehackr-scraper",
     "probe": "gh_run", "workflow": "weekly_scraper.yml", "max_age_h": 24,
     "log_grep": [r"unique deal cards across \d+ regions",
                  r"refreshed the dashboard with [1-9]\d* sorted deals"]},
    # 48 -> 36 on the three daily entries below (2026-08-06). Their runs land
    # AFTER the 09:00 UTC check, so the freshest run the check can ever see is
    # yesterday's — 17-23 h old on a good day, 41-47 h after ONE missed day.
    # 48 therefore needed TWO consecutive misses to alarm; 36 catches the first
    # while leaving 13-19 h of slack over the worst measured run time.
    # sentiment-scraper: cron 08:00 UTC, actually runs 09:51-11:17 (10 days).
    {"name": "sentiment-scraper (AAII weekly data)", "repo": "sentiment-scraper",
     "probe": "gh_run", "workflow": "daily-scrape.yml", "max_age_h": 36},
    # ynab-budget-brief: cron 11:00 UTC, actually runs 12:00-13:40 (10 days).
    # Since the 2026-08-19 quota redesign the run sends TWO messages (Eating
    # Out, then Aoife+Nabila). Each marker is printed only AFTER its
    # send_telegram() returns — together they prove both messages were
    # actually delivered, not merely that Python exited 0.
    {"name": "ynab-budget-brief (7am budget brief)", "repo": "ynab-budget-brief",
     "probe": "gh_run", "workflow": "daily_brief.yml", "max_age_h": 36,
     "log_grep": [r"Sent eating-out brief:", r"Sent family brief:"]},
    {"name": "financial-dashboard-history (2x-daily snapshots)", "repo": "financial-dashboard-history",
     "probe": "gh_run", "workflow": "scraper.yml", "max_age_h": 36},
    # vix-fear-greed: cron 13:00 UTC, actually runs 14:36-15:46 (10 days).
    # fear_greed.py writes the tag to stdout, which the workflow redirects into
    # tag.txt — so the only place the value appears in the log is the "Show
    # result" step's echoed command, where Actions has already interpolated it.
    # Requiring WORD+DIGITS proves a real tag was computed (GREED11 / FEAR12 /
    # NEUTRAL00); an empty tag.txt would render as "**Result:** " and miss.
    {"name": "vix-fear-greed (daily tag)", "repo": "vix-fear-greed",
     "probe": "gh_run", "workflow": "fear-greed.yml", "max_age_h": 36,
     "log_grep": [r"\*\*Result:\*\* [A-Z]+\d+"]},
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
    {"name": "hedgelab (noon hedge check)", "repo": "hedgelab",
     "probe": "gh_run", "workflow": "daily.yml", "max_age_h": 72},
    {"name": "trading-algorithm- (30-min signal)", "repo": "trading-algorithm-",
     "probe": "gh_run", "workflow": "trading_alert.yml", "max_age_h": 72},
    # reddit-scraper's commit step is `git commit … || exit 0`, so a run that
    # scrapes nothing still exits GREEN having written nothing — conclusion-only
    # was blind to it. The workflow has TWO legitimate shapes (03:00 scrape,
    # 09:00 retry that dedupes itself), so each marker is an either/or:
    #   1. the guard step actually PRINTED its decision, and
    #   2. the scrapers actually started (or the run was the legitimate no-op).
    # `[^$\n]+` is load-bearing: Actions echoes the step's source in the log, so
    # a plain "already updated today" would match `echo "…($LAST)…"` on every
    # run and never fail. Excluding `$` keeps the echoed source out.
    {"name": "reddit-scraper (daily data)", "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "daily_scrape.yml", "max_age_h": 36,
     "log_grep": [r"last data/ commit: [^$\n]+ — proceeding"
                  r"|data/ already updated today \([^$\n]+\) — skipping",
                  r"Google News Aggregator"
                  r"|data/ already updated today \([^$\n]+\) — skipping"]},
    # financial-telegram-bot is the owner's most important repo and was entirely
    # unrostered. Its own health-check Telegrams on warn/critical, but nothing
    # watched whether that health check still RUNS — a monitor that dies is
    # indistinguishable from a healthy fleet. Roster the monitor itself.
    {"name": "financial-telegram-bot (daily report)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "daily_report.yml", "max_age_h": 36},
    {"name": "financial-telegram-bot (self-health monitor)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "health-check.yml", "max_age_h": 36},
    # The BACKUP needs two probes because neither failure mode implies the other.
    # `launchd_exit` alone was a probe that could essentially never fail: the script
    # ends with `tail … && mv`, so it used to report mv's status and discard rsync's
    # (fixed 2026-08-06 to `exit $rc`), AND it deliberately `exit 0`s when the T7 is
    # unmounted — so an unplugged drive read "last exit 0 ✅" every morning while
    # nothing was backed up. mtime catches "not running / drive gone"; exit status
    # catches "ran but rsync errored". A silently dead backup is the worst class of
    # failure here: it is only discovered when you need to restore.
    {"name": "T7 Google-Drive backup (ran recently)", "repo": None,
     "probe": "file_mtime", "path": "/Volumes/T7Files/sync.log", "max_age_h": 36},
    {"name": "T7 Google-Drive backup (rsync exit status)", "repo": None,
     "probe": "launchd_exit", "label": "com.jalal.t7-drive-sync"},
    {"name": "zinger-bot (Telegram bot on Vercel)", "repo": "zinger-bot",
     "probe": "web_200", "url": "https://zinger-bot.vercel.app"},
    {"name": "aoife-math (daily game site)", "repo": "aoife-math",
     "probe": "web_200", "url": "https://aoife-math.vercel.app"},
    {"name": "aoife-columns (site)", "repo": "aoife-columns",
     "probe": "web_200", "url": "https://aoife-columns.vercel.app"},
    {"name": "aoife-frameworks (site)", "repo": "aoife-frameworks",
     "probe": "web_200", "url": "https://aoife-frameworks.vercel.app"},
    {"name": "nafis-mortgage (site)", "repo": "nafis-mortgage",
     "probe": "web_200", "url": "https://nafis-mortgage.vercel.app"},
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
    {"name": "aoife-school-bot (30-min Telegram tick)", "repo": "aoife-school-bot",
     "probe": "log_marker",
     "log_path": "~/Library/Logs/aoife-school-bot-tick.log",
     "log_grep": r"TICK OK {date}",
     "live_since": "2026-08-19"},
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
    {"name": "aoife-gcal-sync (nightly Google Calendar publish)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/aoife-gcal-sync.log",
     "log_grep": r"GCAL-SYNC OK {date}",
     "live_since": "2026-08-20"},
    # Added 2026-08-24 with the daily-trackers deploy (launchd
    # com.jalal.daily-trackers, run_daily.sh, 4:30 AM + 5:30 AM retry).
    # Appends five metric rows (Zillow Zestimate, Redfin estimate, MND 30-yr
    # mortgage rate, USD-CAD, USD-BDT) to the "Automa Data" Google Sheet that
    # the Daily Trackers spreadsheet reads live. `TRACKERS_OK 5/5`
    # specifically: a partial night prints `TRACKERS_FAIL [...]` instead
    # (per-metric isolation still writes what it can), and a bounds-rejected
    # value is a FAIL by design — a wrong number in that sheet is worse than a
    # missing one. The 4:30 marker exists well before the 5:00 check; if only
    # the 5:30 retry succeeds, the 6:30 slot sees it (the {date} window is
    # extra slack, not the mechanism).
    {"name": "daily-trackers (nightly Automa Data sheet update)", "repo": "daily-trackers",
     "probe": "log_marker",
     "log_path": "~/PycharmProjects/daily-trackers/cron.log",
     "log_grep": r"TRACKERS_OK 5/5 date={date}",
     "live_since": "2026-08-25"},
]


def _cfg_line(item) -> str:
    """One-line probe config so a failure block is self-describing."""
    keys = ("probe", "repo", "workflow", "url", "path", "label",
            "json_key", "max_age_h", "log_grep", "dest_dir", "names",
            "log_path", "live_since")
    def fmt(v):
        return " + ".join(v) if isinstance(v, list) else v
    return " · ".join(f"{k}={fmt(item[k])}"
                      for k in keys if item.get(k) is not None)


def run_checks() -> list:
    results = []
    for item in FLEET:
        fn = PROBE_FNS[item["probe"]]
        err = ""
        for attempt in range(1, PROBE_ATTEMPTS + 1):
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
        results.append({"name": item["name"], "repo": item.get("repo"),
                        "ok": ok, "detail": detail, "cfg": _cfg_line(item)})
        print(f"  {'✅' if ok else '❌'} {item['name']} — {detail.splitlines()[0]}")
    return results


# ── reporting ───────────────────────────────────────────────────────────────

def annotate_history(results) -> list:
    """Carry failing_since across days (before health.json is overwritten) and
    return the systems that failed last run but are healthy now."""
    try:
        prev = json.load(open(HEALTH_FILE))
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
                  if any(r["name"] == n and r["ok"] for r in results))


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


def format_digest(results, recovered=()) -> str:
    """One line when all healthy; full paste-to-Claude blocks when not.

    Every failing system is named no matter how many fail at once (a fleet-wide
    outage is exactly when the digest must not eat its own tail), and the
    "✅ the other N healthy" line always survives, so the message states the
    scope of the damage even when the detail had to be cut.
    """
    today = datetime.date.today().isoformat()
    bad = [r for r in results if not r["ok"]]
    if not bad:
        text = f"✅ Fleet check {today} — all {len(results)} systems healthy"
        if recovered:
            text += f" · recovered: {', '.join(recovered)}"
        return text
    header = f"🚨 FLEET CHECK {today}: {len(bad)} of {len(results)} FAILING"
    footer = []
    if recovered:
        footer.append(f"💚 recovered today: {', '.join(recovered)}")
    ok_full = [r["name"] for r in results if r["ok"]]
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
    # Header + footer are reserved first; whatever is left is split evenly
    # across the failures, and blocks that come in under their share hand the
    # slack back to the big ones (usually a gh_run block carrying a log tail).
    room = TELEGRAM_LIMIT - len(header) - 1 - _size(footer)
    if sum(_size(b) for b in blocks) > room:
        share = max(0, room // len(bad))
        slack = sum(share - _size(b) for b in blocks if _size(b) < share)
        big = [i for i, b in enumerate(blocks) if _size(b) >= share]
        budget = share + (slack // len(big) if big else 0)
        blocks = [b if _size(b) < share else _failure_block(bad[i], today, budget)
                  for i, b in enumerate(blocks)]
    body = [l for b in blocks for l in b]
    if _size(body) > room:
        # Pathological (dozens of failures at once): fall back to the roll call
        # of what is down. The names are the last thing to go, and the footer
        # is never touched.
        body = [f"❌ {r['name']}" for r in bad]
        while body and _size(body) > room:
            body.pop()
            if body:
                body[-1] = "…(+ more — full list in health.json)"
    return "\n".join([header, ""] + body + footer)


def _telegram_send(text) -> bool:
    """Plain-text send (no parse_mode — log excerpts would break Markdown),
    3 attempts."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(no Telegram creds — digest not sent)")
        return False
    for attempt in range(1, 4):
        try:
            body = json.dumps({"chat_id": chat, "text": text,
                               "disable_web_page_preview": True}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"Telegram digest: HTTP {r.status}")
                return True
        except Exception as e:               # noqa: BLE001
            print(f"WARN: Telegram send attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(15)
    return False


def publish(results, telegram_sent: bool) -> None:
    payload = {"checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "telegram": "sent" if telegram_sent else "failed",
               "results": results}
    with open(HEALTH_FILE, "w") as f:
        json.dump(payload, f, indent=1)
    def git(*args):
        return subprocess.run(["git", "-C", REPO_DIR] + list(args),
                              capture_output=True, text=True, timeout=60)
    git("add", "health.json")
    c = git("commit", "-m", f"Fleet health: {payload['checked']}")
    if c.returncode == 0:
        p = git("push")
        print("health.json pushed" if p.returncode == 0
              else f"WARN: push failed: {p.stderr.strip()[:150]}")
    elif "nothing to commit" not in c.stdout:
        print(f"WARN: commit failed: {c.stderr.strip()[:150]}")


def already_ran_today() -> bool:
    """True if today's check completed AND its digest reached Telegram —
    an undelivered digest makes the 6:30 AM retry slot rerun everything."""
    try:
        h = json.load(open(HEALTH_FILE))
        return (h.get("checked", "")[:10] == datetime.date.today().isoformat()
                and h.get("telegram") == "sent")
    except Exception:                        # noqa: BLE001
        return False


def _lock_is_fresh() -> bool:
    try:
        return time.time() - os.path.getmtime(LOCK_FILE) < LOCK_STALE_S
    except OSError:
        return False


def main(argv=()) -> None:
    now = datetime.datetime.now()
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
        sent = _telegram_send(format_digest(results, recovered))
        publish(results, sent)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    print("=== Done ===")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:                        # noqa: BLE001 — die LOUDLY
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        _telegram_send("🚨 fleet_health.py itself CRASHED — the checker is "
                       f"down, not the fleet:\n{tb[-1500:]}\n"
                       "Paste this to Claude to debug.")
        sys.exit(1)
