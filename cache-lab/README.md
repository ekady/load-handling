# Cache Lab

A small Docker-based benchmark that measures how indexing and caching change read latency for a chat-message workload. It compares four tiers of the same query:

1. **Unindexed table scan** (slowest)
2. **Non-covering B-Tree index**
3. **Covering index**
4. **Redis cache lookup** (fastest)

## Architecture

```
                 ┌──────────────────┐
   writes ─────► │  mysql-primary   │  port 3306
                 │  (binlog on)     │
                 └──────────────────┘
                          │  (intended replication)
                          ▼
                 ┌──────────────────┐
   reads  ─────► │  mysql-replica   │  port 3307, read-only
                 └──────────────────┘
                          │
                          ▼
                 ┌──────────────────┐
                 │   redis-cache    │  port 6379
                 └──────────────────┘
                          ▲
                 ┌────────┴─────────┐
                 │   app-client     │  Python 3.9, runs the benchmark
                 └──────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Defines `mysql-primary`, `mysql-replica`, `redis-cache`, and `app-client`. |
| `init-data.sql` | Runs on first boot of each MySQL container: creates the `repl` user, the `chat_db` database, and the two message tables. |
| `cache-test.py` | The benchmark script: populates data, then times the four query tiers. |

### Schema

Both tables live in `chat_db` and receive identical rows:

- `messages_unindexed` — no secondary index; `conversation_id` lookups require a full scan.
- `messages_indexed` — has a composite index `idx_conversation_time (conversation_id, created_at)`, which serves both the filter and the `ORDER BY ... DESC` without a filesort.

## Prerequisites

- Docker and Docker Compose
- Ports `3306`, `3307`, and `6379` free on the host

## Quick start

```bash
cd cache-lab
docker compose up -d
```

Wait for MySQL to finish its init script (the first boot takes longer because it runs `init-data.sql`), then start the client and install dependencies:

```bash
docker compose exec app-client pip install mysql-connector-python redis
docker compose exec app-client python cache-test.py
```

> `app-client` already installs `mysql-connector-python` and `redis` on startup, so the `pip install` step is only needed if you rebuild the container before that finishes.

To tear everything down:

```bash
docker compose down          # keep data volumes
docker compose down -v       # also remove volumes and re-init on next boot
```

## What the benchmark does

### Experiment 1 — Data ingestion

Checks `messages_indexed` on the replica for its current row count. If fewer than 500,000 rows exist, it inserts batches of 500,000 rows (random `conversation_id` 1–1000, random `user_id` 100–999) into **both** tables via the primary, committing per batch. If the data is already there, it skips straight to the benchmark.

### Experiment 2 — Four-tier read benchmark

All four tiers run the same logical query: *latest 20 messages for conversation 42*.

| Tier | Query | Why it differs |
|------|-------|----------------|
| 1 | `SELECT * FROM messages_unindexed WHERE conversation_id = 42 ORDER BY created_at DESC LIMIT 20` | Full table scan over 500k rows. |
| 2 | `SELECT * FROM messages_indexed WHERE conversation_id = 42 ...` | Index narrows the rows, but `SELECT *` forces a lookup back to the clustered index for every match (non-covering). |
| 3 | `SELECT user_id, message_body FROM messages_indexed WHERE conversation_id = 42 ...` | Index-only scan — every requested column is in the index, so no table access is needed. |
| 4 | `GET messages:42` in Redis (populated from Tier 3, TTL 60s) | Skips the database entirely. |

The script prints per-tier timings and a summary block, e.g.:

```
-- Summary of performance tiers --
Tier 1 (Unindexed SQL): 0.2912 seconds
Tier 2 (Non-covering B-Tree): 0.0034 seconds
Tier 3 (Covering Index): 0.0019 seconds
Tier 4 (Redis Cache): 0.0002 seconds
```

## Expected takeaways

- **Indexing dominates the wins.** Going from a table scan (Tier 1) to an index (Tier 2) is typically the largest single improvement, often 1–2 orders of magnitude.
- **Covering beats non-covering.** Avoiding the row lookup by selecting only indexed columns (Tier 3) roughly halves the remaining cost.
- **Caching is the last mile.** Redis (Tier 4) removes query planning and disk I/O entirely, but the absolute gain over an already-fast covering-index query is small in this dataset — caching pays off most when the underlying query is expensive or the read volume is high.

## Known limitations / things to try

- **Replication is not wired up.** `mysql-primary` runs with `--log-bin` and `mysql-replica` with `--read-only=1`, but the replica never runs `CHANGE MASTER TO ...`, so it does not actually stream the binlog. Each container initializes its own copy of `init-data.sql`. To make it a real replica, add a replication setup step (e.g. an init script that waits for the primary and issues `CHANGE MASTER TO` with the `repl` user created in `init-data.sql`).
- **The benchmark reads from the replica host** (`mysql-replica`), while writes go to the primary — useful for demonstrating read scaling, but note that Tier 1–3 timings therefore come from a separate data set unless replication is enabled.
- **Tier 4 pre-warms its own cache key**, so it measures a *hit*. To measure a cold read plus cache-miss path, delete the key first (`redis-cli DEL messages:42`).
- **Timing is single-shot.** Each tier is measured once with `time.time()`. For more reliable numbers, run several iterations and report the median, or use `time.perf_counter()`.
- **`cache-test.py` is idempotent-ish:** re-running it skips the 500k-row ingestion if the data is already present, so you can iterate on the query tiers without re-seeding.

## Tuning ideas

- Add a **stale-aside** or **write-through** cache invalidation path: update/delete the Redis key when a message is inserted.
- Try a **third table** with a full covering index on `(conversation_id, created_at, user_id, message_body)` to show the trade-off of wider indexes (faster reads, slower writes, more storage).
- Benchmark **read replicas under concurrent load** with a tool like `sysbench` to see where the replica saturates.
