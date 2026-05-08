"""Application version resolution.

Single source of truth for the running app version. Resolution order:

1. ``git describe --tags --always --dirty`` executed at the repo root.
   Works in source checkouts, including detached HEAD (``--always`` falls
   back to the abbreviated commit SHA) and dirty trees (``--dirty`` adds
   a ``-dirty`` suffix).
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

# Hard timeout (seconds) for the ``git describe`` invocation.
_GIT_TIMEOUT_SEC: int = 3


def _try_git_describe() -> Optional[str]:
    """Return ``git describe --tags --always --dirty`` output or ``None``.

    Returns ``None`` on any failure mode: missing ``git`` binary, non-zero
    exit (e.g. no ``.git`` directory), timeout, OS error, empty output.
    Never raises.
    """
    try:
        result = subprocess.run(
            ['git', '-C', str(REPO_ROOT), 'describe', '--tags', '--always', '--dirty'],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
            shell=False,  # explicit for safety review even though it's the default
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("git describe failed: %s", e)
        return None

    if result.returncode != 0:
        logger.debug("git describe non-zero exit: rc=%s stderr=%r",
                     result.returncode, (result.stderr or '').strip())
        return None

    out = (result.stdout or '').strip()
    return out or None


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

    Resolution order: ``git describe`` → ``VERSION`` file → ``'unknown'``.
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
