from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field

from redis.asyncio import Redis

from .models import utcnow

log = logging.getLogger("brain.hub")

QUEUE_SIZE = 256
CHANNEL_PATTERN = "brain:*:events"

Listener = Callable[["Event"], Awaitable[None]]


@dataclass(slots=True)
class Event:
    type: str
    project_id: str
    file: str | None = None
    heading: str | None = None
    slug: str | None = None
    etag: str | None = None
    previous_etag: str | None = None
    agent_id: str | None = None
    department: str | None = None
    subagent_role: str | None = None
    summary: str | None = None
    log_id: int | None = None
    importance: str = "normal"
    affects: list[str] | None = None
    ts: str = field(default_factory=lambda: utcnow().isoformat() + "Z")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> Event:
        data = json.loads(raw)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def sse(self) -> str:
        return f"event: {self.type}\ndata: {self.to_json()}\n\n"


def channel_for(project_id: str) -> str:
    return f"brain:{project_id}:events"


class LocalHub:
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue[Event]]] = {}
        self._listeners: dict[str, set[Listener]] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def subscriber_count(self, project_id: str) -> int:
        return len(self._queues.get(project_id, ()))

    def add_listener(self, project_id: str, listener: Listener) -> None:
        self._listeners.setdefault(project_id, set()).add(listener)

    def remove_listener(self, project_id: str, listener: Listener) -> None:
        listeners = self._listeners.get(project_id)
        if listeners:
            listeners.discard(listener)
            if not listeners:
                self._listeners.pop(project_id, None)

    async def publish(self, event: Event) -> None:
        await self._dispatch(event)

    async def _dispatch(self, event: Event) -> None:
        for queue in list(self._queues.get(event.project_id, ())):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)
        listeners = list(self._listeners.get(event.project_id, ())) + list(
            self._listeners.get("*", ())
        )
        for listener in listeners:
            try:
                await listener(event)
            except Exception:
                log.exception("event listener failed for project %s", event.project_id)

    @asynccontextmanager
    async def subscribe(self, project_id: str) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.setdefault(project_id, set()).add(queue)
        try:
            yield queue
        finally:
            subscribers = self._queues.get(project_id)
            if subscribers:
                subscribers.discard(queue)
                if not subscribers:
                    self._queues.pop(project_id, None)


class RedisHub(LocalHub):
    def __init__(self, redis: Redis) -> None:
        super().__init__()
        self._redis = redis
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._listen(), name="brain-redis-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def publish(self, event: Event) -> None:
        await self._redis.publish(channel_for(event.project_id), event.to_json())

    async def _listen(self) -> None:
        while True:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.psubscribe(CHANNEL_PATTERN)
                async for message in pubsub.listen():
                    if message.get("type") != "pmessage":
                        continue
                    try:
                        await self._dispatch(Event.from_json(message["data"]))
                    except Exception:
                        log.exception("could not dispatch redis event")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("redis subscription dropped, retrying")
                await asyncio.sleep(1)


Hub = LocalHub


def build_hub(redis: Redis | None) -> LocalHub:
    if redis is None:
        return LocalHub()
    return RedisHub(redis)
