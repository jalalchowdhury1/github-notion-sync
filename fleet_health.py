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
import urllib.parse
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


def probe_gh_run(repo, workflow, max_age_h, log_grep=None, expect_event=None, **_):
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
        rescue = None
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
        patterns = [p.replace("{date}", f"(?:{_dates})") for p in patterns]
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
        for cand in runs[1:]:
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


def probe_telegram_webhook(token_env, expect_url, require_query_guard=False, **_):
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
    # so assert it still exists without ever storing or printing its value.
    if require_query_guard and not g.query:
        return False, ("webhook lost its shared-secret query guard — "
                       "the endpoint is now unauthenticated")
    detail = f"webhook registered, {pending} pending"
    if err:
        return False, f"{detail}, last_error={err!r}"
    return True, detail


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

      1. Any ERROR/Traceback in the Lambda log inside 24h -> red with the tail.
         Catches a revoked/rotated-but-not-updated GH_DISPATCH_PAT (urlopen
         raises on the 401), a bad deploy, and Lambda timeouts, in ONE place.
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
    """
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

    errors = _logs(24 * 60, "?ERROR ?Traceback ?\"Task timed out\"")
    if errors:
        return False, (f"{len(errors)} Lambda error line(s) in 24h — first: "
                       f"{errors[0][:180]}\n(check GH_DISPATCH_PAT validity and "
                       f"the gh-dispatcher deploy; log group {log_group})")
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
    return True, (f"{len(pings)} pings/{ping_window_min}m, "
                  f"{len(dispatches)} dispatches/{dispatch_window_h}h, 0 errors")


PROBE_FNS = {"web_fresh": probe_web_fresh, "web_200": probe_web_200,
             "one_clock_lambda": probe_one_clock_lambda,
             "local_stamp": probe_local_stamp, "launchd_exit": probe_launchd_exit,
             "file_mtime": probe_file_mtime, "gh_run": probe_gh_run,
             "planner_backup": probe_planner_backup,
             "log_marker": probe_log_marker,
             "telegram_webhook": probe_telegram_webhook,
             "nuts": probe_nuts,
             "nuts_radar": probe_nuts_radar}

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
    # ── One Clock (AWS EventBridge primary triggers, 2026-08-29) ────────────
    # The dispatch machinery itself. One red row here names the real culprit
    # when several expect_event rows would otherwise go red one by one.
    {"name": "one-clock (EventBridge->Lambda dispatcher)", "repo": "one-clock",
     "probe": "one_clock_lambda"},
    # The dead-man's-switch's own dead-man's-switch: health.yml (GH side)
    # watches this Mac; this row watches health.yml back, and expect_event
    # confirms AWS (one-clock-notion-health, 12:37 UTC) is what fires it —
    # mutual watching, so neither side can die silently.
    {"name": "github-notion-sync (daily health stamp)", "repo": "github-notion-sync",
     "probe": "gh_run", "workflow": "health.yml", "max_age_h": 36,
     "expect_event": "workflow_dispatch"},
    # Watchdog for the AAII scrape. Un-rostered before 2026-08-29 — the thing
    # that catches a silent scrape miss could itself go silent unnoticed.
    # AWS one-clock-sentiment-watchdog 19:30 UTC; GH 20:00 backstop.
    {"name": "sentiment-scraper (evening watchdog)", "repo": "sentiment-scraper",
     "probe": "gh_run", "workflow": "watchdog.yml", "max_age_h": 36,
     "expect_event": "workflow_dispatch"},
    # ────────────────────────────────────────────────────────────────────────
    # sentiment-scraper: cron 08:00 UTC, actually runs 09:51-11:17 (10 days).
    {"name": "sentiment-scraper (AAII weekly data)", "repo": "sentiment-scraper",
     # NO expect_event: only sentiment-scraper's WATCHDOG moved to AWS
     # (one-clock-sentiment-watchdog); this daily scrape is still GitHub-cron.
     "probe": "gh_run", "workflow": "daily-scrape.yml", "max_age_h": 36},
    # ynab-budget-brief: cron 11:00 UTC, actually runs 12:00-13:40 (10 days).
    # Since the 2026-08-19 quota redesign the run sends TWO messages (Eating
    # Out, then Aoife+Nabila). Each marker is printed only AFTER its
    # send_telegram() returns — together they prove both messages were
    # actually delivered, not merely that Python exited 0.
    {"name": "ynab-budget-brief (7am budget brief)", "repo": "ynab-budget-brief",
     "probe": "gh_run", "workflow": "daily_brief.yml", "max_age_h": 36,
     "log_grep": [r"Sent eating-out brief:", r"Sent family brief:"],
     "expect_event": "workflow_dispatch"},
    {"name": "financial-dashboard-history (2x-daily snapshots)", "repo": "financial-dashboard-history",
     "probe": "gh_run", "workflow": "scraper.yml", "max_age_h": 36,
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
    {"name": "hedgelab (noon hedge check)", "repo": "hedgelab",
     "probe": "gh_run", "workflow": "daily.yml", "max_age_h": 72},
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
     "probe": "gh_run", "workflow": "health-check.yml", "max_age_h": 36,
     "expect_event": "workflow_dispatch"},
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
    # The web_200 above probes the ROOT, which a dead handler still serves — a
    # gap noted in AGENTS.md and closed here 2026-08-25. Same two-independent-
    # deaths reasoning as voices-bot: the function can stop serving, OR Telegram
    # can stop pointing at it, and neither probe sees the other's failure.
    {"name": "zinger-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "ZINGER_BOT_TOKEN",
     "expect_url": "https://zinger-bot.vercel.app/api/webhook"},
    {"name": "aoife-math (daily game site)", "repo": "aoife-math",
     "probe": "web_200", "url": "https://aoife-math.vercel.app"},
    {"name": "aoife-columns (site)", "repo": "aoife-columns",
     "probe": "web_200", "url": "https://aoife-columns.vercel.app"},
    {"name": "aoife-frameworks (site)", "repo": "aoife-frameworks",
     "probe": "web_200", "url": "https://aoife-frameworks.vercel.app"},
    {"name": "nafis-mortgage (site)", "repo": "nafis-mortgage",
     "probe": "web_200", "url": "https://nafis-mortgage.vercel.app"},
    # Rostered 2026-08-25 during a coverage audit: aoife-math/columns/frameworks
    # were watched while these three equally-live sisters were not — coverage by
    # accident of when each was built, not by risk. aoife-puzzles matters most:
    # it is the WISC-V prep game, and a broken level reads to Jalal as a real
    # weakness in Aoife rather than a bug (see feedback-puzzle-validity-sacred).
    {"name": "aoife-puzzles (site)", "repo": "aoife-puzzles",
     "probe": "web_200", "url": "https://aoife-puzzles.vercel.app"},
    {"name": "aoife-algebra (site)", "repo": "aoife-algebra",
     "probe": "web_200", "url": "https://aoife-algebra.vercel.app"},
    {"name": "aoife-order (site)", "repo": "aoife-order",
     "probe": "web_200", "url": "https://aoife-order.vercel.app"},
    {"name": "backbench (daily trading brief)", "repo": "backbench",
     "probe": "web_200", "url": "https://backbench.vercel.app"},
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
    # The tick above proves the OUTBOUND half (the Mac pushing previews). It says
    # nothing about the INBOUND half: Jalal replying to the bot. Those die
    # independently — the tick keeps writing TICK OK while an unhooked bot
    # silently swallows every reply. Rostered 2026-08-25.
    # repo: None on purpose, so a healthy sibling row cannot paint over it in
    # Notion (last-row-wins trap).
    {"name": "aoife-school-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "SCHOOL_BOT_TOKEN",
     "expect_url": "https://aoife-school-bot.vercel.app/api/webhook",
     "require_query_guard": True},
    # aoife-milestones-bot had NO probe of any kind before 2026-08-25 — the only
    # live service in the fleet that was entirely unwatched. It is voice-driven
    # and write-through (voice → draft → ✓ → Sheet → Doc + Notion), so a deaf bot
    # loses milestones Jalal believes were recorded; the Sheet is master and it
    # simply stops gaining rows, which looks exactly like a quiet week.
    {"name": "aoife-milestones-bot (function serving)", "repo": "aoife-milestones-bot",
     "probe": "web_200", "url": "https://aoife-milestones-bot.vercel.app/api/webhook"},
    {"name": "aoife-milestones-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "MILESTONES_BOT_TOKEN",
     "expect_url": "https://aoife-milestones-bot.vercel.app/api/webhook",
     "require_query_guard": True},
    # notebooklm-drip: daily 04:00 with a 00:45 retry slot, both landing before
    # this 05:00 check. Grade the marker generate_all.py PRINTS ON COMPLETION,
    # correlated to a dated run header — a bare FINISHED would match a stale one
    # from last week, and drip.log's mtime only proves the wrapper woke up.
    {"name": "notebooklm-drip (nightly Gemini Notebook drip)", "repo": None,
     "probe": "log_marker",
     "log_path": "~/PycharmProjects/notebooklm-library/drip.log",
     "log_grep": r"=== drip {date}[\s\S]*?FINISHED types_still_open="},
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
    {"name": "sheets-backup (nightly sheets -> git)", "repo": "sheets-backup",
     "probe": "gh_run", "workflow": "backup.yml", "max_age_h": 24,
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
    {"name": "mental-models (nightly 3 models + audio)", "repo": "mental-models",
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
     "probe": "web_200", "url": "https://voices-bot.vercel.app/api/webhook"},
    # (b) — the one that actually happened, twice, on 2026-08-24.
    {"name": "voices-bot (telegram webhook registered)", "repo": None,
     "probe": "telegram_webhook", "token_env": "VOICES_BOT_TOKEN",
     "expect_url": "https://voices-bot.vercel.app/api/webhook"},
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
     "probe": "web_200", "url": "https://nuts-sooty.vercel.app"},
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
     "require_query_guard": True},
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
    note = correlated_note(results)
    room = TELEGRAM_LIMIT - len(header) - 1 - _size(note) - _size(footer)
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
    return "\n".join([header, ""] + note + body + footer)


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
