# github-notion-sync

Three jobs live here (full detail in [`AGENTS.md`](AGENTS.md) — read that before
changing anything):

1. **Repo → Notion sync** (below): monthly, lists every repo I own, classifies
   what it uses, and writes/updates rows in a Notion database under
   **💻 Tech & Automation**.
2. **Fleet health** (`fleet_health.py`): every morning on the Mac mini, 71
   checks that each automation actually did its work — not just that it ran.
   Silent at 05:00, one buzzing Telegram at 06:30 if anything is still red, full
   detail behind the morning card's "🛠 Fleet health" button. A cloud watchdog
   (`notion_health.py`, `health.yml`) alerts if the Mac itself goes quiet.
   Tests: `python3 -m unittest test_fleet_health test_probes test_sync`.
3. **Mac Mini Schedule table** (`schedule_snapshot.py` → `notion_schedule.py`).

This repo is public: no tokens, webhook secrets or `?s=` URLs in code or logs.

## How it runs

- **When:** 1st of every month at 9am ET (cron: `0 13 1 * *` UTC).
- **Where:** GitHub Actions workflow `.github/workflows/sync.yml`.
- **Manual run:** Actions tab → "Sync GitHub repos to Notion" → "Run workflow".

## Behavior

- Idempotency key is **Repo URL** — runs are safe to repeat.
- The **Notes** column is never written by the sync. Edit it freely.
- Repos that disappear from GitHub are marked `Status = Deleted`, not removed,
  so manual notes survive.
- Archived-on-GitHub repos are included (`include_archived=True`) and get
  `Status = Archived`, not Deleted.

## Stack detection (heuristic, no LLM)

| Tag | Trigger |
| --- | --- |
| AWS Lambda | `serverless.yml`, `template.yaml`, `samconfig.toml` |
| Vercel | `vercel.json` or `.vercel/` |
| GitHub Actions | `.github/workflows/` directory |
| Docker | `Dockerfile` or `docker-compose.yml` |
| Frontend (Next.js) | `next.config.*` or `next` in package.json |
| Frontend (React) | `react` in package.json |
| API/Backend | `express`, `fastify`, `fastapi`, `flask`, `django` |
| Telegram Bot | `telegraf`, `grammy`, `python-telegram-bot`, etc. |
| Web Scraping | `beautifulsoup`, `scrapy`, `playwright`, `selenium`, repo name contains `scraper` |
| AI/LLM | `anthropic`, `openai`, `langchain`, etc. in repo |
| Trading/Finance | repo name contains `trading`, `finance`, `vix`, `composer`, etc. |
| Static HTML | HTML-only repo with no other framework markers |
| Local Only | nothing else matched |

## Runtime version flagging

Parses `requirements.txt`, `pyproject.toml`, `setup.py`, `runtime.txt`,
`.python-version`, `package.json` engines, `.nvmrc`, and serverless/SAM configs.
Versions in the deprecated set are flagged with ⚠️:

- Python: 3.7, 3.8, 3.9, 3.10
- Node: 12, 14, 16

(Python 3.10 is on the AWS Lambda deprecation list — Oct 31, 2026.)

## Required GitHub Actions secrets

| Secret | Purpose |
| --- | --- |
| `GH_PAT` | GitHub PAT (classic) with `repo` + `read:user` scopes — needed to read private repos |
| `NOTION_TOKEN` | Notion internal integration token (`ntn_...`) shared with the database |
| `NOTION_DATABASE_ID` | UUID of the Notion database (parent of the rows) |

## Local run

```sh
export GH_PAT=ghp_...
export NOTION_TOKEN=ntn_...
export NOTION_DATABASE_ID=...
python sync.py
```

## Re-pointing at a different Notion database

Update `NOTION_DATABASE_ID` (the Notion database UUID — visible in the database
URL: `notion.so/<workspace>/<DB_ID>?v=...`). The database must have the same
column names; new options for the `Stack` multi-select are auto-added when
used.
