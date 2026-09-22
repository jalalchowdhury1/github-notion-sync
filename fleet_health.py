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

def _age_hours(ts: float) -> float:
    return (datetime.datetime.now().timestamp() - ts) / 3600


def _weekday_dates(today: datetime.date) -> list:
    """Today plus the most recent weekday before it — the `{weekday}` log_grep token,
    for weekday-only jobs whose newest output on a Sunday or Monday morning is Friday's."""
    prev = today - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    return [today.isoformat(), prev.isoformat()]


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
        if not data.get(json_key):
            return False, f"no {json_key!r} in the response"
        raw = str(data.get(json_key, ""))
        label = "data"
    ts = _parse_stamp(raw).timestamp()
    age = _age_hours(ts)
    ok = age <= max_age_h
    return ok, f"{label} {age:.0f}h old" + ("" if ok else
                                            f" (limit {max_age_h}h, raw {json_key}={raw!r})")


def probe_web_200(url, expect_text=None, **_):
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
                 no_rescue=False, **_):
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
        _today = datetime.date.today()
        _dates = "|".join(d.isoformat() for d in
                          (_today, _today - datetime.timedelta(days=1)))
        # {today} pins to today alone, no buffer — same token probe_cloudwatch_marker
        # uses. Added here 2026-09-12 (red team round 3) for rows where the
        # today|yesterday buffer is too loose to mean anything. Safe because this
        # monitor runs at 05:00 ET, when the local date and the UTC date agree;
        # it would NOT be safe if fleet-health ever ran between 20:00 (19:00 once DST
        # ends) and midnight.
        # {weekday} = today|the previous weekday, for weekday-only jobs (hedgelab):
        # {date}'s one-day buffer paged Friday's good plan every Sunday and Monday.
        patterns = [p.replace("{today}", _today.isoformat())
                     .replace("{weekday}", f"(?:{'|'.join(_weekday_dates(_today))})")
                     .replace("{date}", f"(?:{_dates})") for p in patterns]
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
    if err:
        return False, f"{detail}, last_error={err!r}"
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


def probe_cloudwatch_marker(log_group, log_grep, max_age_h=30, **_):
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
    """
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
               if not re.search(pat.replace("{today}", today.isoformat())
                                   .replace("{date}", f"(?:{dates})"), text)]
    if missing:
        return False, (f"delivery marker missing in {max_age_h}h of CloudWatch "
                       f"({', '.join(repr(m) for m in missing)})")

    # ok=false means the Lambda ran and FAILED to deliver — the exact state a
    # green Lambda invocation hides. Surface errors=N without failing on it: a
    # partial report still reached him, and paging on one bad section would
    # train him to ignore this row.
    if re.search(r"REPORT_DELIVERED ok=false", text):
        return False, "REPORT_DELIVERED ok=false — Lambda ran but did not deliver"
    errs = re.findall(r"REPORT_DELIVERED ok=true sections=(\d+) errors=(\d+)", text)
    if errs:
        sections, errors = errs[-1]
        note = f"delivered, {sections} sections"
        if int(errors):
            note += f", {errors} section error(s) — report went but incomplete"
        return True, note
    return True, "delivery marker confirmed"


def probe_log_block(log_path, block_re, log_grep, max_age_h, **_):
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
    """
    path = os.path.expanduser(log_path)
    try:
        text = open(path, errors="replace").read()
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
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return False, f"{path} missing"
    age = _age_hours(os.path.getmtime(p))
    if age > max_age_h:
        return False, f"stale: last written {age/24:.1f}d ago (limit {max_age_h}h)"
    lines = [l for l in open(p, errors="replace").read().splitlines() if l.strip()]
    last = lines[-1].strip() if lines else ""
    if not re.search(last_line, last):
        if grace_min and age * 60 <= grace_min:
            hit = next((l.strip() for l in reversed(lines[-grace_lines:])
                        if re.search(last_line, l.strip())), None)
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
     "probe": "gh_run", "workflow": "daily_scraper.yml", "max_age_h": 24,
     "log_grep": [r"Found [1-9]\d* unique deal cards across [1-9]\d* regions",
                  r"Scraped [1-9]\d* deals total"]},
    {"name": "leasehackr-scraper (historical sheet)", "repo": "leasehackr-scraper",
     "probe": "gh_run", "workflow": "weekly_scraper.yml", "max_age_h": 24,
     "log_grep": [r"Found [1-9]\d* unique deal cards across [1-9]\d* regions",
                  r"refreshed the dashboard with [1-9]\d* sorted deals"]},
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
    {"name": "github-notion-sync (daily health stamp)", "repo": "github-notion-sync",
     "probe": "gh_run", "workflow": "health.yml", "max_age_h": 36,
     "log_grep": r"Notion updated: [1-9]\d* rows.*checked {date}",
     "expect_event": "workflow_dispatch"},
    # Watchdog for the AAII scrape. Un-rostered before 2026-08-29 — the thing
    # that catches a silent scrape miss could itself go silent unnoticed.
    # AWS one-clock-sentiment-watchdog 19:30 UTC; GH 20:00 backstop.
    # Marker added 2026-09-12. The watchdog's whole job is to say FRESH or STALE.
    # A green run that printed STALE is the watchdog WORKING and the data being
    # broken — which must page. Asserting FRESH is therefore the data check, and
    # there is no date to pin: it reports a relative age, not an absolute stamp.
    {"name": "sentiment-scraper (evening watchdog)", "repo": "sentiment-scraper",
     "probe": "gh_run", "workflow": "watchdog.yml", "max_age_h": 36,
     # Bounded, not [\d.]+ (red team, 2026-09-12). The watchdog decides FRESH vs
     # STALE itself; an unbounded number meant a mis-set threshold printing
     # "FRESH: last write 740h ago" would still match and go green. 0-47.9h
     # re-derives the freshness claim instead of trusting the watchdog's word.
     "log_grep": r"FRESH: last write (?:[0-9]|[1-3][0-9]|4[0-7])(?:\.\d+)?h ago",
     "expect_event": "workflow_dispatch"},
    # ────────────────────────────────────────────────────────────────────────
    # sentiment-scraper: cron 08:00 UTC, actually runs 09:51-11:17 (10 days).
    {"name": "sentiment-scraper (AAII weekly data)", "repo": "sentiment-scraper",
     # NO expect_event: only sentiment-scraper's WATCHDOG moved to AWS
     # (one-clock-sentiment-watchdog); this daily scrape is still GitHub-cron.
     # Marker added 2026-09-12. Deliberately NOT pinned to {date}: the date in
     # this line is AAII's SURVEY date, which lags the run by days on a weekly
     # release. Pinning it to today would page every day of a normal week. What
     # this proves is that the run reached the sheet with real percentages; the
     # data's own freshness is the WATCHDOG row's job, not this one.
     "probe": "gh_run", "workflow": "daily-scrape.yml", "max_age_h": 36,
     "log_grep": r"Wrote to sheet: .*bull=[\d.]+%.*bear=[\d.]+%"},
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
    {"name": "financial-dashboard-history (2x-daily snapshots)", "repo": "financial-dashboard-history",
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
     "log_grep": [r"slot {today}-(?:AM|PM)",
                  # carried_forward capped under half of the 38 metrics (producer-side red
                  # team 2026-09-12): with every fetcher down, apply_fallbacks copies the
                  # last row forward and still prints "successfully appended". Normal is
                  # 0-3 (last 20 appends).
                  r"(?:Data successfully appended to Google Sheet\. \(\d+ metrics; carried_forward=(?:1[0-8]|[0-9]),"
                  r"|dedupe guard: slot {today}-(?:AM|PM) already has a successful run)"],
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
     "log_grep": r"results/daily/{weekday}\.json"},  # weekday-only: Sun/Mon see Friday's
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
     "log_grep": r"NUTS-SIGNAL (OK unchanged=|CHANGED )",
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
    {"name": "reddit-scraper (daily data)", "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "daily_scrape.yml", "max_age_h": 36,
     "log_grep": [r"last data/ commit: [^$\n]+ — proceeding"
                  r"|data/ already updated today \([^$\n]+\) — skipping",
                  r"DATA PUSHED: [1-9]\d* data files"
                  r"|data/ already updated today \([^$\n]+\) — skipping"]},
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
     "probe": "rsync_log", "log_path": "/Volumes/T7Files/sync.log",
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
    {"name": "aoife-columns (site)", "repo": "aoife-columns",
     "probe": "web_render", "url": "https://aoife-columns.vercel.app",
     "expect_text": "Solve big sums the column way",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    {"name": "aoife-frameworks (site)", "repo": "aoife-frameworks",
     "probe": "web_render", "url": "https://aoife-frameworks.vercel.app",
     "expect_text": "Pick a puzzle.",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    {"name": "nafis-mortgage (site)", "repo": "nafis-mortgage",
     "probe": "web_200", "url": "https://nafis-mortgage.vercel.app",
     "weak_ok": "finished work, nothing scheduled; proves it is served from its own host, not that the page renders"},
    # Rostered 2026-08-25 during a coverage audit: aoife-math/columns/frameworks
    # were watched while these three equally-live sisters were not — coverage by
    # accident of when each was built, not by risk. aoife-puzzles matters most:
    # it is the WISC-V prep game, and a broken level reads to Jalal as a real
    # weakness in Aoife rather than a bug (see feedback-puzzle-validity-sacred).
    {"name": "aoife-puzzles (site)", "repo": "aoife-puzzles",
     "probe": "web_render", "url": "https://aoife-puzzles.vercel.app",
     "expect_text": "Aoife Puzzles",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    {"name": "aoife-algebra (site)", "repo": "aoife-algebra",
     "probe": "web_render", "url": "https://aoife-algebra.vercel.app",
     "expect_text": "A letter is just a mystery box",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
    {"name": "aoife-order (site)", "repo": "aoife-order",
     "probe": "web_render", "url": "https://aoife-order.vercel.app",
     "expect_text": "builds a bag",
     "weak_ok": "static game: proves its own app shell is served, not that the game runs"},
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
    {"name": "trigger-board (07:00 + 18:00 must-do nag)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/trigger-board.log",
     "log_grep": r"BOARD OK \w+ {date}",
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
    {"name": "aoife-gcal-sync (nightly Google Calendar publish)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/Library/Logs/aoife-gcal-sync.log",
     "log_grep": r"GCAL-SYNC OK {date}",
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
     "log_grep": r"TRACKERS_OK 5/5 date={date}",
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
     "log_grep": r"BACKUP OK: \d+ owned tabs, \d+ public sources"},
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
    {"name": "NUTS (trading signal API)", "repo": "NUTS",
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
    {"name": "nuts-radar (catalyst board)", "repo": "nuts-radar",
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
    {"name": "health-hub (tick loop fresh)", "repo": "health-hub",
     "probe": "web_fresh", "url": "https://jalal-health.vercel.app/api/health",
     "json_key": "last_tick", "max_age_h": 3},
    # Same two-independent-deaths reasoning as the other webhook bots; repo None
    # so a healthy tick row can't paint over a deaf bot in Notion.
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
     "grace_min": 16},

    # financial-telegram-bot's two LOCAL launchd jobs. The two existing
    # financial-telegram-bot rows grade the cloud daily report and the
    # self-health monitor; neither sees these, on the highest-stakes
    # automation in the fleet.
    {"name": "defensive-nag (hourly risk prompt)", "repo": None,
     "probe": "log_marker", "log_path": "~/Library/Logs/defensive-nag.log",
     "log_grep": [r"defensive-trigger nag {date}T"]},
    # rubber-band is weekdays 18:30 ONLY, so a Monday 05:00 check is looking at
    # Friday evening — ~58 h. A dated marker would page every Monday. mtime at
    # 72 h clears the weekend and still catches a genuine multi-day stall.
    # Was file_mtime with weak_ok "prints no success marker". Not true (red team
    # round 4): every run prints `published -> <gist raw URL>` after the gist
    # write succeeds. Graded on the newest run block only; 72h covers Fri 18:30
    # to the Monday 05:00 check.
    {"name": "rubber-band (weekday evening check)", "repo": None,
     "probe": "log_block", "log_path": "~/Library/Logs/rubber-band.log",
     "block_re": r"^rubber-band run (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)",
     "log_grep": r"^\s*published → https://gist\.githubusercontent\.com/",
     "max_age_h": 72},

    # The tranche pair. 07:12 publish is the one Jalal would actually notice
    # missing. nag prints no date ("sent N chars" / "nothing due"), so it gets
    # mtime; publish stamps every line and gets the stronger dated marker.
    {"name": "tranche-nag (07:10 reminder)", "repo": None,
     "probe": "log_tail", "path": "~/Library/Logs/tranche-nag.log",
     # tranche-nag.py prints "sent N chars in N message(s)[ plain-fallback]" since 2026-09-12.
     "last_line": r"^(?:sent \d+ chars(?: in \d+ message\(s\)(?: plain-fallback)?)?"
                  r"|nothing due \([1-9]\d* rows parsed\))$",
     "max_age_h": 30},
    {"name": "tranche-publish (07:12 board publish)", "repo": None,
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


def run_checks() -> list:
    # The lint runs HERE, not only in main(). A red team (2026-09-12) pointed out
    # that a programmatic caller doing `from fleet_health import run_checks` would
    # bypass main() entirely and execute an unwaived liveness-only row -- which is
    # the exact route by which a backbench-shaped false green re-enters the board.
    # The guard belongs on the door the rows actually walk through.
    bad = lint_roster()
    if bad:
        names = ", ".join(i["name"] for i in bad)
        raise SystemExit(f"roster lint: liveness-only rows need weak_ok: {names}")
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


def publish(results, telegram_mode: str) -> None:
    payload = {"checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               # "sent"/"failed": unchanged contract — notion_health.py's Dead-Mac
               # watchdog dies on anything but "sent". telegram_mode is the new,
               # separate signal already_ran_today() actually needs: "digest" vs
               # "direct" (see _telegram_send).
               "telegram": "sent" if telegram_mode else "failed",
               "telegram_mode": telegram_mode or None,
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
        h = json.load(open(HEALTH_FILE))
        return (h.get("checked", "")[:10] == datetime.date.today().isoformat()
                and h.get("telegram_mode") == "direct")
    except Exception:                        # noqa: BLE001
        return False


def _lock_is_fresh() -> bool:
    try:
        return time.time() - os.path.getmtime(LOCK_FILE) < LOCK_STALE_S
    except OSError:
        return False


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
        weak = (item["probe"] in LIVENESS_ONLY
                or (item["probe"] == "gh_run" and not item.get("log_grep")))
        if weak and not item.get("weak_ok"):
            offenders.append(item)
    return offenders


def main(argv=()) -> None:
    now = datetime.datetime.now()
    bad = lint_roster()
    if bad:
        print("=== ROSTER LINT FAILED — refusing to run ===")
        for item in bad:
            print(f"  {item['name']}: probe {item['probe']!r} only proves liveness. "
                  f"Give it a real success marker, or add "
                  f'weak_ok="<why liveness is enough>".')
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
        mode = _telegram_send(format_digest(results, recovered), silent=True)   # 05:00 digest — no buzz (11 Sep 2026)
        publish(results, mode)
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
