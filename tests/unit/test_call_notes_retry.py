"""Tests for the 5xx retry helper in post_call_summary_notes (Issue #6a)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.scheduling import _post_with_retry_on_5xx


def _mock_response(status_code: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    return resp


@pytest.fixture(autouse=True)
def _no_real_sleep():
    """Skip the real backoff sleep — tests run instantly."""
    with patch("asyncio.sleep", new_callable=AsyncMock) as m:
        yield m


@pytest.mark.asyncio
async def test_2xx_no_retry():
    """2xx response on first attempt — no retries, return immediately."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=_mock_response(200))

    resp = await _post_with_retry_on_5xx(
        client, "http://x", {}, {}, label="test", max_attempts=3,
    )

    assert resp.status_code == 200
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_4xx_no_retry():
    """4xx (client error) returned as-is — retrying won't help."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=_mock_response(400))

    resp = await _post_with_retry_on_5xx(
        client, "http://x", {}, {}, label="test", max_attempts=3,
    )

    assert resp.status_code == 400
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_5xx_then_2xx_succeeds():
    """5xx on attempt 1, 2xx on attempt 2 — succeeds without exception."""
    client = AsyncMock()
    client.post = AsyncMock(
        side_effect=[_mock_response(500), _mock_response(200)],
    )

    resp = await _post_with_retry_on_5xx(
        client, "http://x", {}, {}, label="test", max_attempts=3,
    )

    assert resp.status_code == 200
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_5xx_exhausts_max_attempts():
    """All attempts return 5xx — return the last response, no exception."""
    client = AsyncMock()
    client.post = AsyncMock(
        side_effect=[_mock_response(503), _mock_response(503), _mock_response(503)],
    )

    resp = await _post_with_retry_on_5xx(
        client, "http://x", {}, {}, label="test", max_attempts=3,
    )

    assert resp.status_code == 503
    assert client.post.call_count == 3


@pytest.mark.asyncio
async def test_5xx_504_502_then_201_succeeds():
    """Different 5xx codes all trigger retry; final 2xx succeeds."""
    client = AsyncMock()
    client.post = AsyncMock(
        side_effect=[_mock_response(504), _mock_response(502), _mock_response(201)],
    )

    resp = await _post_with_retry_on_5xx(
        client, "http://x", {}, {}, label="test", max_attempts=3,
    )

    assert resp.status_code == 201
    assert client.post.call_count == 3


@pytest.mark.asyncio
async def test_default_max_attempts_is_3():
    """Default of 3 attempts: 1 + 2 retries."""
    client = AsyncMock()
    client.post = AsyncMock(
        side_effect=[_mock_response(500), _mock_response(500), _mock_response(500)],
    )

    resp = await _post_with_retry_on_5xx(client, "http://x", {}, {}, label="test")

    assert resp.status_code == 500
    assert client.post.call_count == 3
