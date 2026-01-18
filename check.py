#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import io
import json
import keyword as py_keyword
import logging
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import token as token_mod
import tokenize as tokenize_mod
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class _BelowErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("check")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    handler_out = logging.StreamHandler(sys.stdout)
    handler_out.setLevel(logging.INFO)
    handler_out.addFilter(_BelowErrorFilter())
    handler_out.setFormatter(formatter)

    handler_err = logging.StreamHandler(sys.stderr)
    handler_err.setLevel(logging.ERROR)
    handler_err.setFormatter(formatter)

    logger.addHandler(handler_out)
    logger.addHandler(handler_err)
    logger.propagate = False
    return logger


@dataclass
class SummaryEntry:
    label: str
    state: str
    rc: int
    errors: int
    warnings: int
    log: str
    details: str = ""


@dataclass
class IssueLocation:
    path: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None


@dataclass
class RuffIssue:
    code: str
    message: str
    location: IssueLocation
    end_location: IssueLocation | None = None
    fix_available: bool = False
    url: str | None = None
    raw: str | None = None


@dataclass
class RuffFailure:
    message: str
    raw: str | None = None


@dataclass
class BlackIssue:
    path: str | None
    message: str
    raw: str | None = None


@dataclass
class BlackFailure:
    message: str
    raw: str | None = None


@dataclass
class ImportLinterIssue:
    contract: str
    message: str
    raw: str | None = None


@dataclass
class ImportLinterFailure:
    message: str
    raw: str | None = None


@dataclass
class MypyIssue:
    message: str
    severity: str
    code: str | None
    location: IssueLocation
    notes: list[str] = field(default_factory=list)
    raw: str | None = None


@dataclass
class MypyFailure:
    message: str
    raw: str | None = None


@dataclass
class PyrightIssue:
    message: str
    severity: str
    rule: str | None
    location: IssueLocation
    raw: str | None = None


@dataclass
class PyrightFailure:
    message: str
    raw: str | None = None


@dataclass
class PytestIssue:
    outcome: str
    nodeid: str
    message: str
    location: IssueLocation = field(default_factory=IssueLocation)
    raw: str | None = None


@dataclass
class PytestFailure:
    message: str
    raw: str | None = None


@dataclass
class UnittestIssue:
    outcome: str
    test: str
    message: str
    raw: str | None = None


@dataclass
class UnittestFailure:
    message: str
    raw: str | None = None


@dataclass
class DeptryIssue:
    code: str | None
    message: str
    location: IssueLocation = field(default_factory=IssueLocation)
    raw: str | None = None


@dataclass
class DeptryFailure:
    message: str
    raw: str | None = None


@dataclass
class VultureIssue:
    message: str
    location: IssueLocation
    raw: str | None = None


@dataclass
class VultureFailure:
    message: str
    raw: str | None = None


@dataclass
class SemgrepIssue:
    rule_id: str
    message: str
    severity: str
    location: IssueLocation
    metadata: dict[str, object] = field(default_factory=dict)
    raw: str | None = None


@dataclass
class SemgrepFailure:
    message: str
    raw: str | None = None


@dataclass
class BanditIssue:
    test_id: str
    message: str
    severity: str
    confidence: str | None
    location: IssueLocation
    details: list[str] = field(default_factory=list)
    raw: str | None = None


@dataclass
class BanditFailure:
    message: str
    raw: str | None = None


@dataclass
class PipAuditIssue:
    package: str
    installed_version: str
    vulnerability_id: str
    fix_versions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    raw: str | None = None


@dataclass
class PipAuditFailure:
    message: str
    raw: str | None = None


@dataclass
class DiffCoverFileIssue:
    path: str
    coverage: float | None
    missing_lines: list[int] = field(default_factory=list)
    raw: str | None = None


@dataclass
class DiffCoverSummaryIssue:
    missing_lines: int | None
    coverage: float | None
    message: str
    raw: str | None = None


@dataclass
class DiffCoverThresholdIssue:
    message: str
    required: int | None
    raw: str | None = None


@dataclass
class DiffCoverFailure:
    message: str
    raw: str | None = None


@dataclass
class CoverageReportIssue:
    total: float | None
    fail_under: int | None
    message: str
    raw: str | None = None


@dataclass
class CoverageReportFailure:
    message: str
    raw: str | None = None


@dataclass
class CoverageXmlIssue:
    total: float | None
    fail_under: int | None
    message: str
    raw: str | None = None


@dataclass
class CoverageXmlFailure:
    message: str
    raw: str | None = None


RuffResult = RuffIssue | RuffFailure
BlackResult = BlackIssue | BlackFailure
ImportLinterResult = ImportLinterIssue | ImportLinterFailure
MypyResult = MypyIssue | MypyFailure
PyrightResult = PyrightIssue | PyrightFailure
PytestResult = PytestIssue | PytestFailure
UnittestResult = UnittestIssue | UnittestFailure
DeptryResult = DeptryIssue | DeptryFailure
VultureResult = VultureIssue | VultureFailure
SemgrepResult = SemgrepIssue | SemgrepFailure
BanditResult = BanditIssue | BanditFailure
PipAuditResult = PipAuditIssue | PipAuditFailure
DiffCoverResult = DiffCoverFileIssue | DiffCoverSummaryIssue | DiffCoverThresholdIssue | DiffCoverFailure
CoverageReportResult = CoverageReportIssue | CoverageReportFailure
CoverageXmlResult = CoverageXmlIssue | CoverageXmlFailure

Issue = (
    RuffResult
    | BlackResult
    | ImportLinterResult
    | MypyResult
    | PyrightResult
    | PytestResult
    | UnittestResult
    | DeptryResult
    | VultureResult
    | SemgrepResult
    | BanditResult
    | PipAuditResult
    | DiffCoverResult
    | CoverageReportResult
    | CoverageXmlResult
)


@dataclass
class Config:
    root: Path
    target: str
    venv: Path
    python: Path
    bin_dir: Path
    exclude_csv: str
    run_coverage: bool
    coverage_fail_under: int
    run_diff_cover: bool
    keep_logs: bool
    preserve_color: bool
    keep_ansi_logs: bool
    show_output: bool
    jobs: int
    use_json: bool
    run_bandit: bool
    run_unittest: bool
    semgrep_config: str
    importlinter_config: str | None
    diff_cover_compare_branch: str | None
    env: dict[str, str]


@dataclass
class RunContext:
    config: Config
    log_dir: Path
    summary: list[SummaryEntry]
    issues: list[Issue] = field(default_factory=list)
    issues_by_tool: dict[str, list[Issue]] = field(default_factory=dict)
    status: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


ANSI_COLOR_CODES = {
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
}
ANSI_ATTR_CODES = {"bold": "1"}
ANSI_RESET = "\x1b[0m"


def _ansi_prefix(color: str | None, attrs: list[str] | None) -> str | None:
    codes: list[str] = []
    if attrs:
        for attr in attrs:
            code = ANSI_ATTR_CODES.get(attr)
            if code:
                codes.append(code)
    if color:
        color_code = ANSI_COLOR_CODES.get(color)
        if color_code:
            codes.append(color_code)
    if not codes:
        return None
    return f"\x1b[{';'.join(codes)}m"


def _supports_color(cfg: Config) -> bool:
    if not cfg.preserve_color:
        return False
    if sys.stdout.isatty() or sys.stderr.isatty():
        return True
    return os.environ.get("FORCE_COLOR") == "1"


def _c(cfg: Config, text: str, color: str | None = None, attrs: list[str] | None = None) -> str:
    if not _supports_color(cfg):
        return text
    prefix = _ansi_prefix(color, attrs)
    if not prefix:
        return text
    return f"{prefix}{text}{ANSI_RESET}"


def _highlight_python_line(cfg: Config, line: str) -> str:
    """Minimal highlighting: keywords + operators. Preserves whitespace."""
    if not _supports_color(cfg):
        return line

    src = line
    try:
        tokens = list(tokenize_mod.generate_tokens(io.StringIO(src + "\n").readline))
    except tokenize_mod.TokenError:
        return line

    out: list[str] = []
    cursor = 0

    for tok_type, tok_str, start, end, _ in tokens:
        if tok_type in {token_mod.NL, token_mod.NEWLINE, token_mod.ENDMARKER}:
            continue

        s_col = start[1]
        e_col = end[1]
        if s_col < cursor:
            return line

        out.append(src[cursor:s_col])
        segment = src[s_col:e_col]

        if tok_type == token_mod.NAME and tok_str in py_keyword.kwlist:
            out.append(_c(cfg, segment, "blue", attrs=["bold"]))
        elif tok_type == token_mod.OP:
            out.append(_c(cfg, segment, "cyan"))
        else:
            out.append(segment)

        cursor = e_col

    out.append(src[cursor:])
    return "".join(out)


ANSI_CSI_RE = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(rb"\x1b\][^\x1b]*\x1b\\")


def env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default) == "1"


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except ValueError:
        return None


def to_float(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int):
        return float(value)
    if isinstance(value, float):
        return value
    try:
        return float(value)
    except ValueError:
        return None


def get_str(data: dict[str, object], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str):
        return value
    return None


def get_int(data: dict[str, object], key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, int):
        return value
    return None


def get_dict(data: dict[str, object], key: str) -> dict[str, object] | None:
    value = data.get(key)
    if isinstance(value, dict):
        return value
    return None


def get_list(data: dict[str, object], key: str) -> list[object] | None:
    value = data.get(key)
    if isinstance(value, list):
        return value
    return None


def extract_json_payload(log_text: str) -> object | None:
    """Return the 'best' JSON payload embedded in log_text.

    Strategy: scan for JSON starts and pick the payload that ends farthest to the right.
    This tends to pick the real report (often last), not random bracket noise.
    """
    decoder = json.JSONDecoder()
    best_payload: object | None = None
    best_end = -1

    for match in re.finditer(r"[\[{]", log_text):
        start = match.start()
        try:
            payload, end = decoder.raw_decode(log_text[start:])
        except json.JSONDecodeError:
            continue

        end_index = start + end
        if end_index > best_end:
            best_end = end_index
            best_payload = payload

    return best_payload


def extract_json_lines(log_text: str) -> list[object]:
    """Parse JSON Lines (one JSON value per line). Ignore non-JSON lines."""
    out: list[object] = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "{[":
            continue
        try:
            out.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return out


def sanitize_label(label: str) -> str:
    sanitized = []
    for char in label:
        if char in " /":
            sanitized.append("_")
            continue
        if char.isalnum() or char in "._-":
            sanitized.append(char)
        else:
            sanitized.append("_")
    return "".join(sanitized)


def add_summary(
    ctx: RunContext,
    label: str,
    state: str,
    rc: int,
    errors: int,
    warnings: int,
    log: str,
    details: str = "",
) -> None:
    with ctx.lock:
        ctx.summary.append(SummaryEntry(label, state, rc, errors, warnings, log, details))


def strip_ansi(data: bytes) -> bytes:
    data = ANSI_OSC_RE.sub(b"", data)
    return ANSI_CSI_RE.sub(b"", data)


def run_with_pty(
    cmd: Sequence[str],
    log_path: Path,
    keep_ansi_logs: bool,
    tee_stdout: bool,
    env: dict[str, str],
) -> int:
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
    finally:
        try:
            os.close(slave_fd)
        except OSError:
            pass

    try:
        with log_path.open("wb") as log_file:
            while True:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if master_fd in readable:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        if tee_stdout:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        log_file.write(chunk if keep_ansi_logs else strip_ansi(chunk))
                        log_file.flush()
                        continue
                if proc.poll() is not None:
                    while True:
                        readable, _, _ = select.select([master_fd], [], [], 0)
                        if master_fd not in readable:
                            break
                        chunk = os.read(master_fd, 4096)
                        if not chunk:
                            break
                        if tee_stdout:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                        log_file.write(chunk if keep_ansi_logs else strip_ansi(chunk))
                    break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    return proc.wait()


def run_with_pipe(
    cmd: Sequence[str],
    log_path: Path,
    keep_ansi_logs: bool,
    tee_stdout: bool,
    env: dict[str, str],
) -> int:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    stdout = proc.stdout
    if stdout is None:
        raise RuntimeError("stdout pipe not available")
    with log_path.open("wb") as log_file:
        for chunk in iter(lambda: stdout.read(4096), b""):
            if tee_stdout:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            log_file.write(chunk if keep_ansi_logs else strip_ansi(chunk))
            log_file.flush()
    stdout.close()
    return proc.wait()


def read_log_text(log_path: Path) -> str:
    try:
        data = log_path.read_bytes()
    except OSError:
        return ""
    clean = strip_ansi(data)
    return clean.decode("utf-8", errors="replace")


def extract_counts(
    label: str,
    log_path: Path,
    coverage_fail_under: int,
) -> tuple[int, int]:
    log_text = read_log_text(log_path)
    errors = 0
    warnings = 0

    if label == "ruff":
        matches = re.findall(r"Found (\d+) errors?", log_text)
        if matches:
            errors = int(matches[-1])
        else:
            errors = sum(1 for line in log_text.splitlines() if re.match(r"^[A-Z]{1,4}[0-9]{3,4}\b", line))
    elif label == "black --check":
        match = re.search(r"(\d+) file(s)? would be reformatted", log_text)
        if match:
            errors = int(match.group(1))
        else:
            errors = sum(1 for line in log_text.splitlines() if "would reformat " in line)
    elif label == "import-linter":
        matches = re.findall(r"Contracts: \d+ kept, (\d+) broken\.", log_text)
        if matches and matches[-1].isdigit():
            errors = int(matches[-1])
        else:
            errors = len(re.findall(r"\bBROKEN\b", log_text))
    elif label == "mypy":
        matches = re.findall(r"Found (\d+) errors?", log_text)
        if matches:
            errors = int(matches[-1])
        else:
            errors = sum(1 for line in log_text.splitlines() if re.search(r"^[^:]+:\d+: error:", line))
    elif label in ("pyright", "basedpyright"):
        matches = re.findall(r"(\d+) errors?, (\d+) warnings?", log_text)
        if matches:
            errors = int(matches[-1][0])
            warnings = int(matches[-1][1])
        else:
            errors = len(re.findall(r"\berror\b", log_text, flags=re.IGNORECASE))
            warnings = len(re.findall(r"\bwarning\b", log_text, flags=re.IGNORECASE))
    elif label in ("pytest", "coverage run (pytest)"):
        summary_matches = re.findall(r"={2,} .* in [0-9.]+s ={2,}", log_text)
        if summary_matches:
            summary = summary_matches[-1]
            fail_nums = [int(n) for n in re.findall(r"(\d+) failed", summary)]
            error_nums = [int(n) for n in re.findall(r"(\d+) error(?:s)?", summary)]
            warn_nums = [int(n) for n in re.findall(r"(\d+) warnings?", summary)]
            errors = sum(fail_nums) + sum(error_nums)
            warnings = sum(warn_nums)
        else:
            errors = len(re.findall(r"\bfailed\b|\berror\b", log_text, flags=re.IGNORECASE))
            warnings = len(re.findall(r"\bwarning\b", log_text, flags=re.IGNORECASE))
    elif label == "unittest":
        fail_matches = re.findall(r"failures=(\d+)", log_text)
        error_matches = re.findall(r"errors=(\d+)", log_text)
        failures = int(fail_matches[-1]) if fail_matches else 0
        errs = int(error_matches[-1]) if error_matches else 0
        errors = failures + errs
    elif label == "deptry":
        matches = re.findall(r"Found (\d+) dependency issue(?:s)?", log_text)
        if not matches:
            matches = re.findall(r"Found (\d+) issue(?:s)?", log_text)
        if matches:
            errors = int(matches[-1])
    elif label == "vulture":
        warnings = sum(1 for line in log_text.splitlines() if re.match(r"^[^:]+:\d+: ", line))
    elif label == "semgrep":
        matches = re.findall(r"Ran \d+ rules on \d+ files: (\d+) findings", log_text)
        if matches:
            errors = int(matches[-1])
        else:
            matches = re.findall(r"(\d+) findings?", log_text)
            if matches:
                errors = int(matches[-1])
    elif label == "bandit":
        matches = re.findall(r"Total issues: (\d+)", log_text)
        if matches:
            errors = int(matches[-1])
    elif label == "pip-audit":
        matches = re.findall(r"Found (\d+) vulnerabilities?", log_text)
        if matches:
            errors = int(matches[-1])
        else:
            errors = sum(1 for line in log_text.splitlines() if re.match(r"^[A-Za-z0-9_.-]+\s+[0-9]", line))
    elif label == "diff-cover":
        matches = re.findall(r"Missing: (\d+) lines", log_text)
        if matches:
            warnings = int(matches[-1])
        else:
            warnings = len(re.findall(r"Missing lines", log_text))
        if re.search(
            r"^Failure\.|Coverage is below",
            log_text,
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            errors = 1
    elif label == "coverage report":
        # Informational only. The gate is branch coverage via "coverage xml".
        errors = 0
        warnings = 0
    else:
        errors = len(re.findall(r"\berror\b", log_text, flags=re.IGNORECASE))
        warnings = len(re.findall(r"\bwarning\b", log_text, flags=re.IGNORECASE))

    if errors < 0:
        errors = 0
    if warnings < 0:
        warnings = 0

    return errors, warnings


def issue_location(
    path: str | None,
    line: str | int | None = None,
    column: str | int | None = None,
    end_line: str | int | None = None,
    end_column: str | int | None = None,
) -> IssueLocation:
    return IssueLocation(
        path=path,
        line=to_int(line),
        column=to_int(column),
        end_line=to_int(end_line),
        end_column=to_int(end_column),
    )


def parse_ruff_issues(log_text: str) -> Sequence[RuffResult]:
    payload = extract_json_payload(log_text)
    if isinstance(payload, list):
        json_issues: list[RuffResult] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = get_str(item, "code") or "UNKNOWN"
            message = get_str(item, "message") or ""
            filename = get_str(item, "filename") or get_str(item, "path")
            location_dict = get_dict(item, "location") or {}
            end_location_dict = get_dict(item, "end_location")
            line_number = get_int(location_dict, "row")
            if line_number is None:
                line_number = get_int(location_dict, "line")
            column_number = get_int(location_dict, "column")
            if column_number is None:
                column_number = get_int(location_dict, "col")
            end_line_number = None
            end_column_number = None
            if end_location_dict:
                end_line_number = get_int(end_location_dict, "row")
                if end_line_number is None:
                    end_line_number = get_int(end_location_dict, "line")
                end_column_number = get_int(end_location_dict, "column")
                if end_column_number is None:
                    end_column_number = get_int(end_location_dict, "col")
            location = issue_location(filename, line_number, column_number)
            end_location = None
            if end_line_number is not None or end_column_number is not None:
                end_location = issue_location(filename, end_line_number, end_column_number)
            fix_available = "fix" in item and item.get("fix") is not None
            url = get_str(item, "url")
            json_issues.append(
                RuffIssue(
                    code=code,
                    message=message,
                    location=location,
                    end_location=end_location,
                    fix_available=fix_available,
                    url=url,
                )
            )
        return json_issues

    text_issues: list[RuffResult] = []
    pending_code: str | None = None
    pending_message: str | None = None

    inline_re = re.compile(r"^(.+?):(\d+):(\d+):\s+([A-Z]{1,4}[0-9]{3,4})\s+(.*)$")
    code_re = re.compile(r"^([A-Z]{1,4}[0-9]{3,4})\s+(?:\[[^\]]*]\s+)?(.*)$")
    location_re = re.compile(r"^\s*--> (.+?):(\d+):(\d+)\s*$")

    for text_line in log_text.splitlines():
        inline_match = inline_re.match(text_line)
        if inline_match:
            text_issues.append(
                RuffIssue(
                    code=inline_match.group(4),
                    message=inline_match.group(5).strip(),
                    location=issue_location(
                        inline_match.group(1),
                        inline_match.group(2),
                        inline_match.group(3),
                    ),
                    raw=text_line,
                )
            )
            continue

        code_match = code_re.match(text_line)
        if code_match:
            pending_code = code_match.group(1)
            pending_message = code_match.group(2).strip()
            continue

        location_match = location_re.match(text_line)
        if location_match and pending_code:
            text_issues.append(
                RuffIssue(
                    code=pending_code,
                    message=pending_message or "",
                    location=issue_location(
                        location_match.group(1),
                        location_match.group(2),
                        location_match.group(3),
                    ),
                    raw=text_line,
                )
            )
            pending_code = None
            pending_message = None

    if pending_code:
        text_issues.append(
            RuffIssue(
                code=pending_code,
                message=pending_message or "",
                location=issue_location(None),
                raw=pending_message,
            )
        )

    return text_issues


def parse_black_issues(log_text: str) -> Sequence[BlackResult]:
    issues: list[BlackResult] = []
    reformat_re = re.compile(r"would reformat (.+)$")
    summary_re = re.compile(r"\d+ file(s)? would be reformatted")
    summary_line = None
    for line in log_text.splitlines():
        match = reformat_re.search(line)
        if match:
            issues.append(
                BlackIssue(
                    path=match.group(1).strip(),
                    message="File would be reformatted",
                    raw=line,
                )
            )
            continue
        if summary_re.search(line):
            summary_line = line.strip()
    if not issues and summary_line:
        issues.append(BlackIssue(path=None, message=summary_line, raw=summary_line))
    return issues


def parse_import_linter_issues(log_text: str) -> Sequence[ImportLinterResult]:
    issues: list[ImportLinterResult] = []
    broken_re = re.compile(r"^(.*?)\s+BROKEN$")
    summary_re = re.compile(r"Contracts: \d+ kept, (\d+) broken\.")
    for line in log_text.splitlines():
        match = broken_re.match(line.strip())
        if match:
            issues.append(
                ImportLinterIssue(
                    contract=match.group(1),
                    message="Contract broken",
                    raw=line,
                )
            )
    if not issues:
        summary_match = summary_re.search(log_text)
        if summary_match:
            broken_count = to_int(summary_match.group(1)) or 0
            if broken_count > 0:
                issues.append(
                    ImportLinterIssue(
                        contract="summary",
                        message=f"{broken_count} contracts broken",
                        raw=summary_match.group(0),
                    )
                )
    return issues


def parse_mypy_issues(log_text: str) -> Sequence[MypyResult]:
    json_lines = extract_json_lines(log_text)
    # Declare once to avoid mypy no-redef across branches.
    notes: list[str] = []
    if json_lines:
        issues: list[MypyIssue] = []
        for item in json_lines:
            if not isinstance(item, dict):
                continue
            message = get_str(item, "message") or ""
            severity = get_str(item, "severity") or "error"
            code = get_str(item, "code")
            path = get_str(item, "file") or get_str(item, "path")
            line_number = get_int(item, "line")
            column_number = get_int(item, "column")
            end_line = get_int(item, "end_line")
            if end_line is None:
                end_line = get_int(item, "endLine")
            end_col = get_int(item, "end_column")
            if end_col is None:
                end_col = get_int(item, "endColumn")
            notes = []
            note_hint = get_str(item, "hint")
            if note_hint:
                notes.append(note_hint)
            issues.append(
                MypyIssue(
                    message=message,
                    severity=severity,
                    code=code,
                    location=issue_location(path, line_number, column_number, end_line, end_col),
                    notes=notes,
                )
            )
        return issues

    payload = extract_json_payload(log_text)
    items: list[object] | None = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        payload_items = get_list(payload, "errors") or get_list(payload, "results")
        if payload_items is not None:
            items = payload_items

    if items is not None:
        json_issues: list[MypyIssue] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            message = get_str(item, "message") or ""
            severity = get_str(item, "severity") or "error"
            code = get_str(item, "code")
            path = get_str(item, "file") or get_str(item, "path")
            line_number = get_int(item, "line")
            column_number = get_int(item, "column")
            notes = []
            hints = get_list(item, "hints") or get_list(item, "notes")
            if hints:
                for note in hints:
                    if isinstance(note, str):
                        notes.append(note)
            json_issues.append(
                MypyIssue(
                    message=message,
                    severity=severity,
                    code=code,
                    location=issue_location(path, line_number, column_number),
                    notes=notes,
                )
            )
        return json_issues

    text_issues: list[MypyIssue] = []
    error_re = re.compile(r"^(.+?):(\d+)(?::(\d+))?: error: (.*?)(?:\s+\[([^\]]+)\])?$")
    note_re = re.compile(r"^(.+?):(\d+)(?::(\d+))?: note: (.*)$")

    for text_line in log_text.splitlines():
        match = error_re.match(text_line)
        if match:
            issue = MypyIssue(
                message=match.group(4).strip(),
                severity="error",
                code=match.group(5),
                location=issue_location(match.group(1), match.group(2), match.group(3)),
                raw=text_line,
            )
            text_issues.append(issue)
            continue

        note_match = note_re.match(text_line)
        if note_match and text_issues:
            text_issues[-1].notes.append(note_match.group(4).strip())

    return text_issues


def parse_pyright_issues(log_text: str) -> Sequence[PyrightResult]:
    payload = extract_json_payload(log_text)
    diagnostics: list[object] | None = None
    if isinstance(payload, dict):
        diagnostics = get_list(payload, "generalDiagnostics") or get_list(payload, "diagnostics")

    if diagnostics is not None:
        json_issues: list[PyrightIssue] = []
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            severity = get_str(item, "severity") or "error"
            message = get_str(item, "message") or ""
            rule = get_str(item, "rule")
            path = get_str(item, "file")
            range_data = get_dict(item, "range") or {}
            start = get_dict(range_data, "start") or {}
            end = get_dict(range_data, "end") or {}
            line_number = get_int(start, "line")
            if line_number is not None:
                line_number += 1
            column_number = get_int(start, "character")
            if column_number is not None:
                column_number += 1
            end_line_number = get_int(end, "line")
            if end_line_number is not None:
                end_line_number += 1
            end_column_number = get_int(end, "character")
            if end_column_number is not None:
                end_column_number += 1
            json_issues.append(
                PyrightIssue(
                    message=message,
                    severity=severity,
                    rule=rule,
                    location=issue_location(
                        path,
                        line_number,
                        column_number,
                        end_line_number,
                        end_column_number,
                    ),
                )
            )
        return json_issues

    text_issues: list[PyrightIssue] = []
    diag_re = re.compile(r"^\s*(.+?):(\d+):(\d+)\s+-\s+(error|warning):\s+(.*?)(?:\s+\(([^)]+)\))?$")
    for text_line in log_text.splitlines():
        match = diag_re.match(text_line)
        if match:
            text_issues.append(
                PyrightIssue(
                    message=match.group(5).strip(),
                    severity=match.group(4).lower(),
                    rule=match.group(6),
                    location=issue_location(match.group(1), match.group(2), match.group(3)),
                    raw=text_line,
                )
            )
    return text_issues


def parse_pytest_issues(log_text: str, log_path: Path | None = None) -> Sequence[PytestResult]:
    if log_path is not None:
        junit_path = log_path.with_suffix(".junit.xml")
        if junit_path.is_file():
            return parse_pytest_junit(junit_path)
    return _parse_pytest_text(log_text)


def parse_pytest_junit(junit_path: Path) -> list[PytestResult]:
    try:
        xml_text = junit_path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(xml_text)
    except Exception as exc:
        return [PytestFailure(message=f"failed to parse junit xml: {exc}", raw=None)]

    issues: list[PytestResult] = []
    for testcase in root.iter("testcase"):
        classname = testcase.attrib.get("classname", "")
        name = testcase.attrib.get("name", "")
        nodeid = f"{classname}::{name}" if classname else name

        for tag in ("failure", "error"):
            elem = testcase.find(tag)
            if elem is None:
                continue
            msg = (elem.attrib.get("message") or "").strip()
            text = (elem.text or "").strip()
            message = msg or (text.splitlines()[0] if text else "Test failed")
            file_ = testcase.attrib.get("file")
            line_ = testcase.attrib.get("line")
            issues.append(
                PytestIssue(
                    outcome=tag.upper(),
                    nodeid=nodeid,
                    message=message,
                    location=issue_location(file_, line_),
                    raw=text or None,
                )
            )
    return issues


def _parse_pytest_text(log_text: str) -> Sequence[PytestResult]:
    issues: list[PytestResult] = []
    summary_re = re.compile(r"^(FAILED|ERROR)\s+(.+?)(?:\s+-\s+(.*))?$")
    collecting_re = re.compile(r"^ERROR collecting (.+)$")

    for line in log_text.splitlines():
        collecting_match = collecting_re.match(line)
        if collecting_match:
            issues.append(
                PytestIssue(
                    outcome="ERROR",
                    nodeid=collecting_match.group(1).strip(),
                    message="Collection error",
                    location=issue_location(collecting_match.group(1).strip()),
                    raw=line,
                )
            )
            continue

        match = summary_re.match(line)
        if not match:
            continue
        outcome = match.group(1)
        nodeid = match.group(2).strip()
        message = match.group(3).strip() if match.group(3) else "Test failed"
        location = issue_location(None)
        if "::" in nodeid:
            path, _ = nodeid.split("::", 1)
            location = issue_location(path)
        else:
            location = issue_location(nodeid)
        issues.append(
            PytestIssue(
                outcome=outcome,
                nodeid=nodeid,
                message=message,
                location=location,
                raw=line,
            )
        )

    return issues


def parse_unittest_issues(log_text: str) -> Sequence[UnittestResult]:
    issues: list[UnittestResult] = []
    issue_re = re.compile(r"^(FAIL|ERROR):\s+(.+)$")
    for line in log_text.splitlines():
        match = issue_re.match(line.strip())
        if match:
            issues.append(
                UnittestIssue(
                    outcome=match.group(1),
                    test=match.group(2).strip(),
                    message="Unittest failure",
                    raw=line,
                )
            )
    return issues


def parse_deptry_issues(log_text: str) -> Sequence[DeptryResult]:
    payload = extract_json_payload(log_text)
    if isinstance(payload, list):
        json_issues: list[DeptryIssue] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = get_str(item, "code")
            message = get_str(item, "message") or ""
            path = get_str(item, "path") or get_str(item, "file")
            line_number = get_int(item, "line")
            column_number = get_int(item, "column")
            json_issues.append(
                DeptryIssue(
                    code=code,
                    message=message,
                    location=issue_location(path, line_number, column_number),
                )
            )
        return json_issues

    text_issues: list[DeptryIssue] = []
    detail_re = re.compile(r"^(.+?):(\d+)(?::(\d+))?:\s*([A-Z]{1,4}[0-9]{3})\s+(.*)$")
    code_re = re.compile(r"^([A-Z]{1,4}[0-9]{3})\s+(.*)$")
    for text_line in log_text.splitlines():
        detail_match = detail_re.match(text_line.strip())
        if detail_match:
            text_issues.append(
                DeptryIssue(
                    code=detail_match.group(4),
                    message=detail_match.group(5).strip(),
                    location=issue_location(
                        detail_match.group(1),
                        detail_match.group(2),
                        detail_match.group(3),
                    ),
                    raw=text_line,
                )
            )
            continue
        code_match = code_re.match(text_line.strip())
        if code_match:
            text_issues.append(
                DeptryIssue(
                    code=code_match.group(1),
                    message=code_match.group(2).strip(),
                    raw=text_line,
                )
            )
    return text_issues


def parse_vulture_issues(log_text: str) -> Sequence[VultureResult]:
    issues: list[VultureResult] = []
    detail_re = re.compile(r"^(.+?):(\d+):\s+(.*)$")
    for line in log_text.splitlines():
        match = detail_re.match(line.strip())
        if match:
            issues.append(
                VultureIssue(
                    message=match.group(3).strip(),
                    location=issue_location(match.group(1), match.group(2)),
                    raw=line,
                )
            )
    return issues


def parse_semgrep_issues(log_text: str) -> Sequence[SemgrepResult]:
    payload = extract_json_payload(log_text)
    if isinstance(payload, dict):
        results = get_list(payload, "results")
        if results is not None:
            json_issues: list[SemgrepIssue] = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                rule_id = get_str(item, "check_id") or "unknown"
                path = get_str(item, "path")
                start = get_dict(item, "start") or {}
                end = get_dict(item, "end") or {}
                line_number = get_int(start, "line")
                column_number = get_int(start, "col")
                if column_number is None:
                    column_number = get_int(start, "column")
                end_line_number = get_int(end, "line")
                end_column_number = get_int(end, "col")
                if end_column_number is None:
                    end_column_number = get_int(end, "column")
                extra = get_dict(item, "extra") or {}
                message = get_str(extra, "message") or ""
                severity = get_str(extra, "severity") or "error"
                metadata_raw = get_dict(extra, "metadata") or {}
                metadata: dict[str, object] = {}
                for key, value in metadata_raw.items():
                    metadata[key] = value
                json_issues.append(
                    SemgrepIssue(
                        rule_id=rule_id,
                        message=message,
                        severity=severity,
                        location=issue_location(
                            path,
                            line_number,
                            column_number,
                            end_line_number,
                            end_column_number,
                        ),
                        metadata=metadata,
                    )
                )
            return json_issues

    text_issues: list[SemgrepIssue] = []
    inline_re = re.compile(r"^\s*(.+?):(\d+):(\d+):\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
    table_re = re.compile(
        r"^\s*(.+?):(\d+):(\d+)\s+(error|warning|info)\s+(\S+)\s+(.*)$",
        re.IGNORECASE,
    )
    for text_line in log_text.splitlines():
        inline_match = inline_re.match(text_line)
        if inline_match:
            text_issues.append(
                SemgrepIssue(
                    rule_id=inline_match.group(4),
                    message=inline_match.group(5).strip(),
                    severity="error",
                    location=issue_location(
                        inline_match.group(1),
                        inline_match.group(2),
                        inline_match.group(3),
                    ),
                    raw=text_line,
                )
            )
            continue

        table_match = table_re.match(text_line)
        if table_match:
            text_issues.append(
                SemgrepIssue(
                    rule_id=table_match.group(5),
                    message=table_match.group(6).strip(),
                    severity=table_match.group(4).lower(),
                    location=issue_location(
                        table_match.group(1),
                        table_match.group(2),
                        table_match.group(3),
                    ),
                    raw=text_line,
                )
            )
    return text_issues


def parse_bandit_issues(log_text: str) -> Sequence[BanditResult]:
    payload = extract_json_payload(log_text)
    if isinstance(payload, dict):
        results = get_list(payload, "results")
        if results is not None:
            json_issues: list[BanditIssue] = []
            for item in results:
                if not isinstance(item, dict):
                    continue
                test_id = get_str(item, "test_id") or "B000"
                message = get_str(item, "issue_text") or ""
                severity = get_str(item, "issue_severity") or "HIGH"
                confidence = get_str(item, "issue_confidence")
                path = get_str(item, "filename")
                line_number = get_int(item, "line_number")
                column_number = get_int(item, "col_offset")
                end_column_number = get_int(item, "end_col_offset")
                if column_number is not None:
                    column_number += 1
                if end_column_number is not None:
                    end_column_number += 1
                details: list[str] = []
                more_info = get_str(item, "more_info")
                if more_info:
                    details.append(more_info)
                json_issues.append(
                    BanditIssue(
                        test_id=test_id,
                        message=message,
                        severity=severity,
                        confidence=confidence,
                        location=issue_location(
                            path,
                            line_number,
                            column_number,
                            line_number,
                            end_column_number,
                        ),
                        details=details,
                    )
                )
            return json_issues

    text_issues: list[BanditIssue] = []
    current: BanditIssue | None = None
    issue_re = re.compile(r">> Issue: \[([^\]]+)\]\s+(.*)$")
    sev_re = re.compile(r"^\s*Severity:\s+(\w+)\s+Confidence:\s+(\w+)")
    loc_re = re.compile(r"^\s*Location:\s+(.+?):(\d+)")

    def flush_current() -> None:
        if current is not None:
            text_issues.append(current)

    for text_line in log_text.splitlines():
        issue_match = issue_re.match(text_line)
        if issue_match:
            flush_current()
            current = BanditIssue(
                test_id=issue_match.group(1),
                message=issue_match.group(2).strip(),
                severity="HIGH",
                confidence=None,
                location=issue_location(None),
                raw=text_line,
            )
            continue

        if current is None:
            continue

        sev_match = sev_re.match(text_line)
        if sev_match:
            current.severity = sev_match.group(1)
            current.confidence = sev_match.group(2)
            continue

        loc_match = loc_re.match(text_line)
        if loc_match:
            current.location = issue_location(loc_match.group(1), loc_match.group(2))
            continue

        stripped = text_line.strip()
        if stripped:
            current.details.append(stripped)

    flush_current()
    return text_issues


def parse_pip_audit_issues(log_text: str) -> Sequence[PipAuditResult]:
    payload = extract_json_payload(log_text)
    if isinstance(payload, dict):
        dependencies = get_list(payload, "dependencies")
        if dependencies is not None:
            json_issues: list[PipAuditIssue] = []
            for dep in dependencies:
                if not isinstance(dep, dict):
                    continue
                package = get_str(dep, "name") or "unknown"
                installed_version = get_str(dep, "version") or ""
                vulns = get_list(dep, "vulns") or []
                for vuln in vulns:
                    if not isinstance(vuln, dict):
                        continue
                    vulnerability_id = get_str(vuln, "id") or "unknown"
                    json_fix_versions: list[str] = []
                    for fix in get_list(vuln, "fix_versions") or []:
                        if isinstance(fix, str):
                            json_fix_versions.append(fix)
                    aliases: list[str] = []
                    for alias in get_list(vuln, "aliases") or []:
                        if isinstance(alias, str):
                            aliases.append(alias)
                    description = get_str(vuln, "description") or get_str(vuln, "details")
                    json_issues.append(
                        PipAuditIssue(
                            package=package,
                            installed_version=installed_version,
                            vulnerability_id=vulnerability_id,
                            fix_versions=json_fix_versions,
                            aliases=aliases,
                            description=description,
                        )
                    )
            return json_issues

    if isinstance(payload, list):
        legacy_json_issues: list[PipAuditIssue] = []
        for vuln in payload:
            if not isinstance(vuln, dict):
                continue
            package = get_str(vuln, "name") or get_str(vuln, "package") or "unknown"
            installed_version = get_str(vuln, "version") or get_str(vuln, "installed_version") or ""
            vulnerability_id = get_str(vuln, "id") or get_str(vuln, "vulnerability_id") or "unknown"
            legacy_fix_versions: list[str] = []
            for fix in get_list(vuln, "fix_versions") or []:
                if isinstance(fix, str):
                    legacy_fix_versions.append(fix)
            legacy_json_issues.append(
                PipAuditIssue(
                    package=package,
                    installed_version=installed_version,
                    vulnerability_id=vulnerability_id,
                    fix_versions=legacy_fix_versions,
                )
            )
        return legacy_json_issues

    text_issues: list[PipAuditIssue] = []
    table_re = re.compile(r"^([A-Za-z0-9_.-]+)\s+([0-9][^\s]*)\s+([A-Za-z0-9_.-]+)\s*(.*)$")
    for text_line in log_text.splitlines():
        stripped = text_line.strip()
        if not stripped:
            continue
        if stripped.startswith("Found ") or stripped.startswith("No known vulnerabilities"):
            continue
        if stripped.startswith("Collecting ") or stripped.startswith("Auditing "):
            continue
        if stripped.startswith("name ") and "version" in stripped:
            continue
        match = table_re.match(stripped)
        if not match:
            continue
        package = match.group(1)
        installed_version = match.group(2)
        vulnerability_id = match.group(3)
        text_fix_versions: list[str] = []
        if match.group(4):
            text_fix_versions.append(match.group(4).strip())
        text_issues.append(
            PipAuditIssue(
                package=package,
                installed_version=installed_version,
                vulnerability_id=vulnerability_id,
                fix_versions=text_fix_versions,
                raw=text_line,
            )
        )
    return text_issues


def parse_line_ranges(text: str) -> list[int]:
    out: list[int] = []
    for part in (piece.strip() for piece in text.split(",") if piece.strip()):
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = to_int(start_text)
            end = to_int(end_text)
            if start is None or end is None:
                continue
            out.extend(range(min(start, end), max(start, end) + 1))
        else:
            value = to_int(part)
            if value is not None:
                out.append(value)
    return sorted(set(out))


def parse_diff_cover_issues(
    log_text: str,
    log_path: Path | None = None,
    fail_under: int | None = None,
) -> Sequence[DiffCoverResult]:
    issues: list[DiffCoverResult] = []
    json_path = None
    if log_path is not None:
        json_path = log_path.parent / "diff-cover.json"
    if json_path is not None and json_path.is_file():
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            payload = None

        if isinstance(payload, dict):
            src_stats = payload.get("src_stats")
            if isinstance(src_stats, dict):
                for path, stats in src_stats.items():
                    if not isinstance(path, str) or not isinstance(stats, dict):
                        continue
                    pct = to_float(stats.get("percent_covered"))
                    lines_raw = stats.get("violation_lines")
                    lines: list[int] = []
                    if isinstance(lines_raw, list):
                        for item in lines_raw:
                            if isinstance(item, int):
                                lines.append(item)
                    if lines:
                        issues.append(
                            DiffCoverFileIssue(
                                path=path,
                                coverage=pct,
                                missing_lines=lines,
                            )
                        )
            total_violations = to_int(payload.get("total_num_violations"))
            total_pct = to_float(payload.get("total_percent_covered"))
            issues.append(
                DiffCoverSummaryIssue(
                    missing_lines=total_violations,
                    coverage=total_pct,
                    message="Diff coverage summary",
                )
            )
            if fail_under is not None and total_pct is not None and total_pct < fail_under:
                issues.append(
                    DiffCoverThresholdIssue(
                        message="Diff coverage below threshold",
                        required=fail_under,
                        raw=f"total={total_pct:.1f}%, fail-under={fail_under}%",
                    )
                )
            return issues
    # Fall back to parsing text output.
    file_re = re.compile(r"^(.+?) \((\d+(?:\.\d+)?)%\): Missing lines (.+)$")
    for line in log_text.splitlines():
        match = file_re.match(line.strip())
        if match:
            issues.append(
                DiffCoverFileIssue(
                    path=match.group(1),
                    coverage=to_float(match.group(2)),
                    missing_lines=parse_line_ranges(match.group(3)),
                    raw=line,
                )
            )
    missing_match = re.search(r"Missing:\s+(\d+) lines", log_text)
    coverage_match = re.search(r"Coverage:\s+(\d+(?:\.\d+)?)%", log_text)
    if missing_match or coverage_match:
        issues.append(
            DiffCoverSummaryIssue(
                missing_lines=to_int(missing_match.group(1)) if missing_match else None,
                coverage=to_float(coverage_match.group(1)) if coverage_match else None,
                message="Diff coverage summary",
                raw=missing_match.group(0) if missing_match else None,
            )
        )
    threshold_match = re.search(r"Coverage is below\s+(\d+)%", log_text)
    if threshold_match:
        issues.append(
            DiffCoverThresholdIssue(
                message="Diff coverage below threshold",
                required=to_int(threshold_match.group(1)),
                raw=threshold_match.group(0),
            )
        )
    elif re.search(r"^Failure\.", log_text, re.MULTILINE):
        issues.append(
            DiffCoverThresholdIssue(
                message="Diff coverage below threshold",
                required=None,
                raw="Failure.",
            )
        )
    return issues


def parse_coverage_report_issues(log_text: str, coverage_fail_under: int) -> Sequence[CoverageReportResult]:
    issues: list[CoverageReportResult] = []
    failure_match = re.search(
        r"Coverage failure: total of (\d+(?:\.\d+)?) is less than fail-under=(\d+)",
        log_text,
    )
    if failure_match:
        issues.append(
            CoverageReportIssue(
                total=to_float(failure_match.group(1)),
                fail_under=to_int(failure_match.group(2)),
                message="Coverage below threshold",
                raw=failure_match.group(0),
            )
        )
        return issues

    total_match = re.search(
        r"^TOTAL\s+.*\s(\d+(?:\.\d+)?)%$",
        log_text,
        flags=re.MULTILINE,
    )
    if total_match:
        total = total_match.group(1)
        total_value = to_int(total.split(".", maxsplit=1)[0])
        if total_value is not None and total_value < coverage_fail_under:
            issues.append(
                CoverageReportIssue(
                    total=to_float(total),
                    fail_under=coverage_fail_under,
                    message="Coverage below threshold",
                    raw=total_match.group(0),
                )
            )
    return issues


@dataclass
class CoberturaTotals:
    line_rate: float | None
    branch_rate: float | None
    lines_covered: int | None
    lines_valid: int | None
    branches_covered: int | None
    branches_valid: int | None


def read_cobertura_totals(xml_path: Path) -> CoberturaTotals | None:
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return None

    def _f(name: str) -> float | None:
        value = root.attrib.get(name)
        return to_float(value) if value is not None else None

    def _i(name: str) -> int | None:
        value = root.attrib.get(name)
        return to_int(value) if value is not None else None

    return CoberturaTotals(
        line_rate=_f("line-rate"),
        branch_rate=_f("branch-rate"),
        lines_covered=_i("lines-covered"),
        lines_valid=_i("lines-valid"),
        branches_covered=_i("branches-covered"),
        branches_valid=_i("branches-valid"),
    )


def parse_coverage_xml_issues(
    log_text: str,
    coverage_fail_under: int,
    log_path: Path | None = None,
) -> Sequence[CoverageXmlResult]:
    issues: list[CoverageXmlResult] = []
    xml_path = None
    if log_path is not None:
        candidate = log_path.parent / "coverage.xml"
        if candidate.is_file():
            xml_path = candidate

    if xml_path is not None:
        totals = read_cobertura_totals(xml_path)
        if totals and totals.branch_rate is not None:
            branch_pct = totals.branch_rate * 100.0
            if branch_pct < coverage_fail_under:
                issues.append(
                    CoverageXmlIssue(
                        total=branch_pct,
                        fail_under=coverage_fail_under,
                        message="Branch coverage below threshold",
                        raw=f"branch={branch_pct:.2f}%, fail-under={coverage_fail_under}%",
                    )
                )
            return issues

        issues.append(
            CoverageXmlFailure(
                message="coverage xml produced no readable branch coverage",
                raw=str(xml_path),
            )
        )
        return issues

    failure_match = re.search(
        r"Coverage failure: total of (\d+(?:\.\d+)?) is less than fail-under=(\d+)",
        log_text,
    )
    if failure_match:
        issues.append(
            CoverageXmlIssue(
                total=to_float(failure_match.group(1)),
                fail_under=to_int(failure_match.group(2)),
                message="Coverage below threshold",
                raw=failure_match.group(0),
            )
        )
        return issues

    if re.search(r"Coverage failure:", log_text):
        issues.append(
            CoverageXmlIssue(
                total=None,
                fail_under=coverage_fail_under,
                message="Coverage below threshold",
                raw=log_text,
            )
        )
    return issues


def parse_issues(label: str, log_path: Path, coverage_fail_under: int) -> Sequence[Issue]:
    log_text = read_log_text(log_path)

    if label == "ruff":
        return parse_ruff_issues(log_text)
    if label == "black --check":
        return parse_black_issues(log_text)
    if label == "import-linter":
        return parse_import_linter_issues(log_text)
    if label == "mypy":
        return parse_mypy_issues(log_text)
    if label in ("pyright", "basedpyright"):
        return parse_pyright_issues(log_text)
    if label in ("pytest", "coverage run (pytest)"):
        return parse_pytest_issues(log_text, log_path)
    if label == "unittest":
        return parse_unittest_issues(log_text)
    if label == "deptry":
        return parse_deptry_issues(log_text)
    if label == "vulture":
        return parse_vulture_issues(log_text)
    if label == "semgrep":
        return parse_semgrep_issues(log_text)
    if label == "bandit":
        return parse_bandit_issues(log_text)
    if label == "pip-audit":
        return parse_pip_audit_issues(log_text)
    if label == "diff-cover":
        return parse_diff_cover_issues(log_text, log_path, coverage_fail_under)
    if label == "coverage report":
        return parse_coverage_report_issues(log_text, coverage_fail_under)
    if label == "coverage xml":
        return parse_coverage_xml_issues(log_text, coverage_fail_under, log_path)

    return []


def normalize_severity(value: str) -> str:
    lowered = value.lower()
    if lowered in {"warning", "warn"}:
        return "warning"
    if lowered in {"info", "note"}:
        return "warning"
    return "error"


def issue_severity(issue: Issue) -> str:
    if isinstance(issue, (VultureIssue, DiffCoverFileIssue, DiffCoverSummaryIssue)):
        return "warning"
    if isinstance(issue, SemgrepIssue):
        return normalize_severity(issue.severity)
    if isinstance(issue, BanditIssue):
        if issue.severity.lower() in {"low", "medium"}:
            return "warning"
        return "error"
    if isinstance(issue, (MypyIssue, PyrightIssue)):
        return normalize_severity(issue.severity)
    return "error"


def count_issues(issues: Sequence[Issue]) -> tuple[int, int]:
    errors = 0
    warnings = 0
    for issue in issues:
        if issue_severity(issue) == "warning":
            warnings += 1
        else:
            errors += 1
    return errors, warnings


def format_location(location: IssueLocation | None) -> str:
    if location is None or not location.path:
        return "-"
    text = location.path
    if location.line is not None:
        text += f":{location.line}"
        if location.column is not None:
            text += f":{location.column}"
    return text


def _pretty_path(cfg: Config, raw: str) -> str:
    """Prefer repo-relative paths when possible."""
    try:
        p = Path(raw)
        root = cfg.root.resolve()
        if p.is_absolute():
            rp = p.resolve()
            try:
                return str(rp.relative_to(root))
            except Exception:
                return str(rp)
        return raw
    except Exception:
        return raw


def _cache_key(cfg: Config, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = cfg.root / p
    try:
        return str(p.resolve())
    except Exception:
        return str(p)


def _read_source_lines(cfg: Config, path: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    key = _cache_key(cfg, path)
    if key in cache:
        return cache[key]

    candidates: list[Path] = []
    p = Path(path)
    candidates.append(p)
    if not p.is_absolute():
        candidates.append(cfg.root / p)

    for cand in candidates:
        try:
            if cand.is_file():
                text = cand.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
                cache[key] = lines
                return lines
        except Exception:
            continue

    cache[key] = None
    return None


def _normalize_col(col: int | None) -> int | None:
    if col is None:
        return None
    return col if col > 0 else 1


def _format_span_location(
    cfg: Config,
    path: str | None,
    line: int | None,
    col: int | None,
    end_line: int | None,
    end_col: int | None,
) -> str:
    if not path:
        return "-"
    pretty = _pretty_path(cfg, path)
    if line is None:
        return pretty

    out = f"{pretty}:{line}"
    if col is None:
        return out

    out += f":{col}"
    if end_line == line and end_col is not None and end_col != col:
        out += f"-{end_col}"
    elif end_line is not None and end_col is not None and (end_line != line):
        out += f"-{end_line}:{end_col}"
    return out


def _issue_span(issue: Issue) -> tuple[str | None, int | None, int | None, int | None, int | None]:
    """Return (path, line, col, end_line, end_col) when known."""
    if isinstance(issue, RuffIssue):
        path = issue.location.path
        line = issue.location.line
        col = _normalize_col(issue.location.column)
        if issue.end_location:
            return (path, line, col, issue.end_location.line, _normalize_col(issue.end_location.column))
        return (path, line, col, None, None)

    if isinstance(issue, DiffCoverFileIssue):
        return (issue.path, None, None, None, None)

    loc = getattr(issue, "location", None)
    if isinstance(loc, IssueLocation):
        return (
            loc.path,
            loc.line,
            _normalize_col(loc.column),
            loc.end_line,
            _normalize_col(loc.end_column),
        )

    if isinstance(issue, BlackIssue) and issue.path:
        return (issue.path, None, None, None, None)

    return (None, None, None, None, None)


def _issue_sort_key(issue: Issue) -> tuple[str, int, int, str]:
    path, line, col, _, _ = _issue_span(issue)
    code_value = getattr(issue, "code", "")
    code = code_value if isinstance(code_value, str) else ""
    if not code:
        rule_value = getattr(issue, "rule", "")
        code = rule_value if isinstance(rule_value, str) else ""
    return (path or "", line or 0, col or 0, code)


def _render_source_block(
    cfg: Config,
    source_cache: dict[str, list[str] | None],
    path: str,
    line_no: int,
    col: int | None,
    end_line: int | None,
    end_col: int | None,
    severity: str,
    context: int = 0,
    tabsize: int = 4,
) -> list[str]:
    lines = _read_source_lines(cfg, path, source_cache)
    if not lines:
        return []

    if line_no < 1 or line_no > len(lines):
        return []

    start_line = max(1, line_no - context)
    end_line = min(len(lines), line_no + context)
    width = len(str(end_line))

    block: list[str] = []

    raw_target = lines[line_no - 1]
    is_python = path.endswith(".py")

    for ln in range(start_line, end_line + 1):
        raw = lines[ln - 1].expandtabs(tabsize)
        shown = _highlight_python_line(cfg, raw) if (is_python and ln == line_no) else raw
        prefix = f"  {ln:>{width}} | "
        block.append(prefix + shown)

        if ln == line_no and col is not None:
            col0 = max(1, col)
            if end_line is not None and end_line != line_no:
                end0 = len(raw_target) + 1
            elif end_col is not None and end_col > col0:
                end0 = end_col
            else:
                end0 = col0 + 1

            col0 = min(col0, len(raw_target) + 1)
            end0 = min(end0, len(raw_target) + 1)

            disp_start = len(raw_target[: col0 - 1].expandtabs(tabsize))
            disp_end = len(raw_target[: end0 - 1].expandtabs(tabsize))
            span = max(1, disp_end - disp_start)

            caret = "^" + ("~" * (span - 1))
            caret_color = "red" if severity == "error" else "yellow"
            caret = _c(cfg, caret, caret_color, attrs=["bold"])

            caret_prefix = f"  {'':>{width}} | "
            block.append(caret_prefix + (" " * disp_start) + caret)

    return block


def _wrap_message(text: str, width: int = 100, max_lines: int = 12) -> list[str]:
    cleaned = text.replace("\u00a0", " ").rstrip()
    if not cleaned:
        return []
    out: list[str] = []
    for raw_line in cleaned.splitlines() or [""]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        out.extend(textwrap.wrap(raw_line, width=width, subsequent_indent="  "))
        if len(out) >= max_lines:
            break
    if len(out) >= max_lines:
        out[-1] = out[-1].rstrip("…") + "…"
    return out


def format_issue(label: str, issue: Issue) -> str:
    location: IssueLocation | None = None
    code = "-"
    message = ""
    extras: list[str] = []

    if isinstance(issue, RuffIssue):
        location = issue.location
        code = issue.code
        message = issue.message
        if issue.fix_available:
            extras.append("fixable=true")
        if issue.url:
            extras.append(f"url={issue.url}")
    elif isinstance(issue, RuffFailure):
        message = issue.message
    elif isinstance(issue, BlackIssue):
        if issue.path:
            location = issue_location(issue.path)
        code = "reformat"
        message = issue.message
    elif isinstance(issue, BlackFailure):
        message = issue.message
    elif isinstance(issue, ImportLinterIssue):
        code = issue.contract
        message = issue.message
    elif isinstance(issue, ImportLinterFailure):
        message = issue.message
    elif isinstance(issue, MypyIssue):
        location = issue.location
        code = issue.code or "-"
        message = issue.message
        if issue.notes:
            extras.append(f"notes={'; '.join(issue.notes)}")
    elif isinstance(issue, MypyFailure):
        message = issue.message
    elif isinstance(issue, PyrightIssue):
        location = issue.location
        code = issue.rule or "-"
        message = issue.message
    elif isinstance(issue, PyrightFailure):
        message = issue.message
    elif isinstance(issue, PytestIssue):
        location = issue.location
        code = issue.outcome
        message = issue.message
        extras.append(f"nodeid={issue.nodeid}")
    elif isinstance(issue, PytestFailure):
        message = issue.message
    elif isinstance(issue, UnittestIssue):
        code = issue.outcome
        message = issue.message
        extras.append(f"test={issue.test}")
    elif isinstance(issue, UnittestFailure):
        message = issue.message
    elif isinstance(issue, DeptryIssue):
        location = issue.location
        code = issue.code or "-"
        message = issue.message
    elif isinstance(issue, DeptryFailure):
        message = issue.message
    elif isinstance(issue, VultureIssue):
        location = issue.location
        message = issue.message
    elif isinstance(issue, VultureFailure):
        message = issue.message
    elif isinstance(issue, SemgrepIssue):
        location = issue.location
        code = issue.rule_id
        message = issue.message
        for key in sorted(issue.metadata):
            extras.append(f"{key}={issue.metadata[key]}")
    elif isinstance(issue, SemgrepFailure):
        message = issue.message
    elif isinstance(issue, BanditIssue):
        location = issue.location
        code = issue.test_id
        message = issue.message
        extras.append(f"bandit_severity={issue.severity}")
        if issue.confidence:
            extras.append(f"confidence={issue.confidence}")
        if issue.details:
            extras.append(f"details={'; '.join(issue.details)}")
    elif isinstance(issue, BanditFailure):
        message = issue.message
    elif isinstance(issue, PipAuditIssue):
        code = issue.vulnerability_id
        message = "Vulnerability detected"
        extras.append(f"package={issue.package}")
        extras.append(f"installed_version={issue.installed_version}")
        if issue.fix_versions:
            extras.append(f"fix_versions={','.join(issue.fix_versions)}")
        if issue.aliases:
            extras.append(f"aliases={','.join(issue.aliases)}")
        if issue.description:
            extras.append(f"description={issue.description}")
    elif isinstance(issue, PipAuditFailure):
        message = issue.message
    elif isinstance(issue, DiffCoverFileIssue):
        location = issue_location(issue.path)
        code = "missing-lines"
        message = "Missing lines"
        if issue.coverage is not None:
            extras.append(f"coverage={issue.coverage}")
        if issue.missing_lines:
            extras.append(f"missing_lines={','.join(str(num) for num in issue.missing_lines)}")
    elif isinstance(issue, DiffCoverSummaryIssue):
        code = "summary"
        message = issue.message
        if issue.missing_lines is not None:
            extras.append(f"missing_lines={issue.missing_lines}")
        if issue.coverage is not None:
            extras.append(f"coverage={issue.coverage}")
    elif isinstance(issue, DiffCoverThresholdIssue):
        code = "threshold"
        message = issue.message
        if issue.required is not None:
            extras.append(f"required={issue.required}")
    elif isinstance(issue, DiffCoverFailure):
        message = issue.message
    elif isinstance(issue, CoverageReportIssue):
        code = "fail-under"
        message = issue.message
        if issue.total is not None:
            extras.append(f"total={issue.total}")
        if issue.fail_under is not None:
            extras.append(f"fail_under={issue.fail_under}")
    elif isinstance(issue, CoverageReportFailure):
        message = issue.message
    elif isinstance(issue, CoverageXmlIssue):
        code = "fail-under"
        message = issue.message
        if issue.total is not None:
            extras.append(f"total={issue.total}")
        if issue.fail_under is not None:
            extras.append(f"fail_under={issue.fail_under}")
    elif isinstance(issue, CoverageXmlFailure):
        message = issue.message

    severity = issue_severity(issue)
    metadata_text = f" ({', '.join(extras)})" if extras else ""
    return f"{format_location(location)} | {label} | {severity} | {code} | {message}{metadata_text}"


def print_parsed_issues(ctx: RunContext, label: str, issues: Sequence[Issue]) -> None:
    if not issues:
        return
    logger = logging.getLogger("check")
    logger.info("")
    logger.info("==> parsed issues (%s)", label)
    for issue in issues:
        logger.info(format_issue(label, issue))


def compact_message(text: str, limit: int = 500) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: max(0, limit - 1)] + "…"


def tool_sort_key(label: str) -> int:
    order = [
        "ruff",
        "black --check",
        "import-linter",
        "mypy",
        "basedpyright",
        "pyright",
        "pyright/basedpyright",
        "coverage run (pytest)",
        "pytest",
        "coverage report",
        "coverage xml",
        "diff-cover",
        "unittest",
        "deptry",
        "vulture",
        "semgrep",
        "bandit",
        "pip-audit",
        "coverage",
    ]
    try:
        return order.index(label)
    except ValueError:
        return 999


def print_unified_errors(ctx: RunContext) -> None:
    logger = logging.getLogger("check")
    cfg = ctx.config

    with ctx.lock:
        issues_by_tool = dict(ctx.issues_by_tool)
        summary = list(ctx.summary)

    errors_by_tool: dict[str, list[Issue]] = {}
    warnings_by_tool: dict[str, list[Issue]] = {}

    for tool, issues in issues_by_tool.items():
        for issue in issues:
            sev = issue_severity(issue)
            if sev == "warning":
                warnings_by_tool.setdefault(tool, []).append(issue)
            else:
                errors_by_tool.setdefault(tool, []).append(issue)

    for summary_entry in summary:
        if summary_entry.state not in {"FAIL", "MISSING"}:
            continue
        if summary_entry.label in errors_by_tool or summary_entry.label in warnings_by_tool:
            continue
        errors_by_tool.setdefault(summary_entry.label, [])

    total_errors = sum(len(v) for v in errors_by_tool.values())
    if total_errors == 0 and not any(e.state in {"FAIL", "MISSING"} for e in summary):
        return

    source_cache: dict[str, list[str] | None] = {}

    def tool_header(tool: str, n_errors: int, n_warnings: int) -> str:
        bits = []
        if n_errors:
            bits.append(_c(cfg, f"{n_errors} error" + ("s" if n_errors != 1 else ""), "red", attrs=["bold"]))
        if n_warnings:
            bits.append(
                _c(
                    cfg,
                    f"{n_warnings} warning" + ("s" if n_warnings != 1 else ""),
                    "yellow",
                    attrs=["bold"],
                )
            )
        counts = ", ".join(bits) if bits else "0"
        return _c(cfg, tool, "cyan", attrs=["bold"]) + f" ({counts})"

    logger.info("")
    logger.info(_c(cfg, "==> errors", attrs=["bold"]))

    all_tools = sorted(
        set(errors_by_tool) | set(warnings_by_tool),
        key=lambda t: tool_sort_key(t),
    )

    for tool in all_tools:
        tool_errors = errors_by_tool.get(tool, [])
        tool_warnings = warnings_by_tool.get(tool, [])
        failed_without_issues = any(s.label == tool and s.state in {"FAIL", "MISSING"} for s in summary)
        tool_errors = sorted(tool_errors, key=_issue_sort_key)
        tool_warnings = sorted(tool_warnings, key=_issue_sort_key)
        if not tool_errors and not tool_warnings and not failed_without_issues:
            continue

        logger.info("")
        logger.info(tool_header(tool, len(tool_errors), len(tool_warnings)))

        if not tool_errors and not tool_warnings and failed_without_issues:
            failed_entry = next((s for s in summary if s.label == tool and s.state in {"FAIL", "MISSING"}), None)
            if failed_entry:
                msg = failed_entry.details or f"{tool} failed (see log: {failed_entry.log})"
                logger.info("  " + _c(cfg, "ERROR", "red", attrs=["bold"]) + " " + compact_message(msg, 400))
            continue

        for issues in (tool_errors, tool_warnings):
            for issue in issues:
                sev = issue_severity(issue)
                sev_text = "ERROR" if sev != "warning" else "WARN"
                sev_color = "red" if sev != "warning" else "yellow"

                code = "-"
                msg = getattr(issue, "message", "") or str(issue)

                extras: list[str] = []

                if isinstance(issue, RuffIssue):
                    code = issue.code
                    msg = issue.message
                    if issue.fix_available:
                        extras.append("fixable")
                elif isinstance(issue, BlackIssue):
                    code = "reformat"
                    msg = issue.message
                elif isinstance(issue, ImportLinterIssue):
                    code = issue.contract
                    msg = issue.message
                elif isinstance(issue, MypyIssue):
                    code = issue.code or "-"
                    msg = issue.message
                elif isinstance(issue, PyrightIssue):
                    code = issue.rule or "-"
                    msg = issue.message
                elif isinstance(issue, PytestIssue):
                    code = issue.outcome
                    msg = issue.message
                    extras.append(f"nodeid={issue.nodeid}")
                elif isinstance(issue, UnittestIssue):
                    code = issue.outcome
                    msg = issue.message
                    extras.append(f"test={issue.test}")
                elif isinstance(issue, DeptryIssue):
                    code = issue.code or "-"
                    msg = issue.message
                elif isinstance(issue, VultureIssue):
                    code = "unused"
                    msg = issue.message
                elif isinstance(issue, SemgrepIssue):
                    code = issue.rule_id
                    msg = issue.message
                elif isinstance(issue, BanditIssue):
                    code = issue.test_id
                    msg = issue.message
                    extras.append(f"bandit_severity={issue.severity}")
                    if issue.confidence:
                        extras.append(f"confidence={issue.confidence}")
                elif isinstance(issue, PipAuditIssue):
                    code = issue.vulnerability_id
                    msg = f"{issue.package} {issue.installed_version}: vulnerability {issue.vulnerability_id}"
                    if issue.fix_versions:
                        extras.append("fix=" + ",".join(issue.fix_versions))
                elif isinstance(issue, CoverageXmlIssue):
                    code = "branch"
                    msg = issue.message
                elif isinstance(issue, DiffCoverThresholdIssue):
                    code = "threshold"
                    msg = issue.message
                elif isinstance(issue, DiffCoverFileIssue):
                    code = "missing-lines"
                    missing = ",".join(str(n) for n in issue.missing_lines[:50])
                    if len(issue.missing_lines) > 50:
                        missing += ",…"
                    msg = f"Missing lines: {missing}" if missing else "Missing lines"
                    if issue.coverage is not None:
                        extras.append(f"coverage={issue.coverage:.1f}%")
                elif isinstance(issue, DiffCoverSummaryIssue):
                    code = "summary"
                    msg = issue.message
                    if issue.missing_lines is not None:
                        extras.append(f"missing_lines={issue.missing_lines}")
                    if issue.coverage is not None:
                        extras.append(f"coverage={issue.coverage:.1f}%")
                elif isinstance(issue, CoverageReportIssue):
                    code = "fail-under"
                    msg = issue.message
                    if issue.total is not None:
                        extras.append(f"total={issue.total:.2f}%")
                    if issue.fail_under is not None:
                        extras.append(f"fail_under={issue.fail_under}%")
                elif isinstance(issue, CoverageReportFailure):
                    code = "coverage-report"
                    msg = issue.message
                elif isinstance(issue, CoverageXmlFailure):
                    code = "coverage-xml"
                    msg = issue.message

                path, line_no, col, end_line, end_col = _issue_span(issue)
                location_text = _format_span_location(cfg, path, line_no, col, end_line, end_col)

                header = (
                    "  "
                    + _c(cfg, sev_text, sev_color, attrs=["bold"])
                    + " "
                    + _c(cfg, code, "magenta", attrs=["bold"])
                    + "  "
                    + _c(cfg, location_text, "white", attrs=["bold"])
                )
                if extras:
                    header += "  " + _c(cfg, " ".join(f"[{e}]" for e in extras), "white")

                logger.info(header)

                for wrapped in _wrap_message(msg, width=100, max_lines=10):
                    logger.info("    " + wrapped)

                if path and line_no is not None and line_no > 0:
                    block = _render_source_block(
                        cfg,
                        source_cache,
                        path=path,
                        line_no=line_no,
                        col=col,
                        end_line=end_line,
                        end_col=end_col,
                        severity=sev,
                        context=0,
                        tabsize=4,
                    )
                    for bline in block:
                        logger.info(bline)


def failure_issue(label: str, log_text: str) -> Issue:
    message = f"{label} failed"
    if label == "ruff":
        return RuffFailure(message=message, raw=log_text)
    if label == "black --check":
        return BlackFailure(message=message, raw=log_text)
    if label == "import-linter":
        return ImportLinterFailure(message=message, raw=log_text)
    if label == "mypy":
        return MypyFailure(message=message, raw=log_text)
    if label in ("pyright", "basedpyright"):
        return PyrightFailure(message=message, raw=log_text)
    if label in ("pytest", "coverage run (pytest)"):
        return PytestFailure(message=message, raw=log_text)
    if label == "unittest":
        return UnittestFailure(message=message, raw=log_text)
    if label == "deptry":
        return DeptryFailure(message=message, raw=log_text)
    if label == "vulture":
        return VultureFailure(message=message, raw=log_text)
    if label == "semgrep":
        return SemgrepFailure(message=message, raw=log_text)
    if label == "bandit":
        return BanditFailure(message=message, raw=log_text)
    if label == "pip-audit":
        return PipAuditFailure(message=message, raw=log_text)
    if label == "diff-cover":
        return DiffCoverFailure(message=message, raw=log_text)
    if label == "coverage report":
        return CoverageReportFailure(message=message, raw=log_text)
    if label == "coverage xml":
        return CoverageXmlFailure(message=message, raw=log_text)
    return RuffFailure(message=message, raw=log_text)


def run_cmd(ctx: RunContext, label: str, cmd: Sequence[str], force_pipe: bool = False) -> int:
    logger = logging.getLogger("check")
    safe_label = sanitize_label(label)
    log_path = ctx.log_dir / f"{safe_label}.log"

    tee = ctx.config.show_output
    if tee:
        logger.info("")
        logger.info("==> %s", label)

    use_pty = ctx.config.preserve_color and tee and (not force_pipe)
    if use_pty:
        rc = run_with_pty(cmd, log_path, ctx.config.keep_ansi_logs, tee, ctx.config.env)
    else:
        rc = run_with_pipe(cmd, log_path, ctx.config.keep_ansi_logs, tee, ctx.config.env)

    issues = parse_issues(label, log_path, ctx.config.coverage_fail_under)
    if not issues and rc != 0:
        log_text = read_log_text(log_path)
        issues = [failure_issue(label, log_text)]

    issues_list = list(issues)
    if issues_list:
        errors, warnings = count_issues(issues_list)
    else:
        errors, warnings = extract_counts(label, log_path, ctx.config.coverage_fail_under)

    state = "PASS"
    if rc != 0 or errors > 0:
        state = "FAIL"
        with ctx.lock:
            ctx.status = 1
    elif warnings > 0:
        state = "WARN"

    add_summary(ctx, label, state, rc, errors, warnings, str(log_path))
    if issues_list:
        with ctx.lock:
            ctx.issues_by_tool[label] = issues_list
            ctx.issues.extend(issues_list)
    return rc


def missing(ctx: RunContext, label: str, hint: str) -> None:
    logger = logging.getLogger("check")
    if ctx.config.show_output:
        logger.info("")
        logger.info("==> %s (missing)", label)
        logger.info("%s", hint)
    with ctx.lock:
        ctx.status = 1
    add_summary(ctx, label, "MISSING", 127, 1, 0, "", details=hint)


def skip(ctx: RunContext, label: str, reason: str) -> None:
    logger = logging.getLogger("check")
    if ctx.config.show_output:
        logger.info("")
        logger.info("==> %s (skipped - %s)", label, reason)
    add_summary(ctx, label, "SKIP", 0, 0, 0, "", details=reason)


def run_tool(
    ctx: RunContext,
    label: str,
    cmd: str,
    hint: str,
    args: Sequence[str],
    force_pipe: bool = False,
) -> int:
    tool_path = ctx.config.bin_dir / cmd
    if tool_path.is_file() and os.access(tool_path, os.X_OK):
        return run_cmd(ctx, label, [str(tool_path), *args], force_pipe=force_pipe)
    missing(ctx, label, hint)
    return 127


def run_stage(tasks: Iterable[Callable[[], None]]) -> None:
    for task in tasks:
        task()


def run_stage_parallel(ctx: RunContext, tasks: Sequence[Callable[[], None]]) -> None:
    # jobs=0 => run as many workers as tasks (i.e., "all at once")
    task_list = list(tasks)
    max_workers = ctx.config.jobs if ctx.config.jobs > 0 else len(task_list)
    max_workers = max(1, max_workers)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(task) for task in task_list]
        for fut in concurrent.futures.as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                with ctx.lock:
                    ctx.status = 1
                add_summary(ctx, "runner", "FAIL", 1, 1, 0, "", details=str(exc))


def _format_state_cell(cfg: Config, state: str, width: int = 7) -> str:
    padded = f"{state:<{width}}"
    if state == "PASS":
        return _c(cfg, padded, "green", attrs=["bold"])
    if state in {"FAIL", "MISSING"}:
        return _c(cfg, padded, "red", attrs=["bold"])
    if state == "WARN":
        return _c(cfg, padded, "yellow", attrs=["bold"])
    if state == "SKIP":
        return _c(cfg, padded, "cyan", attrs=["bold"])
    return padded


def print_summary(ctx: RunContext) -> None:
    logger = logging.getLogger("check")
    logger.info("")
    logger.info("==> summary")

    max_label = max((len(entry.label) for entry in ctx.summary), default=4)
    fmt = f"{{:<{max_label}}} | {{:<7}} | {{:>3}} | {{:>6}} | {{:>8}}"

    logger.info(fmt.format("tool", "result", "rc", "errors", "warnings"))
    logger.info("-" * (max_label + 35))

    total_errors = 0
    total_warnings = 0
    clean = 0

    for entry in ctx.summary:
        if entry.state == "PASS":
            clean += 1
        total_errors += entry.errors
        total_warnings += entry.warnings
        state = _format_state_cell(ctx.config, entry.state, width=7)
        logger.info(fmt.format(entry.label, state, entry.rc, entry.errors, entry.warnings))

    logger.info("-" * (max_label + 35))
    logger.info(fmt.format("TOTAL", "", "", total_errors, total_warnings))
    logger.info("")
    logger.info("clean: %s/%s", clean, len(ctx.summary))

    # Branch-first coverage summary (from Cobertura XML written into log_dir)
    xml_path = ctx.log_dir / "coverage.xml"
    totals = read_cobertura_totals(xml_path) if xml_path.is_file() else None
    if totals and totals.branch_rate is not None:
        branch_pct = totals.branch_rate * 100.0
        line_pct = totals.line_rate * 100.0 if totals.line_rate is not None else None

        branch_counts = ""
        if totals.branches_covered is not None and totals.branches_valid is not None:
            branch_counts = f" ({totals.branches_covered}/{totals.branches_valid})"

        line_counts = ""
        if totals.lines_covered is not None and totals.lines_valid is not None:
            line_counts = f" ({totals.lines_covered}/{totals.lines_valid})"

        logger.info("==> coverage")
        logger.info(
            "branch: %.2f%%%s (fail-under=%d%%)",
            branch_pct,
            branch_counts,
            ctx.config.coverage_fail_under,
        )
        if line_pct is not None:
            logger.info("line:   %.2f%%%s", line_pct, line_counts)
        logger.info("")


def command_success(cmd: Sequence[str], env: dict[str, str]) -> bool:
    return (
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        ).returncode
        == 0
    )


def git_ref_exists(ref: str, env: dict[str, str]) -> bool:
    return command_success(
        ["git", "show-ref", "--verify", "--quiet", ref],
        env,
    )


def choose_compare_branch(ctx: RunContext) -> str | None:
    if ctx.config.diff_cover_compare_branch:
        return ctx.config.diff_cover_compare_branch

    candidates = [
        ("refs/remotes/origin/main", "origin/main"),
        ("refs/remotes/origin/master", "origin/master"),
        ("refs/heads/main", "main"),
        ("refs/heads/master", "master"),
    ]
    for ref, name in candidates:
        if git_ref_exists(ref, ctx.config.env):
            return name
    return None


def has_dependency_metadata() -> bool:
    pyproject = Path("pyproject.toml")
    if pyproject.is_file():
        try:
            if "[project]" in pyproject.read_text(errors="replace"):
                return True
        except OSError:
            pass
    return any(Path(".").glob("requirements*.txt"))


def build_config(argv: Sequence[str], logger: logging.Logger) -> Config:
    target = argv[1] if len(argv) > 1 else "."
    if target.startswith("-"):
        logger.error("error: target must be a path, not an option: %s", target)
        raise SystemExit(1)

    root = Path(__file__).resolve().parent
    os.chdir(root)

    target_path = Path(target)
    if target != "." and not target_path.exists():
        logger.error("error: target not found: %s", target)
        raise SystemExit(1)

    venv = root / ".venv"
    python_bin = venv / "bin" / "python"
    bin_dir = venv / "bin"

    if not (python_bin.is_file() and os.access(python_bin, os.X_OK)):
        logger.error("error: .venv python not found at %s", python_bin)
        raise SystemExit(1)

    return Config(
        root=root,
        target=target,
        venv=venv,
        python=python_bin,
        bin_dir=bin_dir,
        exclude_csv=".venv,.git,__pycache__,.mypy_cache,.pytest_cache,data,datasets",
        run_coverage=env_flag("RUN_COVERAGE", "1"),
        coverage_fail_under=env_int("COVERAGE_FAIL_UNDER", 15),
        run_diff_cover=env_flag("RUN_DIFF_COVER", "1"),
        keep_logs=env_flag("KEEP_LOGS", "0"),
        preserve_color=env_flag("PRESERVE_COLOR", "1"),
        keep_ansi_logs=env_flag("KEEP_ANSI_LOGS", "0"),
        show_output=env_flag("SHOW_OUTPUT", "0"),
        jobs=env_int("QA_JOBS", 0),
        use_json=env_flag("USE_JSON_OUTPUT", "1"),
        run_bandit=env_flag("RUN_BANDIT", "0"),
        run_unittest=env_flag("RUN_UNITTEST", "0"),
        semgrep_config=os.environ.get("SEMGREP_CONFIG", "p/python"),
        importlinter_config=os.environ.get("IMPORTLINTER_CONFIG") or None,
        diff_cover_compare_branch=os.environ.get("DIFF_COVER_COMPARE_BRANCH") or None,
        env=os.environ.copy(),
    )


def run_suite(ctx: RunContext) -> None:
    cfg = ctx.config

    def run_ruff() -> None:
        ruff_args = ["check", cfg.target]
        ruff_force_pipe = False
        if cfg.use_json:
            ruff_args = ["check", "--output-format", "json", cfg.target]
            ruff_force_pipe = True
        run_tool(
            ctx,
            "ruff",
            "ruff",
            f"Install: {cfg.python} -m pip install ruff",
            ruff_args,
            force_pipe=ruff_force_pipe,
        )

    def run_black() -> None:
        run_tool(
            ctx,
            "black --check",
            "black",
            f"Install: {cfg.python} -m pip install black",
            ["--check", cfg.target],
        )

    def run_import_linter() -> None:
        lint_imports = cfg.bin_dir / "lint-imports"
        if lint_imports.is_file() and os.access(lint_imports, os.X_OK):
            if cfg.importlinter_config:
                run_cmd(
                    ctx,
                    "import-linter",
                    [str(lint_imports), "--config", cfg.importlinter_config],
                )
            else:
                run_cmd(ctx, "import-linter", [str(lint_imports)])
        else:
            missing(
                ctx,
                "import-linter",
                f"Install: {cfg.python} -m pip install import-linter",
            )

    def run_mypy() -> None:
        mypy_args = [cfg.target]
        mypy_force_pipe = False
        if cfg.use_json:
            mypy_args = ["--output", "json", cfg.target]
            mypy_force_pipe = True

        run_tool(
            ctx,
            "mypy",
            "mypy",
            f"Install: {cfg.python} -m pip install mypy",
            mypy_args,
            force_pipe=mypy_force_pipe,
        )

    def run_pyright() -> None:
        basedpyright = cfg.bin_dir / "basedpyright"
        pyright = cfg.bin_dir / "pyright"
        pyright_args = ["--project", str(cfg.root)]
        pyright_force_pipe = False
        if cfg.use_json:
            pyright_args = ["--outputjson", "--project", str(cfg.root)]
            pyright_force_pipe = True
        if basedpyright.is_file() and os.access(basedpyright, os.X_OK):
            run_cmd(
                ctx,
                "basedpyright",
                [str(basedpyright), *pyright_args],
                force_pipe=pyright_force_pipe,
            )
        elif pyright.is_file() and os.access(pyright, os.X_OK):
            run_cmd(
                ctx,
                "pyright",
                [str(pyright), *pyright_args],
                force_pipe=pyright_force_pipe,
            )
        else:
            missing(
                ctx,
                "pyright/basedpyright",
                f"Install: {cfg.python} -m pip install pyright",
            )

    def run_tests() -> None:
        pytest_bin = cfg.bin_dir / "pytest"
        if pytest_bin.is_file() and os.access(pytest_bin, os.X_OK):
            if cfg.run_coverage:
                if command_success(
                    [str(cfg.python), "-m", "coverage", "--version"],
                    cfg.env,
                ):
                    safe = sanitize_label("coverage run (pytest)")
                    junit_path = str(ctx.log_dir / f"{safe}.junit.xml")
                    rc = run_cmd(
                        ctx,
                        "coverage run (pytest)",
                        [
                            str(cfg.python),
                            "-m",
                            "coverage",
                            "run",
                            "--branch",
                            "-m",
                            "pytest",
                            "--color=yes",
                            "--junitxml",
                            junit_path,
                        ],
                    )
                    if rc == 0:
                        coverage_xml_path = str(ctx.log_dir / "coverage.xml")
                        run_cmd(
                            ctx,
                            "coverage report",
                            [
                                str(cfg.python),
                                "-m",
                                "coverage",
                                "report",
                                "--show-missing",
                            ],
                        )
                        run_cmd(
                            ctx,
                            "coverage xml",
                            [str(cfg.python), "-m", "coverage", "xml", "-o", coverage_xml_path],
                        )

                        if cfg.run_diff_cover:
                            diff_cover = cfg.bin_dir / "diff-cover"
                            if diff_cover.is_file() and os.access(diff_cover, os.X_OK):
                                if command_success(
                                    ["git", "rev-parse", "--is-inside-work-tree"],
                                    cfg.env,
                                ):
                                    compare_branch = choose_compare_branch(ctx)
                                    if compare_branch:
                                        json_report = str(ctx.log_dir / "diff-cover.json")
                                        run_cmd(
                                            ctx,
                                            "diff-cover",
                                            [
                                                str(diff_cover),
                                                coverage_xml_path,
                                                f"--fail-under={cfg.coverage_fail_under}",
                                                f"--compare-branch={compare_branch}",
                                                "--format",
                                                f"json:{json_report}",
                                            ],
                                        )
                                    else:
                                        skip(
                                            ctx,
                                            "diff-cover",
                                            "no obvious main/master compare branch (set DIFF_COVER_COMPARE_BRANCH=...)",
                                        )
                                else:
                                    skip(ctx, "diff-cover", "not a git repo")
                            else:
                                skip(
                                    ctx,
                                    "diff-cover",
                                    "diff-cover not installed (pip install diff-cover)",
                                )
                        else:
                            skip(ctx, "diff-cover", "RUN_DIFF_COVER=0")
                    else:
                        skip(ctx, "coverage report", "pytest failed")
                        skip(ctx, "coverage xml", "pytest failed")
                        skip(ctx, "diff-cover", "pytest failed")
                else:
                    missing(
                        ctx,
                        "coverage",
                        f"Install: {cfg.python} -m pip install coverage",
                    )
                    safe = sanitize_label("pytest")
                    junit_path = str(ctx.log_dir / f"{safe}.junit.xml")
                    run_cmd(
                        ctx,
                        "pytest",
                        [str(pytest_bin), "--color=yes", "--junitxml", junit_path],
                    )
            else:
                safe = sanitize_label("pytest")
                junit_path = str(ctx.log_dir / f"{safe}.junit.xml")
                run_cmd(
                    ctx,
                    "pytest",
                    [str(pytest_bin), "--color=yes", "--junitxml", junit_path],
                )

            if cfg.run_unittest:
                run_cmd(ctx, "unittest", [str(cfg.python), "-m", "unittest"])
        else:
            run_cmd(ctx, "unittest", [str(cfg.python), "-m", "unittest"])

    def run_deptry() -> None:
        deptry = cfg.bin_dir / "deptry"
        if deptry.is_file() and os.access(deptry, os.X_OK):
            if has_dependency_metadata():
                run_cmd(ctx, "deptry", [str(deptry), "."])
            else:
                skip(ctx, "deptry", "no dependency metadata found")
        else:
            missing(ctx, "deptry", f"Install: {cfg.python} -m pip install deptry")

    def run_vulture() -> None:
        vulture = cfg.bin_dir / "vulture"
        if vulture.is_file() and os.access(vulture, os.X_OK):
            run_cmd(
                ctx,
                "vulture",
                [str(vulture), cfg.target, "--exclude", cfg.exclude_csv],
            )
        else:
            missing(ctx, "vulture", f"Install: {cfg.python} -m pip install vulture")

    def run_semgrep() -> None:
        semgrep = cfg.bin_dir / "semgrep"
        if semgrep.is_file() and os.access(semgrep, os.X_OK):
            semgrep_args = [str(semgrep), "scan"]
            semgrep_force_pipe = False
            if cfg.use_json:
                semgrep_args.append("--json")
                semgrep_force_pipe = True
            semgrep_args.extend(
                [
                    "--config",
                    cfg.semgrep_config,
                    "--exclude",
                    ".venv",
                    "--exclude",
                    ".git",
                    "--exclude",
                    "__pycache__",
                    "--exclude",
                    ".mypy_cache",
                    "--exclude",
                    ".pytest_cache",
                    "--exclude",
                    "data",
                    "--exclude",
                    "datasets",
                    cfg.target,
                ]
            )
            run_cmd(ctx, "semgrep", semgrep_args, force_pipe=semgrep_force_pipe)
        else:
            missing(ctx, "semgrep", f"Install: {cfg.python} -m pip install semgrep")

    def run_bandit() -> None:
        if command_success([str(cfg.python), "-m", "bandit", "--version"], cfg.env):
            if not cfg.run_bandit:
                skip(ctx, "bandit", "set RUN_BANDIT=1 to enable")
            else:
                bandit_args = [str(cfg.python), "-m", "bandit"]
                bandit_force_pipe = False
                if cfg.use_json:
                    bandit_args.extend(["-f", "json"])
                    bandit_force_pipe = True
                target_path = Path(cfg.target)
                if target_path.is_dir():
                    run_cmd(
                        ctx,
                        "bandit",
                        [
                            *bandit_args,
                            "-r",
                            cfg.target,
                            "-x",
                            cfg.exclude_csv,
                            "-s",
                            "B101",
                        ],
                        force_pipe=bandit_force_pipe,
                    )
                else:
                    run_cmd(
                        ctx,
                        "bandit",
                        [
                            *bandit_args,
                            cfg.target,
                            "-s",
                            "B101",
                        ],
                        force_pipe=bandit_force_pipe,
                    )
        else:
            missing(ctx, "bandit", f"Install: {cfg.python} -m pip install bandit")

    def run_pip_audit() -> None:
        pip_audit_args: list[str] = []
        pip_audit_force_pipe = False
        if cfg.use_json:
            pip_audit_args = ["-f", "json", "--progress-spinner", "off"]
            pip_audit_force_pipe = True
        run_tool(
            ctx,
            "pip-audit",
            "pip-audit",
            f"Install: {cfg.python} -m pip install pip-audit",
            pip_audit_args,
            force_pipe=pip_audit_force_pipe,
        )

    lint_tasks = [run_ruff, run_black, run_import_linter, run_mypy, run_pyright]
    extra_tasks = [run_deptry, run_vulture, run_semgrep, run_bandit, run_pip_audit]

    run_stage_parallel(ctx, [*lint_tasks, run_tests, *extra_tasks])


def _fixture_ruff_json() -> str:
    return '[\n  {\n    "cell": null,\n    "code": "F401",\n    "end_location": {\n      "column": 10,\n      "row": 1\n    },\n    "filename": "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmptj4m21ot/sample.py",\n    "fix": {\n      "applicability": "safe",\n      "edits": [\n        {\n          "content": "",\n          "end_location": {\n            "column": 1,\n            "row": 2\n          },\n          "location": {\n            "column": 1,\n            "row": 1\n          }\n        }\n      ],\n      "message": "Remove unused import: `os`"\n    },\n    "location": {\n      "column": 8,\n      "row": 1\n    },\n    "message": "`os` imported but unused",\n    "noqa_row": 1,\n    "url": "https://docs.astral.sh/ruff/rules/unused-import"\n  }\n]'


def _fixture_black_reformat() -> str:
    return (
        "would reformat /var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmp31uh7lox/sample.py\n\n"
        "Oh no! \U0001f4a5 \U0001f494 \U0001f4a5\n"
        "1 file would be reformatted."
    )


def _fixture_mypy_error() -> str:
    return (
        "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmpsvyofbdy/sample.py:2: error: "
        'Incompatible return value type (got "str", expected "int")  [return-value]\n'
        "Found 1 error in 1 file (checked 1 source file)"
    )


def _fixture_mypy_json_lines() -> str:
    return "\n".join(
        [
            '{"file":"a.py","line":1,"column":2,"message":"Bad","code":"misc"}',
            '{"file":"b.py","line":3,"column":4,"message":"Worse","code":"arg-type","hint":"Try casting"}',
        ]
    )


def _fixture_bandit_json() -> str:
    return '{\n  "errors": [],\n  "generated_at": "2026-01-15T02:18:24Z",\n  "metrics": {\n    "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmpis3o531q/sample.py": {\n      "CONFIDENCE.HIGH": 3,\n      "CONFIDENCE.LOW": 0,\n      "CONFIDENCE.MEDIUM": 0,\n      "CONFIDENCE.UNDEFINED": 0,\n      "SEVERITY.HIGH": 0,\n      "SEVERITY.LOW": 3,\n      "SEVERITY.MEDIUM": 0,\n      "SEVERITY.UNDEFINED": 0,\n      "loc": 2,\n      "nosec": 0,\n      "skipped_tests": 0\n    },\n    "_totals": {\n      "CONFIDENCE.HIGH": 3,\n      "CONFIDENCE.LOW": 0,\n      "CONFIDENCE.MEDIUM": 0,\n      "CONFIDENCE.UNDEFINED": 0,\n      "SEVERITY.HIGH": 0,\n      "SEVERITY.LOW": 3,\n      "SEVERITY.MEDIUM": 0,\n      "SEVERITY.UNDEFINED": 0,\n      "loc": 2,\n      "nosec": 0,\n      "skipped_tests": 0\n    }\n  },\n  "results": [\n    {\n      "code": "1 import subprocess\\\\n2 subprocess.call(\'ls\', shell=True)\\\\n",\n      "col_offset": 0,\n      "end_col_offset": 17,\n      "filename": "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmpis3o531q/sample.py",\n      "issue_confidence": "HIGH",\n      "issue_cwe": {\n        "id": 78,\n        "link": "https://cwe.mitre.org/data/definitions/78.html"\n      },\n      "issue_severity": "LOW",\n      "issue_text": "Consider possible security implications associated with the subprocess module.",\n      "line_number": 1,\n      "line_range": [\n        1\n      ],\n      "more_info": "https://bandit.readthedocs.io/en/1.9.2/blacklists/blacklist_imports.html#b404-import-subprocess",\n      "test_id": "B404",\n      "test_name": "blacklist"\n    },\n    {\n      "code": "1 import subprocess\\\\n2 subprocess.call(\'ls\', shell=True)\\\\n",\n      "col_offset": 0,\n      "end_col_offset": 33,\n      "filename": "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmpis3o531q/sample.py",\n      "issue_confidence": "HIGH",\n      "issue_cwe": {\n        "id": 78,\n        "link": "https://cwe.mitre.org/data/definitions/78.html"\n      },\n      "issue_severity": "LOW",\n      "issue_text": "Starting a process with a partial executable path",\n      "line_number": 2,\n      "line_range": [\n        2\n      ],\n      "more_info": "https://bandit.readthedocs.io/en/1.9.2/plugins/b607_start_process_with_partial_path.html",\n      "test_id": "B607",\n      "test_name": "start_process_with_partial_path"\n    },\n    {\n      "code": "1 import subprocess\\\\n2 subprocess.call(\'ls\', shell=True)\\\\n",\n      "col_offset": 0,\n      "end_col_offset": 33,\n      "filename": "/var/folders/q_/8bc1ccxs7gb0j26bnvlzqhfc0000gn/T/tmpis3o531q/sample.py",\n      "issue_confidence": "HIGH",\n      "issue_cwe": {\n        "id": 78,\n        "link": "https://cwe.mitre.org/data/definitions/78.html"\n      },\n      "issue_severity": "LOW",\n      "issue_text": "subprocess call with shell=True seems safe, but may be changed in the future, consider rewriting without shell",\n      "line_number": 2,\n      "line_range": [\n        2\n      ],\n      "more_info": "https://bandit.readthedocs.io/en/1.9.2/plugins/b602_subprocess_popen_with_shell_equals_true.html",\n      "test_id": "B602",\n      "test_name": "subprocess_popen_with_shell_equals_true"\n    }\n  ]\n}'


def _fixture_pyright_json() -> str:
    return '{\n    "version": "1.1.408",\n    "time": "1768441814217",\n    "generalDiagnostics": [\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/model/disentanglement/model.py",\n            "severity": "warning",\n            "message": "Import \\"imblearn.over_sampling\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 14,\n                    "character": 5\n                },\n                "end": {\n                    "line": 14,\n                    "character": 27\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/model/disentanglement/model.py",\n            "severity": "warning",\n            "message": "Import \\"sklearn.ensemble\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 17,\n                    "character": 5\n                },\n                "end": {\n                    "line": 17,\n                    "character": 21\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/model/disentanglement/model.py",\n            "severity": "warning",\n            "message": "Import \\"sklearn.linear_model\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 18,\n                    "character": 5\n                },\n                "end": {\n                    "line": 18,\n                    "character": 25\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/model/input/content.py",\n            "severity": "warning",\n            "message": "Import \\"emoji\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 5,\n                    "character": 7\n                },\n                "end": {\n                    "line": 5,\n                    "character": 12\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/model/input/message.py",\n            "severity": "warning",\n            "message": "Import \\"dateutil.parser\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 8,\n                    "character": 5\n                },\n                "end": {\n                    "line": 8,\n                    "character": 20\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/codi/api/utils/compute_statistics.py",\n            "severity": "warning",\n            "message": "Import \\"sklearn.metrics\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 3,\n                    "character": 5\n                },\n                "end": {\n                    "line": 3,\n                    "character": 20\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/servant/scripts/codi_run_channel.py",\n            "severity": "warning",\n            "message": "Import \\"dateutil.parser\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 67,\n                    "character": 13\n                },\n                "end": {\n                    "line": 67,\n                    "character": 28\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "warning",\n            "message": "Import \\"emoji\\" could not be resolved from source",\n            "range": {\n                "start": {\n                    "line": 4,\n                    "character": 7\n                },\n                "end": {\n                    "line": 4,\n                    "character": 12\n                }\n            },\n            "rule": "reportMissingModuleSource"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, Channel]\\" cannot be assigned to parameter \\"uninitialized_channels\\" of type \\"MutableMapping[str, ChannelRef]\\" in function \\"retrieve\\"\\n\xa0\xa0\\"dict[str, Channel]\\" is not assignable to \\"MutableMapping[str, ChannelRef]\\"\\n\xa0\xa0\xa0\xa0Type parameter \\"_VT@MutableMapping\\" is invariant, but \\"Channel\\" is not the same as \\"ChannelRef\\"",\n            "range": {\n                "start": {\n                    "line": 88,\n                    "character": 75\n                },\n                "end": {\n                    "line": 88,\n                    "character": 88\n                }\n            },\n            "rule": "reportArgumentType"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, Channel]\\" cannot be assigned to parameter \\"uninitialized_channels\\" of type \\"MutableMapping[str, ChannelRef]\\" in function \\"retrieve\\"\\n\xa0\xa0\\"dict[str, Channel]\\" is not assignable to \\"MutableMapping[str, ChannelRef]\\"\\n\xa0\xa0\xa0\xa0Type parameter \\"_VT@MutableMapping\\" is invariant, but \\"Channel\\" is not the same as \\"ChannelRef\\"",\n            "range": {\n                "start": {\n                    "line": 112,\n                    "character": 75\n                },\n                "end": {\n                    "line": 112,\n                    "character": 88\n                }\n            },\n            "rule": "reportArgumentType"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, Channel]\\" cannot be assigned to parameter \\"uninitialized_channels\\" of type \\"MutableMapping[str, ChannelRef]\\" in function \\"retrieve\\"\\n\xa0\xa0\\"dict[str, Channel]\\" is not assignable to \\"MutableMapping[str, ChannelRef]\\"\\n\xa0\xa0\xa0\xa0Type parameter \\"_VT@MutableMapping\\" is invariant, but \\"Channel\\" is not the same as \\"ChannelRef\\"",\n            "range": {\n                "start": {\n                    "line": 130,\n                    "character": 80\n                },\n                "end": {\n                    "line": 130,\n                    "character": 93\n                }\n            },\n            "rule": "reportArgumentType"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "error",\n            "message": "\\"emojize\\" is not a known attribute of module \\"emoji\\"",\n            "range": {\n                "start": {\n                    "line": 144,\n                    "character": 26\n                },\n                "end": {\n                    "line": 144,\n                    "character": 33\n                }\n            },\n            "rule": "reportAttributeAccessIssue"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_codi_message_parsing.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, Channel]\\" cannot be assigned to parameter \\"uninitialized_channels\\" of type \\"MutableMapping[str, ChannelRef] | None\\" in function \\"deserialize\\"\\n\xa0\xa0Type \\"dict[str, Channel]\\" is not assignable to type \\"MutableMapping[str, ChannelRef] | None\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, Channel]\\" is not assignable to \\"MutableMapping[str, ChannelRef]\\"\\n\xa0\xa0\xa0\xa0\xa0\xa0Type parameter \\"_VT@MutableMapping\\" is invariant, but \\"Channel\\" is not the same as \\"ChannelRef\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, Channel]\\" is not assignable to \\"None\\"",\n            "range": {\n                "start": {\n                    "line": 183,\n                    "character": 52\n                },\n                "end": {\n                    "line": 183,\n                    "character": 65\n                }\n            },\n            "rule": "reportArgumentType"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_typed_json.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, int]\\" cannot be assigned to parameter \\"obj\\" of type \\"JSON\\" in function \\"json_hash\\"\\n\xa0\xa0Type \\"dict[str, int]\\" is not assignable to type \\"JSON\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"dict[str, JSON]\\"\\n\xa0\xa0\xa0\xa0\xa0\xa0Type parameter \\"_VT@dict\\" is invariant, but \\"int\\" is not the same as \\"JSON\\"\\n\xa0\xa0\xa0\xa0\xa0\xa0Consider switching from \\"dict\\" to \\"Mapping\\" which is covariant in the value type\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"list[JSON]\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"str\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"int\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"float\\"\\n  ...",\n            "range": {\n                "start": {\n                    "line": 133,\n                    "character": 21\n                },\n                "end": {\n                    "line": 133,\n                    "character": 26\n                }\n            },\n            "rule": "reportArgumentType"\n        },\n        {\n            "file": "/Users/wabbit/ws/datatron/python-jeeves/tests/test_typed_json.py",\n            "severity": "error",\n            "message": "Argument of type \\"dict[str, int]\\" cannot be assigned to parameter \\"obj\\" of type \\"JSON\\" in function \\"json_hash\\"\\n\xa0\xa0Type \\"dict[str, int]\\" is not assignable to type \\"JSON\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"dict[str, JSON]\\"\\n\xa0\xa0\xa0\xa0\xa0\xa0Type parameter \\"_VT@dict\\" is invariant, but \\"int\\" is not the same as \\"JSON\\"\\n\xa0\xa0\xa0\xa0\xa0\xa0Consider switching from \\"dict\\" to \\"Mapping\\" which is covariant in the value type\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"list[JSON]\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"str\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"int\\"\\n\xa0\xa0\xa0\xa0\\"dict[str, int]\\" is not assignable to \\"float\\"\\n  ...",\n            "range": {\n                "start": {\n                    "line": 133,\n                    "character": 41\n                },\n                "end": {\n                    "line": 133,\n                    "character": 47\n                }\n            },\n            "rule": "reportArgumentType"\n        }\n    ],\n    "summary": {\n        "filesAnalyzed": 95,\n        "errorCount": 7,\n        "warningCount": 8,\n        "informationCount": 0,\n        "timeInSec": 1.951\n    }\n}'


def _fixture_semgrep_json() -> str:
    return '{"version":"1.147.0","results":[],"errors":[],"paths":{"scanned":["check.py"]},"time":{"rules":[],"rules_parse_time":0.11109089851379395,"profiling_times":{"config_time":0.34958672523498535,"core_time":0.908782958984375,"ignores_time":0.00014162063598632812,"total_time":1.263453722000122},"parsing_time":{"total_time":0.0,"per_file_time":{"mean":0.0,"std_dev":0.0},"very_slow_stats":{"time_ratio":0.0,"count_ratio":0.0},"very_slow_files":[]},"scanning_time":{"total_time":0.580427885055542,"per_file_time":{"mean":0.580427885055542,"std_dev":0.0},"very_slow_stats":{"time_ratio":0.0,"count_ratio":0.0},"very_slow_files":[]},"matching_time":{"total_time":0.0,"per_file_and_rule_time":{"mean":0.0,"std_dev":0.0},"very_slow_stats":{"time_ratio":0.0,"count_ratio":0.0},"very_slow_rules_on_files":[]},"tainting_time":{"total_time":0.0,"per_def_and_rule_time":{"mean":0.0,"std_dev":0.0},"very_slow_stats":{"time_ratio":0.0,"count_ratio":0.0},"very_slow_rules_on_defs":[]},"fixpoint_timeouts":[],"prefiltering":{"project_level_time":0.0,"file_level_time":0.0,"rules_with_project_prefilters_ratio":0.0,"rules_with_file_prefilters_ratio":0.9933774834437086,"rules_selected_ratio":0.046357615894039736,"rules_matched_ratio":0.046357615894039736},"targets":[],"total_bytes":0,"max_memory_bytes":752850944},"engine_requested":"OSS","skipped_rules":[],"profiling_results":[]}'


def _fixture_pip_audit_log() -> str:
    return 'No known vulnerabilities found\n{"dependencies": [{"name": "aiofiles", "version": "25.1.0", "vulns": []}, {"name": "aiohappyeyeballs", "version": "2.6.1", "vulns": []}, {"name": "aiohttp", "version": "3.13.3", "vulns": []}, {"name": "aiosignal", "version": "1.4.0", "vulns": []}, {"name": "aiosqlite", "version": "0.22.1", "vulns": []}, {"name": "alphashape", "version": "1.3.1", "vulns": []}, {"name": "annotated-types", "version": "0.7.0", "vulns": []}, {"name": "anyio", "version": "4.12.0", "vulns": []}, {"name": "asgiref", "version": "3.11.0", "vulns": []}, {"name": "attrs", "version": "25.4.0", "vulns": []}, {"name": "audioop-lts", "version": "0.2.2", "vulns": []}, {"name": "bandit", "version": "1.9.2", "vulns": []}, {"name": "beautifulsoup4", "version": "4.14.3", "vulns": []}, {"name": "black", "version": "25.12.0", "vulns": []}, {"name": "boltons", "version": "21.0.0", "vulns": []}, {"name": "boolean-py", "version": "5.0", "vulns": []}, {"name": "bracex", "version": "2.6", "vulns": []}, {"name": "brotli", "version": "1.2.0", "vulns": []}, {"name": "cachecontrol", "version": "0.14.4", "vulns": []}, {"name": "certifi", "version": "2025.11.12", "vulns": []}, {"name": "cffi", "version": "2.0.0", "vulns": []}, {"name": "chardet", "version": "5.2.0", "vulns": []}, {"name": "charset-normalizer", "version": "3.4.4", "vulns": []}, {"name": "click", "version": "8.1.8", "vulns": []}, {"name": "click-log", "version": "0.4.0", "vulns": []}, {"name": "click-option-group", "version": "0.5.9", "vulns": []}, {"name": "colorama", "version": "0.4.6", "vulns": []}, {"name": "coverage", "version": "7.13.1", "vulns": []}, {"name": "crawl4ai", "version": "0.7.8", "vulns": []}, {"name": "cryptography", "version": "46.0.3", "vulns": []}, {"name": "cssselect", "version": "1.3.0", "vulns": []}, {"name": "cyclonedx-python-lib", "version": "9.1.0", "vulns": []}, {"name": "dataclasses-json", "version": "0.5.7", "vulns": []}, {"name": "defusedxml", "version": "0.7.1", "vulns": []}, {"name": "deptry", "version": "0.24.0", "vulns": []}, {"name": "diff-cover", "version": "10.2.0", "vulns": []}, {"name": "discord", "version": "2.3.2", "vulns": []}, {"name": "discord-py", "version": "2.6.4", "vulns": []}, {"name": "distro", "version": "1.9.0", "vulns": []}, {"name": "django", "version": "6.0.1", "vulns": []}, {"name": "django-stubs", "version": "5.2.8", "vulns": []}, {"name": "django-stubs-ext", "version": "5.2.8", "vulns": []}, {"name": "djangorestframework", "version": "3.16.1", "vulns": []}, {"name": "djangorestframework-stubs", "version": "3.16.7", "vulns": []}, {"name": "emoji", "version": "1.6.3", "vulns": []}, {"name": "exceptiongroup", "version": "1.2.2", "vulns": []}, {"name": "face", "version": "24.0.0", "vulns": []}, {"name": "fake-http-header", "version": "0.3.5", "vulns": []}, {"name": "fake-useragent", "version": "2.2.0", "vulns": []}, {"name": "fastuuid", "version": "0.14.0", "vulns": []}, {"name": "filelock", "version": "3.20.3", "vulns": []}, {"name": "flatbuffers", "version": "25.12.19", "vulns": []}, {"name": "frozenlist", "version": "1.8.0", "vulns": []}, {"name": "fsspec", "version": "2026.1.0", "vulns": []}, {"name": "glom", "version": "22.1.0", "vulns": []}, {"name": "googleapis-common-protos", "version": "1.72.0", "vulns": []}, {"name": "greenlet", "version": "3.3.0", "vulns": []}, {"name": "grimp", "version": "3.14", "vulns": []}, {"name": "grpcio", "version": "1.76.0", "vulns": []}, {"name": "h11", "version": "0.16.0", "vulns": []}, {"name": "h2", "version": "4.3.0", "vulns": []}, {"name": "h3", "version": "4.4.1", "vulns": []}, {"name": "hf-xet", "version": "1.2.0", "vulns": []}, {"name": "hpack", "version": "4.1.0", "vulns": []}, {"name": "httpcore", "version": "1.0.9", "vulns": []}, {"name": "httpx", "version": "0.28.1", "vulns": []}, {"name": "httpx-sse", "version": "0.4.3", "vulns": []}, {"name": "huggingface-hub", "version": "0.36.0", "vulns": []}, {"name": "humanize", "version": "4.15.0", "vulns": []}, {"name": "hyperframe", "version": "6.1.0", "vulns": []}, {"name": "hypothesis", "version": "6.150.2", "vulns": []}, {"name": "idna", "version": "3.11", "vulns": []}, {"name": "imbalanced-learn", "version": "0.14.1", "vulns": []}, {"name": "import-linter", "version": "2.9", "vulns": []}, {"name": "importlib-metadata", "version": "8.7.1", "vulns": []}, {"name": "iniconfig", "version": "2.3.0", "vulns": []}, {"name": "jinja2", "version": "3.1.6", "vulns": []}, {"name": "jiter", "version": "0.12.0", "vulns": []}, {"name": "joblib", "version": "1.5.3", "vulns": []}, {"name": "jsonschema", "version": "4.25.1", "vulns": []}, {"name": "jsonschema-specifications", "version": "2025.9.1", "vulns": []}, {"name": "lark", "version": "1.3.1", "vulns": []}, {"name": "levenshtein", "version": "0.27.3", "vulns": []}, {"name": "libcst", "version": "1.8.6", "vulns": []}, {"name": "librt", "version": "0.7.7", "vulns": []}, {"name": "license-expression", "version": "30.4.4", "vulns": []}, {"name": "litellm", "version": "1.80.15", "vulns": []}, {"name": "lxml", "version": "5.4.0", "vulns": []}, {"name": "markdown-it-py", "version": "4.0.0", "vulns": []}, {"name": "markupsafe", "version": "3.0.3", "vulns": []}, {"name": "marshmallow", "version": "3.26.2", "vulns": []}, {"name": "marshmallow-enum", "version": "1.5.1", "vulns": []}, {"name": "mcp", "version": "1.23.3", "vulns": []}, {"name": "mdurl", "version": "0.1.2", "vulns": []}, {"name": "mpmath", "version": "1.3.0", "vulns": []}, {"name": "msgpack", "version": "1.1.2", "vulns": []}, {"name": "multidict", "version": "6.7.0", "vulns": []}, {"name": "mypy", "version": "1.19.1", "vulns": []}, {"name": "mypy-extensions", "version": "1.1.0", "vulns": []}, {"name": "networkx", "version": "3.6.1", "vulns": []}, {"name": "nltk", "version": "3.9.2", "vulns": []}, {"name": "nodeenv", "version": "1.10.0", "vulns": []}, {"name": "numpy", "version": "2.4.0", "vulns": []}, {"name": "openai", "version": "2.14.0", "vulns": []}, {"name": "opentelemetry-api", "version": "1.37.0", "vulns": []}, {"name": "opentelemetry-exporter-otlp-proto-common", "version": "1.37.0", "vulns": []}, {"name": "opentelemetry-exporter-otlp-proto-http", "version": "1.37.0", "vulns": []}, {"name": "opentelemetry-instrumentation", "version": "0.58b0", "vulns": []}, {"name": "opentelemetry-instrumentation-requests", "version": "0.58b0", "vulns": []}, {"name": "opentelemetry-instrumentation-threading", "version": "0.58b0", "vulns": []}, {"name": "opentelemetry-proto", "version": "1.37.0", "vulns": []}, {"name": "opentelemetry-sdk", "version": "1.37.0", "vulns": []}, {"name": "opentelemetry-semantic-conventions", "version": "0.58b0", "vulns": []}, {"name": "opentelemetry-util-http", "version": "0.58b0", "vulns": []}, {"name": "packageurl-python", "version": "0.17.6", "vulns": []}, {"name": "packaging", "version": "25.0", "vulns": []}, {"name": "patchright", "version": "1.57.2", "vulns": []}, {"name": "pathspec", "version": "1.0.3", "vulns": []}, {"name": "peewee", "version": "3.19.0", "vulns": []}, {"name": "pillow", "version": "12.1.0", "vulns": []}, {"name": "pip", "version": "25.3", "vulns": []}, {"name": "pip-api", "version": "0.0.34", "vulns": []}, {"name": "pip-audit", "version": "2.9.0", "vulns": []}, {"name": "pip-requirements-parser", "version": "32.0.1", "vulns": []}, {"name": "platformdirs", "version": "4.5.1", "vulns": []}, {"name": "playwright", "version": "1.57.0", "vulns": []}, {"name": "pluggy", "version": "1.6.0", "vulns": []}, {"name": "propcache", "version": "0.4.1", "vulns": []}, {"name": "protobuf", "version": "6.33.4", "vulns": []}, {"name": "psutil", "version": "7.2.1", "vulns": []}, {"name": "py-serializable", "version": "2.1.0", "vulns": []}, {"name": "pycparser", "version": "2.23", "vulns": []}, {"name": "pydantic", "version": "2.12.5", "vulns": []}, {"name": "pydantic-core", "version": "2.41.5", "vulns": []}, {"name": "pydantic-settings", "version": "2.12.0", "vulns": []}, {"name": "pyee", "version": "13.0.0", "vulns": []}, {"name": "pygithub", "version": "2.8.1", "vulns": []}, {"name": "pygments", "version": "2.19.2", "vulns": []}, {"name": "pyjwt", "version": "2.10.1", "vulns": []}, {"name": "pynacl", "version": "1.6.2", "vulns": []}, {"name": "pyopenssl", "version": "25.3.0", "vulns": []}, {"name": "pyparsing", "version": "3.3.1", "vulns": []}, {"name": "pyre-check", "version": "0.9.25", "vulns": []}, {"name": "pyre-extensions", "version": "0.0.32", "vulns": []}, {"name": "pyright", "version": "1.1.408", "vulns": []}, {"name": "pytest", "version": "9.0.2", "vulns": []}, {"name": "python-dateutil", "version": "2.9.0.post0", "vulns": []}, {"name": "python-dotenv", "version": "1.2.1", "vulns": []}, {"name": "python-multipart", "version": "0.0.21", "vulns": []}, {"name": "pytokens", "version": "0.3.0", "vulns": []}, {"name": "pytz", "version": "2025.2", "vulns": []}, {"name": "pyyaml", "version": "6.0.3", "vulns": []}, {"name": "pyyaml-ft", "version": "8.0.0", "vulns": []}, {"name": "rank-bm25", "version": "0.2.2", "vulns": []}, {"name": "rapidfuzz", "version": "3.14.3", "vulns": []}, {"name": "referencing", "version": "0.37.0", "vulns": []}, {"name": "regex", "version": "2025.11.3", "vulns": []}, {"name": "requests", "version": "2.32.5", "vulns": []}, {"name": "requirements-parser", "version": "0.13.0", "vulns": []}, {"name": "rich", "version": "14.2.0", "vulns": []}, {"name": "rpds-py", "version": "0.30.0", "vulns": []}, {"name": "rtree", "version": "1.4.1", "vulns": []}, {"name": "ruamel-yaml", "version": "0.19.1", "vulns": []}, {"name": "ruamel-yaml-clib", "version": "0.2.14", "vulns": []}, {"name": "ruff", "version": "0.14.11", "vulns": []}, {"name": "safetensors", "version": "0.7.0", "vulns": []}, {"name": "scikit-learn", "version": "1.8.0", "vulns": []}, {"name": "scipy", "version": "1.17.0", "vulns": []}, {"name": "semgrep", "version": "1.147.0", "vulns": []}, {"name": "sentence-transformers", "version": "5.2.0", "vulns": []}, {"name": "setuptools", "version": "80.9.0", "vulns": []}, {"name": "shapely", "version": "2.1.2", "vulns": []}, {"name": "shellingham", "version": "1.5.4", "vulns": []}, {"name": "six", "version": "1.17.0", "vulns": []}, {"name": "sklearn-compat", "version": "0.1.5", "vulns": []}, {"name": "sniffio", "version": "1.3.1", "vulns": []}, {"name": "snowballstemmer", "version": "2.2.0", "vulns": []}, {"name": "sortedcontainers", "version": "2.4.0", "vulns": []}, {"name": "soupsieve", "version": "2.8.1", "vulns": []}, {"name": "sqlparse", "version": "0.5.5", "vulns": []}, {"name": "sse-starlette", "version": "3.1.2", "vulns": []}, {"name": "starlette", "version": "0.51.0", "vulns": []}, {"name": "stevedore", "version": "5.6.0", "vulns": []}, {"name": "sympy", "version": "1.14.0", "vulns": []}, {"name": "tabulate", "version": "0.9.0", "vulns": []}, {"name": "testslide", "version": "2.7.1", "vulns": []}, {"name": "tf-playwright-stealth", "version": "1.2.0", "vulns": []}, {"name": "threadpoolctl", "version": "3.6.0", "vulns": []}, {"name": "tiktoken", "version": "0.12.0", "vulns": []}, {"name": "timezonefinder", "version": "8.2.0", "vulns": []}, {"name": "tokenizers", "version": "0.22.2", "vulns": []}, {"name": "toml", "version": "0.10.2", "vulns": []}, {"name": "tomli", "version": "2.0.2", "vulns": []}, {"name": "tomli-w", "version": "1.2.0", "vulns": []}, {"name": "torch", "version": "2.9.1", "vulns": []}, {"name": "tqdm", "version": "4.67.1", "vulns": []}, {"name": "transformers", "version": "4.57.3", "vulns": []}, {"name": "trimesh", "version": "4.11.0", "vulns": []}, {"name": "typeguard", "version": "2.13.3", "vulns": []}, {"name": "typer-slim", "version": "0.21.1", "vulns": []}, {"name": "types-python-dateutil", "version": "2.9.0.20251115", "vulns": []}, {"name": "types-pytz", "version": "2025.2.0.20251108", "vulns": []}, {"name": "types-pyyaml", "version": "6.0.12.20250915", "vulns": []}, {"name": "types-requests", "version": "2.32.4.20260107", "vulns": []}, {"name": "types-tqdm", "version": "4.67.0.20250809", "vulns": []}, {"name": "typing-extensions", "version": "4.15.0", "vulns": []}, {"name": "typing-inspect", "version": "0.9.0", "vulns": []}, {"name": "typing-inspection", "version": "0.4.2", "vulns": []}, {"name": "tzwhere", "version": "3.0.3", "vulns": []}, {"name": "urllib3", "version": "2.6.3", "vulns": []}, {"name": "uvicorn", "version": "0.40.0", "vulns": []}, {"name": "vulture", "version": "2.14", "vulns": []}, {"name": "wcmatch", "version": "8.5.2", "vulns": []}, {"name": "wrapt", "version": "1.17.3", "vulns": []}, {"name": "xxhash", "version": "3.6.0", "vulns": []}, {"name": "yarl", "version": "1.22.0", "vulns": []}, {"name": "youtube-transcript-api", "version": "1.2.3", "vulns": []}, {"name": "zipp", "version": "3.23.0", "vulns": []}], "fixes": []}'


def _fixture_deptry_log() -> str:
    return "Scanning 91 files...\n\nSuccess! No dependency issues found."


def _fixture_diff_cover_snippet() -> str:
    return (
        "Failure. Coverage is below 80%.\n"
        "codi/api/apps.py (0.0%): Missing lines 1,4-6\n"
        "Missing: 4716 lines\n"
        "Coverage: 28%"
    )


def _fixture_coverage_report_fail() -> str:
    return "Coverage failure: total of 24 is less than fail-under=80"


def _fixture_coverage_xml_log() -> str:
    return "Wrote XML report to coverage.xml\nCoverage failure: total of 24 is less than fail-under=80"


def _assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def _test_parse_ruff_json() -> None:
    issues = list(parse_ruff_issues(_fixture_ruff_json()))
    _assert_equal(len(issues), 1, "ruff issue count")
    issue = issues[0]
    assert isinstance(issue, RuffIssue), "ruff issue type"
    _assert_equal(issue.code, "F401", "ruff code")
    _assert_equal(issue.location.line, 1, "ruff line")
    _assert_equal(issue.location.column, 8, "ruff column")
    _assert_true(issue.fix_available, "ruff fix flag")
    _assert_equal(issue.url, "https://docs.astral.sh/ruff/rules/unused-import", "ruff url")


def _test_parse_black_reformat() -> None:
    issues = list(parse_black_issues(_fixture_black_reformat()))
    _assert_equal(len(issues), 1, "black issue count")
    issue = issues[0]
    assert isinstance(issue, BlackIssue), "black issue type"
    _assert_true(issue.path is not None and issue.path.endswith("sample.py"), "black path")
    _assert_equal(issue.message, "File would be reformatted", "black message")


def _test_parse_mypy_error() -> None:
    issues = list(parse_mypy_issues(_fixture_mypy_error()))
    _assert_equal(len(issues), 1, "mypy issue count")
    issue = issues[0]
    assert isinstance(issue, MypyIssue), "mypy issue type"
    _assert_equal(issue.location.line, 2, "mypy line")
    _assert_equal(issue.code, "return-value", "mypy code")
    _assert_true("Incompatible return value type" in issue.message, "mypy message")


def _test_parse_mypy_json_lines() -> None:
    issues = list(parse_mypy_issues(_fixture_mypy_json_lines()))
    _assert_equal(len(issues), 2, "mypy json lines count")
    issue = issues[1]
    assert isinstance(issue, MypyIssue), "mypy json lines issue type"
    _assert_true("Try casting" in issue.notes, "mypy json lines hint")


def _test_parse_pyright_json() -> None:
    issues = list(parse_pyright_issues(_fixture_pyright_json()))
    _assert_equal(len(issues), 15, "pyright issue count")
    first = issues[0]
    assert isinstance(first, PyrightIssue), "pyright issue type"
    _assert_equal(first.severity, "warning", "pyright severity")
    _assert_equal(first.rule, "reportMissingModuleSource", "pyright rule")
    _assert_equal(first.location.line, 15, "pyright line")
    _assert_equal(first.location.column, 6, "pyright column")
    _assert_true(
        any(isinstance(issue, PyrightIssue) and issue.severity == "error" for issue in issues),
        "pyright errors present",
    )


def _test_parse_semgrep_json() -> None:
    issues = list(parse_semgrep_issues(_fixture_semgrep_json()))
    _assert_equal(len(issues), 0, "semgrep issue count")


def _test_parse_bandit_json() -> None:
    issues = list(parse_bandit_issues(_fixture_bandit_json()))
    _assert_equal(len(issues), 3, "bandit issue count")
    issue = issues[0]
    assert isinstance(issue, BanditIssue), "bandit issue type"
    _assert_equal(issue.test_id, "B404", "bandit test id")
    _assert_equal(issue.confidence, "HIGH", "bandit confidence")
    _assert_equal(issue.location.line, 1, "bandit line")


def _test_parse_pip_audit_log() -> None:
    issues = list(parse_pip_audit_issues(_fixture_pip_audit_log()))
    _assert_equal(len(issues), 0, "pip-audit issue count")


def _test_parse_deptry_log() -> None:
    issues = list(parse_deptry_issues(_fixture_deptry_log()))
    _assert_equal(len(issues), 0, "deptry issue count")


def _test_parse_diff_cover_snippet() -> None:
    issues = list(parse_diff_cover_issues(_fixture_diff_cover_snippet()))
    file_issue = next((issue for issue in issues if isinstance(issue, DiffCoverFileIssue)), None)
    assert isinstance(file_issue, DiffCoverFileIssue), "diff-cover file issue type"
    _assert_true(1 in file_issue.missing_lines, "diff-cover missing lines parsed")
    _assert_true(any(isinstance(issue, DiffCoverSummaryIssue) for issue in issues), "diff-cover summary issue")
    _assert_true(any(isinstance(issue, DiffCoverThresholdIssue) for issue in issues), "diff-cover threshold issue")


def _test_parse_coverage_report_fail() -> None:
    issues = list(parse_coverage_report_issues(_fixture_coverage_report_fail(), 80))
    _assert_equal(len(issues), 1, "coverage report issue count")
    issue = issues[0]
    assert isinstance(issue, CoverageReportIssue), "coverage report issue type"
    _assert_equal(issue.fail_under, 80, "coverage report fail-under")
    _assert_equal(issue.total, 24.0, "coverage report total")


def _test_parse_coverage_xml_fail() -> None:
    issues = list(parse_coverage_xml_issues(_fixture_coverage_xml_log(), 80))
    _assert_equal(len(issues), 1, "coverage xml issue count")
    issue = issues[0]
    assert isinstance(issue, CoverageXmlIssue), "coverage xml issue type"
    _assert_equal(issue.fail_under, 80, "coverage xml fail-under")
    _assert_equal(issue.total, 24.0, "coverage xml total")


def run_self_tests() -> int:
    logger = setup_logging()
    logger.info("")
    logger.info("==> parser self-tests")
    tests: list[tuple[str, Callable[[], None]]] = [
        ("ruff json", _test_parse_ruff_json),
        ("black reformat", _test_parse_black_reformat),
        ("mypy error", _test_parse_mypy_error),
        ("mypy json lines", _test_parse_mypy_json_lines),
        ("pyright json", _test_parse_pyright_json),
        ("semgrep json", _test_parse_semgrep_json),
        ("bandit json", _test_parse_bandit_json),
        ("pip-audit log", _test_parse_pip_audit_log),
        ("deptry log", _test_parse_deptry_log),
        ("diff-cover snippet", _test_parse_diff_cover_snippet),
        ("coverage report", _test_parse_coverage_report_fail),
        ("coverage xml", _test_parse_coverage_xml_fail),
    ]
    failures = 0
    for name, test in tests:
        try:
            test()
            logger.info("PASS %s", name)
        except AssertionError as exc:
            failures += 1
            logger.error("FAIL %s: %s", name, exc)
        except Exception as exc:  # pragma: no cover - safety net
            failures += 1
            logger.exception("ERROR %s: %s", name, exc)
    logger.info("self-test: %s/%s passed", len(tests) - failures, len(tests))
    return 0 if failures == 0 else 1


def main(argv: Sequence[str]) -> int:
    if "--self-test" in argv:
        return run_self_tests()

    logger = setup_logging()
    config = build_config(argv, logger)
    log_dir = Path(tempfile.mkdtemp(prefix="qa"))
    ctx = RunContext(config=config, log_dir=log_dir, summary=[])

    try:
        run_suite(ctx)
        print_unified_errors(ctx)
        print_summary(ctx)
    finally:
        if ctx.status != 0 or ctx.config.keep_logs:
            logger.info("")
            logger.info("logs: %s", ctx.log_dir)
        else:
            shutil.rmtree(ctx.log_dir, ignore_errors=True)

    return ctx.status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
