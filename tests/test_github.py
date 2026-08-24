"""Tests for GitHub client: webhook, HMAC, payload parsing."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from cr_agent.github.client import (
    get_pr_diff,
    get_pr_info,
    parse_webhook_payload,
    post_pr_comment,
    verify_webhook_signature,
)


class TestWebhookSignature:
    def test_valid_signature(self):
        payload = b'{"action": "opened"}'
        secret = "my_webhook_secret"
        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        assert verify_webhook_signature(payload, expected, secret) is True

    def test_invalid_signature(self):
        payload = b'{"action": "opened"}'
        secret = "my_webhook_secret"
        assert verify_webhook_signature(payload, "sha256=invalid", secret) is False

    def test_empty_signature(self):
        assert verify_webhook_signature(b"payload", "", "secret") is False

    def test_empty_secret(self):
        assert verify_webhook_signature(b"payload", "sha256=abc", "") is False

    def test_tampered_payload(self):
        payload = b'{"action": "opened"}'
        secret = "my_webhook_secret"
        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        # Tamper with payload
        assert verify_webhook_signature(b'{"action": "closed"}', expected, secret) is False


class TestParseWebhookPayload:
    def test_parses_pr_opened(self):
        payload = json.dumps({
            "action": "opened",
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 42,
                "title": "Fix bug",
                "user": {"login": "developer"},
                "body": "This fixes the bug",
                "base": {"ref": "main"},
                "head": {"ref": "fix-branch"},
            }
        }).encode()
        result = parse_webhook_payload(payload)
        assert result is not None
        assert result["action"] == "opened"
        assert result["repo"] == "owner/repo"
        assert result["number"] == 42
        assert result["title"] == "Fix bug"
        assert result["author"] == "developer"

    def test_returns_none_for_non_pr(self):
        payload = json.dumps({
            "action": "opened",
            "issue": {"number": 1}
        }).encode()
        assert parse_webhook_payload(payload) is None

    def test_returns_none_for_invalid_json(self):
        assert parse_webhook_payload(b"not json") is None

    def test_handles_missing_fields(self):
        payload = json.dumps({
            "action": "opened",
            "pull_request": {"number": 1}
        }).encode()
        result = parse_webhook_payload(payload)
        assert result is not None
        assert result["title"] == ""
        assert result["author"] == ""


class TestGitHubClient:
    @patch("cr_agent.github.client.subprocess.run")
    def test_get_pr_diff_masks_secrets(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="diff --git a/file.py\n+token = ghp_1234567890abcdefghijklmnopqrstuv",
            returncode=0,
        )
        diff = get_pr_diff(42, "owner/repo", token="fake_token")
        assert "ghp_1234567890" not in diff
        assert "[REDACTED]" in diff

    @patch("cr_agent.github.client.subprocess.run")
    def test_get_pr_info_parses_json(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "number": 42,
                "title": "Fix bug",
                "author": {"login": "dev"},
                "body": "Fixes issue",
                "baseRefName": "main",
                "headRefName": "fix-branch",
            }),
            returncode=0,
        )
        info = get_pr_info(42, "owner/repo", token="fake_token")
        assert info.number == 42
        assert info.title == "Fix bug"
        assert info.author == "dev"
        assert info.base == "main"
        assert info.head == "fix-branch"

    @patch("cr_agent.github.client.subprocess.run")
    def test_post_pr_comment_creates_new(self, mock_run):
        # First call: list comments (no existing CR comment)
        # Second call: create new comment
        mock_run.side_effect = [
            MagicMock(stdout="[]", returncode=0),  # No existing comments
            MagicMock(stdout="", returncode=0),     # Comment created
        ]
        result = post_pr_comment(42, "owner/repo", "## Code Review Report\nAll good.", token="fake")
        assert result is True

    @patch("cr_agent.github.client.subprocess.run")
    def test_post_pr_comment_updates_existing(self, mock_run):
        # First call: find existing CR comment
        # Second call: update via API
        existing = json.dumps({
            "id": 12345,
            "body": "## Code Review Report\nOld review."
        })
        mock_run.side_effect = [
            MagicMock(stdout=existing, returncode=0),
            MagicMock(stdout="", returncode=0),
        ]
        result = post_pr_comment(42, "owner/repo", "## Code Review Report\nNew review.", token="fake")
        assert result is True
        # Verify the second call used PATCH (update, not create)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "PATCH" in second_call_args