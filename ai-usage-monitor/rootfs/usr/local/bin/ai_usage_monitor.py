#!/usr/bin/env python3
"""Home Assistant add-on for Claude, Codex, and z.ai usage monitoring."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DATA_DIR = Path(os.getenv("AI_USAGE_DATA_DIR", "/data"))
OPTIONS_PATH = Path(os.getenv("AI_USAGE_OPTIONS_PATH", str(DATA_DIR / "options.json")))
CLAUDE_PATH = DATA_DIR / "claude_credentials.json"
CODEX_PATH = DATA_DIR / "codex_auth.json"
ACCOUNTS_PATH = DATA_DIR / "codex_accounts.json"
ZAI_PATH = DATA_DIR / "zai_credentials.json"
STATE_PATH = DATA_DIR / "state.json"


def codex_path(account: str) -> Path:
    """Keep legacy credentials and isolate every additional account."""
    if not re.fullmatch(r"codex(?:[2-9]|[1-9][0-9]{1,8})?", account):
        raise ValueError("Unknown Codex account slot")
    return CODEX_PATH if account == "codex" else DATA_DIR / f"{account}_auth.json"


HA_API = os.getenv("HA_API_URL", "http://supervisor/core/api").rstrip("/")
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN", "")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Manual-redirect login of Claude Code: the callback page shows "code#state"
# for the user to paste back, so the add-on needs no reachable redirect URL.
CLAUDE_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
CLAUDE_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
CLAUDE_LOGIN_TTL = 15 * 60
CLAUDE_DEFAULT_SCOPES = [
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]

CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# Device-code login of the Codex CLI: the user approves a short code on the
# OpenAI page, the add-on polls for the authorization code on its own.
CODEX_ISSUER = "https://auth.openai.com"
CODEX_DEVICE_API = f"{CODEX_ISSUER}/api/accounts/deviceauth"
CODEX_DEVICE_VERIFY_URL = f"{CODEX_ISSUER}/codex/device"
CODEX_DEVICE_REDIRECT_URI = f"{CODEX_ISSUER}/deviceauth/callback"
CODEX_LOGIN_TTL = 15 * 60
ZAI_USAGE_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
HA_EVENT_UPDATE = "ai_usage_monitor_update"

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
    form_body: dict[str, str] | None = None,
    read_body: bool = True,
) -> tuple[int, dict[str, str], Any]:
    request_headers = {"User-Agent": "ha-ai-usage-monitor/0.6.0", **(headers or {})}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    elif form_body is not None:
        data = urlencode(form_body).encode("ascii")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            raw = response.read() if read_body else b""
            parsed: Any = None
            if raw:
                parsed = json.loads(raw.decode("utf-8"))
            return response.status, response_headers, parsed
    except urllib.error.HTTPError as error:
        raw = error.read(16 * 1024)
        body = raw.decode("utf-8", errors="replace")
        response_headers = {key.lower(): value for key, value in error.headers.items()}
        raise HttpFailure(
            error.code, f"HTTP {error.code} from {url}", body, response_headers, url
        ) from error
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


def zai_api_key(value: dict[str, Any]) -> str:
    """Extract a z.ai Coding Plan key from supported JSON formats."""
    containers = [value]
    for key in ("env", "zai", "z.ai", "zai-coding-plan"):
        nested = value.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in (
            "api_key",
            "apiKey",
            "key",
            "token",
            "ANTHROPIC_AUTH_TOKEN",
            "ZAI_API_KEY",
        ):
            candidate = container.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    raise ValueError("z.ai Coding Plan API key is missing")


def validate_zai_credentials(value: dict[str, Any]) -> None:
    """Validate an imported z.ai key without exposing its value."""
    zai_api_key(value)


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
    oauth["expiresAt"] = (
        int(time.time() * 1000) + int(response.get("expires_in", 3600)) * 1000
    )
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


def claude_pkce_pair() -> tuple[str, str]:
    """Return a PKCE code verifier and its S256 challenge."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def claude_authorize_url(challenge: str, state: str) -> str:
    query = urlencode(
        {
            "code": "true",
            "client_id": CLAUDE_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "scope": " ".join(CLAUDE_DEFAULT_SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{CLAUDE_AUTHORIZE_URL}?{query}"


def exchange_claude_code(pasted: str, verifier: str, state: str) -> dict[str, Any]:
    """Exchange a pasted "code#state" for an add-on-owned Claude OAuth grant."""
    code, _, pasted_state = pasted.strip().partition("#")
    if not code:
        raise ValueError("Код авторизации пустой")
    if pasted_state and pasted_state != state:
        raise ValueError("Код от другой попытки входа, начните вход заново")
    _, _, response = http_request(
        CLAUDE_TOKEN_URL,
        method="POST",
        json_body={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT_URI,
            "client_id": CLAUDE_CLIENT_ID,
            "code_verifier": verifier,
            "state": state,
        },
    )
    if not isinstance(response, dict) or not response.get("refresh_token"):
        raise RuntimeError("Claude login response has no refresh_token")
    now_ms = int(time.time() * 1000)
    oauth: dict[str, Any] = {
        "accessToken": response["access_token"],
        "refreshToken": response["refresh_token"],
        "expiresAt": now_ms + int(response.get("expires_in", 3600)) * 1000,
        "scopes": str(response.get("scope") or "").split() or CLAUDE_DEFAULT_SCOPES,
        "_aiUsageLoginAt": now_ms,
    }
    if response.get("refresh_token_expires_in") is not None:
        oauth["refreshTokenExpiresAt"] = (
            now_ms + int(response["refresh_token_expires_in"]) * 1000
        )
    credentials = {"claudeAiOauth": oauth}
    validate_claude_credentials(credentials)
    return credentials


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


def refresh_codex(credentials: dict[str, Any], path: Path | None = None) -> str:
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
    atomic_write_json(path or CODEX_PATH, credentials)
    return tokens["access_token"]


def request_codex_device_code() -> dict[str, Any]:
    _, _, response = http_request(
        f"{CODEX_DEVICE_API}/usercode",
        method="POST",
        json_body={"client_id": CODEX_DEFAULT_CLIENT_ID},
    )
    if not isinstance(response, dict) or not response.get("device_auth_id"):
        raise RuntimeError("Codex device code response has no device_auth_id")
    user_code = response.get("user_code") or response.get("usercode")
    if not isinstance(user_code, str) or not user_code:
        raise RuntimeError("Codex device code response has no user_code")
    return {
        "device_auth_id": response["device_auth_id"],
        "user_code": user_code,
        "interval": max(1.0, parse_float_or_none(response.get("interval")) or 5.0),
    }


def poll_codex_device_code(
    device_auth_id: str, user_code: str
) -> dict[str, Any] | None:
    """Return the authorization code once approved, None while still pending."""
    try:
        _, _, response = http_request(
            f"{CODEX_DEVICE_API}/token",
            method="POST",
            json_body={"device_auth_id": device_auth_id, "user_code": user_code},
        )
    except HttpFailure as error:
        if error.status in (HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND):
            return None
        raise
    if not isinstance(response, dict) or not response.get("authorization_code"):
        raise RuntimeError("Codex device token response has no authorization_code")
    return response


def exchange_codex_device_code(approved: dict[str, Any]) -> dict[str, Any]:
    """Exchange an approved device code for an add-on-owned auth.json."""
    _, _, response = http_request(
        CODEX_TOKEN_URL,
        method="POST",
        form_body={
            "grant_type": "authorization_code",
            "code": approved["authorization_code"],
            "redirect_uri": CODEX_DEVICE_REDIRECT_URI,
            "client_id": CODEX_DEFAULT_CLIENT_ID,
            "code_verifier": approved.get("code_verifier", ""),
        },
    )
    if not isinstance(response, dict) or not response.get("refresh_token"):
        raise RuntimeError("Codex login response has no refresh_token")
    claims = decode_jwt_payload(response.get("id_token", ""))
    auth = claims.get("https://api.openai.com/auth")
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    credentials = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": response.get("id_token", ""),
            "access_token": response["access_token"],
            "refresh_token": response["refresh_token"],
            "account_id": account_id,
        },
        "last_refresh": iso_now(),
    }
    validate_codex_credentials(credentials)
    return credentials


def normalize_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return (
                datetime.fromisoformat(text)
                .astimezone(timezone.utc)
                .isoformat(timespec="seconds")
            )
        except ValueError:
            return value
    return str(value)


def publish_ha_update(
    provider: str,
    updated_at: str,
    windows: dict[str, dict[str, Any]],
    *,
    plan: str | None = None,
    account_id: str | None = None,
    account_name: str | None = None,
) -> None:
    """Publish usage data for the companion Home Assistant integration."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    payload: dict[str, Any] = {
        "provider": provider,
        "updated_at": updated_at,
        "windows": windows,
    }
    if account_id is not None:
        payload["account_id"] = account_id
        payload["account_name"] = account_name
    if plan:
        payload["plan"] = plan
    http_request(
        f"{HA_API}/events/{HA_EVENT_UPDATE}",
        method="POST",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
        json_body=payload,
    )


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


def fetch_codex_usage(access_token: str, account_id: str) -> dict[str, Any]:
    """Fetch Codex subscription windows without consuming a prompt."""
    _, _, value = http_request(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "Accept": "application/json",
        },
    )
    if not isinstance(value, dict):
        raise RuntimeError("Codex usage response is not an object")
    return value


def fetch_zai_usage(api_key: str) -> dict[str, Any]:
    """Fetch z.ai Coding Plan quota windows."""
    _, _, value = http_request(
        ZAI_USAGE_URL,
        headers={
            "Authorization": api_key,
            "Accept-Language": "en-US,en",
            "Accept": "application/json",
        },
    )
    if not isinstance(value, dict):
        raise RuntimeError("z.ai usage response is not an object")
    return value


def parse_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_epoch_reset(value: Any) -> str | None:
    """Normalize an epoch-second/millisecond or ISO reset timestamp."""
    numeric = parse_float_or_none(value)
    if numeric is not None:
        if numeric > 100_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, timezone.utc).isoformat(
            timespec="seconds"
        )
    return normalize_iso(value)


def parse_codex_usage(
    response: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Map Codex usage response and honor explicit exhaustion flags."""
    rate_limit = response.get("rate_limit")
    if not isinstance(rate_limit, dict):
        raise RuntimeError("Codex usage response has no rate_limit")

    windows: dict[str, dict[str, Any]] = {
        "5h": {"enabled": False},
        "weekly": {"enabled": False},
    }
    filled: set[str] = set()
    for slot in ("primary_window", "secondary_window"):
        value = rate_limit.get(slot)
        if not isinstance(value, dict):
            continue
        percent = parse_float_or_none(value.get("used_percent"))
        seconds = parse_float_or_none(value.get("limit_window_seconds"))
        if percent is None or seconds is None or seconds <= 0:
            continue
        window = "5h" if seconds / 60 <= 720 else "weekly"
        if window in filled:
            continue
        remaining = parse_float_or_none(value.get("remaining_percent"))
        if remaining is not None and remaining <= 0:
            percent = 100
        windows[window] = {
            "enabled": True,
            "used_percent": max(0.0, min(100.0, percent)),
            "resets_at": normalize_epoch_reset(value.get("reset_at")),
        }
        filled.add(window)

    if not filled:
        raise RuntimeError("Codex response has no rate-limit windows")
    if rate_limit.get("limit_reached") is True or rate_limit.get("allowed") is False:
        exhausted = max(filled, key=lambda name: windows[name]["used_percent"])
        windows[exhausted]["used_percent"] = 100.0
    return windows, str(response.get("plan_type") or "")


def normalize_zai_reset(value: Any) -> str | None:
    """Normalize an epoch-millisecond or ISO z.ai reset timestamp."""
    return normalize_epoch_reset(value)


def parse_zai_usage(
    response: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], str]:
    """Map both legacy token and current credit quotas to HA windows."""
    data = response.get("data", response)
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
        raise RuntimeError("z.ai usage response has no limits")

    windows: dict[str, dict[str, Any]] = {
        "5h": {"enabled": False},
        "weekly": {"enabled": False},
    }
    filled: set[str] = set()
    for limit in data["limits"]:
        if not isinstance(limit, dict) or limit.get("type") not in (
            "TOKENS_LIMIT",
            "CREDIT_LIMIT",
        ):
            continue
        unit = parse_float_or_none(limit.get("unit"))
        window = "5h" if unit == 3 else "weekly" if unit == 6 else None
        if window is None or window in filled:
            continue
        percent = parse_float_or_none(limit.get("percentage"))
        if percent is None:
            total = parse_float_or_none(limit.get("usage"))
            current = parse_float_or_none(limit.get("currentValue"))
            if total and current is not None:
                percent = current / total * 100
        if percent is None:
            continue
        remaining = parse_float_or_none(limit.get("remaining"))
        if remaining is not None and remaining <= 0:
            percent = 100
        windows[window] = {
            "enabled": True,
            "used_percent": max(0.0, min(100.0, percent)),
            "resets_at": normalize_zai_reset(limit.get("nextResetTime")),
        }
        filled.add(window)

    if not filled:
        raise RuntimeError("z.ai response has no supported quota windows")
    plan = data.get("level") or data.get("planName") or ""
    return windows, str(plan)


class Monitor:
    def __init__(self, options: dict[str, Any]):
        self.options = options
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        try:
            account_config = load_json(ACCOUNTS_PATH)
            self.accounts = account_config["accounts"]
            self.next_account_id = int(account_config["next_id"])
            if not isinstance(self.accounts, dict) or self.next_account_id < 2:
                raise ValueError("Invalid Codex account configuration")
            for account, metadata in self.accounts.items():
                codex_path(account)
                if not isinstance(metadata.get("name"), str):
                    raise ValueError("Invalid Codex account name")
        except FileNotFoundError:
            self.accounts = {"codex": {"name": "Codex 1"}}
            self.next_account_id = 2
        self.state: dict[str, Any] = {
            "started_at": iso_now(),
            "polling": False,
            "claude": {"configured": CLAUDE_PATH.exists()},
            **{
                account: {"configured": codex_path(account).exists()}
                for account in self.accounts
            },
            "zai": {"configured": ZAI_PATH.exists()},
        }
        # Pending in-panel Claude login: the PKCE verifier and state live only in
        # memory, so an add-on restart simply requires starting the login again.
        self.claude_login: dict[str, Any] | None = None
        # Each Codex login attempt gets a new generation; a stale polling
        # thread notices the change and stops.
        self.codex_login_generation = {account: 0 for account in self.accounts}
        try:
            saved = load_json(STATE_PATH)
            for provider in ("claude", *self.accounts, "zai"):
                if isinstance(saved.get(provider), dict):
                    self.state[provider].update(saved[provider])
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        with self.state_lock:
            return json.loads(json.dumps({**self.state, "accounts": self.accounts}))

    def update_provider(self, provider: str, **values: Any) -> None:
        with self.state_lock:
            if provider.startswith("codex") and provider not in self.accounts:
                return
            self.state.setdefault(provider, {}).update(values)
            persistable = {
                "claude": self.state.get("claude", {}),
                **{account: self.state.get(account, {}) for account in self.accounts},
                "zai": self.state.get("zai", {}),
                "last_poll": self.state.get("last_poll"),
            }
            atomic_write_json(STATE_PATH, persistable)

    def _save_accounts(self) -> None:
        atomic_write_json(
            ACCOUNTS_PATH, {"next_id": self.next_account_id, "accounts": self.accounts}
        )

    def add_codex_account(self, name: str) -> str:
        name = self.validate_account_name(name)
        with self.lock, self.state_lock:
            account = f"codex{self.next_account_id}"
            self.next_account_id += 1
            self.accounts[account] = {"name": name}
            try:
                self._save_accounts()
            except Exception:
                self.accounts.pop(account)
                raise
            self.codex_login_generation[account] = 0
            self.state[account] = {"configured": False}
        return account

    @staticmethod
    def validate_account_name(name: Any) -> str:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError("Название аккаунта должно содержать от 1 до 60 символов")
        return name.strip()

    def rename_codex_account(self, account: str, name: str) -> None:
        name = self.validate_account_name(name)
        with self.lock, self.state_lock:
            if account not in self.accounts:
                raise ValueError("Аккаунт не найден")
            previous = self.accounts[account]
            self.accounts[account] = {"name": name}
            try:
                self._save_accounts()
            except Exception:
                self.accounts[account] = previous
                raise

    def delete_codex_account(self, account: str) -> None:
        with self.lock, self.state_lock:
            if account not in self.accounts:
                raise ValueError("Аккаунт не найден")
            # Remove credentials first: if this fails the account stays usable.
            codex_path(account).unlink(missing_ok=True)
            previous = self.accounts.pop(account)
            try:
                self._save_accounts()
            except Exception:
                self.accounts[account] = previous
                raise
            self.codex_login_generation[account] += 1
            self.state.pop(account, None)
            self.state.pop(f"{account}_login", None)

    def publish_accounts(self) -> None:
        with self.state_lock:
            accounts = [
                {"id": key, "name": value["name"]}
                for key, value in self.accounts.items()
            ]
        http_request(
            f"{HA_API}/events/ai_usage_monitor_accounts",
            method="POST",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            json_body={"accounts": accounts},
        )

    def start_claude_login(self) -> str:
        verifier, challenge = claude_pkce_pair()
        state = secrets.token_urlsafe(32)
        with self.state_lock:
            self.claude_login = {
                "verifier": verifier,
                "state": state,
                "expires": time.time() + CLAUDE_LOGIN_TTL,
            }
        return claude_authorize_url(challenge, state)

    def finish_claude_login(self, pasted: str) -> None:
        with self.state_lock:
            login, self.claude_login = self.claude_login, None
        if login is None or login["expires"] < time.time():
            raise ValueError("Вход не начат или устарел, нажмите «Войти в Claude»")
        credentials = exchange_claude_code(pasted, login["verifier"], login["state"])
        # A poll may be refreshing the old file right now; writing after it
        # finishes keeps that poll from overwriting the new grant.
        if not self.lock.acquire(timeout=REQUEST_TIMEOUT * 3):
            raise RuntimeError("Опрос не завершился, повторите вход")
        try:
            atomic_write_json(CLAUDE_PATH, credentials)
        finally:
            self.lock.release()
        self.update_provider("claude", configured=True, error=None)
        logging.info("Claude login completed in the add-on")

    def start_codex_login(self, provider: str = "codex") -> dict[str, Any]:
        codex_path(provider)
        with self.state_lock:
            if provider not in self.accounts:
                raise ValueError("Аккаунт не найден")
        device = request_codex_device_code()
        with self.state_lock:
            if provider not in self.accounts:
                raise ValueError("Аккаунт удалён")
            self.codex_login_generation[provider] += 1
            generation = self.codex_login_generation[provider]
            self.state[f"{provider}_login"] = {
                "status": "pending",
                "user_code": device["user_code"],
                "url": CODEX_DEVICE_VERIFY_URL,
            }
        threading.Thread(
            target=self._complete_codex_login,
            args=(generation, device, provider),
            name=f"{provider}-login",
            daemon=True,
        ).start()
        return self.snapshot()[f"{provider}_login"]

    def _codex_login_current(self, generation: int, provider: str = "codex") -> bool:
        with self.state_lock:
            return (
                provider in self.accounts
                and generation == self.codex_login_generation.get(provider)
            )

    def _set_codex_login(
        self, generation: int, provider: str = "codex", **values: Any
    ) -> None:
        with self.state_lock:
            if (
                provider in self.accounts
                and generation == self.codex_login_generation.get(provider)
            ):
                self.state[f"{provider}_login"] = values

    def _complete_codex_login(
        self, generation: int, device: dict[str, Any], provider: str = "codex"
    ) -> None:
        deadline = time.time() + CODEX_LOGIN_TTL
        try:
            approved = None
            while approved is None:
                if not self._codex_login_current(generation, provider):
                    return
                if time.time() > deadline:
                    raise RuntimeError(
                        "Код не подтверждён за 15 минут, начните вход заново"
                    )
                time.sleep(device["interval"])
                approved = poll_codex_device_code(
                    device["device_auth_id"], device["user_code"]
                )
            credentials = exchange_codex_device_code(approved)
            # Same ordering as the Claude login: never race a poll that may be
            # rewriting the old file after a refresh.
            if not self.lock.acquire(timeout=REQUEST_TIMEOUT * 3):
                raise RuntimeError("Опрос не завершился, повторите вход")
            try:
                with self.state_lock:
                    if (
                        provider not in self.accounts
                        or generation != self.codex_login_generation.get(provider)
                    ):
                        return
                    atomic_write_json(codex_path(provider), credentials)
            finally:
                self.lock.release()
        except Exception as error:
            logging.error("Codex login FAIL: %s", safe_error(error))
            self._set_codex_login(
                generation, provider, status="error", error=safe_error(error)
            )
            return
        self.update_provider(provider, configured=True, error=None)
        self._set_codex_login(generation, provider, status="done")
        logging.info("Codex login completed in the add-on")
        self.poll_all()

    def poll_claude(self) -> None:
        if not CLAUDE_PATH.exists():
            self.update_provider(
                "claude", configured=False, error="Credentials are not configured"
            )
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

        publish_ha_update(
            "claude",
            updated,
            {
                "5h": {
                    "enabled": bool(five_hour),
                    "used_percent": five_percent,
                    "resets_at": five_reset,
                },
                "weekly": {
                    "enabled": bool(seven_day),
                    "used_percent": week_percent,
                    "resets_at": week_reset,
                },
            },
        )
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

    def poll_codex(self, provider: str = "codex") -> None:
        path = codex_path(provider)
        if not path.exists():
            self.update_provider(
                provider, configured=False, error="Credentials are not configured"
            )
            return
        credentials = load_json(path)
        validate_codex_credentials(credentials)
        tokens = credentials["tokens"]
        access_token = tokens["access_token"]
        refreshed = False
        if codex_needs_refresh(credentials):
            access_token = refresh_codex(credentials, path)
            refreshed = True
        try:
            usage = fetch_codex_usage(access_token, tokens["account_id"])
        except HttpFailure as error:
            if error.status != HTTPStatus.UNAUTHORIZED or refreshed:
                raise
            access_token = refresh_codex(credentials, path)
            usage = fetch_codex_usage(access_token, tokens["account_id"])

        windows, plan = parse_codex_usage(usage)
        values: dict[str, Any] = {}
        for window, window_data in windows.items():
            values[f"{window}_enabled"] = window_data["enabled"]
            if window_data["enabled"]:
                values[f"{window}_percent"] = window_data["used_percent"]
                values[f"{window}_reset"] = window_data.get("resets_at")
        updated = iso_now()
        publish_ha_update(
            "codex",
            updated,
            windows,
            plan=plan,
            account_id=provider,
            account_name=self.accounts[provider]["name"],
        )
        self.update_provider(
            provider, configured=True, error=None, last_ok=updated, plan=plan, **values
        )
        logging.info(
            "Codex OK: %s, plan=%s",
            ", ".join(name for name, value in windows.items() if value["enabled"]),
            plan or "unknown",
        )

    def poll_zai(self) -> None:
        if not ZAI_PATH.exists():
            self.update_provider(
                "zai", configured=False, error="Credentials are not configured"
            )
            return
        credentials = load_json(ZAI_PATH)
        api_key = zai_api_key(credentials)
        windows, plan = parse_zai_usage(fetch_zai_usage(api_key))
        updated = iso_now()
        publish_ha_update("zai", updated, windows, plan=plan)
        values: dict[str, Any] = {}
        for window, window_data in windows.items():
            values[f"{window}_enabled"] = window_data["enabled"]
            if window_data["enabled"]:
                values[f"{window}_percent"] = window_data["used_percent"]
                values[f"{window}_reset"] = window_data.get("resets_at")
        self.update_provider(
            "zai", configured=True, error=None, last_ok=updated, plan=plan, **values
        )
        enabled = [window for window, value in windows.items() if value["enabled"]]
        logging.info("z.ai OK: %s, plan=%s", ", ".join(enabled), plan or "unknown")

    def poll_all(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        with self.state_lock:
            self.state["polling"] = True
            self.state["last_poll"] = iso_now()
        try:
            try:
                self.publish_accounts()
            except Exception as error:
                logging.warning("Account sync FAIL: %s", safe_error(error))
            for provider, callback in (
                ("claude", self.poll_claude),
                *(
                    (account, lambda key=account: self.poll_codex(key))
                    for account in self.accounts
                ),
                ("zai", self.poll_zai),
            ):
                try:
                    callback()
                except Exception as error:
                    message = safe_error(error)
                    logging.error("%s FAIL: %s", provider.capitalize(), message)
                    self.update_provider(
                        provider, configured=True, error=message, last_error=iso_now()
                    )
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
            detail = (
                detail or parsed.get("error_description") or parsed.get("message") or ""
            )
        except Exception:
            pass
        endpoint = ""
        if error.url:
            parsed_url = urlsplit(error.url)
            endpoint = f" ({parsed_url.netloc}{parsed_url.path})"
        return f"HTTP {error.status}{endpoint}" + (f": {detail}" if detail else "")
    return str(error).replace("\n", " ")[:500]


def load_options() -> dict[str, Any]:
    defaults = {"interval_minutes": 2, "poll_on_start": True, "debug": False}
    try:
        defaults.update(load_json(OPTIONS_PATH))
    except FileNotFoundError:
        pass
    except Exception as error:
        logging.warning("Cannot read options: %s", safe_error(error))
    defaults["interval_minutes"] = max(2, min(1440, int(defaults["interval_minutes"])))
    return defaults


def next_poll_delay(monitor: Monitor, now: datetime | None = None) -> float:
    """Return regular interval or an earlier moment just after a quota reset."""
    current = now or utc_now()
    delay = float(int(monitor.options["interval_minutes"]) * 60)
    snapshot = monitor.snapshot()
    for provider in ("claude", *snapshot.get("accounts", {"codex": {}}), "zai"):
        state = snapshot.get(provider)
        if not isinstance(state, dict):
            continue
        for key in ("five_hour_reset", "5h_reset", "weekly_reset"):
            value = state.get(key)
            if not isinstance(value, str):
                continue
            normalized = normalize_iso(value)
            try:
                reset = datetime.fromisoformat(normalized) if normalized else None
            except ValueError:
                continue
            if reset is not None and reset > current:
                delay = min(delay, (reset - current).total_seconds() + 5)
    return max(1.0, delay)


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Usage Monitor</title>
<style>
body{font:15px system-ui,sans-serif;margin:0;background:#111827;color:#e5e7eb}main{max-width:760px;margin:auto;padding:24px}
h1{font-size:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}#codexAccounts{display:contents}.card{background:#1f2937;border-radius:12px;padding:18px}
.ok{color:#34d399}.bad{color:#fb7185}.muted{color:#9ca3af}button,label{display:inline-block;margin-top:10px}button{background:#03a9f4;color:#06131b;border:0;border-radius:7px;padding:9px 14px;font-weight:600;cursor:pointer}input{max-width:100%}pre{white-space:pre-wrap;word-break:break-word;background:#111827;padding:10px;border-radius:7px}code{color:#93c5fd}
</style></head><body><main>
<h1>AI Usage Monitor</h1><p class="muted">Файлы сохраняются только в защищённом каталоге <code>/data</code> add-on.</p>
<div class="grid"><section class="card"><h2>Claude</h2><div id="claude">Загрузка…</div><button onclick="claudeLogin()">Войти в Claude</button><div id="claudeLogin" hidden><p class="muted">Подтвердите вход на открывшейся странице (<a id="claudeLoginUrl" target="_blank" rel="noopener">ссылка</a>) и вставьте показанный там код.</p><input id="claudeCode" type="password" autocomplete="off" placeholder="Код со страницы Claude"><br><button onclick="claudeFinish()">Завершить вход</button></div><details><summary class="muted">Импорт файла</summary><p class="muted">Копия файла рабочего CLI перестаёт работать, как только CLI обновит токен, — лучше вход выше.</p><label>Файл <code>.claude/.credentials.json</code><br><input id="claudeFile" type="file" accept="application/json"></label><br><button onclick="upload('claude')">Импортировать</button></details></section>
<div id="codexAccounts"></div><section class="card"><h2>Добавить Codex</h2><input id="accountName" maxlength="60" placeholder="Название аккаунта"><br><button onclick="addAccount()">Добавить аккаунт</button></section>
<section class="card"><h2>z.ai</h2><div id="zai">Загрузка…</div><label>Ключ Coding Plan<br><input id="zaiKey" type="password" autocomplete="off"></label><br><button onclick="saveZai()">Сохранить ключ</button></section></div>
<p><button onclick="pollNow()">Обновить сейчас</button> <span id="message" class="muted"></span></p>
<script>
const api=p=>new URL('./api/'+p,location.href).toString();
function renderProvider(name,p){const el=document.getElementById(name);const cls=p.error?'bad':'ok';const usage=name==='claude'?`5h: ${p.five_hour_percent??'—'}%, неделя: ${p.weekly_percent??'—'}%`:`5h: ${p['5h_percent']??'—'}%, неделя: ${p.weekly_percent??'—'}%`;el.innerHTML=`<p class="${cls}">${p.configured?(p.error?'Ошибка':'Настроено'):'Не настроено'}</p><p>${usage}</p><p class="muted">Последнее обновление: ${p.last_ok??'—'}</p>${p.error?`<pre>${escapeHtml(p.error)}</pre>`:''}`}
function escapeHtml(v){const d=document.createElement('div');d.textContent=v;return d.innerHTML}
async function refresh(){const s=await (await fetch(api('status'))).json();renderProvider('claude',s.claude||{});renderAccounts(s);renderProvider('zai',s.zai||{});document.getElementById('message').textContent=s.polling?'Выполняется опрос…':''}
let accountSignature='';
function renderAccounts(s){const entries=Object.entries(s.accounts||{});const signature=JSON.stringify(s.accounts||{});if(signature!==accountSignature){accountSignature=signature;document.getElementById('codexAccounts').innerHTML=entries.map(([id,a])=>`<section class="card"><h2>${escapeHtml(a.name)}</h2><div id="${id}"></div><button onclick="codexLogin('${id}')">Войти</button><div id="${id}Login"></div><details><summary>Настройки аккаунта</summary><input id="${id}Name" maxlength="60"><button onclick="renameAccount('${id}')">Переименовать</button><p class="muted">Сенсоры: ai_usage_monitor_${id}_usage_…</p><details><summary>Импорт файла</summary><p class="muted">Используйте отдельный вход выше, чтобы не делить токены с CLI.</p><input id="${id}File" type="file" accept="application/json"><button onclick="upload('${id}')">Импортировать</button></details><button onclick="deleteAccount('${id}')">Удалить аккаунт</button></details></section>`).join('');for(const [id,a] of entries)document.getElementById(id+'Name').value=a.name;}for(const [id] of entries){renderProvider(id,s[id]||{});renderCodexLogin(s[id+'_login'],id)}}
async function accountAction(action,body){const r=await fetch(api('accounts/'+action),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();document.getElementById('message').textContent=j.error||j.message||'';await refresh();return r.ok}
async function addAccount(){const el=document.getElementById('accountName');if(await accountAction('add',{name:el.value}))el.value=''}
async function renameAccount(id){await accountAction('rename',{id,name:document.getElementById(id+'Name').value})}
async function deleteAccount(id){if(confirm('Удалить аккаунт, его сохранённые токены и сенсоры HA?'))await accountAction('delete',{id})}
async function upload(name){const f=document.getElementById(name+'File').files[0];if(!f){alert('Выберите JSON-файл');return}const r=await fetch(api('import/'+name),{method:'POST',headers:{'Content-Type':'application/json'},body:await f.text()});const j=await r.json();document.getElementById('message').textContent=j.message||j.error||'';await refresh()}
async function saveZai(){const key=document.getElementById('zaiKey').value.trim();if(!key){alert('Введите ключ Coding Plan');return}const r=await fetch(api('import/zai'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:key})});document.getElementById('zaiKey').value='';const j=await r.json();document.getElementById('message').textContent=j.message||j.error||'';await refresh()}
async function claudeLogin(){const j=await (await fetch(api('claude/login/start'),{method:'POST'})).json();document.getElementById('claudeLoginUrl').href=j.url;document.getElementById('claudeLogin').hidden=false;window.open(j.url,'_blank','noopener')}
async function claudeFinish(){const code=document.getElementById('claudeCode').value.trim();if(!code){alert('Вставьте код со страницы Claude');return}const r=await fetch(api('claude/login/finish'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});document.getElementById('claudeCode').value='';const j=await r.json();if(r.ok)document.getElementById('claudeLogin').hidden=true;document.getElementById('message').textContent=j.message||j.error||'';setTimeout(refresh,3000)}
function renderCodexLogin(l,name='codex'){const el=document.getElementById(name+'Login');if(!l){el.innerHTML='';return}if(l.status==='pending')el.innerHTML=`<p>Откройте <a href="${l.url}" target="_blank" rel="noopener">${l.url}</a> и введите код <b><code>${escapeHtml(l.user_code)}</code></b>. Панель обновится сама.</p>`;else if(l.status==='done')el.innerHTML='<p class="ok">Вход в Codex выполнен</p>';else el.innerHTML=`<pre>${escapeHtml(l.error||'Ошибка входа')}</pre>`}
async function codexLogin(name='codex'){const r=await fetch(api(name+'/login/start'),{method:'POST'});const j=await r.json();if(!r.ok){document.getElementById('message').textContent=j.error||'';return}renderCodexLogin(j,name);window.open(j.url,'_blank','noopener')}
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

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_IMPORT_SIZE:
            raise ValueError("Некорректный размер файла")
        value = json.loads(self.rfile.read(length).decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Файл должен содержать JSON-объект")
        return value

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
                threading.Thread(
                    target=self.monitor.poll_all, name="manual-poll", daemon=True
                ).start()
            self.send_json(
                HTTPStatus.ACCEPTED,
                {"message": "Опрос запущен" if started else "Опрос уже выполняется"},
            )
            return
        if any(
            path.endswith(f"/api/{slot}/login/start")
            for slot in self.monitor.snapshot()["accounts"]
        ):
            provider = path.rstrip("/").split("/")[-3]
            try:
                login = self.monitor.start_codex_login(provider)
            except Exception as error:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": safe_error(error)})
                return
            self.send_json(HTTPStatus.OK, login)
            return
        if path.endswith("/api/claude/login/start"):
            self.send_json(HTTPStatus.OK, {"url": self.monitor.start_claude_login()})
            return
        if path.endswith("/api/claude/login/finish"):
            try:
                code = self.read_json_body().get("code")
                if not isinstance(code, str):
                    raise ValueError("Вставьте код со страницы Claude")
                self.monitor.finish_claude_login(code)
            except Exception as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_error(error)})
                return
            threading.Thread(
                target=self.monitor.poll_all, name="login-poll", daemon=True
            ).start()
            self.send_json(
                HTTPStatus.OK, {"message": "Claude: вход выполнен, запущен опрос"}
            )
            return
        if any(
            path.endswith(f"/api/accounts/{action}")
            for action in ("add", "rename", "delete")
        ):
            try:
                value = self.read_json_body()
                if path.endswith("/add"):
                    account = self.monitor.add_codex_account(value.get("name"))
                elif path.endswith("/rename"):
                    account = value.get("id")
                    self.monitor.rename_codex_account(account, value.get("name"))
                else:
                    account = value.get("id")
                    self.monitor.delete_codex_account(account)
                threading.Thread(target=self.monitor.poll_all, daemon=True).start()
                self.send_json(
                    HTTPStatus.OK, {"id": account, "message": "Аккаунты обновлены"}
                )
            except Exception as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_error(error)})
            return
        provider = None
        if path.endswith("/api/import/claude"):
            provider = "claude"
        elif any(
            path.endswith(f"/api/import/{key}")
            for key in self.monitor.snapshot()["accounts"]
        ):
            provider = path.split("/")[-1]
        elif path.endswith("/api/import/zai"):
            provider = "zai"
        if provider is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            value = self.read_json_body()
            if provider == "claude":
                validate_claude_credentials(value)
                atomic_write_json(CLAUDE_PATH, value)
            elif provider.startswith("codex"):
                validate_codex_credentials(value)
                with self.monitor.lock:
                    if provider not in self.monitor.accounts:
                        raise ValueError("Аккаунт удалён")
                    with self.monitor.state_lock:
                        atomic_write_json(codex_path(provider), value)
                        self.monitor.codex_login_generation[provider] += 1
                        self.monitor.state.pop(f"{provider}_login", None)
            else:
                validate_zai_credentials(value)
                atomic_write_json(ZAI_PATH, {"api_key": zai_api_key(value)})
            self.monitor.update_provider(provider, configured=True, error=None)
            self.send_json(
                HTTPStatus.OK,
                {"message": f"{provider.capitalize()}: файл импортирован"},
            )
        except Exception as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": safe_error(error)})


def scheduler(monitor: Monitor) -> None:
    if monitor.options.get("poll_on_start", True):
        monitor.poll_all()
    while True:
        time.sleep(next_poll_delay(monitor))
        monitor.poll_all()


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options = load_options()
    logging.basicConfig(
        level=logging.DEBUG if options.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info(
        "AI Usage Monitor starting; interval=%s min", options["interval_minutes"]
    )
    monitor = Monitor(options)
    WebHandler.monitor = monitor
    threading.Thread(
        target=scheduler, args=(monitor,), name="scheduler", daemon=True
    ).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8099), WebHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
