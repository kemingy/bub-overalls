"""Length-bounded JSON lines, binary process streams encoded as base64."""

from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_FRAME = 8 * 1024 * 1024
MAX_BUFFER = 8 * 1024 * 1024


async def receive(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise ConnectionError("guest connection closed")
    if len(line) > MAX_FRAME:
        raise ValueError("guest frame too large")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("expected a guest protocol object")
    return message


async def send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
    if len(payload) > MAX_FRAME:
        raise ValueError("host frame too large")
    writer.write(payload)
    await writer.drain()
