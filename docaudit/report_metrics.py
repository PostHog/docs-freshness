import argparse
import collections
import datetime
import http.client
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_KEY = 'sTMFPsFhdP1Ssg'
CAPTURE_URL = 'https://us.i.posthog.com/i/v0/e/'
FINDING_KINDS = ('app-url', 'docs-fragment', 'docs-link', 'sdk-method', 'settings-anchor', 'ui-label', 'ui-label-minor')
TOOL_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = TOOL_ROOT.parent / 'config' / 'posthog.json'
FALLBACK_CAPTURE_DISTINCT_ID = 'docs-audit'
MAX_NEW_FINDINGS = 50
INVENTORY_EVENT_COLUMNS = (
    'url', 'source_repo', 'page_class', 'doc_kind', 'is_route', 'redirected_away', 'created_at', 'agent_line_share', 'nonblank_lines',
    'template_family_size', 'site_inbound_links', 'in_nav', 'redirects_into', 'app_links',
)
MAX_INVENTORY_EVENT_BYTES = 900_000
INVENTORY_EVENT_UUID_NAMESPACE = uuid.UUID('4f9c1d52-6a0e-4c56-9d7e-2b8f3e1a7c40')
SPOOL_DIRECTORY = Path(os.environ.get('DOCAUDIT_SPOOL_ROOT', TOOL_ROOT / 'data' / 'unsent-events'))
TRANSPORT_FAILURES = (urllib.error.URLError, OSError, http.client.HTTPException)
SPOOL_WRITE_FAILED_EXIT_CODE = 3


class SpoolWriteError(Exception):
    pass


def require_field(record, name, path='next-steps'):
    if name not in record:
        raise ValueError(f'Missing required field: {path}.{name}')
    return record[name]


def slug_name(name):
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    if not slug:
        raise ValueError(f'Cannot slug name: {name!r}')
    return slug


def read_checkouts():
    from doccoverage import MONOREPO_ROOT, POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT, SITE_ROOT
    return {'site': SITE_ROOT, 'monorepo': MONOREPO_ROOT, 'posthog_js': POSTHOG_JS_ROOT, 'posthog_python': POSTHOG_PYTHON_ROOT, 'tool': TOOL_ROOT}


def read_commits(allow_unavailable=False):
    commits = {}
    for name, checkout in read_checkouts().items():
        try:
            revision = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            if allow_unavailable:
                continue
            raise
        if not revision:
            if allow_unavailable:
                continue
            raise ValueError(f'Missing HEAD SHA: {checkout}')
        commits[f'{name}_commit'] = revision
    return commits


def read_commit_properties_for_failed_event():
    try:
        return read_commits(allow_unavailable=True)
    except Exception as failure:
        return {'commits_unavailable_reason': f'{type(failure).__name__}: {failure}'}


def read_capture_distinct_id():
    return json.loads(CONFIG_PATH.read_text())['capture_distinct_id']


def read_capture_identity_for_failed_event():
    try:
        return read_capture_distinct_id(), {}
    except Exception as failure:
        return FALLBACK_CAPTURE_DISTINCT_ID, {'config_unavailable_reason': f'{type(failure).__name__}: {failure}'}


def require_known_keys(totals, known_keys, path):
    unknown_keys = sorted(set(totals) - set(known_keys))
    if unknown_keys:
        raise ValueError(f'Unknown keys in {path}: {unknown_keys}')
    return totals


def build_payload(report, duration_seconds, commits, failure_reason=None):
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError('duration_seconds must be finite and nonnegative')
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    distinct_id, config_properties = read_capture_identity_for_failed_event() if failure_reason is not None else (read_capture_distinct_id(), {})
    properties = {'$process_person_profile': False, 'duration_seconds': duration_seconds, **commits, **config_properties}
    payload = {'api_key': API_KEY, 'event': 'docs_audit_run_completed', 'distinct_id': distinct_id, 'properties': properties, 'timestamp': timestamp, 'uuid': str(uuid.uuid4())}
    if failure_reason is not None:
        properties.update(status='failed', failure_reason=failure_reason, run_date=datetime.date.today().isoformat())
        return payload
    from attribution import CATEGORIES
    from next_steps import COVERAGE_SOURCES, SUGGESTION_CONFIDENCES
    properties.update(status='success', run_date=require_field(report, 'run_date'))
    properties['findings_total'] = require_field(report, 'total_findings')
    kind_totals = require_known_keys(require_field(report, 'totals_by_kind'), FINDING_KINDS, 'next-steps.totals_by_kind')
    for kind in FINDING_KINDS:
        properties[f'findings_{slug_name(kind)}'] = kind_totals.get(kind, 0)
    has_previous_run = 'delta' in report
    if has_previous_run != ('new_since_last_run' in report):
        raise ValueError('next-steps.delta and next-steps.new_since_last_run must be both present or both absent')
    properties['has_previous_run'] = has_previous_run
    if has_previous_run:
        delta = require_field(report, 'delta')
        for name in ('new', 'resolved', 'still_open'):
            properties[f'findings_{name}'] = require_field(delta, name, 'delta')
    confidence_totals = require_field(report, 'totals_by_confidence')
    for confidence in SUGGESTION_CONFIDENCES:
        properties[f'suggestions_{confidence}'] = require_field(confidence_totals, confidence, 'totals_by_confidence')
    category_totals = require_known_keys(require_field(report, 'attribution_totals'), CATEGORIES, 'next-steps.attribution_totals')
    for category in CATEGORIES:
        properties[f'category_{category}'] = require_field(category_totals, category, 'attribution_totals')
    coverage_gaps = require_field(report, 'coverage_gaps')
    for source in COVERAGE_SOURCES:
        gaps = require_field(coverage_gaps, source, 'coverage_gaps')
        properties[f'coverage_undocumented_{slug_name(source)}'] = len(require_field(gaps, 'undocumented', f'coverage_gaps.{source}'))
    for guard in require_field(report, 'upstream_guards'):
        name = slug_name(require_field(guard, 'name', 'upstream_guards'))
        if f'guard_{name}_total' in properties:
            raise ValueError(f'Duplicate guard slug: {name}')
        properties[f'guard_{name}_caught'] = require_field(guard, 'caught_at_pr_time', f'upstream_guards.{name}')
        properties[f'guard_{name}_total'] = require_field(guard, 'total', f'upstream_guards.{name}')
    findings = require_field(report, 'attributed_findings')
    pages = collections.Counter(require_field(finding, 'page', 'attributed_findings') for finding in findings)
    properties['findings_by_page'] = json.dumps(dict(sorted(pages.items())), sort_keys=True)
    if not has_previous_run:
        return payload
    new_findings = []
    for page_findings in require_field(report, 'new_since_last_run').values():
        for finding in page_findings:
            new_findings.append({name: require_field(finding, name, 'new_since_last_run') for name in ('page', 'kind', 'detail', 'line')})
    included_new_findings = new_findings[:MAX_NEW_FINDINGS]
    properties['new_findings'] = json.dumps(included_new_findings, sort_keys=True)
    properties['new_findings_included'] = len(included_new_findings)
    return payload


def inventory_run_properties(inventory):
    from docs_inventory import DOC_KINDS, PAGE_CLASSES
    counts = require_field(inventory, 'counts', 'docs-inventory')
    doc_kind_counts = require_field(counts, 'by_doc_kind', 'docs-inventory.counts')
    require_known_keys(doc_kind_counts, DOC_KINDS, 'docs-inventory.counts.by_doc_kind')
    page_class_counts = require_field(counts, 'by_page_class', 'docs-inventory.counts')
    require_known_keys(page_class_counts, PAGE_CLASSES, 'docs-inventory.counts.by_page_class')
    pages_total = require_field(counts, 'pages_total', 'docs-inventory.counts')
    properties = {'docs_pages_total': pages_total}
    for kind in DOC_KINDS:
        properties[f'docs_pages_{slug_name(kind)}'] = require_field(doc_kind_counts, kind, 'docs-inventory.counts.by_doc_kind')
    for name in PAGE_CLASSES:
        properties[f'docs_pages_{slug_name(name)}'] = require_field(page_class_counts, name, 'docs-inventory.counts.by_page_class')
    page_class_total = sum(properties[f'docs_pages_{slug_name(name)}'] for name in PAGE_CLASSES)
    if page_class_total != pages_total:
        raise ValueError(f'docs-inventory.counts.by_page_class sums to {page_class_total}, not pages_total {pages_total}')
    properties['docs_pages_with_redirect_source'] = require_field(counts, 'redirected_away', 'docs-inventory.counts')
    properties['docs_sitemap_template_routes'] = len(require_field(inventory, 'sitemap_template_routes', 'docs-inventory'))
    properties['docs_template_families'] = require_field(counts, 'template_families', 'docs-inventory.counts')
    connectors = require_field(inventory, 'connectors', 'docs-inventory')
    properties['connectors_docs_url_elsewhere'] = len(require_field(connectors, 'docs_url_elsewhere', 'docs-inventory.connectors'))
    properties['docs_duplicate_title_groups'] = len(require_field(inventory, 'duplicate_title_groups', 'docs-inventory'))
    return properties


def compact_json(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def build_inventory_payload(inventory, run_payload):
    pages = require_field(inventory, 'pages', 'docs-inventory')
    rows = [[require_field(page, column, 'docs-inventory.pages') for column in INVENTORY_EVENT_COLUMNS] for page in pages]
    connectors = require_field(inventory, 'connectors', 'docs-inventory')
    properties = {
        '$process_person_profile': False,
        'run_uuid': run_payload['uuid'],
        'run_date': run_payload['properties']['run_date'],
        'site_commit': require_field(inventory, 'site_commit', 'docs-inventory'),
        'monorepo_commit': require_field(inventory, 'monorepo_commit', 'docs-inventory'),
        'page_columns': compact_json(INVENTORY_EVENT_COLUMNS),
        'pages': compact_json(rows),
        'connectors_docs_url_elsewhere_list': compact_json(require_field(connectors, 'docs_url_elsewhere', 'docs-inventory.connectors')),
        'duplicate_title_groups_list': compact_json(require_field(inventory, 'duplicate_title_groups', 'docs-inventory')),
        'sitemap_template_routes_list': compact_json(require_field(inventory, 'sitemap_template_routes', 'docs-inventory')),
    }
    payload = {
        'api_key': API_KEY, 'event': 'docs_audit_page_inventory', 'distinct_id': run_payload['distinct_id'], 'properties': properties,
        'timestamp': run_payload['timestamp'], 'uuid': str(uuid.uuid5(INVENTORY_EVENT_UUID_NAMESPACE, run_payload['uuid'])),
    }
    payload_bytes = len(json.dumps(payload, allow_nan=False).encode('utf-8'))
    if payload_bytes > MAX_INVENTORY_EVENT_BYTES:
        raise ValueError(f'docs_audit_page_inventory payload is {payload_bytes} bytes, over the {MAX_INVENTORY_EVENT_BYTES}-byte limit')
    return payload


def send_payload(payload):
    endpoint = os.environ.get('DOCAUDIT_CAPTURE_URL', CAPTURE_URL)
    encoded_payload = json.dumps(payload, allow_nan=False).encode('utf-8')
    for attempt in range(4):
        request = urllib.request.Request(endpoint, data=encoded_payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
            return
        except urllib.error.HTTPError as failure:
            failure.close()
            if failure.code < 500 or failure.code >= 600 or attempt == 3:
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)


def require_inventory_commits_match_run(inventory, run_properties):
    for commit_name in ('site_commit', 'monorepo_commit'):
        inventory_commit = require_field(inventory, commit_name, 'docs-inventory')
        if inventory_commit != run_properties[commit_name]:
            raise ValueError(f'docs-inventory.{commit_name} {inventory_commit} differs from the run checkout {commit_name} {run_properties[commit_name]}')


def build_success_payloads(duration_seconds):
    from doccoverage import OUTPUT_ROOT
    from docs_inventory import INVENTORY_FILE_NAME
    report = json.loads((OUTPUT_ROOT / 'next-steps.json').read_text(encoding='utf-8'))
    run_payload = build_payload(report, duration_seconds, read_commits())
    inventory = json.loads((OUTPUT_ROOT / INVENTORY_FILE_NAME).read_text(encoding='utf-8'))
    require_inventory_commits_match_run(inventory, run_payload['properties'])
    run_payload['properties'].update(inventory_run_properties(inventory))
    return run_payload, build_inventory_payload(inventory, run_payload)


def is_rejection(failure):
    return isinstance(failure, urllib.error.HTTPError) and 400 <= failure.code < 500


def spool_payload(payload):
    spool_path = SPOOL_DIRECTORY / f'{payload["uuid"]}.json'
    partial_path = spool_path.with_suffix('.partial')
    try:
        SPOOL_DIRECTORY.mkdir(parents=True, exist_ok=True)
        partial_path.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
        os.replace(partial_path, spool_path)
    except OSError as failure:
        raise SpoolWriteError(f'spool write failed: {payload["uuid"]} {payload["event"]} lost: {type(failure).__name__}: {failure}') from failure
    return spool_path


def reject_spooled_payload(spool_path, reason):
    rejected_directory = SPOOL_DIRECTORY / 'rejected'
    rejected_directory.mkdir(parents=True, exist_ok=True)
    os.replace(spool_path, rejected_directory / spool_path.name)
    print(f'Rejected spooled event {spool_path.name}: {reason}', file=sys.stderr)


def spooled_payloads_oldest_first():
    spooled = []
    for spool_path in sorted(SPOOL_DIRECTORY.glob('*.json')):
        try:
            payload = json.loads(spool_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as failure:
            reject_spooled_payload(spool_path, f'unreadable: {type(failure).__name__}: {failure}')
            continue
        spooled.append((spool_path.stat().st_mtime_ns, spool_path.name, spool_path, payload))
    return [(spool_path, payload) for _, _, spool_path, payload in sorted(spooled, key=lambda entry: entry[:2])]


def resend_spooled_payloads():
    for spool_path, payload in spooled_payloads_oldest_first():
        try:
            send_payload(payload)
        except TRANSPORT_FAILURES as failure:
            if is_rejection(failure):
                reject_spooled_payload(spool_path, f'{type(failure).__name__}: {failure}')
                continue
            print(f'Spooled event {spool_path.name} still unsent: {type(failure).__name__}: {failure}', file=sys.stderr)
            continue
        spool_path.unlink()


def deliver_payload(payload, dry_run):
    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))
        return
    try:
        send_payload(payload)
    except TRANSPORT_FAILURES as failure:
        if not is_rejection(failure):
            print(f'Spooled unsent event to {spool_payload(payload)}', file=sys.stderr)
        raise


def delivery_outcome(payload, dry_run):
    try:
        deliver_payload(payload, dry_run)
    except TRANSPORT_FAILURES as failure:
        print(f'Could not deliver {payload["event"]} {payload["uuid"]}: {type(failure).__name__}: {failure}', file=sys.stderr)
        return 'rejected' if is_rejection(failure) else 'spooled'
    return 'delivered'


def deliver_failed_payload_and_exit(arguments, failure_reason):
    deliver_payload(build_payload(None, arguments.duration_seconds, read_commit_properties_for_failed_event(), failure_reason), arguments.dry_run)
    raise SystemExit(f'Sent failed event instead of success: {failure_reason}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--duration-seconds', required=True, type=float)
    parser.add_argument('--failed', action='store_true')
    parser.add_argument('--reason')
    parser.add_argument('--dry-run', action='store_true')
    arguments = parser.parse_args()
    if arguments.failed and not arguments.reason:
        parser.error('--failed requires --reason')
    if arguments.reason and not arguments.failed:
        parser.error('--reason requires --failed')
    if not arguments.dry_run:
        resend_spooled_payloads()
    if arguments.failed:
        deliver_payload(build_payload(None, arguments.duration_seconds, read_commit_properties_for_failed_event(), arguments.reason), arguments.dry_run)
        return
    try:
        run_payload, inventory_payload = build_success_payloads(arguments.duration_seconds)
    except Exception as failure:
        deliver_failed_payload_and_exit(arguments, f'report metrics: {type(failure).__name__}: {failure}')
    outcomes = [(payload, delivery_outcome(payload, arguments.dry_run)) for payload in (inventory_payload, run_payload)]
    spooled_events = [f'{payload["event"]} {payload["uuid"]}' for payload, outcome in outcomes if outcome == 'spooled']
    rejected_events = [f'{payload["event"]} {payload["uuid"]}' for payload, outcome in outcomes if outcome == 'rejected']
    if spooled_events or rejected_events:
        raise SystemExit(f'Undelivered events: spooled for the next run: {spooled_events}; rejected by the capture endpoint, not resent: {rejected_events}')


def run_reporting():
    try:
        main()
    except SpoolWriteError as failure:
        print(failure, file=sys.stderr)
        raise SystemExit(SPOOL_WRITE_FAILED_EXIT_CODE) from failure


if __name__ == '__main__':
    run_reporting()
