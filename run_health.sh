#!/bin/bash
# launchd wrapper for the DAILY fleet health check (5:00 AM, plus a 6:30 AM
# retry slot that no-ops once today's digest has been delivered — everything
# settled before the 7 AM YNAB brief / wake-up).
source /Users/jalalchowdhury/.bash_profile 2>/dev/null || true
export HOME=/Users/jalalchowdhury USER=jalalchowdhury
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Developer/CommandLineTools/usr/bin:$PATH"
cd /Users/jalalchowdhury/PycharmProjects/github-notion-sync

# Daily runs would grow health.log forever — truncate IN PLACE (same inode;
# launchd's O_APPEND fd keeps working) when it passes ~400 KB.
if [ -f health.log ] && [ "$(wc -c < health.log)" -gt 400000 ]; then
  KEEP="$(tail -n 800 health.log)"
  printf '%s\n' "$KEEP" > health.log
fi

# Telegram creds shared with the trip tracker (same chat)
set -a; source "/Users/jalalchowdhury/PycharmProjects/Dhaka flights/.env" 2>/dev/null; set +a

# voices-bot's OWN bot token (@MainJ_bot), read for the telegram_webhook probe.
# Deliberately NOT sourced with `set -a; source .../voices-bot/.env` — that file
# also defines TELEGRAM_TOKEN and would clobber the digest sender above, so
# fleet-health would report on itself through the wrong bot. Extracted by name.
export VOICES_BOT_TOKEN="$(grep '^TELEGRAM_TOKEN=' \
  /Users/jalalchowdhury/PycharmProjects/voices-bot/.env 2>/dev/null | cut -d= -f2-)"

# Same by-name extraction for the three bots rostered 2026-08-25. EVERY one of
# these files defines its own TELEGRAM_TOKEN, so sourcing any of them wholesale
# would clobber the digest sender above and fleet-health would report on itself
# through the wrong bot. Never `set -a; source` a bot .env here.
export ZINGER_BOT_TOKEN="$(grep '^TELEGRAM_TOKEN=' \
  /Users/jalalchowdhury/PycharmProjects/.secrets/telegram.env 2>/dev/null | cut -d= -f2-)"
export SCHOOL_BOT_TOKEN="$(grep '^TELEGRAM_TOKEN=' \
  /Users/jalalchowdhury/PycharmProjects/aoife-school-bot/.env 2>/dev/null | cut -d= -f2-)"
export MILESTONES_BOT_TOKEN="$(grep '^TELEGRAM_TOKEN=' \
  /Users/jalalchowdhury/PycharmProjects/aoife-milestones-bot/.env 2>/dev/null | cut -d= -f2-)"

# From 6 AM on this invocation is the retry slot: fleet_health.py exits early
# if the 5 AM run already sent today's digest, or backs off if that run is
# still going (lock file). (Manual runs: call `python3 fleet_health.py`
# directly — no flag, always runs.)
EXTRA=""
if (( 10#$(date +%H) >= 6 )); then EXTRA="--retry-slot"; fi
python3 fleet_health.py $EXTRA
# Snapshot the Mac's actual job schedule (launchd/cron/Time Machine) →
# schedule.json; commits+pushes only when the job list changed.
python3 schedule_snapshot.py
