#!/usr/bin/env bash
# Run a pinned, checksum-verified scanner. Keep every finding in the JSON report;
# reject fixable findings and assess every remaining advisory against the
# reviewed policy and actual runtime. Unknown findings fail closed.
set -euo pipefail

image=${1:?Supply the local image name}
report=${2:?Supply the JSON report path}
task_scan_dir=$(mktemp -d)
trap 'rm -rf -- "$task_scan_dir"' EXIT
archive="$task_scan_dir/trivy.tar.gz"
curl --fail --silent --show-error --location --retry 3 --connect-timeout 10 --max-time 120 \
  https://github.com/aquasecurity/trivy/releases/download/v0.75.0/trivy_0.75.0_Linux-64bit.tar.gz \
  --output "$archive"
printf '%s  %s\n' c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f "$archive" \
  | sha256sum --check --status
tar --extract --gzip --file "$archive" --directory "$task_scan_dir" trivy
cache_dir="${RUNNER_TEMP:-$task_scan_dir}/tuoni-mcp-trivy-cache"
"$task_scan_dir/trivy" image --cache-dir "$cache_dir" --scanners vuln --no-progress \
  --severity HIGH,CRITICAL --format json --output "$report" "$image"
"$task_scan_dir/trivy" image --cache-dir "$cache_dir" --scanners vuln --no-progress \
  --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image"
uv run --frozen --all-extras python scripts/check_image.py \
  --image "$image" --report "$report" --out "${report%.json}-acceptance.json"
