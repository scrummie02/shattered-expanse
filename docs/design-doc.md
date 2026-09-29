# The Shattered Expanse — Server Design Doc

**Date:** 2026-09-29 · **Status:** draft v0.1 · **Decision:** build custom (see §12)

> An agent-driven space MMORPG with an old-school MMO feel: a persistent
> universe with space pirates, hostile aliens, and AI agents that fly ships,
> mine ore, fight, trade, and build ships from modular parts.

## 1. Vision

Every character starts as an AI agent; human players can join later through the
same client protocol. The core pattern: the LLM issues high-level intent, a
deterministic engine executes it.

Design pillars:

- **Server-authoritative and deterministic.** The sim is the truth; nothing an
  agent says bends physics.
- **LLM out of the hot loop.** Brains decide; the engine moves.
- **Boring technology for the core.** SQLite, JSON, one process. Fancy later.
- **Playable early.** A ship flying in an empty system in week one, pirates by
  week three.

## 2. Architecture at a glance

Three layers, three kinds of process:

```
┌─────────────┐   intents (WS/JSON)   ┌──────────────────┐
│ Agent brain │ ────────────────────▶ │   Sim server     │
│ (per agent, │ ◀──────────────────── │  (authoritative, │
│  Python)    │   state deltas/events │   10 Hz tick)     │
└──────┬──────┘                       └────────┬─────────┘
       │ LLM calls (LiteLLM)                   │ SQL
       ▼                                       ▼
┌─────────────┐                       ┌──────────────────┐
│ 27B (igor)  │                       │ SQLite (WAL)     │
│ 8B (milo,   │                       │ entities, agents,│
│ mini PC)    │                       │ market, memory   │
└─────────────┘                       └──────────────────┘
```

- **Sim server:** one Python asyncio process. Owns the tick, physics, intent
  validation, and persistence.
- **Agent brain:** one lightweight process per agent (or several agents per
  process). Subscribes to state, calls the LLM on a throttle, emits intents.
- **LLM tier:** LiteLLM routes routine decisions to the 8B model and
  dialogue/complex planning to the 27B model.

Human clients later speak the same WebSocket protocol as a brain. No special
casing.

## 3. The simulation core

### 3.1 Tick loop

- Fixed timestep: **10 Hz (dt = 0.1 s)**, accumulator pattern.
- Deterministic order: iterate entities sorted by id; seeded RNG per star
  system for spawns and loot.
- Sense of scale: 500 entities of simple physics is a couple of milliseconds
  in Python. The tick is never the bottleneck — LLM calls are.

### 3.2 World model

- A **star system** is one continuous 3D volume (e.g. 200,000 km per side,
  float64 km coordinates, origin at the star).
- Systems are linked by **jump gates** (entities with a destination). Jumping
  transfers the entity between system entity-sets with a short cooldown.
  v1: 3 hand-authored systems; seeded procedural generation from v2.
- No terrain, no pathfinding. Obstacles are just spheres (stations, large
  asteroids).

### 3.3 Entities and components (v1 — keep it boring)

Plain dataclasses; components as dicts keyed by entity id. A real ECS only if
profiling ever says so.

- `Transform`: position (x, y, z in km), velocity, heading, collision radius
- `Ship`: hull / max_hull, shield / max_shield, energy, ship class
- `Drive`: max speed, acceleration, turn rate
- `Weapon`: damage, range, cooldown, projectile speed
- `Cargo`: capacity, contents (e.g. ore units)
- `Mineable`: resource type, amount remaining (asteroids)
- `Station`: services (refuel / repair / market / shipyard), inventory
- `AgentRef`: account id of the controlling brain (null = derelict or scripted)
- `Gate`: destination system

### 3.4 Physics (arcade, EVE-flavored — not realistic)

- Newtonian drift: `pos += vel * dt`.
- Throttle sets a target velocity along the heading; acceleration-limited
  approach. Turn rate limited by ship class.
- Artificial per-class max speed (game feel, and it keeps the tick cheap).
- Collisions: sphere-sphere with a uniform spatial grid broadphase; resolve by
  push-out plus damage scaled by closing speed.
- Shields absorb damage first and regenerate slowly out of combat; hull damage
  is permanent until repaired at a station.

### 3.5 What the sim does NOT do

No LLM in the tick. No agent-supplied code execution. No per-frame database
writes (in-memory state, periodic snapshots).

## 4. Client protocol v0 (WebSocket + JSON)

One protocol for brains and future human clients.

Client to server:

- `{"t":"auth","token":"..."}`
- `{"t":"intent","id":"<uuid>","action":"set_throttle","value":0.8}`

Intent actions: `set_throttle`, `set_heading` (yaw/pitch), `fire` (target),
`start_mining` (target), `stop`, `dock` (station), `undock`, `jump` (gate),
`say` (text), `trade`, `jettison`. The server validates every intent (range,
cooldown, energy, docked state) and ACKs or rejects with a reason — rejection
is data the brain learns from.

Server to client:

- `snapshot` on join: full entity list for the agent's system.
- `delta` at 10 Hz: changed entities only, plus `events` (hits, kills, chat in
  range, mining yields, market fills).
- Relevance: v1 sends the whole system (small); later, an interest bubble
  (e.g. 5,000 km).

Auth: per-account token issued out-of-band, stored hashed in the DB. Bind to
the tailnet only; rate-limit intents per connection (e.g. 20/s).

## 5. The agent bridge

### 5.1 The loop (per agent)

observe → think (LLM, throttled) → act (intent queue) → repeat

- **Observe:** maintain a local world-view from deltas; build a compact textual
  summary for the prompt (nearby entities, own status, recent events, current
  goal).
- **Think:** LLM call every 2–5 s or on important events (under attack,
  reached waypoint). Routine ticks need no LLM — the last intent persists (a
  ship told to fly straight keeps flying straight).
- **Act:** emit 1–3 intents.

### 5.2 AI LOD (how hundreds of agents stay affordable)

Not every agent thinks every tick:

- **Active** (in combat, in dialogue, near action): LLM every 2–5 s.
- **Ambient** (flying a route, working a rock): LLM every 30–60 s, or pure
  scripted waypoint-following with zero LLM.
- **Dormant** (docked, parked): no LLM at all; the sim just keeps them parked.

Agents promote/demote themselves on events. This is the difference between
needing 3 GPUs and needing 30.

### 5.3 Two-tier LLM (mapped to the lab)

- **Routine tier — 8B model (Granite 8B candidate) on milo and the mini PC:**
  navigation, mining cycles, simple reactions. Short prompts, JSON-ish intents.
- **Cognition tier — 27B Qwen on igor:** dialogue, negotiation, faction
  planning, novel situations.
- LiteLLM in front with routing rules; brains call `agent-quick` / `agent-deep`.
- Rough capacity (estimates, measure on day one): an 8B Q8 on 16 GB VRAM does
  ~40–60 tok/s; a decision call is ~300–600 tokens. With concurrent streams,
  one 8B instance realistically serves **20–40 active agents** at a 5 s think
  cadence — more with ambient LOD doing the heavy lifting. The 27B on igor's
  64 GB VRAM has headroom for long-context dialogue across dozens of
  concurrent conversations.

### 5.4 Persona and memory

Per agent, in SQLite: persona prompt, long-term memory (summarized, not raw
logs), grudges and faction standing, current goal. Memory writes are cheap LLM
summarizations on a slow cadence (every few minutes). This is what makes
pirates hold grudges and traders remember good customers.

## 6. Persistence

- **SQLite in WAL mode**, one file on the sim host. Tables: `entities` (id,
  system, type, components JSON), `accounts`, `agent_memory`,
  `market_orders`, `blueprints`, `ships_owned`.
- Full snapshot every 60 s and on clean shutdown; important events (kills,
  trades) written immediately.
- One server is the v1 scale. Postgres only if systems ever shard across
  hosts — not for a long time.

## 7. Gameplay systems by phase

### Phase 0 — "it flies" (week 1)

Repo skeleton, protocol v0, sim tick, one ship entity, a headless test client.
**Done when:** a script flies a waypoint loop in an empty system.

### Phase 1 — rocks (week 2)

Mineable asteroids, mining laser yielding ore to cargo, persistence
snapshot/restore, one brain agent mining on a loop. **Done when:** the agent
mines 100 units, docks, sells at an NPC price, and a server restart loses
nothing.

### Phase 2 — pirates (weeks 3–4)

Weapons, shields/hull, projectiles, scripted pirate AI plus the first
LLM-driven pirates, loot drops, death and respawn (clone at a station for a
small fee — old-school). **Done when:** a pirate interdicts a miner, the fight
resolves, and the loot gets scooped.

### Phase 3 — economy (weeks 5–6)

Stations with a real market (player buy/sell orders, not just NPC prices), a
mission board (courier, bounty), cargo hauling. **Done when:** two agents
complete a player-priced trade and a bounty mission pays out.

### Phase 4 — ship building (week 7+)

Starfield-style modules: hull classes plus swappable engine, reactor, shield,
and weapon modules whose stats feed the sim components. Blueprints discovered
via loot or research. **Done when:** an agent assembles a custom frigate from
modules and flies it. Deliberately late — the biggest scope risk; everything
before it must work first.

### Phase 5 — humans and factions (later)

A web-canvas 2D observer client (system map + ship status) on the same
WebSocket protocol; NPC alien factions with territory; more systems through
gates. A 3D client only if the game earns it.

## 8. Repo layout (proposed)

```
shattered-expanse/
  sim/      # authoritative server: tick, physics, protocol, persistence
  brain/    # agent runtime: observe/think/act loop, prompts, memory
  proto/    # protocol spec + JSON schemas (single source of truth)
  tools/    # headless client, scenario runner, load tester
  web/      # (phase 5) minimal observer client
  docs/     # this doc, protocol changelog
```

## 9. Hardware mapping (the lab as it stands)

- **Sim server:** negligible CPU — roughly one core at 10 Hz for hundreds of
  entities. Run on milo or igor, whichever is most stable.
- **8B tier:** milo and the mini PC (16 GB VRAM each), one instance each,
  LiteLLM load-balancing between them.
- **27B tier:** igor (64 GB unified VRAM, SYCL llama.cpp) for dialogue and
  planning.
- **Brains:** cheap; co-locate with the sim or spread across nodes. Everything
  over the tailnet.
- The sim never blocks on the LLM: if a brain is slow, its ship just holds its
  last intent. Laggy brain does not mean laggy universe.

## 10. Risks and open questions

- **LLM latency vs. game feel:** handled by intent persistence plus AI LOD, but
  the first load test (50 agents, all active) will tell the truth. Measure
  early.
- **Determinism:** fixed dt, sorted iteration, seeded RNG. LLM output never
  feeds the RNG. Intent logging gives replays for free.
- **Scope:** ship building is the classic trap — it is phase 4 for a reason.
  Cut it without guilt if phases 1–3 aren't fun yet.
- **Content:** pirates and aliens need behavior design, not just models. Start
  scripted; promote the interesting ones to LLM control.
- **Cheating:** brains are trusted clients on a private tailnet for v1, but
  validate intents anyway — free now, essential later.
- **Open:** exact system size and flight times (tune in phase 0); market
  design (order book vs. NPC floor price); jump gates vs. free warp between
  systems.

## 11. What "good" looks like

Open an observer client and watch: a miner working a belt, a pirate lining up
an intercept, station chatter in local, a hauler jumping out. Nobody scripted
this session — the brains, memories, and economy produced it. That is the bar.

## 12. Decision log

- 2026-09-29: **Build custom** over OGameX (no real-time flight),
  EmptyEpsilon (no persistence/economy; GPL-2.0), Evennia (MUD-shaped). Space
  sims skip the hard parts — no terrain, no pathfinding — so ~2–5 k LOC covers
  the core.
- 2026-09-29: Python for the sim v1 (iteration speed); Go/Rust only if
  profiling demands it.
- 2026-09-29: SQLite v1; Postgres deferred.
- 2026-09-29: Two-tier LLM via LiteLLM (8B routine / 27B cognition) on existing
  lab hardware.
