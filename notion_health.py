#!/usr/bin/env python3
"""Stamp the weekly fleet-health results (health.json, pushed by the Mac's
Sunday-5am fleet_health.py run) onto the Notion repos table.

Adds/updates three properties per matching repo row:
  Health         (select: ✅ Healthy / ❌ Failing)
  Health checked (date)
  Health note    (rich_text — the probe detail line)

Runs in GitHub Actions (health-to-notion.yml, Sundays 13:00 UTC) with the same
NOTION_TOKEN / NOTION_DATABASE_ID secrets sync.py already uses. Stdlib only.
Fails loudly (nonzero exit → Actions email) if health.json is stale, so a dead
Mac-side checker can't rot silently.
"""

import datetime
import json
import os
import sys
import urllib.request

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
GH_USER = "jalalchowdhury1"
STALE_DAYS = 8


def http(method, url, token, body=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


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
    age_days = (datetime.datetime.now()
                - datetime.datetime.strptime(checked[:10], "%Y-%m-%d")).days
    if age_days > STALE_DAYS:
        print(f"FATAL: health.json is {age_days} days old — the Mac-side "
              f"fleet_health run has stopped. Investigate com.jalal.fleet-health.")
        sys.exit(1)

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
