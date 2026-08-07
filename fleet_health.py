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

# ── probe implementations ───────────────────────────────────────────────────
# Contract: return (ok, detail). Raise on infrastructure trouble (gets
# retried); return False only for genuine data-level failure.

def _age_hours(ts: float) -> float:
    return (datetime.datetime.now().timestamp() - ts) / 3600


def probe_web_fresh(url, json_key, max_age_h, **_):
    """Fetch JSON and check a timestamp field is recent (data-level freshness)."""
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    raw = str(data.get(json_key, ""))
    ts = datetime.datetime.strptime(raw[:16], "%Y-%m-%d %H:%M").timestamp()
    age = _age_hours(ts)
    ok = age <= max_age_h
    return ok, f"data {age:.0f}h old" + ("" if ok else
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
         "--limit", "1", "--json", "conclusion,createdAt,databaseId"],
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
        log = subprocess.run(
            ["gh", "run", "view", str(run["databaseId"]), "-R",
             f"{GH_USER}/{repo}", "--log"],
            capture_output=True, text=True, timeout=120).stdout
        missing = [pat for pat in patterns if not re.search(pat, log)]
        if missing:
            return False, (f"run green but data marker missing "
                           f"({', '.join(repr(m) for m in missing)})"
                           f"\nrun: {run_url}")
        return True, f"success {age:.0f}h ago, data confirmed"
    return True, f"success {age:.0f}h ago"


PROBE_FNS = {"web_fresh": probe_web_fresh, "web_200": probe_web_200,
             "local_stamp": probe_local_stamp, "launchd_exit": probe_launchd_exit,
             "gh_run": probe_gh_run}

# ── the fleet roster ────────────────────────────────────────────────────────
# repo: GitHub repo name for the Notion row (None = not a repo, Telegram-only).
FLEET = [
    {"name": "dhaka-flights (nightly trip tracker)", "repo": "dhaka-flights",
     "probe": "web_fresh", "url": "https://raw.githubusercontent.com/jalalchowdhury1/dhaka-flights/main/site/data.json",
     "json_key": "updated", "max_age_h": 36},
    {"name": "carmax-scraper (nightly car picks)", "repo": "carmax-scraper",
     "probe": "local_stamp", "path": "~/PycharmProjects/carmax-scraper/.last_success_date",
     "max_age_h": 36},
    # Two separate workflows against the same source — the Daily snapshot and
    # the cumulative Historical sheet. They failed together 2026-08-04/05 but
    # only the Daily one was rostered, so the digest under-reported it as a
    # single failure. Both markers assert the 7-region fan-out ran rather than
    # that deals were found: whole regions legitimately sit at zero deals.
    {"name": "leasehackr-scraper (daily deals)", "repo": "leasehackr-scraper",
     "probe": "gh_run", "workflow": "daily_scraper.yml", "max_age_h": 48,
     "log_grep": [r"unique deal cards across \d+ regions",
                  r"Scraped \d+ deals total"]},
    {"name": "leasehackr-scraper (historical sheet)", "repo": "leasehackr-scraper",
     "probe": "gh_run", "workflow": "weekly_scraper.yml", "max_age_h": 48,
     "log_grep": [r"unique deal cards across \d+ regions",
                  r"refreshed the dashboard with [1-9]\d* sorted deals"]},
    {"name": "sentiment-scraper (AAII weekly data)", "repo": "sentiment-scraper",
     "probe": "gh_run", "workflow": "daily-scrape.yml", "max_age_h": 48},
    {"name": "ynab-budget-brief (7am budget brief)", "repo": "ynab-budget-brief",
     "probe": "gh_run", "workflow": "daily_brief.yml", "max_age_h": 48},
    {"name": "financial-dashboard-history (2x-daily snapshots)", "repo": "financial-dashboard-history",
     "probe": "gh_run", "workflow": "scraper.yml", "max_age_h": 36},
    {"name": "vix-fear-greed (daily tag)", "repo": "vix-fear-greed",
     "probe": "gh_run", "workflow": "fear-greed.yml", "max_age_h": 48},
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
    {"name": "reddit-scraper (daily data)", "repo": "reddit-scraper",
     "probe": "gh_run", "workflow": "daily_scrape.yml", "max_age_h": 36},
    # financial-telegram-bot is the owner's most important repo and was entirely
    # unrostered. Its own health-check Telegrams on warn/critical, but nothing
    # watched whether that health check still RUNS — a monitor that dies is
    # indistinguishable from a healthy fleet. Roster the monitor itself.
    {"name": "financial-telegram-bot (daily report)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "daily_report.yml", "max_age_h": 36},
    {"name": "financial-telegram-bot (self-health monitor)", "repo": "financial-telegram-bot",
     "probe": "gh_run", "workflow": "health-check.yml", "max_age_h": 36},
    {"name": "T7 Google-Drive backup (4am rsync)", "repo": None,
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
]


def _cfg_line(item) -> str:
    """One-line probe config so a failure block is self-describing."""
    keys = ("probe", "repo", "workflow", "url", "path", "label",
            "json_key", "max_age_h", "log_grep")
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


def format_digest(results, recovered=()) -> str:
    """One line when all healthy; full paste-to-Claude blocks when not."""
    today = datetime.date.today().isoformat()
    bad = [r for r in results if not r["ok"]]
    if not bad:
        text = f"✅ Fleet check {today} — all {len(results)} systems healthy"
        if recovered:
            text += f" · recovered: {', '.join(recovered)}"
        return text
    lines = [f"🚨 FLEET CHECK {today}: {len(bad)} of {len(results)} FAILING", ""]
    for r in bad:
        lines.append(f"❌ {r['name']}")
        since = r.get("failing_since")
        if since and since != today:
            lines.append(f"   ⏳ failing since {since}")
        lines.append(f"   [{r['cfg']}]")
        lines.extend(f"   {dl}" for dl in r["detail"].splitlines())
        lines.append("")
    if recovered:
        lines.append(f"💚 recovered today: {', '.join(recovered)}")
    ok_full = [r["name"] for r in results if r["ok"]]
    shorts = [n.split(" (")[0] for n in ok_full]
    # Keep the qualifier when one repo has several probes, otherwise two rows
    # collapse to "leasehackr-scraper, leasehackr-scraper" and read as a dupe.
    ok_names = [s if shorts.count(s) == 1 else full
                for s, full in zip(shorts, ok_full)]
    if ok_names:
        lines.append(f"✅ the other {len(ok_names)} healthy: {', '.join(ok_names)}")
    lines.append("")
    lines.append("Paste this whole message to Claude to debug "
                 "(fleet_health.py in github-notion-sync).")
    text = "\n".join(lines)
    if len(text) > 4000:                     # Telegram hard limit is 4096
        text = text[:3960] + "\n…(truncated — full details in health.json)"
    return text


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
