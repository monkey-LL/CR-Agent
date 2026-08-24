"""Tests for Web UI API server."""

import pytest
from fastapi.testclient import TestClient

from cr_agent.web.server import app


class TestWebAPI:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_health(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_list_rules(self, client):
        resp = client.get("/api/rules")
        assert resp.status_code == 200
        rules = resp.json()
        assert len(rules) >= 25  # We now have 28 rules
        for r in rules:
            assert "rule_id" in r
            assert "severity" in r
            assert "message" in r

    def test_review_empty_diff(self, client):
        resp = client.post("/api/review", json={"diff": ""})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_review_deterministic_only(self, client):
        diff = """--- a/auth.py
+++ b/auth.py
@@ -1,3 +1,5 @@
 def login():
+    token = "sk-1234567890abcdef"
+    print("debug")
     return True
"""
        resp = client.post("/api/review", json={"diff": diff, "use_llm": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "block"  # Hardcoded secret = blocker
        assert len(data["findings"]) >= 2  # Secret + print

    def test_review_clean_code(self, client):
        diff = """--- a/utils.py
+++ b/utils.py
@@ -1,3 +1,5 @@
 def helper():
+    x = 1 + 2
+    return x
"""
        resp = client.post("/api/review", json={"diff": diff, "use_llm": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["verdict"] == "approve"
        assert len(data["findings"]) == 0

    def test_review_markdown_output(self, client):
        diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 def main():
+    eval(user_input)
"""
        resp = client.post("/api/review", json={"diff": diff, "use_llm": False})
        assert resp.status_code == 200
        data = resp.json()
        assert "markdown" in data
        assert "Code Review Report" in data["markdown"]

    def test_index_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")