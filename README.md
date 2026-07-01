# Real-Time Leaderboard

A fullstack application (pre seeded with mock data) that ranks users by points across games and streams standings to
the browser **live** — submit a score and watch the board reorder in real time.

## Features

- **Live rankings** — standings update in the browser the instant a score is submitted, no
  refresh or polling.
- **Global and per-game boards** — one leaderboard across all games plus a board per game.
- **Fast rank queries** — top-N, your rank, and an "around me" window are all `O(log N)`,
  backed by Redis sorted sets.
- **Cumulative scoring** — points accumulate across submissions; your personal best per
  game is shown alongside your total.
- **Time-window reports** — "top players" for any date range or preset (this week, this
  month, …).
- **Accounts** — registration and login with session-based authentication.

## Architecture

```
   React SPA  ──REST /api──▶  ┌──────────────────────────────────┐
  (TanStack,   ◀─EventSource─▶│      Django (ASGI) + Ninja       │
   shadcn/ui)                 │  API · SSE · event consumer      │
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

- **Postgres** is the source of truth — accounts, games, and the append-only history of
  every score.
- **Redis sorted sets** are a derived ranking index (`O(log N)` reads), and **Redis
  Pub/Sub** is the fan-out backplane for live updates.
- **Redpanda** (Kafka-compatible) carries score events: an API request persists the score
  and produces an event; a consumer updates the index and broadcasts the change.
- Redis and Redpanda are disposable — both can be rebuilt by replaying the score history
  from Postgres.

For the full design — data model, key layout, API surface, and the reasoning behind each
decision — see [`docs/DESIGN.md`](docs/DESIGN.md).

## Tech stack

| Layer | Choice |
| --- | --- |
| Frontend | React (Vite), TanStack Query, native `EventSource`, shadcn/ui |
| API | Django + [Django Ninja](https://django-ninja.dev/) (REST, ASGI) |
| Source of truth | PostgreSQL |
| Ranking index & fan-out | Redis (sorted sets + Pub/Sub) |
| Event transport | Redpanda (Kafka API) |
| Real-time | Server-Sent Events |
| Auth | Django session authentication |
| Tooling | [`uv`](https://docs.astral.sh/uv/), Python ≥ 3.14, Django ≥ 6.0 |

## Getting started

### Prerequisites

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- Python ≥ 3.14 (uv can install it for you)
- PostgreSQL, Redis, and Redpanda for the full stack

### Install

```bash
uv sync
```

This creates a local `.venv/` and installs dependencies from `uv.lock`.

> [!IMPORTANT]
> The Django project has not been scaffolded yet, so the `manage.py` commands below will
> only work once it exists. Bootstrap it first:
>
> ```bash
> uv run django-admin startproject config .
> ```

### Run

```bash
uv run python manage.py migrate      # apply migrations
uv run python manage.py runserver    # start the dev server
```

Common tasks:

```bash
uv add <package>                     # add a dependency
uv run python manage.py makemigrations
uv run python manage.py test         # run the test suite
```

> [!TIP]
> Prefer `uv run …` over activating the virtualenv manually so the correct interpreter and
> dependencies are always used.

## Documentation

- [`docs/DESIGN.md`](docs/DESIGN.md) — full system design: decisions, data model, Redis key
  layout, API surface, and failure/consistency model.　
