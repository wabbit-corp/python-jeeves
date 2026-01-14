#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
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
    status: int = 0


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
) -> None:
    ctx.summary.append(SummaryEntry(label, state, rc, errors, warnings, log))


def strip_ansi(data: bytes) -> bytes:
    data = ANSI_OSC_RE.sub(b"", data)
    return ANSI_CSI_RE.sub(b"", data)


def run_with_pty(
    cmd: Sequence[str],
    log_path: Path,
    keep_ansi_logs: bool,
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


def run_with_pipe(cmd: Sequence[str], log_path: Path, env: dict[str, str]) -> int:
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
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log_file.write(chunk)
            log_file.flush()
    stdout.close()
    return proc.wait()


def read_log_text(log_path: Path) -> str:
    try:
        return log_path.read_text(errors="replace")
    except OSError:
        return ""


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
        matches = re.findall(
            r"^TOTAL\s+.*\s(\d+(?:\.\d+)?)%$",
            log_text,
            flags=re.MULTILINE,
        )
        if matches:
            pct = matches[-1]
            try:
                ipct = int(pct.split(".", maxsplit=1)[0])
            except ValueError:
                ipct = coverage_fail_under
            if ipct < coverage_fail_under:
                errors = 1
        elif re.search(r"Coverage failure:", log_text):
            errors = 1
    else:
        errors = len(re.findall(r"\berror\b", log_text, flags=re.IGNORECASE))
        warnings = len(re.findall(r"\bwarning\b", log_text, flags=re.IGNORECASE))

    if errors < 0:
        errors = 0
    if warnings < 0:
        warnings = 0

    return errors, warnings


def run_cmd(ctx: RunContext, label: str, cmd: Sequence[str]) -> int:
    logger = logging.getLogger("check")
    safe_label = sanitize_label(label)
    log_path = ctx.log_dir / f"{safe_label}.log"

    logger.info("")
    logger.info("==> %s", label)

    if ctx.config.preserve_color:
        rc = run_with_pty(cmd, log_path, ctx.config.keep_ansi_logs, ctx.config.env)
    else:
        rc = run_with_pipe(cmd, log_path, ctx.config.env)

    errors, warnings = extract_counts(label, log_path, ctx.config.coverage_fail_under)

    state = "PASS"
    if rc != 0:
        state = "FAIL"
        ctx.status = 1
    elif errors > 0 or warnings > 0:
        state = "WARN"

    add_summary(ctx, label, state, rc, errors, warnings, str(log_path))
    return rc


def missing(ctx: RunContext, label: str, hint: str) -> None:
    logger = logging.getLogger("check")
    logger.info("")
    logger.info("==> %s (missing)", label)
    logger.info("%s", hint)
    ctx.status = 1
    add_summary(ctx, label, "MISSING", 127, 0, 0, "")


def skip(ctx: RunContext, label: str, reason: str) -> None:
    logger = logging.getLogger("check")
    logger.info("")
    logger.info("==> %s (skipped - %s)", label, reason)
    add_summary(ctx, label, "SKIP", 0, 0, 0, "")


def run_tool(ctx: RunContext, label: str, cmd: str, hint: str, args: Sequence[str]) -> int:
    tool_path = ctx.config.bin_dir / cmd
    if tool_path.is_file() and os.access(tool_path, os.X_OK):
        return run_cmd(ctx, label, [str(tool_path), *args])
    missing(ctx, label, hint)
    return 127


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
        logger.info(fmt.format(entry.label, entry.state, entry.rc, entry.errors, entry.warnings))

    logger.info("-" * (max_label + 35))
    logger.info(fmt.format("TOTAL", "", "", total_errors, total_warnings))
    logger.info("")
    logger.info("clean: %s/%s", clean, len(ctx.summary))


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
        coverage_fail_under=env_int("COVERAGE_FAIL_UNDER", 80),
        run_diff_cover=env_flag("RUN_DIFF_COVER", "1"),
        keep_logs=env_flag("KEEP_LOGS", "0"),
        preserve_color=env_flag("PRESERVE_COLOR", "1"),
        keep_ansi_logs=env_flag("KEEP_ANSI_LOGS", "0"),
        run_bandit=env_flag("RUN_BANDIT", "0"),
        run_unittest=env_flag("RUN_UNITTEST", "0"),
        semgrep_config=os.environ.get("SEMGREP_CONFIG", "p/python"),
        importlinter_config=os.environ.get("IMPORTLINTER_CONFIG") or None,
        diff_cover_compare_branch=os.environ.get("DIFF_COVER_COMPARE_BRANCH") or None,
        env=os.environ.copy(),
    )


def run_suite(ctx: RunContext) -> None:
    cfg = ctx.config

    run_tool(
        ctx,
        "ruff",
        "ruff",
        f"Install: {cfg.python} -m pip install ruff",
        ["check", cfg.target],
    )
    run_tool(
        ctx,
        "black --check",
        "black",
        f"Install: {cfg.python} -m pip install black",
        ["--check", cfg.target],
    )

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

    run_tool(
        ctx,
        "mypy",
        "mypy",
        f"Install: {cfg.python} -m pip install mypy",
        [cfg.target],
    )

    basedpyright = cfg.bin_dir / "basedpyright"
    pyright = cfg.bin_dir / "pyright"
    if basedpyright.is_file() and os.access(basedpyright, os.X_OK):
        run_cmd(ctx, "basedpyright", [str(basedpyright), "--project", str(cfg.root)])
    elif pyright.is_file() and os.access(pyright, os.X_OK):
        run_cmd(ctx, "pyright", [str(pyright), "--project", str(cfg.root)])
    else:
        missing(
            ctx,
            "pyright/basedpyright",
            f"Install: {cfg.python} -m pip install pyright",
        )

    pytest_bin = cfg.bin_dir / "pytest"
    if pytest_bin.is_file() and os.access(pytest_bin, os.X_OK):
        if cfg.run_coverage:
            if command_success(
                [str(cfg.python), "-m", "coverage", "--version"],
                cfg.env,
            ):
                rc = run_cmd(
                    ctx,
                    "coverage run (pytest)",
                    [
                        str(cfg.python),
                        "-m",
                        "coverage",
                        "run",
                        "-m",
                        "pytest",
                        "--color=yes",
                    ],
                )
                if rc == 0:
                    run_cmd(
                        ctx,
                        "coverage report",
                        [
                            str(cfg.python),
                            "-m",
                            "coverage",
                            "report",
                            f"--fail-under={cfg.coverage_fail_under}",
                            "--show-missing",
                        ],
                    )
                    run_cmd(
                        ctx,
                        "coverage xml",
                        [str(cfg.python), "-m", "coverage", "xml"],
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
                                    run_cmd(
                                        ctx,
                                        "diff-cover",
                                        [
                                            str(diff_cover),
                                            "coverage.xml",
                                            f"--fail-under={cfg.coverage_fail_under}",
                                            f"--compare-branch={compare_branch}",
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
                run_cmd(ctx, "pytest", [str(pytest_bin), "--color=yes"])
        else:
            run_cmd(ctx, "pytest", [str(pytest_bin), "--color=yes"])

        if cfg.run_unittest:
            run_cmd(ctx, "unittest", [str(cfg.python), "-m", "unittest"])
    else:
        run_cmd(ctx, "unittest", [str(cfg.python), "-m", "unittest"])

    deptry = cfg.bin_dir / "deptry"
    if deptry.is_file() and os.access(deptry, os.X_OK):
        if has_dependency_metadata():
            run_cmd(ctx, "deptry", [str(deptry), "."])
        else:
            skip(ctx, "deptry", "no dependency metadata found")
    else:
        missing(ctx, "deptry", f"Install: {cfg.python} -m pip install deptry")

    vulture = cfg.bin_dir / "vulture"
    if vulture.is_file() and os.access(vulture, os.X_OK):
        run_cmd(
            ctx,
            "vulture",
            [str(vulture), cfg.target, "--exclude", cfg.exclude_csv],
        )
    else:
        missing(ctx, "vulture", f"Install: {cfg.python} -m pip install vulture")

    semgrep = cfg.bin_dir / "semgrep"
    if semgrep.is_file() and os.access(semgrep, os.X_OK):
        run_cmd(
            ctx,
            "semgrep",
            [
                str(semgrep),
                "scan",
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
            ],
        )
    else:
        missing(ctx, "semgrep", f"Install: {cfg.python} -m pip install semgrep")

    if command_success([str(cfg.python), "-m", "bandit", "--version"], cfg.env):
        if not cfg.run_bandit:
            skip(ctx, "bandit", "set RUN_BANDIT=1 to enable")
        else:
            target_path = Path(cfg.target)
            if target_path.is_dir():
                run_cmd(
                    ctx,
                    "bandit",
                    [
                        str(cfg.python),
                        "-m",
                        "bandit",
                        "-r",
                        cfg.target,
                        "-x",
                        cfg.exclude_csv,
                        "-s",
                        "B101",
                    ],
                )
            else:
                run_cmd(
                    ctx,
                    "bandit",
                    [
                        str(cfg.python),
                        "-m",
                        "bandit",
                        cfg.target,
                        "-s",
                        "B101",
                    ],
                )
    else:
        missing(ctx, "bandit", f"Install: {cfg.python} -m pip install bandit")

    run_tool(
        ctx,
        "pip-audit",
        "pip-audit",
        f"Install: {cfg.python} -m pip install pip-audit",
        [],
    )


def main(argv: Sequence[str]) -> int:
    logger = setup_logging()
    config = build_config(argv, logger)
    log_dir = Path(tempfile.mkdtemp(prefix="qa"))
    ctx = RunContext(config=config, log_dir=log_dir, summary=[])

    try:
        run_suite(ctx)
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
