# Load Handling — System Design Experiments

Hands-on experiments for learning how load balancing and load handling work, using
Docker Compose + nginx as a reverse proxy.

| # | Experiment | Config state |
|---|---|---|
| 1 | [Normal Round Robin](#experiment-1-normal-round-robin) | no `weight=` directives |
| 2 | [Weighted Round Robin](#experiment-2-weighted-round-robin) | `weight=3, 1, 1` ← **current** |

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
> currently in the Experiment 2 state (with `weight=` directives) — remove the
> weights to run this experiment, then reload nginx.

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
- `ip_hash` — same client IP always hits the same backend (session persistence)
- `backup`, `down` — mark a server as backup-only or administratively down

---

### Tear down

```bash
docker compose down
```
