#!/usr/bin/env python3
"""Snapshot the Mac mini's actual job schedule into schedule.json.

Ground truth, not documentation: reads ~/Library/LaunchAgents/com.jalal.*.plist,
`crontab -l`, and Time Machine's AutoBackup flag. A daily GitHub Action
(notion_schedule.py in health.yml) then mirrors schedule.json into the Notion
"Mac Mini Schedule" table, so the table can never drift from reality.

Runs ON THE MAC from run_health.sh (launchd com.jalal.fleet-health, daily
5:00 AM + 6:30 AM retry slot) right after fleet_health.py. Commits+pushes
schedule.json ONLY when the job list actually changed, so quiet days make no
commits.

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
    "com.jalal.aoife-library-reminder": {
        "title": "Aoife library holds — 4-week habit reminder",
        "what": "Every-3-days 07:05 Telegram nudge (launchd fires daily; the script only sends on day 1,4,7…28) to the 📡 alerts chat reminding Jalal the concierge can place/swap Warwick library holds for Aoife (project memory project-aoife-library-holds). Runs 2026-09-02 → 2026-09-29 then unloads itself",
        "logs": "~/Library/Logs/aoife-library-reminder.log",
        "notes": "TEMPORARY by design — a habit-forming nudge, not a monitor. Deliberately daytime (07:05, next to the 7:00 budget brief) because a human has to read it. Self-terminates after Sep 29 2026 via launchctl bootout inside the script; STOP PHRASE from Jalal = \"stop the library reminders\" → run `aoife-library-reminder.sh stop` (touches ~/.local/state/aoife-library-reminder.stop + bootout); expect Frequency=Removed after that. Do not add a fleet-health probe"},
    "com.jalal.money-radar-reminder": {
        "title": "Money Radar site — every-3-days green-light nag",
        "what": "Every-3-days 07:05 Telegram nudge (launchd fires daily; the script sends on day 1, 4, 7 …; Jalal chose the cadence 2026-09-02) to the 📡 alerts chat until Jalal green-lights or parks the Money Radar maintenance site (plan: Notion page 3cf24bb1172e814cb5bee0e0d0ccf2fa + ~/PycharmProjects/money-radar/PLAN.md). Runs 2026-09-02 → 2026-09-30 then unloads itself",
        "logs": "~/Library/Logs/money-radar-reminder.log",
        "notes": "TEMPORARY by design — a decision nag, not a monitor. Deliberately daytime (07:05, same slot as the library reminder) because a human has to read it. STOP PHRASE from Jalal = \"stop the tracker reminders\" → run `money-radar-reminder.sh stop` (touches ~/.local/state/money-radar-reminder.stop + bootout); GREEN-LIGHT phrase = \"build the money radar\". Expect Frequency=Removed after either. Do not add a fleet-health probe"},
    "com.jalal.mac-audit": {
        "title": "Mac security audit — nightly posture diff",
        "what": "Snapshots the Mac's security + hygiene state (SIP/FileVault/Gatekeeper/firewall, MDM, LaunchDaemons + LaunchAgents, privileged helpers, login items, cron, LAN-exposed listeners, DNS, /etc/hosts, admins and local users, SSH authorized keys, sudoers.d, TCC privacy grants, system extensions, third-party kexts, root processes, installed apps, Chrome extension IDs + their risky permissions, sustained-CPU hogs, disk space), diffs against last night and Telegrams ONLY when something changed. No collector count here on purpose — it grows, and a number in prose goes stale silently (see mac_audit/collect.py COLLECTORS)",
        "logs": "~/PycharmProjects/mac-audit/cron.log",
        "notes": "03:10 with a 04:10 retry — both deliberately BEFORE the 5:00 fleet check so a failed audit is caught the same morning. SILENT BY DESIGN: no message means nothing changed, so the fleet-health local_stamp probe on state/last_success is the only proof it is alive. Severity drives loudness — CRITICAL (protection switched off, new root LaunchDaemon, new admin, new SSH key) notifies; INFO (a new Chrome extension) is a quiet ping. `tcc_allowed` reads as 'still unreadable' every night because a launchd-spawned bash has no Full Disk Access; that is reported, never silently dropped, and granting FDA to /bin/bash would restore it at the cost of giving every shell script full-disk rights. root_processes is MONOTONIC (union of everything ever seen) because OneDrive's updater daemon is only intermittently running and a plain set-diff flapped nightly. After deliberately changing the Mac's posture, run `uv run mac-audit run` once so the change lands in the baseline instead of ambushing you at 3 AM"},
    "com.jalal.nuts-radar": {
        "title": "NUTS Catalyst Radar — daily catalysts",
        "what": "Rebuilds catalysts.json for nuts-radar.vercel.app from official calendars (FRED release dates, the Fed's FOMC calendar, Nasdaq earnings), deploys it, then Telegrams the day's events plus the link to the 📡 alerts chat",
        "logs": "~/PycharmProjects/nuts-radar/job/run.log",
        "notes": "04:30, deliberately BEFORE the 5:00 fleet check so a failed build is caught the same morning (it was 05:45 first, which put it after the check and made the 6:30 retry slot useless — fleet_health exits early once the digest has gone). Deterministic on purpose — no language model touches it, because an invented earnings date on a page read before the open is worse than an empty section. Build → deploy → message, in that order, so the link already shows today's rows. A failed build leaves yesterday's file and sends nothing; fleet-health's nuts_radar probe then calls the staleness (limit 30 h, which tolerates the 5:00 check seeing a 23 h-old file). deploy.sh always content-stamps the assets first — a cached assets/tree.js would self-check the OLD tree shape and pass silently"},
    "com.jalal.dhaka-flights": {
        "title": "Dhaka flights scraper",
        "what": "Scrapes Google Flights (BOS→DAC/BKK + Singapore-detour variant), publishes results site, Telegrams on crash",
        "logs": "~/PycharmProjects/Dhaka flights/cron.log",
        "notes": "Retry slots no-op after success via .last_run_date stamp; refuses to start after 5:30 AM"},
    "com.jalal.dhaka-hotels": {
        "title": "Dhaka hotel award-rate refresh",
        "what": "Scrapes the IST/SIN card-play shortlist (19 properties since 2026-08-22) and republishes site/hotel_rates.json for the trip site's Stays table",
        "logs": "~/PycharmProjects/Dhaka flights/cron.log",
        "notes": "5:00 AM is the WAKE time — the job then sleeps a random 0-35 min, so it actually runs 05:00-05:35 (a fixed nightly cadence is itself a bot signature; never jitter EARLIER, that walks into the 04:00 flight slot). Because of that it now ALWAYS finishes after the 5:00 fleet check, which therefore grades YESTERDAY's file — by design, and why that probe allows 96 h. Own browser identity (BROWSE_SESSION=hotels); stands down if a flight run is still active, which is also what keeps the two jobs off each other's shared git index. At 19 properties (~5.7 min/run) Browserbase's 60 free min cover ~10 nights; the other ~20 run on local Chrome, paced and scattered (8-20 s between searches). Jalal chose to stay on the free tier 2026-08-22; the $20 Developer plan would make it all-remote"},
    "com.jalal.carmax": {
        "title": "CarMax scraper",
        "what": "Best-value top-trim ICE-SUV finder (11–16k mi) with live KBB/Edmunds valuation of top 3",
        "logs": "~/PycharmProjects/carmax-scraper/cron.log",
        "notes": "Runs in parallel with Dhaka scraper (isolated browse sessions); same stamp-based retry no-op"},
    "com.jalal.t7-drive-sync": {
        "title": "T7 drive sync",
        "what": "Syncs files to the T7Files volume on the Samsung T7 drive",
        "logs": "/tmp/t7-drive-sync.log",
        "notes": "Fleet health probes its launchd exit code daily"},
    "com.jalal.fleet-health": {
        "title": "Fleet health check (all repos)",
        "what": "Data-level probes across every automation (scrapers, GH Actions repos, live sites, webhook bots) → Telegram digest, commits health.json + schedule.json. Deliberately NO probe count here: it grows as repos are rostered and a hardcoded number goes stale silently — read len(FLEET) in fleet_health.py for the live figure",
        "logs": "~/PycharmProjects/github-notion-sync/health.log",
        "notes": 'The daily "is everything working" check (6:30 AM slot is a no-op retry; all settled before wake-up). One-line ✅ when all healthy; full paste-to-Claude diagnostics when not. Companion daily GitHub Action stamps results onto the GitHub Repos table. Governing rule since the 2026-08-06 audit: A GREEN CHECK MUST PROVE THE PRIMARY PATH RAN — if a fallback can satisfy a probe it is a false negative.'},
    "com.jalal.mental-models-backstop": {
        "title": "Mental-models cron-miss backstop",
        "what": "6:00 AM check: if results/daily/$TODAY.json is absent from the mental-models repo (GitHub's 05:10 UTC cron dropped the run), dispatches daily.yml via gh. The artifact check is the dedupe guard — the workflow's own skip-guard covers schedule events only, so a dispatch always runs; the guard still no-ops a LATE cron that fires after a backstop dispatch",
        "logs": "~/Library/Logs/mental-models-backstop.log",
        "notes": "Added 2026-08-28 after GitHub Actions schedule proved unreliable for this repo (fired +31m, +34m, +11h14m late, then never on 08-28). This is the delivery guarantee; GitHub's cron is now best-effort first-attempt. Markers MM-BACKSTOP OK/DISPATCHED date=… are date-pinned and graded by fleet-health's log_marker probe (runs after the 5 AM check, so yesterday's marker is the one graded)."},
    "com.jalal.notebooklm-drip": {
        "title": "Gemini Notebook podcast drip",
        "what": "Fires Audio+Video Overview generation (3+3/day free-tier quota) for book notebooks in ~/PycharmProjects/notebooklm-library/index.tsv; backlog CLEARED 2026-08-09 (all 39 have podcast+video) so it now no-ops in seconds and only picks up newly added books",
        "logs": "~/PycharmProjects/notebooklm-library/drip.log",
        "notes": "Kaiser burner account via notebooklm-py CLI. Second slot moved 4 PM → 11:30 PM on 2026-08-09 (quota hedge that never paid off; no jobs in working hours), then 11:30 PM → 12:45 AM on 2026-08-24 when Jalal fixed the fleet window as midnight–7 AM. Still a transient-failure retry, not a quota retry (reset always landed before 4 AM). Idempotent, so a wasted run is free."},
    "com.jalal.keepawake": {
        "title": "Keep-awake",
        "what": "Prevents system sleep (display may still sleep) so the midnight jobs always fire",
        "logs": "—",
        "notes": "KeepAlive restarts it if it dies; starts on login/reboot"},
    "com.jalal.claude-concierge": {
        "title": "Claude concierge (phone remote-control hub)",
        "what": "Keeps a tmux session 'concierge' running `claude remote-control` so Jalal can reach/spawn Claude Code sessions on this Mac from the Claude phone app; knows the 'fresh start' session-cleanup trick (~/.local/bin/fresh-start.sh)",
        "logs": "~/Library/Logs/claude-concierge.log",
        "notes": "Always-on, KeepAlive + 60s self-heal loop; concierge briefing lives in ~/concierge/CLAUDE.md; added 2026-08-27"},
    "com.jalal.supervisor": {
        "title": "Supervisor (mission watcher)",
        "what": "Watches Google Drive supervisor/inbox for MISSION.md files and runs an autonomous aider build loop; pings Telegram",
        "logs": "~/supervisor/logs/supervisor.log",
        "notes": "Idle unless a mission file is dropped in the inbox"},
    "com.jalal.aoife-planner-backup": {
        "title": "Aoife Planner nightly Drive backup",
        "what": "Snapshots both aoifes-schedule KV blobs (weekly schedule + yearly plan) as dated JSON into Google Drive's 'Aoife Planner Backups' folder",
        "logs": "~/Library/Logs/aoife-planner-backup.log",
        "notes": "3:40 AM, ahead of the 5:00 AM fleet check. /api/plan-get deploys 2026-08-18 — expect a nightly 'PLANNER-BACKUP FAIL … plan' line (and no OK marker) before then; fleet_health.py's planner_backup probe has a matching grace period so it doesn't alert on that known gap"},
    "com.jalal.aoife-school-bot-tick": {
        "title": "Aoife school-bot tick (Telegram morning preview / check-in)",
        "what": "Curls aoife-school-bot.vercel.app/api/tick every 30 min so the family Telegram group gets its morning preview (~07:30) and, only when something is still unlogged, one dynamic evening check-in",
        "logs": "~/Library/Logs/aoife-school-bot-tick.log",
        "notes": "07:00–21:30 ET is DELIBERATE and is the one standing exception to the overnight-only house rule: this job exists to talk to the family during the school day, so it cannot run at 3 AM. Slots with nothing to send log 'TICK OK <date> none' and cost one HTTP call; the fleet probe greps 'TICK OK' specifically, because a tick that cannot reach the planner still answers 200 and writes 'TICK FAIL'. The TICK_SECRET is read from the repo's gitignored .env by scripts/tick.sh and passed to curl through a config file on stdin, so it appears in neither the plist nor `ps`."},
    "com.jalal.aoife-gcal-sync": {
        "title": "Aoife school schedule → Google Calendar",
        "what": "One-way nightly publish of the aoifes-schedule planner (weekly template, dated one-offs, travel/off periods) into the shared 'Aoife's School' Google Calendar",
        "logs": "~/Library/Logs/aoife-gcal-sync.log",
        "notes": "4:10 AM, between the 3:40 backup and the 5:00 fleet check. One-way only — events say 'do not edit here', and the sync overwrites anything edited in Google Calendar. It touches ONLY events carrying its own extendedProperties.private.aoifeSync=v1, so the family's own entries on that calendar are safe. Prints one of three 'GCAL-SYNC WAITING …' markers and exits 0 while the Google-side setup is pending, one per step in the order they clear: calendar-api-disabled (enable Google Calendar API in cloud project hoa-tracker-494016 — the service account cannot enable it itself), calendar-not-shared-yet (a calendar named exactly \"Aoife's School\" shared with claude-sheets@hoa-tracker-494016.iam.gserviceaccount.com), write-permission (shared as 'See all event details' instead of 'Make changes to events'). The fleet probe greps 'GCAL-SYNC OK' with a grace period to 2026-08-20, so a still-unshared calendar gets reported rather than sitting silent."},
    "com.jalal.daily-trackers": {
        "title": "Daily Trackers sheet update",
        "what": "Appends nightly rows (Zillow Zestimate, Redfin estimate, MND 30-yr mortgage rate, USD-CAD, USD-BDT) to the 'Automa Data' Google Sheet that feeds the Daily Trackers spreadsheet",
        "logs": "~/PycharmProjects/daily-trackers/cron.log",
        "notes": "3:30 AM + 4:30/5:30 AM retries (3-slot ladder like dhaka/carmax since 2026-08-24 evening); sheet-level dedupe (a tab already holding today's row is skipped, so retries only fill gaps); Zillow via local browse CLI in its own session (BROWSE_SESSION=trackers); refuses to start after 6:15 AM; replaced the Automa Chrome-extension workflows 2026-08-24"},
    "timemachine": {
        "title": "Time Machine backup",
        "what": "Backs up the Mac to the encrypted T7Backup volume on the Samsung T7",
        "logs": "tmutil latestbackup / System Settings",
        "notes": "T7 must stay plugged in. TM cannot back up the T7 itself — photos' 2nd copy is iCloud only."},
    "com.jalal.health-tick": {
        "title": "Health Hub scheduler tick — every 5 min",
        "what": "curls https://jalal-health.vercel.app/api/tick (secret from ~/.config/secrets.env), which sends Jalal's med nags with confirm buttons, eating-window open/warn/close alerts, and fridge prompts via @JalalHealthBot, all state in the shared Upstash KV (health:* keys)",
        "logs": "~/Library/Logs/health-tick.log",
        "notes": "PRIMARY trigger for the health-hub app (repo health-hub, built 2026-08-26); an hourly GH Actions tick.yml is the backstop, so a dead Mac degrades nags to hourly instead of killing them. Idempotent server-side — overlapping ticks can't double-send. Fleet-health grades the loop via last_tick on /api/health (max 3h)."},
    "com.jalal.toolcheck": {
        "title": "CLI toolbox health check — weekly",
        "what": "Runs `toolcheck` (~/.local/bin/toolcheck), which functionally exercises the whole local CLI toolbox — pandoc, ripgrep, jq, fd, duckdb, sqlite3, htmlq, ghostscript, poppler, qpdf, img2pdf, tesseract, ocrmypdf, pdf2odt, LibreOffice headless, ffmpeg, exiftool, the arm64 PDF-signing venv, plus gh and notebooklm auth. Real round-trips rather than --version checks: it OCRs a rendered image and reads the text back, converts docx to pdf, stamps a signature onto a PDF, queries a CSV. Telegrams the alerts chat ONLY on failure",
        "logs": "~/Library/Logs/toolcheck.log",
        "notes": "SILENT BY DESIGN — no message means every tool passed, so the log is the only proof it ran. WEEKLY not nightly on purpose: these tools barely change, so nightly would be ~365 runs a year to catch maybe one event, and it spawns headless LibreOffice + an OCR pass each time. Added 2026-08-25 after PDF signing was found SILENTLY BROKEN for weeks — a Rosetta arch mismatch (Antigravity's terminal launched an Intel bash, so the venv's universal python loaded x86_64 and refused the arm64 PyMuPDF wheel); nothing would ever have surfaced it. Fires 04:40 Sunday, deliberately before the 05:00 fleet check. The runner reads TELEGRAM_TOKEN/TELEGRAM_CHAT_ID from Dhaka flights/.env BY NAME and must NEVER source it wholesale — that .env also defines BROWSERBASE_API_KEY, and exporting it would contaminate the very check that asserts Browserbase stays project-scoped. Baseline: 26 pass / 0 fail / 1 skip (twitter-cli is skipped because a live probe pops a Chrome Safe Storage Keychain modal at whoever is sitting there)"},
}

# Cron lines matched by substring → catalog entry (+ forced Frequency label).
# The legacy 8 AM Dhaka cron was DISABLED 2026-08-24 (commented out in the
# crontab, backup line preserved in the comment itself) during the fleet-window
# cleanup: it bypassed run_daily.sh's 5:30 AM guard on failure days. Its
# catalog entry was removed with it; cron_jobs() skips '#' lines.
CRON_CATALOG = []

# Rows that describe scheduled work NOT visible from this Mac (cloud companions).
STATIC_JOBS = [
    {"key": "gh:health.yml",
     "title": "Notion health stamping",
     "when": "9:07 AM (13:07 UTC)",
     "frequency": "Daily",
     "mechanism": "GitHub Actions health.yml in github-notion-sync (cloud, not the Mac)",
     "what": "Reads health.json + schedule.json pushed by the 5 AM fleet check; stamps the GitHub Repos table and rebuilds the Mac Mini Schedule table",
     "logs": "gh run list -w health.yml",
     "notes": "Dead-Mac watchdog: fails loudly if health.json is >2 days stale (i.e., the Mac-side check stopped running)"},
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
    # A job that polls (aoife-school-bot-tick: 30 slots, 07:00–21:30) would
    # otherwise render as a 30-time "(retries …)" wall in the Notion table.
    # Evenly spaced slots collapse to the cadence + window instead — same
    # ground truth, one readable line. 4+ slots so a real 2-3 slot retry
    # ladder (carmax 0:00/2:00/4:00) still reads as retries, which it is.
    mins = sorted(s.get("Hour", 0) * 60 + s.get("Minute", 0) for s in slots)
    gaps = {b - a for a, b in zip(mins, mins[1:])}
    if len(mins) >= 4 and len(gaps) == 1:
        step = gaps.pop()
        span = f"{fmt_time(mins[0] // 60, mins[0] % 60)}–{fmt_time(mins[-1] // 60, mins[-1] % 60)}"
        every = f"{step} min" if step < 60 else f"{step // 60} h"
        return f"every {every}, {span}", "Daily"
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
