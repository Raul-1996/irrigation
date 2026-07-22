"""Tests for services/reports.py."""

import os
import sqlite3
import time
from datetime import datetime

import pytest

os.environ["TESTING"] = "1"


class TestReports:
    def test_build_report_text(self, test_db):
        try:
            from services.reports import build_report_text

            with patch_db(test_db):
                result = build_report_text(period="today", fmt="brief")
                assert isinstance(result, str)
        except (ImportError, AttributeError):
            pytest.skip("reports module not fully available")

    def test_import(self):
        try:
            from services.reports import build_report_text

            assert callable(build_report_text)
        except ImportError:
            pytest.skip("reports not available")

    def test_today_and_yesterday_use_controller_local_calendar_bounds(self, test_db, monkeypatch):
        from services import reports

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 7, 19, 12, 0, 0)
                return value if tz is None else value.astimezone(tz)

        previous_tz = os.environ.get("TZ")
        monkeypatch.setenv("TZ", "Etc/GMT-3")
        time.tzset()
        try:
            zone = test_db.create_zone({"name": "Calendar zone", "duration": 10, "group_id": 1})
            with sqlite3.connect(test_db.db_path) as conn:
                conn.executemany(
                    "INSERT INTO water_usage(zone_id, liters, timestamp) VALUES (?, ?, ?)",
                    [
                        (zone["id"], 100.0, "2026-07-17 20:59:59"),
                        (zone["id"], 3.0, "2026-07-17 21:00:00"),
                        (zone["id"], 4.0, "2026-07-18 20:59:59"),
                        (zone["id"], 5.0, "2026-07-18 21:00:00"),
                        (zone["id"], 6.0, "2026-07-19 08:00:00"),
                        (zone["id"], 200.0, "2026-07-19 21:00:00"),
                    ],
                )
            monkeypatch.setattr(reports, "db", test_db)
            monkeypatch.setattr(reports, "datetime", FrozenDateTime)

            today = reports.build_report_text(period="today", fmt="brief")
            yesterday = reports.build_report_text(period="yesterday", fmt="brief")

            assert "всего воды 11.0 л" in today
            assert "всего воды 7.0 л" in yesterday
        finally:
            if previous_tz is None:
                monkeypatch.delenv("TZ", raising=False)
            else:
                monkeypatch.setenv("TZ", previous_tz)
            time.tzset()


def patch_db(db):
    from unittest.mock import patch

    return (
        patch("services.reports.db", db)
        if hasattr(__import__("services.reports", fromlist=["db"]), "db")
        else patch.dict({})
    )
