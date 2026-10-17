from typing import Any

from sqlalchemy import URL, MetaData, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from api.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_database_url() -> URL:
    settings = get_settings()
    if settings.database_url:
        return make_url(settings.database_url)
    return URL.create(
        drivername="postgresql+asyncpg",
        username=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def build_engine_kwargs() -> dict[str, Any]:
    """Pool and TLS options. asyncpg takes `ssl`; `sslmode` is rejected by it."""
    settings = get_settings()

    connect_args: dict[str, Any] = {}
    if settings.db_ssl:
        connect_args["ssl"] = settings.db_ssl

    kwargs: dict[str, Any] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,
    }
    if connect_args:
        kwargs["connect_args"] = connect_args
    if settings.db_prepared_statement_cache_size is not None:
        # A SQLAlchemy dialect option, not an asyncpg connect argument.
        kwargs["prepared_statement_cache_size"] = (
            settings.db_prepared_statement_cache_size
        )
    return kwargs


def build_engine() -> AsyncEngine:
    return create_async_engine(get_database_url(), **build_engine_kwargs())


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def database_ok(engine: AsyncEngine) -> bool:
    """One cheap round trip, so readiness reflects a real connection."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return True
