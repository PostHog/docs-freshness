import copy
import datetime
import http.server
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import docs_inventory
import report_metrics
from attribution import CATEGORIES
from next_steps import COVERAGE_SOURCES


def make_report():
    finding = {'page': 'guide.md', 'kind': 'docs-link', 'detail': '/missing', 'line': 12}
    return {
        'run_date': '2026-10-06', 'total_findings': 2,
        'totals_by_kind': {'docs-link': 1, 'ui-label': 1},
        'totals_by_confidence': {'exact': 1, 'candidate': 0, 'none': 1},
        'delta': {'new': 1, 'resolved': 3, 'still_open': 1},
        'attribution_totals': dict(zip(CATEGORIES, (1, 2, 3, 4, 5))),
        'coverage_gaps': {source: {'undocumented': [{'name': 'missing'}] * index, 'partially_documented': []}
                          for index, source in enumerate(COVERAGE_SOURCES)},
        'upstream_guards': [{'name': 'posthog.com PR check', 'caught_at_pr_time': 4, 'total': 7}],
        'attributed_findings': [finding, {**finding, 'kind': 'ui-label'}],
        'new_since_last_run': {'guide.md': [finding]},
    }


def make_inventory(page_count=2, url_prefix='/docs/guide'):
    page = {
        'url': url_prefix, 'source_repo': 'posthog.com', 'path': 'contents/docs/guide.md', 'title': 'Guide', 'page_class': 'live-route', 'doc_kind': 'mixed prose',
        'is_route': True, 'redirected_away': False, 'created_at': '2024-01-01', 'agent_line_share': 0.3, 'nonblank_lines': 10,
        'template_family_size': 1, 'site_inbound_links': 2, 'in_nav': True, 'redirects_into': 3, 'app_links': 1,
    }
    return {
        'format_version': 1, 'site_commit': 'a' * 40, 'monorepo_commit': 'b' * 40, 'page_columns': list(page),
        'pages': [{**page, 'url': f'{url_prefix}-{index}'} for index in range(page_count)],
        'counts': {
            'pages_total': page_count,
            'by_doc_kind': {
                'templated spec': 0, 'agent-written prose': 0, 'mixed prose': page_count, 'human-written prose': 0, 'empty page': 0,
                'generated page': 0, 'not a page': 0,
            },
            'by_page_class': {
                'live-route': page_count, 'redirected-away': 0, 'not-a-route': 0, 'contested-route': 0, 'generated-route': 0, 'sitemap-template-route': 0,
            },
            'redirected_away': 0, 'template_families': 0,
        },
        'connectors': {
            'source_files': 2, 'by_bucket': {'posthog-docs': 1, 'docs-url-elsewhere': 1, 'docs-url-none': 0, 'no-docs-url': 0},
            'docs_url_elsewhere': [{'connector': 'granola', 'docs_url': 'https://docs.granola.ai/introduction'}], 'docs_url_none': [], 'no_docs_url': [],
        },
        'duplicate_title_groups': [{'title': 'guide', 'urls': [f'{url_prefix}-0', f'{url_prefix}-1']}],
        'sitemap_template_routes': ['/docs/product-analytics/learn/[...chapter]'],
    }


def verify_inventory_run_properties():
    properties = report_metrics.inventory_run_properties(make_inventory())
    assert properties == {
        'docs_pages_total': 2, 'docs_pages_templated_spec': 0, 'docs_pages_agent_written_prose': 0, 'docs_pages_mixed_prose': 2,
        'docs_pages_human_written_prose': 0, 'docs_pages_empty_page': 0, 'docs_pages_generated_page': 0,
        'docs_pages_not_a_page': 0, 'docs_pages_live_route': 2, 'docs_pages_redirected_away': 0, 'docs_pages_not_a_route': 0,
        'docs_pages_contested_route': 0, 'docs_pages_generated_route': 0, 'docs_pages_sitemap_template_route': 0,
        'docs_pages_with_redirect_source': 0, 'docs_sitemap_template_routes': 1, 'docs_template_families': 0, 'connectors_docs_url_elsewhere': 1, 'docs_duplicate_title_groups': 1,
    }, properties
    inventory_with_unknown_kind = make_inventory()
    inventory_with_unknown_kind['counts']['by_doc_kind']['poetry'] = 1
    try:
        report_metrics.inventory_run_properties(inventory_with_unknown_kind)
        raise AssertionError('Unknown doc kind accepted')
    except ValueError as failure:
        assert 'docs-inventory.counts.by_doc_kind' in str(failure) and 'poetry' in str(failure), failure
    inventory_missing_connectors = make_inventory()
    del inventory_missing_connectors['connectors']
    try:
        report_metrics.inventory_run_properties(inventory_missing_connectors)
        raise AssertionError('Missing connectors accepted')
    except ValueError as failure:
        assert 'docs-inventory.connectors' in str(failure), failure


def verify_page_class_counts_sum_to_pages_total_while_redirect_source_flag_counts_separately():
    inventory = make_inventory(page_count=3)
    inventory['counts']['by_page_class'] = {
        'live-route': 1, 'redirected-away': 1, 'not-a-route': 1, 'contested-route': 0, 'generated-route': 0, 'sitemap-template-route': 0,
    }
    inventory['counts']['redirected_away'] = 2
    properties = report_metrics.inventory_run_properties(inventory)
    page_class_total = sum(properties[f'docs_pages_{report_metrics.slug_name(name)}'] for name in docs_inventory.PAGE_CLASSES)
    assert page_class_total == properties['docs_pages_total'] == 3, properties
    assert properties['docs_pages_redirected_away'] == 1 and properties['docs_pages_with_redirect_source'] == 2, properties
    inventory['counts']['by_page_class']['redirected-away'] = 2
    try:
        report_metrics.inventory_run_properties(inventory)
        raise AssertionError('Page class counts that miss pages_total accepted')
    except ValueError as failure:
        assert 'sums to 4, not pages_total 3' in str(failure), failure


def verify_inventory_payload_links_to_run_event(report, commits):
    run_payload = report_metrics.build_payload(report, 1, commits)
    inventory_payload = report_metrics.build_inventory_payload(make_inventory(), run_payload)
    assert set(inventory_payload) == {'api_key', 'event', 'distinct_id', 'properties', 'timestamp', 'uuid'}
    assert inventory_payload['event'] == 'docs_audit_page_inventory'
    assert inventory_payload['distinct_id'] == run_payload['distinct_id']
    assert inventory_payload['uuid'] != run_payload['uuid']
    assert inventory_payload['uuid'] == report_metrics.build_inventory_payload(make_inventory(), run_payload)['uuid']
    properties = inventory_payload['properties']
    assert properties['run_uuid'] == run_payload['uuid']
    assert properties['run_date'] == '2026-10-06'
    assert properties['$process_person_profile'] is False
    assert (properties['site_commit'], properties['monorepo_commit']) == ('a' * 40, 'b' * 40)
    columns = json.loads(properties['page_columns'])
    assert columns == list(report_metrics.INVENTORY_EVENT_COLUMNS)
    rows = json.loads(properties['pages'])
    assert dict(zip(columns, rows[0]))['url'] == '/docs/guide-0' and dict(zip(columns, rows[1]))['url'] == '/docs/guide-1'
    assert ', ' not in properties['pages'] and ', ' not in properties['page_columns']
    assert json.loads(properties['connectors_docs_url_elsewhere_list']) == [{'connector': 'granola', 'docs_url': 'https://docs.granola.ai/introduction'}]
    assert json.loads(properties['duplicate_title_groups_list']) == [{'title': 'guide', 'urls': ['/docs/guide-0', '/docs/guide-1']}]
    assert json.loads(properties['sitemap_template_routes_list']) == ['/docs/product-analytics/learn/[...chapter]']
    inventory_missing_page_field = make_inventory()
    del inventory_missing_page_field['pages'][1]['app_links']
    try:
        report_metrics.build_inventory_payload(inventory_missing_page_field, run_payload)
        raise AssertionError('Page without app_links accepted')
    except ValueError as failure:
        assert 'docs-inventory.pages.app_links' in str(failure), failure


def verify_inventory_payload_for_about_todays_four_thousand_rows_fits_and_twice_that_is_rejected(report, commits):
    run_payload = report_metrics.build_payload(report, 1, commits)
    long_url_prefix = '/docs/data-warehouse/sources/some-long-connector-name/configuration-reference'
    inventory_payload = report_metrics.build_inventory_payload(make_inventory(page_count=4000, url_prefix=long_url_prefix), run_payload)
    assert len(json.dumps(inventory_payload).encode('utf-8')) < 750_000
    try:
        report_metrics.build_inventory_payload(make_inventory(page_count=8000, url_prefix=long_url_prefix), run_payload)
        raise AssertionError('Oversized inventory payload accepted')
    except ValueError as failure:
        assert 'over the 900000-byte limit' in str(failure), failure


def verify_payload(report, commits):
    payload = report_metrics.build_payload(report, 12.5, commits)
    assert set(payload) == {'api_key', 'event', 'distinct_id', 'properties', 'timestamp', 'uuid'}
    assert str(uuid.UUID(payload['uuid'])) == payload['uuid']
    assert payload['api_key'] == 'sTMFPsFhdP1Ssg'
    assert payload['event'] == 'docs_audit_run_completed'
    assert payload['distinct_id'] == 'docs-audit:john.w'
    expected_properties = {
        '$process_person_profile': False, 'status': 'success', 'duration_seconds': 12.5,
        'run_date': '2026-10-06', **commits, 'findings_total': 2,
        'findings_app_url': 0, 'findings_docs_fragment': 0, 'findings_docs_link': 1,
        'findings_sdk_method': 0, 'findings_settings_anchor': 0, 'findings_ui_label': 1, 'findings_ui_label_minor': 0,
        'has_previous_run': True, 'findings_new': 1, 'findings_resolved': 3, 'findings_still_open': 1,
        'suggestions_exact': 1, 'suggestions_candidate': 0, 'suggestions_none': 1,
        'category_introduced_upstream': 1, 'category_introduced_by_docs_change': 2,
        'category_wrong_when_written': 3, 'category_shipped_undocumented': 4, 'category_docs_removed': 5,
        'coverage_undocumented_js_config': 0, 'coverage_undocumented_settings': 1,
        'coverage_undocumented_settings_sections': 2, 'coverage_undocumented_js_changelog': 3,
        'coverage_undocumented_python_changelog': 4, 'coverage_undocumented_js_methods': 5,
        'coverage_undocumented_python_config': 6, 'coverage_undocumented_python_methods': 7,
        'guard_posthog_com_pr_check_caught': 4, 'guard_posthog_com_pr_check_total': 7,
        'findings_by_page': '{"guide.md": 2}',
        'new_findings': '[{"detail": "/missing", "kind": "docs-link", "line": 12, "page": "guide.md"}]',
        'new_findings_included': 1,
    }
    assert payload['properties'] == expected_properties, payload
    failure_payload = report_metrics.build_payload(None, 4, {'site_commit': 'a' * 40}, 'audit: broken')
    assert failure_payload['properties'] == {
        '$process_person_profile': False, 'status': 'failed', 'duration_seconds': 4,
        'site_commit': 'a' * 40, 'failure_reason': 'audit: broken', 'run_date': datetime.date.today().isoformat(),
    }
    for field_path in (('delta',), ('new_since_last_run',), ('coverage_gaps', 'settings'), ('totals_by_confidence', 'exact'), ('attribution_totals', 'docs_removed')):
        incomplete_report = copy.deepcopy(report)
        container = incomplete_report
        for name in field_path[:-1]:
            container = container[name]
        del container[field_path[-1]]
        try:
            report_metrics.build_payload(incomplete_report, 1, commits)
            raise AssertionError(f'Missing {field_path} accepted')
        except ValueError as failure:
            assert '.'.join(field_path) in str(failure), failure
    many_new = copy.deepcopy(report)
    many_new['new_since_last_run']['guide.md'] *= 60
    truncated_properties = report_metrics.build_payload(many_new, 1, commits)['properties']
    assert len(json.loads(truncated_properties['new_findings'])) == 50
    assert truncated_properties['new_findings_included'] == 50
    for totals_name, unknown_key in (('totals_by_kind', 'brand-new-kind'), ('attribution_totals', 'brand_new_category')):
        report_with_unknown_key = copy.deepcopy(report)
        report_with_unknown_key[totals_name][unknown_key] = 1
        try:
            report_metrics.build_payload(report_with_unknown_key, 1, commits)
            raise AssertionError(f'Unknown {totals_name} key accepted')
        except ValueError as failure:
            assert totals_name in str(failure) and unknown_key in str(failure), failure
    return payload


def verify_baseline_run_payload_omits_previous_run_comparison(report, commits):
    baseline_report = copy.deepcopy(report)
    del baseline_report['delta']
    del baseline_report['new_since_last_run']
    baseline_properties = report_metrics.build_payload(baseline_report, 1, commits)['properties']
    assert baseline_properties['has_previous_run'] is False
    comparison_properties = {'findings_new', 'findings_resolved', 'findings_still_open', 'new_findings', 'new_findings_included'}
    assert comparison_properties.isdisjoint(baseline_properties), baseline_properties
    assert baseline_properties['findings_total'] == 2


class CaptureServer:
    def __init__(self, failures_before_success=0, rejected_event=None):
        self.requests = []
        self.failures_remaining = failures_before_success
        capture_server = self

        class CaptureHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                capture_server.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if capture_server.requests[-1]['event'] == rejected_event:
                    self.send_response(400)
                    self.end_headers()
                    return
                if capture_server.failures_remaining > 0:
                    capture_server.failures_remaining -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *arguments):
                return

        self.server = http.server.HTTPServer(('127.0.0.1', 0), CaptureHandler)
        self.url = f'http://127.0.0.1:{self.server.server_port}/capture'

    def __enter__(self):
        self.serving_thread = threading.Thread(target=self.server.serve_forever)
        self.serving_thread.start()
        return self

    def __exit__(self, *exception_details):
        self.server.shutdown()
        self.serving_thread.join()
        self.server.server_close()


def run_report_metrics_against_capture_server(tool_directory, output_directory, *arguments, rejected_event=None):
    with CaptureServer(rejected_event=rejected_event) as capture_server:
        environment = {
            **os.environ, 'DOCAUDIT_OUTPUT_ROOT': str(output_directory), 'DOCAUDIT_CAPTURE_URL': capture_server.url,
            'DOCAUDIT_SPOOL_ROOT': str(Path(output_directory) / 'unsent-events'),
        }
        completed = subprocess.run(['python3', 'report_metrics.py', *arguments], cwd=tool_directory,
                                   env=environment, capture_output=True, text=True)
    return completed, capture_server.requests


def verify_malformed_report_sends_failed_event(root, report, report_name, mutate_report, expected_reason):
    output_directory = root / f'{report_name}-output'
    output_directory.mkdir()
    malformed_report = copy.deepcopy(report)
    mutate_report(malformed_report)
    (output_directory / 'next-steps.json').write_text(json.dumps(malformed_report))
    completed, captured_requests = run_report_metrics_against_capture_server(report_metrics.TOOL_ROOT, output_directory, '--duration-seconds', '3')
    assert completed.returncode == 1, completed
    assert len(captured_requests) == 1, captured_requests
    properties = captured_requests[0]['properties']
    assert properties['status'] == 'failed', properties
    assert properties['failure_reason'] == expected_reason, properties


def make_inventory_at_checkout_commits():
    checkout_commits = report_metrics.read_commits()
    return {**make_inventory(), 'site_commit': checkout_commits['site_commit'], 'monorepo_commit': checkout_commits['monorepo_commit']}


def verify_inventory_from_other_commits_sends_failed_event_naming_both_shas(root, report):
    output_directory = write_success_outputs(root / 'other-commits-output', report)
    (output_directory / 'docs-inventory.json').write_text(json.dumps(make_inventory()))
    completed, captured_requests = run_report_metrics_against_capture_server(report_metrics.TOOL_ROOT, output_directory, '--duration-seconds', '3')
    assert completed.returncode == 1, completed
    assert [request['properties']['status'] for request in captured_requests] == ['failed'], captured_requests
    failure_reason = captured_requests[0]['properties']['failure_reason']
    assert 'a' * 40 in failure_reason and report_metrics.read_commits()['site_commit'] in failure_reason, failure_reason


def verify_unwritable_spool_reports_lost_event_and_exits_with_distinct_code(root):
    blocking_file = root / 'spool-parent-is-a-file'
    blocking_file.write_text('not a directory')
    with CaptureServer(failures_before_success=4) as capture_server:
        environment = {
            **os.environ, 'DOCAUDIT_OUTPUT_ROOT': str(root), 'DOCAUDIT_CAPTURE_URL': capture_server.url,
            'DOCAUDIT_SPOOL_ROOT': str(blocking_file / 'unsent-events'),
        }
        completed = subprocess.run(['python3', 'report_metrics.py', '--failed', '--reason', 'guard: disk', '--duration-seconds', '1'],
                                   cwd=report_metrics.TOOL_ROOT, env=environment, capture_output=True, text=True)
        lost_uuid = capture_server.requests[0]['uuid']
    assert completed.returncode == report_metrics.SPOOL_WRITE_FAILED_EXIT_CODE, completed
    assert f'spool write failed: {lost_uuid} docs_audit_run_completed lost: ' in completed.stderr, completed.stderr
    assert 'Spooled unsent event' not in completed.stderr, completed.stderr


def write_success_outputs(output_directory, report):
    output_directory.mkdir()
    (output_directory / 'next-steps.json').write_text(json.dumps(report))
    (output_directory / 'docs-inventory.json').write_text(json.dumps(make_inventory_at_checkout_commits()))
    return output_directory


def verify_success_sends_inventory_event_before_run_event(root, report):
    output_directory = write_success_outputs(root / 'success-output', report)
    completed, captured_requests = run_report_metrics_against_capture_server(report_metrics.TOOL_ROOT, output_directory, '--duration-seconds', '3')
    assert completed.returncode == 0, completed
    assert [request['event'] for request in captured_requests] == ['docs_audit_page_inventory', 'docs_audit_run_completed'], captured_requests
    inventory_request, run_request = captured_requests
    assert inventory_request['properties']['run_uuid'] == run_request['uuid']
    assert run_request['properties']['status'] == 'success'
    assert run_request['properties']['docs_pages_total'] == 2 and run_request['properties']['connectors_docs_url_elsewhere'] == 1


def verify_rejected_inventory_event_still_sends_success_run_event_and_exits_non_zero(root, report):
    output_directory = write_success_outputs(root / 'rejected-inventory-output', report)
    completed, captured_requests = run_report_metrics_against_capture_server(
        report_metrics.TOOL_ROOT, output_directory, '--duration-seconds', '3', rejected_event='docs_audit_page_inventory',
    )
    assert completed.returncode == 1 and 'rejected by the capture endpoint, not resent: [\'docs_audit_page_inventory' in completed.stderr, completed
    assert [request['event'] for request in captured_requests] == ['docs_audit_page_inventory', 'docs_audit_run_completed'], captured_requests
    assert captured_requests[1]['properties']['status'] == 'success', captured_requests[1]['properties']
    assert not list((output_directory / 'unsent-events').glob('*.json'))


def verify_missing_inventory_sends_failed_event(root, report):
    output_directory = root / 'missing-inventory-output'
    output_directory.mkdir()
    (output_directory / 'next-steps.json').write_text(json.dumps(report))
    completed, captured_requests = run_report_metrics_against_capture_server(report_metrics.TOOL_ROOT, output_directory, '--duration-seconds', '3')
    assert completed.returncode == 1, completed
    assert len(captured_requests) == 1, captured_requests
    properties = captured_requests[0]['properties']
    assert properties['status'] == 'failed', properties
    assert properties['failure_reason'].startswith('report metrics: FileNotFoundError: ') and 'docs-inventory.json' in properties['failure_reason'], properties


def read_dry_run_payloads(stdout):
    decoder = json.JSONDecoder()
    payloads = []
    position = 0
    while stdout[position:].strip():
        position += len(stdout[position:]) - len(stdout[position:].lstrip())
        payload, position = decoder.raw_decode(stdout, position)
        payloads.append(payload)
    return payloads


def verify_broken_tool_module_still_sends_failed_events(root):
    tool_copy = root / 'broken-tool-copy'
    tool_copy.mkdir()
    (tool_copy / 'report_metrics.py').write_text((report_metrics.TOOL_ROOT / 'report_metrics.py').read_text())
    (tool_copy / 'doccoverage.py').write_text('def broken(:\n')
    completed, captured_requests = run_report_metrics_against_capture_server(tool_copy, root, '--failed', '--reason', 'audit: broken', '--duration-seconds', '2')
    assert completed.returncode == 0, completed
    assert len(captured_requests) == 1, captured_requests
    properties = captured_requests[0]['properties']
    assert properties['status'] == 'failed' and properties['failure_reason'] == 'audit: broken', properties
    assert properties['commits_unavailable_reason'].startswith('SyntaxError: '), properties
    assert not any(name.endswith('_commit') for name in properties), properties
    completed, captured_requests = run_report_metrics_against_capture_server(tool_copy, root, '--duration-seconds', '2')
    assert completed.returncode == 1, completed
    assert len(captured_requests) == 1, captured_requests
    properties = captured_requests[0]['properties']
    assert properties['status'] == 'failed', properties
    assert properties['failure_reason'].startswith('report metrics: SyntaxError: '), properties


def verify_transport(payload):
    with CaptureServer(failures_before_success=2) as capture_server:
        with patch.dict(os.environ, {'DOCAUDIT_CAPTURE_URL': capture_server.url}):
            with patch.object(report_metrics.time, 'sleep') as sleep:
                report_metrics.send_payload(payload)
                assert [call.args[0] for call in sleep.call_args_list] == [1, 2]
            assert capture_server.requests == [payload] * 3
            assert {request['uuid'] for request in capture_server.requests} == {payload['uuid']}
            capture_server.failures_remaining = 4
            with patch.object(report_metrics.time, 'sleep'):
                try:
                    report_metrics.send_payload(payload)
                    raise AssertionError('Permanent server failure accepted')
                except report_metrics.urllib.error.HTTPError as failure:
                    assert failure.code == 503
            assert len(capture_server.requests) == 7


def run_failed_report_in_process(capture_server, spool_directory, reason):
    arguments = ['report_metrics.py', '--failed', '--reason', reason, '--duration-seconds', '1']
    with patch.dict(os.environ, {'DOCAUDIT_CAPTURE_URL': capture_server.url}), patch.object(report_metrics, 'SPOOL_DIRECTORY', spool_directory), \
            patch.object(report_metrics.time, 'sleep'), patch.object(report_metrics.sys, 'argv', arguments):
        try:
            report_metrics.main()
        except report_metrics.TRANSPORT_FAILURES as failure:
            return failure
    return None


def spooled_names(spool_directory):
    return sorted(path.name for path in spool_directory.glob('*.json'))


def verify_unsendable_event_spools_once_and_next_invocation_delivers_it_with_original_uuid_and_timestamp(root):
    spool_directory = root / 'spool-delivered'
    with CaptureServer(failures_before_success=4) as capture_server:
        failure = run_failed_report_in_process(capture_server, spool_directory, 'guard: dns down')
        assert isinstance(failure, report_metrics.urllib.error.HTTPError) and failure.code == 503, failure
        unsent = capture_server.requests[0]
        assert [request['uuid'] for request in capture_server.requests] == [unsent['uuid']] * 4, capture_server.requests
        assert spooled_names(spool_directory) == [f'{unsent["uuid"]}.json'], spooled_names(spool_directory)
        assert run_failed_report_in_process(capture_server, spool_directory, 'guard: next day') is None
        resent, current = capture_server.requests[4:]
    assert (resent['uuid'], resent['timestamp']) == (unsent['uuid'], unsent['timestamp']), resent
    assert resent['properties']['failure_reason'] == 'guard: dns down' and current['properties']['failure_reason'] == 'guard: next day'
    assert spooled_names(spool_directory) == []


def verify_spooled_event_rejected_with_4xx_moves_to_rejected_and_is_never_resent(root):
    spool_directory = root / 'spool-rejected'
    obsolete_payload = {'api_key': 'k', 'event': 'docs_audit_obsolete_event', 'distinct_id': 'd', 'properties': {}, 'timestamp': '2026-10-01T00:00:00+00:00', 'uuid': str(uuid.uuid4())}
    with patch.object(report_metrics, 'SPOOL_DIRECTORY', spool_directory):
        report_metrics.spool_payload(obsolete_payload)
    with CaptureServer(rejected_event='docs_audit_obsolete_event') as capture_server:
        assert run_failed_report_in_process(capture_server, spool_directory, 'guard: one') is None
        assert run_failed_report_in_process(capture_server, spool_directory, 'guard: two') is None
        sent_events = [request['event'] for request in capture_server.requests]
    assert sent_events == ['docs_audit_obsolete_event', 'docs_audit_run_completed', 'docs_audit_run_completed'], sent_events
    assert spooled_names(spool_directory) == []
    assert sorted(path.name for path in (spool_directory / 'rejected').iterdir()) == [f'{obsolete_payload["uuid"]}.json']


def verify_spooled_event_that_fails_again_stays_while_current_event_still_sends(root):
    spool_directory = root / 'spool-still-failing'
    stale_payload = {'api_key': 'k', 'event': 'docs_audit_run_completed', 'distinct_id': 'd', 'properties': {}, 'timestamp': '2026-10-01T00:00:00+00:00', 'uuid': str(uuid.uuid4())}
    with patch.object(report_metrics, 'SPOOL_DIRECTORY', spool_directory):
        report_metrics.spool_payload(stale_payload)
    with CaptureServer(failures_before_success=4) as capture_server:
        assert run_failed_report_in_process(capture_server, spool_directory, 'guard: current') is None
        requests = capture_server.requests
    assert [request['uuid'] for request in requests[:4]] == [stale_payload['uuid']] * 4, requests
    assert len(requests) == 5 and requests[4]['properties']['failure_reason'] == 'guard: current', requests
    assert spooled_names(spool_directory) == [f'{stale_payload["uuid"]}.json']


def run_success_report_in_process(capture_server, spool_directory, output_directory):
    arguments = ['report_metrics.py', '--duration-seconds', '1']
    with patch.dict(os.environ, {'DOCAUDIT_CAPTURE_URL': capture_server.url}), patch.object(report_metrics, 'SPOOL_DIRECTORY', spool_directory), \
            patch.object(report_metrics.time, 'sleep'), patch.object(report_metrics.sys, 'argv', arguments), \
            patch.object(docs_inventory.doccoverage, 'OUTPUT_ROOT', output_directory):
        try:
            report_metrics.main()
        except SystemExit as exit_request:
            return exit_request
    return None


def verify_network_down_for_both_sends_spools_both_payloads_without_a_failed_event_and_next_run_delivers_them_in_order(root, report):
    output_directory = write_success_outputs(root / 'network-down-output', report)
    spool_directory = root / 'spool-network-down'
    with CaptureServer(failures_before_success=8) as capture_server:
        exit_request = run_success_report_in_process(capture_server, spool_directory, output_directory)
        undelivered = [capture_server.requests[0], capture_server.requests[4]]
        assert exit_request is not None and exit_request.code != 0, exit_request
        assert all(request['uuid'] in str(exit_request.code).split('; rejected')[0] for request in undelivered), exit_request.code
        assert str(exit_request.code).endswith('rejected by the capture endpoint, not resent: []'), exit_request.code
        assert [request['event'] for request in capture_server.requests] == ['docs_audit_page_inventory'] * 4 + ['docs_audit_run_completed'] * 4
        assert {request['properties'].get('status') for request in capture_server.requests} <= {None, 'success'}, capture_server.requests
        assert spooled_names(spool_directory) == sorted(f'{request["uuid"]}.json' for request in undelivered)
        assert run_failed_report_in_process(capture_server, spool_directory, 'guard: next day') is None
        redelivered = capture_server.requests[8:10]
    assert [(request['event'], request['uuid'], request['timestamp']) for request in redelivered] == [
        (request['event'], request['uuid'], request['timestamp']) for request in undelivered
    ], redelivered
    assert spooled_names(spool_directory) == []


def make_refresh_fixture(fixture_directory):
    fixture_directory.mkdir()
    (fixture_directory / 'data').mkdir()
    (fixture_directory / 'refresh.sh').write_text((report_metrics.TOOL_ROOT / 'refresh.sh').read_text())
    (fixture_directory / 'doccoverage.py').write_text(
        "SITE_ROOT = '/fixture/site'\nMONOREPO_ROOT = '/fixture/monorepo'\n"
        "POSTHOG_JS_ROOT = '/fixture/js'\nPOSTHOG_PYTHON_ROOT = '/fixture/python'\nTOOL_ROOT = '/fixture/tool'\n"
    )
    (fixture_directory / 'attribution.py').write_text(
        'import os\n'
        "if os.environ['GUARD_FIXTURE_MODE'] not in ('success', 'report-fails', 'interrupted', 'stalled-pull'):\n"
        "    raise SystemExit('fixture reached prefetch')\n"
    )
    (fixture_directory / 'history_index.py').write_text(
        'import os, pathlib, time\n'
        "if os.environ['GUARD_FIXTURE_MODE'] == 'interrupted':\n"
        "    pathlib.Path('history-index-started').write_text(str(os.getpid()))\n"
        '    time.sleep(60)\n'
    )
    (fixture_directory / 'all_sections.py').write_text('')
    (fixture_directory / 'docs_inventory.py').write_text('')
    (fixture_directory / 'next_steps.py').write_text('')
    (fixture_directory / 'sitecustomize.py').write_text(
        'import io\nimport urllib.request\n'
        "sitemap = '<urlset>' + ''.join(f'<url><loc>https://posthog.com/docs/{index}</loc></url>' for index in range(10000)) + '</urlset>'\n"
        "urllib.request.urlopen = lambda *arguments, **options: io.BytesIO(sitemap.encode())\n"
    )
    (fixture_directory / 'report_metrics.py').write_text(
        'import json, os, sys\n'
        "with open(os.environ['REPORT_CALLS_FILE'], 'a') as calls:\n"
        "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if os.environ['GUARD_FIXTURE_MODE'] == 'report-fails':\n"
        "    raise SystemExit('fixture report failed')\n"
    )
    git_fixture = fixture_directory / 'git'
    git_fixture.write_text(
        '#!/usr/bin/env python3\n'
        'import os, pathlib, sys, time\n'
        'checkout = sys.argv[2]\n'
        'arguments = sys.argv[3:]\n'
        "mode = os.environ['GUARD_FIXTURE_MODE']\n"
        "if arguments[-2:] == ['pull', '--ff-only'] and mode == 'stalled-pull':\n"
        "    pathlib.Path('pull-started').write_text(f\"{os.getpid()} {os.environ.get('GIT_TERMINAL_PROMPT')}\")\n"
        '    time.sleep(60)\n'
        "if arguments == ['status', '--porcelain', '--', '.', ':(exclude)data'] and checkout == '/fixture/tool':\n"
        "    print({True: ' M refresh.sh', False: ''}[mode == 'dirty-tool'])\n"
        '    raise SystemExit(0)\n'
        "if arguments == ['status', '--porcelain']:\n"
        "    print({True: ' M dirty', False: ''}[mode == 'dirty' and checkout == '/fixture/js'])\n"
        '    raise SystemExit(0)\n'
        "if arguments == ['rev-list', '--count', '@{upstream}..HEAD']:\n"
        "    print({True: '2', False: '0'}[mode == 'ahead' and checkout == '/fixture/monorepo'])\n"
        '    raise SystemExit(0)\n'
        "if arguments == ['symbolic-ref', '--short', 'HEAD']:\n"
        "    if mode == 'detached' and checkout == '/fixture/python':\n"
        '        raise SystemExit(1)\n'
        "    branch = 'master'\n"
        "    if mode == 'feature' and checkout == '/fixture/monorepo':\n"
        "        branch = 'feature'\n"
        "    if mode == 'site-tracking' and checkout == '/fixture/site':\n"
        "        branch = 'docs-master-audit'\n"
        '    print(branch)\n'
        '    raise SystemExit(0)\n'
        "if arguments == ['symbolic-ref', '--short', 'refs/remotes/origin/HEAD'] and mode == 'missing-remote-head' and checkout == '/fixture/python':\n"
        '    raise SystemExit(1)\n'
        "if arguments[-1] == '@{upstream}' and mode == 'foreign-upstream' and checkout == '/fixture/js':\n"
        "    print('fork/master')\n"
        '    raise SystemExit(0)\n'
        "print('origin/master')\n"
    )
    git_fixture.chmod(0o755)
    return fixture_directory


def refresh_environment(fixture_directory, mode):
    return {**os.environ, 'PATH': str(fixture_directory) + os.pathsep + os.environ['PATH'],
            'PYTHONPATH': str(fixture_directory), 'GUARD_FIXTURE_MODE': mode, 'DOCAUDIT_BACKGROUND_PRIORITY': '1',
            'REPORT_CALLS_FILE': str(fixture_directory / 'report-calls.jsonl')}


def read_report_calls(fixture_directory):
    calls_path = fixture_directory / 'report-calls.jsonl'
    if not calls_path.exists():
        return []
    return [json.loads(line) for line in calls_path.read_text().splitlines()]


def run_refresh(fixture_directory, mode, *arguments):
    return subprocess.run(['bash', 'refresh.sh', '--no-pull', *arguments], cwd=fixture_directory,
                          env=refresh_environment(fixture_directory, mode), capture_output=True, text=True)


def assert_failure_reported(report_calls, expected_reason):
    assert len(report_calls) == 1, report_calls
    arguments = report_calls[0]
    assert arguments[:3] == ['--failed', '--reason', expected_reason], arguments
    assert arguments[3] == '--duration-seconds' and arguments[4].isdigit(), arguments


def verify_refresh_checkouts(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-fixture')
    for mode in ('default', 'site-tracking', 'dirty-tool', 'missing-remote-head', 'dirty', 'feature', 'detached', 'ahead', 'foreign-upstream'):
        completed = run_refresh(fixture_directory, mode, '--no-report')
        assert completed.returncode != 0, completed
        assert read_report_calls(fixture_directory) == []
        if mode in ('default', 'site-tracking', 'dirty-tool'):
            assert 'fixture reached prefetch' in completed.stderr, completed.stderr
            continue
        assert 'checkout guard: /fixture/' in completed.stderr, completed.stderr
        assert 'fixture reached prefetch' not in completed.stderr
        if mode == 'dirty':
            assert 'worktree is dirty' in completed.stderr
        if mode == 'feature':
            assert "branch 'feature' is not default 'master'" in completed.stderr
        if mode == 'ahead':
            assert "branch 'master' has 2 commits not on 'origin/master'" in completed.stderr
        if mode == 'missing-remote-head':
            assert 'refs/remotes/origin/HEAD is missing; run: git -C /fixture/python remote set-head origin --auto' in completed.stderr, completed.stderr
        if mode == 'foreign-upstream':
            assert "branch 'master' tracks 'fork/master', not default 'origin/master'" in completed.stderr


def verify_refresh_reports_failed_step(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-failed-step')
    completed = run_refresh(fixture_directory, 'default')
    assert completed.returncode == 1, completed
    assert_failure_reported(read_report_calls(fixture_directory), 'attribution prefetch: command failed (exit 1)')


def verify_refresh_reports_guard_reason(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-guard-reason')
    completed = run_refresh(fixture_directory, 'dirty')
    assert completed.returncode == 1, completed
    assert_failure_reported(read_report_calls(fixture_directory), 'checkout guard: /fixture/js: worktree is dirty')


def verify_refresh_rejects_dirty_tool_code_when_reporting(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-dirty-tool')
    completed = run_refresh(fixture_directory, 'dirty-tool')
    assert completed.returncode == 1, completed
    assert 'fixture reached prefetch' not in completed.stderr, completed.stderr
    report_calls = read_report_calls(fixture_directory)
    assert len(report_calls) == 1, report_calls
    assert report_calls[0][2].startswith('checkout guard: /fixture/tool: tool code has uncommitted changes'), report_calls
    assert report_calls[0][2].endswith('): M refresh.sh'), report_calls


def verify_refresh_reports_success(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-success')
    completed = run_refresh(fixture_directory, 'success')
    assert completed.returncode == 0, completed
    report_calls = read_report_calls(fixture_directory)
    assert len(report_calls) == 1 and report_calls[0][0] == '--duration-seconds' and report_calls[0][1].isdigit(), report_calls
    assert '--failed' not in report_calls[0]


def verify_refresh_leaves_failed_event_to_report_metrics_after_success_report_attempt(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-report-fails')
    completed = run_refresh(fixture_directory, 'report-fails')
    assert completed.returncode == 1, completed
    assert 'not sending a failed event' in completed.stderr, completed.stderr
    report_calls = read_report_calls(fixture_directory)
    assert len(report_calls) == 1 and '--failed' not in report_calls[0], report_calls


def verify_refresh_reports_termination(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-terminated')
    refresh_process = subprocess.Popen(['bash', 'refresh.sh', '--no-pull'], cwd=fixture_directory,
                                       env=refresh_environment(fixture_directory, 'interrupted'),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    started_marker = fixture_directory / 'history-index-started'
    deadline = time.monotonic() + 30
    while not started_marker.exists():
        assert time.monotonic() < deadline and refresh_process.poll() is None, refresh_process.communicate()
        time.sleep(0.05)
    os.killpg(refresh_process.pid, signal.SIGTERM)
    refresh_process.communicate(timeout=30)
    assert refresh_process.returncode == 143, refresh_process.returncode
    assert_failure_reported(read_report_calls(fixture_directory), 'history index: interrupted by SIGTERM')


def is_process_alive(process_id):
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    return True


def terminate_bash_alone_during_step(fixture_directory, mode, started_marker_name, expected_reason, *arguments):
    refresh_process = subprocess.Popen(['bash', 'refresh.sh', *arguments], cwd=fixture_directory,
                                       env=refresh_environment(fixture_directory, mode),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    started_marker = fixture_directory / started_marker_name
    deadline = time.monotonic() + 30
    while not (started_marker.exists() and started_marker.read_text()):
        assert time.monotonic() < deadline and refresh_process.poll() is None, refresh_process.communicate()
        time.sleep(0.05)
    started_marker_fields = started_marker.read_text().split()
    step_process_id = int(started_marker_fields[0])
    try:
        os.kill(refresh_process.pid, signal.SIGTERM)
        refresh_process.communicate(timeout=10)
        assert refresh_process.returncode == 143, refresh_process.returncode
        assert_failure_reported(read_report_calls(fixture_directory), expected_reason)
        step_deadline = time.monotonic() + 5
        while is_process_alive(step_process_id):
            assert time.monotonic() < step_deadline, f'{expected_reason} step {step_process_id} survived bash termination'
            time.sleep(0.05)
        return started_marker_fields
    finally:
        if is_process_alive(step_process_id):
            os.kill(step_process_id, signal.SIGKILL)
        if refresh_process.poll() is None:
            os.killpg(refresh_process.pid, signal.SIGKILL)


def verify_refresh_reports_termination_of_bash_alone_and_stops_step(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-bash-terminated')
    terminate_bash_alone_during_step(fixture_directory, 'interrupted', 'history-index-started', 'history index: interrupted by SIGTERM', '--no-pull')


def verify_refresh_reports_termination_of_bash_alone_during_stalled_pull_without_prompting(root):
    fixture_directory = make_refresh_fixture(root / 'refresh-stalled-pull')
    started_marker_fields = terminate_bash_alone_during_step(fixture_directory, 'stalled-pull', 'pull-started', 'pull /fixture/site: interrupted by SIGTERM')
    assert started_marker_fields[1] == '0', started_marker_fields


def main():
    commits = {f'{name}_commit': str(index) * 40 for index, name in enumerate(report_metrics.read_checkouts(), start=1)}
    with tempfile.TemporaryDirectory(dir=report_metrics.TOOL_ROOT, prefix='.report-test-') as temporary_directory:
        output_directory = Path(temporary_directory)
        fixture_path = output_directory / 'next-steps.json'
        fixture_path.write_text(json.dumps(make_report()))
        (output_directory / 'docs-inventory.json').write_text(json.dumps(make_inventory_at_checkout_commits()))
        report = json.loads(fixture_path.read_text())
        payload = verify_payload(report, commits)
        verify_inventory_run_properties()
        verify_page_class_counts_sum_to_pages_total_while_redirect_source_flag_counts_separately()
        verify_inventory_payload_links_to_run_event(report, commits)
        verify_inventory_payload_for_about_todays_four_thousand_rows_fits_and_twice_that_is_rejected(report, commits)
        verify_success_sends_inventory_event_before_run_event(output_directory, report)
        verify_rejected_inventory_event_still_sends_success_run_event_and_exits_non_zero(output_directory, report)
        verify_inventory_from_other_commits_sends_failed_event_naming_both_shas(output_directory, report)
        verify_unwritable_spool_reports_lost_event_and_exits_with_distinct_code(output_directory)
        verify_missing_inventory_sends_failed_event(output_directory, report)
        verify_baseline_run_payload_omits_previous_run_comparison(report, commits)
        verify_transport(payload)
        verify_unsendable_event_spools_once_and_next_invocation_delivers_it_with_original_uuid_and_timestamp(output_directory)
        verify_spooled_event_rejected_with_4xx_moves_to_rejected_and_is_never_resent(output_directory)
        verify_spooled_event_that_fails_again_stays_while_current_event_still_sends(output_directory)
        verify_network_down_for_both_sends_spools_both_payloads_without_a_failed_event_and_next_run_delivers_them_in_order(output_directory, report)
        verify_malformed_report_sends_failed_event(output_directory, report, 'missing-run-date', lambda malformed_report: malformed_report.pop('run_date'),
                                                   'report metrics: ValueError: Missing required field: next-steps.run_date')
        verify_malformed_report_sends_failed_event(output_directory, report, 'list-totals-by-kind', lambda malformed_report: malformed_report.update(totals_by_kind=[]),
                                                   "report metrics: AttributeError: 'list' object has no attribute 'get'")
        verify_broken_tool_module_still_sends_failed_events(output_directory)
        verify_refresh_checkouts(output_directory)
        verify_refresh_reports_failed_step(output_directory)
        verify_refresh_reports_guard_reason(output_directory)
        verify_refresh_rejects_dirty_tool_code_when_reporting(output_directory)
        verify_refresh_reports_success(output_directory)
        verify_refresh_leaves_failed_event_to_report_metrics_after_success_report_attempt(output_directory)
        verify_refresh_reports_termination(output_directory)
        verify_refresh_reports_termination_of_bash_alone_and_stops_step(output_directory)
        verify_refresh_reports_termination_of_bash_alone_during_stalled_pull_without_prompting(output_directory)
        environment = {**os.environ, 'DOCAUDIT_OUTPUT_ROOT': str(output_directory), 'DOCAUDIT_CAPTURE_URL': 'http://127.0.0.1:1/never'}
        completed = subprocess.run(['python3', 'report_metrics.py', '--dry-run', '--duration-seconds', '1'], cwd=report_metrics.TOOL_ROOT, env=environment, capture_output=True, text=True, check=True)
        dry_inventory_payload, dry_payload = read_dry_run_payloads(completed.stdout)
        assert dry_inventory_payload['event'] == 'docs_audit_page_inventory'
        assert dry_inventory_payload['properties']['run_uuid'] == dry_payload['uuid']
        assert dry_payload['properties']['findings_total'] == 2
        assert dry_payload['properties']['docs_pages_total'] == 2
        assert dry_payload['properties']['duration_seconds'] == 1
        assert all(dry_payload['properties'][f'{name}_commit'] for name in report_metrics.read_checkouts())
        completed = subprocess.run(['python3', 'report_metrics.py', '--failed', '--reason', 'guard: dirty', '--dry-run', '--duration-seconds', '2'], cwd=report_metrics.TOOL_ROOT, env=environment, capture_output=True, text=True, check=True)
        assert json.loads(completed.stdout)['properties']['failure_reason'] == 'guard: dirty'
    print('PASS')


if __name__ == '__main__':
    main()
