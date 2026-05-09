"""Unit tests for services.version.get_app_version.

Format under test: ``2.<N> (<short_sha>[+dirty])``.
Resolution chain: git (rev-list count + describe sha) → ``VERSION`` file → ``'unknown'``.

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


def _scripted_run(scripts):
    """Build a fake subprocess.run that responds to argv pattern matches.

    ``scripts`` is a list of ``(predicate, completed)`` pairs. The fake
    iterates the list and returns the first ``completed`` whose predicate
    matches the argv list. ``predicate`` may be a callable taking argv,
    or a substring tuple — every element must appear in argv.
    """
    def _matches(predicate, argv):
        if callable(predicate):
            return predicate(argv)
        return all(token in argv for token in predicate)

    def fake_run(argv, **kwargs):
        for predicate, completed in scripts:
            if _matches(predicate, argv):
                return completed
        raise AssertionError(f'unexpected git invocation: {argv}')

    return fake_run


# ── git success: 2.<N> (<sha>) ─────────────────────────────────────────────

def test_returns_formatted_version_from_count_and_sha(monkeypatch):
    """Two successful git calls compose ``2.113 (f75c54c)``."""
    captured = []

    def fake_run(argv, **kwargs):
        captured.append((argv, kwargs))
        if 'rev-list' in argv:
            return _make_completed(stdout='113\n', returncode=0)
        if 'describe' in argv:
            return _make_completed(stdout='f75c54c\n', returncode=0)
        raise AssertionError(f'unexpected: {argv}')

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == '2.113 (f75c54c)'

    assert len(captured) == 2
    rev_list_argv = captured[0][0]
    describe_argv = captured[1][0]

    # rev-list call: counts commits since the v2-base anchor tag.
    assert rev_list_argv[0] == 'git'
    assert 'rev-list' in rev_list_argv
    assert '--count' in rev_list_argv
    assert 'v2-base..HEAD' in rev_list_argv

    # describe call: short SHA + optional +dirty marker.
    assert describe_argv[0] == 'git'
    assert 'describe' in describe_argv
    assert '--always' in describe_argv
    assert '--dirty=+dirty' in describe_argv
    # We deliberately do NOT pass --tags so v2-base never appears in output.
    assert '--tags' not in describe_argv

    # Security-relevant kwargs on each call.
    for _argv, kwargs in captured:
        assert kwargs.get('shell', False) is False
        assert kwargs.get('check') is False
        assert kwargs.get('timeout') == 3
        assert kwargs.get('capture_output') is True


def test_dirty_marker_passes_through_inside_parentheses(monkeypatch):
    """``+dirty`` from describe ends up inside the parentheses."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout='113\n', returncode=0)
        return _make_completed(stdout='f75c54c+dirty\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert get_app_version() == '2.113 (f75c54c+dirty)'


def test_zero_count_at_anchor_commit(monkeypatch):
    """Sitting exactly on the v2-base tag yields ``2.0 (<sha>)``."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout='0\n', returncode=0)
        return _make_completed(stdout='bd6213e\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert get_app_version() == '2.0 (bd6213e)'


# ── git failure modes → VERSION file fallback ─────────────────────────────

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
    """git times out → VERSION file used."""
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


def test_falls_back_when_rev_list_returns_nonzero(monkeypatch, tmp_path):
    """rev-list non-zero (e.g. tag missing) → VERSION file used."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(
                stdout='', returncode=128,
                stderr="fatal: ambiguous argument 'v2-base..HEAD'",
            )
        # describe is never reached on the failure path
        raise AssertionError('describe should not run after rev-list failure')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    (tmp_path / 'VERSION').write_text('3.1.4\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '3.1.4'


def test_falls_back_when_describe_returns_nonzero(monkeypatch, tmp_path):
    """rev-list ok but describe non-zero → VERSION file used."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout='113\n', returncode=0)
        return _make_completed(stdout='', returncode=128, stderr='fatal: not a git repository')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    (tmp_path / 'VERSION').write_text('7.0.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '7.0.0'


def test_falls_back_when_rev_list_stdout_empty(monkeypatch, tmp_path):
    """rc=0 but empty count is still treated as a miss."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout='   \n', returncode=0)
        raise AssertionError('describe should not run when count is empty')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    (tmp_path / 'VERSION').write_text('6.0.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '6.0.0'


def test_falls_back_when_describe_stdout_empty(monkeypatch, tmp_path):
    """rc=0 on describe but empty SHA → fallback."""
    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout='113\n', returncode=0)
        return _make_completed(stdout='\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)
    (tmp_path / 'VERSION').write_text('8.0.0\n', encoding='utf-8')
    monkeypatch.setattr(version_mod, 'REPO_ROOT', tmp_path)

    assert get_app_version() == '8.0.0'


# ── No git AND no VERSION file → 'unknown' ───────────────────────────────

def test_returns_unknown_when_both_sources_fail(monkeypatch, tmp_path):
    """git unavailable + VERSION file missing → 'unknown'."""
    def boom(*a, **kw):
        raise FileNotFoundError('no git')
    monkeypatch.setattr(subprocess, 'run', boom)

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
    """Subsequent calls must NOT invoke subprocess.run again."""
    calls = {'n': 0}

    def fake_run(argv, **kwargs):
        calls['n'] += 1
        if 'rev-list' in argv:
            return _make_completed(stdout='113\n', returncode=0)
        return _make_completed(stdout='f75c54c\n', returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == '2.113 (f75c54c)'
    assert get_app_version() == '2.113 (f75c54c)'
    assert get_app_version() == '2.113 (f75c54c)'
    # Two calls (rev-list + describe) on first invocation, zero after.
    assert calls['n'] == 2


def test_reset_cache_forces_recompute(monkeypatch):
    """reset_cache() makes the next call recompute."""
    counts = iter(['100\n', '200\n'])
    shas = iter(['aaaaaaa\n', 'bbbbbbb\n'])

    def fake_run(argv, **kwargs):
        if 'rev-list' in argv:
            return _make_completed(stdout=next(counts), returncode=0)
        return _make_completed(stdout=next(shas), returncode=0)

    monkeypatch.setattr(subprocess, 'run', fake_run)

    assert get_app_version() == '2.100 (aaaaaaa)'
    # Without reset, cached value is returned.
    assert get_app_version() == '2.100 (aaaaaaa)'
    reset_cache()
    assert get_app_version() == '2.200 (bbbbbbb)'
