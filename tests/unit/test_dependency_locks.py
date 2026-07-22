from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def _requirements(path: str) -> list[Requirement]:
    parsed: list[Requirement] = []
    for raw_line in (ROOT / path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            parsed.append(Requirement(line))
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
