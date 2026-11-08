# Load Handling — System Design Experiments

Hands-on experiments for learning how load balancing and load handling work, using
Docker Compose + nginx as a reverse proxy.

| # | Experiment | Config state |
|---|---|---|
| 1 | [Normal Round Robin](#experiment-1-normal-round-robin) | no `weight=` / balancing directives |
| 2 | [Weighted Round Robin](#experiment-2-weighted-round-robin) | `weight=3, 1, 1` |
| 3 | [IP Hashing](#experiment-3-ip-hashing) | `ip_hash;` |
| 4 | [Failure Handling: `max_fails`](#experiment-4-failure-handling-max_fails--fail_timeout) | `weight=` + `max_fails=` / `fail_timeout=` ← **current** |

> **Note:** `nginx.conf` is mounted read-only into the container, so edits on the
> host only take effect after a reload:
> ```bash
> docker exec load-balancer nginx -t && docker exec load-balancer nginx -s reload
> ```

---

## Experiment 1: Normal Round Robin

**Goal:** Demonstrate nginx distributing incoming requests evenly across three
backend servers using its default round-robin algorithm.

### Architecture

```
                 ┌───► web1:80   ("Node web dari 1")
                 │
Browser ──► load-balancer ──┼───► web2:80   ("Node web ini adalah 2")
            (nginx, :8080)  │
                 └───► web3:80   ("Node web ini 3")
```

- **3 backend replicas** (`web1`, `web2`, `web3`) — each an `nginx:alpine` container
  serving a unique `index.html` so you can see which backend answered.
- **1 load balancer** (`load-balancer`) — nginx proxying port `8080` → backends.

### Files

| File | Purpose |
|---|---|
| `docker-compose.yml` | Defines the 4 containers, mounts the config, publishes `8080` |
| `nginx.conf` | Load balancer config: upstream pool + reverse proxy rules |

### `docker-compose.yml`

```yaml
version: '3.8'

services:
  web1:
    image: nginx:alpine
    container_name: web1
    command: /bin/sh -c "echo 'Node web dari 1' > /usr/share/nginx/html/index.html && exec nginx -g 'daemon off;'"
  web2:
    image: nginx:alpine
    container_name: web2
    command: /bin/sh -c "echo 'Node web ini adalah 2' > /usr/share/nginx/html/index.html && exec nginx -g 'daemon off;'"
  web3:
    image: nginx:alpine
    container_name: web3
    command: /bin/sh -c "echo 'Node web ini 3' > /usr/share/nginx/html/index.html && exec nginx -g 'daemon off;'"
  load-balancer:
    image: nginx:alpine
    container_name: load-balancer
    ports:
      - "8080:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - web1
      - web2
      - web3
```

### `nginx.conf`

```nginx
events {
    worker_connections 1024;
}

http {
    upstream backend_servers {
        server web1:80;
        server web2:80;
        server web3:80;
    }

    server {
        listen 80;

        location / {
            proxy_pass http://backend_servers;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }
    }
}
```

> The listing above is the **baseline** for Experiment 1. The file on disk is
> currently in the Experiment 4 state (weights + `max_fails`/`fail_timeout`)
> — remove the balancing directives to run this experiment, then reload nginx.

### How it works

1. `upstream backend_servers` defines the pool of backends. By default nginx uses
   **round-robin**: request *n* goes to the *(n mod 3)*-th server.
2. `location /` matches every request; `proxy_pass http://backend_servers` forwards
   it to whichever backend round-robin selected.
3. `proxy_set_header Host $host` preserves the original `Host:` header so the
   backend knows which domain was requested; `X-Real-IP $remote_addr` passes the
   real client IP (otherwise the backend only ever sees the proxy's IP).
4. `worker_connections 1024` (in the `events` block) allows each nginx worker to
   hold up to 1024 simultaneous connections. Note: in proxy mode each client
   request uses **2** slots (client→LB and LB→backend).
5. The compose file bind-mounts `./nginx.conf` into the container **read-only**
   (`:ro`), so the container always runs *this* config instead of the stock one.

### Run it

```bash
docker compose up -d
```

> **Note:** the `version:` key in compose is obsolete and only produces a warning —
> it's safe to delete.

### Observe the round robin

With curl (fresh request each time, no cache):

```bash
for i in $(seq 1 9); do curl -s http://localhost:8080/; echo; done
```

Expected output:

```
Node web dari 1
Node web ini adalah 2
Node web ini 3
Node web dari 1
Node web ini adalah 2
Node web ini 3
...
```

Or per-backend distribution:

```bash
for i in $(seq 1 9); do curl -s http://localhost:8080/; done | sort | uniq -c
```

Expected: `3` occurrences of each node.

In a browser, refresh `http://localhost:8080/` repeatedly — the displayed text
should rotate through the three nodes.

### Troubleshooting / gotchas found during this experiment

1. **`event` vs `events`** — the block must be spelled `events { ... }` (plural).
   A typo gives `unknown directive "event"`. Validate configs quickly with:
   ```bash
   docker exec load-balancer nginx -t
   ```
2. **Browser caching can hide the load balancing** — nginx serves static files
   with `ETag`/`Last-Modified` headers. If two backends return an **identical
   ETag** (same file size + same modification time) while serving *different*
   content, the browser's conditional refresh gets `304 Not Modified` and keeps
   showing the cached copy — it looks like only one node is ever hit, even though
   round-robin works fine underneath.
   - Verify server-side behavior with cache-busting requests:
     ```bash
     curl -s -H 'Cache-Control: no-cache' http://localhost:8080/
     ```
   - Use a hard refresh (`Cmd/Ctrl + Shift + R`) to bypass the browser cache.
   - Permanent fixes: give each backend's file a unique size/mtime, or disable
     caching at the proxy:
     ```nginx
     location / {
         add_header Cache-Control "no-store";
     }
     ```
3. **`host not found in upstream "web1"`** when running `nginx -t` outside the
   compose network — expected; the backend hostnames only resolve inside the
   compose network.

---

## Experiment 2: Weighted Round Robin

**Goal:** Direct traffic to backends **proportionally to their capacity** instead
of evenly, by assigning a `weight` to each server in the upstream pool.

**Scenario:** web1 is a bigger machine (3× capacity), web2 and web3 are small
(1× each) → weights `3 : 1 : 1`.

### Code change

Only the `upstream` block changes — everything else stays the same as Experiment 1:

```nginx
http {
    upstream backend_servers {
        server web1:80 weight=3;   # ← 60% of traffic
        server web2:80 weight=1;   # ← 20% of traffic
        server web3:80 weight=1;   # ← 20% of traffic
    }

    server {
        listen 80;

        location / {
            proxy_pass http://backend_servers;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }
    }
}
```

`weight` defaults to `1`, so Experiment 1's bare config is really `1 : 1 : 1`.

### How it works

- **Long-run share** of each backend = `weight / total weights`.
  Here total = 3 + 1 + 1 = **5**, so web1 gets 3/5 = **60%**, web2 and web3
  get 1/5 = **20%** each.
- nginx uses **smooth** weighted round-robin: each server's slots are spread
  across the cycle rather than clumped together (you get `1 · 1 · 2 · 1 · 3`,
  not `1 · 1 · 1 · 2 · 3`), which avoids bursty traffic on the heavy backend.
- The pattern **repeats every `sum of weights` requests** — here a cycle of 5.
  Observed cycle (verified):

  ```
  cycle:   1  1  2  1  3  │ 1  1  2  1  3  │ 1  1  2  1  3
             └──── 3 slots for web1 ────┘
  ```

  The exact interleaving depends on nginx's internal tie-breaking, but what is
  guaranteed is the **proportion**: every cycle of 5 requests gives web1 three
  slots, web2 one, web3 one. A window that doesn't line up with cycle
  boundaries (e.g. `... 1 3 1 1 2 ...`) can also make web1 look adjacent —
  that's the next cycle starting, not a different pattern.

### Apply the change

Edit `nginx.conf` on the host, then test + reload (no container restart needed):

```bash
docker exec load-balancer nginx -t && docker exec load-balancer nginx -s reload
```

### Observe the weighted distribution

Sequence (15 requests = exactly 3 cycles):

```bash
for i in $(seq 1 15); do curl -s http://localhost:8080/; done | awk '{printf "%s ", $NF} END {print ""}'
```

Expected output:

```
1 1 2 1 3 1 1 2 1 3 1 1 2 1 3
```

Distribution:

```bash
for i in $(seq 1 15); do curl -s http://localhost:8080/; done | sort | uniq -c
```

Expected:

```
   9 Node web dari 1
   3 Node web ini 3
   3 Node web ini adalah 2
```

Which is exactly **3 : 1 : 1** → web1 **60%** (weight 3), web2 **20%**, web3 **20%**.

### Experiment variations

| Weights | Cycle length (sum) | web1 share |
|---|---|---|
| `1,1,1` (Experiment 1) | 3 | 33% |
| `3,1,1` (this experiment) | 5 | 60% |
| `6,2,2` | 10 | 60% |
| `5,1,1` | 7 | 71% |

The exact sequence grows with the weights, but the **proportions stay constant**:
`share = weight / sum`.

Related directives to explore later:

- `least_conn` — send to the backend with the fewest active connections
  (combines with `weight`, e.g. `server web1:80 weight=3 least_conn;`)
- `ip_hash` — same client IP always hits the same backend → **see Experiment 3**
- `backup`, `down` — mark a server as backup-only or administratively down

---

## Experiment 3: IP Hashing

**Goal:** Make routing **sticky** — the same client always reaches the same
backend, instead of rotating through them.

**Scenario:** backends hold per-node state (user sessions, file uploads, a local
cache). Round-robin would scatter one user's requests across three servers, and
every request would miss the other nodes' state.

### Code change

Only one line in the `upstream` block (weights removed to isolate the experiment —
`ip_hash` can be combined with `weight=`):

```nginx
http {
    upstream backend_servers {
        ip_hash;               # ← route by client IP instead of round-robin
        server web1:80;
        server web2:80;
        server web3:80;
    }

    server {
        listen 80;

        location / {
            proxy_pass http://backend_servers;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }
    }
}
```

### How it works

For each request nginx hashes the **client IP** and picks a slot:

1. IPv4: only the **first three octets** are used — the last octet is *ignored*
   (confirmed in nginx source: `iphp->addrlen = 3`). IPv6 uses all 16 bytes.
2. `hash = 89;` then for each of the 3 bytes: `hash = (hash * 113 + byte) % 6271`
3. `slot = hash % total_weight` → walk the server list by weight to that slot.

Consequences:

- **Same client → same backend, always** (sticky), as long as the pool is unchanged.
- **Everyone on the same /24 network counts as one client.** All Docker containers
  on one bridge network (e.g. `172.19.0.x`), an office LAN, or carrier NAT users
  hash *identically* → they all land on a **single** backend.
- Which backend a given IP lands on is an implementation detail — don't hardcode
  expectations; they change with IPs, weights, and pool membership.

### Apply the change

Edit `nginx.conf`, then test + reload (no restart needed):

```bash
docker exec load-balancer nginx -t && docker exec load-balancer nginx -s reload
```

### Observe the stickiness

**Same client, repeated requests** — all go to one node (here: always `3`):

```bash
for i in $(seq 1 9); do curl -s http://localhost:8080/; done | awk '{printf "%s ", $NF} END {print ""}'
```

```
3 3 3 3 3 3 3 3 3
```

vs. Experiment 1's rotation `1 2 3 1 2 3 1 2 3`. The distribution check shows
`9` for one node and nothing for the others.

**Different client networks → spread.** Verified on this machine (hash computed
by hand from the algorithm above first, then confirmed live):

| Client | Source IP seen by nginx | Result |
|---|---|---|
| host browser/curl | `192.168.65.1` | → web3 |
| container on `172.19.0.0/24` | `172.19.0.x` | → web3 |
| container on `172.23.0.0/24` | `172.23.0.x` | → web2 |
| container on `172.24.0.0/24` | `172.24.0.x` | → web1 |

*(Your mapping will differ — your IPs hash differently. The invariant is:
each client network is stuck to exactly one backend.)*

Reproduce the multi-network part:

```bash
docker network create --subnet 172.23.0.0/24 net-a
docker network create --subnet 172.24.0.0/24 net-b
docker network connect net-a load-balancer
docker network connect net-b load-balancer

docker run --rm --network net-a curlimages/curl -s http://load-balancer/   # some node
docker run --rm --network net-b curlimages/curl -s http://load-balancer/   # likely a different one
docker run --rm --network load-handling_default curlimages/curl -s http://load-balancer/

# cleanup
docker network disconnect net-a load-balancer
docker network disconnect net-b load-balancer
docker network rm net-a net-b
```

> A neat side-effect you may notice: sequential `docker run --rm` containers get
> the **same recycled IP** after the previous one exits — so they all map to the
> same node. Run them concurrently (or on different networks) for distinct IPs.

### Gotchas

1. **Same /24 → one node.** ip_hash ignores the last octet, so an entire Docker
   bridge subnet or office LAN piles onto **one** backend — the other two idle.
   With NAT'd clients (corporate proxy, mobile carriers) many *different* users
   share one IP and also collapse onto one node. If that node is hot, use
   cookie-based stickiness instead.
2. **Pool changes reshuffle most mappings.** Adding/removing a server rehashes
   nearly everything — sticky sessions break. The alternative
   `hash $remote_addr consistent;` (consistent hashing) only moves the clients
   that must move.
3. **Backend failure fails over.** If the assigned node is down/marked failed,
   nginx tries other peers (up to 20 tries) — clients get routed elsewhere until
   it recovers, then snap back.
4. **IP changes break stickiness too** — DHCP/mobile re-addressing moves a client
   to a different node; anything truly per-user should live in a real session
   store (Redis/DB), with ip_hash only as a caching optimization.

---

## Experiment 4: Failure Handling: `max_fails` / `fail_timeout`

**Goal:** Understand what nginx does when a backend dies — when it stops using
it, when it retries, and what the client actually experiences.

**Scenario:** stop one backend mid-traffic (`docker stop web2`) and measure
every request instead of just watching the browser.

### Code change

Per-server failure directives in the `upstream` block (balancing directives
commented out to isolate the experiment):

```nginx
http {
    upstream backend_servers {
        # ip_hash; # Use ip_hash for session persistence
        # least_conn; # Use least_conn for load balancing based on the least number of connections
        server web1:80 weight=3 max_fails=3 fail_timeout=10s;
        server web2:80 weight=1 max_fails=2 fail_timeout=30s;
        server web3:80 weight=1 max_fails=1 fail_timeout=5s;
    }
    ...
}
```

### What `max_fails` and `fail_timeout` do

```nginx
server web2:80 max_fails=2 fail_timeout=3s;
```

1. nginx counts **consecutive failures** (connect errors, timeouts) per backend.
2. When the count reaches `max_fails` → the backend enters a **penalty box: it is
   skipped for the next `fail_timeout` seconds**.
3. When `fail_timeout` expires → the backend becomes eligible **again — forever,
   by design**, because nginx must periodically re-probe it to notice it
   recovered.

Key implications:

- `max_fails=2` does **not** mean "stop using it after 2 tries." It means
  *"skip for `fail_timeout`, probe again, skip, probe again…"* — slow probe
  requests continue for as long as the backend is down.
- `fail_timeout` is both the **penalty duration** *and* the **recovery-probe
  interval**.
- Defaults (if omitted): `max_fails=1 fail_timeout=10s`.
- `max_fails=0` = never consider it failed (always try it).

### The experiment

```bash
docker stop web2
# time every request instead of eyeballing the browser:
for i in $(seq 1 16); do
  curl -s --max-time 70 -o /tmp/b.txt -w "%{time_total} %{http_code} " http://localhost:8080/
  printf '%s node=%s\n' "$(date +%T)" "$(awk '{printf "%s", $NF}' /tmp/b.txt)"
  sleep 1
done
docker logs load-balancer --since 5m   # watch connect errors
```

**Measured result** (web2 stopped, `fail_timeout=3s` at the time):

```
20:07:42  web2 stopped
request 1:    38.85s  → 200 node=1     ← first probe (web2 still "healthy")
requests 2-7: ~0.004s → 200            ← web2 in penalty box ✓
request 8:     3.07s  → 200 node=1     ← probe after fail_timeout elapsed
requests 9-15: ~0.004s → 200           ← penalty box ✓
request 16:    3.12s  → 200 node=1     ← probe again
```

Error log shows exactly the 3 stalls:

```
13:08:21 connect() failed (113: Host is unreachable) ... upstream: "http://172.19.0.4:80/"
13:08:31 connect() failed (113: Host is unreachable)
13:08:43 connect() failed (113: Host is unreachable)
```

Everything else is sub-5ms and web2 never appears in responses — **the mechanism
works**. The three findings below are why it can *feel* broken.

### Finding 1 — you cannot reload while a backend is down

```
$ docker exec load-balancer nginx -t
nginx: [emerg] host not found in upstream "web2:80" in /etc/nginx/nginx.conf:10
```

nginx resolves static upstream names **at config-load time**, and Docker's DNS
**stops resolving a stopped container's name**. So while web2 is down, every
`nginx -t` / reload fails → the running config (and its failure counters) stay
exactly as they were. Start the backend first, or comment its `server` line out
before reloading.

> Related: a **successful** reload spawns fresh workers → all failure counters
> reset to zero. Reloading repeatedly while testing wipes the state you're
> observing.

### Finding 2 — each failure is slow

nginx waits for the TCP connect to the dead IP before counting a failure:

- Nothing answers ARP → kernel gives up after **~3s** (`EHOSTUNREACH` /
  `connect() failed (113)`); the first attempt after death retransmitted for
  **~39s** in our test.
- nginx's own ceiling: `proxy_connect_timeout` defaults to **60 seconds**.

So one probe cycle ≈ `fail_timeout` (skip) + connect-failure time → with
`fail_timeout=3s`, roughly **one slow request every ~6 seconds** — far more
"slow loads" than `max_fails=2` suggests. Fix: `proxy_connect_timeout 1s;` in
the `server`/`location` block, and/or a larger `fail_timeout`.

### Finding 3 — failures are masked as `200 OK`

Default `proxy_next_upstream` includes `error timeout`: after web2's connect
fails, nginx **silently retries the same request on the next healthy server**.
The browser sees a slow-but-successful response — never an error page. To
observe failures at all, use `curl -w '%{time_total}'` timing or the error log.

### Remedies

| Goal | Fix |
|---|---|
| Failures detectable in ~1s instead of 3–39s | `proxy_connect_timeout 1s;` (in `server`/`location`, not `upstream`) |
| Fewer slow probes | Raise `fail_timeout` (e.g. `30s` → one probe per ~30s; trade-off: slower recovery detection) |
| Backend fully out of rotation, no probes | Comment out its `server` line, then reload (do this **before** stopping it, or start it again to reload) |
| Never skip — always try | `max_fails=0` |
| See failures in tests | Timed `curl` or `docker logs load-balancer` (responses are retried → always 200) |
| Instant failover without slow client requests | Active health checks — not available in open-source nginx (nginx Plus, Lua/njs, external tools) |

### Restore

```bash
docker start web2
docker exec load-balancer nginx -t && docker exec load-balancer nginx -s reload
```

*(Reloading after the backend returns also resets all failure counters.)*

---

### Tear down

```bash
docker compose down
```
