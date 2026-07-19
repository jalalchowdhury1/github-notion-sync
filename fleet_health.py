#!/usr/bin/env python3
"""Weekly fleet health check — verifies every scheduled automation ACTUALLY
produced data (not just green checkmarks — the 2026-07 CarMax incident ran
green for 17 days while writing nothing).

Runs ON THE MAC (launchd com.jalal.fleet-health, Sundays 5:00 AM) because only
the Mac can see all three worlds: local launchd stamps, GitHub Actions (gh CLI),
and the live sites. Outputs:
  1. Telegram digest (one ✅/⚠️ line per system)
  2. health.json committed+pushed to this repo — a weekly GitHub Action
     (health-to-notion.yml) then stamps the results into the Notion repos table.

Stdlib only (+ the gh CLI and git, both already on the Mac).
"""

import datetime
import json
import os
import subprocess
import urllib.request

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
HEALTH_FILE = os.path.join(REPO_DIR, "health.json")
GH_USER = "jalalchowdhury1"

# ── probe implementations ───────────────────────────────────────────────────

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
    return ok, f"data {age:.0f}h old" + ("" if ok else f" (limit {max_age_h}h)")


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
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                         timeout=15).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == label:
            code = parts[1]
            return code == "0", f"last exit {code}"
    return False, "job not loaded"


def probe_gh_run(repo, workflow, max_age_h, log_grep=None, **_):
    """Latest workflow run: recent + successful; optionally grep the log for a
    data-level marker (e.g. 'Scraped [1-9]' proves rows actually moved)."""
    out = subprocess.run(
        ["gh", "run", "list", "-R", f"{GH_USER}/{repo}", "--workflow", workflow,
         "--limit", "1", "--json", "conclusion,createdAt,databaseId"],
        capture_output=True, text=True, timeout=60).stdout
    runs = json.loads(out or "[]")
    if not runs:
        return False, "no runs found"
    run = runs[0]
    ts = datetime.datetime.strptime(run["createdAt"][:16], "%Y-%m-%dT%H:%M")
    age = _age_hours(ts.replace(tzinfo=datetime.timezone.utc).timestamp())
    if run["conclusion"] != "success":
        return False, f"last run {run['conclusion']} ({age:.0f}h ago)"
    if age > max_age_h:
        return False, f"no run in {age/24:.1f}d (limit {max_age_h}h)"
    if log_grep:
        log = subprocess.run(
            ["gh", "run", "view", str(run["databaseId"]), "-R",
             f"{GH_USER}/{repo}", "--log"],
            capture_output=True, text=True, timeout=120).stdout
        import re
        if not re.search(log_grep, log):
            return False, f"run green but data marker missing ({log_grep!r})"
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
    {"name": "leasehackr-scraper (daily deals)", "repo": "leasehackr-scraper",
     "probe": "gh_run", "workflow": "daily_scraper.yml", "max_age_h": 48,
     "log_grep": r"Scraped [1-9]\d* deals"},
    {"name": "sentiment-scraper (AAII weekly data)", "repo": "sentiment-scraper",
     "probe": "gh_run", "workflow": "daily-scrape.yml", "max_age_h": 48},
    {"name": "ynab-budget-brief (7am budget brief)", "repo": "ynab-budget-brief",
     "probe": "gh_run", "workflow": "daily_brief.yml", "max_age_h": 48},
    {"name": "financial-dashboard-history (2x-daily snapshots)", "repo": "financial-dashboard-history",
     "probe": "gh_run", "workflow": "scraper.yml", "max_age_h": 36},
    {"name": "vix-fear-greed (daily tag)", "repo": "vix-fear-greed",
     "probe": "gh_run", "workflow": "fear-greed.yml", "max_age_h": 48},
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


def run_checks() -> list:
    results = []
    for item in FLEET:
        fn = PROBE_FNS[item["probe"]]
        try:
            ok, detail = fn(**item)
        except Exception as e:               # noqa: BLE001 — a probe crash IS a failure
            ok, detail = False, f"probe error: {type(e).__name__}: {e}"[:160]
        results.append({"name": item["name"], "repo": item.get("repo"),
                        "ok": ok, "detail": detail})
        print(f"  {'✅' if ok else '❌'} {item['name']} — {detail}")
    return results


def send_telegram(results) -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("(no Telegram creds — digest not sent)")
        return
    bad = [r for r in results if not r["ok"]]
    head = ("✅ *Weekly fleet check: all systems healthy*"
            if not bad else
            f"⚠️ *Weekly fleet check: {len(bad)} of {len(results)} systems need attention*")
    lines = [head, ""]
    for r in results:
        lines.append(f"{'✅' if r['ok'] else '❌'} {r['name']}\n     _{r['detail']}_")
    lines.append(f"\n_{datetime.date.today().isoformat()} · details land in the Notion repos table_")
    body = json.dumps({"chat_id": chat, "text": "\n".join(lines),
                       "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"Telegram digest: HTTP {r.status}")
    except Exception as e:                   # noqa: BLE001
        print(f"WARN: Telegram digest failed: {e}")


def publish(results) -> None:
    payload = {"checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "results": results}
    with open(HEALTH_FILE, "w") as f:
        json.dump(payload, f, indent=1)
    def git(*args):
        return subprocess.run(["git", "-C", REPO_DIR] + list(args),
                              capture_output=True, text=True, timeout=60)
    git("add", "health.json")
    c = git("commit", "-m", f"Weekly health: {payload['checked']}")
    if c.returncode == 0:
        p = git("push")
        print("health.json pushed" if p.returncode == 0
              else f"WARN: push failed: {p.stderr.strip()[:150]}")
    elif "nothing to commit" not in c.stdout:
        print(f"WARN: commit failed: {c.stderr.strip()[:150]}")


def main():
    print(f"=== Fleet health check {datetime.datetime.now():%Y-%m-%d %H:%M} ===")
    results = run_checks()
    send_telegram(results)
    publish(results)
    print("=== Done ===")


if __name__ == "__main__":
    main()
