from __future__ import annotations

import asyncio
import base64
import json
import tarfile
from pathlib import Path

import pytest
from bub import BubFramework

from bub_sandbox.environment import GuestProcess, MicroVMEnvironment, workspace_archive
from bub_sandbox.protocol import MAX_BUFFER, receive


def test_workspace_import_omits_credentials_and_host_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.py").write_text("print(1)")
    (workspace / "build").mkdir()
    (workspace / "build/guest.raw").write_text("generated disk; never import")
    (workspace / ".env").write_text("secret")
    (workspace / ".env.production").write_text("secret")
    (workspace / ".git").mkdir()
    (workspace / ".git/config").write_text("secret")
    (workspace / "symlink").symlink_to(tmp_path)
    (workspace / "sub").mkdir()
    (workspace / "sub/.aws").mkdir()
    (workspace / "sub/.aws/credentials").write_text("secret")
    (workspace / "sub/file.txt").write_text("data")
    destination = tmp_path / "workspace.tar"
    workspace_archive(workspace, destination)
    with tarfile.open(destination) as archive:
        assert set(archive.getnames()) == {"source.py", "sub", "sub/file.txt"}
        assert all(entry.uid == entry.gid == 1000 for entry in archive.getmembers())
        assert all(not entry.issym() for entry in archive.getmembers())


async def test_protocol_rejects_disconnect_and_non_object_messages() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"[]\n")
    with pytest.raises(ValueError, match="object"):
        await receive(reader)
    reader.feed_eof()
    with pytest.raises(ConnectionError, match="closed"):
        await receive(reader)


async def test_process_exit_preserves_detached_descendant_liveness() -> None:
    environment = MicroVMEnvironment()
    process = GuestProcess(environment, 1)
    process.event({"event": "stdout", "data": base64.b64encode(b"hello\x00world").decode()})
    process.event({"event": "exit", "code": 7})
    assert await process.wait() == 7
    assert process.is_running()
    process.event({"event": "tree_exit"})
    process.event({"event": "stdout_eof"})
    assert not process.is_running()
    assert await process.stdout.read() == b"hello\x00world"
    await environment.close()


async def test_unread_process_output_has_a_hard_memory_bound() -> None:
    environment = MicroVMEnvironment()
    process = GuestProcess(environment, 1)
    process.stdout.feed_data(b"x" * MAX_BUFFER)
    with pytest.raises(BufferError, match="unread"):
        process.event({"event": "stdout", "data": "eA=="})
    await environment.close()


async def test_guest_failure_fails_pending_requests_and_process_streams() -> None:
    environment = MicroVMEnvironment()
    future = asyncio.get_running_loop().create_future()
    environment._pending[1] = future
    process = GuestProcess(environment, 2)
    environment._processes[2] = process
    environment._fail(ConnectionError("guest died"))
    with pytest.raises(ConnectionError, match="guest died"):
        await future
    with pytest.raises(ConnectionError, match="guest died"):
        await process.stdout.read()
    assert await process.wait() == -1
    assert not process.is_running()
    await environment.close()


async def test_plugin_runtime_failure_cannot_fall_back_to_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bub_sandbox import plugin

    monkeypatch.setenv("BUB_SANDBOX_HELPER", str(tmp_path / "nonexistent"))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    framework.plugin_manager.register(plugin, name="sandbox")
    async with framework.running():
        with pytest.raises(FileNotFoundError, match="helper"):
            await framework.get_environment("session")
    assert framework._environments == {}


async def test_out_of_order_responses_and_process_events() -> None:
    reader = asyncio.StreamReader()
    requests = []

    class Writer:
        def write(self, data: bytes) -> None:
            requests.append(json.loads(data))
            if len(requests) == 2:
                for request in reversed(requests):
                    reader.feed_data(json.dumps({"id": request["id"], "result": request["id"]}).encode() + b"\n")

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            reader.feed_eof()

        async def wait_closed(self) -> None:
            pass

        @property
        def transport(self):
            return self

        def abort(self) -> None:
            pass

    environment = MicroVMEnvironment()
    environment._writer = Writer()
    environment._reader_task = asyncio.create_task(environment._read(reader))
    try:
        first = asyncio.create_task(environment._request("ping"))
        second = asyncio.create_task(environment._request("ping"))
        assert await asyncio.gather(first, second) == [1, 2]
    finally:
        await environment.close()
