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


def _mock_response(status_code=200, json_data=None, text=""):
    """创建 mock httpx.Response。"""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = text
    mock.json.return_value = json_data if json_data is not None else {}
    return mock


class TestGitHubClient:
    @patch("cr_agent.github.client.httpx.get")
    def test_get_pr_diff_masks_secrets(self, mock_get):
        mock_get.return_value = _mock_response(
            status_code=200,
            text="diff --git a/file.py\n+token = ghp_1234567890abcdefghijklmnopqrstuv",
        )
        diff = get_pr_diff(42, "owner/repo", token="fake_token")
        assert "ghp_1234567890" not in diff
        assert "[REDACTED]" in diff

    @patch("cr_agent.github.client.httpx.get")
    def test_get_pr_diff_raises_on_error(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404, text="Not Found")
        try:
            get_pr_diff(42, "owner/repo", token="fake_token")
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            assert "404" in str(e)

    @patch("cr_agent.github.client.httpx.get")
    def test_get_pr_info_parses_json(self, mock_get):
        mock_get.return_value = _mock_response(
            status_code=200,
            json_data={
                "number": 42,
                "title": "Fix bug",
                "user": {"login": "dev"},
                "body": "Fixes issue",
                "base": {"ref": "main"},
                "head": {"ref": "fix-branch"},
            },
        )
        info = get_pr_info(42, "owner/repo", token="fake_token")
        assert info.number == 42
        assert info.title == "Fix bug"
        assert info.author == "dev"
        assert info.base == "main"
        assert info.head == "fix-branch"

    @patch("cr_agent.github.client.httpx.get")
    def test_post_pr_comment_creates_new(self, mock_get):
        # List comments returns empty (no existing CR comment)
        mock_get.return_value = _mock_response(status_code=200, json_data=[])

        with patch("cr_agent.github.client.httpx.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=201)
            result = post_pr_comment(42, "owner/repo", "## Code Review Report\nAll good.", token="fake")
            assert result is True

    @patch("cr_agent.github.client.httpx.get")
    def test_post_pr_comment_updates_existing(self, mock_get):
        # List comments returns existing CR comment
        mock_get.return_value = _mock_response(
            status_code=200,
            json_data=[{
                "id": 12345,
                "body": "## Code Review Report\nOld review."
            }],
        )

        with patch("cr_agent.github.client.httpx.patch") as mock_patch:
            mock_patch.return_value = _mock_response(status_code=200)
            result = post_pr_comment(42, "owner/repo", "## Code Review Report\nNew review.", token="fake")
            assert result is True

    @patch("cr_agent.github.client.httpx.get")
    def test_post_pr_comment_no_token_still_works(self, mock_get):
        # List comments returns empty
        mock_get.return_value = _mock_response(status_code=200, json_data=[])

        with patch("cr_agent.github.client.httpx.post") as mock_post:
            mock_post.return_value = _mock_response(status_code=201)
            result = post_pr_comment(42, "owner/repo", "## Code Review Report\nAll good.", token=None)
            assert result is True
