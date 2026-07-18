"""Apply Phase 2 package resolutions and render the findings inventory.

The JSON inventory remains authoritative. Finding IDs are the immutable one-based
array positions established by Phase 1; never reorder or insert entries.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = ROOT / "findings-inventory.json"
PACKAGES_PATH = ROOT / "PHASE2-PACKAGES.json"
MARKDOWN_PATH = ROOT / "findings-inventory.md"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DISPOSITIONS = {"pending", "fixed", "deferred", "not_applicable"}


def _default_implementation(finding: dict) -> dict:
    if finding["status"] == "VERIFIED_FALSE":
        return {
            "disposition": "not_applicable",
            "reason": "Фаза 1 признала находку ложной; production-fix не применяется.",
            "package": "phase1-verification",
            "commit": None,
            "tests": [],
        }
    return {
        "disposition": "pending",
        "reason": None,
        "package": None,
        "commit": None,
        "tests": [],
    }


def apply_packages(inventory: list[dict], package_doc: dict) -> None:
    for finding in inventory:
        finding["implementation"] = _default_implementation(finding)

    seen_roots: set[int] = set()
    package_slugs: set[str] = set()
    for package in package_doc["packages"]:
        slug = package["slug"]
        if slug in package_slugs:
            raise ValueError(f"duplicate package slug: {slug}")
        package_slugs.add(slug)
        commit = package["fix_commit"]
        if not COMMIT_RE.fullmatch(commit):
            raise ValueError(f"package {slug}: invalid fix_commit")
        if not COMMIT_RE.fullmatch(package["merge_commit"]):
            raise ValueError(f"package {slug}: invalid merge_commit")
        for resolution in package["resolutions"]:
            finding_id = resolution["id"]
            if finding_id in seen_roots:
                raise ValueError(f"finding #{finding_id} resolved more than once")
            seen_roots.add(finding_id)
            finding = inventory[finding_id - 1]
            if finding.get("duplicate_of") is not None:
                raise ValueError(f"finding #{finding_id} is a duplicate, resolve its root")
            disposition = resolution["disposition"]
            resolution_commit = resolution.get("commit", commit)
            if disposition == "fixed" and not COMMIT_RE.fullmatch(resolution_commit):
                raise ValueError(f"finding #{finding_id}: invalid resolution commit")
            implementation = {
                "disposition": disposition,
                "reason": resolution["reason"],
                "package": slug,
                "commit": resolution_commit if disposition == "fixed" else None,
                "tests": resolution.get("tests", []),
            }
            finding["implementation"] = implementation

    for finding in inventory:
        duplicate_of = finding.get("duplicate_of")
        if duplicate_of is not None:
            finding["implementation"] = deepcopy(inventory[duplicate_of - 1]["implementation"])


def validate(inventory: list[dict], package_doc: dict) -> Counter:
    if len(inventory) != 152:
        raise ValueError(f"expected 152 findings, got {len(inventory)}")
    status_counts = Counter(item["status"] for item in inventory)
    if status_counts != Counter({"VERIFIED_REAL": 147, "VERIFIED_FALSE": 5}):
        raise ValueError(f"unexpected status counts: {status_counts}")
    if package_doc.get("schema_version") != 1:
        raise ValueError("unsupported PHASE2-PACKAGES schema")

    for finding_id, finding in enumerate(inventory, start=1):
        implementation = finding["implementation"]
        disposition = implementation["disposition"]
        if disposition not in DISPOSITIONS:
            raise ValueError(f"finding #{finding_id}: invalid disposition {disposition}")
        if finding["status"] == "VERIFIED_FALSE":
            if disposition != "not_applicable":
                raise ValueError(f"finding #{finding_id}: false finding must be not_applicable")
        elif disposition == "not_applicable":
            raise ValueError(f"finding #{finding_id}: real finding cannot be not_applicable")
        if disposition == "fixed":
            if not implementation["reason"] or not implementation["package"]:
                raise ValueError(f"finding #{finding_id}: fixed resolution lacks metadata")
            if not COMMIT_RE.fullmatch(implementation["commit"] or ""):
                raise ValueError(f"finding #{finding_id}: fixed resolution lacks full commit")
            if not implementation["tests"] or len(implementation["tests"]) != len(set(implementation["tests"])):
                raise ValueError(f"finding #{finding_id}: fixed resolution needs unique tests")
        elif disposition == "deferred":
            if not implementation["reason"] or not implementation["package"]:
                raise ValueError(f"finding #{finding_id}: deferred resolution lacks reason/package")
            if implementation["commit"] is not None or implementation["tests"]:
                raise ValueError(f"finding #{finding_id}: deferred resolution cannot have commit/tests")
        elif disposition == "pending":
            if finding["status"] != "VERIFIED_REAL":
                raise ValueError(f"finding #{finding_id}: only real findings may be pending")
            if any(implementation[key] is not None for key in ("reason", "package", "commit")) or implementation["tests"]:
                raise ValueError(f"finding #{finding_id}: pending resolution must be empty")

        duplicate_of = finding.get("duplicate_of")
        if duplicate_of is not None:
            if not 1 <= duplicate_of < finding_id:
                raise ValueError(f"finding #{finding_id}: invalid duplicate target")
            target = inventory[duplicate_of - 1]
            if target.get("duplicate_of") is not None:
                raise ValueError(f"finding #{finding_id}: duplicate chain")
            if finding["status"] != target["status"] or finding["file"] != target["file"]:
                raise ValueError(f"finding #{finding_id}: duplicate classification mismatch")
            if implementation != target["implementation"]:
                raise ValueError(f"finding #{finding_id}: duplicate implementation mismatch")

    return Counter(item["implementation"]["disposition"] for item in inventory)


def _render_finding(finding_id: int, finding: dict) -> list[str]:
    implementation = finding["implementation"]
    lines = [
        f"### #{finding_id} [{finding['severity']}] `{finding['current_location']}` — {finding['title']}",
        "",
        f"- **Статус:** `{finding['status']}`.",
        f"- **Происхождение:** `{finding['main_or_branch']}`.",
        f"- **Верификация:** {finding['verified_reason']}",
        f"- **Репродукция/сценарий:** {finding['reproduction']}",
    ]
    if finding.get("duplicate_of") is not None:
        lines.append(f"- **Дубль:** `#{finding['duplicate_of']}`.")
    if finding.get("decision_notes"):
        lines.append(f"- **Решение оркестратора:** {finding['decision_notes']}")
    lines.append(f"- **Disposition Фаз 2–3:** `{implementation['disposition']}`.")
    if implementation["reason"]:
        lines.append(f"- **Причина disposition:** {implementation['reason']}")
    if implementation["package"]:
        lines.append(f"- **Пакет:** `{implementation['package']}`.")
    if implementation["commit"]:
        lines.append(f"- **Fix commit:** `{implementation['commit']}`.")
    if implementation["tests"]:
        rendered_tests = ", ".join(f"`{nodeid}`" for nodeid in implementation["tests"])
        lines.append(f"- **Regression tests:** {rendered_tests}.")
    lines.append("")
    return lines


def render_markdown(inventory: list[dict], disposition_counts: Counter) -> str:
    real = [(index, item) for index, item in enumerate(inventory, start=1) if item["status"] == "VERIFIED_REAL"]
    false = [(index, item) for index, item in enumerate(inventory, start=1) if item["status"] == "VERIFIED_FALSE"]
    lines = [
        "# Финальный инвентарь аудита wb-irrigation",
        "",
        "Фаза 1 завершена 2026-07-18: каждая исходная находка получила требуемые независимые роли, все расхождения разрешены оркестратором, пять линз diff-review завершены.",
        "",
        "- Всего записей: **152** (138 исходных + 14 новых).",
        "- `VERIFIED_REAL`: **147**.",
        "- `VERIFIED_FALSE`: **5**.",
        "- `UNVERIFIED`: **0**.",
        "- `duplicate_of` означает подтверждённый дубль корневой причины; запись сохранена для трассируемости.",
        f"- Phase 2 disposition: **{disposition_counts['fixed']} fixed · {disposition_counts['deferred']} deferred · {disposition_counts['pending']} pending · {disposition_counts['not_applicable']} not_applicable**.",
        "",
        "Подробные отчёты и оговорки: `PHASE1-RAW-REPORTS-2026-07-18.md`. Машиночитаемый источник: `findings-inventory.json`. Интеграционные пакеты: `PHASE2-PACKAGES.json`.",
        "",
        f"## Подтверждённые — {len(real)}",
        "",
    ]
    for finding_id, finding in real:
        lines.extend(_render_finding(finding_id, finding))
    lines.extend([f"## Отклонённые — {len(false)}", ""])
    for finding_id, finding in false:
        lines.extend(_render_finding(finding_id, finding))
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate without rewriting files")
    args = parser.parse_args()

    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    package_doc = json.loads(PACKAGES_PATH.read_text(encoding="utf-8"))
    apply_packages(inventory, package_doc)
    disposition_counts = validate(inventory, package_doc)
    rendered_json = json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
    rendered_markdown = render_markdown(inventory, disposition_counts)

    if args.check:
        if INVENTORY_PATH.read_text(encoding="utf-8") != rendered_json:
            raise SystemExit("findings-inventory.json is not synchronized with PHASE2-PACKAGES.json")
        if MARKDOWN_PATH.read_text(encoding="utf-8") != rendered_markdown:
            raise SystemExit("findings-inventory.md is not synchronized with findings-inventory.json")
        print(f"inventory valid: {dict(sorted(disposition_counts.items()))}")
        return

    INVENTORY_PATH.write_text(rendered_json, encoding="utf-8")
    MARKDOWN_PATH.write_text(rendered_markdown, encoding="utf-8")
    print(f"inventory rendered: {dict(sorted(disposition_counts.items()))}")


if __name__ == "__main__":
    main()
