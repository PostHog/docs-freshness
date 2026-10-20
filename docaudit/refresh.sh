#!/bin/bash
set -Eeuo pipefail

if [ -z "${DOCAUDIT_BACKGROUND_PRIORITY:-}" ]; then
    export DOCAUDIT_BACKGROUND_PRIORITY=1
    if command -v taskpolicy >/dev/null 2>&1; then
        exec taskpolicy -b nice -n 20 bash "$0" "$@"
    fi
    exec nice -n 20 bash "$0" "$@"
fi

should_pull=true
should_report=true
for argument in "$@"; do
    case "$argument" in
        --no-pull) should_pull=false ;;
        --no-report) should_report=false ;;
        *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

started_at=$SECONDS
current_step='checkout guard'
routes_candidate=''
guard_errors=''
guard_output=''
failure_reason=''
success_report_attempted=false
step_pid=''

finish_run() {
    exit_status=$?
    trap - EXIT
    if [ -n "$routes_candidate" ]; then
        rm -f "$routes_candidate"
    fi
    if [ -n "$guard_errors" ]; then
        rm -f "$guard_errors"
    fi
    if [ -n "$guard_output" ]; then
        rm -f "$guard_output"
    fi
    if [ "$exit_status" -ne 0 ] && "$success_report_attempted"; then
        printf 'Success reporting failed (exit %s); not sending a failed event because the success event may have landed\n' "$exit_status" >&2
        exit "$exit_status"
    fi
    if [ "$exit_status" -ne 0 ] && "$should_report"; then
        python3 report_metrics.py --failed --reason "${failure_reason:-$current_step: command failed (exit $exit_status)}" --duration-seconds "$((SECONDS - started_at))" || printf 'Failure reporting failed\n' >&2
    fi
    exit "$exit_status"
}
exit_on_signal() {
    failure_reason="$current_step: interrupted by $1"
    if [ -n "$step_pid" ]; then
        kill -TERM "$step_pid" 2>/dev/null || true
    fi
    exit "$2"
}
run_step() {
    "$@" &
    step_pid=$!
    local step_status=0
    wait "$step_pid" || step_status=$?
    step_pid=''
    return "$step_status"
}
trap finish_run EXIT
trap 'exit_on_signal SIGHUP 129' HUP
trap 'exit_on_signal SIGINT 130' INT
trap 'exit_on_signal SIGTERM 143' TERM

guard_errors=$(mktemp "${TMPDIR:-/tmp}/docaudit-guard.XXXXXX")
guard_output=$(mktemp "${TMPDIR:-/tmp}/docaudit-guard-output.XXXXXX")
guard_status=0
read -r -d '' checkout_guard_script <<'PYGUARD' || true
import subprocess
import sys
from doccoverage import SITE_ROOT, MONOREPO_ROOT, POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT, TOOL_ROOT

def read_git(checkout, *arguments):
    return subprocess.run(['git', '-C', str(checkout), *arguments], check=True, capture_output=True, text=True).stdout.strip()

checkouts = (SITE_ROOT, MONOREPO_ROOT, POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT)
for checkout in checkouts:
    try:
        branch = read_git(checkout, 'symbolic-ref', '--short', 'HEAD')
        upstream = ''
        try:
            upstream = read_git(checkout, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')
        except subprocess.CalledProcessError:
            pass
        try:
            default_upstream = read_git(checkout, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD')
        except subprocess.CalledProcessError:
            raise ValueError(f'refs/remotes/origin/HEAD is missing; run: git -C {checkout} remote set-head origin --auto') from None
        default_branch = default_upstream.removeprefix('origin/')
        if upstream != default_upstream:
            raise ValueError(f'branch {branch!r} tracks {upstream!r}, not default {default_upstream!r}')
        if branch != default_branch and checkout != SITE_ROOT:
            raise ValueError(f'branch {branch!r} is not default {default_branch!r} (upstream {upstream!r})')
        local_only_commits = read_git(checkout, 'rev-list', '--count', '@{upstream}..HEAD')
        if local_only_commits != '0':
            raise ValueError(f'branch {branch!r} has {local_only_commits} commits not on {upstream!r}')
        if read_git(checkout, 'status', '--porcelain'):
            raise ValueError('worktree is dirty')
    except (subprocess.CalledProcessError, ValueError) as failure:
        raise SystemExit(f'{checkout}: {failure}')
if sys.argv[1] == 'true':
    try:
        tool_changes = read_git(TOOL_ROOT, 'status', '--porcelain', '--', '.', ':(exclude)data')
    except subprocess.CalledProcessError as failure:
        raise SystemExit(f'{TOOL_ROOT}: {failure}')
    if tool_changes:
        raise SystemExit(f'{TOOL_ROOT}: tool code has uncommitted changes, so tool_commit would not match the code that ran (commit them or use --no-report): ' + '; '.join(tool_changes.splitlines()))
for checkout in checkouts:
    print(checkout)
PYGUARD
run_step python3 -c "$checkout_guard_script" "$should_report" >"$guard_output" 2>"$guard_errors" || guard_status=$?
if [ "$guard_status" -ne 0 ]; then
    failure_reason="checkout guard: $(<"$guard_errors")"
    printf '%s\n' "$failure_reason" >&2
    exit 1
fi
cat "$guard_errors" >&2
checkout_paths=$(<"$guard_output")
if "$should_pull"; then
    while IFS= read -r checkout; do
        current_step="pull $checkout"
        GIT_TERMINAL_PROMPT=0 run_step git -C "$checkout" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 pull --ff-only
    done <<< "$checkout_paths"
fi

current_step='attribution prefetch'
run_step python3 attribution.py --prefetch
current_step='history index'
run_step python3 history_index.py

current_step='live routes'
routes_candidate=$(mktemp data/live-routes.XXXXXX)
read -r -d '' live_routes_script <<'PY' || true
import sys
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from urllib.parse import urlsplit

with urllib.request.urlopen('https://posthog.com/sitemap/sitemap-0.xml', timeout=60) as response:
    sitemap = ElementTree.fromstring(response.read())
routes = sorted({
    urlsplit(location.text).path.rstrip('/')
    for location in sitemap.iter() if location.tag.rsplit('}', 1)[-1] == 'loc' and location.text
})
if len(routes) < 10000:
    raise SystemExit(f'Refusing to replace data/live-routes.txt: fetched only {len(routes)} routes; old file retained')
Path(sys.argv[1]).write_text('\n'.join(routes) + '\n', encoding='utf-8')
print(f'Refreshed {len(routes)} live routes')
PY
run_step python3 -c "$live_routes_script" "$routes_candidate"
mv "$routes_candidate" data/live-routes.txt

candidate_directory=output.partial
rm -rf "$candidate_directory"
mkdir "$candidate_directory"
current_step='audit'
DOCAUDIT_OUTPUT_ROOT="$PWD/$candidate_directory" run_step python3 all_sections.py > "$candidate_directory/all-sections.txt"
current_step='coverage'
DOCAUDIT_OUTPUT_ROOT="$PWD/$candidate_directory" run_step python3 doccoverage.py --json "$candidate_directory/coverage.json" > "$candidate_directory/coverage.txt"
current_step='attribution build'
DOCAUDIT_OUTPUT_ROOT="$PWD/$candidate_directory" run_step python3 attribution.py --build
current_step='docs inventory'
DOCAUDIT_OUTPUT_ROOT="$PWD/$candidate_directory" run_step python3 docs_inventory.py

current_step='archive output'
previous_directory=''
if [ -d output ]; then
    for required_file in output/all-findings.txt output/coverage.json; do
        if [ ! -f "$required_file" ]; then
            printf 'Cannot archive output: missing %s\n' "$required_file" >&2
            exit 1
        fi
    done
    findings_file=output/all-findings.txt
    previous_date=$(date -r "$findings_file" +%Y-%m-%d)
    previous_directory="output-$previous_date"
    archive_suffix=2
    while [ -e "$previous_directory" ]; do
        previous_directory="output-$previous_date-$archive_suffix"
        archive_suffix=$((archive_suffix + 1))
    done
    mv output "$previous_directory"
fi
mv "$candidate_directory" output
current_step='next steps'
if [ -n "$previous_directory" ]; then
    run_step python3 next_steps.py --previous "$previous_directory"
fi
if [ -z "$previous_directory" ]; then
    run_step python3 next_steps.py
fi
printf '%s/output/next-steps.md\n' "$PWD"
current_step='report metrics'
if "$should_report"; then
    success_report_attempted=true
    run_step python3 report_metrics.py --duration-seconds "$((SECONDS - started_at))"
fi
