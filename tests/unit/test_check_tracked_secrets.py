from __future__ import annotations

import subprocess

from tools.check_tracked_secrets import main, scan_repository, scan_text


def test_reports_sshpass_literal_without_echoing_value(tmp_path, capsys):
    unsafe_value = "synthetic-credential-for-regression"
    markdown = "sshpass " + "-p " + unsafe_value + " ssh root@example.invalid\n"
    tracked = tmp_path / "README.md"
    tracked.write_text(markdown)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)

    assert main(["--repo", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "README.md:1: sshpass-literal" in output.err
    assert unsafe_value not in output.err
    assert unsafe_value not in output.out


def test_all_tracked_files_include_markdown_but_not_untracked_files(tmp_path):
    tracked = tmp_path / "notes.md"
    tracked.write_text("safe documentation\n")
    untracked = tmp_path / "local.md"
    untracked.write_text("sshpass " + "-p " + "synthetic-untracked-value\n")

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "notes.md"], check=True)

    report = scan_repository(tmp_path)
    assert report.scanned_paths == ("notes.md",)
    assert report.findings == ()


def test_environment_backed_sshpass_is_allowed():
    command = 'SSHPASS="${WB_ROOT_PASSWORD:?required}" sshpass -e ssh root@example.invalid'
    assert scan_text("deploy.md", command) == ()


def test_root_password_literal_in_markdown_is_rejected():
    for password_label in ("password", "пароль"):
        markdown = "- **Root " + password_label + ":** " + "synthetic-root-value"
        findings = scan_text("access.md", markdown)
        assert [finding.rule_id for finding in findings] == ["root-password-literal"]


def test_tls_verification_bypass_is_rejected():
    unsafe_setting = "NODE_TLS_REJECT_" + "UNAUTHORIZED=0"
    findings = scan_text("access.md", unsafe_setting)
    assert [finding.rule_id for finding in findings] == ["tls-verification-disabled"]


def test_provider_token_prefix_is_rejected():
    synthetic_token = "AK" + "IA" + ("A" * 16)
    findings = scan_text("fixture.txt", synthetic_token)
    assert [finding.rule_id for finding in findings] == ["provider-token"]
