#!/usr/bin/env python3
"""Discover ATS career boards, collect current postings, and sync to TeamPeach's D1 Worker API.

Standard-library only. The collector runs on GitHub Actions; neither Render nor a user's PC
is used for scheduled collection.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED_FILE = ROOT / "boards.seed.json"
USER_AGENT = "TeamPeach-Jobs-Collector/1.0 (+https://teampeach.org/tools/peachjobs/)"
ATS = {
    "ashby": {
        "domains": ["jobs.ashbyhq.com"],
        "api": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "board": "https://jobs.ashbyhq.com/{slug}",
    },
    "greenhouse": {
        "domains": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
        "api": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "board": "https://boards.greenhouse.io/{slug}",
    },
    "lever": {
        "domains": ["jobs.lever.co"],
        "api": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "board": "https://jobs.lever.co/{slug}",
    },
}
RESERVED = {"api", "static", "assets", "_next", "favicon.ico", "robots.txt", "embed", "css", "js", "images"}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def request_bytes(url: str, method: str = "GET", timeout: int = 25, retries: int = 4,
                  headers: dict[str, str] | None = None) -> bytes:
    base_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    base_headers.update(headers or {})
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=base_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            if exc.code in (403, 429, 500, 502, 503, 504) and attempt < retries - 1:
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = min(30, max(1, int(retry_after)))
                except ValueError:
                    delay = min(20, 2 ** attempt)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(min(20, 2 ** attempt))
    raise RuntimeError("request retries exhausted")


def request_json(url: str, **kwargs: Any) -> Any:
    return json.loads(request_bytes(url, **kwargs).decode("utf-8", errors="replace"))


def slug_from_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value)
        slug = urllib.parse.unquote(parsed.path.strip("/").split("/", 1)[0]).strip()
    except Exception:
        return None
    if not slug or slug.lower() in RESERVED or not SLUG_RE.fullmatch(slug):
        return None
    if re.fullmatch(r"[0-9a-f-]{30,}", slug, re.I):
        return None
    return slug


def discover_candidates(days: int = 30) -> dict[str, set[str]]:
    """Recent Wayback captures + urlscan public scans; no broad site crawling."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    result = {key: set() for key in ATS}
    for ats, config in ATS.items():
        for host in config["domains"]:
            query = urllib.parse.urlencode({
                "url": host, "matchType": "domain", "filter": "statuscode:200",
                "fl": "original", "collapse": "urlkey", "from": start, "output": "json",
            })
            try:
                rows = request_json("https://web.archive.org/cdx/search/cdx?" + query, timeout=120, retries=3)
                for row in rows[1:] if isinstance(rows, list) and rows else []:
                    if row:
                        slug = slug_from_url(str(row[0]))
                        if slug:
                            result[ats].add(slug)
                log(f"Wayback {host}: {len(result[ats])} candidate boards")
            except Exception as exc:
                log(f"Wayback discovery warning for {host}: {exc}")
            # urlscan indexes recent public browser scans and supplements archive lag.
            scan_q = urllib.parse.quote(f"page.domain:{host}", safe="")
            try:
                rows = request_json(f"https://urlscan.io/api/v1/search/?q={scan_q}&size=1000", timeout=60, retries=2)
                for row in rows.get("results", []) if isinstance(rows, dict) else []:
                    slug = slug_from_url(str((row.get("page") or {}).get("url") or ""))
                    if slug:
                        result[ats].add(slug)
                log(f"urlscan {host}: {len(result[ats])} candidate boards")
            except Exception as exc:
                log(f"urlscan discovery warning for {host}: {exc}")
    return result


def board_api_url(ats: str, slug: str) -> str:
    return ATS[ats]["api"].format(slug=urllib.parse.quote(slug, safe=""))


def board_page_url(ats: str, slug: str) -> str:
    return ATS[ats]["board"].format(slug=urllib.parse.quote(slug, safe=""))


def validate_board(ats: str, slug: str) -> bool:
    """Verify a discovered slug against the provider's actual public JSON API.

    Several ATS front doors reject HEAD even when their normal GET endpoint works,
    so validation deliberately checks the same response shape the collector consumes.
    """
    try:
        payload = request_json(board_api_url(ats, slug), timeout=18, retries=2)
        jobs = get_jobs_payload(ats, payload)
        return isinstance(jobs, list)
    except Exception:
        return False


def humanize(slug: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-_.]+", " ", slug)).strip().title() or slug


def parse_date(value: Any, epoch_ms: bool = False) -> str | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value) / (1000 if epoch_ms or value > 10_000_000_000 else 1)
            dt = datetime.fromtimestamp(number, tz=timezone.utc)
        else:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def strip_html(value: Any, max_len: int = 500) -> str:
    text = re.sub(r"<[^>]*>", " ", str(value or ""))
    return re.sub(r"\s+", " ", unescape(text)).strip()[:max_len]


def get_jobs_payload(ats: str, data: Any) -> Any:
    if ats in ("ashby", "greenhouse"):
        return data.get("jobs") if isinstance(data, dict) else None
    return data if isinstance(data, list) else None


def normalize_job(ats: str, slug: str, raw: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    if ats == "ashby":
        if raw.get("isListed") is False:
            return None
        title = raw.get("title") or ""
        company_name = raw.get("companyName") or raw.get("organizationName")
        external_id = raw.get("id") or raw.get("jobUrl")
        location = raw.get("location") or ""
        workplace = raw.get("workplaceType") or ("remote" if raw.get("isRemote") else "")
        employment = raw.get("employmentType") or ""
        published = parse_date(raw.get("publishedAt") or raw.get("updatedAt"))
        url = raw.get("jobUrl") or ""
        department, team = raw.get("department") or "", raw.get("team") or ""
    elif ats == "greenhouse":
        title = raw.get("title") or ""
        company_obj = raw.get("company") or {}
        company_name = (company_obj.get("name") if isinstance(company_obj, dict) else company_obj) or raw.get("companyName")
        external_id = raw.get("id") or raw.get("absolute_url")
        loc = raw.get("location") or {}
        location = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        workplace = "remote" if "remote" in location.lower() else ""
        employment = ""
        published = parse_date(raw.get("first_published") or raw.get("updated_at"))
        url = raw.get("absolute_url") or ""
        department, team = "", ""
    else:
        categories = raw.get("categories") or {}
        title = raw.get("text") or ""
        company_name = raw.get("company") or raw.get("companyName")
        external_id = raw.get("id") or raw.get("hostedUrl")
        location = categories.get("location") or ""
        workplace = raw.get("workplaceType") or ("remote" if "remote" in location.lower() else "")
        employment = categories.get("commitment") or ""
        published = parse_date(raw.get("createdAt"), epoch_ms=True)
        url = raw.get("hostedUrl") or ""
        department, team = categories.get("department") or "", categories.get("team") or ""

    if not title or not external_id or not url or not published:
        return None
    try:
        published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
    if published_dt < now - timedelta(days=30) or published_dt > now + timedelta(days=1):
        return None

    workplace_l = str(workplace).lower()
    location_l = str(location).lower()
    if "hybrid" in workplace_l:
        remote = "hybrid"
    elif "remote" in workplace_l or "remote" in location_l:
        remote = "remote"
    elif workplace_l in ("onsite", "on-site", "on site"):
        remote = "onsite"
    else:
        remote = "unspecified"

    job_url = str(url).strip()
    if not job_url.startswith("https://"):
        return None
    apply_url = raw.get("applyUrl") or raw.get("apply_url") or job_url
    if not str(apply_url).startswith("https://"):
        apply_url = job_url
    return {
        "ats": ats,
        "board_key": slug,
        "external_id": str(external_id)[:160],
        "company": strip_html(company_name or humanize(slug), 160),
        "title": strip_html(title, 240),
        "department": strip_html(department, 120),
        "team": strip_html(team, 120),
        "employment_type": strip_html(employment, 100),
        "location": strip_html(location, 300),
        "remote_status": remote,
        "published_at": published,
        "job_url": job_url[:1800],
        "apply_url": str(apply_url)[:1800],
        "source_url": board_page_url(ats, slug),
    }


def worker_request(base_url: str, token: str, path: str, method: str = "GET",
                   payload: dict[str, Any] | None = None, timeout: int = 45) -> Any:
    url = base_url.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = request_bytes(url, method=method, timeout=timeout, retries=4, headers=headers) if body is None else None
    if body is not None:
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    data = response.read()
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise RuntimeError("Cloudflare Worker rejected the ingest token. Check both secrets match.") from exc
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(min(20, 2 ** attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise
                time.sleep(min(20, 2 ** attempt))
    return json.loads((data or b"{}").decode("utf-8", errors="replace"))


def fetch_board(ats: str, slug: str, cutoff_now: datetime) -> tuple[str, str, list[dict[str, Any]], str | None]:
    try:
        data = request_json(board_api_url(ats, slug), timeout=35, retries=3)
        raw_jobs = get_jobs_payload(ats, data)
        if not isinstance(raw_jobs, list):
            raise ValueError("ATS returned an unexpected jobs payload")
        jobs = [job for item in raw_jobs if isinstance(item, dict)
                if (job := normalize_job(ats, slug, item, cutoff_now)) is not None]
        return ats, slug, jobs, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ats, slug, [], "board_not_found"
        return ats, slug, [], f"http_{exc.code}"
    except Exception as exc:
        return ats, slug, [], str(exc)[:220]


def main() -> int:
    worker_url = os.environ.get("CLOUDFLARE_WORKER_URL", "").strip()
    token = os.environ.get("CLOUDFLARE_INGEST_TOKEN", "").strip()
    if not worker_url.startswith("https://") or not token:
        log("Set CLOUDFLARE_WORKER_URL and CLOUDFLARE_INGEST_TOKEN in repository variables/secrets.")
        return 2

    health = request_json(worker_url.rstrip("/") + "/v1/jobs/health", timeout=15, retries=2)
    if not health.get("ok"):
        log("The TeamPeach jobs database is not ready: " + str(health))
        return 2

    existing_result = worker_request(worker_url, token, "/v1/ingest/boards")
    existing = existing_result.get("boards", [])
    registry: dict[str, dict[str, dict[str, str]]] = {ats: {} for ats in ATS}
    for board in existing:
        ats, slug = board.get("ats"), board.get("board_key")
        if ats in registry and slug:
            registry[ats][slug.lower()] = {"ats": ats, "board_key": slug,
                "company": board.get("company") or humanize(slug),
                "source_url": board.get("source_url") or board_page_url(ats, slug)}

    candidates = discover_candidates(days=30)
    seed = json.loads(SEED_FILE.read_text(encoding="utf-8")) if SEED_FILE.exists() else {}
    new_boards: list[dict[str, str]] = []
    for ats in ATS:
        candidate_set = set(candidates[ats]) | set(seed.get(ats, []))
        fresh = [slug for slug in sorted(candidate_set, key=str.lower)
                 if slug.lower() not in registry[ats]]
        log(f"{ats}: {len(registry[ats])} known boards; validating {len(fresh)} new candidates")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            valid = [slug for slug, ok in zip(fresh, pool.map(lambda x: validate_board(ats, x), fresh)) if ok]
        for slug in valid:
            board = {"ats": ats, "board_key": slug, "company": humanize(slug),
                     "source_url": board_page_url(ats, slug)}
            registry[ats][slug.lower()] = board
            new_boards.append(board)
        log(f"{ats}: {len(valid)} new boards verified")

    if new_boards:
        for offset in range(0, len(new_boards), 250):
            worker_request(worker_url, token, "/v1/ingest/boards", "POST",
                           {"boards": new_boards[offset:offset + 250]})
        log(f"Registered {len(new_boards)} new career boards in Cloudflare D1")

    all_boards = [(ats, row["board_key"]) for ats in ATS for row in registry[ats].values()]
    max_boards = int(os.environ.get("MAX_BOARDS_PER_RUN", "0") or 0)
    if max_boards > 0:
        all_boards = all_boards[:max_boards]
    log(f"Starting 30-day scan of {len(all_boards)} employer boards")

    now = datetime.now(timezone.utc)
    batch: list[dict[str, Any]] = []
    checked = succeeded = failures = jobs_count = 0
    batch_size = 200
    workers = max(2, min(16, int(os.environ.get("COLLECTOR_CONCURRENCY", "12"))))

    def flush() -> None:
        nonlocal batch
        if batch:
            worker_request(worker_url, token, "/v1/ingest/jobs", "POST", {"jobs": batch})
            batch = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_board, ats, slug, now) for ats, slug in all_boards]
        for future in concurrent.futures.as_completed(futures):
            ats, slug, jobs, error = future.result()
            checked += 1
            if error:
                failures += 1
                if error != "board_not_found":
                    log(f"Board scan warning {ats}/{slug}: {error}")
            else:
                succeeded += 1
            jobs_count += len(jobs)
            batch.extend(jobs)
            if len(batch) >= batch_size:
                flush()
            if checked % 250 == 0 or checked == len(all_boards):
                log(f"Progress {checked}/{len(all_boards)} boards; {jobs_count:,} qualifying jobs; {failures} board errors")
    flush()

    summary = {"collected_at": now.isoformat(), "boards_total": len(all_boards),
               "boards_succeeded": succeeded, "boards_with_errors": failures,
               "jobs_sent": jobs_count, "days": 30}
    worker_request(worker_url, token, "/v1/ingest/run", "POST", summary)
    log("Completed: " + json.dumps(summary))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
