# Development Environment

This project ships with a VSCode workspace configuration in `.vscode/` that provides:

- Python interpreter auto-detected from `.venv`
- `ruff` for formatting + lint, `mypy` for type checking — both run on save
- Integrated test runner (pytest)
- Debug configurations for the API and every worker
- Task palette entries for every `make` target
- Recommended extensions

After cloning, open the project in VSCode — it will prompt to install recommended extensions. Accept.

## Setup

```bash
uv sync --all-groups          # installs everything including dev deps
cp .env.example .env.local    # copy and fill in your API keys
```

VSCode picks up `.venv/bin/python` automatically via `settings.json`.

## Running tasks (Cmd+Shift+P → "Run Task")

| Task | Equivalent |
|---|---|
| Up: Infrastructure | `make up` |
| Up: Observability | `make up-obs` |
| Up: Application (api + workers) | `make up-app` |
| Down: Everything | `make down` |
| Seed Mongo Indexes | `make seed-indexes` |
| Health Check | `make check` |
| Test: Unit | `make test` |
| Lint | `make lint` |
| Typecheck | `make typecheck` |
| Eval: Fast | `make eval-fast` |

## Debugging a service

Each service has a launch configuration in `.vscode/launch.json`. Press **F5** and pick the service.

**Important**: before debugging a worker locally, stop its Docker counterpart first. Otherwise two instances consume from the same Kafka consumer group, splitting messages between them:

```bash
# Example: debug scout-worker locally
docker compose -f docker/docker-compose.app.yml stop scout-worker

# Then press F5 → "Worker: Scout" in VSCode
```

The API launch config runs on port 8080 with `--reload`, same as `make run-dev`.

The compound launch **"All Workers (debug)"** starts orchestrator + scout + knowledge + analyst + publisher + sse-coordinator simultaneously. Stop the corresponding Docker containers first.

## REST Client (humao.rest-client)

Sample `.http` files live in [`docs/api-examples/`](api-examples/). Open any file and click **Send Request** above each block.

Variables at the top of each file (`@host`, `@tenant`) can be overridden in a `.env` file in the same directory, or just edited inline.

Common flow:
1. `01-create-category.http` — get a `category_id`
2. `02-add-sources.http` — paste the `category_id`, add sources
3. `03-create-job.http` — get a `job_id`
4. `04-stream-job.http` — paste the `job_id`, watch SSE events

## Personal overrides

Files matching `*.local.json` or `*.local` inside `.vscode/` are gitignored. Add a `settings.local.json` for personal preferences (font size, theme, etc.) without affecting team settings.
