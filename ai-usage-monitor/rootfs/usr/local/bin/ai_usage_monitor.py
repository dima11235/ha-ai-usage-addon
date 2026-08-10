#!/usr/bin/env python3
"""Home Assistant add-on for Claude and Codex subscription usage monitoring."""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DATA_DIR = Path(os.getenv("AI_USAGE_DATA_DIR", "/data"))
OPTIONS_PATH = Path(os.getenv("AI_USAGE_OPTIONS_PATH", str(DATA_DIR / "options.json")))
CLAUDE_PATH = DATA_DIR / "claude_credentials.json"
CODEX_PATH = DATA_DIR / "codex_auth.json"
STATE_PATH = DATA_DIR / "state.json"

HA_API = os.getenv("HA_API_URL", "http://supervisor/core/api").rstrip("/")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_DEFAULT_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]

CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/responses"

MAX_IMPORT_SIZE = 128 * 1024
REQUEST_TIMEOUT = 35


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def token_expiry(token: str) -> int | None:
    value = decode_jwt_payload(token).get("exp")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class HttpFailure(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        body: str = "",
        headers: dict[str, str] | None = None,
        url: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.url = url


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    read_body: bool = True,
) -> tuple[int, dict[str, str], Any]:
    request_headers = {"User-Agent": "ha-ai-usage-monitor/0.1.0", **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            response_headers = {key.lower(): value for key, value in response.headers.items()}
            raw = response.read() if read_body else b""
            parsed: Any = None
            if raw:
                parsed = json.loads(raw.decode("utf-8"))
            return response.status, response_headers, parsed
    except urllib.error.HTTPError as error:
        raw = error.read(16 * 1024)
        body = raw.decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in error.headers.items()}
        raise HttpFailure(error.code, f"HTTP {error.code} from {url}", body, response_headers, url) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Request to {url} failed: {error.reason}") from error


def validate_claude_credentials(value: dict[str, Any]) -> None:
    oauth = value.get("claudeAiOauth", value)
    if not isinstance(oauth, dict):
        raise ValueError("claudeAiOauth must be an object")
    if not isinstance(oauth.get("accessToken") or oauth.get("access_token"), str):
        raise ValueError("Claude access token is missing")
    if not isinstance(oauth.get("refreshToken") or oauth.get("refresh_token"), str):
        raise ValueError("Claude refresh token is missing")


def validate_codex_credentials(value: dict[str, Any]) -> None:
    tokens = value.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Codex tokens object is missing")
    for key in ("access_token", "refresh_token", "account_id"):
        if not isinstance(tokens.get(key), str) or not tokens[key]:
            raise ValueError(f"Codex {key} is missing")


def claude_oauth(value: dict[str, Any]) -> dict[str, Any]:
    oauth = value.get("claudeAiOauth", value)
    if not isinstance(oauth, dict):
        raise ValueError("Claude OAuth object is missing")
    return oauth


def claude_needs_refresh(oauth: dict[str, Any]) -> bool:
    now_ms = int(time.time() * 1000)
    access_expiry = oauth.get("expiresAt")
    refresh_expiry = oauth.get("refreshTokenExpiresAt")
    try:
        return int(access_expiry) <= now_ms + 5 * 60 * 1000
    except (TypeError, ValueError):
        pass
    try:
        if int(refresh_expiry) <= now_ms + 14 * 24 * 60 * 60 * 1000:
            return True
    except (TypeError, ValueError):
        pass
    return False


def refresh_claude(credentials: dict[str, Any]) -> str:
    oauth = claude_oauth(credentials)
    refresh_token = oauth.get("refreshToken") or oauth.get("refresh_token")
    scopes = oauth.get("scopes") or CLAUDE_DEFAULT_SCOPES
    if not isinstance(scopes, list):
        scopes = CLAUDE_DEFAULT_SCOPES
    _, _, response = http_request(
        CLAUDE_TOKEN_URL,
        method="POST",
        json_body={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_CLIENT_ID,
            "scope": " ".join(str(scope) for scope in scopes),
        },
    )
    if not isinstance(response, dict) or not response.get("access_token"):
        raise RuntimeError("Claude refresh response has no access_token")
    oauth["accessToken"] = response["access_token"]
    oauth["refreshToken"] = response.get("refresh_token") or refresh_token
    oauth["expiresAt"] = int(time.time() * 1000) + int(response.get("expires_in", 3600)) * 1000
    oauth["_aiUsageLastRefreshAt"] = int(time.time() * 1000)
    if response.get("refresh_token_expires_in") is not None:
        oauth["refreshTokenExpiresAt"] = (
            int(time.time() * 1000) + int(response["refresh_token_expires_in"]) * 1000
        )
    response_scope = response.get("scope")
    if isinstance(response_scope, str):
        oauth["scopes"] = response_scope.split()
    atomic_write_json(CLAUDE_PATH, credentials)
    return oauth["accessToken"]


def codex_client_id(credentials: dict[str, Any]) -> str:
    token = credentials.get("tokens", {}).get("id_token", "")
    audience = decode_jwt_payload(token).get("aud")
    if isinstance(audience, str) and audience.startswith("app_"):
        return audience
    if isinstance(audience, list):
        for item in audience:
            if isinstance(item, str) and item.startswith("app_"):
                return item
    return CODEX_DEFAULT_CLIENT_ID


def codex_needs_refresh(credentials: dict[str, Any]) -> bool:
    access_token = credentials.get("tokens", {}).get("access_token", "")
    expiry = token_expiry(access_token)
    return expiry is not None and expiry <= int(time.time()) + 60 * 60


def refresh_codex(credentials: dict[str, Any]) -> str:
    tokens = credentials["tokens"]
    old_account_id = tokens.get("account_id")
    _, _, response = http_request(
        CODEX_TOKEN_URL,
        method="POST",
        json_body={
            "client_id": codex_client_id(credentials),
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        },
    )
    if not isinstance(response, dict) or not response.get("access_token"):
        raise RuntimeError("Codex refresh response has no access_token")
    if response.get("id_token"):
        tokens["id_token"] = response["id_token"]
    tokens["access_token"] = response["access_token"]
    if response.get("refresh_token"):
        tokens["refresh_token"] = response["refresh_token"]
    new_account_id = tokens.get("account_id")
    if old_account_id and new_account_id != old_account_id:
        raise RuntimeError("Codex account changed during token refresh")
    credentials["last_refresh"] = iso_now()
    atomic_write_json(CODEX_PATH, credentials)
    return tokens["access_token"]


def normalize_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return value
    return str(value)


def ha_service(domain: str, service: str, body: dict[str, Any]) -> None:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    http_request(
        f"{HA_API}/services/{domain}/{service}",
        method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
        json_body=body,
    )


def set_ha_number(name: str, value: float) -> None:
    ha_service("input_number", "set_value", {"entity_id": f"input_number.{name}", "value": float(value)})


def set_ha_text(name: str, value: str) -> None:
    ha_service("input_text", "set_value", {"entity_id": f"input_text.{name}", "value": str(value)})


def fetch_claude_usage(access_token: str) -> dict[str, Any]:
    _, _, value = http_request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    if not isinstance(value, dict):
        raise RuntimeError("Claude usage response is not an object")
    return value


def fetch_codex_headers(access_token: str, account_id: str) -> dict[str, str]:
    session_id = secrets.token_hex(16)
    body = {
        "model": "gpt-5.5",
        "instructions": "",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
        "stream": True,
        "store": False,
    }
    _, headers, _ = http_request(
        CODEX_USAGE_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "session_id": session_id,
        },
        json_body=body,
        read_body=False,
    )
    return headers


def parse_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class Monitor:
    def __init__(self, options: dict[str, Any]):
        self.options = options
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.state: dict[str, Any] = {
            "started_at": iso_now(),
            "polling": False,
            "claude": {"configured": CLAUDE_PATH.exists()},
            "codex": {"configured": CODEX_PATH.exists()},
        }
        try:
            saved = load_json(STATE_PATH)
            for provider in ("claude", "codex"):
                if isinstance(saved.get(provider), dict):
                    self.state[provider].update(saved[provider])
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            return json.loads(json.dumps(self.state))

    def update_provider(self, provider: str, **values: Any) -> None:
        with self.state_lock:
            self.state.setdefault(provider, {}).update(values)
            persistable = {
                "claude": self.state.get("claude", {}),
                "codex": self.state.get("codex", {}),
                "last_poll": self.state.get("last_poll"),
            }
            atomic_write_json(STATE_PATH, persistable)

    def poll_claude(self) -> None:
        if not CLAUDE_PATH.exists():
            self.update_provider("claude", configured=False, error="Credentials are not configured")
            return
        credentials = load_json(CLAUDE_PATH)
        validate_claude_credentials(credentials)
        oauth = claude_oauth(credentials)
        access_token = oauth.get("accessToken") or oauth.get("access_token")
        refreshed = False
        if claude_needs_refresh(oauth):
            access_token = refresh_claude(credentials)
            refreshed = True
        try:
            usage = fetch_claude_usage(access_token)
        except HttpFailure as error:
            if error.status != HTTPStatus.UNAUTHORIZED or refreshed:
                raise
            access_token = refresh_claude(credentials)
            usage = fetch_claude_usage(access_token)

        five_hour = usage.get("five_hour") or {}
        seven_day = usage.get("seven_day") or {}
        five_percent = parse_float(five_hour.get("utilization"))
        week_percent = parse_float(seven_day.get("utilization"))
        five_reset = normalize_iso(five_hour.get("resets_at"))
        week_reset = normalize_iso(seven_day.get("resets_at"))
        updated = iso_now()

        set_ha_number("ai_usage_claude_5h_percent", five_percent)
        set_ha_number("ai_usage_claude_weekly_percent", week_percent)
        if five_reset:
            set_ha_text("ai_usage_claude_5h_reset", five_reset)
        if week_reset:
            set_ha_text("ai_usage_claude_weekly_reset", week_reset)
        set_ha_text("ai_usage_claude_updated_at", updated)
        self.update_provider(
            "claude",
            configured=True,
            error=None,
            last_ok=updated,
            five_hour_percent=five_percent,
            weekly_percent=week_percent,
            five_hour_reset=five_reset,
            weekly_reset=week_reset,
        )
        logging.info("Claude OK: 5h=%s%%, weekly=%s%%", five_percent, week_percent)

    def poll_codex(self) -> None:
        if not CODEX_PATH.exists():
            self.update_provider("codex", configured=False, error="Credentials are not configured")
            return
        credentials = load_json(CODEX_PATH)
        validate_codex_credentials(credentials)
        tokens = credentials["tokens"]
        access_token = tokens["access_token"]
        refreshed = False
        if codex_needs_refresh(credentials):
            access_token = refresh_codex(credentials)
            refreshed = True
        try:
            headers = fetch_codex_headers(access_token, tokens["account_id"])
        except HttpFailure as error:
            if error.status != HTTPStatus.UNAUTHORIZED or refreshed:
                raise
            access_token = refresh_codex(credentials)
            headers = fetch_codex_headers(access_token, tokens["account_id"])

        filled: set[str] = set()
        values: dict[str, Any] = {}
        for slot in ("primary", "secondary"):
            percent = headers.get(f"x-codex-{slot}-used-percent")
            minutes = parse_float(headers.get(f"x-codex-{slot}-window-minutes"))
            reset_epoch = headers.get(f"x-codex-{slot}-reset-at")
            if percent is None or minutes <= 0:
                continue
            window = "5h" if minutes <= 720 else "weekly"
            filled.add(window)
            reset_iso = None
            if reset_epoch:
                reset_iso = datetime.fromtimestamp(int(reset_epoch), timezone.utc).isoformat(timespec="seconds")
            percent_value = parse_float(percent)
            set_ha_number(f"ai_usage_codex_{window}_percent", percent_value)
            if reset_iso:
                set_ha_text(f"ai_usage_codex_{window}_reset", reset_iso)
            values[f"{window}_percent"] = percent_value
            values[f"{window}_reset"] = reset_iso

        if not filled:
            raise RuntimeError("Codex response has no rate-limit headers")
        for window in ("5h", "weekly"):
            if window not in filled:
                set_ha_text(f"ai_usage_codex_{window}_reset", "None")
                values[f"{window}_enabled"] = False
            else:
                values[f"{window}_enabled"] = True

        plan = headers.get("x-codex-plan-type") or ""
        updated = iso_now()
        if plan:
            set_ha_text("ai_usage_codex_plan", plan)
        set_ha_text("ai_usage_codex_updated_at", updated)
        self.update_provider("codex", configured=True, error=None, last_ok=updated, plan=plan, **values)
        logging.info("Codex OK: %s, plan=%s", ", ".join(sorted(filled)), plan or "unknown")

    def poll_all(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        with self.state_lock:
            self.state["polling"] = True
            self.state["last_poll"] = iso_now()
        try:
            for provider, callback in (("claude", self.poll_claude), ("codex", self.poll_codex)):
                try:
                    callback()
                except Exception as error:
                    message = safe_error(error)
                    logging.error("%s FAIL: %s", provider.capitalize(), message)
                    self.update_provider(provider, configured=True, error=message, last_error=iso_now())
            return True
        finally:
            with self.state_lock:
                self.state["polling"] = False
            self.lock.release()


def safe_error(error: Exception) -> str:
    if isinstance(error, HttpFailure):
        detail = ""
        try:
            parsed = json.loads(error.body)
            error_value = parsed.get("error")
            if isinstance(error_value, dict):
                detail = error_value.get("message") or error_value.get("type") or ""
            elif isinstance(error_value, str):
                detail = error_value
            detail = detail or parsed.get("error_description") or parsed.get("message") or ""
        except Exception:
            pass
        endpoint = ""
        if error.url:
            parsed_url = urlsplit(error.url)
            endpoint = f" ({parsed_url.netloc}{parsed_url.path})"
        return f"HTTP {error.status}{endpoint}" + (f": {detail}" if detail else "")
    return str(error).replace("\n", " ")[:500]


def load_options() -> dict[str, Any]:
    defaults = {"interval_minutes": 15, "poll_on_start": True, "debug": False}
    try:
        defaults.update(load_json(OPTIONS_PATH))
    except FileNotFoundError:
        pass
    except Exception as error:
        logging.warning("Cannot read options: %s", safe_error(error))
    defaults["interval_minutes"] = max(5, min(1440, int(defaults["interval_minutes"])))
    return defaults


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Usage Monitor</title>
<style>
body{font:15px system-ui,sans-serif;margin:0;background:#111827;color:#e5e7eb}main{max-width:760px;margin:auto;padding:24px}
h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:#1f2937;border-radius:12px;padding:18px}
.ok{color:#34d399}.bad{color:#fb7185}.muted{color:#9ca3af}button,label{display:inline-block;margin-top:10px}button{background:#03a9f4;color:#06131b;border:0;border-radius:7px;padding:9px 14px;font-weight:600;cursor:pointer}input{max-width:100%}pre{white-space:pre-wrap;word-break:break-word;background:#111827;padding:10px;border-radius:7px}code{color:#93c5fd}
</style></head><body><main>
<h1>AI Usage Monitor</h1><p class="muted">Файлы сохраняются только в защищённом каталоге <code>/data</code> add-on.</p>
<div class="grid"><section class="card"><h2>Claude</h2><div id="claude">Загрузка…</div><label>Файл <code>.claude/.credentials.json</code><br><input id="claudeFile" type="file" accept="application/json"></label><br><button onclick="upload('claude')">Импортировать</button></section>
<section class="card"><h2>Codex</h2><div id="codex">Загрузка…</div><label>Файл <code>.codex/auth.json</code><br><input id="codexFile" type="file" accept="application/json"></label><br><button onclick="upload('codex')">Импортировать</button></section></div>
<p><button onclick="pollNow()">Обновить сейчас</button> <span id="message" class="muted"></span></p>
<script>
const api=p=>new URL('./api/'+p,location.href).toString();
function renderProvider(name,p){const el=document.getElementById(name);const cls=p.error?'bad':'ok';const usage=name==='claude'?`5h: ${p.five_hour_percent??'—'}%, неделя: ${p.weekly_percent??'—'}%`:`5h: ${p['5h_percent']??'—'}%, неделя: ${p.weekly_percent??'—'}%`;el.innerHTML=`<p class="${cls}">${p.configured?(p.error?'Ошибка':'Настроено'):'Не настроено'}</p><p>${usage}</p><p class="muted">Последнее обновление: ${p.last_ok??'—'}</p>${p.error?`<pre>${escapeHtml(p.error)}</pre>`:''}`}
function escapeHtml(v){const d=document.createElement('div');d.textContent=v;return d.innerHTML}
async function refresh(){const s=await (await fetch(api('status'))).json();renderProvider('claude',s.claude||{});renderProvider('codex',s.codex||{});document.getElementById('message').textContent=s.polling?'Выполняется опрос…':''}
async function upload(name){const f=document.getElementById(name+'File').files[0];if(!f){alert('Выберите JSON-файл');return}const r=await fetch(api('import/'+name),{method:'POST',headers:{'Content-Type':'application/json'},body:await f.text()});const j=await r.json();document.getElementById('message').textContent=j.message||j.error||'';await refresh()}
async function pollNow(){const r=await fetch(api('poll'),{method:'POST'});const j=await r.json();document.getElementById('message').textContent=j.message||j.error||'';setTimeout(refresh,2000)}
refresh();setInterval(refresh,5000);
</script></main></body></html>"""


class WebHandler(BaseHTTPRequestHandler):
    monitor: Monitor

    def log_message(self, format_string: str, *args: Any) -> None:
        if self.monitor.options.get("debug"):
            logging.debug("HTTP: " + format_string, *args)

    def send_json(self, status: int, value: dict[str, Any]) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path.endswith("/api/status") or path == "/api/status":
            self.send_json(HTTPStatus.OK, self.monitor.snapshot())
            return
        raw = PAGE.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path.endswith("/api/poll"):
            started = not self.monitor.snapshot().get("polling", False)
            if started:
                threading.Thread(target=self.monitor.poll_all, name="manual-poll", daemon=True).start()
            self.send_json(HTTPStatus.ACCEPTED, {"message": "Опрос запущен" if started else "Опрос уже выполняется"})
            return
        provider = None
        if path.endswith("/api/import/claude"):
            provider = "claude"
        elif path.endswith("/api/import/codex"):
            provider = "codex"
        if provider is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_IMPORT_SIZE:
                raise ValueError("Некорректный размер файла")
            value = json.loads(self.rfile.read(length).decode("utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("Файл должен содержать JSON-объект")
            if provider == "claude":
                validate_claude_credentials(value)
                atomic_write_json(CLAUDE_PATH, value)
            else:
                validate_codex_credentials(value)
                atomic_write_json(CODEX_PATH, value)
            self.monitor.update_provider(provider, configured=True, error=None)
            self.send_json(HTTPStatus.OK, {"message": f"{provider.capitalize()}: файл импортирован"})
        except Exception as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_error(error)})


def scheduler(monitor: Monitor) -> None:
    if monitor.options.get("poll_on_start", True):
        monitor.poll_all()
    interval = int(monitor.options["interval_minutes"]) * 60
    while True:
        time.sleep(interval)
        monitor.poll_all()


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options = load_options()
    logging.basicConfig(
        level=logging.DEBUG if options.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("AI Usage Monitor starting; interval=%s min", options["interval_minutes"])
    monitor = Monitor(options)
    WebHandler.monitor = monitor
    threading.Thread(target=scheduler, args=(monitor,), name="scheduler", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), WebHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
