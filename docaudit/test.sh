#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
pattern="${1:-*_test.py}"
pinned_site_revision="$(cat data/mutation-site-revision.txt)"
pinned_site="$PWD/data/mutation-site"

checkout_pinned_site() {
  if [ ! -e "$pinned_site/.git" ]; then
    git -C "$(python3 -c 'import doccoverage; print(doccoverage.SITE_ROOT)')" worktree add -q --detach "$pinned_site" "$pinned_site_revision"
  fi
  git -C "$pinned_site" checkout -q --detach "$pinned_site_revision"
}

failed_tests=()
for test_script in $pattern; do
  echo "== $test_script"
  if [ "$test_script" = mutation_test.py ]; then
    checkout_pinned_site
    DOCAUDIT_SITE_ROOT="$pinned_site" python3 "$test_script" || failed_tests+=("$test_script")
    continue
  fi
  python3 "$test_script" || failed_tests+=("$test_script")
done
if (( ${#failed_tests[@]} > 0 )); then
  echo "FAILED: ${failed_tests[*]}" >&2
  exit 1
fi
