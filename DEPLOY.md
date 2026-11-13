# Deploying DataSentry

One process serves the console, the API and the WebSocket on one port. There is
no separate frontend to build and no separate service to deploy — the console
is static files served by the same FastAPI app, which is why this document is
short.

```
┌─────────────────────────────────────────┐
│  datasentry  (one container, one port)  │
│                                         │
│   /            home page + playground    │
│   /console     console (static, no build)│
│   /api/v1      REST                      │
│   /ws/incidents WebSocket push           │
│   /healthz     liveness                  │
│   /readyz      readiness                 │
│   /metrics     Prometheus                │
│                                         │
│   /data        SQLite control plane  ◄── needs a persistent volume
└─────────────────────────────────────────┘
```

---

## Before anything else: two things that decide whether it works

### 1. The volume is not optional

The control plane is SQLite on disk. On Render, Railway, Fly, Heroku and Cloud
Run the filesystem is **ephemeral** — it is destroyed on every restart, every
deploy and every free-tier spin-down, and the service keeps reporting healthy
while it happens. Without a mounted volume you will lose the incidents, the
dispositions and the append-only audit log, and the first sign of it will be a
console that looks like a fresh install.

The Fly and Compose manifests mount one; the Render blueprint uses the free plan (no disk) and relies on the snapshot below. If you write your own, mount a
volume at `/data` and set:

```
DS_DATA_DIR=/data
DS_DB_PATH=/data/datasentry.db
DS_WAREHOUSE_PATH=/data/warehouse.db
```

### 2. The image ships a seeded snapshot

Seeding takes minutes, far longer than any platform's startup timeout, so it
happens at **build** time: the Docker build runs the demo once and bakes a
compact snapshot (`py ds.py snapshot`) into the image. On boot, `ds.py serve`
copies it into `/data` only if `/data` has no database yet, so a fresh or
ephemeral deployment comes up populated and a persistent one is never
overwritten. Built with `--build-arg SEED_DEMO=0`, the image ships empty and
the console shows a first-run panel; then seed it once by hand:

```bash
# Render (only needed with SEED_DEMO=0, or to re-roll the world)
render ssh <service> -- python ds.py demo

# Fly
fly ssh console -C "python ds.py demo"

# Docker / Compose
docker compose run --rm datasentry python ds.py demo
```

---

## Render (recommended, free)

A blueprint is committed at [`render.yaml`](render.yaml).

1. Push this repository to GitHub.
2. In the Render dashboard: **New → Blueprint**, pick the repository, **Apply**.
3. Render reads `render.yaml`: Docker runtime, **free** plan, `/healthz` as the
   health check. The first build takes about 5 minutes, because it runs the
   demo once (~3 min) and bakes a seeded snapshot into the image.
4. Optional: set `GROQ_API_KEY` **or** `GEMINI_API_KEY` when prompted (both have
   a free tier). Without one, the deterministic `heuristic` reasoner runs, so the
   site works with no keys at all. Both are `sync: false` and never committed.
5. Open `https://<service>.onrender.com`. The home page, the playground, the
   console at `/console` and the API docs at `/api/docs` are all live.

**What the free plan does and does not keep.** Every boot restores the baked
snapshot into the empty `/data`, so a cold start after the 15-minute idle
spin-down shows a populated warehouse within seconds. Changes made through the
console (dispositions, approvals, audit-log entries) last until the next restart
or deploy, then go back to the snapshot. To keep them, set `plan: starter` and
uncomment the `disk` block in `render.yaml`. The snapshot only seeds an empty
disk, and never overwrites one.

To ship an empty control plane instead, build with `--build-arg SEED_DEMO=0`.

### Why not Vercel or Netlify?

They run serverless functions: no long-lived process, no WebSocket push for
`/ws/incidents`, a read-only filesystem for SQLite, and a 10–60 s request
limit. DataSentry is a stateful service with a background control plane, so it
belongs on a container host (Render, Fly, Railway, Koyeb, Cloud Run, Hugging
Face Spaces). Every one of those runs the same `Dockerfile` unchanged.

## Fly.io

[`fly.toml`](fly.toml) is committed.

```bash
fly launch --no-deploy          # claim the app name
fly volumes create datasentry_data --size 1
fly secrets set GROQ_API_KEY=gsk_...
fly deploy
fly ssh console -C "python ds.py demo"
```

`min_machines_running = 1` and a single machine are deliberate: the control
plane is one SQLite file, so a second machine on the same volume is not a
scale-out, it is two writers.

## Railway, Koyeb, Cloud Run, anything that runs a container

```bash
docker build -t datasentry .
docker run -p 8787:8787 -v datasentry-data:/data \
  -e DS_HOST=0.0.0.0 -e DS_DATA_DIR=/data \
  -e DS_DB_PATH=/data/datasentry.db \
  -e DS_WAREHOUSE_PATH=/data/warehouse.db \
  -e GROQ_API_KEY=gsk_... \
  datasentry
```

The container binds `PORT` when the platform injects one and `DS_PORT`
otherwise, so the same image runs locally and on a PaaS unchanged.

Cloud Run scales to zero and has no persistent local disk; if you deploy there,
either accept that the control plane is ephemeral (fine for a demo) or move the
store behind a network filesystem.

## Plain VM, systemd

```ini
[Unit]
Description=DataSentry
After=network.target

[Service]
User=datasentry
WorkingDirectory=/opt/datasentry
Environment=DS_HOST=0.0.0.0
Environment=DS_DATA_DIR=/var/lib/datasentry
Environment=DS_LOG_JSON=1
EnvironmentFile=/etc/datasentry/env
ExecStart=/opt/datasentry/.venv/bin/python ds.py serve
Restart=always

[Install]
WantedBy=multi-user.target
```

Put the API key in `/etc/datasentry/env` with mode `0600` rather than in the
unit file, which is world-readable.

---

## Configuration

Everything is environment variables; [`.env.example`](.env.example) is the
annotated list. The ones that matter in a deployment:

| Variable | Why |
|---|---|
| `DS_HOST=0.0.0.0` | Bind outside the container. The default is `127.0.0.1`. |
| `DS_DATA_DIR`, `DS_DB_PATH`, `DS_WAREHOUSE_PATH` | Point at the mounted volume. |
| `GROQ_API_KEY` *or* `GEMINI_API_KEY` | Model-backed reasoning. Both have a free tier. Without either, the deterministic reasoner answers and everything still works. |
| `DS_PROFILE_SALT` | **Change it.** It is what makes the hashed sample values in a profile non-reversible. A default salt on real data is not a hash, it is an encoding. |
| `DS_LOG_JSON=1` | Structured logs for an aggregator. |
| `DS_CORS_ORIGINS` | Only for a split deployment. Empty means same-origin, which is correct when one process serves both. |

`PORT` is read when the platform injects it. `DS_PORT` wins locally.

---

## Health checks

| Path | Answers | Use it for |
|---|---|---|
| `/healthz` | is the process alive | Load balancer and platform health check. Touches no database, so polling it every few seconds is free. |
| `/readyz` | is the control plane readable and the lineage index built | Readiness gate. Returns `503` when the store is unreadable, and reports `seeded: false` on a fresh deployment. |
| `/metrics` | Prometheus text format | Scraping. |
| `/api/v1/health/reasoning` | which provider is answering, which models, what it costs | Confirming a key landed. |

Liveness and readiness are separate because the answers imply different
actions: a failed liveness check means restart me, a failed readiness check
means stop sending traffic while I finish coming up — and restarting would
only make that take longer.

---

## Security before this faces anyone but you

The threat model and its architectural mitigations are in
[`docs/08-security.md`](docs/08-security.md). What a deployment has to add:

- **There is no authentication.** Every API call is anonymous; the audit log
  records `x-ds-user` if a proxy sets it and `anonymous` if not. Put it behind
  an identity-aware proxy, or on a private network, before anyone else can
  reach it. The approval endpoints execute actions.
- **Change `DS_PROFILE_SALT`.** See the table above.
- **The playground is public by design.** `/api/v1/playground/*` runs the
  detector and the SQL resolver on visitors' input. It writes nothing, caps
  every input before parsing, and is rate limited per client and globally —
  but it is compute anyone can spend. The global window is the one that
  protects the process behind a shared proxy address.
- **Leave `DS_CORS_ORIGINS` empty** unless you actually split the deployment.
  Same-origin is not a limitation here, it is the correct configuration.
- **The reasoning identity is read-only by construction** (ADR-008) and the
  action enum contains nothing irreversible — no `DROP`, no `DELETE`, no
  `TRUNCATE`, because they were never implemented. That is a stronger
  guarantee than a policy forbidding them, and it holds whichever provider is
  answering.

---

## Verifying a deployment

```bash
curl -s https://<host>/healthz
curl -s https://<host>/readyz                      # seeded: true, after you seed
curl -s https://<host>/api/v1/health/reasoning     # provider, models, rates
curl -s https://<host>/api/v1/health/detectors     # observed FPR against target
```

Then open `/console`. If it shows the first-run panel, the deployment is
working and the database is empty — seed it.
