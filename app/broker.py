"""Минимальный брокер событий для SSE.

Подписчики — живые подключения к /api/events. Брокер не переживает перезапуск:
это не очередь задач, а способ сказать фронту «пересчитал, забери».
"""

from __future__ import annotations

import asyncio


class Broker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)


broker = Broker()
