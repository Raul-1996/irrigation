"""Phase-2 regressions for the Programs API and wizard UI."""

from pathlib import Path

import pytest

from routes import programs_api

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _create_program(app, *, name="Original", schedule_type="weekdays", interval_days=None):
    zone = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
    return app.db.create_program(
        {
            "name": name,
            "time": "06:00",
            "schedule_type": schedule_type,
            "interval_days": interval_days,
            "days": [] if schedule_type == "interval" else [0],
            "zones": [zone["id"]],
        }
    )


def _program_payload(program, **updates):
    payload = {
        "name": program["name"],
        "time": program["time"],
        "type": program["type"],
        "schedule_type": program["schedule_type"],
        "interval_days": program["interval_days"],
        "even_odd": program["even_odd"],
        "days": program["days"],
        "zones": program["zones"],
        "extra_times": program["extra_times"],
    }
    payload.update(updates)
    return payload


def _template_source():
    return (PROJECT_ROOT / "templates/programs.html").read_text(encoding="utf-8")


def _between(source, start, end):
    return source[source.index(start) : source.index(end, source.index(start))]


class TestPutValidationParity:
    def test_put_rejects_invalid_schedule_type_without_persisting_it(self, admin_client, app):
        program = _create_program(app)

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json=_program_payload(program, schedule_type="daily"),
        )

        assert response.status_code == 400
        assert response.get_json()["success"] is False
        assert app.db.get_program(program["id"])["schedule_type"] == "weekdays"

    def test_partial_put_uses_stored_schedule_fields_instead_of_raising_keyerror(self, admin_client, app):
        program = _create_program(app)

        response = admin_client.put(f"/api/programs/{program['id']}", json={"name": "Renamed"})

        assert response.status_code == 200
        assert response.get_json()["name"] == "Renamed"

    def test_put_switch_to_interval_requires_interval_days(self, admin_client, app):
        program = _create_program(app)

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json={"schedule_type": "interval"},
        )

        assert response.status_code == 400
        assert "interval_days" in response.get_json()["message"]
        assert app.db.get_program(program["id"])["schedule_type"] == "weekdays"


class TestIntervalDaysValidation:
    @pytest.mark.parametrize("interval_days", [-1, "0", "3", 1.5, 31, True])
    def test_post_rejects_unsafe_interval_days(self, admin_client, app, interval_days):
        zone = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        response = admin_client.post(
            "/api/programs",
            json={
                "name": "Unsafe interval",
                "time": "06:00",
                "schedule_type": "interval",
                "interval_days": interval_days,
                "days": [],
                "zones": [zone["id"]],
            },
        )

        assert response.status_code == 400
        assert "interval_days" in response.get_json()["message"]

    @pytest.mark.parametrize("interval_days", [1, 30])
    def test_post_accepts_bounded_integer_interval_days(self, admin_client, app, interval_days):
        zone = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})

        response = admin_client.post(
            "/api/programs",
            json={
                "name": "Safe interval",
                "time": "06:00",
                "schedule_type": "interval",
                "interval_days": interval_days,
                "days": [],
                "zones": [zone["id"]],
            },
        )

        assert response.status_code == 201
        assert response.get_json()["interval_days"] == interval_days

    def test_put_rejects_unsafe_interval_days(self, admin_client, app):
        program = _create_program(app, schedule_type="interval", interval_days=3)

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json={"interval_days": -1},
        )

        assert response.status_code == 400
        assert app.db.get_program(program["id"])["interval_days"] == 3


class TestCandidateExtraTimesConflictChecks:
    def test_post_blocks_conflict_found_only_at_candidate_extra_time(self, admin_client, app, monkeypatch):
        zone = app.db.create_zone({"name": "Z1", "duration": 10, "group_id": 1})
        calls = []

        def fake_check(**kwargs):
            candidate_times = [kwargs["time"], *(kwargs.get("extra_times") or [])]
            calls.extend(candidate_times)
            return [{"program_id": 99}] if "12:00" in candidate_times else []

        monkeypatch.setattr(programs_api.db.programs, "check_program_conflicts", fake_check)

        response = admin_client.post(
            "/api/programs",
            json={
                "name": "Candidate extras",
                "time": "06:00",
                "extra_times": ["12:00", "18:00"],
                "days": [0],
                "zones": [zone["id"]],
            },
        )

        assert response.status_code == 200
        assert response.get_json()["success"] is False
        assert calls == ["06:00", "12:00", "18:00"]

    def test_put_blocks_conflict_found_only_at_candidate_extra_time(self, admin_client, app, monkeypatch):
        program = _create_program(app)
        calls = []

        def fake_check(**kwargs):
            candidate_times = [kwargs["time"], *(kwargs.get("extra_times") or [])]
            calls.extend(candidate_times)
            return [{"program_id": 99}] if "12:00" in candidate_times else []

        monkeypatch.setattr(programs_api.db.programs, "check_program_conflicts", fake_check)

        response = admin_client.put(
            f"/api/programs/{program['id']}",
            json={"extra_times": ["12:00"]},
        )

        assert response.status_code == 200
        assert response.get_json()["success"] is False
        assert calls == ["06:00", "12:00"]
        assert app.db.get_program(program["id"])["extra_times"] == []

    def test_conflict_probe_checks_each_candidate_start_time(self, admin_client, monkeypatch):
        calls = []

        def fake_check(**kwargs):
            candidate_times = [kwargs["time"], *(kwargs.get("extra_times") or [])]
            calls.extend(candidate_times)
            return [{"program_id": 99}] if "18:00" in candidate_times else []

        monkeypatch.setattr(programs_api.db.programs, "check_program_conflicts", fake_check)

        response = admin_client.post(
            "/api/programs/check-conflicts",
            json={
                "program_id": None,
                "time": "06:00",
                "extra_times": ["12:00", "18:00"],
                "days": [0],
                "zones": [1],
            },
        )

        assert response.status_code == 200
        assert response.get_json()["has_conflicts"] is True
        assert calls == ["06:00", "12:00", "18:00"]


class TestProgramsWizardResponseHandling:
    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("async function toggleEnabled", "async function duplicateProgram"),
            ("async function duplicateProgram", "async function runProgram"),
            ("async function deleteProgram", "function editProgram"),
            ("async function saveWizard", "// Toast"),
        ],
    )
    def test_mutations_report_http_and_success_false_errors(self, start, end):
        body = _between(_template_source(), start, end)

        assert "!resp.ok" in body
        assert "data.success === false" in body
        assert "showToast(" in body

    def test_wizard_escapes_zone_names_before_assigning_inner_html(self):
        source = _template_source()
        zone_step = _between(source, "// Step 4: Zones", "// Step 5: Summary")

        assert "${escapeHtml(z.name)}" in zone_step
        assert "<div>${z.name}</div>" not in zone_step

    def test_manual_run_ui_reports_accepted_request_without_claiming_watering_started(self):
        run_program = _between(_template_source(), "async function runProgram", "async function deleteProgram")

        assert "const data = await readResponseJson(resp)" in run_program
        assert "!resp.ok || data.success === false" in run_program
        assert "data.message || '⏳ Запрос на полив принят'" in run_program
        assert "Полив запущен" not in run_program
