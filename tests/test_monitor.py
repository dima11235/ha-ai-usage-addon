import base64
import importlib.util
import json
import sys
import unittest
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
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
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
        with patch.object(monitor, "http_request", return_value=(200, {}, response)) as request:
            with patch.object(monitor, "atomic_write_json") as write:
                token = monitor.refresh_claude(credentials)
        self.assertEqual(token, "new-access")
        self.assertEqual(credentials["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertEqual(request.call_args.kwargs["json_body"]["grant_type"], "refresh_token")
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
        with patch.object(monitor, "http_request", return_value=(200, {}, response)) as request:
            with patch.object(monitor, "atomic_write_json") as write:
                token = monitor.refresh_codex(credentials)
        self.assertEqual(token, "new-access")
        self.assertEqual(credentials["tokens"]["refresh_token"], "new-refresh")
        self.assertEqual(request.call_args.kwargs["json_body"]["client_id"], "app_test_client")
        write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
