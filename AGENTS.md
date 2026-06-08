# AGENTS.md — github-notion-sync

> **Single source of truth for anyone (human or AI) touching this repo.** Read it fully
> before changing code. This is a tiny repo — one script (`sync.py`) plus two GitHub
> Actions workflows. No LLM-facing docs were consolidated; `README.md` is kept as the
> human/GitHub landing page. If something here is wrong, fix *this* file.

---

## 1. What this is

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

---

## 4. Gotchas / hard rules

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
- `.github/workflows/sync.yml` — monthly cron + manual dispatch; runs `python sync.py`.
- `.github/workflows/keepalive.yml` — biweekly empty-commit keepalive to prevent 60-day
  cron auto-disable (only runs the commit when idle ≥ 40 days; `contents: write`).
- `README.md` — human-facing landing page (kept; see §5 for its drift vs. code).
- `.gitignore` — ignores `.env*`, `__pycache__`, venvs, editor dirs.
