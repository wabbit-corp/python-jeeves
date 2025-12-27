# ROOT/servant/modules/read_own_code.py

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from servant.json import JSONDict
from servant.defs import ToolDef

_MAX_READ_LINES = 250
_DEFAULT_LS_MAX_ENTRIES = 200
_DEFAULT_GREP_MAX_MATCHES = 200
_DEFAULT_GREP_MAX_FILES = 2000
_DEFAULT_MAX_LINE_LENGTH = 20_000  # cap absurdly long lines


def _repo_root() -> Path:
    """
    This file is: ROOT/servant/modules/read_own_code.py
    parents[2] -> ROOT
    """
    return Path(__file__).resolve().parents[2]


def _is_within_root(root: Path, target: Path) -> bool:
    try:
        root = root.resolve()
        target = target.resolve()
    except Exception:
        return False
    return target == root or root in target.parents


def _resolve_under_root(root: Path, rel_path: str) -> Path:
    if rel_path is None:
        rel_path = "."
    rel_path = rel_path.strip()

    p = Path(rel_path)
    if p.is_absolute() or rel_path.startswith("~"):
        raise ValueError("Absolute paths are not allowed.")

    candidate = (root / p).resolve()
    if not _is_within_root(root, candidate):
        raise ValueError("Path escapes repo root.")

    return candidate


def _ensure_py_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Path is not a file.")
    if path.suffix != ".py":
        raise ValueError("Only .py files are allowed.")


def _safe_read_lines(path: Path, start_line: int, end_line: int) -> Tuple[int, List[Dict[str, Any]]]:
    _ensure_py_file(path)

    if start_line < 1:
        raise ValueError("start_line must be >= 1.")
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line.")
    if (end_line - start_line + 1) > _MAX_READ_LINES:
        raise ValueError(f"Requested too many lines (max {_MAX_READ_LINES}).")

    out: List[Dict[str, Any]] = []
    total = 0

    with path.open("r", encoding="utf-8", errors="replace") as f:
        # We want total_lines too. So we read once; store only requested slice.
        for idx, raw in enumerate(f, start=1):
            total = idx
            if idx < start_line:
                continue
            if idx > end_line:
                # stop storing, but keep counting in a second loop
                break

            s = raw.rstrip("\n")
            if len(s) > _DEFAULT_MAX_LINE_LENGTH:
                s = s[:_DEFAULT_MAX_LINE_LENGTH] + "…"
            out.append({"no": idx, "text": s})

        # If we broke early, continue counting total lines without storing.
        for idx, _ in enumerate(f, start=total + 1):
            total = idx

    return total, out


def _ls_dir(path: Path, recursive: bool, max_entries: int) -> List[Dict[str, Any]]:
    if not path.exists():
        raise ValueError("Path does not exist.")
    if not path.is_dir():
        raise ValueError("Path must be a directory for ls.")

    max_entries = max(1, min(int(max_entries), 5000))

    def stat_entry(p: Path) -> Dict[str, Any]:
        try:
            st = p.stat()
            return {
                "path": str(p),
                "name": p.name,
                "is_dir": p.is_dir(),
                "size": st.st_size,
                "mtime_epoch": int(st.st_mtime),
            }
        except OSError:
            return {"path": str(p), "name": p.name, "is_dir": p.is_dir(), "error": "stat_failed"}

    out: List[Dict[str, Any]] = []

    if not recursive:
        for child in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            out.append(stat_entry(child))
            if len(out) >= max_entries:
                break
        return out

    skip_dirs = {".git", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        rp = Path(root)

        for d in sorted(dirs):
            out.append(stat_entry(rp / d))
            if len(out) >= max_entries:
                return out

        for fn in sorted(files):
            out.append(stat_entry(rp / fn))
            if len(out) >= max_entries:
                return out

    return out


def _iter_py_files(base: Path, max_files: int) -> List[Path]:
    max_files = max(1, min(int(max_files), 10000))

    if base.is_file():
        _ensure_py_file(base)
        return [base]

    if not base.exists():
        raise ValueError("Path does not exist.")
    if not base.is_dir():
        raise ValueError("Path must be a directory or a .py file.")

    skip_dirs = {".git", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}
    files: List[Path] = []

    for root, dirs, filenames in os.walk(base):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            files.append(Path(root) / name)
            if len(files) >= max_files:
                return files

    return files


def _grep_in_files(
    files: List[Path],
    pattern: str,
    *,
    regex: bool,
    ignore_case: bool,
    whole_word: bool,
    max_matches: int,
) -> List[Dict[str, Any]]:
    if not pattern:
        raise ValueError("pattern must be non-empty.")

    max_matches = max(1, min(int(max_matches), 5000))

    flags = re.MULTILINE
    if ignore_case:
        flags |= re.IGNORECASE

    rx_src = pattern if regex else re.escape(pattern)
    if whole_word:
        rx_src = r"\b" + rx_src + r"\b"

    try:
        rx = re.compile(rx_src, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}") from e

    matches: List[Dict[str, Any]] = []
    for fp in files:
        if fp.suffix != ".py":
            continue

        try:
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for i, raw in enumerate(f, start=1):
                    line = raw.rstrip("\n")
                    if len(line) > _DEFAULT_MAX_LINE_LENGTH:
                        line = line[:_DEFAULT_MAX_LINE_LENGTH] + "…"
                    if rx.search(line):
                        matches.append({"file": str(fp), "line": i, "text": line})
                        if len(matches) >= max_matches:
                            return matches
        except OSError:
            continue

    return matches


# -----------------------
# Tool entrypoints
# -----------------------

async def own_code_ls(path: str = ".", recursive: bool = False, max_entries: int = _DEFAULT_LS_MAX_ENTRIES) -> JSONDict:
    root = _repo_root()
    p = _resolve_under_root(root, path)
    items = _ls_dir(p, recursive=bool(recursive), max_entries=int(max_entries))
    return {"ok": True, "root": str(root), "path": str(p), "recursive": bool(recursive), "items": items}


async def own_code_read(path: str, start_line: int = 1, end_line: int = 250) -> JSONDict:
    root = _repo_root()
    p = _resolve_under_root(root, path)
    _ensure_py_file(p)

    start_line = int(start_line)
    end_line = int(end_line)
    total, lines = _safe_read_lines(p, start_line=start_line, end_line=end_line)

    return {
        "ok": True,
        "root": str(root),
        "path": str(p),
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total,
        "lines": lines,
    }


async def own_code_grep(
    pattern: str,
    path: str = ".",
    regex: bool = False,
    ignore_case: bool = False,
    whole_word: bool = False,
    max_matches: int = _DEFAULT_GREP_MAX_MATCHES,
    max_files: int = _DEFAULT_GREP_MAX_FILES,
) -> JSONDict:
    root = _repo_root()
    base = _resolve_under_root(root, path)

    files = _iter_py_files(base, max_files=int(max_files))
    matches = _grep_in_files(
        files,
        pattern,
        regex=bool(regex),
        ignore_case=bool(ignore_case),
        whole_word=bool(whole_word),
        max_matches=int(max_matches),
    )

    truncated = (len(matches) >= int(max_matches)) or (len(files) >= int(max_files))

    return {
        "ok": True,
        "root": str(root),
        "path": str(base),
        "pattern": pattern,
        "regex": bool(regex),
        "ignore_case": bool(ignore_case),
        "whole_word": bool(whole_word),
        "files_scanned": len(files),
        "matches": matches,
        "truncated": truncated,
    }


# -----------------------
# ToolDefs (split)
# -----------------------

own_code_ls_schema: ToolDef = ToolDef(
    name="own_code_ls",
    function=lambda ctx, obj: own_code_ls(
        path=obj.get("path", "."),
        recursive=obj.get("recursive", False),
        max_entries=obj.get("max_entries", _DEFAULT_LS_MAX_ENTRIES),
    ),
    schema={
        "name": "own_code_ls",
        "description": "List files/directories under repo ROOT only (ROOT = two directories above this module).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path under ROOT (no absolute paths).", "default": "."},
                "recursive": {"type": "boolean", "description": "Recurse into subdirectories.", "default": False},
                "max_entries": {"type": "integer", "description": "Cap the number of returned entries (1..5000).", "default": _DEFAULT_LS_MAX_ENTRIES},
            },
        },
    },
)

own_code_read_schema: ToolDef = ToolDef(
    name="own_code_read",
    function=lambda ctx, obj: own_code_read(
        path=obj["path"],
        start_line=obj.get("start_line", 1),
        end_line=obj.get("end_line", 250),
    ),
    schema={
        "name": "own_code_read",
        "description": "Read a .py file under repo ROOT by line range (max 250 lines per call).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to a .py file under ROOT."},
                "start_line": {"type": "integer", "description": "1-indexed start line (inclusive).", "default": 1},
                "end_line": {"type": "integer", "description": "1-indexed end line (inclusive). Must be within 250 lines of start_line.", "default": 250},
            },
            "required": ["path"],
        },
    },
)

own_code_grep_schema: ToolDef = ToolDef(
    name="own_code_grep",
    function=lambda ctx, obj: own_code_grep(
        pattern=obj["pattern"],
        path=obj.get("path", "."),
        regex=obj.get("regex", False),
        ignore_case=obj.get("ignore_case", False),
        whole_word=obj.get("whole_word", False),
        max_matches=obj.get("max_matches", _DEFAULT_GREP_MAX_MATCHES),
        max_files=obj.get("max_files", _DEFAULT_GREP_MAX_FILES),
    ),
    schema={
        "name": "own_code_grep",
        "description": "Search for a pattern across .py files under repo ROOT only.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Pattern to search for (literal unless regex=true)."},
                "path": {"type": "string", "description": "Relative path under ROOT to search (dir or .py file).", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as regex.", "default": False},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search.", "default": False},
                "whole_word": {"type": "boolean", "description": "Match whole words only.", "default": False},
                "max_matches": {"type": "integer", "description": "Cap returned matches (1..5000).", "default": _DEFAULT_GREP_MAX_MATCHES},
                "max_files": {"type": "integer", "description": "Cap scanned .py files (1..10000).", "default": _DEFAULT_GREP_MAX_FILES},
            },
            "required": ["pattern"],
        },
    },
)
