"""A dependency-free MCP (Model Context Protocol) server exposing the desktop tools.

Speaks JSON-RPC 2.0 over stdio, one JSON message per line (the MCP stdio
transport), so any MCP client -- Claude Desktop, Claude Code, IDE agents --
can drive the desktop through :class:`~locum.llm.tools.DesktopToolkit`::

    # claude_desktop_config.json
    {"mcpServers": {"locum": {"command": "locum", "args": ["mcp", "--policy", "default"]}}}

Supported methods: ``initialize``, ``notifications/initialized`` (and every
other notification, silently), ``ping``, ``tools/list`` and ``tools/call``
(results carry ``text`` and ``image`` content plus ``isError``). Unknown
methods get JSON-RPC error ``-32601``.

While :meth:`MCPServer.serve` runs, tool calls execute one at a time on a
worker thread, so the server keeps answering ``ping`` / ``tools/list`` and
reading ``notifications/cancelled`` while a long call runs. Every call has a
time limit (``call_timeout``, default 60 s, extended for tools given a longer
``timeout`` argument): past it the client gets an ``isError`` result saying so
and the call is stopped at its next step. ``notifications/cancelled`` stops a
call without a response, as the protocol requires, and a call whose request
carries ``_meta.progressToken`` gets ``notifications/progress`` heartbeats.

Nothing but protocol messages is ever
written to stdout: applications started with ``launch_app`` get ``/dev/null``
as stdin and stdout, and while :meth:`MCPServer.serve` runs on the real stdout,
``sys.stdout`` and file descriptor 1 are pointed at stderr (protocol frames go
to a private duplicate of the original descriptor), so stray ``print`` calls,
native libraries and child processes cannot corrupt the JSON-RPC stream.
"""

from __future__ import annotations

import io
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple, Union

from .._cancel import CallCancelled, CancelScope
from ..errors import FailSafeTriggered
from .safety import AuditLog, Policy
from .tools import DesktopToolkit

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

_INSTRUCTIONS = (
    "Desktop automation tools. Call `observe` first: it returns the interactive elements with numeric ids "
    "and an annotated screenshot. Act on element ids (click, type_text, set_value), selectors, visible text "
    "or screenshot coordinates. Element ids and coordinates refer to the latest observe/screenshot call; "
    "observe again after the screen changes -- with changes_only=true after an action, which returns just "
    "what changed (ids of unchanged elements stay valid) and a cropped image. "
    "Some actions may be blocked by the safety policy."
)


def _version() -> str:
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # pragma: no cover
        return "0.0.0"


class MCPServer:
    """MCP request handler around a :class:`DesktopToolkit`.

    Args:
        desktop: The desktop to drive (default: created lazily on the first tool request).
        policy: Safety policy for every tool call (default: :class:`Policy`).
        audit: Audit log (default: in-memory).
        toolkit: A prepared toolkit (overrides ``desktop``/``policy``/``audit``).
        name: Server name reported by ``initialize``.
        call_timeout: Seconds a ``tools/call`` may run under :meth:`serve` before the
            client gets a timeout result (``None`` = no limit). A tool called with a
            larger ``timeout`` argument (``wait_for``) gets that plus a margin.
        progress_interval: Seconds between ``notifications/progress`` heartbeats.
    """

    #: Extra seconds granted beyond a tool's own ``timeout`` argument.
    TIMEOUT_MARGIN = 15.0

    def __init__(
        self,
        desktop: Any = None,
        policy: Optional[Policy] = None,
        audit: Optional[AuditLog] = None,
        *,
        toolkit: Optional[DesktopToolkit] = None,
        name: str = "locum",
        call_timeout: Optional[float] = 60.0,
        progress_interval: float = 2.0,
    ):
        if call_timeout is not None and (not math.isfinite(float(call_timeout)) or float(call_timeout) <= 0):
            raise ValueError(f"call_timeout must be a finite number > 0 (or None), got {call_timeout!r}")
        self.call_timeout = None if call_timeout is None else float(call_timeout)
        self.progress_interval = max(0.05, float(progress_interval))
        self._out: Optional[TextIO] = None
        self._write_lock = threading.Lock()
        self._jobs: Dict[Any, "_Job"] = {}
        self._jobs_lock = threading.Lock()
        self._queue: "queue.Queue[Optional[_Job]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._batch_depth = 0
        self._desktop = desktop
        self.policy = policy
        self.audit = audit
        self._toolkit = toolkit
        if toolkit is not None:
            self._protect_launches(toolkit)
        self.name = name
        self.initialized = False
        self.protocol_version = LATEST_PROTOCOL_VERSION
        self.client_info: Dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def toolkit(self) -> DesktopToolkit:
        if self._toolkit is None:
            with self._lock:
                if self._toolkit is None:
                    desktop = self._desktop
                    if desktop is None:
                        from ..desktop import Desktop

                        desktop = Desktop()
                    self._toolkit = DesktopToolkit(desktop, self.policy, self.audit)
                    self._protect_launches(self._toolkit)
        return self._toolkit

    @staticmethod
    def _protect_launches(toolkit: Any) -> None:
        """Keep launched applications off the protocol stdin/stdout (explicit options win)."""
        kw = getattr(toolkit, "launch_kwargs", None)
        if isinstance(kw, dict):
            kw.setdefault("stdin", subprocess.DEVNULL)
            kw.setdefault("stdout", subprocess.DEVNULL)

    # --------------------------------------------------------------- dispatch
    @staticmethod
    def _error(id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": id, "error": err}

    @staticmethod
    def _result(id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": id, "result": result}

    def handle_message(self, message: Any) -> Optional[Dict[str, Any]]:
        """Handle one decoded JSON-RPC message; returns the response, or ``None`` for notifications."""
        if not isinstance(message, dict):
            return self._error(None, INVALID_REQUEST, "Invalid Request: expected a JSON object")
        is_notification = "id" not in message
        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            if is_notification:
                return None  # e.g. a stray response object from the client
            if "result" in message or "error" in message:
                return None  # responses to server-initiated requests (none are sent) are ignored
            return self._error(msg_id, INVALID_REQUEST, "Invalid Request: missing 'method'")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(msg_id, INVALID_PARAMS, "'params' must be an object")

        if is_notification:
            if method == "notifications/initialized":
                self.initialized = True
            elif method == "notifications/cancelled":
                self._cancel_request(params)
            return None

        try:
            if method == "initialize":
                return self._result(msg_id, self._initialize(params))
            if method == "ping":
                return self._result(msg_id, {})
            if method == "tools/list":
                return self._result(msg_id, {"tools": self.toolkit.to_mcp()})
            if method == "tools/call":
                return self._tools_call(msg_id, params)
            return self._error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        except FailSafeTriggered as exc:
            return self._result(msg_id, {"content": [{"type": "text", "text": f"Aborted by the fail-safe: {exc}"}], "isError": True})
        except Exception as exc:
            return self._error(msg_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}: {exc}")

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        requested = params.get("protocolVersion")
        self.protocol_version = requested if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        info = params.get("clientInfo")
        self.client_info = info if isinstance(info, dict) else {}
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": _version()},
            "instructions": _INSTRUCTIONS,
        }

    def _tools_call(self, msg_id: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return self._error(msg_id, INVALID_PARAMS, "tools/call requires a tool 'name'")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return self._error(msg_id, INVALID_PARAMS, "'arguments' must be an object")
        toolkit = self.toolkit
        if name not in toolkit.registry:
            return self._error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
        if self._worker is None:  # direct (synchronous) use: run here
            res = toolkit.call(name, args)
            return self._result(msg_id, {"content": res.to_mcp_content(), "isError": bool(res.is_error)})
        # serve(): run on the worker thread; the response is written when it finishes.
        meta = params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        job = _Job(msg_id, name, args, self._timeout_for(args), token, batch=self._batch_depth > 0)
        with self._jobs_lock:
            if _key(msg_id) in self._jobs:
                return self._error(msg_id, INVALID_REQUEST, f"Request id {msg_id!r} is already in progress")
            self._jobs[_key(msg_id)] = job
        self._queue.put(job)
        threading.Thread(target=self._monitor, args=(job,), name="locum-mcp-monitor", daemon=True).start()
        if job.batch:
            job.done.wait()
            return job.response
        return None

    # ------------------------------------------------------- call execution
    def _timeout_for(self, args: Dict[str, Any]) -> Optional[float]:
        t = self.call_timeout
        req = args.get("timeout")
        if t is not None and isinstance(req, (int, float)) and not isinstance(req, bool) and math.isfinite(req):
            t = max(t, float(req) + self.TIMEOUT_MARGIN)
        return t

    def _send(self, obj: Any) -> None:
        out = self._out
        if out is None:
            return
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                pass  # client gone: the read loop ends at EOF

    def _finish(self, job: "_Job", response: Optional[Dict[str, Any]]) -> None:
        """Answer ``job`` once (the worker, the watchdog or a cancel may race to it)."""
        with self._jobs_lock:
            if job.answered:
                return
            job.answered = True
            self._jobs.pop(_key(job.id), None)
        job.response = response
        if response is not None and not job.batch:
            self._send(response)
        job.done.set()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            if job.answered:
                continue  # cancelled or timed out while queued
            job.started = time.monotonic()
            try:
                with job.scope:
                    job.scope.check()
                    res = self.toolkit.call(job.name, job.args)
                response: Optional[Dict[str, Any]] = self._result(
                    job.id, {"content": res.to_mcp_content(), "isError": bool(res.is_error)})
            except CallCancelled:
                response = None if job.scope.cancelled else self._timeout_result(job)
            except FailSafeTriggered as exc:
                response = self._result(job.id, {"content": [{"type": "text", "text": f"Aborted by the fail-safe: {exc}"}], "isError": True})
            except Exception as exc:
                response = self._error(job.id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}: {exc}")
            self._finish(job, response)

    def _timeout_result(self, job: "_Job", queued: bool = False) -> Dict[str, Any]:
        if queued:
            text = (f"Timed out: {job.name} waited {job.timeout:g}s for an earlier call that is still running "
                    "and never started. The server is still serving; try again shortly.")
        else:
            text = (f"Timed out: {job.name} did not finish within {job.timeout:g}s and was stopped. The server "
                    "is still serving; try a narrower call (observe one window, a more specific selector).")
        return self._result(job.id, {"content": [{"type": "text", "text": text}], "isError": True})

    def _monitor(self, job: "_Job") -> None:
        """Progress heartbeats, and the hard deadline when the worker cannot stop by itself
        (stuck inside one native call) or the job is still queued behind such a call."""
        grace = 1.0  # let the worker's own (cooperative) timeout answer first
        while True:
            wait = self.progress_interval
            if job.timeout is not None:
                wait = min(wait, max(0.01, job.received + job.timeout + grace - time.monotonic()))
            if job.done.wait(wait):
                return
            now = time.monotonic()
            if job.timeout is not None and now >= job.received + job.timeout + grace:
                job.scope.cancel("timed out")
                self._finish(job, self._timeout_result(job, queued=job.started is None))
                return
            if job.token is not None and job.started is not None:
                elapsed = now - job.started
                params: Dict[str, Any] = {"progressToken": job.token, "progress": round(elapsed, 1),
                                          "message": f"{job.name} running for {elapsed:.0f}s"}
                if job.timeout is not None:
                    params["total"] = job.timeout
                self._send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})

    def _cancel_request(self, params: Dict[str, Any]) -> None:
        with self._jobs_lock:
            job = self._jobs.get(_key(params.get("requestId")))
        if job is None:
            return
        reason = params.get("reason")
        job.scope.cancel(str(reason) if reason else "cancelled by the client")
        if job.started is None:
            self._finish(job, None)  # never ran: nothing to stop, no response (per the protocol)

    def handle_line(self, line: str) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
        """Decode one line of input and handle it (JSON-RPC batches are supported)."""
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except ValueError as exc:
            return self._error(None, PARSE_ERROR, f"Parse error: {exc}")
        if isinstance(message, list):
            if not message:
                return self._error(None, INVALID_REQUEST, "Invalid Request: empty batch")
            self._batch_depth += 1  # a batch answers as one array: its tool calls are awaited
            try:
                responses = [r for r in (self.handle_message(m) for m in message) if r is not None]
            finally:
                self._batch_depth -= 1
            return responses or None
        return self.handle_message(message)

    def serve(self, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> None:
        """Read newline-delimited JSON-RPC from ``stdin`` and answer on ``stdout`` until EOF.

        When ``stdout`` is omitted (or is ``sys.stdout``) the process's stdout carries the protocol;
        for the duration of the call everything else that would write there
        (``print``, native code, inherited child descriptors) is sent to stderr.
        The process's own stdin/stdout are read and written as UTF-8 (the MCP
        stdio encoding) whatever the locale -- on Windows a piped ``sys.stdin``
        would otherwise decode with the ANSI code page and garble every
        non-ASCII argument (``Müller``, ``€``).
        """
        detach: Optional[Callable[[], None]] = None
        if stdin is None or stdin is sys.stdin:
            inp, detach = _utf8_stream(sys.stdin, "r")
        else:
            inp = stdin
        restore: Optional[Callable[[], None]] = None
        if stdout is not None and stdout is not sys.stdout:
            out: TextIO = stdout
        else:
            out, restore = _claim_stdout()
        self._out = out
        self._worker = threading.Thread(target=self._work, name="locum-mcp-worker", daemon=True)
        self._worker.start()
        try:
            for line in iter(inp.readline, ""):
                response = self.handle_line(line)
                if response is not None:
                    self._send(response)
        finally:
            # End of input: answer the calls already received (a client may pipe its
            # requests and close stdin), each within its own time limit, then stop.
            with self._jobs_lock:
                pending = list(self._jobs.values())
            self._queue.put(None)
            limits = [j.received + j.timeout + 2.0 for j in pending if j.timeout is not None]
            if pending and len(limits) < len(pending):
                self._worker.join()  # a call without a time limit: wait for it, as before
            else:
                self._worker.join(timeout=max([1.0] + [t - time.monotonic() for t in limits]))
            for job in pending:
                job.done.wait(timeout=max(0.0, job.received + (job.timeout or 0) + 2.0 - time.monotonic()))
            self._worker = None
            self._out = None
            if restore is not None:
                restore()
            if detach is not None:
                detach()


def _key(request_id: Any) -> Any:
    """Hashable key for a JSON-RPC id (ids are strings or numbers; anything else by repr)."""
    try:
        hash(request_id)
        return request_id
    except TypeError:
        return repr(request_id)


class _Job:
    """One ``tools/call`` running (or queued) on the server's worker thread."""

    def __init__(self, msg_id: Any, name: str, args: Dict[str, Any], timeout: Optional[float],
                 token: Any, *, batch: bool = False) -> None:
        self.id = msg_id
        self.name = name
        self.args = args
        self.timeout = timeout
        self.token = token
        self.batch = batch
        self.received = time.monotonic()
        self.started: Optional[float] = None
        self.scope = CancelScope(timeout)  # counts from receipt: queueing time is included
        self.done = threading.Event()
        self.answered = False
        self.response: Optional[Dict[str, Any]] = None


def _utf8_stream(stream: Any, mode: str) -> Tuple[Any, Optional[Callable[[], None]]]:
    """A UTF-8 text view of a process stream (``mode`` ``"r"``/``"w"``) and a function that releases it.

    Streams that are already UTF-8, or have no binary ``buffer`` (``StringIO``,
    captured streams), are returned unchanged. The wrapper is detached afterwards,
    so the underlying stream stays open.
    """
    encoding = str(getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
    buffer = getattr(stream, "buffer", None)
    if buffer is None or encoding in ("utf8", "utf8sig"):
        return stream, None
    try:
        if mode == "w":
            stream.flush()
            wrapper = io.TextIOWrapper(buffer, encoding="utf-8", errors="replace", newline="\n", write_through=True)
        else:
            wrapper = io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")
    except Exception:
        return stream, None

    def release() -> None:
        try:
            if mode == "w":
                wrapper.flush()
            wrapper.detach()
        except Exception:
            pass

    return wrapper, release


def _claim_stdout() -> Tuple[TextIO, Callable[[], None]]:
    """Reserve the real stdout for protocol frames.

    Returns ``(protocol_stream, restore)``. ``sys.stdout`` is pointed at
    ``sys.stderr`` and, when real descriptors are available, fd 1 is duplicated
    for the protocol and then replaced by stderr's descriptor, so child processes
    and C libraries writing to fd 1 end up on stderr too.
    """
    real = sys.stdout
    try:
        real.flush()
    except Exception:
        pass
    try:
        fd = real.fileno()
        err_fd = sys.stderr.fileno()
        proto_fd = os.dup(fd)
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        # No real descriptors (embedded interpreter, captured streams): Python-level only.
        return _claim_stdout_py(real)
    try:
        os.dup2(err_fd, fd)
    except OSError:
        os.close(proto_fd)
        return _claim_stdout_py(real)
    proto = io.TextIOWrapper(io.FileIO(proto_fd, "wb", closefd=True), encoding="utf-8", newline="\n", write_through=True)
    sys.stdout = sys.stderr

    def restore() -> None:
        try:
            proto.flush()
        except Exception:
            pass
        try:
            os.dup2(proto_fd, fd)
        finally:
            try:
                proto.close()
            except Exception:
                pass
            sys.stdout = real

    return proto, restore


def _claim_stdout_py(real: TextIO) -> Tuple[TextIO, Callable[[], None]]:
    """Fallback of :func:`_claim_stdout` without descriptors: redirect ``sys.stdout`` only.

    The protocol stream is a UTF-8 view of ``real`` (a cp1252 console stream
    could not even encode ``€``).
    """
    proto, release = _utf8_stream(real, "w")
    sys.stdout = sys.stderr

    def restore() -> None:
        if release is not None:
            release()
        sys.stdout = real

    return proto, restore


def serve(
    desktop: Any = None,
    policy: Optional[Policy] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    *,
    audit: Optional[AuditLog] = None,
) -> None:
    """Run an MCP server over stdio (blocks until stdin closes)."""
    MCPServer(desktop, policy, audit).serve(stdin, stdout)


_default_server: Optional[MCPServer] = None


def handle_message(message: Any, server: Optional[MCPServer] = None) -> Optional[Dict[str, Any]]:
    """Module-level convenience: handle ``message`` with ``server`` (default: a lazily created one)."""
    global _default_server
    if server is None:
        if _default_server is None:
            _default_server = MCPServer()
        server = _default_server
    return server.handle_message(message)


__all__ = [
    "LATEST_PROTOCOL_VERSION",
    "MCPServer",
    "METHOD_NOT_FOUND",
    "handle_message",
    "serve",
]
