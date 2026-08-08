# mec-cast-logging-service

A logging service for collecting, storing, and querying logs from mec-cast applications.

Logs arrive over HTTP as JSON, land in PostgreSQL, and come back out through a filtered
query API with full-text search over messages and containment search over structured context.

## Requirements

- Python 3.11+
- PostgreSQL 12+ (an empty database the service can create tables in)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Copy the sample configuration and point it at your database:

```bash
cp .env.example .env
```

Every setting is an environment variable prefixed with `MECLOG_` — see [.env.example](.env.example).
The only one you must set is `MECLOG_DATABASE_URL`.

Create the schema:

```bash
mec-cast-logs migrate
```

Migrations also run automatically at startup unless you set `MECLOG_AUTO_MIGRATE=false`.

## Running

```bash
mec-cast-logs serve --port 8000
```

Interactive API docs are at `http://localhost:8000/docs`.

For development with reload:

```bash
mec-cast-logs serve --reload
```

Or drive uvicorn directly (for example, behind a process manager):

```bash
uvicorn mec_cast_logging.app:app --host 0.0.0.0 --port 8000 --workers 4
```

## API

Base path is `/api/v1`. Health endpoints sit at the root.

| Method | Path                 | Purpose                                    |
| ------ | -------------------- | ------------------------------------------ |
| POST   | `/api/v1/logs`       | Ingest one entry or a batch                |
| GET    | `/api/v1/logs`       | Query entries, newest first                |
| GET    | `/api/v1/logs/{id}`  | Fetch a single entry                       |
| GET    | `/api/v1/stats`      | Counts by level and service over a window  |
| GET    | `/health`            | Liveness — does not touch the database     |
| GET    | `/health/ready`      | Readiness — 503 when the database is down  |

### Ingesting

A single entry:

```bash
curl -X POST http://localhost:8000/api/v1/logs \
  -H 'Content-Type: application/json' \
  -d '{
        "level": "ERROR",
        "service": "mec-cast-encoder",
        "message": "stream dropped mid-broadcast",
        "host": "encoder-03",
        "logger": "encoder.pipeline",
        "trace_id": "b7f1c2",
        "context": {"stream_id": 1204, "retries": 3}
      }'
```

Post a JSON array for a batch — it is written in one round trip. The response gives the
generated ids in submission order:

```json
{ "accepted": 2, "ids": [1041, 1042] }
```

Fields:

| Field       | Required | Notes                                                              |
| ----------- | -------- | ------------------------------------------------------------------ |
| `level`     | yes      | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`                 |
| `service`   | yes      | Which mec-cast app emitted it                                      |
| `message`   | yes      | Free text; indexed for full-text search                            |
| `timestamp` | no       | ISO 8601. Defaults to ingestion time; naive values are read as UTC |
| `host`      | no       | Emitting machine                                                   |
| `logger`    | no       | Logger name within the app                                         |
| `trace_id`  | no       | Correlates entries across services                                 |
| `context`   | no       | Arbitrary JSON object; stored as JSONB and queryable               |

Unknown top-level fields are rejected rather than silently dropped — put extras in `context`.

### Querying

```bash
# Errors and worse from the encoder in the last hour
curl -G http://localhost:8000/api/v1/logs \
  --data-urlencode 'service=mec-cast-encoder' \
  --data-urlencode 'min_level=ERROR' \
  --data-urlencode "start=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)"

# Full-text search over the message
curl -G http://localhost:8000/api/v1/logs --data-urlencode 'q=stream dropped'

# Entries whose context contains a given key/value pair
curl -G http://localhost:8000/api/v1/logs --data-urlencode 'contains={"stream_id": 1204}'

# Everything belonging to one trace
curl -G http://localhost:8000/api/v1/logs --data-urlencode 'trace_id=b7f1c2'
```

Parameters: `service` and `level` are repeatable and match any of the given values;
`min_level` filters by severity floor; `start` / `end` bound the timestamp; `q` runs a
websearch-style full-text query; `contains` takes a JSON object matched against `context`;
`host` and `trace_id` are exact matches; `limit` sets the page size.

Results are newest first. When more rows remain, the response carries a `next_cursor` —
pass it back as `cursor` for the following page:

```json
{ "items": [ ... ], "count": 100, "next_cursor": "MjAyNi0wMy0wMVQxMjowMDowMCswMDowMHwxMDQx" }
```

Paging is keyset-based on `(timestamp, id)`, so rows arriving mid-walk never cause skips or
duplicates the way an `OFFSET` would.

## Retention

Nothing is deleted automatically. Trim old entries with:

```bash
mec-cast-logs purge --days 30 --dry-run
```

Drop `--dry-run` to delete. Without `--days` it uses `MECLOG_RETENTION_DAYS`. Run it from
cron or a systemd timer at whatever cadence suits your volume.

## Data model

One table, `log_entries`, with indexes covering the query paths above:

- `(timestamp DESC, id DESC)` — the default newest-first scan and keyset paging
- `(service, timestamp DESC, id DESC)` and `(severity, timestamp DESC, id DESC)` — filtered scans
- partial index on `trace_id` — trace lookups
- GIN on `context` (`jsonb_path_ops`) — containment queries
- GIN on a generated `search_vector` — full-text search

`severity` is a numeric mirror of `level` (10–50, matching stdlib `logging`) so `min_level`
compares by rank rather than alphabetically.

## Testing

```bash
pytest tests/test_schemas.py
```

Those run anywhere — validation and cursor logic, no database.

The end-to-end tests in `tests/test_api.py` need a real PostgreSQL. Point them at a
throwaway database; each test truncates the table afterwards, so do not aim it at anything
you care about:

```bash
MECLOG_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/mec_cast_logs_test pytest
```

Without that variable the database-backed tests skip rather than fail.

Lint and format:

```bash
ruff check . && ruff format --check .
```

## Layout

```
src/mec_cast_logging/
  app.py           FastAPI app factory, lifespan, error handling
  api.py           HTTP routes
  schemas.py       Request/response models, cursor encoding
  repository.py    All SQL
  db.py            Connection pool and migration runner
  config.py        Settings
  cli.py           serve / migrate / purge
  migrations/      Ordered .sql files, applied once and recorded
tests/
```

## Not included yet

Deliberately out of scope for this first cut: authentication, rate limiting, a web UI, and
non-HTTP ingestion (syslog, message queues). Add them when a caller needs them.
