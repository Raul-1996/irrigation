"""Regression tests for issue #43: per-step validation in program wizard."""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_file(relpath):
    with open(os.path.join(PROJECT_ROOT, relpath), encoding="utf-8") as f:
        return f.read()


class TestWizardValidation:
    """Wizard must validate required fields before step transition (#43)."""

    def test_validate_wizard_step_function_exists(self):
        html = read_file("templates/programs.html")
        assert "function validateWizardStep(" in html

    def test_wizard_next_calls_validator(self):
        html = read_file("templates/programs.html")
        # Extract the wizardNext function body
        idx = html.index("function wizardNext()")
        body = html[idx : idx + 300]
        assert "validateWizardStep(wizardStep)" in body
        # Validator must abort transition on failure
        assert "if (!validateWizardStep(wizardStep)) return;" in body

    def test_step1_validates_name_not_empty(self):
        html = read_file("templates/programs.html")
        idx = html.index("function validateWizardStep(")
        body = html[idx : idx + 2000]
        # Step 1 branch must check wizardData.name
        assert "step === 1" in body
        assert "wizardData.name" in body

    def test_step2_validates_schedule(self):
        html = read_file("templates/programs.html")
        idx = html.index("function validateWizardStep(")
        body = html[idx : idx + 2000]
        assert "step === 2" in body
        # Each schedule branch must be validated
        assert "schedule_type === 'weekdays'" in body
        assert "wizardData.days.length === 0" in body
        assert "schedule_type === 'interval'" in body
        assert "schedule_type === 'even-odd'" in body

    def test_step4_validates_zones_selected(self):
        html = read_file("templates/programs.html")
        idx = html.index("function validateWizardStep(")
        body = html[idx : idx + 2000]
        assert "step === 4" in body
        assert "wizardData.zones.length === 0" in body

    def test_name_input_has_id_for_focus(self):
        """Validator focuses the name input on error — needs an id."""
        html = read_file("templates/programs.html")
        assert 'id="wiz-name"' in html

    def test_aria_invalid_css_rule_exists(self):
        """Red-border style for invalid inputs must be defined."""
        css = read_file("static/css/programs.css")
        assert '[aria-invalid="true"]' in css
