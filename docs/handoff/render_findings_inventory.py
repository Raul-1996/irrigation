"""Apply package resolutions and render the findings inventory.

The JSON inventory remains authoritative. Finding IDs are immutable one-based
array positions: never reorder or insert entries, only append new records. The
Phase 1 prefix (#1..#152) is additionally protected by validation.
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
POST_REVIEW_ID_RE = re.compile(r"^PR-(\d{3})$")
DECISION_ID_RE = re.compile(r"^(IL|KD|RD)-(\d{3})$")
DISPOSITIONS = {"pending", "fixed", "deferred", "not_applicable"}
VERIFICATION_STATUSES = {"VERIFIED_REAL", "VERIFIED_FALSE", "INTENTIONAL_LIMITATION"}
HISTORICAL_COUNT = 152
HISTORICAL_STATUS_COUNTS = Counter({"VERIFIED_REAL": 147, "VERIFIED_FALSE": 5})


def _default_implementation(finding: dict) -> dict:
    if finding["status"] in {"VERIFIED_FALSE", "INTENTIONAL_LIMITATION"}:
        if finding["status"] == "VERIFIED_FALSE":
            return {
                "disposition": "not_applicable",
                "reason": "Фаза 1 признала находку ложной; production-fix не применяется.",
                "package": "phase1-verification",
                "commit": None,
                "tests": [],
            }
        return {
            "disposition": "not_applicable",
            "reason": "Это явно зафиксированное ограничение/решение продукта, а не дефект.",
            "package": "orchestrator-decision",
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
    schema_version = package_doc.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("unsupported PHASE2-PACKAGES schema")

    for finding in inventory:
        # Phase 2 resolutions remain ledger-owned. Appended post-review records
        # are authoritative in-place until their future package ledger exists.
        if finding.get("post_review_id") or finding.get("decision_id"):
            finding.setdefault("implementation", _default_implementation(finding))
        else:
            finding["implementation"] = _default_implementation(finding)

    seen_roots: set[int] = set()
    package_slugs: set[str] = set()
    for package in package_doc["packages"]:
        slug = package["slug"]
        if slug in package_slugs:
            raise ValueError(f"duplicate package slug: {slug}")
        package_slugs.add(slug)
        decision_only = package.get("decision_only", False)
        if not isinstance(decision_only, bool):
            raise ValueError(f"package {slug}: decision_only must be boolean")
        if decision_only and schema_version < 2:
            raise ValueError(f"package {slug}: decision-only package requires schema 2")
        commit = package["fix_commit"]
        merge_commit = package["merge_commit"]
        if decision_only:
            if commit is not None or merge_commit is not None:
                raise ValueError(f"package {slug}: decision-only commits must be null")
            if not package["resolutions"]:
                raise ValueError(f"package {slug}: decision-only package must resolve findings")
        else:
            if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
                raise ValueError(f"package {slug}: invalid fix_commit")
            if not isinstance(merge_commit, str) or not COMMIT_RE.fullmatch(merge_commit):
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
            if decision_only and disposition != "deferred":
                raise ValueError(f"finding #{finding_id}: decision-only package may only defer")
            resolution_commit = resolution.get("commit", commit)
            if disposition == "fixed" and (
                not isinstance(resolution_commit, str) or not COMMIT_RE.fullmatch(resolution_commit)
            ):
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
    if len(inventory) < HISTORICAL_COUNT:
        raise ValueError(f"expected at least {HISTORICAL_COUNT} findings, got {len(inventory)}")
    historical_status_counts = Counter(item["status"] for item in inventory[:HISTORICAL_COUNT])
    if historical_status_counts != HISTORICAL_STATUS_COUNTS:
        raise ValueError(f"historical prefix changed: {historical_status_counts}")

    post_review_ids: list[int] = []
    decision_ids: set[str] = set()
    for finding_id, finding in enumerate(inventory, start=1):
        status = finding["status"]
        if status not in VERIFICATION_STATUSES:
            raise ValueError(f"finding #{finding_id}: invalid verification status {status}")

        post_review_id = finding.get("post_review_id")
        decision_id = finding.get("decision_id")
        if finding_id <= HISTORICAL_COUNT:
            if post_review_id or decision_id:
                raise ValueError(f"historical finding #{finding_id}: appended ID is forbidden")
        elif bool(post_review_id) == bool(decision_id):
            raise ValueError(f"finding #{finding_id}: exactly one appended stable ID is required")

        if post_review_id:
            match = POST_REVIEW_ID_RE.fullmatch(post_review_id)
            if not match:
                raise ValueError(f"finding #{finding_id}: invalid post_review_id {post_review_id}")
            post_review_ids.append(int(match.group(1)))
            owners = finding.get("owner_packages")
            if not isinstance(owners, list) or not owners or len(owners) != len(set(owners)):
                raise ValueError(f"finding #{finding_id}: owner_packages must be a unique non-empty list")
            for key in ("title", "file", "current_location", "evidence", "reproduction"):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    raise ValueError(f"finding #{finding_id}: missing {key}")
        if decision_id:
            if not DECISION_ID_RE.fullmatch(decision_id) or decision_id in decision_ids:
                raise ValueError(f"finding #{finding_id}: invalid/duplicate decision_id {decision_id}")
            decision_ids.add(decision_id)

        reopens = finding.get("reopens", [])
        if not isinstance(reopens, list) or len(reopens) != len(set(reopens)):
            raise ValueError(f"finding #{finding_id}: reopens must be a unique list")
        if any(not isinstance(item, int) or not 1 <= item <= HISTORICAL_COUNT for item in reopens):
            raise ValueError(f"finding #{finding_id}: invalid historical reopen target")

        implementation = finding["implementation"]
        disposition = implementation["disposition"]
        if disposition not in DISPOSITIONS:
            raise ValueError(f"finding #{finding_id}: invalid disposition {disposition}")

        partial_remediations = finding.get("partial_remediations", [])
        if not isinstance(partial_remediations, list):
            raise ValueError(f"finding #{finding_id}: partial_remediations must be a list")
        if partial_remediations and disposition != "pending":
            raise ValueError(f"finding #{finding_id}: partial remediation requires pending disposition")
        partial_packages: set[str] = set()
        for partial in partial_remediations:
            if not isinstance(partial, dict):
                raise ValueError(f"finding #{finding_id}: partial remediation must be an object")
            package = partial.get("package")
            if not isinstance(package, str) or not package.strip() or package in partial_packages:
                raise ValueError(f"finding #{finding_id}: invalid/duplicate partial package")
            partial_packages.add(package)
            fix_commits = partial.get("fix_commits")
            if (
                not isinstance(fix_commits, list)
                or not fix_commits
                or any(not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit) for commit in fix_commits)
            ):
                raise ValueError(f"finding #{finding_id}: partial remediation needs unique full fix commits")
            if len(fix_commits) != len(set(fix_commits)):
                raise ValueError(f"finding #{finding_id}: partial remediation needs unique full fix commits")
            merge_commit = partial.get("merge_commit")
            if not isinstance(merge_commit, str) or not COMMIT_RE.fullmatch(merge_commit):
                raise ValueError(f"finding #{finding_id}: partial remediation needs a full merge commit")
            tests = partial.get("tests")
            if (
                not isinstance(tests, list)
                or not tests
                or any(not isinstance(test, str) or not test.strip() for test in tests)
            ):
                raise ValueError(f"finding #{finding_id}: partial remediation needs unique tests")
            if len(tests) != len(set(tests)):
                raise ValueError(f"finding #{finding_id}: partial remediation needs unique tests")
            for key in ("summary", "verification", "remaining"):
                if not isinstance(partial.get(key), str) or not partial[key].strip():
                    raise ValueError(f"finding #{finding_id}: partial remediation missing {key}")

        if status in {"VERIFIED_FALSE", "INTENTIONAL_LIMITATION"}:
            if disposition != "not_applicable":
                raise ValueError(f"finding #{finding_id}: non-defect decision must be not_applicable")
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
            if status != "VERIFIED_REAL":
                raise ValueError(f"finding #{finding_id}: only real findings may be pending")
            if (
                any(implementation[key] is not None for key in ("reason", "package", "commit"))
                or implementation["tests"]
            ):
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

    if post_review_ids and post_review_ids != list(range(1, len(post_review_ids) + 1)):
        raise ValueError(f"post-review IDs must be contiguous and ordered, got {post_review_ids}")

    return Counter(item["implementation"]["disposition"] for item in inventory)


def _render_finding(finding_id: int, finding: dict) -> list[str]:
    implementation = finding["implementation"]
    stable_id = finding.get("post_review_id") or finding.get("decision_id")
    rendered_id = f"#{finding_id}" + (f" / {stable_id}" if stable_id else "")
    lines = [
        f"### {rendered_id} [{finding['severity']}] `{finding['current_location']}` — {finding['title']}",
        "",
        f"- **Статус:** `{finding['status']}`.",
        f"- **Происхождение:** `{finding['main_or_branch']}`.",
        f"- **Верификация:** {finding['verified_reason']}",
        f"- **Репродукция/сценарий:** {finding['reproduction']}",
    ]
    if stable_id and finding.get("evidence"):
        lines.append(f"- **Evidence:** {finding['evidence']}")
    if finding.get("duplicate_of") is not None:
        lines.append(f"- **Дубль:** `#{finding['duplicate_of']}`.")
    if finding.get("reopens"):
        rendered_reopens = ", ".join(f"#{item}" for item in finding["reopens"])
        lines.append(f"- **Переоткрывает:** {rendered_reopens}.")
    if finding.get("owner_packages"):
        rendered_owners = ", ".join(f"`{owner}`" for owner in finding["owner_packages"])
        lines.append(f"- **Owner packages:** {rendered_owners}.")
    if finding.get("decision_notes"):
        lines.append(f"- **Решение оркестратора:** {finding['decision_notes']}")
    for partial in finding.get("partial_remediations", []):
        fix_commits = ", ".join(f"`{commit}`" for commit in partial["fix_commits"])
        tests = ", ".join(f"`{test}`" for test in partial["tests"])
        lines.extend(
            [
                f"- **Partial remediation `{partial['package']}`:** {partial['summary']}",
                f"  - Fix commits: {fix_commits}; merge: `{partial['merge_commit']}`.",
                f"  - Regression tests: {tests}.",
                f"  - Verification: {partial['verification']}",
                f"  - Осталось: {partial['remaining']}",
            ]
        )
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
    indexed = list(enumerate(inventory, start=1))
    historical_real = [(index, item) for index, item in indexed[:HISTORICAL_COUNT] if item["status"] == "VERIFIED_REAL"]
    historical_false = [
        (index, item) for index, item in indexed[:HISTORICAL_COUNT] if item["status"] == "VERIFIED_FALSE"
    ]
    post_review = [
        (index, item) for index, item in indexed if item.get("post_review_id") and item["status"] == "VERIFIED_REAL"
    ]
    intentional = [(index, item) for index, item in indexed if item["status"] == "INTENTIONAL_LIMITATION"]
    rejected = [(index, item) for index, item in indexed[HISTORICAL_COUNT:] if item["status"] == "VERIFIED_FALSE"]
    status_counts = Counter(item["status"] for item in inventory)
    lines = [
        "# Финальный инвентарь аудита wb-irrigation",
        "",
        "Исторический реестр Фаз 1–3 сохранён неизменяемым префиксом #1–#152. После полного post-review текущего HEAD добавлены новые и переоткрытые корневые дефекты с отдельными стабильными PR-ID, owner packages и проверяемыми сценариями.",
        "",
        f"- Всего записей: **{len(inventory)}** (152 исторических + {len(inventory) - HISTORICAL_COUNT} post-review решений).",
        f"- `VERIFIED_REAL`: **{status_counts['VERIFIED_REAL']}**.",
        f"- `VERIFIED_FALSE`: **{status_counts['VERIFIED_FALSE']}**.",
        f"- `INTENTIONAL_LIMITATION`: **{status_counts['INTENTIONAL_LIMITATION']}**.",
        "- `UNVERIFIED`: **0**.",
        "- `duplicate_of` означает подтверждённый дубль корневой причины; запись сохранена для трассируемости.",
        f"- Remediation disposition: **{disposition_counts['fixed']} fixed · {disposition_counts['deferred']} deferred · {disposition_counts['pending']} pending · {disposition_counts['not_applicable']} not_applicable**.",
        "",
        "Подробные отчёты и оговорки: `PHASE1-RAW-REPORTS-2026-07-18.md`. Машиночитаемый источник: `findings-inventory.json`. Интеграционные пакеты: `PHASE2-PACKAGES.json`.",
        "",
        f"## Post-review remediation — {len(post_review)}",
        "",
    ]
    for finding_id, finding in post_review:
        lines.extend(_render_finding(finding_id, finding))
    lines.extend([f"## Intentional limitations — {len(intentional)}", ""])
    for finding_id, finding in intentional:
        lines.extend(_render_finding(finding_id, finding))
    lines.extend([f"## Post-review rejected — {len(rejected)}", ""])
    for finding_id, finding in rejected:
        lines.extend(_render_finding(finding_id, finding))
    lines.extend([f"## Исторические подтверждённые — {len(historical_real)}", ""])
    for finding_id, finding in historical_real:
        lines.extend(_render_finding(finding_id, finding))
    lines.extend([f"## Исторические отклонённые — {len(historical_false)}", ""])
    for finding_id, finding in historical_false:
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
