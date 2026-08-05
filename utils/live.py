"""In-process pub/sub for live checkout updates (SSE).

The AMQP consumer, Stripe webhook handlers, and API endpoints all run on the
same event loop in a single uvicorn process, so a dict of asyncio.Queues is
enough to fan "checkout N changed" wake-ups out to any open event streams.
Queues carry no payload -- subscribers re-read the checkout from the DB on
wake-up, so a burst of updates coalesces into one refresh (maxsize=1).

If the service ever runs with multiple workers, this must move to
broker-backed fan-out; a queue registered in one worker is invisible to the
others.
"""

import asyncio
from collections import defaultdict

_subscribers: "dict[int, set[asyncio.Queue]]" = defaultdict(set)


def subscribe(checkout_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    _subscribers[int(checkout_id)].add(queue)
    return queue


def unsubscribe(checkout_id: int, queue: asyncio.Queue) -> None:
    subscribers = _subscribers.get(int(checkout_id))
    if subscribers is None:
        return
    subscribers.discard(queue)
    if not subscribers:
        _subscribers.pop(int(checkout_id), None)


def notify(checkout_id) -> None:
    """Wake every stream watching this checkout. Callable from any handler on
    the loop; a no-op when nobody is watching or the id is not an int."""
    try:
        key = int(checkout_id)
    except (TypeError, ValueError):
        return
    for queue in list(_subscribers.get(key, ())):
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass  # a wake-up is already pending; coalescing is the point
