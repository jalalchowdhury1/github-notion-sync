"""GitHub -> Notion repo sync.

Reads all repos owned by the authenticated user, classifies each one's stack
from its file contents, and upserts a row in a Notion database.

Idempotency key: the "Repo URL" column. Existing rows are matched by URL and
updated; missing rows are created. Repos that disappear from GitHub are marked
Status=Deleted (not removed) so manual Notes are preserved.

The "Notes" column is read-only from this script's perspective: it is never
written, so anything the user types there survives every sync.

Required env vars:
    GH_PAT             GitHub PAT with repo + read:user scopes
    NOTION_TOKEN       Notion internal integration token (ntn_...)
    NOTION_DATABASE_ID Database UUID (the parent of the rows we upsert)
    ANTHROPIC_API_KEY  Anthropic API key for description generation (Claude Haiku)
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib import error, parse, request

GITHUB_API = "https://api.github.com"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_VERSION = "2023-06-01"

STALE_AFTER_DAYS = 180

DEPRECATED_PYTHON = {"3.7", "3.8", "3.9", "3.10"}
DEPRECATED_NODE = {"12", "14", "16"}


@dataclass
class Repo:
    name: str
    full_name: str
    url: str
    description: str
    language: str
    is_private: bool
    is_archived: bool
    pushed_at: str
    default_branch: str
    files: set[str] = field(default_factory=set)
    readme: str = ""


def http(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    retries: int = 3,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, data=data, method=method, headers=headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return resp.status, {}
                return resp.status, json.loads(raw)
        except error.HTTPError as e:
            payload = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            if e.code != 404:
                print(f"HTTP {e.code} on {method} {url}: {payload}", file=sys.stderr)
            try:
                return e.code, json.loads(payload)
            except json.JSONDecodeError:
                return e.code, {"_raw": payload}
        except (error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
    raise RuntimeError(f"network error after retries: {last_err}")


# --- GitHub --------------------------------------------------------------- #


def gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-notion-sync",
    }


def list_user_repos(token: str, include_archived: bool = False) -> list[Repo]:
    repos: list[Repo] = []
    page = 1
    while True:
        status, data = http(
            "GET",
            f"{GITHUB_API}/user/repos?per_page=100&page={page}&affiliation=owner&sort=pushed",
            gh_headers(token),
        )
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"GitHub list repos failed: {status} {data}")
        if not data:
            break
        for r in data:
            if r.get("archived") and not include_archived:
                continue
            repos.append(
                Repo(
                    name=r["name"],
                    full_name=r["full_name"],
                    url=r["html_url"],
                    description=r.get("description") or "",
                    language=r.get("language") or "Other",
                    is_private=r["private"],
                    is_archived=r.get("archived", False),
                    pushed_at=r["pushed_at"],
                    default_branch=r.get("default_branch") or "main",
                )
            )
        page += 1
    return repos


def fetch_tree(token: str, repo: Repo) -> set[str]:
    status, data = http(
        "GET",
        f"{GITHUB_API}/repos/{repo.full_name}/git/trees/{repo.default_branch}?recursive=1",
        gh_headers(token),
    )
    if status != 200 or not isinstance(data, dict):
        return set()
    return {item["path"] for item in data.get("tree", []) if item.get("type") == "blob"}


def fetch_file(token: str, repo: Repo, path: str) -> str:
    status, data = http(
        "GET",
        f"{GITHUB_API}/repos/{repo.full_name}/contents/{parse.quote(path)}?ref={repo.default_branch}",
        gh_headers(token),
    )
    if status != 200 or not isinstance(data, dict):
        return ""
    if data.get("encoding") == "base64" and data.get("content"):
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def fetch_last_actions_run(token: str, repo: Repo) -> str | None:
    """Return ISO timestamp of the most recent successful Actions run, or None."""
    status, data = http(
        "GET",
        f"{GITHUB_API}/repos/{repo.full_name}/actions/runs?per_page=1&status=success",
        gh_headers(token),
    )
    if status != 200 or not isinstance(data, dict):
        return None
    runs = data.get("workflow_runs") or []
    if not runs:
        return None
    return runs[0].get("updated_at") or runs[0].get("created_at")


def fetch_readme(token: str, repo: Repo) -> str:
    status, data = http(
        "GET",
        f"{GITHUB_API}/repos/{repo.full_name}/readme",
        gh_headers(token),
    )
    if status != 200 or not isinstance(data, dict):
        return ""
    if data.get("encoding") == "base64" and data.get("content"):
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


# --- Classification ------------------------------------------------------- #


STACK_FILE_RULES: list[tuple[str, list[str]]] = [
    ("AWS Lambda", ["serverless.yml", "serverless.yaml", "template.yaml", "template.yml", "samconfig.toml"]),
    ("Vercel", ["vercel.json", ".vercel"]),
    ("GitHub Actions", [".github/workflows"]),
    ("Docker", ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]),
    ("Frontend (Next.js)", ["next.config.js", "next.config.mjs", "next.config.ts"]),
]


def detect_stack(repo: Repo) -> list[str]:
    tags: list[str] = []
    files = repo.files
    paths_lower = {f.lower() for f in files}

    for tag, markers in STACK_FILE_RULES:
        for m in markers:
            ml = m.lower()
            if ml in paths_lower or any(p.startswith(ml + "/") or p == ml for p in paths_lower):
                tags.append(tag)
                break

    if "package.json" in paths_lower:
        pkg = repo.files_content.get("package.json", "") if hasattr(repo, "files_content") else ""
        if pkg:
            try:
                pkg_json = json.loads(pkg)
                deps = {**pkg_json.get("dependencies", {}), **pkg_json.get("devDependencies", {})}
                if "next" in deps and "Frontend (Next.js)" not in tags:
                    tags.append("Frontend (Next.js)")
                if "react" in deps and "Frontend (Next.js)" not in tags and "Frontend (React)" not in tags:
                    tags.append("Frontend (React)")
                if "express" in deps or "fastify" in deps or "@hono/node-server" in deps:
                    if "API/Backend" not in tags:
                        tags.append("API/Backend")
                if "node-telegram-bot-api" in deps or "telegraf" in deps or "grammy" in deps:
                    if "Telegram Bot" not in tags:
                        tags.append("Telegram Bot")
            except json.JSONDecodeError:
                pass

    blob = " ".join(files) + "\n" + repo.readme.lower()
    blob_l = blob.lower()
    name_l = repo.name.lower()

    if any(k in blob_l for k in ["beautifulsoup", "scrapy", "playwright", "selenium", "puppeteer", "cheerio"]) or "scraper" in name_l:
        if "Web Scraping" not in tags:
            tags.append("Web Scraping")

    if any(k in name_l for k in ["bot", "telegram"]) and "Telegram Bot" not in tags:
        if "telegram" in blob_l or "telegraf" in blob_l or "python-telegram-bot" in blob_l:
            tags.append("Telegram Bot")

    if any(k in name_l for k in ["trading", "finance", "vix", "stock", "composer", "fear-greed"]):
        if "Trading/Finance" not in tags:
            tags.append("Trading/Finance")

    if any(k in blob_l for k in ["anthropic", "openai", " llm", "claude-", "gpt-", "langchain"]):
        if "AI/LLM" not in tags:
            tags.append("AI/LLM")

    if "fastapi" in blob_l or "flask" in blob_l or "django" in blob_l:
        if "API/Backend" not in tags:
            tags.append("API/Backend")

    if repo.language == "HTML" and not any(t in tags for t in ["Vercel", "Frontend (Next.js)", "Frontend (React)"]):
        tags.append("Static HTML")

    if not tags:
        tags.append("Local Only")

    return tags


def detect_runtimes(repo: Repo) -> str:
    notes: list[str] = []
    fc = getattr(repo, "files_content", {})

    py_versions: set[str] = set()
    for key in ("setup.py", "pyproject.toml", "runtime.txt", ".python-version"):
        if key in fc:
            for m in re.findall(r"python[_\- ]?(\d+\.\d+)", fc[key], flags=re.I):
                py_versions.add(m)
            for m in re.findall(r"(\d+\.\d+\.\d+)", fc[key]):
                if m.startswith("3."):
                    py_versions.add(".".join(m.split(".")[:2]))
    for key in ("serverless.yml", "serverless.yaml", "template.yaml", "template.yml"):
        if key in fc:
            for m in re.findall(r"python(\d+\.\d+)", fc[key], flags=re.I):
                py_versions.add(m)
    if "requirements.txt" in repo.files and not py_versions and repo.language == "Python":
        py_versions.add("?")

    for v in sorted(py_versions):
        flag = " ⚠️ deprecated" if v in DEPRECATED_PYTHON else ""
        notes.append(f"Python {v}{flag}")

    node_versions: set[str] = set()
    if "package.json" in fc:
        try:
            pkg = json.loads(fc["package.json"])
            engine = pkg.get("engines", {}).get("node", "")
            if engine:
                m = re.search(r"(\d+)", engine)
                if m:
                    node_versions.add(m.group(1))
        except json.JSONDecodeError:
            pass
    if ".nvmrc" in fc:
        m = re.search(r"v?(\d+)", fc[".nvmrc"])
        if m:
            node_versions.add(m.group(1))

    for v in sorted(node_versions):
        flag = " ⚠️ deprecated" if v in DEPRECATED_NODE else ""
        notes.append(f"Node {v}{flag}")

    return ", ".join(notes) if notes else ""


def clean_readme_excerpt(readme: str, max_chars: int = 1500) -> str:
    if not readme:
        return ""
    text = re.sub(r"^---.*?---\s*", "", readme, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_chars]


def describe_with_claude(api_key: str, repo: Repo, stack: list[str]) -> str:
    file_sample = sorted(repo.files)[:40]
    readme_excerpt = clean_readme_excerpt(repo.readme)

    user_content = f"""Write a 1-2 sentence description (max 200 chars) of what this GitHub repo DOES, in plain factual prose. Focus on the project's purpose and what it accomplishes — not its README structure, badges, or "getting started" boilerplate.

Repo name: {repo.name}
GitHub description: {repo.description or "(none)"}
Primary language: {repo.language}
Detected stack: {", ".join(stack) if stack else "(none)"}
Visibility: {"private" if repo.is_private else "public"}

File list (sample):
{chr(10).join(file_sample) if file_sample else "(empty)"}

README excerpt:
{readme_excerpt or "(no README)"}

Output ONLY the description. No quotes, no labels, no preface. If you genuinely cannot tell what it does, write a description based on the repo name and stack — don't say "no description available"."""

    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 200,
        "messages": [{"role": "user", "content": user_content}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        status, data = http("POST", ANTHROPIC_API, headers, body)
    except Exception as e:
        print(f"    Claude error for {repo.name}: {e}", file=sys.stderr)
        return ""
    if status != 200:
        print(f"    Claude {status} for {repo.name}: {data}", file=sys.stderr)
        return ""
    try:
        text = data["content"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return ""
    text = text.strip().strip('"').strip("'")
    text = re.sub(r"\s+", " ", text)
    if len(text) > 350:
        text = text[:347].rstrip() + "..."
    return text


def make_description(repo: Repo, stack: list[str], anthropic_key: str | None) -> str:
    if anthropic_key:
        desc = describe_with_claude(anthropic_key, repo, stack)
        if desc:
            return desc

    if repo.description:
        base = repo.description.strip()
    else:
        base = ""
    if repo.readme and not base:
        cleaned = clean_readme_excerpt(repo.readme, 600)
        lines = [l.strip() for l in cleaned.splitlines()]
        body_lines: list[str] = []
        for l in lines:
            if not l or l.startswith("#") or l.startswith("|"):
                if body_lines:
                    break
                continue
            body_lines.append(l)
            if len(" ".join(body_lines)) > 240:
                break
        base = " ".join(body_lines).strip()
    base = re.sub(r"\s+", " ", base).strip()
    if len(base) > 350:
        base = base[:347].rstrip() + "..."
    if not base:
        stack_str = ", ".join(stack) if stack else "project"
        base = f"{repo.name} ({stack_str})"
    return base


def compute_status(repo: Repo, last_actions_run: str | None = None) -> str:
    if repo.is_archived:
        return "Archived"
    now = datetime.now(tz=timezone.utc)
    pushed = datetime.fromisoformat(repo.pushed_at.replace("Z", "+00:00"))
    most_recent = pushed
    if last_actions_run:
        try:
            run_dt = datetime.fromisoformat(last_actions_run.replace("Z", "+00:00"))
            if run_dt > most_recent:
                most_recent = run_dt
        except ValueError:
            pass
    age_days = (now - most_recent).days
    return "Stale" if age_days > STALE_AFTER_DAYS else "Active"


# --- Notion --------------------------------------------------------------- #


def notion_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_query_all(token: str, database_id: str) -> dict[str, dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        status, data = http(
            "POST",
            f"{NOTION_API}/databases/{database_id}/query",
            notion_headers(token),
            body,
        )
        if status != 200:
            raise RuntimeError(f"Notion query failed: {status} {data}")
        for page in data.get("results", []):
            url_prop = page.get("properties", {}).get("Repo URL", {})
            url = url_prop.get("url")
            if url:
                by_url[url] = page
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return by_url


def text_chunks(s: str) -> list[dict[str, Any]]:
    if not s:
        return []
    return [{"type": "text", "text": {"content": s[:1900]}}]


def build_props(repo: Repo, stack: list[str], runtimes: str, status: str, description: str) -> dict[str, Any]:
    return {
        "Name": {"title": [{"type": "text", "text": {"content": repo.name}}]},
        "Description": {"rich_text": text_chunks(description)},
        "Stack": {"multi_select": [{"name": s} for s in stack]},
        "Language": {"select": {"name": repo.language if repo.language in {"Python", "TypeScript", "JavaScript", "HTML", "Go", "Rust"} else "Other"}},
        "Visibility": {"select": {"name": "Private" if repo.is_private else "Public"}},
        "Status": {"select": {"name": status}},
        "Last updated": {"date": {"start": repo.pushed_at[:10]}},
        "Repo URL": {"url": repo.url},
        "Runtime versions": {"rich_text": text_chunks(runtimes)},
    }


def upsert_page(
    token: str,
    database_id: str,
    existing: dict[str, dict[str, Any]],
    repo: Repo,
    props: dict[str, Any],
) -> str:
    page = existing.get(repo.url)
    if page:
        page_id = page["id"]
        status, _ = http(
            "PATCH",
            f"{NOTION_API}/pages/{page_id}",
            notion_headers(token),
            {"properties": props},
        )
        if status not in (200, 201):
            raise RuntimeError(f"Notion update failed for {repo.name}: {status}")
        return "updated"
    body = {
        "parent": {"type": "database_id", "database_id": database_id},
        "properties": props,
    }
    status, _ = http(
        "POST",
        f"{NOTION_API}/pages",
        notion_headers(token),
        body,
    )
    if status not in (200, 201):
        raise RuntimeError(f"Notion create failed for {repo.name}: {status}")
    return "created"


def mark_deleted(token: str, page: dict[str, Any]) -> None:
    http(
        "PATCH",
        f"{NOTION_API}/pages/{page['id']}",
        notion_headers(token),
        {"properties": {"Status": {"select": {"name": "Deleted"}}}},
    )


# --- Main ----------------------------------------------------------------- #


def main() -> int:
    gh_token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
    notion_token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID") or os.environ.get("NOTION_DATA_SOURCE_ID")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not gh_token or not notion_token or not database_id:
        print("Missing GH_PAT, NOTION_TOKEN, or NOTION_DATABASE_ID", file=sys.stderr)
        return 1
    if not anthropic_key:
        print("WARN: ANTHROPIC_API_KEY not set — falling back to heuristic descriptions", file=sys.stderr)

    print("Listing GitHub repos...", flush=True)
    repos = list_user_repos(gh_token, include_archived=False)
    print(f"  Found {len(repos)} repos", flush=True)

    last_actions: dict[str, str | None] = {}
    for repo in repos:
        print(f"  Scanning {repo.full_name}...", flush=True)
        repo.files = fetch_tree(gh_token, repo)
        repo.readme = fetch_readme(gh_token, repo)
        last_actions[repo.url] = fetch_last_actions_run(gh_token, repo)
        wanted_files = [
            "package.json", ".nvmrc", "pyproject.toml", "setup.py", "runtime.txt",
            ".python-version", "serverless.yml", "serverless.yaml",
            "template.yaml", "template.yml",
        ]
        repo.files_content = {}
        for f in wanted_files:
            if f in repo.files:
                content = fetch_file(gh_token, repo, f)
                if content:
                    repo.files_content[f] = content

    print("Querying existing Notion rows...", flush=True)
    existing = notion_query_all(notion_token, database_id)
    print(f"  {len(existing)} existing rows", flush=True)

    seen_urls: set[str] = set()
    created = updated = 0

    for repo in repos:
        seen_urls.add(repo.url)
        stack = detect_stack(repo)
        runtimes = detect_runtimes(repo)
        status = compute_status(repo, last_actions.get(repo.url))
        description = make_description(repo, stack, anthropic_key)
        props = build_props(repo, stack, runtimes, status, description)
        action = upsert_page(notion_token, database_id, existing, repo, props)
        if action == "created":
            created += 1
        else:
            updated += 1
        print(f"    [{action}] {repo.name} | {','.join(stack)} | {status}", flush=True)

    deleted = 0
    for url, page in existing.items():
        if url in seen_urls:
            continue
        cur_status = (
            page.get("properties", {})
            .get("Status", {})
            .get("select", {}) or {}
        ).get("name")
        if cur_status == "Deleted":
            continue
        mark_deleted(notion_token, page)
        deleted += 1
        print(f"    [deleted] {url}", flush=True)

    print(f"\nDone. created={created} updated={updated} marked_deleted={deleted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
