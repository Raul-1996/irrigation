"""Application version resolution.

Single source of truth for the running app version.

Format: ``2.<N> (<short_sha>[+dirty])`` where:
- ``2`` is the major (v2 line, anchored at the ``v2-base`` git tag).
- ``<N>`` is the count of commits between ``v2-base`` and ``HEAD``.
- ``<short_sha>`` is the abbreviated commit SHA of ``HEAD``.
- ``+dirty`` is appended when the working tree has uncommitted changes.

Resolution order:

1. Two ``git`` invocations executed at the repo root:
   - ``git rev-list --count v2-base..HEAD`` — the commit counter.
   - ``git describe --always --dirty=+dirty`` — the short SHA (with
     optional ``+dirty`` suffix). Run without ``--tags`` so that
     lightweight tags like ``v2-base`` do NOT inject themselves into
     the output; we only want the SHA here.
2. Contents of the ``VERSION`` file at the repo root. Used in shipped
   artifacts (Docker images, .deb/.rpm packages) where ``.git`` is not
   present.
3. The literal string ``'unknown'`` if neither source is available.

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
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Repo root is the parent of the ``services`` package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Module-level cache. ``None`` means "not yet resolved".
_CACHED: Optional[str] = None

# Hard timeout (seconds) for each git invocation.
_GIT_TIMEOUT_SEC: int = 3

# Anchor tag marking the start of the v2 line. Commits since this tag
# form the second component of the version string.
V2_BASE_TAG: str = 'v2-base'


def _run_git(args: list[str]):
    """Execute ``git -C REPO_ROOT <args>`` with safe defaults.

    Returns the ``CompletedProcess`` on success, ``None`` on any failure
    mode (missing binary, timeout, OS error). Never raises.
    """
    try:
        return subprocess.run(
            # ``-c safe.directory=<root>`` keeps git from refusing to read the
            # repo when run as a different uid than the working tree owner
            # (e.g. systemd-launched root vs. data-volume owner uid).
            ['git', '-c', f'safe.directory={REPO_ROOT}', '-C', str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("git %s failed: %s", args, e)
        return None


def _try_git_describe() -> Optional[str]:
    """Compose ``2.<N> (<sha>[+dirty])`` via two git invocations.

    Returns ``None`` if either invocation fails or yields empty output.
    Never raises.
    """
    count_proc = _run_git(['rev-list', '--count', f'{V2_BASE_TAG}..HEAD'])
    if count_proc is None or count_proc.returncode != 0:
        if count_proc is not None:
            logger.debug("rev-list non-zero exit: rc=%s stderr=%r",
                         count_proc.returncode, (count_proc.stderr or '').strip())
        return None
    count = (count_proc.stdout or '').strip()
    if not count:
        return None

    sha_proc = _run_git(['describe', '--always', '--dirty=+dirty'])
    if sha_proc is None or sha_proc.returncode != 0:
        if sha_proc is not None:
            logger.debug("describe non-zero exit: rc=%s stderr=%r",
                         sha_proc.returncode, (sha_proc.stderr or '').strip())
        return None
    sha = (sha_proc.stdout or '').strip()
    if not sha:
        return None

    return f'2.{count} ({sha})'


def _try_version_file() -> Optional[str]:
    """Return the trimmed contents of ``<repo>/VERSION`` or ``None``."""
    try:
        text = (REPO_ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("VERSION file read failed: %s", e)
        return None
    return text or None


def get_app_version() -> str:
    """Return the running application version, with caching.

    Resolution order: git (count + describe) → ``VERSION`` file → ``'unknown'``.
    The result is cached for the lifetime of the process. Call
    :func:`reset_cache` to force a re-evaluation (used in tests).
    """
    global _CACHED
    if _CACHED is not None:
        return _CACHED

    version = _try_git_describe()
    if version is None:
        version = _try_version_file()
    if version is None:
        version = 'unknown'

    _CACHED = version
    return _CACHED


def reset_cache() -> None:
    """Clear the cached version. Intended for tests."""
    global _CACHED
    _CACHED = None
