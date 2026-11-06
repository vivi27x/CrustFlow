"""In-process pub/sub that feeds the dashboard's SSE stream."""

import asyncio
from typing import Any

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def publish(event: str, data: dict[str, Any]) -> None:
    for queue in list(_subscribers):
        try:
            queue.put_nowait({"event": event, "data": data})
        except asyncio.QueueFull:
            # A stalled dashboard tab must never slow the pipeline. It catches up on reload.
            _subscribers.discard(queue)
