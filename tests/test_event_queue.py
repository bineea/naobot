import pytest

from naobot.models import Envelope, MessageType
from naobot.server import BoundedPriorityEventQueue


def event(name: str, priority: int) -> Envelope:
    return Envelope(type=MessageType.EVENT, priority=priority, payload={"name": name})


def test_priority_event_queue_defaults_to_capacity_32() -> None:
    assert BoundedPriorityEventQueue().capacity == 32


@pytest.mark.asyncio
async def test_priority_event_queue_orders_high_priority_first_and_preserves_fifo() -> None:
    queue = BoundedPriorityEventQueue(capacity=4)
    await queue.put(event("low", 1))
    await queue.put(event("high-first", 8))
    await queue.put(event("high-second", 8))

    assert (await queue.get()).payload["name"] == "high-first"
    assert (await queue.get()).payload["name"] == "high-second"
    assert (await queue.get()).payload["name"] == "low"


@pytest.mark.asyncio
async def test_priority_event_queue_evicts_lowest_for_more_important_event() -> None:
    queue = BoundedPriorityEventQueue(capacity=2)
    await queue.put(event("low", 1))
    await queue.put(event("medium", 4))

    result = await queue.put(event("high", 9))

    assert result.accepted is True
    assert result.evicted is not None
    assert result.evicted.payload["name"] == "low"
    assert (await queue.get()).payload["name"] == "high"


@pytest.mark.asyncio
async def test_priority_event_queue_rejects_new_low_priority_when_full() -> None:
    queue = BoundedPriorityEventQueue(capacity=2)
    await queue.put(event("high", 9))
    await queue.put(event("medium", 4))

    result = await queue.put(event("low", 1))

    assert result.accepted is False
    assert result.evicted is None
