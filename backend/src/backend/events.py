"""实时事件总线：支持 InMemory 和 Redis pub/sub 两种模式。

用于 SSE 实时推送任务进度更新。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

# ── 事件类型 ────────────────────────────────────────────────────────────────

EVENT_JOB_UPDATED = "job_updated"
EVENT_DOCUMENT_UPDATED = "document_updated"


def make_event(event: str, data: dict[str, object]) -> str:
    """构造 SSE 格式事件字符串。"""
    lines = [f"event: {event}"]
    payload = json.dumps(data, ensure_ascii=False, default=str)
    # SSE 规范要求 data 以换行结尾，多行 data 用 \n 前缀
    for line in payload.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


# ── 抽象事件总线 ────────────────────────────────────────────────────────────


class EventBus(ABC):
    """事件总线抽象基类。"""

    @abstractmethod
    async def publish(self, channel: str, event: str, data: dict[str, object]) -> None:
        """发布事件到指定频道。"""
        ...

    @abstractmethod
    async def subscribe(self, channel: str) -> asyncio.Queue:
        """订阅频道，返回一个异步队列接收事件。"""
        ...

    @abstractmethod
    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        """取消订阅。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭事件总线。"""
        ...


# ── InMemory 实现 ──────────────────────────────────────────────────────────


class InMemoryEventBus(EventBus):
    """进程内事件总线：使用 asyncio.Queue 在同一个进程内分发事件。

    适用于 ThreadPoolExecutor 模式或无 Redis 的场景。
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    async def publish(self, channel: str, event: str, data: dict[str, object]) -> None:
        queues = self._subscribers.get(channel, set())
        if not queues:
            return
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        for queue in list(queues):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # 丢弃消息，避免阻塞发布者

    async def subscribe(self, channel: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(channel, set()).add(queue)
        return queue

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(channel)
        if subscribers:
            subscribers.discard(queue)

    async def close(self) -> None:
        self._subscribers.clear()


# ── Redis 实现 ──────────────────────────────────────────────────────────────


class RedisEventBus(EventBus):
    """Redis pub/sub 事件总线。

    适用于 Celery 模式（跨进程事件分发）。
    """

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url)
        self._pubsub = self._redis.pubsub()
        self._subscriptions: dict[str, asyncio.Queue] = {}
        self._listener_task: asyncio.Task | None = None

    async def publish(self, channel: str, event: str, data: dict[str, object]) -> None:
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        await self._redis.publish(channel, message)

    async def subscribe(self, channel: str) -> asyncio.Queue:
        if channel in self._subscriptions:
            return self._subscriptions[channel]

        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscriptions[channel] = queue

        await self._pubsub.subscribe(channel)

        # 启动监听任务（仅一次）
        if self._listener_task is None:
            self._listener_task = asyncio.create_task(self._listen())

        return queue

    async def _listen(self) -> None:
        """持续监听 Redis pub/sub 消息并分发到对应的队列。"""
        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue
                channel = message["channel"].decode() if isinstance(message["channel"], bytes) else message["channel"]
                data = message["data"].decode() if isinstance(message["data"], bytes) else message["data"]
                queue = self._subscriptions.get(channel)
                if queue is not None:
                    try:
                        queue.put_nowait(data)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("redis_event_bus_listener_error")

    async def unsubscribe(self, channel: str, queue: asyncio.Queue) -> None:
        if channel in self._subscriptions:
            del self._subscriptions[channel]
        await self._pubsub.unsubscribe(channel)

    async def close(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        await self._pubsub.close()
        await self._redis.close()


# ── 工厂函数 ────────────────────────────────────────────────────────────────


_event_bus: EventBus | None = None


async def get_event_bus(settings: Settings) -> EventBus:
    """获取或创建事件总线单例。"""
    global _event_bus
    if _event_bus is not None:
        return _event_bus

    if settings.redis_url:
        _event_bus = RedisEventBus(settings.redis_url)
        logger.info("event_bus: using Redis pub/sub (%s)", settings.redis_url)
    else:
        _event_bus = InMemoryEventBus()
        logger.info("event_bus: using InMemory (no Redis configured)")
    return _event_bus


async def close_event_bus() -> None:
    """关闭事件总线。"""
    global _event_bus
    if _event_bus is not None:
        await _event_bus.close()
        _event_bus = None