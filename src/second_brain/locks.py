from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from redis.asyncio import Redis

RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class LockRegistry(Protocol):
    def hold(self, *key: str) -> AsyncIterator[None]: ...


class LocalLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[tuple[str, ...], asyncio.Lock] = {}
        self._waiters: dict[tuple[str, ...], int] = {}
        self._guard = asyncio.Lock()

    async def _acquire(self, key: tuple[str, ...]) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._waiters[key] = self._waiters.get(key, 0) + 1
        await lock.acquire()
        return lock

    async def _release(self, key: tuple[str, ...], lock: asyncio.Lock) -> None:
        lock.release()
        async with self._guard:
            remaining = self._waiters.get(key, 1) - 1
            if remaining <= 0:
                self._waiters.pop(key, None)
                self._locks.pop(key, None)
            else:
                self._waiters[key] = remaining

    @asynccontextmanager
    async def hold(self, *key: str) -> AsyncIterator[None]:
        lock = await self._acquire(key)
        try:
            yield
        finally:
            await self._release(key, lock)


class RedisLockRegistry:
    def __init__(self, redis: Redis, ttl_ms: int = 5000, retry_ms: int = 50) -> None:
        self._redis = redis
        self._local = LocalLockRegistry()
        self._ttl_ms = ttl_ms
        self._retry_ms = retry_ms

    @asynccontextmanager
    async def hold(self, *key: str) -> AsyncIterator[None]:
        name = "brain:lock:" + ":".join(key)
        token = secrets.token_hex(16)
        async with self._local.hold(*key):
            deadline = asyncio.get_running_loop().time() + (self._ttl_ms * 4) / 1000
            while not await self._redis.set(name, token, nx=True, px=self._ttl_ms):
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError(f"could not acquire distributed lock {name}")
                await asyncio.sleep(self._retry_ms / 1000)
            try:
                yield
            finally:
                await self._redis.eval(RELEASE_SCRIPT, 1, name, token)


def build_lock_registry(redis: Redis | None) -> LockRegistry:
    if redis is None:
        return LocalLockRegistry()
    return RedisLockRegistry(redis)
