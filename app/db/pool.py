"""Database connection pool (asyncpg)."""
import asyncpg
from app.config import DATABASE_URL

_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create the asyncpg connection pool."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Get the current pool, creating one if needed."""
    if _pool is None:
        await create_pool()
    return _pool  # type: ignore[return-value]


async def close_pool():
    """Close the pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None