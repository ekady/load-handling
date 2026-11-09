# Data Layer — System Design Experiments

Hands-on experiments for learning how the data layer behaves under load: SQL vs
NoSQL write throughput, the cost of an index, and how an application shards data
across nodes. Uses Docker Compose with MySQL 8, MongoDB 5, and a Python client.

| # | Experiment | What it shows |
|---|---|---|
| 1 | [Ingesting 10,000 Messages](#experiment-1-ingesting-10000-messages-sql-vs-nosql) | Row-store batch insert vs document-store `insert_many` |
| 2 | [B-Tree Composite vs Full Table Scan](#experiment-2-b-tree-composite-vs-full-table-scan) | Composite index vs unindexed scan |
| 3 | [Hash-Based Sharding Router](#experiment-3-hash-based-application-sharding-router) | Deterministic `conversation_id → shard` routing |

> **⚠️ Read [Known issues](#known-issues--gotchas) first.** Experiment 2 currently
> produces an *inverted* result (scan appears faster than the index) because the
> replica is **not actually replicating** — see
> [Issue 3](#issue-3-the-replica-has-no-replication-channel).

---

## Architecture

```
                      ┌─────────────────────────────┐
                      │  app-client (python:3.9)    │
                      │  lab-test.py                │
                      └──────┬───────────┬──────────┘
                             │           │
              writes / reads │           │ writes / reads
                             ▼           ▼
              ┌──────────────────┐   ┌──────────────────┐   ┌─────────────┐
              │  mysql-primary   │   │  mysql-replica   │   │  mongodb    │
              │  :3306           │   │  :3307           │   │  :27017     │
              │  server-id=1     │   │  server-id=2     │   │  chat_db    │
              │  --log-bin       │   │  --read-only     │   │  .messages  │
              │  chat_db.*       │   │  chat_db.*       │   └─────────────┘
              └──────────────────┘   └──────────────────┘
                       │
                       └── binlog enabled, but NO replication channel configured
                           (replica never receives rows — see Issue 3)
```

### Services

| Service | Image | Port | Role |
|---|---|---|---|
| `mysql-primary` | `mysql:8.0` | `3306→3306` | Write target. `server-id=1`, binlog on (ROW format) |
| `mysql-replica` | `mysql:8.0` | `3307→3306` | Intended read replica. `server-id=2`, `--read-only=1` |
| `mongodb` | `mongo:5.0` | `27017` | Document store for the NoSQL comparison |
| `app-client` | `python:3.9` | — | Mounts `./` at `/app`, installs deps, runs `lab-test.py` |

### Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Defines the 4 services, mounts the init script into both MySQL containers |
| `init-data.sql` | First-boot script: creates the `repl` user, `chat_db`, and both message tables |
| `lab-test.py` | Runs all three experiments end-to-end |

---

## Quick start

```bash
docker compose down -v     # ← -v is mandatory, see Issue 1
docker compose up -d

# wait for app-client to finish pip install (takes ~15s on first boot)
docker exec app-client python -c "import mysql.connector, pymongo" && \
  docker exec app-client python lab-test.py
```

### `init-data.sql`

```sql
create user if not exists 'repl'@'%' identified by 'repl_password';
grant replication slave on *.* to 'repl'@'%';
flush privileges;

create database if not exists chat_db;
use chat_db;

create table messages_unindexed (
    id bigint auto_increment primary key,
    conversation_id bigint not null,
    user_id int not null,
    message_body text,
    created_at timestamp default current_timestamp
);

create table messages_indexed (
    id bigint auto_increment primary key,
    conversation_id int not null,
    user_id int not null,
    message_body text,
    created_at timestamp default current_timestamp,
    index idx_conversation_time (conversation_id, created_at)
);
```

The two tables are **identical except for one thing**: `messages_indexed` carries
a composite B-Tree index on `(conversation_id, created_at)`. That single difference
is the whole point of Experiment 2 — same data, same query, different access path.

> `repl` + the `grant replication slave` grant are already set up for when
> replication gets wired (see [Issue 3](#issue-3-the-replica-has-no-replication-channel)).
> The user exists but nothing authenticates with it yet.

---

## Experiment 1: Ingesting 10,000 Messages (SQL vs NoSQL)

**Goal:** Compare bulk-write throughput of a relational row store against a
document store for the same logical workload.

### What the script does

1. Generates 10,000 tuples `(conversation_id, user_id, message_body)` with
   `conversation_id ∈ [1, 500]`, `user_id ∈ [100, 999]`.
2. `executemany(INSERT ...)` into `messages_indexed`, then `commit()`.
3. Builds the same 10,000 records as BSON documents (plus a `created_at`
   epoch float) and calls `insert_many()`.
4. Repeats the SQL insert into `messages_unindexed` so Experiment 2 has data to
   scan.

### Observed output

```
=== EXPERIMENT 1: Ingesting 10,000 Messages (SQL vs NoSQL) ===
Inserting messages into MySQL primary...
MySQL Insertion Time: 0.07 seconds
=== NoSQL Batch write ====
✓ NoSQL (MongoDB Document Store) 10,000 Inserts: 41.95 ms
```

### How to read it

- `0.07 s` = **70 ms** for MySQL vs **42 ms** for MongoDB on this run — the same
  order of magnitude, both trivially fast for 10k rows.
- The comparison is **not apples-to-apples**: MySQL does one `executemany` in a
  *single transaction* (one round-trip + one fsync on commit), while
  `insert_many` is a `bulkWrite` with its own write-concern semantics. MongoDB's
  `created_at` is also a client-side `time.time()` float, so it does slightly
  more work per document.
- **What actually matters at this scale:** neither number is interesting. Both
  are dominated by network round-trips, which is why `executemany`/batched writes
  beat row-by-row inserts by orders of magnitude. Try replacing `executemany`
  with a loop of `execute()` + `commit()` each iteration to see the real cost of
  per-row transactions (typically 100–1000× slower).

---

## Experiment 2: B-Tree Composite vs Full Table Scan

**Goal:** Measure how much a composite index changes the cost of a
`WHERE ... ORDER BY ... LIMIT` query.

### The query (run twice — once per table)

```sql
SELECT *
FROM messages_{indexed|unindexed}
WHERE conversation_id = %s
ORDER BY created_at DESC
LIMIT 20;
```

`conversation_id = 42` is used as the probe value.

### How it works

- **`messages_indexed`** — the optimizer can use `idx_conversation_time`
  `(conversation_id, created_at)`. Because the index key order already matches
  the requested `ORDER BY`, MySQL walks the index backwards from
  `conversation_id = 42` and stops after 20 rows. **Cost ∝ LIMIT, not table size.**
- **`messages_unindexed`** — no index on `conversation_id`, so MySQL must read
  every row, sort it, and then take 20. **Cost ∝ table size.**

The gap should widen linearly as the table grows — that's the lesson.

### ⚠️ Observed output (currently wrong — see Issue 3)

```
=== EXPERIMENT 2: B-Tree Composite vs Full Table Scan ===
✓ SQL (B-Tree Composite Index) Query Time: 0.92 ms
✓ SQL (Full Table Scan) Query Time: 0.25 ms
```

The "Full Table Scan" appears **3.7× faster** than the index. That is not a
real result — it's an artifact:

> The queries run against `mysql-replica`, whose `messages_unindexed` table is
> **empty (0 rows)**. Scanning zero rows is instant. The primary holds ~30,000
> rows at this point, so the two queries are not measuring the same data.

**Do not draw conclusions from this experiment until Issue 3 is fixed.** With
data present on the table being scanned, the correct ordering is
(index ≪ scan) — e.g. from an earlier run where both queries hit a populated
table:

```
✓ SQL (B-Tree Composite Index) Query Time: 1.04 ms
✓ SQL (Full Table Scan) Query Time: 4.11 ms
```

---

## Experiment 3: Hash-Based Application Sharding Router

**Goal:** Show application-side sharding — routing each conversation to a shard
by hashing its ID, without asking the database.

```python
num_shards = 2

def get_shard(conversation_id):
    shard_id = hash(conversation_id) % num_shards
    return shard_id
```

### Observed output

```
--- EXPERIMENT 3: Hash-Based Application Sharding Router ---
Conversation ID 8 routed to Shard 0
Conversation ID 82 routed to Shard 0
Conversation ID 88 routed to Shard 0
Conversation ID 23 routed to Shard 1
Conversation ID 45 routed to Shard 1
Conversation ID 54 routed to Shard 0
Conversation ID 46 routed to Shard 0
Conversation ID 15 routed to Shard 1
Conversation ID 29 routed to Shard 1
Conversation ID 11 routed to Shard 1
```

### How to read it

- **Deterministic:** the same `conversation_id` always lands on the same shard,
  so a conversation's messages stay co-located and a `WHERE conversation_id = X`
  query only needs to hit one shard.
- **Uniform-ish:** with `hash(int) == int` for CPython, `id % 2` just splits
  odd/even — you can verify by hand. Real deployments use a better mixer
  (`crc32`, `md5`, or consistent hashing) because sequential/auto-increment IDs
  are already well distributed by parity, but *correlated* ID schemes (e.g. IDs
  derived from a timestamp) can skew badly under a naive `%`.
- **This is the trade-off:** `hash(id) % N` spreads load evenly, but changing `N`
  (adding a shard) **reshuffles nearly every key** — a re-sharding event. That's
  exactly what *consistent hashing* exists to avoid: it only moves the keys that
  absolutely must move.

---

## Known issues & gotchas

### Issue 1: `init-data.sql` only runs against an empty data directory

The MySQL entrypoint executes `/docker-entrypoint-initdb.d/*.sql` **once**, on
first boot, when `/var/lib/mysql` is empty. After that it never runs again —
editing the script has no effect on an existing container.

```bash
docker compose down -v    # -v removes the anonymous data volume
docker compose up -d
```

`down` **without** `-v` keeps the volume, so your edits silently don't apply.
This is the single most confusing thing about this setup.

Verify it worked:

```bash
docker logs mysql-primary 2>&1 | grep init-data
docker exec mysql-primary mysql -uroot -pmy-secret-pw \
  -e "use chat_db; show tables;"
# expect: messages_indexed / messages_unindexed
```

### Issue 2: a syntax error mid-script leaves you with a half-created database

The script originally lacked `;` after the first `create table`. Because
statements are separated by `;`, the parser hit the bare `create` keyword on the
next line and aborted:

```
ERROR 1064 (42000) at line 8: You have an error in your SQL syntax; ...
near 'create table message_indexed ('
```

Everything **before** line 8 had already committed (that's why `chat_db` and the
`repl` user existed while the tables didn't), and the entrypoint does **not**
roll back or retry. Result: a database with zero tables, and no obvious error
unless you read `docker logs`.

**Symptom:** `Unknown database` / `Table 'chat_db.x' doesn't exist` even though
"the init script creates it."

**Lesson:** always check `docker logs mysql-primary` after `up -d`. The entrypoint
prints `running /docker-entrypoint-initdb.d/init-data.sql` followed by any error.

### Issue 3: the replica has no replication channel

**This is the big one, and it's why Experiment 2 is invalid.**

`mysql-replica` gets its `chat_db` **from its own copy of `init-data.sql`**, not
from the primary. The compose file mounts the script into both containers, so
both end up with identical *schemas* — but:

```bash
docker exec mysql-replica mysql -uroot -pmy-secret-pw -e "show slave status\G"
# (no output — there is no replication channel)
```

- `--server-id=2` only *labels* the server. It does not connect it to anything.
- No `CHANGE MASTER TO` / `CHANGE REPLICATION SOURCE TO` is ever issued.
- Therefore **rows inserted on the primary never appear on the replica.**

| | primary | replica |
|---|---|---|
| `chat_db` | ✅ | ✅ (own init script) |
| tables | ✅ | ✅ (own init script) |
| rows | ~30,000 | **0** |
| replicating | n/a | ❌ no |

**Fix (when you want it):** point the replica at the primary and start the
channel. Using the credentials `init-data.sql` already creates:

```bash
docker exec mysql-replica mysql -uroot -pmy-secret-pw -e "
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST='mysql-primary',
  SOURCE_USER='repl',
  SOURCE_PASSWORD='repl_password',
  SOURCE_LOG_FILE='mysql-bin.000001',
  SOURCE_LOG_POS=0,
  GET_SOURCE_PUBLIC_KEY=1;
START REPLICA;
SHOW REPLICA STATUS\G\G"
```

Check `Replica_IO_Running: Yes` and `Replica_SQL_Running: Yes`, then rows will
flow. Note MySQL 8 also needs `REPLICATION SLAVE` (or `REPLICATION CLIENT`)
grants — already present in `init-data.sql`.

> **Why not just mount the init script on the replica instead?** That's what's
> happening now, and it only fixes the *schema* problem (error 1049). Two
> independent copies of a database are not a replica — any read you take there is
> silently stale/empty. For a read-scaling experiment you need real replication.

### Issue 4: re-running the script accumulates rows

`lab-test.py` has no `TRUNCATE`/cleanup at the top, so every run adds another
10,000 rows to each MySQL table (and 10,000 documents to MongoDB). After a few
runs the tables hold 30k+, which quietly changes Experiment 2's timings.

```bash
# reset the data without rebuilding containers
docker exec mysql-primary mysql -uroot -pmy-secret-pw -e "
  TRUNCATE TABLE chat_db.messages_indexed;
  TRUNCATE TABLE chat_db.messages_unindexed;"
docker exec mongodb mongosh --quiet --eval "db.messages.deleteMany({})"
```

Or just `docker compose down -v` for a completely clean slate.

### Issue 5: `app-client` races its dependencies

The container's command is `pip install ... && tail -f /dev/null`, so there's a
window after `up -d` where the container is up but `import mysql.connector`
fails with `ModuleNotFoundError`.

`depends_on` only waits for the *container* to start, not for pip to finish.
Wait for the import to succeed before running the test:

```bash
until docker exec app-client python -c "import mysql.connector, pymongo" 2>/dev/null; do
  sleep 3
done
```

### Issue 6: `version: '3.8'` is obsolete

The `version:` key in Compose V2 is deprecated and only produces a warning —
safe to delete.

### Issue 7: table naming (`message_indexed` vs `messages_indexed`)

The script originally created `message_indexed` (singular) while `lab-test.py`
queried `messages_indexed` (plural) → `Table 'chat_db.messages_indexed' doesn't
exist`. The schema now uses `messages_indexed`, matching `messages_unindexed`.
If you reintroduce the singular form, Experiment 1 fails on the first insert.

---

## Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| `Unknown database 'chat_db'` | Init script never ran, or ran and failed | `docker compose down -v && up -d`, then check `docker logs mysql-primary` |
| `Table 'chat_db.messages_indexed' doesn't exist` | Init died mid-script (syntax error) | Read `docker logs mysql-primary` for `ERROR 1064`, fix the SQL, `down -v` |
| Edited `init-data.sql`, no effect | Init only runs on an empty volume | `docker compose down -v` |
| `ModuleNotFoundError: mysql` | pip still installing | Wait and retry (Issue 5) |
| Scan appears faster than index | Replica table is empty | See Issue 3 |
| Row counts keep growing | No cleanup between runs | See Issue 4 |
| Replica reads return stale/empty data | No replication channel | See Issue 3 |

---

### Tear down

```bash
docker compose down -v     # -v to also drop the data volumes
```
