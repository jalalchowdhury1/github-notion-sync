#!/usr/bin/env python3
"""Stamp the daily fleet-health results (health.json, pushed by the Mac's
5 AM fleet_health.py run) onto the Notion repos table.

Adds/updates three properties per matching repo row:
  Health         (select: ✅ Healthy / ❌ Failing)
  Health checked (date)
  Health note    (rich_text — the probe detail line)

Runs in GitHub Actions (health.yml, daily 13:07 UTC) with the same
NOTION_TOKEN / NOTION_DATABASE_ID secrets sync.py already uses. Stdlib only.
This is the DEAD-MAC WATCHDOG: it fails loudly — nonzero exit (Actions email)
AND a Telegram of its own — at the first daily check that finds health.json
older than STALE_HOURS, i.e. within ~1.4 days of the last good stamp. The
Telegram matters because a dead Mac cannot send its own digest, and an Actions
email is easy to miss for a week.
"""

import datetime
import json
import os
import sys
import time
import urllib.request

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GH_USER = "jalalchowdhury1"

# The Mac stamps health.json at 05:00 local (09:00 UTC) daily; this workflow's
# 13:07 UTC cron actually starts 14:38-15:49 UTC (8 days measured). `checked` is
# written in the Mac's LOCAL time and compared against the runner's UTC clock,
# which INFLATES the age by 4 h (EDT) / 5 h (EST) — the safe direction. So a
# normal day measures 10-12 h here and ONE missed Mac run measures 34-37 h.
# 24 h sits between them, so the watchdog fires at the first check after a
# missed stamp: ~1.4 days later, which is the "within two days" guarantee the
# docs claim. The old `age_days > STALE_DAYS(2)` on a date-only comparison did
# not fire until the THIRD day after the last stamp (~3.4 days) — the docs said
# two days and the code delivered three and a half.
STALE_HOURS = 24


def _stamp_age_hours(checked: str) -> float:
    for fmt, n in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            when = datetime.datetime.strptime(checked[:n], fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unparseable 'checked' value {checked!r}")
    return (datetime.datetime.now() - when).total_seconds() / 3600


def telegram_alert(text) -> bool:
    """Best-effort cloud-side alert — the ONLY channel that survives a dead Mac.

    Plain text, no parse_mode (same reason as fleet_health.py: probe details are
    full of _*[ ). Missing creds degrade to a warning: the nonzero exit and the
    Actions failure email still stand, so a workflow without the secrets wired
    is quieter but never crashes.
    """
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("WARN: no TELEGRAM_TOKEN/TELEGRAM_CHAT_ID in this workflow — "
              "alerting by Actions email only")
        return False
    body = json.dumps({"chat_id": chat, "text": text,
                       "disable_web_page_preview": True}).encode()
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"Telegram alert sent: HTTP {r.status}")
                return True
        except Exception as e:            # noqa: BLE001 — alerting must not crash
            print(f"WARN: Telegram attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(10)
    return False


def die(msg) -> None:
    """Fail this run three ways at once: log, Telegram, nonzero exit."""
    print(f"FATAL: {msg}")
    telegram_alert(f"🚨 FLEET WATCHDOG (cloud, github-notion-sync)\n\n{msg}")
    sys.exit(1)


def http(method, url, token, body=None):
    """3 attempts / 20s pause on network errors — same convention as
    fleet_health.py's PROBE_ATTEMPTS (a single flaky Notion read timeout
    should not fail the whole run)."""
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except Exception:                    # noqa: BLE001 — infra error: retry
            if attempt == 3:
                raise
            print(f"WARN: Notion {method} attempt {attempt} failed, retrying")
            time.sleep(20)


def ensure_properties(token, db_id):
    """Create the health columns if they don't exist (idempotent)."""
    _, db = http("GET", f"{NOTION_API}/databases/{db_id}", token)
    have = db.get("properties", {})
    want = {}
    if "Health" not in have:
        want["Health"] = {"select": {"options": [
            {"name": "✅ Healthy", "color": "green"},
            {"name": "❌ Failing", "color": "red"}]}}
    if "Health checked" not in have:
        want["Health checked"] = {"date": {}}
    if "Health note" not in have:
        want["Health note"] = {"rich_text": {}}
    if want:
        http("PATCH", f"{NOTION_API}/databases/{db_id}", token,
             {"properties": want})
        print(f"added properties: {sorted(want)}")


def pages_by_repo_url(token, db_id):
    out, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        _, data = http("POST", f"{NOTION_API}/databases/{db_id}/query", token, body)
        for page in data.get("results", []):
            url = (page.get("properties", {}).get("Repo URL", {}) or {}).get("url")
            if url:
                out[url.rstrip("/")] = page["id"]
        if not data.get("has_more"):
            return out
        cursor = data.get("next_cursor")


def main():
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DATABASE_ID"]

    health = json.load(open("health.json"))
    checked = health["checked"]
    age_h = _stamp_age_hours(checked)
    if age_h > STALE_HOURS:
        die(f"health.json has not been updated in {age_h:.0f}h "
            f"(last stamp {checked}, limit {STALE_HOURS}h). The Mac-side "
            f"fleet_health run has stopped, so NOTHING is watching the fleet "
            f"right now. Investigate com.jalal.fleet-health on the Mac mini "
            f"(launchctl list | grep fleet-health; tail health.log).")

    # A digest the owner never RECEIVED is indistinguishable from a healthy fleet:
    # publish() stamps `checked` even when _telegram_send() returned False, so a
    # dropped Telegram leaves this watchdog looking at a perfectly fresh file. The
    # creds are sourced cross-repo (run_health.sh: `source ".../Dhaka flights/.env"`,
    # 2>/dev/null, unchecked) — a rename there silences every digest, INCLUDING the
    # self-crash panic message, which uses the same sender. Fail loudly instead:
    # this is the only cloud-side check that survives a dead Mac.
    if health.get("telegram") != "sent":
        die(f"fleet digest was NOT delivered (telegram={health.get('telegram')!r}) "
            f"as of {checked}. The checks ran, but the owner heard nothing — silence "
            f"here reads as health. Check TELEGRAM_TOKEN/TELEGRAM_CHAT_ID reaching "
            f"run_health.sh (sourced from the Dhaka flights .env).")

    ensure_properties(token, db_id)
    pages = pages_by_repo_url(token, db_id)
    date_iso = checked[:10]

    updated = missing = 0
    for r in health["results"]:
        if not r.get("repo"):
            continue                      # local-only jobs have no repo row
        page_id = pages.get(f"https://github.com/{GH_USER}/{r['repo']}")
        if not page_id:
            print(f"  (no Notion row for {r['repo']} — monthly sync will add it)")
            missing += 1
            continue
        http("PATCH", f"{NOTION_API}/pages/{page_id}", token, {"properties": {
            "Health": {"select": {"name": "✅ Healthy" if r["ok"] else "❌ Failing"}},
            "Health checked": {"date": {"start": date_iso}},
            "Health note": {"rich_text": [{"type": "text",
                                           "text": {"content": r["detail"][:1900]}}]},
        }})
        updated += 1
        print(f"  {'✅' if r['ok'] else '❌'} {r['repo']}: {r['detail']}")
    print(f"Notion updated: {updated} rows ({missing} without rows yet), checked {checked}")


if __name__ == "__main__":
    main()
