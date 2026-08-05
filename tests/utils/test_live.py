import asyncio
import unittest

from utils import live


def run(coro):
    """Run on a private loop WITHOUT touching the thread's current loop --
    asyncio.run() would unset it and break older get_event_loop()-based
    tests elsewhere in the suite."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestLiveRegistry(unittest.TestCase):
    def setUp(self):
        # The registry is module-global; start each test clean.
        live._subscribers.clear()

    def test_notify_wakes_subscriber(self):
        async def scenario():
            queue = live.subscribe(7)
            live.notify(7)
            await asyncio.wait_for(queue.get(), timeout=1)
            live.unsubscribe(7, queue)

        run(scenario())

    def test_notify_without_subscribers_is_noop(self):
        live.notify(1234)  # must not raise
        self.assertEqual(live._subscribers, {})

    def test_notify_non_int_id_is_noop(self):
        live.notify(None)
        live.notify("not-a-number")

    def test_string_id_reaches_int_subscriber(self):
        # Handlers sometimes carry ids as strings; both must hit one bucket.
        async def scenario():
            queue = live.subscribe(9)
            live.notify("9")
            await asyncio.wait_for(queue.get(), timeout=1)

        run(scenario())

    def test_burst_coalesces_to_single_wakeup(self):
        async def scenario():
            queue = live.subscribe(3)
            for _ in range(5):
                live.notify(3)
            await asyncio.wait_for(queue.get(), timeout=1)
            self.assertTrue(queue.empty())

        run(scenario())

    def test_unsubscribe_removes_empty_bucket(self):
        async def scenario():
            queue = live.subscribe(5)
            live.unsubscribe(5, queue)
            self.assertEqual(live._subscribers, {})
            live.unsubscribe(5, queue)  # double-unsubscribe must not raise

        run(scenario())

    def test_notify_wakes_a_pending_waiter(self):
        # The SSE loop awaits queue.get() with a timeout; a notify must
        # release it well before that timeout.
        async def scenario():
            queue = live.subscribe(11)
            waiter = asyncio.create_task(asyncio.wait_for(queue.get(), timeout=5))
            await asyncio.sleep(0)  # let the waiter block on get()
            live.notify(11)
            await asyncio.wait_for(waiter, timeout=1)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
