import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")


def _logical_lines(path: str) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw_line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    assert not pending, f"unterminated continuation in {path}"
    return logical


def _requirements(path: str) -> list[Requirement]:
    parsed: list[Requirement] = []
    for line in _logical_lines(path):
        requirement = line.split("--hash=", 1)[0].strip()
        parsed.append(Requirement(requirement))
    return parsed


def _exact_pins(path: str) -> dict[str, Version]:
    pins: dict[str, Version] = {}
    for requirement in _requirements(path):
        specifiers = list(requirement.specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        pins[requirement.name.lower()] = Version(specifiers[0].version)
    return pins


def _assert_direct_requirements_are_locked(source: str, lock: str) -> None:
    pins = _exact_pins(lock)
    for requirement in _requirements(source):
        name = requirement.name.lower()
        assert name in pins, f"{requirement.name} is missing from {lock}"
        assert pins[name] in requirement.specifier, f"{requirement} rejects locked version {pins[name]}"


def test_production_requirements_have_compatible_exact_pins():
    _assert_direct_requirements_are_locked("requirements.txt", "requirements.lock")


def test_development_requirements_have_compatible_exact_pins():
    _assert_direct_requirements_are_locked("requirements-dev.txt", "requirements-dev.lock")


def test_every_locked_requirement_has_a_sha256_artifact_hash():
    for path in ("requirements.lock", "requirements-dev.lock"):
        logical = _logical_lines(path)
        assert logical, f"{path} must not be empty"
        for entry in logical:
            requirement = entry.split("--hash=", 1)[0].strip()
            assert HASH.search(entry), f"{requirement} has no SHA-256 artifact hash"
