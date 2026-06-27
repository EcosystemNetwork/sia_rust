# Aquarium tab (SIA Studio)

An agent-controlled 2D fish aquarium, surfaced under the **🐠 Aquarium** tab in
SIA Studio. Ported from [asaltveit/2d-fish-aquariums](https://github.com/asaltveit/2d-fish-aquariums).

Each tank is run by its own Claude agent: it sets a biome, researches 3–5
species, populates the tank with ecologically plausible zones, and must keep
every fish fed on its hunger timer (over-feed a full fish and it dies; let a
hungry fish starve and it dies). The browser renders the fish procedurally on a
`<canvas>` and receives live updates over a WebSocket.

## What changed vs. upstream

- The Apify **fish-researcher** actor + HTTP standby server are gone. Its
  scraping logic (`research.py`, pure `httpx` + `BeautifulSoup`) runs in-process
  via `researcher.Researcher`, which prefers the bundled snapshots in
  `species_data/` and only scrapes FishBase/Wikipedia live for un-bundled
  species. No Apify token required.
- The backend mounts onto SIA Studio's existing FastAPI app
  (`sia.web.server.create_app`) instead of standing up its own server.
- The WebSocket path is `/ws/aquarium/{id}` (was `/ws/{id}`); the frontend lives
  under `/aquarium/js` + `/aquarium/css`.

## Enabling

The agents call the Claude API, so they are **opt-in** — nothing starts unless
both are set:

```bash
SIA_AQUARIUM=1 ANTHROPIC_API_KEY=sk-ant-... sia web
# optional: AQUARIUM_COUNT=4 (number of tanks)
```

Install the extra deps with: `pip install 'sia-agent[aquarium]'`
(`anthropic`, `httpx`, `beautifulsoup4`).

When disabled (the default), the tab still loads and shows a short "backend is
off" message — `sia run`'s background dashboard never spawns billable agents.

## Deployment note

The aquarium needs a **persistent** process (long-lived WebSockets + background
agent loops). It works under `sia web` and any normal uvicorn/Railway-style
host. It will **not** work on serverless hosts (e.g. Vercel), where there is no
persistent process or WebSocket support — there the tab degrades to the disabled
empty state.
