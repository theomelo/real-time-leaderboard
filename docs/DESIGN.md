# Real-Time Leaderboard — System Design

A fullstack app that ranks users by cumulative points across games, serves rank and top-N queries in **O(log N)**, and pushes standings to a React client live.

> **Governing principle — one durable source of truth, two disposable derived layers.**
> Postgres is the source of truth. The Redis ZSETs and the Redpanda topic are both *derived and disposable* — either can be lost and rebuilt by replaying `ScoreEntry` from Postgres.

## Decisions

Each decision states the problem, the options considered and rejected, and the choice made with its reasoning.

### 1. Where the ranking data lives

**Problem.** A leaderboard must answer "who's on top?" and "what rank am I?" *constantly* and fast, while also durably storing accounts and the full history of every score.

**Solution:** Hybrid of **Postgres and Redis**.

**Reasoning**: Postgres is the durable source of truth (accounts, games, append-only score history); Redis sorted sets are a derived O(log N)ranking index. Each tool does what it is best at, and Redis being rebuildable makes it disposable.

**Other considered solutions**

- **Relational DB only** — a rank lookup becomes `COUNT(*) WHERE score > x`, an O(N) scan that degrades under write load. SQL's weakest operation. <!-- SAY WHY NOT --->
- **Redis only** — in-memory first, and awkward for accounts, login, and time-window reports (a sorted set has no time dimension). <!-- SAY WHY NOT --->

### 2. Event ingestion

**Problem.** Doing all write work — persist, update index, fan out — synchronously inside the HTTP request couples the user's latency to downstream work and offers no retry or spike absorption.

**Solution:** **Redpanda** (Kafka-compatible, single binary) as the event pipeline. A producer emits a score event on submit; a consumer updates Redis and rings the fan-out bell.

**Reasoning:** It gives the durable event-log / streaming model with the transferable Kafka API, without Kafka's operational weight. (Learning goal: dip into Kafka.)

**Other considered solutions**

- **Fully synchronous** — simplest and adequate for realistic load, but rejected because it offers no decoupling, retry, or event-driven experience.
- **Celery task queue** — fine and well-trodden, but rejected because it doesn't give the event-log / streaming model we wanted.
- **Kafka proper** — the right concepts, but rejected for its heavy operational weight (brokers, ZooKeeper/JVM).
- **Redis Streams** — lean and reuses Redis with the same concepts, but rejected because its API is more niche and transfers less directly.

### 3. Where the durable log lives

**Problem.** Since Redis is a derived, rebuildable index, we need a durable log to replay from. Which store is the authority?

**Solution:** **Postgres `ScoreEntry` is the durable log.** Redpanda is transport and Redis is an index — both disposable, both rebuilt by replaying `ScoreEntry`.

**Reasoning:** It keeps a single source of truth, so every failure reduces to "rebuild from Postgres."

**Other considered solutions**

- **The Redpanda topic as the durable log** — "purer" event-sourcing, but rejected because it stakes durability on broker retention config and creates a *second* source of truth, contradicting Decision 1.

### 4. What a "score" means

**Problem.** A submission `{game, points}` isn't automatically a leaderboard value. Rank by cumulative total, best single score, or latest submission?

**Solution:** **Cumulative points (`SUM`) drive rank** and live in Redis ZSETs. "Best" (`MAX` per user-per-game) is a *displayed stat only*, computed on read from Postgres — no ZSET.

**Reasoning:** Cumulative keeps the global board coherent, and "best" is never a ranking query, so it doesn't earn a sorted set — the data structure matches the query, not the data.

**Other considered solutions**

- **Best single score as the ranking basis** — authentic to arcade tables, but rejected because a "global across all games" board becomes incoherent (best-pacman vs best-chess aren't comparable).
- **Latest submission** — only right for rating systems, and rejected because a normal leaderboard shouldn't drop when you play one bad round.

### 5. Board topology

**Problem.** The product needs a global leaderboard across all games *and* per-game leaderboards.

**Solution:** **Maintain a global ZSET and one ZSET per game**, both updated on each write (2× `ZINCRBY`).

**Reasoning:** A tiny redundant write keeps every read O(log N), including the most-viewed global board.

**Other considered solutions**

- **Per-game only, compute global on demand** (`ZUNIONSTORE`) — rejected because it makes the most-viewed board the most expensive (O(total members), growing with game count).
- **Global only** — rejected because it fails the per-game requirement.

### 6. Real-time delivery

**Problem.** Rankings must update live in the browser as scores arrive.

**Solution:** **Server-Sent Events** (one-way server→browser) + **Redis Pub/Sub** as the fan-out backplane, so separate web-server processes can push to their own open connections.

**Reasoning:** It matches the one-directional shape of the data, is lighter than Channels, and the session cookie rides the SSE connection with no special handling.

**Other considered solutions**

- **Polling** — zero new infra and ~80% of the feel, but rejected because it isn't truly live and wastes requests.
- **WebSockets / Django Channels** — full-duplex, but rejected because the data flows one way only, so it's overkill and heavier (ASGI + Channels + channel layer).

### 7. Authentication

**Problem.** Register + login, working cleanly with the SSE connection and browser clients.

**Solution:** **Django session auth** (`contrib.auth`).

**Reasoning:** The session cookie is auto-sent on the SSE connection with zero special handling; it's the least code; and it fits the browser-only scope for now (a future mobile client would add tokens *alongside*, not replace).

**Other considered solutions**

- **JWT / token auth** — stateless and API-friendly, but rejected because native `EventSource` can't set an `Authorization` header (forcing a token-in-query-param workaround on the headline feature) and revocation is harder.

### 8. Backend framework & API style

**Problem.** Which Python framework serves the API, and REST or GraphQL? The app is async, real-time, and API-first — but also leans on an ORM, admin, and built-in auth.

**Solution:** **Django + Django Ninja, REST.**

**Reasoning:** It keeps Django's batteries (ORM, migrations, admin, session auth) while giving FastAPI-style ergonomics (Pydantic schemas, type hints) and auto-generated OpenAPI/Swagger docs.

**Other considered solutions**

- **Flask** — rejected as sync-first (the worst fit for the SSE/async core) with fewer batteries than Django.
- **FastAPI** — excellent async/SSE and native Swagger, but rejected because it gives back the ORM, admin, and auth we rely on (reopening Decisions 6–7); only worth it as a FastAPI learning goal, which this isn't.
- **Django REST Framework** — mature, but rejected for heavier ergonomics (serializers/viewsets) than needed.
- **GraphQL** — rejected because the API is mostly simple reads plus a few writes, so GraphQL's flexibility adds tooling without solving a real problem here.

### 9. Front-end architecture

**Problem.** A *distinct* front-end application must consume the API and contain its own data-transformation logic, so a server-rendered approach is a poor structural fit.

**Solution:** **A React SPA served same-origin** (Vite proxy in dev; Django/nginx serves the build in prod). **TanStack Query** for REST reads, native **EventSource** for the SSE stream, **shadcn/ui** for accessible components.

**Reasoning:** Same-origin serving means the session cookie just works (no CORS/CSRF friction). The client-side logic — merging SSE deltas into local state, computing the "around me" slice, highlighting the current user, and formatting/animating rank changes — is the app's own data transformation, keeping FE and BE cleanly separated.

**Other considered solutions**

- **Django SSR + htmx** — a fine architecture generally, but rejected because it blurs FE/BE separation and minimizes client-side logic.
- **Separate-origin SPA** — rejected because it needs CORS-with-credentials, `SameSite=None` cookies, and cross-origin CSRF, undercutting the session-cookie simplicity of Decision 7.
- **Vue / Svelte** — both fine, but rejected because React is the strongest fit here and matches the developer's existing strength.

---

## Architecture

```
   React SPA  ──REST /api──▶  ┌──────────────────────────────────┐
  (TanStack,   ◀─EventSource─▶│      Django (ASGI) + Ninja       │
   shadcn/ui)                 │  Ninja API · SSE · consumer      │
                              └───┬────────────┬───────────┬─────┘
                    1·INSERT(sync)│  2·produce │  ZINCRBY  │ PUBLISH
                                  ▼    (sync)  ▼           ▼
                            ┌──────────┐  ┌──────────┐  ┌──────────┐
                            │ Postgres │  │ Redpanda │  │  Redis   │
                            │  TRUTH   │  │ transport│  │ ZSETs +  │
                            │          │  │          │  │ Pub/Sub  │
                            └────┬─────┘  └──────────┘  └──────────┘
                                 └── replay → rebuild ──▶ Redis
```

Postgres is the one durable store; Redpanda and Redis are derived/disposable, rebuilt
from `ScoreEntry`.

## Write path

```
POST /api/scores {game_slug, points}   [session auth]
  1. validate + resolve game
  2. Postgres: INSERT ScoreEntry(...)          ← synchronous, durable truth
  3. produce event → Redpanda topic "scores"   ← synchronous, append
  4. return 202 Accepted
     ─────────── async boundary ───────────
  5. consumer reads "scores":
       ZINCRBY lb:total:global   points user
       ZINCRBY lb:total:game:{id} points user
       PUBLISH updates:global / updates:game:{id}
  6. web servers (subscribed to Redis Pub/Sub) push fresh top-N
     down open SSE connections → React client updates live
```

**Eventual-consistency window.** Between the `202` and the consumer finishing, the score
is durably saved but the rank index hasn't updated — an immediate rank check may be stale
for a sub-second beat. The Postgres write is kept synchronous so the thing we can't lose
is never deferred; only the rebuildable work goes async.

## Data model (Postgres — source of truth)

```python
# Users: Django's built-in User (session auth) + optional Profile for editable fields.

class Profile(models.Model):                      # 1:1 extension → clean UPDATE target
    user         = models.OneToOneField(User, on_delete=CASCADE, related_name="profile")
    display_name = models.CharField(max_length=50, blank=True)
    avatar_url   = models.URLField(blank=True)

class Game(models.Model):                          # admin- or owner-managed → full CRUD
    slug  = models.SlugField(unique=True)          # "pacman" — used in URLs
    name  = models.CharField(max_length=100)
    owner = models.ForeignKey(User, null=True, on_delete=SET_NULL, related_name="games")

class ScoreEntry(models.Model):                    # append-only history = source of truth
    user       = models.ForeignKey(User, on_delete=CASCADE, related_name="scores")
    game       = models.ForeignKey(Game, on_delete=CASCADE, related_name="scores")
    points     = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["game", "created_at"])]  # time-window reports
```

`ScoreEntry` is itself an append-only event log — "rebuild Redis" means "replay every row
in order and re-aggregate." `Game` and `Profile` carry the Update/Delete half of CRUD;
`ScoreEntry` stays append-only by design.

## Redis key design (derived)

| Key                     | Type     | Holds                                       |
| ----------------------- | -------- | ------------------------------------------- |
| `lb:total:global`       | ZSET     | cumulative points across all games          |
| `lb:total:game:{id}`    | ZSET     | cumulative points in that game              |
| `updates:global`        | Pub/Sub  | global fan-out channel                      |
| `updates:game:{id}`     | Pub/Sub  | per-game fan-out channel                    |

No key for "best" — that is a `MAX` off Postgres on read. ZSET members are `user_id`
(stable); display names live in `Profile` and are joined on read.

Read queries (one call each, O(log N)):
- Top-N — `ZREVRANGE lb:total:global 0 9 WITHSCORES`
- My rank — `ZREVRANK lb:total:global <uid>` (+ `ZSCORE` for points)
- Around me — `ZREVRANGE lb:total:global <rank-5> <rank+5> WITHSCORES`
- Best (stat) — `SELECT MAX(points) FROM scoreentry WHERE user_id=? AND game_id=?`

## API surface (Django Ninja, REST)

| Method            | Path                                     | Purpose                                  | Backed by            |
| ----------------- | ---------------------------------------- | ---------------------------------------- | -------------------- |
| POST              | `/api/auth/register`                     | create account                           | Postgres             |
| POST              | `/api/auth/login`                        | start session (sets cookie)              | Django auth          |
| POST              | `/api/auth/logout`                       | end session                              | Django auth          |
| GET/PATCH/DELETE  | `/api/me`                                | read / update / delete own profile       | Postgres (Profile)   |
| GET/POST          | `/api/games`                             | list / create game                       | Postgres (Game)      |
| PATCH/DELETE      | `/api/games/{slug}`                      | update / delete game (owner/admin)        | Postgres (Game)      |
| POST              | `/api/scores`                            | submit score → INSERT + produce → `202`   | Postgres + Redpanda  |
| GET               | `/api/leaderboards/global`               | global top-N (paginated)                  | Redis ZSET           |
| GET               | `/api/leaderboards/games/{slug}`         | per-game top-N                            | Redis ZSET           |
| GET               | `/api/leaderboards/global/me`            | my rank + score + around-me window        | Redis ZSET           |
| GET               | `/api/leaderboards/games/{slug}/me`      | my per-game rank + best stat              | Redis + Postgres MAX |
| GET               | `/api/reports/top`                       | time-window top players (arbitrary/preset)| Postgres aggregate   |
| GET (SSE)         | `/api/leaderboards/global/stream`        | live global updates                       | SSE + Redis sub      |
| GET (SSE)         | `/api/leaderboards/games/{slug}/stream`  | live per-game updates                     | SSE + Redis sub      |

Time-window report (the one query that ignores Redis; presets resolve server-side to
`from`/`to` against a configured timezone, then hit the same query):

```sql
SELECT user_id, SUM(points) AS total
FROM   scoreentry
WHERE  created_at BETWEEN :from AND :to        -- [AND game_id = :g]
GROUP BY user_id
ORDER BY total DESC
LIMIT :n;
```

## Front-end architecture

React SPA served same-origin (Vite proxy in dev; Django/nginx serves the build in prod),
so the session cookie and SSE connection work with no CORS/CSRF gymnastics.

- **React + Vite** — SPA & build
- **TanStack Query** — REST reads, caching, loading/error state
- **native EventSource** — SSE live stream, pushing updates into the query cache
- **shadcn/ui** — Radix + Tailwind components

Client-side logic (the Chingu-authored transformation Tier 3 requires): merge SSE deltas
into local board state, compute the "around me" slice from rank + window, highlight the
current user's row, format ranks/points and animate position changes.

## Leaderboard screen (UI)

The one structurally novel screen. **Job:** "see where I stand right now, and watch it
move as scores come in." **Primary action:** submit a score. **Entry:** post-login or nav
click → opens on the Global board, live-connected.

**Pattern.** Follows the live ranked-table convention (chess.com, Kaggle, arcade
high-score tables): a ranked list + a pinned "your standing" block + a board picker.
*Breaks* one sub-convention — score submission is an inline action on this screen, not a
separate page, because the submit → watch-your-rank-move loop is the entire point and a
page hop would sever it.

```
┌────────────────────────────────────────────────────────────────┐
│  🏆 Leaderboard          Reports              [avatar ▾]       │  ← top nav
├────────────────────────────────────────────────────────────────┤
│  Board: [ Global            ▾ ]  🔍             ● live ⟳ 2s   │  ← board filter · status
│                                                                │
│  #   Player           Points              Best     [+ Score]   │  ← submit
│  ─────────────────────────────────────────────────────────     │
│   1  alice            12,400  ▲+120        900                 │
│   2  bob              11,980  ▬             740                │
│   3  carol            11,200  ▼ (−2 ranks) 680                 │
│   4  dave              9,850  ▲+300        610                 │
│   …                                                            │
│  ── your standing ──────────────────────────────────────────   │
│  17  you (theo)        4,320  ▲+50         520   ← highlighted │  ← pinned row
│  18  erin              4,180  ▬             470                │
│                                                                │
│                        [ ⌄ around me ]   [ top of board ⌃ ]    │
├────────────────────────────────────────────────────────────────┤
│  Top players this week →                                       │  ← time-window report
└────────────────────────────────────────────────────────────────┘
```

Key elements & behavior:

- **Board filter** — a searchable combobox, *not* tabs. Default item "Global" (the
  cross-game aggregate); below it a searchable list of games. Chosen over tabs because
  games are user-creatable (Decision 9), so the count is unbounded and tabs don't scale.
  Selecting an item swaps the ZSET read and resubscribes SSE to
  `updates:global | updates:game:{id}`; updates `?board=<slug>`; no reload.
- **Points delta** — on each SSE update the row shows `▲+N` beside **Points** (green for a
  gain), then **fades after ~3s** so the board reads calmly at rest. Rank movement is
  shown by the row re-animating (FLIP) to its new position, not a second arrow. This is
  transient *UI* state: the stream carries the new absolute score (truth); the client
  diffs it against its cached value to derive the delta and animation. The server never
  sends "deltas" — keeping the fan-out payload simple and consistent with "Redis carries
  truth, client derives presentation."
- **Your standing** — always rendered (top-N slice + your rank ± window from
  `/leaderboards/global/me`); your row is highlighted and sticks while scrolling.
- **Submit** — inline popover (game select + positive-integer points) → `POST /api/scores`
  → `202` → optimistic "submitting…"; the real position lands when the SSE delta arrives
  (the sub-second eventual-consistency beat). Invalid points → inline `:user-invalid`
  message, submit disabled until valid; network failure reverts the optimistic row.

**Empty state.** Never fires for Global or seeded games (seed data guarantees content). It
*does* fire for a freshly user-created game with no scores, and drives the "your standing"
block for a new user with no rank yet:

```
┌────────────────────────────────────────────────────────────────┐
│  Board: [ Tetris (new)      ▾ ]                 ● live         │
│                                                                │
│                     [ trophy outline ]                         │
│         No scores in Tetris yet — be the first to rank.        │
│              [ Submit a score → ]                              │
└────────────────────────────────────────────────────────────────┘
```

**Responsive.** The board filter is already a dropdown (nothing to collapse). The table
drops the **Best** column on mobile (secondary stat). Submit becomes a sticky bottom-right
FAB so it stays thumb-reachable while scrolling.

## Failure & consistency

Every failure mode reduces to "rebuild the derived layer from the truth."

- **Redis lost / cold / drifted** — run `rebuild_leaderboards`: recompute every ZSET via
  `SUM(points) GROUP BY user (, game)` then `ZADD`. On a missing key at read time, lazily
  rebuild that one board and serve.
- **Redpanda message lost** — recoverable; the truth is in Postgres. Replay the topic (or
  `ScoreEntry` history) to re-drive the consumer.
- **Crash between INSERT & produce** — row exists but no event emitted → Redis never hears
  it. Known gap; fix later with the transactional **outbox** pattern. Acceptable for v1.
- **Scaling** — ZSET ops are O(log N); real load is SSE fan-out, handled by adding web
  servers behind the shared Pub/Sub bus. Postgres takes writes + the cold report query.

**Known follow-ups.** (1) Transactional outbox to close the INSERT→produce gap.
(2) Preset timezone anchoring — "this week" resolves against a configured TZ, not UTC.
(3) Bucketed ZSETs remain a *later* optimization if reports ever move onto a hot path.

## Chingu Tier 3 compliance

Tier 3 is fullstack. All seven criteria are satisfied:

1. **Distinct FE/BE files (SRP)** — separate React SPA vs Django app; within the BE, views
   / consumer / models / Redis layer are separate concerns.
2. **DB accessed only from BE** — Postgres touched only by Django + the consumer.
3. **App-specific API only in BE** — full Ninja REST API.
4. **Auth coupled with custom API** — Django session auth wired into our own endpoints.
5. **FE logic transforms/presents data** — SSE-delta merge, "around me" computation,
   current-user highlight, rank formatting/animation.
6. **FE app accesses BE API of own design** — React SPA consumes the Ninja API + SSE.
7. **CRUD** — Create/Read across scores & leaderboards; Update/Delete on `Game` and
   `Profile`.

Beyond the minimum, the design adds Redis sorted-set ranking, an event-driven ingestion
pipeline, and real-time SSE.
