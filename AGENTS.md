# AGENTS.md — github-notion-sync

> **Single source of truth for anyone (human or AI) touching this repo.** Read it fully
> before changing code. `README.md` is the short human/GitHub landing page. If something
> here is wrong, fix *this* file. **This repo is PUBLIC** — see §1.6 before adding anything.
>
> Structure: §1 says how things work **today**. The "why" and dated incidents live in
> §1.8, §4 and the history sections at the end. Where old prose conflicts with §1, §1 wins.

---

## 1. What this is

Three jobs share this repo:

| Job | Runs where / when | Files |
|---|---|---|
| **Repo → Notion sync** (the original) | GitHub Actions, monthly `0 13 1 * *` | `sync.py`, `sync.yml`, `test_sync.py` |
| **Fleet health**: does every automation on the Mac and in the cloud still do real work? | Mac launchd `com.jalal.fleet-health`, 05:00 + 06:30; cloud watchdog `health.yml` daily | `fleet_health.py`, `run_health.sh`, `notion_health.py`, `health.yml`, `health.json`, `test_fleet_health.py`, `test_probes.py` |
| **Mac Mini Schedule table**: the Mac's real launchd/cron list, mirrored to Notion | after each fleet run (Mac) + `health.yml` (cloud) | `schedule_snapshot.py`, `schedule.json`, `notion_schedule.py` |

`keepalive.yml` stops GitHub from disabling the scheduled workflows on a quiet repo.

**The one rule behind fleet health:** a green row must prove the job's PRIMARY path did
real WORK recently. Being alive, exiting 0, serving HTTP 200 or having a green CI run is
not enough. That rule was born from the 2026-07 CarMax incident: 17 days of green CI with
zero rows.

### 1.1 Fleet health: a normal day

```
04:30-05:00  jobs run (planner backup 03:40, trackers, gcal, catalysts 04:30 …)
05:00  run_health.sh → fleet_health.py        SILENT. Grades all rows, hands the digest to
                                               health-hub's Silent digest (queued, not sent),
                                               commits + pushes health.json.
06:30  run_health.sh → fleet_health.py --retry-slot
                                               Re-grades everything (the digest mode always
                                               re-runs, so reds that healed by 06:30 go
                                               green), overwrites the queued digest.
                                               loud_alert(): if anything is STILL red, ONE
                                               buzzing Telegram naming the reds.
06:50-10:00 ET  health-hub sends the ⚪ morning card; its "🛠 Fleet health" button
                replays the full digest. Stamps delivered.fleet → /api/health
                digest_fleet_at.
~12:37 UTC  health.yml (One Clock dispatch; GitHub cron 13:07 UTC is the backstop)
            notion_health.py = the CLOUD WATCHDOG. It dies (log + Telegram + red run) when:
              · health.json is > 24 h old          (the Mac or its job is dead)
              · telegram != "sent"                  (the Mac could not deliver)
              · mode "digest" and digest_fleet_at is > 20 h old (the card is not
                going out). A card that is only LATE (10:00 ET window still open,
                stamp exactly one day old) is not judged until the next check.
            Then stamps Health / Health checked / Health note on each repo's Notion row.
```

**Which runs make noise.**
- 05:00 is always silent.
- 06:30 buzzes once per failing row per day (`loud` in health.json), never before
  `LOUD_FROM = 06:25`. A manual re-run later that day repeats nothing.
- Rows that failed only because the checker couldn't look (`probe error: …`, e.g. a
  GitHub outage) are listed as "could not check", never counted as "systems failing".
- The checker crashing is always loud (see exit codes).

**Flags.**
- `run_health.sh` adds `--retry-slot` from 06:00. That makes the run skip when it's
  already done (direct mode, no unbuzzed reds) or back off while a live 05:00 run
  holds `.fleet_health.lock`. A lock only counts while its pid is alive, or, if the
  lock is unreadable, for 2 h.
- `run_health.sh --force` re-grades on purpose during the day. Without it a daytime
  run silently no-ops.

**Exit codes.**

| Code | Who | Meaning | Who alerts |
|---|---|---|---|
| 0 | fleet_health.py | ran (or skipped cleanly) | — |
| 3 | fleet_health.py `cli()` | crashed, or refused to run (roster lint) | it already sent its own 🚨 |
| any other | fleet_health.py | died before its handlers existed (SyntaxError, ImportError, kill) | `run_health.sh` curl-sends 🚨 |
| 1 | notion_health.py | watchdog tripped | it sent its own Telegram; the Actions run goes red |

**health.json** (committed; read by the cloud watchdog):
- `checked`: Mac-local "YYYY-MM-DD HH:MM"
- `telegram`: "sent" means queued **or** sent, so the card check exists
- `telegram_mode`: "digest", "direct" or null
- `loud`: `{date, names}`
- `results[]`: `{name, repo, probe, ok, detail, cfg, failing_since}`

**Digest contract.**
- All green: one line ("✅ … all N systems healthy", plus "· recovered: X" the day after).
- Any red: one pasteable diagnostic block per failure. The block holds:
  - the config line
  - "⏳ failing since"
  - the detail
  - the run URL and failed-step log tail
- The budget is **per failure, never tail-chopped**. Telegram caps at 4096 characters,
  and the old truncation dropped whole systems. A failure name is never dropped; the log
  tail goes first.
- Plain text, no parse_mode, 3 send attempts.

**Probe errors.** A probe RAISES on infra trouble (network, `gh`/`aws` failure, 5xx);
`run_checks` retries it 3× with 20 s pauses. A returned `False` is real signal and is
never retried.

### 1.2 Probe reference (21 types, `PROBE_FNS`)

Contract: `fn(**row) -> (ok, detail)`; raise only for infra trouble.

**Liveness-only** (needs `weak_ok="<why that is enough>"`, or the lint refuses the whole
run):

| Probe | Proves | Key params |
|---|---|---|
| `web_200` | host serves 200 and stays on-host (a login-wall redirect is red) | `url`, `expect_text` |
| `launchd_exit` | last exit 0 (a retired `.plist.retired` reads green "RETIRED — delete this row") | `label` |
| `file_mtime` | file was written recently (0 rows today) | `path`, `max_age_h` |

**Data-level:**

| Probe | Proves | Key params / trap |
|---|---|---|
| `web_fresh` | a live JSON's own stamp is fresh, optionally row counts | `url`, `json_key` (dotted), `max_age_h`, `rows_key`, `min_rows` ({key: floor}). Future stamp = red |
| `web_render` | headless Chrome ran the page's JS; no uncaught error; text visible in `<body>` | `url`, `expect_text` |
| `bot_selftest` | a synthetic message through the bot's real handler produced a reply | `url`, `secret_env` (header only). 5xx = retry |
| `telegram_webhook` | webhook points at the right function; guard rejects unauth POST; no error < 24 h | `token_env`, `expect_url` (no query!), `require_guard` |
| `gh_run` | newest run recent + green + log markers | `repo`, `workflow`, `max_age_h`, `log_grep`, `expect_event`, `no_rescue`, `rescue_max_failures`. See §1.3 |
| `log_marker` | a local log carries today's success marker | `log_path`, `log_grep`, `live_since` |
| `log_block` | markers inside the NEWEST run block of an append-only log | `log_path`, `block_re` (group 1 = stamp), `log_grep`, `fail_grep`, `max_age_h`. For weekly jobs |
| `log_tail` | fresh log whose LAST line is a success shape | `path`, `last_line`, `max_age_h`, `grace_min`, `grace_lines` |
| `local_stamp` | a stamp file holds a recent ISO date | `path`, `max_age_h` |
| `launchd_running` | a KeepAlive daemon has a live pid right now | `label` |
| `planner_backup` | today's dated JSON snapshots parse, and the OK marker is present | `dest_dir`, `names`, `log_path` |
| `rsync_log` | the last rsync block finished with exit 0 and walked ≥ `min_files` regular files | `log_path`, `max_age_h`, `min_files` |
| `cloudwatch_marker` | a Lambda log carries its success marker | `log_group`, `log_grep`, `today_only`; with `today_only=False`: `fail_grep`, `active_window` (UTC, may wrap), newest outcome wins |
| `one_clock_lambda` | EventBridge → gh-dispatcher is alive: pings, dispatches, no unrecovered error | windows/minimums |
| `nuts` | NUTS signal payload: unit test, downloads, price freshness, eval freshness, holding | `url` |
| `nuts_radar` | radar site 200 + repo's `selfcheck.js` passes + catalysts fresh | `url`, `repo_dir`, `catalysts_url` |
| `freshness` | what the SCREEN serves reflects the newest input: the app's `GET /api/freshness` (contract v1, ages in hours only); red when an item's input is past `graceH` and the served copy is older, or `servedAgeH` > `maxAgeH`. The app reports, the probe judges | `url`, `expect_items` |
| `pending_stamp` | a Mac drift checker's JSON stamp: red when the checker stopped (`checkedAt` > `max_check_age_h`) or a change sat unsynced past `pending_grace_h` (aoife calendar copy) | `path`, `max_check_age_h`, `pending_grace_h` |

### 1.3 Markers and date tokens

`log_grep` is one regex or a list; **ALL** must match. Assert that the pipeline RAN
("across N regions"), not that a count was nonzero, or a quiet day false-alarms.

| Token | Expands to | Use when |
|---|---|---|
| `{today}` | today | the job runs BEFORE the 05:00 check (else the yesterday arm hides a failed morning) |
| `{date}` | today\|yesterday | the job runs after 05:00 (its newest output is yesterday's) |
| `{weekday}` | today\|previous weekday | weekday-only jobs (Monday sees Friday) |

- Tokens are expanded by `gh_run`, `log_marker` and `cloudwatch_marker(today_only=True)`
  only, all through `_expand_dates()`.
- Anywhere else a token would be searched for literally and never match, so
  `lint_roster` refuses it (weak_ok does not excuse it). Regex quantifiers like `\d{4}`
  are fine.

**Traps that have bitten, every one of them real:**
- **The Actions echo trap.** Actions prints each step's source into the log (colour
  `\x1b[36;1m`), so `echo "DONE"` matches `DONE` on every run. Anchor markers on the log's
  own timestamp (`\dZ DONE`) or exclude the echoed form.
- **Markers printed before the work.** "Fetching X…" prints even when the fetch fails.
  Grade a line that only prints AFTER success (AM Reads, round 8).
- **Success lines that print on partial failure.** A job that logs "published" after
  half its inputs failed needs `fail_grep` (rubber-band `curve X: FAILED`).
- **The lint rejects vacuous markers.** A `log_grep` that matches `""` or `"zz"` can
  never miss.
- **Verify every marker against a real recent log**
  (`gh run view <id> -R jalalchowdhury1/<repo> --log`). A marker that can never match is
  a permanent false alarm; one that can never miss is decoration.

**gh_run rescues.** A red newest run is forgiven only when a SUCCESS inside `max_age_h`
proves the work happened. With `expect_event`, that success must come via the primary
trigger.
- `no_rescue=True` for workflows whose runs are distinct jobs (AM vs PM snapshot).
- `rescue_max_failures=N` for cyclic jobs. More than N failed runs in the window is
  flapping and stays red.
- A run < 1 h old and still running is ignored.
- A run that is still running and older than that is HUNG.

### 1.4 Adding a row (checklist)

1. Pick the **strongest** probe from §1.2 that grades the job's own output. Use a
   liveness probe only when nothing better exists, and write why in `weak_ok`.
2. Take a marker from a **real** log, printed only after the work succeeded. Check the
   echo trap and prints-before-work.
3. Pick the date token (§1.3) from when the job runs relative to 05:00.
4. Derive `max_age_h` from the schedule. Grade it at BOTH 05:00 and 06:30. Add slack for
   the DST night (+1 h). Measure the worst legitimate gap (weekends, holidays).
5. `repo`: the GitHub repo name if `notion_health` should stamp its Notion row. Else
   `None`. One Notion row per repo, and the **last** result for a repo wins.
6. Prove the row can go red: feed it the failure shape, e.g. in `test_probes.py` or a
   `TestRosterGuards` test. Then run the side-effect-free grade (§1.5) and see it green.
7. **Writer feeds a screen? Grade the screen too** (2026-10-02). If the job writes data
   that a live app later SHOWS (KV doc, report, mission, feed), mark its row
   `feeds_screen: True` and add a `freshness` row for the same repo that reads the app's
   `/api/freshness`. The lint refuses a `feeds_screen` row without one. A `web_fresh`/`nuts`
   row that fetches the SAME file the page renders may stand in: mark it `screen_side: True`.
   Rows match by `screen_repo` (falls back to `repo`), so a writer with `repo: None` or a
   different repo (rubber-band → financial-telegram-bot) still pairs. Spec + judge rule:
   `~/PycharmProjects/FRESHNESS-CONTRACT.md`; `/ship` runs `freshness-check <url>` (same judge).
   `paused_if: {"path": log, "last_line": regex}` (2026-10-03): when the job's own newest log line
   says it is idle on purpose (dhaka-yearly `NIGHTLY IDLE` between trips), the screen row reports
   `PAUSED — …` (ok) instead of a false red; the writer's row still grades the job. Why: the aoife-typing
   coach logged `wrote coach` every 15 min (green) while the screen served the 11 Sep
   mission for three weeks (reader read `at`, writer stamped `generatedAt`).

### 1.5 Testing and dry runs

```sh
python3 -m unittest test_fleet_health test_probes test_sync      # ~170 tests, stdlib, ~20 s
# coverage (optional)
uv run --no-project --with coverage -q python -m coverage run --include=fleet_health.py \
  -m unittest test_fleet_health test_probes && uv run --no-project --with coverage -q python -m coverage report
```

**A side-effect-free live grade** sends nothing and commits nothing:
1. Export the same env as `run_health.sh`: everything up to `EXTRA=""`. It reads each
   token BY NAME.
2. Run `python3 -c "import fleet_health as fh; rs = fh.run_checks(); print([r['name'] for r in rs if not r['ok']])"`.

⚠️ **`python3 fleet_health.py` is NOT a dry run.** It:
- queues or sends the digest
- can buzz after 06:25
- commits and pushes `health.json`

The tests fake `subprocess` and `git`, so they never push.

### 1.6 Secrets and the public repo

- Never put a token, webhook secret or `?s=` query in a roster row or a detail string.
  `telegram_webhook` compares scheme+host+path only and never echoes the query.
- `run_health.sh` pulls each bot's token and secret **by name** with `grep`. Never
  `set -a; source` a bot `.env`: they all define `TELEGRAM_TOKEN` and would hijack the
  digest sender. The full list is in §3's env table.
- The Mac's digest bot and chat, `DIGEST_URL` and `DIGEST_KEY` come from the sourced
  `~/PycharmProjects/Dhaka flights/.env`.
- Before a commit, grep the diff for `bot[0-9]{8,}:`, `ghp_`, `ntn_`, `sk-`, and `?s=`.

### 1.7 Retiring a job or a row

1. `launchctl bootout gui/$(id -u)/<label>`.
2. Rename the plist to `<label>.plist.retired`. Never delete it.
3. The launchd probes now read green "RETIRED — delete this row". The digest shows
   "🗂 retired". Delete the roster row.
4. A row whose job still runs but whose output is proven by another row: delete it and
   say which row covers it (comment in `FLEET`).

### 1.8 Design rules (one line each; the incidents are in §4 and the history sections)

- Grade the PRIMARY path. A backstop carrying a dead primary is red (`expect_event`).
  The financial-telegram-bot Lambda stayed dead for 2 months behind a GHA backstop.
- Grade data, not transport: a stale cached body still returns 200 (NUTS).
- Many rows going stale at once is ONE event (the dispatcher). The digest says so.
- Hourly and 5-minute jobs are graded by the NEWEST outcome, not "any success today".
  One lone trailing failure is noted; two or more are red (ynab-nag).
- Test the door, not the key: prove a guard rejects an unauth POST.
- Infra errors raise and retry. A red is never retried.
- Every way the checker can die ends in a loud Telegram (exit-code table).
- **Producers must say "done" in a way a failure can't fake (round 8b, 2026-09-27).**
  Seven jobs used to report success even when their real work failed. Each now writes
  a proof line that only a clean run can produce, and its row matches that line:

  | job (repo commit) | the proof line or field the row requires |
  |---|---|
  | llm-balance-check (~/.local/bin d003967) | `{date} hh:mm:ss openrouter=N sent=none\|200` |
  | trigger-board (concierge 4df4e16) | `BOARD OK <slot> {date} errors=0` |
  | mac-audit (b6565c2) | stamp written only when the Telegram send succeeds (row unchanged) |
  | carmax (e7d39f5) | stamp written only when Sheet and Telegram both succeed (row unchanged) |
  | health-hub (1b04832) | `/api/health` `send_ok_at` fresh, and newer than `send_error_at` (web_fresh `fail_key`) |
  | defensive-nag (2a3966d) | last line `nag outcome: sent=N failed=0 pending=…` |
  | dhaka-yearly (865b47b) | `data/run-{today_ymd}.log` last line `NIGHTLY OK plans=N problems=0 pushed=<sha> notify=ok\|none` |

  Probe support added for this: `web_fresh` `fail_key`/`fail_note_key` (red when the
  newest attempt failed), and a `{today_ymd}` (YYYYMMDD) token in `log_tail` paths.
- **Login-only sites (2026-09-27).** dhaka-flights and nafis-mortgage sit behind Vercel
  Authentication. `web_fresh` / `web_200` take `bypass_env`: the env var holding that
  project's "Protection Bypass for Automation" secret, sent as the
  `x-vercel-protection-bypass` header. Secrets live in
  `~/PycharmProjects/.secrets/vercel-bypass.env` (mode 600), sourced by run_health.sh.
  A missing secret is a plain red ("<VAR> not set"), and the value never lands in a detail line.
- **AAII moved off the Google Sheet (2026-09-27).** sentiment-scraper is retired (its
  service-account key had leaked); both its rows are replaced by two `/api/aaii` rows on the
  dashboard (freshness of `as_of`, and `source` = aaii.com). AWS schedule
  `one-clock-sentiment-watchdog` set DISABLED (not deleted).
- **Whole-project audit follow-ups (2026-09-27).** Rows tightened: dashboard `stale=0`, Google
  News `GOOGLE NEWS: saved N`, dhaka-yearly accepts `NIGHTLY IDLE`, new mac-heartbeat row.
  Still to add: **milestones recap row on 2 Oct** (`bot_selftest` POST /api/recap with
  X-Selftest; red until then because the Aug proof is missing), and hedgelab
  `"expect_event": "workflow_dispatch"` once the one-clock-dispatcher token covers hedgelab.
  After 4 Jan 2027: retire the v1 dhaka-flights/dhaka-hotels jobs and rows; pause the
  dhaka-yearly live-site row 6-31 Jan (idle nights don't deploy).

Also known and accepted:
- A failed loud send is not retried; the red still sits in the card.
- If the digest hand-off fails at 05:00, the 06:30 run sends a second direct digest.
### 1.9 Mac Mini Schedule table (details)

**Plus a third job (2026-07-20): the self-maintaining "Mac Mini Schedule" Notion table.**
- `schedule_snapshot.py` — runs on the Mac right after `fleet_health.py`
  (same `run_health.sh` wrapper). Reads GROUND TRUTH — every
  `~/Library/LaunchAgents/com.jalal.*.plist` (via plistlib), `crontab -l`,
  and Time Machine's AutoBackup flag — and writes `schedule.json`
  (commits+pushes ONLY when the job list changed; quiet days make no
  commits). Human text for known jobs lives in its `CATALOG` /
  `CRON_CATALOG` dicts; unknown jobs still get a row, flagged
  "🆕 needs description", so nothing new can hide. `STATIC_JOBS` holds
  cloud-side rows (the health.yml stamping job itself).
- `notion_schedule.py` — runs in `health.yml` after `notion_health.py`.
  Mirrors schedule.json into the Notion **Mac Mini Schedule** database
  (secret `NOTION_SCHEDULE_DB_ID` — separate from the repos table's
  `NOTION_DATABASE_ID`). Upserts keyed by hidden `Key` rich_text column
  (launchd label / `cron:<line>` / `timemachine` / `gh:…`); pre-existing
  rows are matched by Job title once, then stamped with a Key. **`Notes`
  is written only on row creation** (manual edits survive — same sacred-Notes
  rule as sync.py). Vanished jobs get `Frequency = Removed` (soft delete).
  **Two traps, both hit for real on 2026-08-16:** (1) because `Notes` is
  create-only, editing a job's `notes` in `CATALOG` does NOT reach a row that
  already exists — the CATALOG text is only ever a seed for NEW rows, so
  correcting an existing row means hand-editing Notion as well (three rows had
  silently rotted: the hotel job's Notes was empty, fleet-health still said
  "weekly" long after it went daily, and T7 still carried a "⚠️ currently
  failing" TCC warning that both its probes had been contradicting for weeks).
  (2) The soft-delete had **never once executed** — no job had ever vanished
  before — and `Removed` was not among the `Frequency` select's options, so the
  first real removal died on `validation_error: Invalid select value`. The
  option now exists (red); if this database is ever rebuilt, recreate it or the
  same first-removal failure returns. `When (ET)` IS overwritten every sync
  (it is derived from the plist), so timing caveats that are not visible to
  launchd — e.g. the hotel job's 0-35 min in-script start jitter — belong in
  `Notes`, never in `When (ET)`.

### 1.10 Repo → Notion sync (details)

A single-file Python script (`sync.py`, stdlib-only — no third-party packages) that
**mirrors the owner's GitHub repos into a Notion database**. On each run it:

1. Lists every repo the authenticated user **owns** (paginated, `affiliation=owner`,
   sorted by `pushed`), **including GitHub-archived repos** (`include_archived=True`).
2. For each repo, fetches the file tree, the README, the timestamp of the last
   **successful** Actions run, and the contents of a few manifest files
   (`package.json`, `pyproject.toml`, `serverless.yml`, etc.).
3. Classifies the **stack** (heuristic, file/name/README based — see §6) and parses
   **runtime versions**, flagging deprecated ones with ⚠️.
4. Generates a 1–2 sentence **description** via the Anthropic API (Claude Haiku,
   `claude-haiku-4-5`), with a heuristic fallback if no key / the call fails.
5. **Upserts** one Notion page (row) per repo, keyed by **Repo URL** (idempotent).
6. Marks rows for repos that vanished from GitHub as **Status = Deleted** (never
   deletes them, so manual `Notes` survive).

**Trigger / where it runs:** GitHub Actions workflow `.github/workflows/sync.yml`,
on a **monthly cron `0 13 1 * *`** (1st of each month, 13:00 UTC = 9am ET / 8am EST),
plus `workflow_dispatch` (Actions tab → "Sync GitHub repos to Notion" → Run workflow).
Runs on `ubuntu-latest`, Python 3.12, 15-min timeout. Nothing is deployed anywhere —
the script just calls the GitHub, Notion, and Anthropic HTTP APIs.

The target Notion database lives under the **💻 Tech & Automation** area (per README);
its UUID is supplied at runtime via the `NOTION_DATABASE_ID` secret (not stored in repo).

**Repo:** `github.com/jalalchowdhury1/github-notion-sync` (public).

---

## 2. Architecture / data flow

```
GitHub Actions (monthly cron, or manual)
        │
        ▼
   python sync.py        (stdlib urllib only — no requirements.txt)
        │
        ├─▶ GitHub API   /user/repos, /git/trees, /contents, /readme, /actions/runs
        │       (paginated; reads file tree + manifests + README + last good CI run)
        │
        ├─▶ classify      detect_stack() + detect_runtimes()  (pure, heuristic)
        │
        ├─▶ Anthropic API /v1/messages  describe_with_claude()  (Claude Haiku)
        │       (falls back to README/GitHub-description heuristic if no key/error)
        │
        └─▶ Notion API    query DB by "Repo URL" → PATCH (update) or POST (create) page
                          → repos gone from GitHub get Status=Deleted (PATCH)
                          → archived repos get Status=Archived, NOT Deleted
```

All HTTP goes through one helper, `http()`, which retries `429/502/503/504` (and network
errors) up to 3× with exponential backoff (`2**attempt` seconds), 30s timeout. 404 is
returned quietly (no stderr noise).

---

## 3. How to run / test / deploy

**This is not "deployed" — it just runs in Actions or locally.** No build step, no
`requirements.txt` (stdlib only). Tests (stdlib `unittest`, no deps, nothing real sent):
`python3 -m unittest test_sync test_fleet_health test_probes -v`.
- `test_fleet_health.py` — digest (budget, correlated staleness, recovered/retired),
  retry slot + lock, roster lint, loud alerts, exit codes, the round-7 probe changes,
  and `TestRosterGuards` (rows that exist because of a real silent failure — add a
  row test whenever an incident adds a row).
- `test_probes.py` — one test per return branch of every other probe (local HTTP
  server + faked `gh`/`aws`/`launchctl`/`node`/Chrome), `run_checks` retries,
  `_telegram_send` routing, date tokens, the notion_health card window.
- `test_sync.py` — `compute_status`'s Active/Stale/Archived labelling. The
  network-touching sync paths are still untested.

### Local run
```sh
export GH_PAT=ghp_...            # or GITHUB_TOKEN (fallback name, see §5)
export NOTION_TOKEN=ntn_...
export NOTION_DATABASE_ID=...    # or NOTION_DATA_SOURCE_ID (fallback name)
export ANTHROPIC_API_KEY=sk-ant-...   # optional; omit to use heuristic descriptions
python sync.py
```
Exit `0` on success, `1` if `GH_PAT`/`NOTION_TOKEN`/`NOTION_DATABASE_ID` are missing.
A missing `ANTHROPIC_API_KEY` only prints a WARN and silently falls back — it is **not**
fatal. Output is verbose progress to stdout ending in
`Done. created=… updated=… marked_deleted=…`.

### CI run / schedule
`.github/workflows/sync.yml` runs `python sync.py` with the four env vars wired from
repo secrets (see §5). Monthly cron `0 13 1 * *` + manual dispatch.

### Env vars / where secrets live
Secrets live in **GitHub Actions repository secrets** (Settings → Secrets and variables →
Actions). Never hardcode any of them — the repo is **public**.

| Var | Required? | Purpose |
|---|---|---|
| `GH_PAT` | yes | GitHub PAT (classic) with `repo` + `read:user` scopes; needed to read **private** repos. Falls back to `GITHUB_TOKEN` if unset. |
| `NOTION_TOKEN` | yes | Notion internal integration token (`ntn_…`); the integration must be shared with the target database. |
| `NOTION_DATABASE_ID` | yes | UUID of the Notion database (parent of the rows). Falls back to `NOTION_DATA_SOURCE_ID` if unset. |
| `ANTHROPIC_API_KEY` | optional | Anthropic API key for Claude-generated descriptions. If absent, descriptions fall back to README/GitHub-description heuristics. |
| `NOTION_SCHEDULE_DB_ID` | yes (health.yml only) | UUID of the **Mac Mini Schedule** Notion database (under 💻 Tech & Automation). Used only by `notion_schedule.py`. |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | strongly recommended (health.yml only) | Lets `notion_health.py` send the dead-Mac / undelivered-digest alert from the cloud. Same bot+chat the Mac uses (`~/PycharmProjects/Dhaka flights/.env`). If unset, the watchdog still fails the run but only GitHub's failure email carries it. |
| `ZINGER_BOT_TOKEN` / `SCHOOL_BOT_TOKEN` / `MILESTONES_BOT_TOKEN` / `HEALTH_BOT_TOKEN` | Mac-side only (`run_health.sh`) | Each bot's own token for its `telegram_webhook` row, grepped BY NAME from that bot's `.env` (health: `~/.config/secrets.env`). Same never-`source` rule as below. |
| `ZINGER_/VOICES_/SCHOOL_/MILESTONES_/HEALTH_WEBHOOK_SECRET` | Mac-side only (`run_health.sh`) | Webhook secrets for the `bot_selftest` rows, sent only as a request header. Never logged, never in the roster. |
| `DIGEST_URL` / `DIGEST_KEY` | Mac-side only (from the sourced `Dhaka flights/.env`) | health-hub Silent digest hand-off (`digest_post`). The cloud watchdog does not use them (it reads `/api/health` unauthenticated). |
| `VOICES_BOT_TOKEN` | Mac-side only (`run_health.sh`) | @MainJ_bot's own token, for the `telegram_webhook` probe. Exported by NAME in `run_health.sh` — **never** `source` voices-bot/.env wholesale, it defines `TELEGRAM_TOKEN` too and would clobber the digest sender, making fleet-health report on itself through the wrong bot. |

---

## 4. Gotchas / hard rules

- **Several probes stale at once is ONE event — the digest now says so
  (`correlated_note`, added 2026-08-27).** Every probe only ever sees its own
  system, so a shared upstream cause reads as N unrelated failures. On
  2026-08-27 GitHub silently dropped ~9 h of scheduled events across the whole
  account (01:02→09:57 UTC); the digest reported leasehackr ×2 + mental-models
  as three separate repo bugs, and the repos were fine. `correlated_note` now
  prefixes the digest when **≥2 `gh_run` probes report *never started*** — the
  exact `no run in …` string, anchored with `_NEVER_STARTED`. Two shapes: same
  repo → "debug them together"; **≥2 repos → suspect GitHub's cron dispatcher
  and check the newest `event=schedule` run ACROSS all repos before touching
  any of them.** Hard rules: (1) a run that STARTED and failed must never count
  — it has a real per-repo cause, and folding it in sends the reader hunting a
  fleet outage that isn't there; (2) the note is reserved out of `room`
  alongside header/footer, so trimming can never eat the one line that
  reframes the whole digest. **Do not "fix" this by tightening `max_age_h`** —
  the 36 h windows deliberately absorb GitHub's cron drift, and today's digest
  under-reported 3-of-8 precisely because only three entries sit at 24 h. The
  correlation line is the fix; the windows are not the bug.

- **`nuts` probe (added 2026-08-25) — grade the SIGNAL, not the transport.**
  NUTS models a live ~$178k Composer symphony, and until 2026-08-25 it was the
  only unwatched link in a chain whose watched ends were green: both consumers
  (`trading-algorithm-`, voices-bot `/nuts`) read its `/evaluate` API and alert
  only on CHANGE, so silence is their normal output. If NUTS freezes, the API
  keeps serving HTTP 200 with a stale cached body, the consumers see "no
  change" and stay quiet, and their green rows prove only that *they* woke up.
  A frozen signal is indistinguishable from a quiet market — which is why
  `web_200` here would be decoration, and `probe_nuts` grades the payload:
  `unit_test.pass` (NUTS's own RSI self-check; fail = DO NOT TRADE = hard red),
  `download_errors` empty, worst `data_quality[*].last_date` ≤ 5 d, and
  `evaluated_at` ≤ 96 h. The threshold table (measured, in the probe docstring)
  is calendar-based on purpose: the first draft's 4 d / 80 h sat *below* the
  Monday-holiday / Good-Friday row (84.4 h) and would have false-alarmed ~6
  mornings a year. Hard rules: **plain GET only, NEVER `?force=true`** (a bare
  GET returns the cached body at zero cost to NUTS — see the read-only rule in
  the memory file `reference-nuts-algo`); **never modify the NUTS repo from
  here**; the site probe targets `nuts-sooty.vercel.app`, NOT `nuts.vercel.app`
  (an unrelated old app that would serve a cheerful 200 forever). Two entries
  for the same reason as voices-bot below — API and frontend fail
  independently — and the site entry's `repo` is `None` for the same
  last-row-wins trap.

- **`nuts_radar` probe (added 2026-08-25) — grade the TREE SHAPE, not uptime.**
  `nuts-radar.vercel.app` answers "if this condition crosses, what does the book
  become?" by re-walking a copy of NUTS's tree *shape* (`assets/tree.js`; it holds
  no thresholds — every number is read live from `/evaluate`). That copy is the
  whole risk: if NUTS's trees are edited, a stale shape keeps confidently
  reporting the OLD destinations — the same silent divergence that had
  trading-algorithm- reporting BIL while NUTS was TQQQ. The page self-checks on
  every load and hides all consequences on mismatch, but **nobody is looking at
  the page at 5 AM**, so `web_200` here would be decoration.
  So the probe shells out to the radar repo's own `job/selfcheck.js`, which
  replays that shape against a live `/evaluate` and asserts it reproduces NUTS's
  own frontrunners / ftlt / blackswan results; exit 1 = drift. **Running the
  repo's script instead of reimplementing the walk here is deliberate — a third
  copy of the tree would be a third thing to drift.** It needs `node` on PATH
  (raises, so it retries rather than false-alarming) and the repo present on the
  Mac at `repo_dir`. `catalysts.json` freshness is asserted **only once
  `generated_at` is non-null**, because the file is a hand-seeded placeholder
  until the 5:15 gather ships — when `job/` lands, drop that exemption.

- **Two silent failures caught 2026-09-19 (both rostered + unit-tested in
  `TestRosterGuards` / `TestLogTailGrace`).** (1) @TweetSyn_bot — the ALERTS bot,
  `TELEGRAM_TOKEN` from the Dhaka flights .env — owns the Silent-digest card
  buttons (callbacks to health-hub `/api/defensive`). Its webhook came back EMPTY
  between the 06:50 card and 08:45; nothing on the Mac calls deleteWebhook, the
  deleter is off-Mac and unproven. health-hub's tick now re-registers it every
  5 min; row "alerts bot (digest-tap webhook registered)" pages if that stops
  working. (2) `log_tail` grew `grace_min`/`grace_lines`: aoife-typing logs up to
  ~14 min of free-model timeouts before `wrote coach`, and a probe inside that
  window read a retry line as the outcome and paged. With `grace_min` set, a
  file written that recently passes if a success line sits in the last
  `grace_lines` (12) lines; a window with no success at all still fails.
- **`telegram_webhook` probe (added 2026-08-24).** A webhook bot has no
  scheduled run to grade, so "did it run" is the wrong question. It dies two
  independent ways and **neither probe catches the other's failure**, which is
  why `voices-bot` is rostered twice:
  1. the Vercel function stops serving → `web_200`;
  2. Telegram stops pointing at it → `telegram_webhook`.
  A dead function still has a valid webhook registration, and an unhooked bot
  still returns 200 on a GET. Case 2 is not hypothetical: deactivating the old
  n8n `MAIN` workflow made n8n call `deleteWebhook` on @MainJ_bot and wipe the
  Vercel registration — twice in one afternoon. The bot went silently deaf
  while every "is it up?" signal stayed green.
  The two entries also differ in `repo`: the webhook one is `None` **on
  purpose**, because `notion_health.py` stamps one row per repo and the LAST
  result wins — a healthy `web_200` would paint the `voices-bot` row ✅ while
  the bot was deaf. Same trap as the dhaka-hotels/dhaka-flights pair.

- **`mental-models` marker is un-fakeable by design.** Its workflow exposes a
  `dry_run` input, so a manual test run could otherwise satisfy the probe while
  the nightly cron was dead. `MENTAL-MODELS OK n/3 date=… index=A->B` is printed
  only when the brief was *delivered* AND the rotation index *advanced*;
  `--dry-run` and `--no-telegram` deliberately cannot print it (7 tests in
  `mental-models/tests/test_marker.py` assert this). When adding any probe to a
  repo whose workflow has a manual dry-run mode, check the same thing.

- **A green check must prove the PRIMARY path ran — a fallback satisfying it is a
  false negative.** This is the rule the fleet exists to enforce, learned the hard
  way on 2026-08-06. `financial-telegram-bot` has two senders: a Lambda (primary)
  and a GitHub Actions runner (backstop). Its health check asked *"did a report
  arrive?"*, the backstop kept answering yes, and the primary stayed dead for **two
  months** with every layer green. When adding a probe, ask: *if the primary died and
  only the fallback ran, would this probe still pass?* If yes, the probe is wrong —
  assert the primary specifically (or assert the artifact only the primary produces).

- **Roster EVERY workflow a repo schedules, and never trust in-workflow alerting to
  cover it.** The same 2026-08-06 outage failed `hedgelab` and `trading-algorithm-`
  for hours in silence because neither was rostered. Both had an `if: failure()`
  Telegram step — useless here: when GitHub can't acquire a runner the job never
  starts, so **no step inside the workflow can ever fire**. Only an external probe
  sees that class of failure. A repo's own alerting is never a reason to skip it.

- **`max_age_h` must be derived from the real cron, including weekends and GitHub's
  lateness.** GH cron routinely fires 30–90 min late and some repos here run 2–3 h
  late (`financial-dashboard-history`'s "02:00 UTC" job lands ~04:57 UTC). For
  **weekday-only** crons the Friday→Monday gap is ~60–64 h at the Monday 09:00 UTC
  check, and the Monday run fires *after* it — hence `hedgelab` and
  `trading-algorithm-` use **72**, not 48. Too tight = the digest cries wolf every
  Monday and the owner learns to ignore it, which is the real failure.

- **…and it must catch the FIRST missed day, not the second.** The other failure
  mode of a loose value is silence. Derive it with two numbers, both measured
  (`gh api …/contents/.github/workflows/<file> | base64 -d | grep cron` for the
  schedule, `gh run list` for what actually happens): **A** = the age the probe
  sees on a *normal* day at the 09:00 UTC check, **B** = the age after ONE missed
  day. Put `max_age_h` between them, nearer B. Runs that land *after* 09:00 UTC
  make A ≈ 17–23 h and B ≈ 41–47 h — so the old **48** on `sentiment-scraper`,
  `ynab-budget-brief` and `vix-fear-greed` needed **two** consecutive misses to
  alarm; they are **36** as of 2026-08-06. `leasehackr-scraper`'s crons moved to
  03:54/03:56 UTC that same day, putting A at 1.5–3.1 h and B at 25.5–27.1 h — so
  36 would sleep through a missed day there and both its entries are **24**.
  Re-derive whenever a cron moves; a stale `max_age_h` is silent, not loud.

- **A timestamp the job writes UNCONDITIONALLY is not evidence that the job
  produced anything.** Same rule as the fallback one above, one level lower down:
  there the fallback satisfied the check, here the *act of running* did.
  `dhaka-hotels` rewrites and pushes `hotel_rates.json` every night even when it
  scraped nothing — so its top-level `updated` measures only that the job woke up.
  From 2026-08-11 to 08-16 the Browserbase free tier was dry, **zero** rates
  refreshed, and this probe was ✅ every single morning: `updated` said today,
  while every row's `checked` said Aug 10. Fixed 2026-08-16 by grading the OLDEST
  per-row `checked` (`rows_key="rows"`), the field that is stamped *only* on a
  real scrape. When adding a probe, ask both questions: *would a fallback satisfy
  this?* and *would a run that did nothing satisfy this?*

- **`com.jalal.dhaka-hotels` fires at 5:00 AM — the same minute as the fleet check
  itself.** Rostered 2026-08-06 (it had been in `schedule_snapshot` but not in
  `FLEET`). The two jobs are not ordered, so the probe usually grades *yesterday's*
  `site/hotel_rates.json`. Both stamps are date-only, which `probe_web_fresh`
  parses as MIDNIGHT — ages are up to a day pessimistic (the safe direction, but
  budget for it). `max_age_h` is **96**, not 36, because the graded field changed
  from `updated` (stamped nightly, so ~29 h normal / ~53 h after one miss) to the
  oldest row's `checked`: a single property MISSing is routine and self-heals the
  next night, so 36 would cry wolf constantly. 96 fires on roughly three dead
  nights — early enough to matter, quiet enough to stay believed. The probe reads
  the *published* file on `raw.githubusercontent`, which also covers the
  commit+push, and it is deliberately not `launchd_exit`: `run_hotel_rates.sh`
  `exit 0`s when a flight run still holds the browser session, so standing down
  would look identical to a refresh (the same trap as the T7 backup). **Not
  scheduled here — flag the contention, don't "fix" it**: 5:00 AM is chosen so the
  hotel run lands after every flight slot.

- **`notion_health.py` writes ONE row per repo, last result wins.** Several FLEET
  entries can share a repo (leasehackr Daily + Historical, financial-telegram-bot
  report + self-health). Whichever runs *last* decides that row's ✅/❌, so a
  healthy probe can paint over a failing sibling in Notion. Telegram carries every
  entry independently and is the real alert channel. This is why the `dhaka-hotels`
  entry uses `repo: None` — stamping it would let a healthy hotel refresh mark the
  `dhaka-flights` row green while the flight tracker was down.

- **The `keepalive.yml` workflow exists ONLY to dodge GitHub's 60-day cron auto-disable.**
  GitHub suspends scheduled workflows after 60 days without a commit. This repo's own
  sync never pushes commits, so `keepalive.yml` runs on the **1st and 15th** (`17 3 1,15 * *`)
  and makes an *empty* `chore: keepalive [skip ci]` commit **only when the repo has been
  idle ≥ 40 days** (or `workflow_dispatch` with `force=true`). It needs `contents: write`
  (the only workflow that does; `sync.yml` is `contents: read`). Do not delete it or the
  monthly sync will eventually stop firing. The comment in the file mentions "Daily/Historical
  scrapers" — that wording was copied from another repo's keepalive; here the only cron it
  protects is the monthly sync.

- **Notion `Notes` column is sacred — the script NEVER writes it.** `build_props()` only
  emits `Name, Description, Stack, Language, Visibility, Status, Last updated, Repo URL,
  Runtime versions`. Any manual `Notes` survive every sync. Do not add `Notes` to
  `build_props`.

- **Idempotency key is the `Repo URL` column** (`repo.html_url`). `notion_query_all()`
  indexes existing rows by that URL; an existing row is PATCHed, a missing one is POSTed.
  Don't change the key field without migrating existing rows, or every run will create
  duplicates.

- **Deletes are soft.** Repos present in Notion but no longer returned by GitHub are set
  to `Status=Deleted` (and skipped if already Deleted). Rows are never removed. This still
  catches **renamed** repos (new URL = new row created, old URL = marked Deleted).
- **⚠️ Archived ≠ Deleted — do NOT set `include_archived=False` again.** Until 2026-08-29
  archived repos were skipped at list time, so they fell out of `seen_urls`, hit the
  "vanished from GitHub" sweep, and were labelled `Status=Deleted` — which is false: the
  repo is still there, deliberately frozen. It also made `compute_status`'s
  `is_archived -> "Archived"` branch dead code that could never execute. Found when
  `vix-fear-greed` was archived (2026-08-29) and would have flipped to Deleted on the next
  monthly run. `test_sync.py` pins the labelling; the list flag itself is network wiring
  and is verified by running the sync and reading the row back.

- **The target Notion DB must already have the exact column names/types** used in
  `build_props` (`Name`=title, `Repo URL`=url, `Stack`=multi_select, `Language`/`Visibility`/
  `Status`=select, `Last updated`=date, `Description`/`Runtime versions`=rich_text). New
  `Stack` multi-select **options** are auto-created when first used; new *columns* are not.

- **`Language` is collapsed to a fixed whitelist.** Only `Python, TypeScript, JavaScript,
  HTML, Go, Rust` pass through; everything else becomes `Other` (so the Notion `select`
  doesn't sprawl). The `Stack` "Static HTML" tag additionally requires `repo.language ==
  "HTML"`.

- **`files_content` is a dynamic attribute, not a dataclass field.** `main()` attaches
  `repo.files_content = {…}` at runtime (manifest contents for the wanted files).
  `detect_stack` guards it with `hasattr`, `detect_runtimes` with `getattr(..., {})`. If you
  call those classifiers outside `main()` without setting `files_content`, package.json/
  runtime parsing is silently skipped — they won't crash, but they'll under-detect.

- **Rich-text is truncated to 1900 chars** (`text_chunks`, Notion's per-block ~2000 limit).
  Descriptions are also capped (~350 chars in `make_description`, 200-char ask to Claude).

- **`Last updated` uses `pushed_at[:10]`** (the GitHub push date), but **`Status` (Active/
  Stale)** uses the *later* of `pushed_at` and the last **successful** Actions run
  (`fetch_last_actions_run`). So a repo whose only activity is green scheduled CI runs (its
  data lives elsewhere, e.g. Sheets) stays `Active` even though its push date is old.
  `STALE_AFTER_DAYS = 180`. Archived repos → `Status=Archived`.

- **`http()` returns the parsed error body on most 4xx/5xx** (only retrying 429/502/503/504,
  and 404 is silent). Callers must check `status` themselves — many do
  `if status != 200: return ""/set()` and degrade gracefully rather than raise. The Notion
  query/upsert paths DO raise `RuntimeError` on non-2xx, which fails the whole run (intended:
  a broken Notion call should fail loudly).

- **No third-party deps. Keep it stdlib.** The whole point is zero-install; `sync.yml` does
  not `pip install` anything. Don't add `requirements.txt` / imports that need it without
  also updating the workflow.

---

## 5. Known issues / drift corrections

These are corrections where the **code is the source of truth** over older prose:

- **README's "Stack detection (heuristic, no LLM)" heading is misleading.** *Stack
  detection* is heuristic, but **descriptions ARE generated by an LLM** (Claude Haiku 4.5
  via the Anthropic API; `describe_with_claude` / `make_description`). The LLM is the
  primary description source; the heuristic is only the fallback.
- **README's "Required GitHub Actions secrets" table omits `ANTHROPIC_API_KEY`**, and the
  README "Local run" block omits `export ANTHROPIC_API_KEY`. Both are real, used inputs
  (wired in `sync.yml` and read in `main()`). Treated as documented above in §3.
- **README does not mention the fallback env var names** the code accepts: `GITHUB_TOKEN`
  (for `GH_PAT`) and `NOTION_DATA_SOURCE_ID` (for `NOTION_DATABASE_ID`). See §3.
- **Deprecated-version sets** (`sync.py`): Python `{3.7, 3.8, 3.9, 3.10}`, Node `{12, 14, 16}`.
  README's note that "Python 3.10 is on the AWS Lambda deprecation list (Oct 31 2026)" is
  consistent with the code including 3.10 in the deprecated set.
- No open TODOs/bugs flagged by the owner. (Tests: see §3.)

---

## 6. Stack & runtime classification reference (`detect_stack` / `detect_runtimes`)

**File/marker rules** (`STACK_FILE_RULES`, matched case-insensitively against the file tree;
directory markers match a path that equals or starts with `marker/`):

| Tag | Trigger files |
|---|---|
| AWS Lambda | `serverless.yml/.yaml`, `template.yaml/.yml`, `samconfig.toml` |
| Vercel | `vercel.json`, `.vercel` |
| GitHub Actions | `.github/workflows` (directory) |
| Docker | `Dockerfile`, `docker-compose.yml/.yaml` |
| Frontend (Next.js) | `next.config.js/.mjs/.ts` |

**package.json deps** (when present and parseable): `next` → Frontend (Next.js); `react` →
Frontend (React); `express`/`fastify`/`@hono/node-server` → API/Backend;
`node-telegram-bot-api`/`telegraf`/`grammy` → Telegram Bot.

**Name + README + filename blob heuristics:**
- Web Scraping: `beautifulsoup`/`scrapy`/`playwright`/`selenium`/`puppeteer`/`cheerio` in
  blob, or repo name contains `scraper`.
- Telegram Bot: name contains `bot`/`telegram` **and** blob mentions `telegram`/`telegraf`/
  `python-telegram-bot`.
- Trading/Finance: name contains `trading`/`finance`/`vix`/`stock`/`composer`/`fear-greed`.
- AI/LLM: blob contains `anthropic`/`openai`/` llm`/`claude-`/`gpt-`/`langchain`.
- API/Backend: blob contains `fastapi`/`flask`/`django`.
- Static HTML: `repo.language == "HTML"` and no Vercel/Next/React tag.
- **Local Only**: nothing else matched (default).

**Runtime parsing** (`detect_runtimes`): Python versions from `setup.py`, `pyproject.toml`,
`runtime.txt`, `.python-version`, serverless/SAM `pythonX.Y`; a Python repo with
`requirements.txt` but no detectable version gets `Python ?`. Node versions from
`package.json` `engines.node` and `.nvmrc`. Versions in the deprecated sets above get
`⚠️ deprecated`.

---

## 7. File / module map

- `sync.py` — the entire program (stdlib only). Key parts:
  - `http()` — single retrying HTTP helper for all three APIs.
  - `gh_headers` / `list_user_repos` / `fetch_tree` / `fetch_file` / `fetch_readme` /
    `fetch_last_actions_run` — GitHub reads.
  - `detect_stack` / `detect_runtimes` / `clean_readme_excerpt` — heuristic classification.
  - `describe_with_claude` / `make_description` — LLM description + heuristic fallback.
  - `compute_status` — Active/Stale/Archived (180-day window, considers last good CI run).
  - `notion_headers` / `notion_query_all` / `text_chunks` / `build_props` / `upsert_page` /
    `mark_deleted` — Notion reads/writes (upsert by Repo URL; soft-delete).
  - `main()` — orchestrates the run; reads env vars (incl. fallback names); returns exit code.
- `fleet_health.py` / `run_health.sh` — Mac-side daily fleet health check (see §1).
- `test_fleet_health.py` / `test_probes.py` — stdlib `unittest` cover for the
  fleet check (see §3 for what each covers).
- `schedule_snapshot.py` — Mac-side ground-truth snapshot of launchd/cron/Time
  Machine schedules → `schedule.json` (see §1). **Add a `CATALOG` entry whenever
  adding a launchd job**, or the Notion row will carry a 🆕 placeholder.
  `describe_calendar` collapses 4+ evenly spaced slots to
  `every 30 min, 7:00 AM–9:30 PM` (added 2026-08-18 for the school-bot tick,
  whose 30 slots would otherwise render as a 30-time "(retries …)" wall);
  2–3 slot retry ladders like carmax's 0:00/2:00/4:00 still read as retries.
- `notion_health.py` / `notion_schedule.py` — cloud-side watchdog + Notion stamping,
  run by `.github/workflows/health.yml` (One Clock dispatch ~12:37 UTC; cron 13:07
  UTC backstop).
- `.github/workflows/sync.yml` — monthly cron + manual dispatch; runs `python sync.py`.
- `.github/workflows/keepalive.yml` — biweekly empty-commit keepalive to prevent 60-day
  cron auto-disable (only runs the commit when idle ≥ 40 days; `contents: write`).
- `README.md` — human-facing landing page (kept; see §5 for its drift vs. code).
- `.gitignore` — ignores `.env*`, `__pycache__`, venvs, editor dirs.

## Retirement 2026-08-29 (44 → 43 probes): vix-fear-greed

`vix-fear-greed` was archived and its probe removed. Its only job was writing the
FEAR/GREED tag into the VIX sheet's cell C2. That computation now lives in
`financial-telegram-bot` (`dashboard/lib/vixFearGreed.js`, cascade
CBOE → FRED → C2), and both the dashboard and the Telegram brief read it from
`/api/sheets` rather than the cell — so the tag is covered by the existing
`financial-telegram-bot` probes. **Removing a probe is only correct when the thing
it proved is proved elsewhere**; here the daily brief's own probe fails if the VIX
row goes missing. A probe pointed at an archived repo can only ever fail.

## Coverage audit 2026-08-25 (31 → 41 probes)

Jalal asked "what else in my fleet has no health check". Diffed the roster against
ground truth — every `com.jalal.*` launchd job, every repo with an active cron
workflow, every live URL, and every bot's `getWebhookInfo`. Nine gaps closed:

- **`aoife-milestones-bot` had NO probe of any kind** — the only live service that
  was entirely unwatched. Now two (function serving + webhook registered). It is
  voice-driven and write-through, so a deaf bot loses milestones Jalal believes
  were saved, and the master Sheet just stops gaining rows — indistinguishable
  from a quiet week.
- **`notebooklm-drip`** — a daily 04:00 launchd job with nothing watching it.
  `log_marker` on `FINISHED types_still_open=`, correlated to a dated run header:
  a bare `FINISHED` would match a stale one from last week, and drip.log's mtime
  only ever proves the wrapper woke up.
- **Webhook probes for `zinger-bot` and `aoife-school-bot`.** The
  `telegram_webhook` probe type existed but had only ever been applied to
  voices-bot. zinger's `web_200` hits the ROOT, which a dead handler still serves;
  school-bot's tick proves the OUTBOUND half only, while an unhooked bot silently
  swallows every reply.
- **`aoife-puzzles` / `aoife-algebra` / `aoife-order` / `backbench`** — live sites
  unwatched while their siblings were watched. Coverage by accident of build
  order, not by risk.

**`telegram_webhook` no longer matches the full URL.** aoife-milestones-bot and
aoife-school-bot guard their webhook with a shared secret in the query string
(`?s=…`) and **this repo is public** — hardcoding `expect_url` would have
published the guard. It now compares scheme+host+path only, never echoes the
query back in an error message, and takes `require_guard: True` to prove the
guard still BITES: it POSTs one bare `{"update_id":0}` with no `?s=` and no
`X-Telegram-Bot-Api-Secret-Token` header and requires a 401/403. (Until
2026-09-09 it merely asserted `?s=` was present in the registered URL; when
milestones-bot moved its secret into Telegram's header on 2026-09-07 — the
query form leaked into every Vercel access-log line — the probe paged for a
day on a bot that had become MORE locked down. Test the door, not the shape
of the key.) A guard that silently vanishes still pages — `test_fleet_health`
has the open-endpoint case.

Every new probe's failure branches were verified to actually fire before commit
(wrong host, wrong path, missing token, absent query guard, bogus log pattern,
missing log), plus a regression check that voices-bot still passes unchanged and
that no error message leaks a secret. A probe that cannot fail is decoration.

**Not gaps, delete instead:** `aoife-math-2a`, `aoife-math-game`,
`aoife-subtraction-game`, `long-subtraction-aoife` are all still deployed and
serving 200. They are the four superseded math repos already marked for deletion;
they were deliberately NOT rostered.


## 11 Sep 2026 — silent 05:00 fleet digest (partly superseded 27 Sep: reds after the 06:30 re-check now buzz once, see §1.1)
`fleet_health.py` sends the daily digest with `_telegram_send(..., silent=True)` (disable_notification):
it lands at 05:00 ET and is read at breakfast. The self-crash panic message stays loud.

## Silent digest hand-off (11 Sep 2026)

The overnight send calls `digest_post("fleet", text, parse_mode)` first (health-hub `api/digest.js`, env `DIGEST_URL` + `DIGEST_KEY` — repo secret DIGEST_KEY, URL in health.yml; only the silent 05:00 digest is handed off, alerts stay direct). Stored → no direct message; the 07:00 ⚪ Silent digest card carries a button that replays it in full (36 h). Collector down or env missing → the old silent direct send. The DIGEST itself stays silent; since 27 Sep (owner decision) `loud_alert()` sends a SEPARATE buzzing message for reds still standing at 06:30 (§1.1). The card goes out 06:50-10:00 ET, not 07:00 sharp.



## History: the fleet-health §1 prose as it stood before the 2026-09-27 rewrite

> Kept for the WHY behind each rule. **Superseded by §1 wherever they differ.** Known
> stale points: the probe count (now 71 rows, 19 types), "the 6:30 slot no-ops once the
> digest reached Telegram" (digest mode always re-runs), lock age (now pid-based),
> "exits nonzero" (now exit 3), health.yml "13:07 UTC" (One Clock ~12:37 is primary),
> and the 10-12 h / 34-37 h watchdog numbers (a normal day now reads ~6 h).

**This repo now has TWO jobs** (2026-07-19): the original monthly repo→Notion
sync, and the DAILY FLEET HEALTH system (weekly→daily 2026-07-26):
- `fleet_health.py` — runs on Jalal's Mac (launchd `com.jalal.fleet-health`,
  daily 5:00 AM **plus a 6:30 AM retry slot** so everything is settled
  before the 7 AM YNAB brief / wake-up — run_health.sh passes
  `--retry-slot` from 6 AM and the script no-ops if today's digest already
  reached Telegram, or backs off if the 5 AM run still holds
  `.fleet_health.lock` (gitignored; locks >2 h old are treated as crashed
  and ignored); manual `python3 fleet_health.py` always runs. Wrapper
  `run_health.sh` also truncates `health.log` in place past ~400 KB —
  gitignored). 41 data-level probes (local launchd stamps/exit codes, `gh`
  runs with log-grep data markers, live-site checks). `log_grep` takes one
  regex or a list (ALL must match); assert the *pipeline ran* rather than that
  a count was nonzero, or a legitimately quiet source false-alarms at 5 AM.
  The Mac-side twin of that is the `log_marker` probe (added 2026-08-18 for
  `aoife-school-bot`): same `log_grep` contract against a local launchd log,
  where `{date}` in a pattern expands to today|yesterday. Use it for a job
  whose slots do NOT all land before the 5 AM check — grading file mtime or
  exit code there measures only that the job woke up.
  A workflow with several legitimate shapes (reddit-scraper's real scrape vs.
  its retry-window no-op) gets ONE pattern with an `|` covering both, not two
  patterns that can't both hold. **Every marker must be verified against a real
  recent log** (`gh run view <id> --log`): Actions echoes each step's *source*
  into the log, so a naive `already updated today` matches the `echo "…($LAST)…"`
  line on every run and can never fail — reddit-scraper's markers use `[^$\n]+`
  precisely to exclude the echoed form. A marker that can never match is a
  permanent false alarm; a marker that can never miss is decoration. One
  repo may hold several probes (leasehackr has Daily + Historical) — the digest
  keeps the "(qualifier)" on those so they don't read as duplicates. **Digest contract:**
  all healthy → ONE plain-text line ("✅ Fleet check … all N systems
  healthy", plus "· recovered: X" the first healthy day after a failure);
  any failure → a full diagnostic block per failure (probe config line,
  "⏳ failing since DATE" when the failure spans days, multi-line detail
  incl. run URL + failed-step log tail for gh_run probes — the tail drops the
  post-job cleanup block, strips ANSI/timestamps, and prepends the first real
  error signature, since a naive tail shows only `git config --unset` noise)
  designed to be
  pasted verbatim into Claude to debug. **The digest is budgeted per failure,
  never tail-chopped.** Telegram's cap is 4096 chars (`TELEGRAM_LIMIT = 4000`)
  and one gh_run failure block runs ~1.5 KB, so 3+ simultaneous failures blow
  the budget — and the old whole-message truncation dropped the LAST blocks
  *and* the "✅ the other N healthy" line, i.e. a 4-repo outage reported as a
  2-repo one, in exactly the scenario the digest exists for (measured: 4 synthetic
  failures → 1 name lost + no healthy line). Now header and footer are reserved
  first, the remainder is split evenly across failures (blocks that come in under
  their share hand the slack back to the big ones), and each block fills to its
  share in priority order: name → "failing since" → config line → detail → log
  tail. **A failure name is never dropped** (past ~15 simultaneous failures the
  body degrades to a plain roll call of names); the log tail is what goes.
  Losing a tail costs one paste-into-Claude round trip; losing a name means the
  owner never learns that system is down. Telegram is plain text (NO
  parse_mode — log excerpts full of `_*[` used to be able to 400 the
  Markdown digest) with 3 send attempts. Probes RAISE on infra errors
  (network blip, gh failure) → retried 3× with 20 s pauses; a returned
  False (stale data, red run) is real signal, never retried. If the script
  itself crashes, a 🚨 panic Telegram goes out and it exits nonzero.
  Commits+pushes `health.json` (records `telegram: sent/failed` — read by
  the Dead-Mac watchdog below, unchanged meaning; `telegram_mode:
  digest/direct/null`, added 2026-09-15 — a Silent-digest hand-off only
  QUEUES the card, so `already_ran_today()` requires `direct` to skip the
  6:30 retry, letting a 5:00 finding that self-heals by 6:30 — e.g. the
  mental-models 6 AM backstop — get re-checked and corrected before the
  card actually reaches Jalal at 06:50-07:30 — and `failing_since` per
  failing system, carried across days by `annotate_history`). Probes live
  in the `FLEET` list — add new automations there.
- `notion_health.py` + `.github/workflows/health.yml` (daily 13:07 UTC) —
  stamps Health / Health checked / Health note onto each repo's row in the
  same Notion DB (keyed by Repo URL, same secrets as sync.py; auto-creates
  the three properties). **Dead-Mac watchdog:** it `die()`s — log + Telegram +
  nonzero exit — when `health.json` is older than `STALE_HOURS = 24`, or when
  the Mac's own digest went undelivered (`telegram != "sent"`). The Mac stamps
  at 05:00 local and this workflow actually starts 14:38–15:49 UTC, so a normal
  day measures 10–12 h (`checked` is Mac-local time read against a UTC runner —
  a 4–5 h overstatement, the safe direction) and ONE missed Mac run measures
  34–37 h: it fires at the first check after a missed stamp, ~1.4 days later.
  That is the real "within two days" guarantee; the previous `age_days > 2` on
  a date-only comparison did not fire until the THIRD day (~3.4 days) while the
  docs claimed two. The Telegram is the point: a dead Mac cannot send its own
  digest, and a red Actions run + GitHub's failure email can go unread for a
  week. `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` are wired in `health.yml`; if they
  are unset the alert degrades to email-only and never crashes.
  **Since One Clock the dispatch lands ~12:37 UTC**, so the real latency is a
  little shorter than the numbers above. **Card-delivery check (red team
  2026-09-27):** when `telegram_mode == "digest"`, "sent" only means QUEUED in
  health-hub; the watchdog also reads `digest_fleet_at` from
  jalal-health.vercel.app/api/health (stamped only after Telegram accepted a
  card naming the fleet item) and dies past `CARD_STALE_HOURS = 20` — a dead
  card flush would otherwise swallow every fleet red silently.
- **Red team 2026-09-27 (round 7) — rules that generalise:**
  - A job that runs BEFORE the 05:00 check gets `{today}`, never `{date}`:
    the yesterday arm let a failed morning pass (daily-trackers, gcal-sync,
    planner-backup). Weekday-only markers get `{weekday}` (trading-algorithm:
    72 h alone left two green mornings after a Tuesday death).
  - An hourly/5-minute job is graded by the NEWEST outcome, not "any success
    in the window": `cloudwatch_marker(today_only=False, fail_grep=,
    active_window=)` (ynab-nag), `log_tail` with a hard age (defensive-nag),
    stamped grace hits measured against the newest line (aoife-typing).
  - A lock backs the 6:30 retry off only while its pid is alive (job-reaper
    SIGKILLs leave orphans). Every nonzero exit alerts: fleet_health.py exits
    3 after its own 🚨; run_health.sh alerts on any other code (SyntaxError).
  - The lint rejects a `log_grep` that matches "" or "zz". 5xx from a bot
    selftest or the catalysts fetch is infra (retried), not a red row.
  - **Reds are LOUD (owner decision 2026-09-27).** `loud_alert()` sends one
    buzzing Telegram listing the failing systems — never before 06:25 (the
    05:00 run stays silent; 06:30 re-checks first), once per failing name per
    day (`loud` in health.json). A silent 05:00 direct send no longer lets the
    6:30 slot skip while reds are unbuzzed.
  - `web_fresh(min_rows=)` floors the scraped lists (dhaka-flights published
    flights=0 on 08-23 with a fresh stamp). `gh_run(rescue_max_failures=)`
    stops "an earlier run succeeded" from hiding a job that fails every other
    run (reddit-backup 1, trading-algorithm 2).