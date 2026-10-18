import os
import platform
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bub_overalls import OverallsConfig, OverallsEnvironment


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if platform.system() == "Darwin" and os.environ.get("BUB_OVERALLS_TEST") == "1":
        return
    skip_sandbox = pytest.mark.skip(reason="requires macOS and BUB_OVERALLS_TEST=1")
    for item in items:
        if "sandbox" in item.keywords:
            item.add_marker(skip_sandbox)


@pytest_asyncio.fixture
async def environment(tmp_path: Path) -> AsyncIterator[OverallsEnvironment]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with OverallsEnvironment(OverallsConfig(), workspace) as sandbox:
        yield sandbox
