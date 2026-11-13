"""Keep CLI asyncio.run tests isolated from pytest-asyncio's legacy default loop."""

import asyncio
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def explicit_default_loop() -> Iterator[None]:
    # Without an explicit None, the plugin's get_event_loop() may allocate an idle
    # loop which a later CLI asyncio.run() replaces without owning/closing it.
    # Each async test and CLI invocation instead owns its own Runner-managed loop.
    asyncio.set_event_loop(None)
    yield
