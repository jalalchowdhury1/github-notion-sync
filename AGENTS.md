# AGENTS.md — github-notion-sync

> **Single source of truth for anyone (human or AI) touching this repo.** Read it fully
> before changing code. This is a tiny repo — one script (`sync.py`) plus two GitHub
> Actions workflows. No LLM-facing docs were consolidated; `README.md` is kept as the
> human/GitHub landing page. If something here is wrong, fix *this* file.

---

## 1. What this is

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
  gitignored). 25 data-level probes (local launchd stamps/exit codes, `gh`
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
  Commits+pushes `health.json` (records `telegram: sent/failed` — an
  undelivered digest makes the 6:30 slot rerun — and `failing_since` per
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
Born from the 2026-07 CarMax incident: 17 days of green CI with zero rows —
hence data-level markers, not conclusions, wherever possible.


A single-file Python script (`sync.py`, stdlib-only — no third-party packages) that
**mirrors the owner's GitHub repos into a Notion database**. On each run it:

1. Lists every repo the authenticated user **owns** (paginated, `affiliation=owner`,
   sorted by `pushed`), skipping GitHub-archived repos by default.
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
```

All HTTP goes through one helper, `http()`, which retries `429/502/503/504` (and network
errors) up to 3× with exponential backoff (`2**attempt` seconds), 30s timeout. 404 is
returned quietly (no stderr noise).

---

## 3. How to run / test / deploy

**This is not "deployed" — it just runs in Actions or locally.** No build step, no
`requirements.txt` (stdlib only). There are **no automated tests** in the repo.

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

---

## 4. Gotchas / hard rules

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
  to `Status=Deleted` (and skipped if already Deleted). Rows are never removed. Note this
  also catches **renamed** repos (new URL = new row created, old URL = marked Deleted) and
  repos that became **archived** (archived repos are skipped at list time → look "gone").

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
- No open TODOs/bugs flagged by the owner. No tests exist (none claimed).

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
- `schedule_snapshot.py` — Mac-side ground-truth snapshot of launchd/cron/Time
  Machine schedules → `schedule.json` (see §1). **Add a `CATALOG` entry whenever
  adding a launchd job**, or the Notion row will carry a 🆕 placeholder.
  `describe_calendar` collapses 4+ evenly spaced slots to
  `every 30 min, 7:00 AM–9:30 PM` (added 2026-08-18 for the school-bot tick,
  whose 30 slots would otherwise render as a 30-time "(retries …)" wall);
  2–3 slot retry ladders like carmax's 0:00/2:00/4:00 still read as retries.
- `notion_health.py` / `notion_schedule.py` — cloud-side Notion stamping, run by
  `.github/workflows/health.yml` (daily 13:07 UTC).
- `.github/workflows/sync.yml` — monthly cron + manual dispatch; runs `python sync.py`.
- `.github/workflows/keepalive.yml` — biweekly empty-commit keepalive to prevent 60-day
  cron auto-disable (only runs the commit when idle ≥ 40 days; `contents: write`).
- `README.md` — human-facing landing page (kept; see §5 for its drift vs. code).
- `.gitignore` — ignores `.env*`, `__pycache__`, venvs, editor dirs.
