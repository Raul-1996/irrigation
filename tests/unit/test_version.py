"""Unit tests for services.version.get_app_version.

Covers the resolution chain: ``git describe`` → ``VERSION`` file → ``'unknown'``,
the module-level cache, and ``reset_cache`` semantics.

The function is exercised in isolation via ``monkeypatch`` — we never spawn
a real ``git`` subprocess and never touch the on-disk ``VERSION`` file.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from services import version as version_mod
from services.version import get_app_version, reset_cache


@pytest.fixture(autouse=True)
def _reset_version_cache():
    """Ensure each test starts with a clean module-level cache."""
    reset_cache()
    yield
    reset_cache()


def _make_completed(stdout: str = '', returncode: int = 0, stderr: str = ''):
    """Build a stand-in for subprocess.CompletedProcess for monkeypatching."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ── git describe success ──────────────────────────────────────────────────

def test_get_app_version_returns_git_describe_output(monkeypatch):
    """When git describe succeeds, its trimmed output wins."""
    captured = {}

    def fake_run(args, **kwargs):
        captured['args'] = args
        captured['kwargs'] = kwargs
        return _make_completed(stdout='v1.2.3-4-gabc1234\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == 'v1.2.3-4-gabc1234'

    # Sanity-check security-relevant invocation parameters.
    assert captured['args'][0] == 'git'
    assert 'describe' in captured['args']
    assert '--tags' in captured['args']
    assert '--always' in captured['args']
    assert '--dirty' in captured['args']
    assert captured['kwargs'].get('shell', False) is False
    assert captured['kwargs'].get('check') is False
    assert captured['kwargs'].get('timeout') == 3
    assert captured['kwargs'].get('capture_output') is True


def test_get_app_version_strips_trailing_dirty_marker(monkeypatch):
    """``--dirty`` suffix is part of the describe output and must pass through."""
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _make_completed(stdout='v2.0.186-dirty\n', returncode=0),
    )
    assert get_app_version() == 'v2.0.186-dirty'


# ── git describe failure modes → VERSION file fallback ───────────────────

def test_falls_back_to_version_file_when_git_missing(monkeypatch, tmp_path):
    """No ``git`` binary → FileNotFoundError → VERSION file used."""
    def boom(*a, **kw):
        raise FileNotFoundError('git binary not on PATH')
    monkeypatch.setattr(subprocess, 'run', boom)

    fake_version = tmp_path / 'VERSION'
    fake_version.write_text('9.9.9-from-file\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '9.9.9-from-file'


def test_falls_back_to_version_file_on_timeout(monkeypatch, tmp_path):
    """git describe times out → VERSION file used."""
    def slow(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else 'git', timeout=3)
    monkeypatch.setattr(subprocess, 'run', slow)

    (tmp_path / 'VERSION').write_text('5.0.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '5.0.0'


def test_falls_back_to_version_file_on_oserror(monkeypatch, tmp_path):
    """Generic OSError from subprocess.run → VERSION file used."""
    def oserr(*a, **kw):
        raise OSError('Resource temporarily unavailable')
    monkeypatch.setattr(subprocess, 'run', oserr)

    (tmp_path / 'VERSION').write_text('4.2.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '4.2.0'


def test_falls_back_to_version_file_when_git_returns_nonzero(monkeypatch, tmp_path):
    """git ran but returned non-zero (no tags / not a repo) → VERSION file."""
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _make_completed(stdout='', returncode=128, stderr='fatal: not a git repository'),
    )
    (tmp_path / 'VERSION').write_text('3.1.4\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '3.1.4'


def test_falls_back_to_version_file_when_git_stdout_empty(monkeypatch, tmp_path):
    """rc=0 but empty stdout is still treated as a miss."""
    monkeypatch.setattr(
        subprocess, 'run',
        lambda *a, **kw: _make_completed(stdout='   \n', returncode=0),
    )
    (tmp_path / 'VERSION').write_text('7.0.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '7.0.0'


# ── No git AND no VERSION file → 'unknown' ───────────────────────────────

def test_returns_unknown_when_both_sources_fail(monkeypatch, tmp_path):
    """git unavailable + VERSION file missing → 'unknown'."""
    def boom(*a, **kw):
        raise FileNotFoundError('no git')
    monkeypatch.setattr(subprocess, 'run', boom)

    # tmp_path is empty — VERSION file does not exist.
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == 'unknown'


def test_returns_unknown_when_version_file_is_empty(monkeypatch, tmp_path):
    """Empty VERSION file is treated as a miss → 'unknown'."""
    monkeypatch.setattr(subprocess, 'run',
                        lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    (tmp_path / 'VERSION').write_text('   \n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == 'unknown'


# ── Caching behaviour ─────────────────────────────────────────────────────

def test_result_is_cached_across_calls(monkeypatch):
    """Second call must NOT invoke subprocess.run again."""
    calls = {'n': 0}

    def fake_run(*a, **kw):
        calls['n'] += 1
        return _make_completed(stdout='v1.0.0\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == 'v1.0.0'
    assert get_app_version() == 'v1.0.0'
    assert get_app_version() == 'v1.0.0'
    assert calls['n'] == 1


def test_reset_cache_forces_recompute(monkeypatch):
    """reset_cache() makes the next call recompute via subprocess.run."""
    answers = iter(['v1.0.0\n', 'v2.0.0\n'])

    def fake_run(*a, **kw):
        return _make_completed(stdout=next(answers), returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == 'v1.0.0'
    # Without reset, cached value is returned.
    assert get_app_version() == 'v1.0.0'
    reset_cache()
    assert get_app_version() == 'v2.0.0'
