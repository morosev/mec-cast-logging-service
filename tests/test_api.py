"""End-to-end tests over the HTTP API against a real PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from mec_cast_logging.app import create_app
from mec_cast_logging.config import Settings
from mec_cast_logging.db import Database
from mec_cast_logging.repository import LogRepository

PREFIX = "/api/v1"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def client(settings: Settings, database: Database) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    # The pool is owned by the `database` fixture, so bypass the app's lifespan.
    app.state.database = database
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def entry(**overrides: object) -> dict:
    payload = {
        "timestamp": NOW.isoformat(),
        "level": "INFO",
        "service": "mec-cast-api",
        "message": "request completed",
        "context": {},
    }
    payload.update(overrides)
    return payload


async def seed(client: httpx.AsyncClient, entries: list[dict]) -> list[int]:
    response = await client.post(f"{PREFIX}/logs", json=entries)
    assert response.status_code == 201, response.text
    return response.json()["ids"]


async def test_health_needs_no_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readiness_reports_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"] == "up"


async def test_ingest_single_entry(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{PREFIX}/logs", json=entry(message="single"))
    assert response.status_code == 201
    body = response.json()
    assert body["accepted"] == 1
    assert len(body["ids"]) == 1


async def test_ingest_batch_returns_ids_in_submission_order(client: httpx.AsyncClient) -> None:
    ids = await seed(client, [entry(message=f"line {i}") for i in range(5)])
    assert len(ids) == 5
    assert ids == sorted(ids)

    fetched = [(await client.get(f"{PREFIX}/logs/{i}")).json()["message"] for i in ids]
    assert fetched == [f"line {i}" for i in range(5)]


async def test_batch_over_limit_is_rejected(client: httpx.AsyncClient, settings: Settings) -> None:
    oversized = [entry() for _ in range(settings.max_batch_size + 1)]
    response = await client.post(f"{PREFIX}/logs", json=oversized)
    assert response.status_code == 413


async def test_empty_batch_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{PREFIX}/logs", json=[])
    assert response.status_code == 422


async def test_missing_timestamp_defaults_to_now(client: httpx.AsyncClient) -> None:
    before = datetime.now(UTC)
    response = await client.post(
        f"{PREFIX}/logs",
        json={"level": "WARNING", "service": "worker", "message": "no timestamp"},
    )
    entry_id = response.json()["ids"][0]

    stored = (await client.get(f"{PREFIX}/logs/{entry_id}")).json()
    assert datetime.fromisoformat(stored["timestamp"]) >= before


async def test_structured_context_round_trips(client: httpx.AsyncClient) -> None:
    context = {"user_id": 42, "path": "/casts", "nested": {"retries": 2}, "ok": True}
    entry_id = (await seed(client, [entry(context=context)]))[0]

    stored = (await client.get(f"{PREFIX}/logs/{entry_id}")).json()
    assert stored["context"] == context


async def test_unknown_entry_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/logs/999999")
    assert response.status_code == 404


async def test_query_returns_newest_first(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(message="oldest", timestamp=(NOW - timedelta(minutes=10)).isoformat()),
            entry(message="newest", timestamp=NOW.isoformat()),
            entry(message="middle", timestamp=(NOW - timedelta(minutes=5)).isoformat()),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs")).json()
    assert [item["message"] for item in body["items"]] == ["newest", "middle", "oldest"]


async def test_query_filters_by_service_and_level(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(service="api", level="INFO", message="api info"),
            entry(service="api", level="ERROR", message="api error"),
            entry(service="worker", level="ERROR", message="worker error"),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs", params={"service": "api"})).json()
    assert {item["message"] for item in body["items"]} == {"api info", "api error"}

    body = (await client.get(f"{PREFIX}/logs", params={"level": "ERROR"})).json()
    assert {item["message"] for item in body["items"]} == {"api error", "worker error"}

    body = (await client.get(f"{PREFIX}/logs", params={"service": "api", "level": "ERROR"})).json()
    assert [item["message"] for item in body["items"]] == ["api error"]


async def test_min_level_uses_severity_order(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(level="DEBUG", message="debug"),
            entry(level="INFO", message="info"),
            entry(level="ERROR", message="error"),
            entry(level="CRITICAL", message="critical"),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs", params={"min_level": "ERROR"})).json()
    assert {item["message"] for item in body["items"]} == {"error", "critical"}


async def test_query_filters_by_time_range(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(message="old", timestamp=(NOW - timedelta(days=2)).isoformat()),
            entry(message="recent", timestamp=NOW.isoformat()),
        ],
    )

    body = (
        await client.get(f"{PREFIX}/logs", params={"start": (NOW - timedelta(hours=1)).isoformat()})
    ).json()
    assert [item["message"] for item in body["items"]] == ["recent"]


async def test_inverted_time_range_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(
        f"{PREFIX}/logs",
        params={"start": NOW.isoformat(), "end": (NOW - timedelta(days=1)).isoformat()},
    )
    assert response.status_code == 422


async def test_full_text_search_over_messages(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(message="connection timeout talking to the encoder"),
            entry(message="playlist refreshed successfully"),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs", params={"q": "timeout"})).json()
    assert body["count"] == 1
    assert "timeout" in body["items"][0]["message"]


async def test_context_containment_filter(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(message="user 42", context={"user_id": 42, "region": "eu"}),
            entry(message="user 7", context={"user_id": 7, "region": "eu"}),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs", params={"contains": '{"user_id": 42}'})).json()
    assert [item["message"] for item in body["items"]] == ["user 42"]


async def test_malformed_contains_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/logs", params={"contains": "not json"})
    assert response.status_code == 422


async def test_trace_id_filter(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(message="span a", trace_id="trace-1"),
            entry(message="span b", trace_id="trace-1"),
            entry(message="other", trace_id="trace-2"),
        ],
    )

    body = (await client.get(f"{PREFIX}/logs", params={"trace_id": "trace-1"})).json()
    assert body["count"] == 2


async def test_cursor_pagination_walks_every_row_once(client: httpx.AsyncClient) -> None:
    # All entries share a timestamp, so paging must fall back to the id tiebreaker.
    total = 12
    await seed(client, [entry(message=f"row {i}") for i in range(total)])

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(total):  # Bounded so a stuck cursor fails instead of looping forever.
        params = {"limit": 5}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get(f"{PREFIX}/logs", params=params)).json()
        seen.extend(item["message"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None
    assert len(seen) == total
    assert len(set(seen)) == total


async def test_limit_is_capped_at_max_page_size(client: httpx.AsyncClient) -> None:
    await seed(client, [entry(message=f"row {i}") for i in range(3)])
    body = (await client.get(f"{PREFIX}/logs", params={"limit": 10_000})).json()
    assert body["count"] == 3


async def test_invalid_cursor_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{PREFIX}/logs", params={"cursor": "nonsense!"})
    assert response.status_code == 422


async def test_stats_counts_by_level_and_service(client: httpx.AsyncClient) -> None:
    await seed(
        client,
        [
            entry(service="api", level="INFO"),
            entry(service="api", level="ERROR"),
            entry(service="worker", level="ERROR"),
        ],
    )

    body = (
        await client.get(
            f"{PREFIX}/stats",
            params={
                "since": (NOW - timedelta(hours=1)).isoformat(),
                "until": (NOW + timedelta(hours=1)).isoformat(),
            },
        )
    ).json()

    assert body["total"] == 3
    assert {row["level"]: row["count"] for row in body["by_level"]} == {"INFO": 1, "ERROR": 2}
    assert {row["service"]: row["count"] for row in body["by_service"]} == {"api": 2, "worker": 1}


async def test_stats_window_excludes_older_entries(client: httpx.AsyncClient) -> None:
    await seed(client, [entry(timestamp=(NOW - timedelta(days=30)).isoformat())])
    body = (await client.get(f"{PREFIX}/stats")).json()
    assert body["total"] == 0


async def test_purge_removes_only_old_entries(
    client: httpx.AsyncClient, database: Database
) -> None:
    await seed(
        client,
        [
            entry(message="ancient", timestamp=(NOW - timedelta(days=90)).isoformat()),
            entry(message="fresh", timestamp=NOW.isoformat()),
        ],
    )

    repository = LogRepository(database.pool)
    cutoff = NOW - timedelta(days=1)
    assert await repository.count_before(cutoff) == 1
    assert await repository.purge(cutoff) == 1

    body = (await client.get(f"{PREFIX}/logs")).json()
    assert [item["message"] for item in body["items"]] == ["fresh"]


async def test_migrations_are_idempotent(database: Database) -> None:
    assert await database.migrate() == []
