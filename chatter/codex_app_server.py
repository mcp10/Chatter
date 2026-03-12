"""Async helper for talking to `codex app-server` over stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from itertools import count
from typing import Any, Callable

from . import __version__

_PROCESS_CONFIG_ARGS = (
    'apps._default.default_tools_approval_mode="prompt"',
    "apps._default.default_tools_enabled=true",
)


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server transport fails."""


class CodexAppServerClient:
    """Minimal async JSON-RPC client for the Codex app-server."""

    def __init__(
        self,
        cwd: str,
        *,
        stderr_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._stderr_callback = stderr_callback
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._next_request_id = count(1)
        self._lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def ensure_started(self) -> None:
        async with self._lifecycle_lock:
            if self._proc is not None and self._proc.returncode is None:
                return

            await self.close()
            self._proc = await asyncio.create_subprocess_exec(
                "codex",
                "app-server",
                *sum((["-c", value] for value in _PROCESS_CONFIG_ARGS), []),
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())

            try:
                await self._request_started(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "chatter",
                            "version": __version__,
                        },
                        "capabilities": {
                            "experimentalApi": True,
                        },
                    },
                    timeout=15.0,
                )
                await self._notify_started("initialized")
            except Exception:
                await self.close()
                raise

    async def close(self) -> None:
        proc = self._proc
        self._proc = None

        if proc is not None and proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            with suppress(Exception):
                await proc.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        self._reader_task = None
        self._stderr_task = None
        self._reject_pending(CodexAppServerError("Codex app-server closed."))

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> Any:
        await self.ensure_started()
        return await self._request_started(method, params, timeout=timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.ensure_started()
        await self._notify_started(method, params)

    async def respond(
        self,
        request_id: int | str,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if error is not None and result is not None:
            raise ValueError("JSON-RPC response cannot contain both result and error.")
        if error is None and result is None:
            result = {}

        message: dict[str, Any] = {"id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        await self._send_message(message)

    async def next_event(self, *, timeout: float | None = None) -> dict[str, Any]:
        await self.ensure_started()
        if timeout is None:
            return await self._events.get()
        return await asyncio.wait_for(self._events.get(), timeout=timeout)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        try:
            await self.request(
                "turn/interrupt",
                {
                    "threadId": thread_id,
                    "turnId": turn_id,
                },
                timeout=10.0,
            )
        except Exception:
            pass

    def drain_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _send_message(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise CodexAppServerError("Codex app-server is not running.")

        payload = json.dumps(message, separators=(",", ":")) + "\n"
        async with self._write_lock:
            proc.stdin.write(payload.encode("utf-8"))
            await proc.stdin.drain()

    async def _request_started(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> Any:
        request_id = next(self._next_request_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_message(
                {
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _notify_started(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send_message(message)

    async def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise CodexAppServerError(
                        f"Invalid JSON from Codex app-server: {exc}"
                    ) from exc

                if "id" in message and "method" not in message:
                    self._resolve_pending(message)
                    continue

                method = str(message.get("method") or "")
                if method.startswith("codex/event/"):
                    continue

                await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reject_pending(exc)
            await self._events.put(
                {
                    "method": "error",
                    "params": {
                        "message": str(exc),
                    },
                }
            )
        finally:
            return_code = None
            if proc.returncode is None:
                with suppress(Exception):
                    return_code = await proc.wait()
            else:
                return_code = proc.returncode

            if return_code not in (None, 0):
                self._reject_pending(
                    CodexAppServerError(
                        f"Codex app-server exited with code {return_code}."
                    )
                )

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return

        while True:
            raw_line = await proc.stderr.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line and self._stderr_callback is not None:
                self._stderr_callback(line)

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return

        future = self._pending.get(request_id)
        if future is None or future.done():
            return

        error = message.get("error")
        if error is not None:
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, default=str)
            else:
                detail = str(error)
            future.set_exception(CodexAppServerError(detail))
            return

        future.set_result(message.get("result"))

    def _reject_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
