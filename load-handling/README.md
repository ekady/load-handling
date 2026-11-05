# Load Handling — System Design Experiments

Hands-on experiments for learning how load balancing and load handling work, using
Docker Compose + nginx as a reverse proxy.

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

### Tear down

```bash
docker compose down
```
