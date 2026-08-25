"""GitHub：webhook 服务器、HMAC 验证、PR 操作。

学习重点：
  - HMAC webhook 验证：为什么需要以及如何实现（防止伪造的 webhook）
  - 通过 GitHub REST API 获取 PR diff 和发表评论：不依赖 gh CLI
  - PR 评论的幂等性：更新已有评论 vs 创建新评论
  - Token 注入：通过 GH_TOKEN 环境变量传入，绝不出现在提示词或日志中

两种运行模式：
  1. Webhook 模式：FastAPI 服务器接收 GitHub webhook，触发代码审查
  2. CLI 模式：通过 PR 编号手动审查（用于测试/学习）

为什么用 GitHub REST API 而不是 gh CLI？
  - 不需要安装 gh CLI，降低部署门槛
  - httpx 是项目已有依赖，不引入新依赖
  - Token 通过 GH_TOKEN 环境变量注入，不会出现在代码中
  - 更容易测试（mock httpx 比 mock subprocess 更直接）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os

import httpx
from dataclasses import dataclass

from cr_agent.security.sanitizer import mask_secrets

GITHUB_API_BASE = "https://api.github.com"


@dataclass
class PRInfo:
    """从 GitHub 提取的 PR 元数据。"""

    number: int
    repo: str  # owner/repo 格式
    title: str = ""
    author: str = ""
    body: str = ""
    base: str = ""
    head: str = ""


def _build_headers(token: str | None) -> dict[str, str]:
    """构建 GitHub API 请求头。"""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """验证 GitHub webhook 的 HMAC-SHA256 签名。

    GitHub 发送 X-Hub-Signature-256: sha256=<hex_digest>
    我们用密钥重新计算 HMAC 并进行比较。

    为什么需要验证？没有验证的话，任何人都可以发送伪造的 webhook 来触发
   任意 PR 的审查，甚至更糟，窃取我们的审查结果。

    常量时间比较（hmac.compare_digest）可防止时序攻击。
    """
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def get_pr_diff(pr_number: int, repo: str, token: str | None = None) -> str:
    """通过 GitHub REST API 获取 PR 的 diff。

    GET /repos/{owner}/{repo}/pulls/{pr_number}
    Accept: application/vnd.github.v3.diff

    Raises:
        RuntimeError: API 请求失败（非 200 状态码）。
    """
    headers = _build_headers(token)
    headers["Accept"] = "application/vnd.github.v3.diff"

    url = f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}"
    resp = httpx.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API get_pr_diff failed (status {resp.status_code}): {resp.text[:200]}"
        )
    diff = resp.text
    return mask_secrets(diff)


def get_pr_info(pr_number: int, repo: str, token: str | None = None) -> PRInfo:
    """通过 GitHub REST API 获取 PR 元数据。

    GET /repos/{owner}/{repo}/pulls/{pr_number}
    返回 JSON，包含 number, title, user, body, base, head 等字段。

    Raises:
        RuntimeError: API 请求失败。
    """
    headers = _build_headers(token)
    url = f"{GITHUB_API_BASE}/repos/{repo}/pulls/{pr_number}"
    resp = httpx.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub API get_pr_info failed (status {resp.status_code}): {resp.text[:200]}"
        )
    data = resp.json()
    return PRInfo(
        number=data.get("number", pr_number),
        repo=repo,
        title=data.get("title", ""),
        author=data.get("user", {}).get("login", ""),
        body=data.get("body", ""),
        base=data.get("base", {}).get("ref", ""),
        head=data.get("head", {}).get("ref", ""),
    )


def post_pr_comment(pr_number: int, repo: str, body: str, token: str | None = None) -> bool:
    """通过 GitHub REST API 在 PR 上发表评论。

    幂等性：检查是否已存在 CR Agent 的评论，若存在则更新，
    而不是在重新审查时创建重复评论。通过 "## Code Review Report" 标题来识别评论。

    GET  /repos/{owner}/{repo}/issues/{pr_number}/comments  — 列出评论
    POST /repos/{owner}/{repo}/issues/{pr_number}/comments  — 创建评论
    PATCH /repos/{owner}/{repo}/issues/comments/{comment_id} — 更新评论
    """
    headers = _build_headers(token)

    # 检查已有的审查评论（幂等性）
    existing_comment_id = None
    list_url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{pr_number}/comments"
    try:
        list_resp = httpx.get(list_url, headers=headers, timeout=30)
        if list_resp.status_code == 200:
            for comment in list_resp.json():
                if "Code Review Report" in comment.get("body", ""):
                    existing_comment_id = comment.get("id")
                    break
    except Exception:
        pass  # 列出评论失败时降级为创建新评论

    if existing_comment_id:
        # 更新已有评论
        patch_url = f"{GITHUB_API_BASE}/repos/{repo}/issues/comments/{existing_comment_id}"
        resp = httpx.patch(patch_url, headers=headers, json={"body": body}, timeout=30)
        return resp.status_code == 200
    else:
        # 创建新评论
        resp = httpx.post(list_url, headers=headers, json={"body": body}, timeout=30)
        return resp.status_code in (200, 201)


def parse_webhook_payload(payload: bytes) -> dict | None:
    """解析 GitHub webhook 负载，如果是 PR 事件则提取 PR 信息。

    非 PR 事件返回 None。
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None

    if "pull_request" not in data:
        return None

    pr = data["pull_request"]
    action = data.get("action", "")
    repo = data.get("repository", {}).get("full_name", "")

    return {
        "action": action,
        "repo": repo,
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "author": pr.get("user", {}).get("login", ""),
        "body": pr.get("body", ""),
        "base": pr.get("base", {}).get("ref", ""),
        "head": pr.get("head", {}).get("ref", ""),
    }
