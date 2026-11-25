# -*- coding: utf-8 -*-
"""Deterministic no-model A2A peer for the GIT-1 T2 acceptance run.

The peer is an independent process. It accepts A2A JSON-RPC, performs the
requested task, and records action counts. It does not call a model.
"""

# Future
from __future__ import annotations

# Standard
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
import uuid

_LOCK = threading.Lock()
_TASKS: dict[str, dict] = {}
_STATS = {
    "http_posts": 0,
    "unauthorized": 0,
    "business_actions": 0,
    "send_message": 0,
    "get_task": 0,
    "list_tasks": 0,
    "cancel_requests": 0,
    "cancel_applied": 0,
    "cancel_rejected": 0,
    "subscribe_requests": 0,
    "duplicate_message_ids": 0,
    "timeouts_started": 0,
    "timeouts_finished_after_client_may_have_given_up": 0,
    "jsonrpc_errors": 0,
    "direct_calls": 0,
    "received_methods": [],
    "seen_message_ids": {},
}
_CONFIG = {
    "name": "peer",
    "protocol": "1.0",
    "cancel": "supported",
    "secret": "",
    "slow_seconds": 8,
    "timeout_seconds": 12,
}


def _text_from_message(message: dict) -> str:
    """Return the first text part of an A2A message."""
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    return ""


def _state(name: str) -> str:
    """Return a protocol-shaped task state name."""
    if _CONFIG["protocol"].startswith("1"):
        return {
            "working": "TASK_STATE_WORKING",
            "completed": "TASK_STATE_COMPLETED",
            "canceled": "TASK_STATE_CANCELED",
            "failed": "TASK_STATE_FAILED",
        }[name]
    return name


def _artifact(text: str) -> dict:
    """Return one artifact in the peer's protocol shape."""
    if _CONFIG["protocol"].startswith("1"):
        return {"artifactId": uuid.uuid4().hex, "name": "result.txt", "parts": [{"text": text}]}
    return {"artifactId": uuid.uuid4().hex, "name": "result.txt", "kind": "artifact", "parts": [{"kind": "text", "text": text}]}


def _task_wire(task: dict) -> dict:
    """Return the task object this peer exposes."""
    wire = {
        "id": task["id"],
        "contextId": task["contextId"],
        "status": {"state": task["state"]},
        "metadata": {
            "peer": _CONFIG["name"],
            "protocol": _CONFIG["protocol"],
            "backendAction": task["action"],
        },
    }
    if task.get("artifact_text"):
        wire["artifacts"] = [_artifact(task["artifact_text"])]
        wire["status"]["message"] = {
            "role": "ROLE_AGENT" if _CONFIG["protocol"].startswith("1") else "agent",
            "parts": [{"text": task["artifact_text"]}],
        }
    return wire


def _finish_later(task_id: str, delay: float, final_state: str, artifact_text: str, count_timeout: bool) -> None:
    """Complete a task after a delay unless it was canceled."""

    def _run() -> None:
        time.sleep(delay)
        with _LOCK:
            task = _TASKS.get(task_id)
            if task is None or task.get("stop"):
                return
            if task["state"] in (_state("canceled"), _state("completed"), _state("failed")):
                return
            task["state"] = _state(final_state)
            task["artifact_text"] = artifact_text
            if count_timeout:
                _STATS["timeouts_finished_after_client_may_have_given_up"] += 1

    threading.Thread(target=_run, name=f"task-{task_id[:8]}", daemon=True).start()


def _handle_rpc(body: dict, direct: bool) -> tuple[int, dict]:
    """Handle one JSON-RPC call and return the HTTP status plus body."""
    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    with _LOCK:
        _STATS["http_posts"] += 1
        if direct:
            _STATS["direct_calls"] += 1
        if isinstance(method, str) and method not in _STATS["received_methods"]:
            _STATS["received_methods"].append(method)
        elif isinstance(method, str):
            _STATS["received_methods"].append(method)

    def _ok(result: dict) -> tuple[int, dict]:
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _err(code: int, message: str, http_status: int = 200) -> tuple[int, dict]:
        return http_status, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _err(-32600, "Invalid Request", 400)

    send_methods = {"SendMessage", "message/send"}
    get_methods = {"GetTask", "tasks/get"}
    list_methods = {"ListTasks", "tasks/list"}
    cancel_methods = {"CancelTask", "tasks/cancel"}
    subscribe_methods = {"SubscribeToTask", "tasks/resubscribe", "message/stream", "SendStreamingMessage"}

    if method in subscribe_methods:
        with _LOCK:
            _STATS["subscribe_requests"] += 1
        return _err(-32601, "SubscribeToTask is not implemented by this synthetic peer")

    if method in get_methods:
        task_id = params.get("id") or params.get("taskId") or params.get("task_id")
        with _LOCK:
            _STATS["get_task"] += 1
            task = _TASKS.get(str(task_id))
        if task is None:
            return _err(-32001, "Task not found")
        return _ok(_task_wire(task))

    if method in list_methods:
        with _LOCK:
            _STATS["list_tasks"] += 1
            tasks = [_task_wire(task) for task in _TASKS.values()]
        return _ok({"tasks": tasks})

    if method in cancel_methods:
        task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
        with _LOCK:
            _STATS["cancel_requests"] += 1
            task = _TASKS.get(task_id)
            if _CONFIG["cancel"] != "supported":
                _STATS["cancel_rejected"] += 1
                rejected = task is not None
            else:
                rejected = False
        if _CONFIG["cancel"] != "supported":
            if rejected:
                return _err(-32004, "Cancel is not supported by this backend; the task keeps running")
            return _err(-32004, "Cancel is not supported by this backend")
        if task is None:
            return _err(-32001, "Task not found")
        with _LOCK:
            if task["state"] in (_state("completed"), _state("failed"), _state("canceled")):
                return _ok(_task_wire(task))
            task["stop"] = True
            task["state"] = _state("canceled")
            _STATS["cancel_applied"] += 1
            wire = _task_wire(task)
        return _ok(wire)

    if method not in send_methods:
        return _err(-32601, f"Method not found: {method}")

    message = params.get("message") if isinstance(params.get("message"), dict) else {}
    message_id = str(message.get("messageId") or "")
    text = _text_from_message(message)
    task_id = uuid.uuid4().hex
    context_id = str(params.get("contextId") or uuid.uuid4().hex)
    with _LOCK:
        _STATS["send_message"] += 1
        _STATS["business_actions"] += 1
        if message_id:
            seen = _STATS["seen_message_ids"].get(message_id, 0) + 1
            _STATS["seen_message_ids"][message_id] = seen
            if seen > 1:
                _STATS["duplicate_message_ids"] += 1
        task = {
            "id": task_id,
            "contextId": context_id,
            "state": _state("working"),
            "action": _STATS["business_actions"],
            "text": text,
            "messageId": message_id,
            "artifact_text": "",
            "stop": False,
            "received_method": method,
        }
        _TASKS[task_id] = task

    if text == "JSONRPC-ERROR":
        with _LOCK:
            _TASKS.pop(task_id, None)
            _STATS["jsonrpc_errors"] += 1
            _STATS["business_actions"] -= 1
        return _err(-32000, "synthetic upstream failure")

    if text == "TIMEOUT":
        with _LOCK:
            _STATS["timeouts_started"] += 1
        time.sleep(float(_CONFIG["timeout_seconds"]))
        with _LOCK:
            current = _TASKS.get(task_id)
            if current and not current.get("stop"):
                current["state"] = _state("completed")
                current["artifact_text"] = "GIT1-TIMEOUT-FINISHED"
                _STATS["timeouts_finished_after_client_may_have_given_up"] += 1
                return _ok(_task_wire(current))
        return _err(-32002, "timeout task disappeared")

    if text == "SLOW-CANCEL":
        _finish_later(task_id, float(_CONFIG["slow_seconds"]), "completed", "GIT1-SLOW-FINISHED", False)
        with _LOCK:
            return _ok(_task_wire(_TASKS[task_id]))

    artifact = text if text else "GIT1-EMPTY"
    with _LOCK:
        task["state"] = _state("completed")
        task["artifact_text"] = artifact
        return _ok(_task_wire(task))


class _Handler(BaseHTTPRequestHandler):
    """Loopback HTTP handler for one synthetic peer."""

    server_version = "git1-synthetic-a2a/1.0"

    def log_message(self, fmt: str, *args) -> None:
        """Write a secret-free access line."""
        method = args[0] if args else ""
        print(f"{_CONFIG['name']} {method}", flush=True)

    def _authorized(self) -> bool:
        expected = _CONFIG["secret"]
        presented = self.headers.get("Authorization", "")
        return presented == f"Bearer {expected}"

    def _write(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        """Serve health and secret-free stats."""
        if self.path.startswith("/health"):
            self._write(200, {"ok": True, "peer": _CONFIG["name"], "protocol": _CONFIG["protocol"], "cancel": _CONFIG["cancel"]})
            return
        if self.path.startswith("/git1/stats"):
            if not self._authorized():
                with _LOCK:
                    _STATS["unauthorized"] += 1
                self._write(401, {"error": "unauthorized"})
                return
            with _LOCK:
                stats = {
                    "peer": _CONFIG["name"],
                    "protocol": _CONFIG["protocol"],
                    "cancel": _CONFIG["cancel"],
                    "counts": {key: value for key, value in _STATS.items() if key != "seen_message_ids"},
                    "duplicate_message_id_histogram": dict(_STATS["seen_message_ids"]),
                    "tasks": [_task_wire(task) for task in _TASKS.values()],
                }
            self._write(200, stats)
            return
        self._write(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        """Accept an A2A JSON-RPC request."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not self._authorized():
            with _LOCK:
                _STATS["unauthorized"] += 1
            self._write(401, {"error": "unauthorized"})
            return
        try:
            body = json.loads(raw.decode())
        except json.JSONDecodeError:
            self._write(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})
            return
        direct = self.headers.get("X-Git1-Direct") == "1"
        status, payload = _handle_rpc(body, direct)
        self._write(status, payload)


def main() -> None:
    """Start the peer on a loopback port."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--protocol", choices=["0.3", "1.0"], required=True)
    parser.add_argument("--cancel", choices=["supported", "unsupported"], required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--slow-seconds", type=float, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=12)
    args = parser.parse_args()
    _CONFIG["name"] = args.name
    _CONFIG["protocol"] = args.protocol
    _CONFIG["cancel"] = args.cancel
    _CONFIG["slow_seconds"] = args.slow_seconds
    _CONFIG["timeout_seconds"] = args.timeout_seconds
    with open(args.secret_file, encoding="utf-8") as handle:
        _CONFIG["secret"] = handle.read().strip()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"listening {args.name} 127.0.0.1:{args.port} protocol={args.protocol} cancel={args.cancel}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
