"""Application version resolution.

Single source of truth for the running app version.

Format: ``2.<N>+<short_sha>[+dirty]`` where:
- ``2`` is the major (v2 line, anchored at the ``v2-base`` git tag).
- ``<N>`` is the count of commits between ``v2-base`` and ``HEAD``.
- ``<short_sha>`` is the abbreviated commit SHA of ``HEAD``.
- ``+dirty`` is appended when the working tree has uncommitted changes.

The string is semver build-metadata shaped and uses only URL-safe
characters (``[A-Za-z0-9._+-]``) — it is embedded verbatim in asset
cache-bust query strings, so spaces/parens are not allowed.

Resolution order:

1. A trusted ``WB_APP_VERSION`` deployment override. This is used by native
   releases whose immutable checkout SHA is known to the deployment scripts
   even when the runtime user cannot read ``.git``.
2. Two ``git`` invocations executed at the repo root:
   - ``git rev-list --count v2-base..HEAD`` — the commit counter.
   - ``git describe --always --dirty=+dirty`` — the short SHA (with
     optional ``+dirty`` suffix). Run without ``--tags`` so that
     lightweight tags like ``v2-base`` do NOT inject themselves into
     the output; we only want the SHA here.
3. Contents of the ``VERSION`` file at the repo root. Used in shipped
   artifacts (Docker images, .deb/.rpm packages) where ``.git`` is not
   present.
4. The literal string ``'unknown'`` if no valid source is available.

The result is cached at module level: the first call performs the work,
subsequent calls return the cached string. Use :func:`reset_cache` from
tests that need to re-evaluate (e.g. when monkeypatching ``subprocess.run``).

Security/robustness notes:
- ``subprocess.run`` is invoked with ``shell=False`` (the default), an
  argument list (never a string), a 3 second timeout, and ``check=False``.
- ``FileNotFoundError`` (no ``git`` binary), ``TimeoutExpired`` and
  generic ``OSError`` are swallowed and treated as "git unavailable".
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root is the parent of the ``services`` package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Module-level cache. ``None`` means "not yet resolved".
_CACHED: str | None = None

# Hard timeout (seconds) for each git invocation.
_GIT_TIMEOUT_SEC: int = 3

# Anchor tag marking the start of the v2 line. Commits since this tag
# form the second component of the version string.
V2_BASE_TAG: str = "v2-base"

# Versions are embedded verbatim in static-asset query strings. Keep the
# alphabet deliberately narrower than generic URI syntax and cap operator
# input so a malformed EnvironmentFile cannot create unbounded URLs/log data.
_MAX_VERSION_LENGTH: int = 128
_SAFE_VERSION_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._+-]{{0,{_MAX_VERSION_LENGTH - 1}}}\Z")


def _validate_version(value: str, *, source: str) -> str | None:
    """Return a URL-safe version candidate, or reject it with a safe fallback."""
    if len(value) <= _MAX_VERSION_LENGTH and _SAFE_VERSION_RE.fullmatch(value):
        return value
    logger.warning("Ignoring invalid application version from %s", source)
    return None


def _try_deploy_override() -> str | None:
    """Return a validated immutable deployment version when configured."""
    value = os.environ.get("WB_APP_VERSION")
    if value is None or value == "":
        return None
    return _validate_version(value, source="WB_APP_VERSION")


def _run_git(args: list[str]):
    """Execute git against REPO_ROOT with safe defaults.

    Uses ``--git-dir`` + ``--work-tree`` instead of ``-C`` to skip
    directory *discovery*. Discovery triggers git's "dubious ownership"
    check, which rejects repos whose working tree is owned by a uid that
    does not appear in ``/etc/passwd``. That happens on embedded
    controllers whose data volume was initialised on another host
    (e.g. WB-Irrigation: ``/mnt/data/wb-irrigation`` is uid 1001, no
    such user on the controller). Passing the dirs explicitly bypasses
    discovery and the associated check. ``REPO_ROOT`` is derived from
    ``__file__`` at import — never user input — so passing it directly
    is safe.

    Returns the ``CompletedProcess`` on success, ``None`` on any failure
    mode (missing binary, timeout, OS error). Never raises.
    """
    try:
        return subprocess.run(
            [
                "git",
                "--git-dir",
                str(REPO_ROOT / ".git"),
                "--work-tree",
                str(REPO_ROOT),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("git %s failed: %s", args, e)
        return None


def _try_git_describe() -> str | None:
    """Compose ``2.<N>+<sha>[+dirty]`` via two git invocations.

    Returns ``None`` if either invocation fails or yields empty output.
    Never raises.
    """
    count_proc = _run_git(["rev-list", "--count", f"{V2_BASE_TAG}..HEAD"])
    if count_proc is None or count_proc.returncode != 0:
        if count_proc is not None:
            # Git is present and refused — unexpected, surface it. The legitimate
            # "no git" cases (missing binary, timeout) stay at debug via _run_git's
            # except clause.
            logger.warning(
                "rev-list non-zero exit: rc=%s stderr=%r", count_proc.returncode, (count_proc.stderr or "").strip()
            )
        return None
    count = (count_proc.stdout or "").strip()
    if not count:
        return None

    sha_proc = _run_git(["describe", "--always", "--dirty=+dirty"])
    if sha_proc is None or sha_proc.returncode != 0:
        if sha_proc is not None:
            logger.warning(
                "describe non-zero exit: rc=%s stderr=%r", sha_proc.returncode, (sha_proc.stderr or "").strip()
            )
        return None
    sha = (sha_proc.stdout or "").strip()
    if not sha:
        return None

    return _validate_version(f"2.{count}+{sha}", source="git")


def _try_version_file() -> str | None:
    """Return the trimmed contents of ``<repo>/VERSION`` or ``None``."""
    try:
        text = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("VERSION file read failed: %s", e)
        return None
    return _validate_version(text, source="VERSION file") if text else None


def get_app_version() -> str:
    """Return the running application version, with caching.

    Resolution order: ``WB_APP_VERSION`` → git (count + describe) → ``VERSION``
    file → ``'unknown'``. The result is cached for the lifetime of the process. Call
    :func:`reset_cache` to force a re-evaluation (used in tests).
    """
    global _CACHED
    if _CACHED is not None:
        return _CACHED

    version = _try_deploy_override()
    if version is None:
        version = _try_git_describe()
    if version is None:
        version = _try_version_file()
    if version is None:
        version = "unknown"

    _CACHED = version
    return _CACHED


def reset_cache() -> None:
    """Clear the cached version. Intended for tests."""
    global _CACHED
    _CACHED = None
