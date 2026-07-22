#!/usr/bin/env python3
"""Detect high-confidence secrets in Git-tracked files without uploading data."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SHELL_VALUE = r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|`[^`\r\n]*`|[^\s;&|]+)"""


@dataclass(frozen=True, slots=True)
class Finding:
    """A redacted finding; matched content is intentionally not retained."""

    path: str
    line: int
    rule_id: str
    description: str


@dataclass(frozen=True, slots=True)
class Rule:
    """A high-confidence pattern and its optional secret-value group."""

    rule_id: str
    description: str
    pattern: re.Pattern[str]
    value_group: str | None = None


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Deterministic repository scan result."""

    scanned_paths: tuple[str, ...]
    findings: tuple[Finding, ...]


class ScanError(RuntimeError):
    """Raised when the tracked-file scan cannot complete safely."""


_RULES = (
    Rule(
        "sshpass-literal",
        "sshpass receives a literal password",
        re.compile(rf"\bsshpass\s+(?:-p|--password)(?:\s+|=)(?P<value>{_SHELL_VALUE})", re.IGNORECASE),
        "value",
    ),
    Rule(
        "password-env-literal",
        "a password transport variable contains a literal value",
        re.compile(rf"\b(?:SSHPASS|WB_ROOT_PASSWORD|ROOT_PASSWORD)\s*=\s*(?P<value>{_SHELL_VALUE})"),
        "value",
    ),
    Rule(
        "root-password-literal",
        "a root password is written as a literal",
        re.compile(
            rf"\broot\s+(?:password|парол(?:ь|я))\s*[:=]\s*(?:\*\*)?\s*(?P<value>{_SHELL_VALUE})",
            re.IGNORECASE,
        ),
        "value",
    ),
    Rule(
        "tls-verification-disabled",
        "Node.js TLS certificate verification is disabled",
        re.compile(r"\bNODE_TLS_REJECT_UNAUTHORIZED\s*=\s*0\b"),
    ),
    Rule(
        "private-key",
        "a private key header is tracked",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),
    Rule(
        "provider-token",
        "a token with a well-known provider prefix is tracked",
        re.compile(
            r"\b(?:"
            r"(?:AKIA|ASIA)[0-9A-Z]{16}|"
            r"gh[pousr]_[A-Za-z0-9]{36,}|"
            r"github_pat_[A-Za-z0-9_]{50,}|"
            r"xox[baprs]-[A-Za-z0-9-]{20,}|"
            r"AIza[0-9A-Za-z_-]{35}|"
            r"sk_live_[0-9A-Za-z]{16,}"
            r")\b"
        ),
    ),
    Rule(
        "credential-url",
        "a URL contains a literal password",
        re.compile(
            rf"\b(?:https?|ssh|postgres(?:ql)?|mysql|mqtt)://[^\s/:@]+:(?P<value>{_SHELL_VALUE})@",
            re.IGNORECASE,
        ),
        "value",
    ),
    Rule(
        "bearer-token",
        "a literal bearer token is tracked",
        re.compile(r"\bbearer\s+(?P<value>[A-Za-z0-9._~+/=-]{20,})", re.IGNORECASE),
        "value",
    ),
)

_SAFE_LITERAL_MARKERS = frozenset(
    {
        "changeme",
        "dummy",
        "example",
        "none",
        "not-set",
        "null",
        "placeholder",
        "redacted",
        "test",
    }
)


def _unwrap(value: str) -> str:
    value = value.strip()
    while len(value) >= 2 and (value[0], value[-1]) in {('"', '"'), ("'", "'"), ("`", "`")}:
        value = value[1:-1].strip()
    return value


def _is_safe_reference(value: str) -> bool:
    value = _unwrap(value)
    lowered = value.casefold()
    if not value:
        return True
    if value.startswith(("$", "<", "{{")):
        return True
    if lowered in _SAFE_LITERAL_MARKERS:
        return True
    return any(marker in lowered for marker in ("placeholder", "redacted", "secret-store", "secret_manager"))


def scan_text(path: str, text: str) -> tuple[Finding, ...]:
    """Scan text while retaining only redacted finding metadata."""
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule in _RULES:
            for match in rule.pattern.finditer(line):
                if rule.value_group and _is_safe_reference(match.group(rule.value_group)):
                    continue
                findings.append(Finding(path, line_number, rule.rule_id, rule.description))
    return tuple(findings)


def tracked_paths(repo: Path) -> tuple[str, ...]:
    """Return Git-tracked paths in a stable order."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", "--cached"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScanError("cannot enumerate Git-tracked files") from exc

    paths = (os.fsdecode(raw_path) for raw_path in completed.stdout.split(b"\0") if raw_path)
    return tuple(sorted(paths))


def _read_tracked_path(repo: Path, relative_path: str) -> str | None:
    parts = PurePosixPath(relative_path).parts
    full_path = repo.joinpath(*parts)
    try:
        if full_path.is_symlink():
            return os.readlink(full_path)
        if not full_path.exists() or full_path.is_dir():
            return None
        return full_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise ScanError(f"cannot read tracked path: {_display_path(relative_path)}") from exc


def scan_repository(repo: Path) -> ScanReport:
    """Scan every present Git-tracked file in the working tree."""
    repo = repo.resolve()
    scanned_paths: list[str] = []
    findings: list[Finding] = []
    for relative_path in tracked_paths(repo):
        text = _read_tracked_path(repo, relative_path)
        if text is None:
            continue
        scanned_paths.append(relative_path)
        findings.extend(scan_text(relative_path, text))
    findings.sort(key=lambda item: (item.path, item.line, item.rule_id))
    return ScanReport(tuple(scanned_paths), tuple(findings))


def _display_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _format_finding(finding: Finding) -> str:
    path = _display_path(finding.path)
    return f"{path}:{finding.line}: {finding.rule_id}: {finding.description} (value omitted)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository root (default: current directory)")
    args = parser.parse_args(argv)

    try:
        report = scan_repository(args.repo)
    except ScanError as exc:
        print(f"Tracked secret scan error: {exc}", file=sys.stderr)
        return 2

    if report.findings:
        print("Tracked secret scan failed; matched values are intentionally omitted:", file=sys.stderr)
        for finding in report.findings:
            print(_format_finding(finding), file=sys.stderr)
        return 1

    print(f"Tracked secret scan passed ({len(report.scanned_paths)} files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
