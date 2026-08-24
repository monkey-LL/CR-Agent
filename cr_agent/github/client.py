"""GitHub：webhook 服务器、HMAC 验证、PR 操作。

学习重点：
  - HMAC webhook 验证：为什么需要以及如何实现（防止伪造的 webhook）
  - 通过 gh CLI 调用 GitHub API：无需 API 库，只需 subprocess
  - PR 评论的幂等性：更新已有评论 vs 创建新评论
  - Token 注入：通过 GH_TOKEN 环境变量传入，绝不出现在提示词或日志中

两种运行模式：
  1. Webhook 模式：FastAPI 服务器接收 GitHub webhook，触发代码审查
  2. CLI 模式：通过 PR 编号手动审查（用于测试/学习）

为什么用 gh CLI 而不是 PyGithub/requests？
  - gh 自动处理认证、分页、速率限制
  - 代码更少，依赖更少
  - Token 通过 GH_TOKEN 环境变量注入，不会出现在我们的代码中
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from dataclasses import dataclass

from cr_agent.security.sanitizer import mask_secrets
from cr_agent.security.env_sanitizer import build_safe_env


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
    """使用 gh CLI 获取 PR 的 diff。

    gh pr diff <number> --repo <owner/repo>
    """
    env = {"GH_TOKEN": token} if token else None
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        capture_output=True,
        text=True,
        timeout=30,
        env=build_safe_env(env),
    )
    diff = result.stdout
    return mask_secrets(diff)


def get_pr_info(pr_number: int, repo: str, token: str | None = None) -> PRInfo:
    """使用 gh CLI 获取 PR 元数据。

    gh pr view <number> --repo <owner/repo> --json number,title,author,body,baseRefName,headRefName
    """
    env = {"GH_TOKEN": token} if token else None
    result = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number), "--repo", repo,
            "--json", "number,title,author,body,baseRefName,headRefName",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=build_safe_env(env),
    )
    data = json.loads(result.stdout)
    return PRInfo(
        number=data.get("number", pr_number),
        repo=repo,
        title=data.get("title", ""),
        author=data.get("author", {}).get("login", "") if isinstance(data.get("author"), dict) else str(data.get("author", "")),
        body=data.get("body", ""),
        base=data.get("baseRefName", ""),
        head=data.get("headRefName", ""),
    )


def post_pr_comment(pr_number: int, repo: str, body: str, token: str | None = None) -> bool:
    """使用 gh CLI 在 PR 上发表评论。

    幂等性：检查是否已存在 CR Agent 的评论，若存在则更新，
    而不是在重新审查时创建重复评论。通过 "## Code Review Report" 标题来识别评论。
    """
    import os as _os

    env = {"GH_TOKEN": token} if token else None
    full_env = build_safe_env(env)

    # 检查已有的审查评论（幂等性）
    list_result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", repo,
         "--json", "comments", "--jq", ".comments[] | {id: .id, body: .body}"],
        capture_output=True,
        text=True,
        timeout=30,
        env=full_env,
    )

    # 通过标题查找已有的 CR Agent 评论
    existing_comment_id = None
    if list_result.returncode == 0 and list_result.stdout.strip():
        try:
            import json as _json
            comments = _json.loads(list_result.stdout) if list_result.stdout.strip().startswith("[") else None
            if comments is None:
                # 尝试逐行解析 JSON（jq 每行输出一个 JSON 对象）
                for line in list_result.stdout.strip().split("\n"):
                    if line.strip():
                        try:
                            c = _json.loads(line)
                            if "Code Review Report" in c.get("body", ""):
                                existing_comment_id = str(c.get("id", ""))
                                break
                        except _json.JSONDecodeError:
                            continue
            elif isinstance(comments, list):
                for c in comments:
                    if isinstance(c, dict) and "Code Review Report" in c.get("body", ""):
                        existing_comment_id = str(c.get("id", ""))
                        break
        except (_json.JSONDecodeError, TypeError):
            pass

    if existing_comment_id:
        # 更新已有评论
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/comments/{existing_comment_id}",
             "--method", "PATCH", "--field", f"body={body}"],
            capture_output=True,
            text=True,
            timeout=30,
            env=full_env,
        )
        return result.returncode == 0
    else:
        # 创建新评论
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
            capture_output=True,
            text=True,
            timeout=30,
            env=full_env,
        )
        return result.returncode == 0


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
