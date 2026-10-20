import os
import platform
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from bub_overalls import OverallsConfig, OverallsEnvironment


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "sandbox" not in item.keywords:
            continue
        system = "Linux" if "landlock" in item.keywords else "Darwin"
        if platform.system() != system or os.environ.get("BUB_OVERALLS_TEST") != "1":
            item.add_marker(
                pytest.mark.skip(reason=f"requires {system} and BUB_OVERALLS_TEST=1")
            )


@pytest_asyncio.fixture
async def environment(tmp_path: Path) -> AsyncIterator[OverallsEnvironment]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with OverallsEnvironment(OverallsConfig(), workspace) as sandbox:
        yield sandbox
