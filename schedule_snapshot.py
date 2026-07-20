#!/usr/bin/env python3
"""Snapshot the Mac mini's actual job schedule into schedule.json.

Ground truth, not documentation: reads ~/Library/LaunchAgents/com.jalal.*.plist,
`crontab -l`, and Time Machine's AutoBackup flag. A weekly GitHub Action
(notion_schedule.py in health.yml) then mirrors schedule.json into the Notion
"Mac Mini Schedule" table, so the table can never drift from reality.

Runs ON THE MAC from run_health.sh (launchd com.jalal.fleet-health, Sundays
5:00 AM) right after fleet_health.py. Commits+pushes schedule.json ONLY when
the job list actually changed, so quiet weeks make no commits.

Human-facing text (title / what-it-does / logs / notes) for KNOWN jobs lives in
CATALOG below — add an entry when adding a launchd job. Unknown jobs still get
a row automatically, flagged "needs description", so nothing new can hide.

Stdlib only.
"""

import datetime
import glob
import json
import os
import plistlib
import subprocess

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(REPO_DIR, "schedule.json")
LAUNCH_AGENT_GLOB = os.path.expanduser("~/Library/LaunchAgents/com.jalal.*.plist")

WEEKDAYS = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
            4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}

# ── human-facing text for known jobs (key = launchd label / special id) ──────
CATALOG = {
    "com.jalal.dhaka-flights": {
        "title": "Dhaka flights scraper",
        "what": "Scrapes Google Flights (BOS→DAC/BKK + Singapore-detour variant), publishes results site, Telegrams on crash",
        "logs": "~/PycharmProjects/Dhaka flights/cron.log",
        "notes": "Retry slots no-op after success via .last_run_date stamp; refuses to start after 5:30 AM"},
    "com.jalal.carmax": {
        "title": "CarMax scraper",
        "what": "Best-value top-trim ICE-SUV finder (11–16k mi) with live KBB/Edmunds valuation of top 3",
        "logs": "~/PycharmProjects/carmax-scraper/cron.log",
        "notes": "Runs in parallel with Dhaka scraper (isolated browse sessions); same stamp-based retry no-op"},
    "com.jalal.t7-drive-sync": {
        "title": "T7 drive sync",
        "what": "Syncs files to the T7Files volume on the Samsung T7 drive",
        "logs": "/tmp/t7-drive-sync.log",
        "notes": "Fleet health probes its launchd exit code weekly"},
    "com.jalal.fleet-health": {
        "title": "Fleet health check (all repos)",
        "what": "13 data-level probes across every automation (scrapers, GH Actions repos, live sites) → Telegram digest, commits health.json + schedule.json",
        "logs": "~/PycharmProjects/github-notion-sync/health.log",
        "notes": 'The weekly "is everything working" check. Companion GitHub Action stamps results onto the GitHub Repos table.'},
    "com.jalal.keepawake": {
        "title": "Keep-awake",
        "what": "Prevents system sleep (display may still sleep) so the midnight jobs always fire",
        "logs": "—",
        "notes": "KeepAlive restarts it if it dies; starts on login/reboot"},
    "com.jalal.supervisor": {
        "title": "Supervisor (mission watcher)",
        "what": "Watches Google Drive supervisor/inbox for MISSION.md files and runs an autonomous aider build loop; pings Telegram",
        "logs": "~/supervisor/logs/supervisor.log",
        "notes": "Idle unless a mission file is dropped in the inbox"},
    "timemachine": {
        "title": "Time Machine backup",
        "what": "Backs up the Mac to the encrypted T7Backup volume on the Samsung T7",
        "logs": "tmutil latestbackup / System Settings",
        "notes": "T7 must stay plugged in. TM cannot back up the T7 itself — photos' 2nd copy is iCloud only."},
}

# Cron lines matched by substring → catalog entry (+ forced Frequency label).
CRON_CATALOG = [
    (("Dhaka flights", "run_daily.py"), {
        "title": "Legacy Dhaka cron (8 AM)",
        "frequency": "Legacy",
        "what": "Pre-launchd leftover of the Dhaka scraper schedule",
        "logs": "~/PycharmProjects/Dhaka flights/cron.log",
        "notes": 'Harmless — .last_run_date stamp makes it log "Already ran today, skipping" — but it bypasses run_daily.sh\'s 5:30 AM guard on failure days. Candidate for removal (crontab -e).'}),
]

# Rows that describe scheduled work NOT visible from this Mac (cloud companions).
STATIC_JOBS = [
    {"key": "gh:health.yml",
     "title": "Notion health stamping",
     "when": "Sunday 9:07 AM (13:07 UTC)",
     "frequency": "Weekly",
     "mechanism": "GitHub Actions health.yml in github-notion-sync (cloud, not the Mac)",
     "what": "Reads health.json + schedule.json pushed by the 5 AM fleet check; stamps the GitHub Repos table and rebuilds the Mac Mini Schedule table",
     "logs": "gh run list -w health.yml",
     "notes": "Fails loudly if health.json is >8 days stale (i.e., the Mac-side check stopped running)"},
]


# ── schedule derivation ─────────────────────────────────────────────────────

def fmt_time(hour, minute):
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {ampm}"


def describe_calendar(interval):
    """StartCalendarInterval (dict or list of dicts) → (when, frequency)."""
    slots = interval if isinstance(interval, list) else [interval]
    times = [fmt_time(s.get("Hour", 0), s.get("Minute", 0)) for s in slots]
    weekdays = [s["Weekday"] for s in slots if "Weekday" in s]
    if weekdays:
        days = ", ".join(dict.fromkeys(WEEKDAYS.get(d, f"day {d}") for d in weekdays))
        return f"{days} {times[0]}", "Weekly"
    when = times[0]
    if len(times) > 1:
        when += f" (retries {', '.join(times[1:])})"
    return when, "Daily"


def launchd_jobs():
    jobs = []
    for path in sorted(glob.glob(LAUNCH_AGENT_GLOB)):
        with open(path, "rb") as f:
            plist = plistlib.load(f)
        label = plist.get("Label", os.path.basename(path)[:-6])
        args = plist.get("ProgramArguments", [])
        target = next((a for a in args if not a.startswith("/bin/") and
                       not a.endswith(("bash", "python", "python3"))), "?")
        cal = plist.get("StartCalendarInterval")
        if cal:
            when, freq = describe_calendar(cal)
        elif plist.get("KeepAlive") or plist.get("RunAtLoad"):
            when, freq = "—", "Always on"
        else:
            when, freq = "on demand", "Always on"
        meta = CATALOG.get(label, {})
        jobs.append({
            "key": label,
            "title": meta.get("title", label),
            "when": when,
            "frequency": freq,
            "mechanism": f"launchd {label} → {target.replace(os.path.expanduser('~'), '~')}",
            "what": meta.get("what", "🆕 New job — needs description (add to CATALOG in schedule_snapshot.py)"),
            "logs": meta.get("logs", plist.get("StandardOutPath", "—").replace(os.path.expanduser("~"), "~")),
            "notes": meta.get("notes", ""),
        })
    return jobs


def cron_jobs():
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                         timeout=15).stdout
    jobs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 6:
            continue
        minute, hour, dom, month, dow = fields[:5]
        command = " ".join(fields[5:])
        if minute.isdigit() and hour.isdigit() and (dom, month, dow) == ("*", "*", "*"):
            when, freq = fmt_time(int(hour), int(minute)), "Daily"
        else:
            when, freq = f"cron: {' '.join(fields[:5])}", "Daily"
        meta = next((m for subs, m in CRON_CATALOG
                     if all(s in line for s in subs)), {})
        jobs.append({
            "key": f"cron:{' '.join(line.split())}",
            "title": meta.get("title", f"cron job ({when})"),
            "when": when,
            "frequency": meta.get("frequency", freq),
            "mechanism": f"crontab → {command[:120].replace(os.path.expanduser('~'), '~')}",
            "what": meta.get("what", "🆕 New cron entry — needs description (add to CRON_CATALOG in schedule_snapshot.py)"),
            "logs": meta.get("logs", "—"),
            "notes": meta.get("notes", ""),
        })
    return jobs


def timemachine_job():
    out = subprocess.run(
        ["defaults", "read", "/Library/Preferences/com.apple.TimeMachine", "AutoBackup"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    if out != "1":
        return []
    meta = CATALOG["timemachine"]
    return [{"key": "timemachine", "title": meta["title"],
             "when": "Every hour", "frequency": "Hourly",
             "mechanism": "macOS backupd (AutoBackup on)",
             "what": meta["what"], "logs": meta["logs"], "notes": meta["notes"]}]


# ── write + publish ─────────────────────────────────────────────────────────

def build_jobs():
    return launchd_jobs() + cron_jobs() + timemachine_job() + STATIC_JOBS


def main():
    jobs = build_jobs()
    print(f"=== Schedule snapshot: {len(jobs)} jobs ===")
    for j in jobs:
        print(f"  {j['frequency']:<9} {j['when']:<35} {j['title']}")

    try:
        old = json.load(open(SCHEDULE_FILE))["jobs"]
    except Exception:                        # noqa: BLE001 — first run / bad file
        old = None
    if jobs == old:
        print("No schedule changes — nothing to commit.")
        return

    with open(SCHEDULE_FILE, "w") as f:
        json.dump({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                   "jobs": jobs}, f, indent=1, ensure_ascii=False)

    def git(*args):
        return subprocess.run(["git", "-C", REPO_DIR] + list(args),
                              capture_output=True, text=True, timeout=60)
    git("add", "schedule.json")
    c = git("commit", "-m", f"Schedule snapshot: {datetime.date.today().isoformat()}")
    if c.returncode == 0:
        p = git("push")
        print("schedule.json pushed" if p.returncode == 0
              else f"WARN: push failed: {p.stderr.strip()[:150]}")
    elif "nothing to commit" not in c.stdout:
        print(f"WARN: commit failed: {c.stderr.strip()[:150]}")


if __name__ == "__main__":
    main()
