"""Image acceptance fails closed when scan identity, risk, or runtime changes."""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path

import pytest

from scripts.check_image import assess


@pytest.fixture
def assessment():
    policy = json.loads((Path(__file__).resolve().parents[1] / "scripts/image_risks.json").read_text())
    runtime = {"checks": dict.fromkeys((
        "non_root", "no_capabilities", "no_new_privileges", "no_setid_files", "no_installers",
        "patched_certifi", "trusted_ca_bundles", "no_infocmp", "no_homed", "no_archive_tar",
    ), True)}
    finding = {"VulnerabilityID": "CVE-2026-76642", "PkgName": "mount",
               "InstalledVersion": "2.41.5-0+deb13u1", "Severity": "HIGH"}
    report = {"Metadata": {"ImageID": "sha256:inspected", "OS": {"Family": "debian"}},
              "Results": [{"Class": "os-pkgs", "Vulnerabilities": [finding]}]}

    def check(**changes):
        return assess(report, runtime, policy, image_id="sha256:inspected",
                      base_image=policy["base_image"], today=date(2026, 10, 22), **changes)

    return check, report, runtime, policy, finding


def test_reviewed_unfixed_advisory_records_reason_and_image_identity(assessment):
    check, _, _, policy, _ = assessment
    result = check()
    assert result["passed"]
    assert result["image_id"] == "sha256:inspected"
    assert result["assessed_findings"][0]["reason"] == policy["assessments"]["CVE-2026-76642"]["reason"]
    assert len(result["scan_sha256"]) == len(result["policy_sha256"]) == 64


@pytest.mark.parametrize("change", [
    {"VulnerabilityID": "CVE-unknown"}, {"PkgName": "unexpected"},
    {"InstalledVersion": "unreviewed"}, {"Severity": "CRITICAL"},
])
def test_unknown_advisories_packages_versions_and_critical_findings_fail(assessment, change):
    check, _, _, _, finding = assessment
    finding.update(change)
    with pytest.raises(ValueError, match="Unreviewed"):
        check()


def test_available_fixes_cannot_be_accepted_as_unfixed(assessment):
    check, _, _, _, finding = assessment
    finding["FixedVersion"] = "patched"
    with pytest.raises(ValueError, match="available fix"):
        check()


@pytest.mark.parametrize("condition", [
    "non_root", "no_capabilities", "no_new_privileges", "no_setid_files", "no_installers",
    "patched_certifi", "trusted_ca_bundles", "no_infocmp", "no_homed", "no_archive_tar",
])
@pytest.mark.parametrize("value", [False, None, "true"])
def test_missing_or_failed_runtime_protections_fail(assessment, condition, value):
    check, _, runtime, _, _ = assessment
    runtime["checks"][condition] = value
    with pytest.raises(ValueError, match="Runtime checks failed"):
        check()


def test_review_applies_only_to_the_pinned_base_and_exact_scanned_image(assessment):
    check, report, _, policy, _ = assessment
    report["Metadata"]["ImageID"] = "sha256:different"
    with pytest.raises(ValueError, match="inspected image"):
        check()
    report["Metadata"]["ImageID"] = "sha256:inspected"
    original_base = policy["base_image"]
    with pytest.raises(ValueError, match="base image"):
        assess(report, {"checks": {}}, policy, image_id="sha256:inspected", base_image=original_base + "changed")


def test_expired_assessment_and_incomplete_or_wrong_scan_fail(assessment):
    check, report, _, policy, _ = assessment
    policy["review_before"] = "2026-10-22"
    with pytest.raises(ValueError, match="expired"):
        check()
    policy["review_before"] = "2026-11-22"
    original = copy.deepcopy(report)
    for broken in ({}, {"Metadata": original["Metadata"]}, {**original, "Results": []}):
        report.clear()
        report.update(broken)
        with pytest.raises(ValueError):
            check()


def test_os_assessments_do_not_exempt_python_dependencies(assessment):
    check, report, _, _, _ = assessment
    report["Results"][0]["Class"] = "lang-pkgs"
    report["Results"].append({"Class": "os-pkgs", "Vulnerabilities": []})
    with pytest.raises(ValueError, match="Unreviewed"):
        check()
