from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ci_third_party_actions_are_immutable_and_permissions_are_read_only():
    action_refs = re.findall(r"^\s*-\s+uses:\s+([^#\s]+)", CI_WORKFLOW, flags=re.MULTILINE)

    assert action_refs
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref in action_refs)
    assert re.search(r"^permissions:\n\s+contents:\s+read$", CI_WORKFLOW, flags=re.MULTILINE)
