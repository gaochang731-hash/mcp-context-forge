# -*- coding: utf-8 -*-
"""Isolated loopback acceptance run for ContextForge v1.1.1 A2A.

The script starts two synthetic peers and one gateway on 127.0.0.1.
It writes a redacted JSON report. It does not print raw secrets.
"""

# Future
from __future__ import annotations

# Standard
import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path("/workspace")
RUN_DIR = Path("/tmp/git1-t2")
EVIDENCE = ROOT / "evidence" / "git1-t2"
PYTHON = ROOT / ".venv" / "bin" / "python"
TZ = ZoneInfo("Asia/Singapore")
GATEWAY = "http://127.0.0.1:4444"
SECRETS: dict[str, str] = {}
RESULTS: dict = {"events": [], "cases": [], "exchanges": []}
PROCS: list[subprocess.Popen] = []


def now_sg() -> str:
    """Return the current time in Asia/Singapore."""
    return datetime.now(TZ).isoformat(timespec="seconds")


def redact(value):
    """Replace known secret strings and bearer values."""
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _sensitive_key(str(key)) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = value
        for secret in SECRETS.values():
            if secret and secret in text:
                text = text.replace(secret, "[REDACTED]")
        if text.lower().startswith("bearer "):
            return "Bearer [REDACTED]"
        return text
    return value


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"access_token", "password", "auth_token", "authorization", "jwt", "secret", "token"} or lowered.endswith("_secret")


def event(title: str, **details) -> None:
    """Record one timeline event."""
    RESULTS["events"].append({"at": now_sg(), "name": title, **redact(details)})
    print(f"{now_sg()} {title}", flush=True)


def case(title: str, verdict: str, **details) -> None:
    """Record one acceptance case."""
    http_status = details.pop("status", None)
    item = {"at": now_sg(), "name": title, "verdict": verdict}
    if http_status is not None:
        item["http_status"] = http_status
    item.update(redact(details))
    RESULTS["cases"].append(item)
    print(f"CASE {verdict} {title}", flush=True)


def save() -> None:
    """Write the redacted report to the repository evidence directory."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = redact(RESULTS)
    (EVIDENCE / "results.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def http(method: str, url: str, token: str | None = None, body=None, form: dict | None = None, timeout: float = 30, headers: dict | None = None, record: bool = True):
    """Send one HTTP request and return status, payload, and headers."""
    data = None
    req_headers = dict(headers or {})
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        boundary = "----git1" + uuid.uuid4().hex
        chunks: list[bytes] = []
        for key, item in form.items():
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{item}\r\n".encode())
        chunks.append(f"--{boundary}--\r\n".encode())
        data = b"".join(chunks)
        req_headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            resp_headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        resp_headers = dict(exc.headers)
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 3)
        result = {"status": 0, "error": type(exc).__name__, "detail": str(exc), "elapsed_s": elapsed, "json": None, "text": ""}
        if record:
            RESULTS["exchanges"].append(redact({"method": method, "url": url, "body": body, "form": form, **result}))
        return result
    text = raw.decode("utf-8", errors="replace")
    parsed = None
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    elapsed = round(time.monotonic() - started, 3)
    result = {"status": status, "json": parsed, "text": text[:4000], "elapsed_s": elapsed, "headers": resp_headers}
    if record:
        RESULTS["exchanges"].append(redact({"method": method, "url": url, "body": body, "form": form, "status": status, "json": parsed, "text": text[:1500], "elapsed_s": elapsed}))
    return result


def claims_of(token: str) -> dict:
    """Return non-secret JWT claims."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
    except Exception as exc:
        return {"decode": type(exc).__name__}
    keep = ("token_use", "teams", "scopes", "is_admin", "iss", "aud", "auth_provider", "sub_present")
    out = {key: payload.get(key) for key in ("token_use", "teams", "scopes", "is_admin", "iss", "aud", "auth_provider")}
    out["sub_present"] = bool(payload.get("sub"))
    out["jti_present"] = bool(payload.get("jti"))
    out["email_claim"] = payload.get("email")
    out["_kept"] = list(keep)
    return out


def spawn(name: str, args: list[str], env: dict | None = None, log_name: str | None = None) -> subprocess.Popen:
    """Start a child process and keep its log."""
    log_path = RUN_DIR / (log_name or f"{name}.log")
    handle = open(log_path, "ab")
    proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, env=env, cwd=str(RUN_DIR))
    PROCS.append(proc)
    event("spawn", name=name, pid=proc.pid, log=str(log_path))
    return proc


def wait_http(url: str, timeout: float = 60) -> bool:
    """Wait until a URL answers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = http("GET", url, timeout=2, record=False)
        if result["status"] and result["status"] < 500:
            return True
        time.sleep(0.4)
    return False


def peer_stats(port: int, secret: str) -> dict:
    """Read secret-free stats from a synthetic peer."""
    result = http("GET", f"http://127.0.0.1:{port}/git1/stats", token=secret, timeout=5)
    return result.get("json") or {"status": result["status"], "error": result.get("error") or result.get("text")}


def rss_kb(pid: int) -> int | None:
    """Return VmRSS in KiB for a process."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def main() -> None:
    """Run the acceptance sequence."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(RUN_DIR, 0o700)
    started = time.monotonic()
    RESULTS["started_at"] = now_sg()
    RESULTS["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    RESULTS["branch"] = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()

    import secrets as pysecrets

    def pw(length: int) -> str:
        return "Aa1!" + pysecrets.token_urlsafe(length)

    SECRETS["admin_password"] = pw(24)
    SECRETS["user_a_password"] = pw(16)
    SECRETS["user_b_password"] = pw(16)
    SECRETS["default_user_password"] = pw(18)
    SECRETS["basic_auth_password"] = pw(18)
    SECRETS["jwt"] = pysecrets.token_urlsafe(48)
    SECRETS["enc"] = pysecrets.token_urlsafe(48)
    SECRETS["upstream_a"] = "git1-up-a-" + pysecrets.token_urlsafe(18)
    SECRETS["upstream_b"] = "git1-up-b-" + pysecrets.token_urlsafe(18)
    secret_file = RUN_DIR / "local-secrets.json"
    secret_file.write_text(json.dumps(SECRETS), encoding="utf-8")
    os.chmod(secret_file, 0o600)
    for key, filename in (("upstream_a", "upstream-a"), ("upstream_b", "upstream-b")):
        path = RUN_DIR / filename
        path.write_text(SECRETS[key], encoding="utf-8")
        os.chmod(path, 0o600)

    env = os.environ.copy()
    for key in list(env):
        if any(part in key.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "KEY", "COOKIE")):
            env.pop(key, None)
    env.update(
        {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": "C.UTF-8",
            "PYTHONPATH": str(ROOT),
            "VIRTUAL_ENV": str(ROOT / ".venv"),
            "HOST": "127.0.0.1",
            "PORT": "4444",
            "DATABASE_URL": f"sqlite:///{RUN_DIR / 'mcp.db'}",
            "JWT_SECRET_KEY": SECRETS["jwt"],
            "AUTH_ENCRYPTION_SECRET": SECRETS["enc"],
            "PLATFORM_ADMIN_EMAIL": "admin@example.com",
            "PLATFORM_ADMIN_PASSWORD": SECRETS["admin_password"],
            "DEFAULT_USER_PASSWORD": SECRETS["default_user_password"],
            "BASIC_AUTH_PASSWORD": SECRETS["basic_auth_password"],
            "ADMIN_REQUIRE_PASSWORD_CHANGE_ON_BOOTSTRAP": "false",
            "AUTH_REQUIRED": "true",
            "EMAIL_AUTH_ENABLED": "true",
            "PUBLIC_REGISTRATION_ENABLED": "false",
            "MCPGATEWAY_A2A_ENABLED": "true",
            "MCPGATEWAY_UI_ENABLED": "true",
            "MCPGATEWAY_ADMIN_API_ENABLED": "true",
            "CACHE_TYPE": "database",
            "SSRF_ALLOW_LOCALHOST": "true",
            "HTTPX_READ_TIMEOUT": "5",
            "HTTPX_CONNECT_TIMEOUT": "3",
            "LOG_LEVEL": "INFO",
            "OBSERVABILITY_ENABLED": "false",
            "PLUGINS_ENABLED": "false",
        }
    )
    RESULTS["gateway_env_flags"] = {
        "HOST": "127.0.0.1",
        "PORT": "4444",
        "SSRF_ALLOW_LOCALHOST": "true",
        "HTTPX_READ_TIMEOUT": "5",
        "MCPGATEWAY_UI_ENABLED": "true",
        "MCPGATEWAY_ADMIN_API_ENABLED": "true",
        "CACHE_TYPE": "database",
        "OBSERVABILITY_ENABLED": "false",
        "note": "SSRF_ALLOW_LOCALHOST is a local-test exception. The production default is false.",
    }

    peer = str(ROOT / "tests" / "acceptance" / "git1_t2" / "synthetic_peer.py")
    spawn("peer-a", [str(PYTHON), peer, "--name", "company-a", "--port", "9101", "--protocol", "1.0", "--cancel", "supported", "--secret-file", str(RUN_DIR / "upstream-a"), "--slow-seconds", "8", "--timeout-seconds", "12"])
    spawn("peer-b", [str(PYTHON), peer, "--name", "company-b", "--port", "9102", "--protocol", "0.3", "--cancel", "unsupported", "--secret-file", str(RUN_DIR / "upstream-b"), "--slow-seconds", "8", "--timeout-seconds", "12"])
    peers_up = wait_http("http://127.0.0.1:9101/health") and wait_http("http://127.0.0.1:9102/health")
    case("synthetic-peers-listen", "PASS" if peers_up else "FAIL", health_a=http("GET", "http://127.0.0.1:9101/health", record=False).get("json"), health_b=http("GET", "http://127.0.0.1:9102/health", record=False).get("json"))

    ssrf = subprocess.run(
        [str(PYTHON), "-c", "from mcpgateway.common.validators import SecurityValidator\ntry:\n    SecurityValidator.validate_url('http://127.0.0.1:9101/', 'A2A agent URL')\n    print('ALLOWED')\nexcept Exception as exc:\n    print(type(exc).__name__ + ': ' + str(exc)[:300])"],
        cwd=str(ROOT),
        env={**env, "SSRF_ALLOW_LOCALHOST": "false", "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    case("production-default-ssrf-blocks-loopback", "PASS" if "ALLOWED" not in ssrf.stdout else "FAIL", stdout=ssrf.stdout[-500:], stderr=ssrf.stderr[-500:])

    boot = time.monotonic()
    gateway = spawn("gateway", [str(PYTHON), "-m", "mcpgateway", "--host", "127.0.0.1", "--port", "4444"], env=env, log_name="gateway.log")
    gateway_up = False
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if gateway.poll() is not None:
            break
        probe = http("GET", GATEWAY + "/health", timeout=2, record=False)
        if probe["status"] and probe["status"] < 500:
            gateway_up = True
            break
        time.sleep(0.4)
    case("gateway-health", "PASS" if gateway_up else "FAIL", elapsed_s=round(time.monotonic() - boot, 3), health=http("GET", GATEWAY + "/health", record=False))
    if not gateway_up:
        log = (RUN_DIR / "gateway.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        case("gateway-log-tail", "FAIL", log=log)
        save()
        return

    version = http("GET", GATEWAY + "/health")
    case("health-body", "INFO", body=version.get("json") or version.get("text", "")[:500])

    admin_login = http("POST", GATEWAY + "/auth/email/login", body={"email": "admin@example.com", "password": SECRETS["admin_password"]})
    admin_token = (admin_login.get("json") or {}).get("access_token")
    case("admin-login", "PASS" if admin_token else "FAIL", status=admin_login["status"], claims=claims_of(admin_token) if admin_token else None, body_keys=list((admin_login.get("json") or {}).keys()))
    if not admin_token:
        save()
        return
    SECRETS["admin_token"] = admin_token

    def admin(method, path, body=None, form=None, timeout=30):
        return http(method, GATEWAY + path, token=admin_token, body=body, form=form, timeout=timeout)

    version_auth = admin("GET", "/version")
    case("version", "INFO", status=version_auth["status"], body=version_auth.get("json") or version_auth.get("text", "")[:800])

    users = {}
    for email, full, password_key in (
        ("user-a@company-a.example", "Company A Operator", "user_a_password"),
        ("user-b@company-b.example", "Company B Operator", "user_b_password"),
    ):
        created = admin("POST", "/auth/email/admin/users", {"email": email, "password": SECRETS[password_key], "full_name": full, "is_admin": False, "password_change_required": False})
        login = http("POST", GATEWAY + "/auth/email/login", body={"email": email, "password": SECRETS[password_key]})
        token = (login.get("json") or {}).get("access_token")
        roles = admin("GET", f"/rbac/users/{email}/roles")
        perms = admin("GET", f"/rbac/permissions/user/{email}")
        users[email] = {"token": token, "login_status": login["status"]}
        if token:
            SECRETS[email] = token
        case(
            f"default-stack-{email}",
            "PASS" if token else "FAIL",
            create_status=created["status"],
            create_body=created.get("json"),
            claims=claims_of(token) if token else None,
            roles=roles.get("json"),
            permissions=perms.get("json"),
        )

    teams = {}
    for slug, name, email in (("company-a", "Company A", "user-a@company-a.example"), ("company-b", "Company B", "user-b@company-b.example")):
        created = admin("POST", "/teams/", {"name": name, "slug": slug, "visibility": "private", "members": [{"email": email, "role": "member"}]})
        teams[slug] = created.get("json")
        case(f"create-{slug}", "PASS" if created["status"] in (200, 201) else "FAIL", status=created["status"], body=created.get("json") or created.get("text"))

    def team_id(slug: str) -> str | None:
        body = teams.get(slug) or {}
        if isinstance(body, dict):
            for key in ("id",):
                if body.get(key):
                    return body[key]
            team = body.get("team") or body.get("data") or {}
            if isinstance(team, dict) and team.get("id"):
                return team["id"]
        return None

    team_a = team_id("company-a")
    team_b = team_id("company-b")
    case("team-ids", "PASS" if team_a and team_b else "FAIL", team_a=team_a, team_b=team_b, raw_keys={key: list((value or {}).keys()) if isinstance(value, dict) else type(value).__name__ for key, value in teams.items()})

    role = admin("POST", "/rbac/roles", {"name": "git1_a2a_invoke", "description": "Least privilege A2A read and invoke", "scope": "team", "permissions": ["a2a.read", "a2a.invoke"]})
    role_id = (role.get("json") or {}).get("id")
    case("custom-least-privilege-role", "PASS" if role["status"] in (200, 201) else "FAIL", status=role["status"], body=role.get("json") or role.get("text"))

    for email, team in (("user-a@company-a.example", team_a), ("user-b@company-b.example", team_b)):
        assigned = admin("POST", f"/rbac/users/{email}/roles", {"role_id": role_id, "scope": "team", "scope_id": team}) if role_id and team else {"status": 0, "json": None}
        roles = admin("GET", f"/rbac/users/{email}/roles")
        revoked = []
        for item in roles.get("json") or []:
            if not isinstance(item, dict):
                continue
            if item.get("role_name") == "team_admin" and item.get("scope") == "team":
                scope_id = item.get("scope_id") or ""
                revoked.append(admin("DELETE", f"/rbac/users/{email}/roles/{item.get('role_id')}?scope=team&scope_id={scope_id}"))
        perms = http("GET", GATEWAY + "/rbac/my/permissions", token=users[email]["token"])
        case(
            f"least-privilege-assigned-{email}",
            "INFO",
            assign_status=assigned.get("status"),
            assign_body=assigned.get("json") or assigned.get("text"),
            revoked=[item.get("status") for item in revoked],
            roles_after=(admin("GET", f"/rbac/users/{email}/roles").get("json")),
            my_permissions=perms.get("json"),
        )

    def user_call(email, method, path, body=None, timeout=30):
        return http(method, GATEWAY + path, token=users[email]["token"], body=body, timeout=timeout)

    tokens = {}
    for email, team, label, perms in (
        ("user-a@company-a.example", team_a, "a-invoke", ["a2a.read", "a2a.invoke"]),
        ("user-a@company-a.example", team_a, "a-read", ["a2a.read"]),
        ("user-b@company-b.example", team_b, "b-invoke", ["a2a.read", "a2a.invoke"]),
        ("user-b@company-b.example", team_b, "b-read", ["a2a.read"]),
    ):
        created = user_call(email, "POST", "/tokens", {"name": label, "expires_in_days": 1, "team_id": team, "scope": {"permissions": perms}})
        access = (created.get("json") or {}).get("access_token")
        if access:
            SECRETS[label] = access
            tokens[label] = access
        meta = created.get("json") or {}
        case(f"token-{label}", "PASS" if access else "FAIL", status=created["status"], warnings=meta.get("warnings"), token_meta=meta.get("token"), claims=claims_of(access) if access else None, error=None if access else meta or created.get("text"))

    def scoped(label, method, path, body=None, timeout=30, form=None):
        return http(method, GATEWAY + path, token=tokens.get(label), body=body, form=form, timeout=timeout)

    agents = {}
    for label, name, endpoint, protocol, secret_key, team, auth in (
        ("a", "company-a-agent", "http://127.0.0.1:9101/", "1.0", "upstream_a", team_a, SECRETS["upstream_a"]),
        ("b", "company-b-agent", "http://127.0.0.1:9102/", "0.3", "upstream_b", team_b, SECRETS["upstream_b"]),
    ):
        created = admin(
            "POST",
            "/a2a",
            {
                "agent": {
                    "name": name,
                    "description": f"Synthetic peer for fictional {label}",
                    "endpoint_url": endpoint,
                    "agent_type": "jsonrpc",
                    "protocol_version": protocol,
                    "auth_type": "bearer",
                    "auth_token": auth,
                    "capabilities": {"streaming": False},
                },
                "team_id": team,
                "visibility": "team",
            },
        )
        agents[label] = created.get("json") if isinstance(created.get("json"), dict) else {}
        case(f"register-agent-{label}", "PASS" if created["status"] in (200, 201) else "FAIL", status=created["status"], body=created.get("json") or created.get("text"))

    public = admin(
        "POST",
        "/a2a",
        {
            "agent": {
                "name": "public-synthetic",
                "description": "Platform-public synthetic peer on the company A process",
                "endpoint_url": "http://127.0.0.1:9101/",
                "agent_type": "jsonrpc",
                "protocol_version": "1.0",
                "auth_type": "bearer",
                "auth_token": SECRETS["upstream_a"],
                "capabilities": {"streaming": False},
            },
            "visibility": "public",
        },
    )
    case("register-public-agent", "PASS" if public["status"] in (200, 201) else "FAIL", status=public["status"], body=public.get("json") or public.get("text"))

    agent_a_id = (agents.get("a") or {}).get("id")
    agent_b_id = (agents.get("b") or {}).get("id")
    public_id = (public.get("json") or {}).get("id") if isinstance(public.get("json"), dict) else None

    def rpc(label, agent, method, params, timeout=30):
        return scoped(label, "POST", f"/a2a/{agent}/jsonrpc", {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}, timeout=timeout)

    def message(text, message_id=None, role="ROLE_USER"):
        return {"message": {"messageId": message_id or uuid.uuid4().hex, "role": role, "parts": [{"text": text}]}}

    unauth = http("POST", GATEWAY + "/a2a/company-a-agent/jsonrpc", body={"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": message("GIT1-TEST-001")})
    case("unauthenticated-jsonrpc", "PASS" if unauth["status"] in (401, 403) else "FAIL", status=unauth["status"], body=unauth.get("json") or unauth.get("text"))

    send = rpc("a-invoke", "company-a-agent", "SendMessage", message("GIT1-TEST-001"))
    task_a = (((send.get("json") or {}).get("result") or {}).get("id")) if isinstance(send.get("json"), dict) else None
    case("sendmessage-1.0", "PASS" if send["status"] == 200 and task_a else "FAIL", status=send["status"], body=send.get("json") or send.get("text"), peer=peer_stats(9101, SECRETS["upstream_a"]))

    get_task = rpc("a-invoke", "company-a-agent", "GetTask", {"id": task_a}) if task_a else {"status": 0}
    list_tasks = rpc("a-invoke", "company-a-agent", "ListTasks", {})
    subscribe = rpc("a-invoke", "company-a-agent", "SubscribeToTask", {"id": task_a or "missing"})
    case("get-list-subscribe-1.0", "INFO", get_task=get_task.get("json") or get_task.get("text"), list_tasks=list_tasks.get("json"), subscribe=subscribe.get("json") or subscribe.get("text"), subscribe_status=subscribe.get("status"))

    legacy_names = rpc("b-invoke", "company-b-agent", "SendMessage", message("GIT1-TEST-001", role="user"))
    task_b = (((legacy_names.get("json") or {}).get("result") or {}).get("id")) if isinstance(legacy_names.get("json"), dict) else None
    case("sendmessage-rewritten-to-0.3", "PASS" if legacy_names["status"] == 200 and task_b else "FAIL", status=legacy_names["status"], body=legacy_names.get("json") or legacy_names.get("text"), peer=peer_stats(9102, SECRETS["upstream_b"]))
    get_b = rpc("b-invoke", "company-b-agent", "GetTask", {"id": task_b}) if task_b else {"status": 0}
    list_b = rpc("b-invoke", "company-b-agent", "ListTasks", {})
    sub_b = rpc("b-invoke", "company-b-agent", "SubscribeToTask", {"id": task_b or "missing"})
    case("get-list-subscribe-0.3", "INFO", get_task=get_b.get("json"), list_tasks=list_b.get("json"), subscribe=sub_b.get("json") or sub_b.get("text"))

    cross_method = rpc("a-invoke", "company-a-agent", "message/send", message("GIT1-FROM-LEGACY-METHOD"))
    case("legacy-method-on-v1-agent", "INFO", status=cross_method["status"], body=cross_method.get("json"), peer_methods=(peer_stats(9101, SECRETS["upstream_a"]).get("counts") or {}).get("received_methods"))

    slow = rpc("a-invoke", "company-a-agent", "SendMessage", message("SLOW-CANCEL"))
    slow_id = (((slow.get("json") or {}).get("result") or {}).get("id")) if isinstance(slow.get("json"), dict) else None
    cancel = rpc("a-invoke", "company-a-agent", "CancelTask", {"id": slow_id}) if slow_id else {"status": 0}
    time.sleep(1)
    after_cancel = rpc("a-invoke", "company-a-agent", "GetTask", {"id": slow_id}) if slow_id else {"status": 0}
    case("cancel-supported-backend", "INFO", send=slow.get("json"), cancel=cancel.get("json"), after=after_cancel.get("json"), peer=peer_stats(9101, SECRETS["upstream_a"]))

    slow_b = rpc("b-invoke", "company-b-agent", "SendMessage", message("SLOW-CANCEL"))
    slow_b_id = (((slow_b.get("json") or {}).get("result") or {}).get("id")) if isinstance(slow_b.get("json"), dict) else None
    cancel_b = rpc("b-invoke", "company-b-agent", "CancelTask", {"id": slow_b_id}) if slow_b_id else {"status": 0}
    case("cancel-unsupported-backend", "INFO", send=slow_b.get("json"), cancel=cancel_b.get("json"), cancel_status=cancel_b.get("status"), peer=peer_stats(9102, SECRETS["upstream_b"]))

    if agent_a_id and slow_id:
        before = peer_stats(9101, SECRETS["upstream_a"])
        local_cancel = scoped("a-invoke", "POST", f"/a2a/{agent_a_id}/tasks/{slow_id}/cancel")
        after = peer_stats(9101, SECRETS["upstream_a"])
        case(
            "gateway-rest-cancel-does-not-signal-peer",
            "INFO",
            status=local_cancel["status"],
            body=local_cancel.get("json") or local_cancel.get("text"),
            peer_cancel_before=(before.get("counts") or {}).get("cancel_requests"),
            peer_cancel_after=(after.get("counts") or {}).get("cancel_requests"),
        )

    dup_id = uuid.uuid4().hex
    dup1 = rpc("a-invoke", "company-a-agent", "SendMessage", message("GIT1-DUP", dup_id))
    dup2 = rpc("a-invoke", "company-a-agent", "SendMessage", message("GIT1-DUP", dup_id))
    case("duplicate-dispatch", "INFO", first=dup1.get("json"), second=dup2.get("json"), peer=peer_stats(9101, SECRETS["upstream_a"]))

    err = rpc("a-invoke", "company-a-agent", "SendMessage", message("JSONRPC-ERROR"))
    case("jsonrpc-error-http", "INFO", status=err["status"], body=err.get("json") or err.get("text"), peer=peer_stats(9101, SECRETS["upstream_a"]))

    timeout_call = rpc("b-invoke", "company-b-agent", "SendMessage", message("TIMEOUT"), timeout=20)
    case("upstream-timeout", "INFO", status=timeout_call["status"], body=timeout_call.get("json") or timeout_call.get("text") or timeout_call.get("error"), elapsed_s=timeout_call.get("elapsed_s"))

    # Isolation negatives.
    cross_list = scoped("b-invoke", "GET", "/a2a")
    cross_card = scoped("b-invoke", "GET", f"/a2a/{agent_a_id}/card") if agent_a_id else {"status": 0}
    cross_invoke = rpc("b-invoke", "company-a-agent", "GetTask", {"id": task_a or "guess"})
    cross_rest = scoped("b-invoke", "GET", f"/a2a/{agent_a_id}/tasks/{task_a}") if agent_a_id and task_a else {"status": 0}
    guessed = scoped("b-invoke", "GET", f"/a2a/{agent_a_id}/tasks/deadbeefdeadbeefdeadbeefdeadbeef") if agent_a_id else {"status": 0}
    read_only_invoke = rpc("a-read", "company-a-agent", "SendMessage", message("GIT1-SHOULD-NOT-RUN"))
    own_list = scoped("a-invoke", "GET", "/a2a")
    own_card = scoped("a-invoke", "GET", f"/a2a/{agent_a_id}/card") if agent_a_id else {"status": 0}
    public_by_b = rpc("b-invoke", "public-synthetic", "SendMessage", message("GIT1-PUBLIC"))
    case(
        "isolation-negatives",
        "INFO",
        b_list_status=cross_list["status"],
        b_list=cross_list.get("json"),
        b_card_status=cross_card.get("status"),
        b_card=cross_card.get("json") or cross_card.get("text"),
        b_invoke_status=cross_invoke.get("status"),
        b_invoke=cross_invoke.get("json") or cross_invoke.get("text"),
        b_rest_task_status=cross_rest.get("status"),
        b_rest_task=cross_rest.get("json") or cross_rest.get("text"),
        guessed_status=guessed.get("status"),
        guessed=guessed.get("json") or guessed.get("text"),
        read_only_invoke_status=read_only_invoke.get("status"),
        read_only_invoke=read_only_invoke.get("json") or read_only_invoke.get("text"),
        a_list=own_list.get("json"),
        a_card=own_card.get("json"),
        public_by_b_status=public_by_b.get("status"),
        public_by_b=public_by_b.get("json") or public_by_b.get("text"),
    )

    # GHSA owner modification: company B tries to take company A's agent.
    ghsa_put = scoped("b-invoke", "PUT", f"/a2a/{agent_a_id}", {"name": "stolen-by-b", "owner_email": "user-b@company-b.example", "team_id": team_b, "visibility": "team"}) if agent_a_id else {"status": 0}
    ghsa_admin = scoped(
        "b-invoke",
        "POST",
        f"/admin/a2a/{agent_a_id}/edit",
        form={"name": "stolen-by-admin-edit", "endpoint_url": "http://127.0.0.1:9102/", "agent_type": "jsonrpc", "visibility": "team", "team_id": team_b or "", "owner_email": "user-b@company-b.example"},
    ) if agent_a_id else {"status": 0}
    owner_after = admin("GET", f"/a2a/{agent_a_id}") if agent_a_id else {"status": 0}
    case("ghsa-cross-team-owner-update", "INFO", put_status=ghsa_put.get("status"), put=ghsa_put.get("json") or ghsa_put.get("text"), admin_edit_status=ghsa_admin.get("status"), admin_edit=ghsa_admin.get("json") or ghsa_admin.get("text"), owner_after=owner_after.get("json"))

    # Direct bypass using the card URL and the known upstream secret.
    direct_anon = http("POST", "http://127.0.0.1:9101/", body={"jsonrpc": "2.0", "id": "direct-anon", "method": "SendMessage", "params": message("GIT1-DIRECT-ANON")}, headers={"X-Git1-Direct": "1"})
    direct_secret = http(
        "POST",
        "http://127.0.0.1:9101/",
        token=SECRETS["upstream_a"],
        body={"jsonrpc": "2.0", "id": "direct-secret", "method": "SendMessage", "params": message("GIT1-DIRECT-SECRET")},
        headers={"X-Git1-Direct": "1"},
    )
    case("agent-card-direct-bypass", "INFO", anon_status=direct_anon["status"], anon=direct_anon.get("json"), with_upstream_status=direct_secret["status"], with_upstream=direct_secret.get("json"), peer=peer_stats(9101, SECRETS["upstream_a"]))

    # Delete registration is not a task stop.
    ephemeral = admin(
        "POST",
        "/a2a",
        {
            "agent": {
                "name": "company-a-ephemeral",
                "endpoint_url": "http://127.0.0.1:9101/",
                "agent_type": "jsonrpc",
                "protocol_version": "1.0",
                "auth_type": "bearer",
                "auth_token": SECRETS["upstream_a"],
            },
            "team_id": team_a,
            "visibility": "team",
        },
    )
    eph_id = (ephemeral.get("json") or {}).get("id") if isinstance(ephemeral.get("json"), dict) else None
    eph_send = rpc("a-invoke", "company-a-ephemeral", "SendMessage", message("SLOW-CANCEL"))
    eph_task = (((eph_send.get("json") or {}).get("result") or {}).get("id")) if isinstance(eph_send.get("json"), dict) else None
    deleted = admin("DELETE", f"/a2a/{eph_id}") if eph_id else {"status": 0}
    time.sleep(1)
    peer_after_delete = peer_stats(9101, SECRETS["upstream_a"])
    gone = rpc("a-invoke", "company-a-ephemeral", "GetTask", {"id": eph_task or "missing"})
    case("delete-registration-is-not-cancel", "INFO", register=ephemeral.get("status"), task=eph_task, delete_status=deleted.get("status"), peer_task_states=[(item.get("id"), (item.get("status") or {}).get("state")) for item in peer_after_delete.get("tasks") or []], gateway_after_delete=gone.get("json") or gone.get("text"), gateway_status=gone.get("status"))

    # Restart recovery.
    restart_send = rpc("a-invoke", "company-a-agent", "SendMessage", message("SLOW-CANCEL"))
    restart_task = (((restart_send.get("json") or {}).get("result") or {}).get("id")) if isinstance(restart_send.get("json"), dict) else None
    event("gateway-kill", pid=gateway.pid, task=restart_task)
    gateway.terminate()
    try:
        gateway.wait(timeout=15)
    except subprocess.TimeoutExpired:
        gateway.kill()
    time.sleep(1)
    during = peer_stats(9101, SECRETS["upstream_a"])
    boot2 = time.monotonic()
    gateway2 = spawn("gateway-restart", [str(PYTHON), "-m", "mcpgateway", "--host", "127.0.0.1", "--port", "4444"], env=env, log_name="gateway-restart.log")
    up2 = wait_http(GATEWAY + "/health", timeout=90)
    recovered = rpc("a-invoke", "company-a-agent", "GetTask", {"id": restart_task}) if up2 and restart_task else {"status": 0, "json": None}
    case("restart-recovery", "INFO", health=up2, boot_s=round(time.monotonic() - boot2, 3), peer_during_outage=during, get_after=recovered.get("json") or recovered.get("text"), status=recovered.get("status"))

    # Secrets at rest and in exports.
    db_rows = []
    try:
        conn = sqlite3.connect(RUN_DIR / "mcp.db")
        cur = conn.cursor()
        cur.execute("select name, auth_type, auth_value, endpoint_url from a2a_agents")
        for name, auth_type, auth_value, endpoint in cur.fetchall():
            raw = auth_value or ""
            db_rows.append(
                {
                    "name": name,
                    "auth_type": auth_type,
                    "endpoint_url": endpoint,
                    "auth_value_length": len(raw),
                    "contains_upstream_a": SECRETS["upstream_a"] in raw,
                    "contains_upstream_b": SECRETS["upstream_b"] in raw,
                    "prefix": raw[:12],
                }
            )
        cur.execute("select count(*) from audit_trails")
        audit_count = cur.fetchone()[0]
        cur.execute("select action, resource_type, user_email, team_id, resource_id, request_path from audit_trails order by timestamp desc limit 30")
        audit_rows = [{"action": a, "resource_type": b, "user_email": c, "team_id": d, "resource_id": e, "request_path": f} for a, b, c, d, e, f in cur.fetchall()]
        conn.close()
    except Exception as exc:
        audit_count = None
        audit_rows = []
        db_rows.append({"error": str(exc)})
    case("secret-storage-db", "INFO", rows=db_rows, audit_count=audit_count, audit_sample=audit_rows)

    log_text = ""
    for name in ("gateway.log", "gateway-restart.log", "peer-a.log", "peer-b.log"):
        path = RUN_DIR / name
        if path.exists():
            log_text += path.read_text(encoding="utf-8", errors="replace")
    case(
        "secret-in-logs",
        "FAIL" if (SECRETS["upstream_a"] in log_text or SECRETS["upstream_b"] in log_text or SECRETS["admin_password"] in log_text) else "PASS",
        upstream_a_present=SECRETS["upstream_a"] in log_text,
        upstream_b_present=SECRETS["upstream_b"] in log_text,
        admin_password_present=SECRETS["admin_password"] in log_text,
        jwt_secret_present=SECRETS["jwt"] in log_text,
        log_bytes=len(log_text),
    )
    excerpt_lines = [line for line in log_text.splitlines() if "a2a_call" in line or "A2A external" in line or "revocation" in line.lower()]
    (EVIDENCE).mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "log-excerpt.txt").write_text("\n".join(redact(line) for line in excerpt_lines[:80]), encoding="utf-8")

    # Fail-open revocation. Use a fresh session token, insert its jti, then hide the table.
    fresh_login = http("POST", GATEWAY + "/auth/email/login", body={"email": "user-a@company-a.example", "password": SECRETS["user_a_password"]})
    fresh = (fresh_login.get("json") or {}).get("access_token")
    if fresh:
        SECRETS["fresh"] = fresh
    jti = None
    if fresh:
        part = fresh.split(".")[1]
        part += "=" * (-len(part) % 4)
        jti = json.loads(base64.urlsafe_b64decode(part)).get("jti")
    before_revoke = http("GET", GATEWAY + "/a2a", token=fresh) if fresh else {"status": 0}
    inserted = None
    if jti:
        conn = sqlite3.connect(RUN_DIR / "mcp.db", timeout=10)
        try:
            conn.execute(
                "insert into token_revocations (jti, revoked_at, revoked_by, reason) values (?, datetime('now'), ?, ?)",
                (jti, "admin@example.com", "git1-fail-open-probe"),
            )
            conn.commit()
            inserted = True
        except Exception as exc:
            inserted = str(exc)
        conn.close()
    after_revoke = http("GET", GATEWAY + "/a2a", token=fresh) if fresh else {"status": 0}
    renamed = None
    conn = sqlite3.connect(RUN_DIR / "mcp.db", timeout=10)
    try:
        conn.execute("alter table token_revocations rename to token_revocations_hidden")
        conn.commit()
        renamed = True
    except Exception as exc:
        renamed = str(exc)
    conn.close()
    during_outage = http("GET", GATEWAY + "/a2a", token=fresh) if fresh else {"status": 0}
    api_during = scoped("a-invoke", "GET", "/a2a")
    # Restore the table so later inspection still works.
    conn = sqlite3.connect(RUN_DIR / "mcp.db", timeout=10)
    try:
        conn.execute("alter table token_revocations_hidden rename to token_revocations")
        conn.commit()
    except Exception as exc:
        event("restore-revocation-table-failed", error=str(exc))
    conn.close()
    restored = http("GET", GATEWAY + "/a2a", token=fresh) if fresh else {"status": 0}
    case(
        "revocation-storage-unavailable",
        "INFO",
        jti_present=bool(jti),
        inserted=inserted,
        before_status=before_revoke.get("status"),
        after_revoke_status=after_revoke.get("status"),
        after_revoke_body=after_revoke.get("json") or after_revoke.get("text"),
        renamed=renamed,
        during_outage_status=during_outage.get("status"),
        during_outage_body=during_outage.get("json") or during_outage.get("text"),
        active_api_token_during_outage_status=api_during.get("status"),
        restored_status=restored.get("status"),
        restored_body=restored.get("json") or restored.get("text"),
    )

    # API token revoke path, separate from the session-token table outage.
    revoke_target = user_call("user-a@company-a.example", "POST", "/tokens", {"name": "revoke-me", "expires_in_days": 1, "team_id": team_a, "scope": {"permissions": ["a2a.read"]}})
    revoke_token = (revoke_target.get("json") or {}).get("access_token")
    revoke_id = ((revoke_target.get("json") or {}).get("token") or {}).get("id")
    if revoke_token:
        SECRETS["revoke_me"] = revoke_token
    before_api = http("GET", GATEWAY + "/a2a", token=revoke_token) if revoke_token else {"status": 0}
    revoked = user_call("user-a@company-a.example", "DELETE", f"/tokens/{revoke_id}") if revoke_id else {"status": 0}
    after_api = http("GET", GATEWAY + "/a2a", token=revoke_token) if revoke_token else {"status": 0}
    case("api-token-revoke-when-storage-available", "INFO", before=before_api.get("status"), revoke_status=revoked.get("status"), after=after_api.get("status"), after_body=after_api.get("json") or after_api.get("text"))

    ui = http("GET", GATEWAY + "/admin", timeout=15)
    ui_login = http("GET", GATEWAY + "/admin/login", timeout=15)
    docs = http("GET", GATEWAY + "/docs", timeout=15)
    case("local-admin-ui", "INFO", admin_status=ui["status"], admin_head=ui.get("text", "")[:400], login_status=ui_login["status"], login_head=ui_login.get("text", "")[:400], docs_status=docs["status"])
    html = ui_login.get("text") or ui.get("text") or ""
    if html:
        (EVIDENCE / "admin-login-excerpt.html").write_text(redact(html[:2000]), encoding="utf-8")

    samples = []
    for proc, label in ((gateway2, "gateway"),):
        samples.append({"label": label, "pid": proc.pid, "rss_kib": rss_kb(proc.pid), "alive": proc.poll() is None})
    for proc in PROCS:
        if proc.pid and proc.poll() is None:
            samples.append({"pid": proc.pid, "rss_kib": rss_kb(proc.pid)})
    mem = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()[:5]
    RESULTS["resources"] = {
        "elapsed_s": round(time.monotonic() - started, 3),
        "finished_at": now_sg(),
        "rss_samples": samples,
        "meminfo_head": mem,
        "nproc": os.cpu_count(),
    }
    versions = subprocess.run([str(PYTHON), "-c", "import importlib.metadata as m, platform, sys\nmods=['fastapi','uvicorn','sqlalchemy','pydantic','alembic','cryptography','cpex','mcp']\nprint(json.dumps({'python':sys.version,'platform':platform.platform(),'packages':{n:m.version(n) for n in mods}}))"], text=True, capture_output=True, cwd=ROOT, env={**env, "PYTHONPATH": str(ROOT)})
    RESULTS["versions_raw"] = versions.stdout
    case("final-peer-counts", "INFO", peer_a=peer_stats(9101, SECRETS["upstream_a"]), peer_b=peer_stats(9102, SECRETS["upstream_b"]))
    save()
    event("done", elapsed_s=RESULTS["resources"]["elapsed_s"])


if __name__ == "__main__":
    try:
        main()
    finally:
        save()
        for proc in PROCS:
            if proc.poll() is None:
                proc.terminate()
        time.sleep(1)
        for proc in PROCS:
            if proc.poll() is None:
                proc.kill()
