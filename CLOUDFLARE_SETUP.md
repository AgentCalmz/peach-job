# Cloudflare setup for TeamPeach PeachJobs

This guide creates the D1 database, connects the existing TeamPeach Worker to it, installs the schema, and securely connects GitHub Actions.

**You only have to do the Cloudflare setup once.** Your PC is not used for recurring collection. GitHub Actions performs all scheduled job-board collection every four hours.

## What you are connecting

```text
GitHub Actions (every 4 hours)
        |
        | protected ingestion requests
        v
Existing Cloudflare Worker
teampeach-domain-api
        |
        | D1 binding: JOBS_DB
        v
Cloudflare D1 database
teampeach-jobs
        |
        | public read API
        v
TeamPeach PeachJobs page
https://teampeach.org/tools/peachjobs/
```

The updated Worker is in [AgentCalmz/domains](https://github.com/AgentCalmz/domains). It is the same Worker already used for TeamPeach domain data. Do not create another Worker or replace its existing name/hostname.

## Part 1 — Create the D1 database in Cloudflare

1. Sign in at [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Select the Cloudflare account that owns the existing `teampeach-domain-api` Worker. If you have multiple Cloudflare accounts, this matters: the database and Worker must belong to the same account.
3. In the left navigation, open **Workers & Pages**, then find **D1 SQL Database** (the navigation label can appear under Storage & databases).
4. Select **Create database**.
5. Enter this exact database name:

   `teampeach-jobs`

6. Leave the data location on **Automatic** unless you have a specific data residency requirement. Cloudflare recommends automatic placement; Africa is not currently one of the available manual location hints.
7. Select **Create**.
8. Open the new database and copy its **Database ID**. It is a UUID-like value. Do not copy the database name in place of the ID.

Keep the tab open or save the ID somewhere private. The ID is not the ingestion password, but it is needed in the Worker configuration.

## Part 2 — Put the D1 ID into the Worker configuration

Open [AgentCalmz/domains → wrangler.toml](https://github.com/AgentCalmz/domains/blob/main/wrangler.toml).

The file currently contains this placeholder:

```toml
[[d1_databases]]
binding = "JOBS_DB"
database_name = "teampeach-jobs"
database_id = "REPLACE_WITH_TEAMPEACH_JOBS_D1_ID"
migrations_dir = "jobs/migrations"
```

Replace only the placeholder after `database_id =` with the Database ID you copied from Cloudflare. Keep the quotation marks and do not change the other values.

For example, the line should look like:

```toml
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Use your real ID, not the example above. Save/commit the change to the `main` branch.

**You do not need to create a second Worker or manually add a different D1 binding in the dashboard.** The `wrangler.toml` configuration declares the correct binding name (`JOBS_DB`) and the deployment in Part 3 attaches it to the existing Worker.

## Part 3 — Apply the database tables and deploy the Worker

This is a one-time deployment step, not the recurring data collection. It can be done from your Windows PC using Git Bash. It does not mean your PC will be used to search for jobs.

### 3A. Install/check Node.js

If Node.js is not installed, install the current Node.js LTS release from [nodejs.org](https://nodejs.org/). Open Git Bash and check:

```bash
node --version
npm --version
```

### 3B. Download the Worker repository

In Git Bash:

```bash
git clone https://github.com/AgentCalmz/domains.git
cd domains
```

If you already have a local clone, enter that folder and run `git pull` so it includes the updated `wrangler.toml` and Worker code.

Check that `wrangler.toml` now contains your real D1 database ID before continuing.

### 3C. Authorize Wrangler with Cloudflare

Run:

```bash
npx wrangler@4 login
```

A browser tab will open. Sign in to Cloudflare and authorize Wrangler for the account that owns `teampeach-domain-api`.

### 3D. Apply the schema to the remote D1 database

From the `domains` repository root, run:

```bash
npx wrangler@4 d1 migrations apply teampeach-jobs --remote
```

Wrangler should show that it is applying migration `0001_init.sql` to the remote database. Confirm the operation when prompted.

This creates the three tables used by PeachJobs:

- `job_boards` — the persistent employer/ATS board registry.
- `jobs` — normalized job postings.
- `job_collection_runs` — collection history and run statistics.

The migration is already stored in the repository at `jobs/migrations/0001_init.sql`. Do not create the tables manually in a separate way and then attempt to apply the same migration again.

### 3E. Create the private ingestion token

Generate a long random value using a password manager (at least 32 random bytes). Do not use a password you already use elsewhere.

Run:

```bash
npx wrangler@4 secret put JOBS_INGEST_TOKEN
```

When Wrangler prompts for the value, paste the random token and press Enter. Save the same token in your password manager. Do not paste it into GitHub source files, the public website, or this chat.

This secret protects the data-ingestion endpoints. Public PeachJobs search requests do not need this secret.

### 3F. Deploy the updated existing Worker

Run:

```bash
npx wrangler@4 deploy
```

This deploys the Worker named `teampeach-domain-api` from the repository configuration, with its new D1 binding and new jobs endpoints, while retaining its existing domain-data routes.

Wait for Wrangler to report a successful deployment. If it reports that the database ID is still the placeholder, stop and correct `wrangler.toml` before trying again.

## Part 4 — Verify the Worker

Open these URLs in your browser:

**Database health**

`https://teampeach-domain-api.creatorletofficial.workers.dev/v1/jobs/health`

Expected result (the total will initially be zero):

```json
{
  "ok": true,
  "service": "teampeach-jobs",
  "database": "ready",
  "total": 0,
  "window_days": 30
}
```

**Job statistics**

`https://teampeach-domain-api.creatorletofficial.workers.dev/v1/jobs/stats`

It should return valid JSON with zero counts initially. A database error or HTTP 503 means the D1 binding/schema is not ready; do not proceed to the GitHub run until this is fixed.

## Part 5 — Connect GitHub Actions to the Worker

Open [AgentCalmz/peach-job → Settings → Secrets and variables → Actions](https://github.com/AgentCalmz/peach-job/settings/secrets/actions).

### 5A. Add a repository variable

Under **Variables**, choose **New repository variable**:

- Name: `CLOUDFLARE_WORKER_URL`
- Value: `https://teampeach-domain-api.creatorletofficial.workers.dev`

Save it.

### 5B. Add a repository secret

Under **Secrets**, choose **New repository secret**:

- Name: `CLOUDFLARE_INGEST_TOKEN`
- Secret: the exact same random token that you entered for `JOBS_INGEST_TOKEN` in Part 3E.

Save it. The two names are intentionally different, but the values must match exactly.

Do not create a variable for the token and do not put the token in `boards.seed.json` or any source file.

## Part 6 — Run the first collection

1. Open [AgentCalmz/peach-job → Actions](https://github.com/AgentCalmz/peach-job/actions).
2. Select **TeamPeach job collection (every 4 hours)**.
3. Select **Run workflow**, choose `main`, then start it.
4. Open the new run and wait for the collector step to finish. The run can take time because it discovers employers and checks their public ATS boards.
5. If successful, open the health endpoint again. Its `total` should now be greater than zero if qualifying postings were found.
6. Open [PeachJobs](https://teampeach.org/tools/peachjobs/) and test a search with a role such as `software engineer`, then set the date window to 30 days.

## Troubleshooting

- **Health returns 503 / database not ready:** verify the D1 ID in `wrangler.toml`, ensure migration `0001_init.sql` was applied to the remote database, and redeploy the Worker.
- **Collector returns 401:** the GitHub secret `CLOUDFLARE_INGEST_TOKEN` does not exactly match the Worker secret `JOBS_INGEST_TOKEN`. Update one so the values match.
- **Collector says database is not ready:** fix the health endpoint first; collector ingestion cannot work until it returns `ok: true`.
- **No job listings found:** check the Actions log for source/ATS errors. Some boards may be temporarily unavailable, removed, or return no recently published jobs.
- **Do not share the ingestion token** in issues, commits, screenshots, or messages.

## Usage and cost note

The Cloudflare D1 Free plan currently includes 5 million rows read per day, 100,000 rows written per day and 5 GB of aggregate storage. Exceeding the daily free read/write limits causes D1 queries to fail until the daily reset; monitor D1 usage in Cloudflare after the first few collection runs. Six collections per day will make more frequent writes than the earlier once-daily schedule. See [Cloudflare D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/) and [D1 limits](https://developers.cloudflare.com/d1/platform/limits/).
