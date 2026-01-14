from __future__ import annotations

import asyncio

from typed_json import JSON, JSONDict, coerce_str, obj_to_json, require_obj
from servant.defs import GlobalContext, ToolDef, SECRET_GITHUB_TOKEN

from github import Github
from github.GithubException import GithubException
from github.Issue import Issue
from github.Label import Label
from github.Repository import Repository

_REPO_FULL_NAME = "wabbit-corp/python-jeeves"


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
    return f"## {kind}\n\n" f"{description}\n"


def _try_get_labels(repo: Repository, label_names: list[str]) -> list[Label]:
    labels: list[Label] = []
    for ln in label_names:
        try:
            labels.append(repo.get_label(ln))
        except GithubException:
            # Label doesn't exist or no access. Fine. We can live without stickers.
            continue
    return labels


def _create_issue_sync(token: str, title: str, body: str, label_names: list[str]) -> JSONDict:
    gh = Github(token)
    repo: Repository = gh.get_repo(_REPO_FULL_NAME)

    labels = _try_get_labels(repo, label_names)

    try:
        if labels:
            issue: Issue = repo.create_issue(title="[Vox] " + title, body=body, labels=labels)
            used = [l.name for l in labels]
        else:
            issue = repo.create_issue(title="[Vox] " + title, body=body)
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
        "labels_applied": obj_to_json(used),
    }


async def file_bug_report(name: str, description: str = "", *, ctx: GlobalContext) -> JSONDict:
    token = coerce_str(ctx.secrets.get(SECRET_GITHUB_TOKEN), field="github.token", allow_empty=False)
    title = _normalize_title("Bug", name)
    body = _build_body("Bug Report", description)

    # PyGithub is sync, so do it off the event loop.
    return await asyncio.to_thread(_create_issue_sync, token, title, body, ["bug"])


async def file_feature_request(name: str, description: str = "", *, ctx: GlobalContext) -> JSONDict:
    token = coerce_str(ctx.secrets.get(SECRET_GITHUB_TOKEN), field="github.token", allow_empty=False)
    title = _normalize_title("Feature", name)
    body = _build_body("Feature Request", description)

    # Common label is "enhancement". Some repos use "feature". We'll try both.
    return await asyncio.to_thread(_create_issue_sync, token, title, body, ["enhancement", "feature"])


async def _file_bug_report_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    name = coerce_str(
        data.get("name"),
        field="name",
        allow_empty=False,
        allow_non_str=False,
    )
    description = coerce_str(
        data.get("description"),
        field="description",
        default="",
        allow_non_str=False,
    )
    return await file_bug_report(name=name, description=description, ctx=ctx)


async def _file_feature_request_tool(ctx: GlobalContext, obj: JSON) -> JSONDict:
    data = require_obj(obj)
    name = coerce_str(
        data.get("name"),
        field="name",
        allow_empty=False,
        allow_non_str=False,
    )
    description = coerce_str(
        data.get("description"),
        field="description",
        default="",
        allow_non_str=False,
    )
    return await file_feature_request(name=name, description=description, ctx=ctx)


file_bug_report_schema: ToolDef = ToolDef(
    name="file_bug_report",
    function=_file_bug_report_tool,
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
    function=_file_feature_request_tool,
    schema={
        "name": "file_feature_request",
        "description": "Create a GitHub issue (feature request) in wabbit-corp/python-jeeves.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short feature title."},
                "description": {
                    "type": "string",
                    "description": "Feature details and rationale.",
                },
            },
            "required": ["name"],
        },
    },
)
