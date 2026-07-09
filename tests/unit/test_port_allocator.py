"""PortAllocator recycle pool behavior."""
from __future__ import annotations

from aitp_playground.hosting.port_allocator import PortAllocator


def test_allocate_monotonic() -> None:
    pa = PortAllocator(start=9000)
    assert pa.allocate() == 9000
    assert pa.allocate() == 9001
    assert pa.allocate() == 9002


def test_release_recycles_port() -> None:
    pa = PortAllocator(start=9100)
    a, _b = pa.allocate(), pa.allocate()  # 9100, 9101
    pa.release(a)
    # Recycled port comes back first (LIFO).
    assert pa.allocate() == a
    # Next allocation is fresh.
    assert pa.allocate() == 9102


def test_release_ignores_out_of_range() -> None:
    pa = PortAllocator(start=9200)
    pa.allocate()
    pa.release(9999)  # never allocated — silently ignored
    # Free pool still empty; allocate goes to next monotonic.
    assert pa.allocate() == 9201


def test_reset_clears_pool() -> None:
    pa = PortAllocator(start=9300)
    p = pa.allocate()
    pa.release(p)
    pa.reset()
    assert pa.allocate() == 9300


def test_allocate_with_offset() -> None:
    pa = PortAllocator(start=8100)
    # Offsets are honored independently of allocation order.
    assert pa.allocate(offset=2) == 8102
    assert pa.allocate(offset=1) == 8101
    assert pa.allocate(offset=0) == 8100
    # Subsequent monotonic allocation skips ports already in use.
    assert pa.allocate() == 8103


def test_offset_collision_falls_back() -> None:
    pa = PortAllocator(start=8100)
    assert pa.allocate(offset=0) == 8100
    # Same offset again collides → fall back to the next free monotonic slot.
    assert pa.allocate(offset=0) == 8101
