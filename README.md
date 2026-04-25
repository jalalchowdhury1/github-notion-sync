# github-notion-sync

Monthly sync from GitHub → Notion. Lists every repo I own, classifies what it
uses (AWS Lambda, Vercel, GitHub Actions, etc.), and writes/updates rows in a
Notion database under **💻 Tech & Automation**.

## How it runs

- **When:** 1st of every month at 9am ET (cron: `0 13 1 * *` UTC).
- **Where:** GitHub Actions workflow `.github/workflows/sync.yml`.
- **Manual run:** Actions tab → "Sync GitHub repos to Notion" → "Run workflow".

## Behavior

- Idempotency key is **Repo URL** — runs are safe to repeat.
- The **Notes** column is never written by the sync. Edit it freely.
- Repos that disappear from GitHub are marked `Status = Deleted`, not removed,
  so manual notes survive.
- Archived-on-GitHub repos are skipped at listing time (set
  `include_archived=True` in `sync.py` to include them).

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
