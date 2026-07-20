#!/bin/bash
# launchd wrapper for the Sunday-5am fleet health check.
source /Users/jalalchowdhury/.bash_profile 2>/dev/null || true
export HOME=/Users/jalalchowdhury USER=jalalchowdhury
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Developer/CommandLineTools/usr/bin:$PATH"
cd /Users/jalalchowdhury/PycharmProjects/github-notion-sync
# Telegram creds shared with the trip tracker (same chat)
set -a; source "/Users/jalalchowdhury/PycharmProjects/Dhaka flights/.env" 2>/dev/null; set +a
python3 fleet_health.py
# Snapshot the Mac's actual job schedule (launchd/cron/Time Machine) →
# schedule.json; commits+pushes only when the job list changed.
python3 schedule_snapshot.py
