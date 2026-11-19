# TeamPeach Jobs Collector

A separate scheduled data pipeline for **PeachJobs**, TeamPeach's 30-day job-search tool.

The pipeline follows the same division of work as TeamPeach's domain-data system:

1. GitHub Actions runs the collector every four hours at minute 17 (00:17, 04:17, 08:17, 12:17, 16:17 and 20:17 Africa/Lagos).
2. The collector discovers public Ashby, Greenhouse and Lever employer boards using recent public archive captures, urlscan results and a small seed list.
3. It reads the persistent employer-board registry from TeamPeach's Cloudflare Worker, validates newly discovered boards, fetches public job-posting APIs and retains only postings published within the last 30 days.
4. It sends normalized batches to the protected Worker ingestion endpoint.
5. The Worker writes records into Cloudflare D1. TeamPeach's public PeachJobs page reads and filters the D1 records through the same Worker.

GitHub Actions can occasionally start a scheduled workflow later than its nominal time. Manual dispatch remains available in the Actions tab.

No Render processing, Supabase dependency, always-on server, resume upload or local PC scheduler is required. The collector uses Python's standard library only.

## Source strategy

The primary source layer is employer career infrastructure, not a small selection of general remote-job feeds. ATS APIs are public per-company endpoints; they do not offer a global search endpoint. This collector therefore maintains a verified employer-board registry and incrementally discovers additional boards.

Supported boards:

- Ashby public job-board API (`jobs.ashbyhq.com`)
- Greenhouse public job-board API (`boards-api.greenhouse.io`)
- Lever public postings API (`api.lever.co`)

The initial seed list is deliberately a starting point, not the final universe. The registry grows as archive discovery and urlscan identify new employer boards. A failed source scan is logged and does not erase previously collected listings.

Only standard public listing fields are retained: employer, title, team/department, location, work arrangement, employment type, publication date and the original application URL. Full job-description text is not copied into the database.

## Cloudflare setup

Follow the detailed, click-by-click instructions in [CLOUDFLARE_SETUP.md](CLOUDFLARE_SETUP.md).

The jobs API extends TeamPeach's existing Worker at:

`https://teampeach-domain-api.creatorletofficial.workers.dev`

The database is named `teampeach-jobs`; its D1 binding must be named `JOBS_DB`. The binding is declared in `AgentCalmz/domains/wrangler.toml` so the Worker deploy and Git configuration stay consistent.

## GitHub Actions configuration

In this repository, open **Settings → Secrets and variables → Actions** and add:

**Repository variable**

- `CLOUDFLARE_WORKER_URL` = `https://teampeach-domain-api.creatorletofficial.workers.dev`

**Repository secret**

- `CLOUDFLARE_INGEST_TOKEN` = the exact same value as the Cloudflare Worker secret `JOBS_INGEST_TOKEN`.

Then open **Actions → TeamPeach job collection (every 4 hours)** and select **Run workflow** to verify the first complete run.

## Data window and freshness

The collector deliberately rejects listings without a valid publication timestamp and listings older than 30 days. This prevents undated or old live posts from being incorrectly presented as newly published. D1 keeps a rolling 45-day retention window. `published_at`, `first_seen_at` and `last_seen_at` remain separate so publication age is not confused with collector discovery time.

A source outage does not mean a job is closed. Listings carry their last-seen timestamp and can be labelled stale when they have not been observed recently. Historical coverage starts accumulating from the first successful collection; deleted listings from before that time cannot be reconstructed from a current ATS endpoint.

## Local validation

```bash
python -m unittest discover -s tests -v
python -m py_compile collector/collect.py
```

No external Python packages are required.
