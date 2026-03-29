"""Performance tests: SSE with multiple clients."""
import pytest
import os
import queue
import threading
import time

os.environ['TESTING'] = '1'

pytestmark = pytest.mark.slow


class TestSSELoad:
    def test_10_sse_clients(self):
        """Register 10 SSE clients and broadcast to all."""
        from services import sse_hub

        clients = []
        for _ in range(10):
            q = sse_hub.register_client()
            clients.append(q)

        try:
            # Broadcast 100 messages
            t0 = time.time()
            for i in range(100):
                sse_hub.broadcast(f'{{"msg": {i}}}')
            elapsed_ms = (time.time() - t0) * 1000

            # All clients should have received all messages
            for q in clients:
                count = 0
                while not q.empty():
                    q.get_nowait()
                    count += 1
                assert count == 100, f"Client received {count} messages instead of 100"

            # Should complete in reasonable time
            assert elapsed_ms < 1000, f"Broadcasting 100 messages to 10 clients took {elapsed_ms:.0f}ms"
        finally:
            for q in clients:
                sse_hub.unregister_client(q)

    def test_sse_cleanup_on_disconnect(self):
        """Unregistered clients should be cleaned up."""
        from services import sse_hub

        q = sse_hub.register_client()
        initial_count = len(sse_hub._SSE_HUB_CLIENTS)

        sse_hub.unregister_client(q)
        assert len(sse_hub._SSE_HUB_CLIENTS) == initial_count - 1
