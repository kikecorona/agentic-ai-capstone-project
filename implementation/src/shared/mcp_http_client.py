"""Sync wrapper around an MCP streamable-HTTP client session.

The MCP Python SDK is async-only — every ``ClientSession.call_tool`` returns
a coroutine. Most of our service code is synchronous (RAG, BP, SD all call
peers from inside sync request handlers), so we maintain *one*
long-running asyncio loop in a daemon thread per ``MCPHttpClient`` instance
and shuttle coroutines onto it via ``asyncio.run_coroutine_threadsafe``.

This is the minimum-viable plumbing for the multi-process ``start_all``
deployment ([§8.5 POC](../../../PROJECT_ARCHITECTURE.md#85-considerations-for-the-poc)
splits services across processes; each peer is reached over MCP-HTTP).
The cost is one HTTP round-trip per call; the LLM time dominates so the
overhead is invisible. A future optimisation could pool sessions across
peers if call volume grows, but the POC's per-service ``MCPHttpClient``
pattern is plenty.

Usage::

    rag = MCPHttpClient(url="http://127.0.0.1:8102/mcp", name="rag")
    out = rag.call("retrieve", {"query": "...", "domain_filter": "bp"})
    rag.close()

The session is opened lazily on the first ``.call(...)`` so importing this
module never blocks on a network connection.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


class _AsyncBridge:
    """Long-running asyncio loop hosted on a daemon thread.

    All MCP work happens on this loop so the HTTP keep-alive connection +
    the SSE response stream stay alive across calls. Sync callers submit
    coroutines via :py:meth:`run` and block on the future result.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="mcp-http-bridge",
            daemon=True,
        )
        self._thread.start()
        self._closed = False

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def run(self, coro, *, timeout: float | None = None):
        if self._closed:
            raise RuntimeError("MCPHttpClient bridge is closed")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=2)


class MCPHttpClient:
    """Holds a live :class:`mcp.ClientSession` to a remote MCP server.

    The session opens on demand (first ``call``) and stays open for the
    life of the client. Reconnect is best-effort: a transport error
    closes the session and the next call re-opens it.
    """

    def __init__(self, url: str, *, name: str = "peer", timeout_s: float = 60.0):
        self.url = url
        self.name = name
        self._timeout = timeout_s
        self._bridge = _AsyncBridge()
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------

    def _open_session(self) -> None:
        async def _open():
            stack = AsyncExitStack()
            r, w, _ = await stack.enter_async_context(streamablehttp_client(self.url))
            session = ClientSession(r, w)
            await stack.enter_async_context(session)
            await session.initialize()
            return stack, session

        stack, session = self._bridge.run(_open(), timeout=self._timeout)
        self._stack = stack
        self._session = session

    def _close_session(self) -> None:
        if self._stack is None:
            return
        stack = self._stack
        self._stack = None
        self._session = None

        async def _shut():
            await stack.aclose()

        try:
            self._bridge.run(_shut(), timeout=self._timeout)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._close_session()
            self._bridge.close()

    # -- calls ---------------------------------------------------------------

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke ``tool`` on the remote MCP and return the parsed result.

        FastMCP servers return either structured JSON content or a list of
        text content blocks; we normalise both into a Python value
        (``dict`` / ``list`` / ``str``) so callers don't deal with the
        wire format.
        """
        with self._lock:
            if self._session is None:
                self._open_session()
            try:
                return self._bridge.run(self._call(tool, args or {}), timeout=self._timeout)
            except Exception:
                # Best-effort reconnect: any transport-level failure drops
                # the session so the next call opens a fresh one. The
                # current call still raises so the caller knows.
                self._close_session()
                raise

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        assert self._session is not None
        result = await self._session.call_tool(tool, args)
        return _unpack_result(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unpack_result(result: Any) -> Any:
    """Convert an MCP ``CallToolResult`` into a plain Python value.

    Tool calls in our codebase return JSON-serialisable dicts. The wire
    format wraps them as either ``structuredContent`` or a list of text
    content blocks; we accept either.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # structuredContent is already a Python value when set.
        return structured
    content = getattr(result, "content", None) or []
    for block in content:
        block_type = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        text = getattr(block, "text", None) or (
            block.get("text") if isinstance(block, dict) else None
        )
        if block_type == "text" and text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    # No content — return None to signal an empty response.
    return None
