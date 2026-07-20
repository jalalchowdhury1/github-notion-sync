#!/usr/bin/env python3
"""Mirror schedule.json (pushed by the Mac's schedule_snapshot.py) into the
Notion "Mac Mini Schedule" table, so the table always matches what is actually
scheduled on the Mac.

Runs in GitHub Actions (health.yml, Sundays 13:07 UTC) with NOTION_TOKEN plus
NOTION_SCHEDULE_DB_ID (the schedule database's UUID — a separate secret from
the repos table's NOTION_DATABASE_ID). Stdlib only.

Rules, mirroring sync.py's conventions:
  - Upsert keyed by the hidden "Key" rich_text column (launchd label /
    "cron:<line>" / "timemachine" / "gh:..."); rows created before the Key
    column existed are matched by Job title once, then stamped with a Key.
  - "Notes" is written ONLY when a row is first created — manual edits survive.
  - Jobs that vanished from the Mac get Frequency = "Removed" (soft delete,
    row and Notes survive).
"""

import json
import os
import urllib.request

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def http(method, url, token, body=None):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode())


def rt(text):
    return {"rich_text": [{"type": "text", "text": {"content": str(text)[:1900]}}]}


def ensure_key_property(token, db_id):
    _, db = http("GET", f"{NOTION_API}/databases/{db_id}", token)
    if "Key" not in db.get("properties", {}):
        http("PATCH", f"{NOTION_API}/databases/{db_id}", token,
             {"properties": {"Key": {"rich_text": {}}}})
        print("added property: Key")


def query_pages(token, db_id):
    """Return (by_key, by_title, frequency_by_page_id) for all rows."""
    by_key, by_title, freq = {}, {}, {}
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        _, data = http("POST", f"{NOTION_API}/databases/{db_id}/query", token, body)
        for page in data.get("results", []):
            props = page.get("properties", {})
            key = "".join(t.get("plain_text", "")
                          for t in props.get("Key", {}).get("rich_text", []))
            title = "".join(t.get("plain_text", "")
                            for t in props.get("Job", {}).get("title", []))
            sel = props.get("Frequency", {}).get("select") or {}
            freq[page["id"]] = sel.get("name", "")
            if key:
                by_key[key] = page["id"]
            if title:
                by_title.setdefault(title, page["id"])
        if not data.get("has_more"):
            return by_key, by_title, freq
        cursor = data.get("next_cursor")


def job_props(job):
    return {
        "Job": {"title": [{"type": "text", "text": {"content": job["title"][:1900]}}]},
        "Key": rt(job["key"]),
        "When (ET)": rt(job["when"]),
        "Frequency": {"select": {"name": job["frequency"]}},
        "Mechanism": rt(job["mechanism"]),
        "What it does": rt(job["what"]),
        "Logs": rt(job["logs"]),
    }


def main():
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_SCHEDULE_DB_ID"]

    jobs = json.load(open("schedule.json"))["jobs"]
    ensure_key_property(token, db_id)
    by_key, by_title, freq = query_pages(token, db_id)

    created = updated = 0
    live_ids = set()
    for job in jobs:
        page_id = by_key.get(job["key"]) or by_title.get(job["title"])
        props = job_props(job)
        if page_id:
            http("PATCH", f"{NOTION_API}/pages/{page_id}", token,
                 {"properties": props})
            updated += 1
            print(f"  ~ {job['title']}")
        else:
            props["Notes"] = rt(job["notes"])   # Notes only on create — manual edits survive
            _, page = http("POST", f"{NOTION_API}/pages", token,
                           {"parent": {"database_id": db_id}, "properties": props})
            page_id = page["id"]
            created += 1
            print(f"  + {job['title']}")
        live_ids.add(page_id)

    removed = 0
    for page_id, frequency in freq.items():
        if page_id not in live_ids and frequency != "Removed":
            http("PATCH", f"{NOTION_API}/pages/{page_id}", token,
                 {"properties": {"Frequency": {"select": {"name": "Removed"}}}})
            removed += 1
    print(f"Schedule table: created={created} updated={updated} marked_removed={removed}")


if __name__ == "__main__":
    main()
