import pytest

from api import main


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRedis:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.pings = 0

    async def ping(self):
        self.pings += 1
        if not self.healthy:
            raise ConnectionError("redis unavailable")
        return True


@pytest.fixture
def ready_app(monkeypatch):
    """Points the readiness endpoint at fakes, so no live services are needed."""

    def apply(*, database_up: bool = True, redis_client=None):
        async def fake_database_ok(engine):
            if not database_up:
                raise ConnectionError("postgres unavailable")
            return True

        monkeypatch.setattr(main, "database_ok", fake_database_ok)
        monkeypatch.setattr(main.app.state, "engine", object(), raising=False)
        monkeypatch.setattr(main.app.state, "redis", redis_client, raising=False)

    return apply


def body(response):
    import json

    return json.loads(response.body)


@pytest.mark.anyio
async def test_ready_when_postgres_and_redis_are_up(ready_app):
    redis_client = FakeRedis()
    ready_app(redis_client=redis_client)

    response = await main.readyz()

    assert response.status_code == 200
    assert body(response) == {
        "status": "ready",
        "checks": {"postgres": "ok", "redis": "ok"},
    }
    assert redis_client.pings == 1


@pytest.mark.anyio
async def test_ready_without_redis_when_redis_is_disabled(ready_app):
    """Running without Redis must not make the service permanently unready."""
    ready_app(redis_client=None)

    response = await main.readyz()

    assert response.status_code == 200
    assert body(response) == {"status": "ready", "checks": {"postgres": "ok"}}


@pytest.mark.anyio
async def test_not_ready_when_postgres_is_down(ready_app):
    ready_app(database_up=False, redis_client=None)

    response = await main.readyz()

    assert response.status_code == 503
    assert body(response) == {"status": "not_ready", "checks": {"postgres": "error"}}


@pytest.mark.anyio
async def test_not_ready_when_redis_is_down(ready_app):
    ready_app(redis_client=FakeRedis(healthy=False))

    response = await main.readyz()

    assert response.status_code == 503
    assert body(response)["checks"] == {"postgres": "ok", "redis": "error"}


@pytest.mark.anyio
async def test_readiness_response_never_leaks_error_details(ready_app):
    ready_app(database_up=False, redis_client=FakeRedis(healthy=False))

    response = await main.readyz()
    text = response.body.decode()

    assert "unavailable" not in text
    assert "ConnectionError" not in text
    assert "Traceback" not in text
