import base64
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "ai-usage-monitor"
    / "rootfs"
    / "usr"
    / "local"
    / "bin"
    / "ai_usage_monitor.py"
)
SPEC = importlib.util.spec_from_file_location("ai_usage_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def jwt(payload):
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    )
    return f"header.{encoded}.signature"


class CredentialTests(unittest.TestCase):
    def test_claude_validation_accepts_export(self):
        monitor.validate_claude_credentials(
            {"claudeAiOauth": {"accessToken": "access", "refreshToken": "refresh"}}
        )

    def test_codex_validation_requires_account(self):
        with self.assertRaisesRegex(ValueError, "account_id"):
            monitor.validate_codex_credentials(
                {"tokens": {"access_token": "access", "refresh_token": "refresh"}}
            )

    def test_codex_client_id_comes_from_id_token(self):
        credentials = {
            "tokens": {
                "id_token": jwt({"aud": ["app_test_client"]}),
                "access_token": jwt({"exp": 4_000_000_000}),
                "refresh_token": "refresh",
                "account_id": "account",
            }
        }
        self.assertEqual(monitor.codex_client_id(credentials), "app_test_client")

    def test_zai_validation_accepts_direct_key(self):
        credentials = {"api_key": "zai_test"}
        monitor.validate_zai_credentials(credentials)
        self.assertEqual(monitor.zai_api_key(credentials), "zai_test")

    def test_zai_key_can_come_from_claude_settings(self):
        credentials = {"env": {"ANTHROPIC_AUTH_TOKEN": "zai_test"}}
        self.assertEqual(monitor.zai_api_key(credentials), "zai_test")


class RefreshTests(unittest.TestCase):
    def test_claude_does_not_refresh_valid_access_token(self):
        with patch.object(monitor.time, "time", return_value=1_000_000):
            self.assertFalse(
                monitor.claude_needs_refresh(
                    {
                        "expiresAt": 1_000_000_000 + 60 * 60 * 1000,
                        "refreshTokenExpiresAt": 1_000_000_000 + 60 * 1000,
                    }
                )
            )

    def test_claude_rotates_tokens(self):
        credentials = {
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "scopes": ["user:profile"],
            }
        }
        response = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "refresh_token_expires_in": 7200,
            "scope": "user:profile",
        }
        with patch.object(
            monitor, "http_request", return_value=(200, {}, response)
        ) as request:
            with patch.object(monitor, "atomic_write_json") as write:
                token = monitor.refresh_claude(credentials)
        self.assertEqual(token, "new-access")
        self.assertEqual(credentials["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertEqual(
            request.call_args.kwargs["json_body"]["grant_type"], "refresh_token"
        )
        write.assert_called_once()

    def test_codex_rotates_tokens(self):
        credentials = {
            "tokens": {
                "id_token": jwt({"aud": ["app_test_client"]}),
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "account_id": "account",
            }
        }
        response = {
            "id_token": jwt({"aud": ["app_test_client"]}),
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        }
        with patch.object(
            monitor, "http_request", return_value=(200, {}, response)
        ) as request:
            with patch.object(monitor, "atomic_write_json") as write:
                token = monitor.refresh_codex(credentials)
        self.assertEqual(token, "new-access")
        self.assertEqual(credentials["tokens"]["refresh_token"], "new-refresh")
        self.assertEqual(
            request.call_args.kwargs["json_body"]["client_id"], "app_test_client"
        )
        write.assert_called_once()


class HomeAssistantPublishingTests(unittest.TestCase):
    def test_publish_ha_update_uses_event_api(self):
        windows = {
            "5h": {
                "enabled": True,
                "used_percent": 12.5,
                "resets_at": "2026-09-08T10:00:00+00:00",
            },
            "weekly": {
                "enabled": True,
                "used_percent": 34.0,
                "resets_at": "2026-09-14T10:00:00+00:00",
            },
        }
        with patch.object(monitor, "SUPERVISOR_TOKEN", "secret"):
            with patch.object(
                monitor, "http_request", return_value=(200, {}, None)
            ) as request:
                monitor.publish_ha_update(
                    "codex",
                    "2026-09-08T09:00:00+00:00",
                    windows,
                    plan="plus",
                )

        self.assertEqual(
            request.call_args.args[0],
            f"{monitor.HA_API}/events/{monitor.HA_EVENT_UPDATE}",
        )
        self.assertEqual(request.call_args.kwargs["method"], "POST")
        self.assertEqual(
            request.call_args.kwargs["json_body"],
            {
                "provider": "codex",
                "updated_at": "2026-09-08T09:00:00+00:00",
                "windows": windows,
                "plan": "plus",
            },
        )


class ZaiUsageTests(unittest.TestCase):
    def test_parse_zai_token_windows(self):
        windows, plan = monitor.parse_zai_usage(
            {
                "data": {
                    "level": "pro",
                    "limits": [
                        {
                            "type": "TOKENS_LIMIT",
                            "unit": 3,
                            "percentage": 12,
                            "nextResetTime": 1_800_000_000_000,
                        },
                        {
                            "type": "TOKENS_LIMIT",
                            "unit": 6,
                            "percentage": 34,
                            "nextResetTime": 1_800_100_000_000,
                        },
                        {"type": "TIME_LIMIT", "unit": 5, "percentage": 5},
                    ],
                }
            }
        )
        self.assertEqual(plan, "pro")
        self.assertEqual(windows["5h"]["used_percent"], 12.0)
        self.assertEqual(windows["weekly"]["used_percent"], 34.0)
        self.assertEqual(windows["5h"]["resets_at"], "2027-01-15T08:00:00+00:00")

    def test_parse_zai_current_credit_windows(self):
        windows, plan = monitor.parse_zai_usage(
            {
                "data": {
                    "level": "lite",
                    "limits": [
                        {
                            "type": "CREDIT_LIMIT",
                            "unit": 3,
                            "usage": 2000,
                            "currentValue": 500,
                            "nextResetTime": "2026-09-08T18:00:00Z",
                        },
                        {
                            "type": "CREDIT_LIMIT",
                            "unit": 6,
                            "percentage": 94,
                            "remaining": 0,
                        },
                    ],
                }
            }
        )
        self.assertEqual(plan, "lite")
        self.assertEqual(windows["5h"]["used_percent"], 25.0)
        self.assertEqual(windows["weekly"]["used_percent"], 100.0)
        self.assertEqual(windows["5h"]["resets_at"], "2026-09-08T18:00:00+00:00")


class CodexUsageTests(unittest.TestCase):
    def test_limit_reached_forces_most_used_window_to_100(self):
        windows, plan = monitor.parse_codex_usage(
            {
                "plan_type": "plus",
                "rate_limit": {
                    "allowed": False,
                    "limit_reached": True,
                    "primary_window": {
                        "used_percent": 94,
                        "limit_window_seconds": 18_000,
                        "reset_at": 1_800_000_000,
                    },
                    "secondary_window": {
                        "used_percent": 32,
                        "limit_window_seconds": 604_800,
                        "reset_at": 1_800_100_000,
                    },
                },
            }
        )
        self.assertEqual(plan, "plus")
        self.assertEqual(windows["5h"]["used_percent"], 100.0)
        self.assertEqual(windows["weekly"]["used_percent"], 32.0)

    def test_available_window_keeps_reported_percent(self):
        windows, _ = monitor.parse_codex_usage(
            {
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 94,
                        "limit_window_seconds": 18_000,
                    },
                }
            }
        )
        self.assertEqual(windows["5h"]["used_percent"], 94.0)


class SchedulerTests(unittest.TestCase):
    class FakeMonitor:
        options = {"interval_minutes": 2}

        def __init__(self, state):
            self.state = state

        def snapshot(self):
            return self.state

    def test_poll_is_scheduled_just_after_reset(self):
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        fake = self.FakeMonitor({"codex": {"5h_reset": "2026-09-08T12:00:30+00:00"}})
        self.assertEqual(monitor.next_poll_delay(fake, now), 35.0)

    def test_past_reset_does_not_create_poll_loop(self):
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
        fake = self.FakeMonitor({"codex": {"5h_reset": "2026-09-08T11:59:30+00:00"}})
        self.assertEqual(monitor.next_poll_delay(fake, now), 120.0)


if __name__ == "__main__":
    unittest.main()
