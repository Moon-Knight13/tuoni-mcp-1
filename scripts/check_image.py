"""Check runtime trust/privilege boundaries and assess each unfixed image advisory.

The policy applies to the pinned base and a non-root stdio deployment with no
capabilities, no new privileges, and a read-only root filesystem. It is not an
acceptance of privileged, root, or externally exposed HTTP deployments.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import subprocess
import sys
import sysconfig


COMPROMISED_ROOT = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"


def runtime_probe() -> dict:
    import certifi

    contexts = (ssl.create_default_context(), ssl.create_default_context(cafile=certifi.where()))
    trusted = [context.get_ca_certs(binary_form=True) for context in contexts]
    setid_files = []
    for directory, children, files in os.walk("/"):
        if directory == "/":
            children[:] = [name for name in children if name not in {"proc", "sys", "dev"}]
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                setid_files.append(str(path))
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
    stdlib = Path(sysconfig.get_path("stdlib"))
    installers = [str(path) for pattern in ("pip", "pip-*.dist-info")
                  for path in (stdlib / "site-packages").glob(pattern)]
    if (stdlib / "ensurepip").exists():
        installers.append(str(stdlib / "ensurepip"))
    archive_tar = [str(path) for directory in ("/usr/share/perl", "/usr/lib", "/usr/local/lib")
                   for path in Path(directory).rglob("Archive/Tar.pm")]
    certifi_version = importlib.metadata.version("certifi")
    checks = {
        "non_root": os.geteuid() == 10001 and os.getegid() == 10001,
        "no_capabilities": int(status["CapEff"].strip(), 16) == 0 and int(status["CapBnd"].strip(), 16) == 0,
        "no_new_privileges": status["NoNewPrivs"].strip() == "1",
        "no_setid_files": not setid_files,
        "no_installers": not installers and importlib.util.find_spec("pip") is None,
        "patched_certifi": tuple(int(part) for part in certifi_version.split(".")) >= (2026, 10, 19),
        "trusted_ca_bundles": all(trusted) and all(
            hashlib.sha256(certificate).hexdigest() != COMPROMISED_ROOT
            for bundle in trusted for certificate in bundle
        ),
        "no_infocmp": shutil.which("infocmp") is None,
        "no_homed": not any(Path(directory, "systemd-homed").exists()
                             for directory in ("/usr/lib/systemd", "/lib/systemd", "/usr/bin", "/usr/sbin")),
        "no_archive_tar": not archive_tar,
    }
    return {"checks": checks, "certifi": certifi_version, "setid_files": setid_files,
            "installers": installers, "archive_tar_modules": archive_tar}


def assess(report: dict, runtime: dict, policy: dict, *, image_id: str, base_image: str,
           today: date | None = None) -> dict:
    if policy.get("schema_version") != 1 or policy.get("base_image") != base_image:
        raise ValueError("Image policy does not cover this base image; review the new base first")
    if date.fromisoformat(policy["review_before"]) <= (today or date.today()):
        raise ValueError("Image policy assessment has expired; review current advisories")
    if report.get("Metadata", {}).get("ImageID") != image_id:
        raise ValueError("Vulnerability report does not describe the inspected image")
    if (report.get("Metadata", {}).get("OS", {}).get("Family") != "debian"
            or not isinstance(report.get("Results"), list)
            or not any(result.get("Class") == "os-pkgs" for result in report["Results"])):
        raise ValueError("The image assessment requires a complete Debian OS scan")
    checks = runtime.get("checks", {})
    required = {"non_root", "no_capabilities", "no_new_privileges", "no_setid_files", "no_installers",
                "patched_certifi", "trusted_ca_bundles", "no_infocmp", "no_homed", "no_archive_tar"}
    if any(checks.get(name) is not True for name in required):
        raise ValueError("Runtime checks failed: " + ", ".join(sorted(name for name in required if checks.get(name) is not True)))
    assessed = []
    for result in report.get("Results", []):
        for finding in result.get("Vulnerabilities", []):
            if finding.get("Severity") not in {"HIGH", "CRITICAL"}:
                continue
            identifier = finding["VulnerabilityID"]
            rule = policy.get("assessments", {}).get(identifier)
            if finding.get("FixedVersion"):
                raise ValueError(f"{identifier} has an available fix; update the image")
            if (result.get("Class") != "os-pkgs" or finding.get("Severity") != "HIGH" or not rule
                    or finding["PkgName"] not in rule["packages"]
                    or finding["InstalledVersion"] not in rule["versions"]
                    or any(checks.get(name) is not True for name in rule["conditions"])):
                raise ValueError(f"Unreviewed image finding: {identifier} in {finding['PkgName']}")
            assessed.append({"id": identifier, "package": finding["PkgName"],
                             "version": finding["InstalledVersion"], "reason": rule["reason"]})
    return {"passed": True, "image_id": image_id, "base_image": base_image,
            "deployment": policy["deployment"], "review_before": policy["review_before"],
            "scan_sha256": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
            "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
            "runtime": runtime, "assessed_findings": assessed}


def check(image: str, report: dict, policy: dict) -> dict:
    inspected = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    if inspected["Config"].get("User") != "10001:10001":
        raise ValueError("The image must default to UID/GID 10001")
    image_id = inspected["Id"]
    # Pin the probe to the same image ID that the scanner inspected, even if a
    # mutable local tag changes while this check runs.
    command = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--entrypoint", "python", image_id, "-", "--probe"]
    process = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True,
                             check=True, timeout=60)
    runtime = json.loads(process.stdout)
    base = inspected["Config"].get("Labels", {}).get("org.opencontainers.image.base.name")
    return assess(report, runtime, policy, image_id=image_id, base_image=base)


def main() -> int:
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(runtime_probe(), sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("image_risks.json"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        report = check(args.image, json.loads(args.report.read_text()), json.loads(args.policy.read_text()))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"Image assessment failed: {exc}", file=sys.stderr)
        return 1
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
