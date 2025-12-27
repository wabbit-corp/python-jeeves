from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from servant.json import JSONDict
from servant.defs import ToolDef

from github import Github
from github.GithubException import GithubException

from servant.secrets import SECRET_GITHUB_TOKEN  # type: ignore


_REPO_FULL_NAME = "wabbit-corp/python-jeeves"


def _get_token(ctx: Any) -> str:
    try:
        token = ctx.secrets[SECRET_GITHUB_TOKEN]
    except Exception as e:
        raise ValueError(
            f"Missing GitHub token in ctx.secrets[{SECRET_GITHUB_TOKEN!r}]"
        ) from e

    if not isinstance(token, str) or not token.strip():
        raise ValueError("GitHub token is empty/invalid in ctx.secrets.")

    return token.strip()


def _normalize_title(prefix: str, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("name must be non-empty.")
    # Keep titles readable and less likely to annoy GitHub UI.
    name = " ".join(name.split())
    title = f"{prefix}: {name}"
    # Soft cap (GitHub allows long titles, humans don't).
    return title[:200]


def _build_body(kind: str, description: str) -> str:
    description = (description or "").strip()
    if not description:
        description = "_No description provided._"

    # Keep it simple and template-ish without turning it into bureaucracy.
    return (
        f"## {kind}\n\n"
        f"{description}\n"
    )


def _try_get_labels(repo, label_names: List[str]):
    labels = []
    for ln in label_names:
        try:
            labels.append(repo.get_label(ln))
        except GithubException:
            # Label doesn't exist or no access. Fine. We can live without stickers.
            continue
    return labels


def _create_issue_sync(token: str, title: str, body: str, label_names: List[str]) -> JSONDict:
    gh = Github(token)
    repo = gh.get_repo(_REPO_FULL_NAME)

    labels = _try_get_labels(repo, label_names)

    try:
        if labels:
            issue = repo.create_issue(title='[Vox] ' + title, body=body, labels=labels)
            used = [l.name for l in labels]
        else:
            issue = repo.create_issue(title='[Vox] ' + title, body=body)
            used = []
    except GithubException as e:
        msg = (getattr(e, "data", {}) or {}).get("message") or str(e)
        raise RuntimeError(f"GitHub error creating issue: {msg}") from e

    return {
        "ok": True,
        "repo": _REPO_FULL_NAME,
        "issue_number": issue.number,
        "issue_url": issue.html_url,
        "title": issue.title,
        "labels_applied": used,
    }


async def file_bug_report(name: str, description: str = "", *, ctx: Any) -> JSONDict:
    token = _get_token(ctx)
    title = _normalize_title("Bug", name)
    body = _build_body("Bug Report", description)

    # PyGithub is sync, so do it off the event loop.
    return await asyncio.to_thread(_create_issue_sync, token, title, body, ["bug"])


async def file_feature_request(name: str, description: str = "", *, ctx: Any) -> JSONDict:
    token = _get_token(ctx)
    title = _normalize_title("Feature", name)
    body = _build_body("Feature Request", description)

    # Common label is "enhancement". Some repos use "feature". We'll try both.
    return await asyncio.to_thread(_create_issue_sync, token, title, body, ["enhancement", "feature"])


file_bug_report_schema: ToolDef = ToolDef(
    name="file_bug_report",
    function=lambda ctx, obj: file_bug_report(
        name=obj["name"],
        description=obj.get("description", ""),
        ctx=ctx,
    ),
    schema={
        "name": "file_bug_report",
        "description": "Create a GitHub issue (bug report) in wabbit-corp/python-jeeves.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short bug title."},
                "description": {"type": "string", "description": "Bug details."},
            },
            "required": ["name"],
        },
    },
)

file_feature_request_schema: ToolDef = ToolDef(
    name="file_feature_request",
    function=lambda ctx, obj: file_feature_request(
        name=obj["name"],
        description=obj.get("description", ""),
        ctx=ctx,
    ),
    schema={
        "name": "file_feature_request",
        "description": "Create a GitHub issue (feature request) in wabbit-corp/python-jeeves.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short feature title."},
                "description": {"type": "string", "description": "Feature details and rationale."},
            },
            "required": ["name"],
        },
    },
)
