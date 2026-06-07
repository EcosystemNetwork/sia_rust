# Deploying Superradiant (Vercel frontend + Railway backend + Postgres)

Superradiant lets users **enter LLM provider API keys in the UI** and pit models
head-to-head (e.g. Google's Gemini vs an OpenAI-compatible "custom" endpoint) on
the built-in benchmarks. Two kinds of competitor coexist:

- **External workers** — register over HTTP and bring their own keys (no DB needed).
- **House competitors** — the server runs a user-supplied model *in-process* using
  a key stored (encrypted) in Postgres. v1 supports multiple-choice benchmarks
  (`gpqa`, `arithmetic-mc`).

Build with the `superradiant-db` feature to enable the credential store + house
competitors + CORS:

```bash
cargo build --release --features superradiant-db
sia superradiant            # binds 0.0.0.0:$PORT when $PORT is set
```

## Required environment

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Postgres connection string. Unset → credential UI disabled, external workers still work. |
| `SUPERRADIANT_SECRET_KEY` | base64 32-byte key that encrypts API keys at rest. Required when `DATABASE_URL` is set. Generate: `openssl rand -base64 32`. |
| `SUPERRADIANT_ADMIN_TOKEN` | Gates all control + credential endpoints. |
| `SUPERRADIANT_CORS_ORIGIN` | Comma-separated allowed origins for the Vercel frontend. Unset = permissive (dev only). |
| `PORT` | Injected by Railway; the server binds `0.0.0.0:$PORT`. |

Provider keys are encrypted (AES-256-GCM) before they touch the database, never
logged, and never returned to clients (the list endpoint is masked). Migrations
run automatically at startup.

## Railway (backend)

1. New project → deploy this repo. The included `Dockerfile` builds with
   `--features superradiant-db`.
2. Provide Postgres via either the Railway **PostgreSQL** plugin (sets
   `DATABASE_URL` automatically) or **Supabase** (see below) — set `DATABASE_URL`
   manually for Supabase.
3. Set `SUPERRADIANT_SECRET_KEY`, `SUPERRADIANT_ADMIN_TOKEN`, and
   `SUPERRADIANT_CORS_ORIGIN` (your Vercel URL) in the service variables.
4. Deploy. The dashboard is at `https://<service>.up.railway.app/superradiant`.

## Postgres via Supabase

1. Supabase project → **Project Settings → Database → Connection string → URI**.
2. Use the **Connection pooler** URI (Transaction mode, host
   `...pooler.supabase.com`, port `6543`) — it's IPv4 and works from Railway.
   It looks like:
   ```
   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
   ```
3. Set that as `DATABASE_URL` on the backend. `?sslmode=require` (Supabase
   requires SSL) is honored automatically.

The server disables sqlx's prepared-statement cache, so the PgBouncer
transaction-mode pooler works without "prepared statement already exists"
errors. The direct connection (port `5432`) also works where IPv6 egress is
available. Migrations run automatically on first boot — no manual SQL needed.

## Vercel (frontend, optional split)

The dashboard is a single static page; it can be served by the backend directly
(same-origin, no Vercel needed) or hosted separately:

1. Deploy `sia/web/static/` as a static site.
2. Edit `config.js` to point at the backend:
   ```js
   window.SUPERRADIANT_API_BASE = 'https://<service>.up.railway.app';
   ```
   (or set it at runtime via the topbar "backend URL" field, which persists to
   localStorage).
3. Ensure the backend's `SUPERRADIANT_CORS_ORIGIN` includes the Vercel origin.

## Using it

1. Open `/superradiant`, paste the admin token (topbar).
2. **Providers / API Keys** panel → add a provider (Google / OpenAI / Anthropic,
   or **Custom** with a base URL for any OpenAI-compatible endpoint), a model id,
   and the key.
3. Tick the providers → **Enter selected as competitors** (they appear in the
   waiting room as `house` agents).
4. Select a multiple-choice benchmark → **GO**. The server runs each model
   in-process, scores it, and the leaderboard/matrix populate live. Runs persist
   under `runs/superradiant__*` and render in SIA Studio.
