import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import attribution
import history_index
from doccoverage import TOOL_ROOT


def initialize_repository(root):
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    return root


def fixture_environment(date):
    return {
        **os.environ, 'GIT_AUTHOR_NAME': 'Attribution fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
        'GIT_COMMITTER_NAME': 'Attribution fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
        'GIT_AUTHOR_DATE': date + 'T12:00:00Z', 'GIT_COMMITTER_DATE': date + 'T12:00:00Z',
    }


def commit_staged(checkout, message, date):
    subprocess.run(['git', '-C', str(checkout), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-q', '-m', message], check=True, env=fixture_environment(date))
    return attribution.git(checkout, 'rev-parse', 'HEAD').strip()


def commit_file(checkout, path, text, date, message=None):
    destination = checkout / path
    if text is None:
        destination.unlink()
    if text is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    subprocess.run(['git', '-C', str(checkout), 'add', path], check=True, env=fixture_environment(date))
    return commit_staged(checkout, message or path, date)


def move_file(checkout, source, destination, date):
    subprocess.run(['git', '-C', str(checkout), 'mv', source, destination], check=True, env=fixture_environment(date))
    return commit_staged(checkout, f'{source} -> {destination}', date)


def finding(page, kind, detail):
    return {'page': page, 'kind': kind, 'detail': detail, 'suggestion': '', 'confidence': 'none'}


def write_report_output(output_directory, findings):
    (output_directory / 'all-findings.txt').write_text(''.join(
        '\t'.join((entry['page'], entry['kind'], entry['detail'], '', 'none', '1')) + '\n' for entry in findings
    ))
    locations = [{
        'page': entry['page'], 'kind': entry['kind'], 'detail': entry['detail'], 'claim': entry['detail'], 'line': 1,
        'source_url': attribution.doccoverage.github_blob_url('site', 'f' * 40, 'contents/docs/' + entry['page'], 1),
        'live_urls': [attribution.doccoverage.text_fragment_url(attribution.doccoverage.docs_page_url(entry['page']), '', entry['detail'])],
    } for entry in findings]
    (output_directory / 'finding-locations.json').write_text(json.dumps({'site_revision': 'f' * 40, 'findings': locations}))


def verify_chronology(root):
    monorepo = initialize_repository(root / 'attribution-monorepo')
    site = initialize_repository(root / 'attribution-site')
    initial_label = 'export const Button = () => <button>SAVE INSIGHT</button>\n'
    commit_file(monorepo, 'frontend/src/button.tsx', initial_label, '2025-10-02')
    source_revision = commit_file(monorepo, 'frontend/src/button.tsx', 'export const Button = () => <button>Store insight</button>\n', '2025-11-10')
    commit_file(monorepo, 'frontend/src/button.tsx', '// Save insight is obsolete\nexport const Button = () => <button>Store insight</button>\n', '2025-11-15')
    commit_file(monorepo, 'frontend/src/button.tsx', 'export const Button = () => <button>Store insight</button>\n', '2025-11-20')
    claim_revision = commit_file(site, 'contents/docs/older.mdx', 'Click **Save insight**.\n', '2025-10-20')
    commit_file(site, 'contents/docs/newer.mdx', 'Click **Save insight**.\n', '2025-12-01')
    commit_file(site, 'contents/docs/target.mdx', '# Existing heading\n', '2025-10-05')
    commit_file(site, 'contents/docs/linked.mdx', '[Target](/docs/target#existing-heading)\n', '2025-10-25')
    target_revision = commit_file(site, 'contents/docs/target.mdx', '# Renamed heading\n', '2025-12-02')
    findings = [
        finding('older.mdx', 'ui-label', 'Save insight'),
        finding('newer.mdx', 'ui-label', 'Save insight'),
        finding('linked.mdx', 'docs-fragment', '/docs/target#existing-heading (/docs/target)'),
    ]
    repositories = {'monorepo': monorepo, 'site': site, 'posthog-js': monorepo, 'posthog-python': monorepo}
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'chronology-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        classified = attribution.attribute_findings(findings, root, build=True)
        from next_steps import build_report, format_markdown

        output_directory = root / 'report-output'
        output_directory.mkdir()
        write_report_output(output_directory, findings)
        (output_directory / 'coverage.json').write_text('{}')
        report = build_report(output_directory)
        assert report['attribution_totals'] == dict(zip(attribution.CATEGORIES, (1, 1, 1, 0, 0))), report['attributed_findings']
        assert [guard['caught_at_pr_time'] for guard in report['upstream_guards']] == [1, 1, 0]
        markdown = format_markdown(report)
        assert f'[{source_revision[:12]}](https://github.com/PostHog/posthog/commit/{source_revision}) 2025-11-10' in markdown, markdown
        assert 'before the change: [frontend/src/button.tsx#L1 @ ' in markdown, markdown
        assert 'Source: [contents/docs/older.mdx#L1](https://github.com/PostHog/posthog.com/blob/' in markdown, markdown
        assert markdown.count('1 of 1 would have been caught at PR time') == 2, markdown
        assert '- wrong when written, ui-label: 1;' in markdown, markdown
        serialized_report = json.loads(json.dumps(report))
        assert all(entry['attribution'].get('source_changed_at') for entry in serialized_report['attributed_findings'])
    assert list(classified.values()) == ['introduced_upstream', 'wrong_when_written', 'introduced_by_docs_change'], classified
    assert findings[0]['attribution']['claim_written_at']['event']['revision'] == claim_revision
    assert findings[0]['attribution']['source_changed_at']['revision'] == source_revision
    assert findings[2]['attribution']['source_changed_at']['revision'] == target_revision
    assert findings[2]['attribution']['source_changed_at']['file'] == 'contents/docs/target.mdx'
    assert attribution.label_line_count('frontend/src/button.test.tsx', ['<button>Save insight</button>'], '<button>Save insight</button>', 'Save insight') == 0
    assert attribution.label_line_count('frontend/src/button.tsx', ["const sentence = 'Rebuilds the prompt'"], "const sentence = 'Rebuilds the prompt'", 'Rebuild') == 0
    assert attribution.label_line_count('frontend/src/button.tsx', ['<p>Its agent log will appear.</p>'], '<p>Its agent log will appear.</p>', 'Agent log') == 0
    assert attribution.label_line_count('products/traces/fixture.json', ['{"message": "Choose a new policy"}'], '{"message": "Choose a new policy"}', 'New policy') == 0
    print('PASS synthetic removal after claim, removal before claim, target heading renamed after link')
    return repositories, findings


def verify_cache_reuse(root, repositories, findings):
    cache_file = root / 'cache-reuse.json'
    missing = finding('newer.mdx', 'app-url', 'never-existed')
    commit_file(repositories['site'], 'contents/docs/newer.mdx', 'Click **Save insight**.\nGo to /never-existed.\n', '2025-12-03')
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', cache_file), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings([findings[0], missing], root, build=True)
        with patch.object(attribution, 'disappearance_search', wraps=attribution.disappearance_search) as history:
            attribution.attribute_findings([findings[0], missing], root, build=True)
            assert history.call_count == 0, history.call_count
            previous_head = attribution.repository_head('monorepo')
            commit_file(repositories['monorepo'], 'products/widget.txt', 'Unrelated change\n', '2025-12-04')
            attribution.attribute_findings([findings[0], missing], root, build=True)
            assert history.call_count == 2, history.call_count
            assert all(call.kwargs['revision_range'] == (f'{previous_head}..HEAD',) for call in history.call_args_list), history.call_args_list
            assert findings[0]['attribution']['category'] == 'introduced_upstream'
            assert missing['attribution']['category'] == 'wrong_when_written'
            attribution.attribute_findings([findings[0], missing], root, build=True)
            assert history.call_count == 2, history.call_count
    cache = json.loads(cache_file.read_text())
    assert all(entry['status'] != 'error' for entry in cache['sources'].values())
    assert all(entry['computed_at_head'] == attribution.git(repositories['monorepo'], 'rev-parse', 'HEAD').strip() for entry in cache['sources'].values())
    print('PASS historical cache revalidates only commits after its recorded HEAD, including never-existing claims')


def verify_cache_revalidation(root, repositories):
    monorepo = repositories['monorepo']
    commit_file(monorepo, 'frontend/src/pin.tsx', 'export const Pin = () => <button>Pin insight</button>\n', '2025-01-01')
    commit_file(monorepo, 'frontend/src/pin.tsx', 'export const Pin = () => <button>Fasten insight</button>\n', '2025-01-10')
    commit_file(repositories['site'], 'contents/docs/pin.mdx', 'Click **Pin insight**.\n', '2025-01-15')
    entry = finding('pin.mdx', 'ui-label', 'Pin insight')
    gap_path = 'packages/browser/src/revalidated.ts'
    commit_file(repositories['posthog-js'], gap_path, 'revalidated_option?: boolean\n', '2025-02-01')
    def revalidated_gaps():
        return {'js-config': {'undocumented': [{'name': 'revalidated_option', 'origin': str(repositories['posthog-js'] / gap_path)}], 'partially_documented': []}}
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'revalidation-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings([entry], root, build=True)
        assert entry['attribution']['category'] == 'wrong_when_written', entry
        shipped_gaps = revalidated_gaps()
        attribution.attribute_gaps(shipped_gaps)
        assert shipped_gaps['js-config']['undocumented'][0]['attribution']['category'] == 'shipped_undocumented'
        commit_file(monorepo, 'frontend/src/pin.tsx', 'export const Pin = () => <button>Pin insight</button>\n', '2025-01-20')
        removed_again_revision = commit_file(monorepo, 'frontend/src/pin.tsx', 'export const Pin = () => <button>Fasten insight</button>\n', '2025-01-30')
        attribution.attribute_findings([entry], root, build=True)
        commit_file(repositories['site'], 'contents/docs/revalidated.mdx', '- `revalidated_option`: revalidates.\n', '2025-02-05')
        docs_removal_revision = commit_file(repositories['site'], 'contents/docs/revalidated.mdx', 'Other text\n', '2025-02-06')
        removed_gaps = revalidated_gaps()
        attribution.attribute_gaps(removed_gaps)
    assert entry['attribution']['category'] == 'wrong_when_written' and entry['attribution']['rule'] == 'source_absent_when_claim_written', entry
    cached_sources = json.loads((root / 'revalidation-cache.json').read_text())['sources']
    assert cached_sources[attribution.source_cache_key('monorepo', attribution.finding_reference(entry))]['event']['revision'] == removed_again_revision, cached_sources
    removed = removed_gaps['js-config']['undocumented'][0]['attribution']
    assert removed['category'] == 'docs_removed' and removed['source_changed_at']['revision'] == docs_removal_revision, removed
    print('PASS cached removal revalidated after restore and re-removal, claim absent when written stays wrong_when_written; cached shipped gap revalidated after docs add and remove')


def verify_docs_removed_needs_whole_identifier_after_shipping(root, repositories):
    gap_path = 'packages/browser/src/early.ts'
    commit_file(repositories['site'], 'contents/docs/early-before.mdx', '- `later_option`: later.\n', '2025-03-01')
    commit_file(repositories['site'], 'contents/docs/early-before.mdx', 'Other text\n', '2025-03-02')
    commit_file(repositories['posthog-js'], gap_path, 'EarlyThing?: boolean\nlater_option?: boolean\n', '2025-03-05')
    commit_file(repositories['site'], 'contents/docs/early-after.mdx', '- `updateEarlyThingEnrollment()`: enrolls.\n', '2025-03-06')
    commit_file(repositories['site'], 'contents/docs/early-after.mdx', 'Other text\n', '2025-03-07')
    gaps = {'js-config': {'undocumented': [{'name': name, 'origin': str(repositories['posthog-js'] / gap_path)} for name in ('EarlyThing', 'later_option')], 'partially_documented': []}}
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'boundary-cache.json'):
        attribution.attribute_gaps(gaps)
    assert [entry['attribution']['category'] for entry in gaps['js-config']['undocumented']] == ['shipped_undocumented', 'shipped_undocumented'], gaps
    print('PASS docs removal ignores longer identifiers and removals before the source shipped')


def verify_docs_link_written_route(root, repositories):
    site = repositories['site']
    commit_file(site, 'contents/docs/hop-old.mdx', '# Old page\n', '2024-04-01')
    commit_file(site, 'contents/docs/hop-link.mdx', '[Old](/docs/hop-old)\n', '2024-04-02')
    subprocess.run(['git', '-C', str(site), 'rm', '-q', 'contents/docs/hop-old.mdx'], check=True)
    (site / 'vercel.json').write_text('{"redirects": [{"source": "/docs/hop-old", "destination": "/docs/hop-never-existed"}]}')
    subprocess.run(['git', '-C', str(site), 'add', 'vercel.json'], check=True)
    deletion_revision = commit_staged(site, 'Delete hop-old and redirect it to a page that never existed', '2024-04-03')
    commit_file(site, 'contents/docs/moved-a.mdx', '# Moved page\n', '2024-05-01')
    subprocess.run(['git', '-C', str(site), 'mv', 'contents/docs/moved-a.mdx', 'contents/docs/moved-b.mdx'], check=True)
    (site / 'vercel.json').write_text('{"redirects": [{"source": "/docs/hop-old", "destination": "/docs/hop-never-existed"}, {"source": "/docs/moved-a", "destination": "/docs/moved-b"}]}')
    subprocess.run(['git', '-C', str(site), 'add', 'vercel.json'], check=True)
    commit_staged(site, 'Move moved-a to moved-b with a redirect', '2024-05-02')
    commit_file(site, 'contents/docs/moved-link.mdx', '[Moved](/docs/moved-a)\n', '2024-05-03')
    redirect_removal_revision = commit_file(site, 'vercel.json', '{"redirects": [{"source": "/docs/hop-old", "destination": "/docs/hop-never-existed"}]}', '2024-05-04')
    entries = [
        finding('hop-link.mdx', 'docs-link', '/docs/hop-old (missing: /docs/hop-never-existed)'),
        finding('moved-link.mdx', 'docs-link', '/docs/moved-a (missing: /docs/moved-a)'),
    ]
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'written-route-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings(entries, root, build=True)
    deleted, moved = (entry['attribution'] for entry in entries)
    assert deleted['category'] == 'introduced_by_docs_change' and deleted['source_changed_at']['revision'] == deletion_revision, deleted
    assert moved['category'] == 'introduced_by_docs_change' and moved['source_changed_at']['revision'] == redirect_removal_revision, moved
    assert moved['before']['file'] == 'vercel.json', moved
    print('PASS docs-link searches the written route behind a redirect and prefers a later redirect removal over an earlier move')


def verify_docs_link_redirect_chain_loses_middle_hop(root, repositories):
    site = repositories['site']
    commit_file(site, 'contents/docs/chain-c.mdx', '# Chain end\n', '2024-06-01')
    commit_file(site, 'vercel.json', '{"redirects": [{"source": "/docs/chain-a", "destination": "/docs/chain-b"}, {"source": "/docs/chain-b", "destination": "/docs/chain-c"}]}', '2024-06-02')
    commit_file(site, 'contents/docs/chain-link.mdx', '[Chain](/docs/chain-a)\n[Absolute chain](https://posthog.com/docs/chain-a)\n', '2024-06-03')
    middle_removal_revision = commit_file(site, 'vercel.json', '{"redirects": [{"source": "/docs/chain-a", "destination": "/docs/chain-b"}]}', '2024-06-04')
    entries = [
        finding('chain-link.mdx', 'docs-link', '/docs/chain-a (missing: /docs/chain-b)'),
        finding('chain-link.mdx', 'docs-link', 'https://posthog.com/docs/chain-a (missing: /docs/chain-b)'),
    ]
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'redirect-chain-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings(entries, root, build=True)
    for entry in entries:
        evidence = entry['attribution']
        assert evidence['category'] == 'introduced_by_docs_change' and evidence['source_changed_at']['revision'] == middle_removal_revision, evidence
        assert evidence['before']['file'] == 'vercel.json', evidence
    print('PASS docs-link credits the removal of a middle redirect hop, for relative and absolute posthog.com links')


def verify_full_history_and_errors(root, repositories):
    before = finding('before.mdx', 'ui-label', 'Save insight')
    never_existed = finding('before.mdx', 'app-url', 'outside-window')
    commit_file(repositories['site'], 'contents/docs/before.mdx', 'Save insight\n/outside-window\n', '2025-09-01')
    commit_file(repositories['site'], 'contents/docs/before.mdx', 'Save insight\nSave insight\n/outside-window\n', '2025-12-05')
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'before-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings([before, never_existed], root, build=True)
    assert before['attribution']['category'] == 'wrong_when_written' and before['attribution']['rule'] == 'source_absent_when_claim_written', before
    assert before['attribution']['source_changed_at']['date'] == '2025-10-02', before
    assert before['attribution']['claim_written_at']['event']['date'] == '2025-09-01'
    assert never_existed['attribution']['category'] == 'wrong_when_written', never_existed
    before.pop('attribution')
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'error-cache.json'), patch.object(attribution, 'disappearance_search', side_effect=ValueError('Synthetic git failure')), contextlib.redirect_stdout(io.StringIO()):
        try:
            attribution.attribute_findings([before], root, build=True)
            raise AssertionError('Git errors must fail attribution')
        except ValueError as failure:
            assert 'Save insight' in str(failure) and 'Synthetic git failure' in str(failure)
    assert 'attribution' not in before
    invalid_output = root / 'unparseable-output'
    invalid_output.mkdir()
    (invalid_output / 'all-findings.txt').write_text('unparseable-finding\n')
    failed_build = subprocess.run(['python3', 'attribution.py', '--build'], cwd=TOOL_ROOT, env={**os.environ, 'DOCAUDIT_OUTPUT_ROOT': str(invalid_output)}, capture_output=True, text=True)
    assert failed_build.returncode != 0 and 'all-findings.txt:1' in failed_build.stderr
    print('PASS full history, first duplicate occurrence, label absent when written, named errors without categories and nonzero build')


def settings_map_source(*sections):
    blocks = ''.join(
        f"    {{\n        id: '{section}',\n        level: '{level}',\n        settings: [\n"
        + ''.join(f"            {{ id: '{anchor}', title: 'Example' }},\n" for anchor in anchors)
        + '        ],\n    },\n'
        for section, level, anchors in sections
    )
    return 'export const SettingsMap = [\n' + blocks + ']\n'


def settings_line(source_text, anchor):
    return next(number for number, line in enumerate(source_text.splitlines(), start=1) if f"id: '{anchor}'" in line)


def attribute_settings(root, repositories, entry):
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'settings-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings([entry], root, build=True)
    return entry['attribution']


def verify_settings_move(root, repositories):
    monorepo = repositories['monorepo']
    audit = attribution.checker()
    before_move = settings_map_source(('project-first', 'project', ['example-anchor', 'kept-anchor']), ('project-other', 'project', ['other-anchor']))
    commit_file(monorepo, attribution.SETTINGS_MAP, before_move, '2025-10-03')
    commit_file(repositories['site'], 'contents/docs/settings.mdx', '[Setting](https://us.posthog.com/settings/project-first#example-anchor)\n', '2025-10-04')
    after_move = settings_map_source(('project-first', 'project', ['kept-anchor']), ('project-other', 'project', ['other-anchor', 'example-anchor']))
    move_revision = commit_file(monorepo, attribution.SETTINGS_MAP, after_move, '2025-10-05')
    moved = attribute_settings(root, repositories, finding('settings.mdx', 'settings-anchor', 'settings/project-first#example-anchor'))
    assert moved['category'] == 'introduced_upstream', moved
    assert moved['source_changed_at']['revision'] == move_revision, moved
    assert moved['before'] == {'revision': attribution.git(monorepo, 'rev-parse', move_revision + '^').strip(), 'file': attribution.SETTINGS_MAP, 'line': settings_line(before_move, 'example-anchor')}, moved

    organization_first = settings_map_source(('organization-details', 'organization', ['organization-delete']), ('project-first', 'project', ['kept-anchor']), ('project-other', 'project', ['other-anchor', 'example-anchor']))
    commit_file(monorepo, attribution.SETTINGS_MAP, organization_first, '2025-10-06')
    commit_file(repositories['site'], 'contents/docs/organization.mdx', '[Delete](https://us.posthog.com/settings/organization#organization-delete)\n', '2025-10-07')
    members_inserted_first = organization_first.replace('export const SettingsMap = [\n', settings_map_source(('organization-members', 'organization', ['invite'])).removesuffix(']\n'), 1)
    insertion_revision = commit_file(monorepo, attribution.SETTINGS_MAP, members_inserted_first, '2025-10-08')
    scope = attribution.settings_scope_at(monorepo, insertion_revision)
    assert 'organization-delete' in scope.all_ids
    assert 'organization-delete' not in audit.settings_anchor_ids_in_scope('organization', scope)
    level_link = attribute_settings(root, repositories, finding('organization.mdx', 'settings-anchor', 'settings/organization#organization-delete'))
    assert level_link['category'] == 'introduced_upstream', level_link
    assert level_link['source_changed_at']['revision'] == insertion_revision, level_link

    commit_file(repositories['site'], 'contents/docs/wrong-settings.mdx', '[Setting](https://us.posthog.com/settings/project-other#kept-anchor)\n', '2025-10-09')
    elsewhere_only = attribute_settings(root, repositories, finding('wrong-settings.mdx', 'settings-anchor', 'settings/project-other#kept-anchor'))
    assert elsewhere_only['category'] == 'wrong_when_written' and elsewhere_only['rule'] == 'source_never_existed_in_full_history', elsewhere_only
    assert 'kept-anchor' in audit.settings_anchor_ids_in_scope('project-never-defined', scope)
    environment_revision = commit_file(monorepo, attribution.SETTINGS_MAP, settings_map_source(('environment-replay', 'environment', ['replay-anchor'])), '2025-10-10')
    assert 'replay-anchor' in audit.settings_anchor_ids_in_scope('project-replay', attribution.settings_scope_at(monorepo, environment_revision))
    print('PASS settings anchor scoped per linked section: moved between sections, level link losing its first section, present only elsewhere, equivalents')


def verify_sdk_and_deleted_target(root, repositories):
    commit_file(repositories['monorepo'], 'packages/browser/src/client.ts', 'export class PostHog {\n    removedMethod<Value>() {}\n}\n', '2025-10-06')
    commit_file(repositories['monorepo'], 'packages/web/src/generic.ts', 'export class PostHog {\n    webOnlyMethod() {}\n}\n', '2025-10-06')
    commit_file(repositories['monorepo'], 'posthog/client.py', 'class Client:\n    def removed_method(self):\n        pass\n', '2025-10-06')
    commit_file(repositories['site'], 'contents/docs/sdk.mdx', 'posthog.removedMethod()\nposthog.removed_method()\nposthog.webOnlyMethod()\n', '2025-10-07')
    commit_file(repositories['monorepo'], 'packages/browser/src/client.ts', 'export class PostHog {}\n', '2025-10-08')
    commit_file(repositories['monorepo'], 'packages/web/src/generic.ts', 'export class PostHog {}\n', '2025-10-08')
    commit_file(repositories['monorepo'], 'posthog/client.py', 'class Client:\n    pass\n', '2025-10-08')
    commit_file(repositories['site'], 'contents/docs/deleted.mdx', '# Delete me\n', '2025-10-06')
    commit_file(repositories['site'], 'contents/docs/deletion-link.mdx', '[Deleted](/docs/deleted)\n', '2025-10-07')
    commit_file(repositories['site'], 'contents/docs/deleted.mdx', None, '2025-10-08')
    entries = [
        finding('sdk.mdx', 'sdk-method', 'js: posthog.removedMethod'),
        finding('sdk.mdx', 'sdk-method', 'python: posthog.removed_method'),
        finding('deletion-link.mdx', 'docs-link', '/docs/deleted (missing: /docs/deleted)'),
        finding('sdk.mdx', 'sdk-method', 'typescript: posthog.webOnlyMethod'),
    ]
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'sdk-deletion-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings(entries, root, build=True)
    assert [entry['attribution']['category'] for entry in entries] == ['introduced_upstream', 'introduced_upstream', 'introduced_by_docs_change', 'introduced_upstream'], entries
    assert entries[3]['attribution']['source_changed_at']['file'] == 'packages/web/src/generic.ts', entries[3]
    print('PASS generic JavaScript method, Python method, target file deletion and any-package JavaScript method')


def verify_moved_page(root, repositories):
    commit_file(repositories['monorepo'], 'frontend/src/archive.tsx', 'export const Archive = () => <button>Archive insight</button>\n', '2025-10-05')
    claim_revision = commit_file(repositories['site'], 'contents/docs/moved-before.mdx', 'Click **Archive insight**.\n', '2025-10-10')
    removal_revision = commit_file(repositories['monorepo'], 'frontend/src/archive.tsx', 'export const Archive = () => <button>Store insight</button>\n', '2025-10-20')
    move_file(repositories['site'], 'contents/docs/moved-before.mdx', 'contents/docs/moved-after.mdx', '2025-10-25')
    entry = finding('moved-after.mdx', 'ui-label', 'Archive insight')
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'moved-cache.json'), contextlib.redirect_stdout(io.StringIO()):
        attribution.attribute_findings([entry], root, build=True)
    assert entry['attribution']['category'] == 'introduced_upstream', entry
    assert entry['attribution']['claim_written_at']['event']['revision'] == claim_revision, entry
    assert entry['attribution']['source_changed_at']['revision'] == removal_revision, entry
    print('PASS claim written before its page moved keeps the original write date')


def verify_caught_replay():
    def attributed(kind, repository, category, changed_file):
        return {'kind': kind, 'attribution': {'repository': repository, 'category': category, 'source_changed_at': {'file': changed_file}}}
    assert attribution.caught_at_pr_time(attributed('docs-fragment', 'site', 'introduced_by_docs_change', 'contents/docs/target.mdx'))
    assert attribution.caught_at_pr_time(attributed('docs-link', 'site', 'wrong_when_written', 'contents/docs/target.mdx'))
    assert attribution.caught_at_pr_time(attributed('sdk-method', 'posthog-python', 'introduced_upstream', 'posthog/__init__.py'))
    assert not attribution.caught_at_pr_time(attributed('ui-label', 'monorepo', 'introduced_upstream', 'frontend/src/button.stories.tsx'))
    assert not attribution.caught_at_pr_time(attributed('ui-label', 'monorepo', 'wrong_when_written', 'frontend/src/button.tsx'))
    assert not attribution.caught_at_pr_time(attributed('sdk-method', 'posthog-js', 'introduced_upstream', 'README.md'))
    assert not attribution.caught_at_pr_time(attributed('settings', 'monorepo', 'shipped_undocumented', attribution.SETTINGS_MAP))
    assert not attribution.caught_at_pr_time(attributed('js-changelog', 'posthog-js', 'shipped_undocumented', 'packages/browser/CHANGELOG.md'))
    print('PASS caught-at-PR-time count replays the gate path reachability and admits categories no gate checks')


def verify_site_gate_and_legacy_sdk_layout_verdicts():
    def attributed(kind, repository, category, changed_file):
        return {'kind': kind, 'attribution': {'repository': repository, 'category': category, 'source_changed_at': {'file': changed_file}}}
    assert attribution.caught_at_pr_time(attributed('app-url', 'monorepo', 'wrong_when_written', 'frontend/src/scenes/urls.ts'))
    assert attribution.caught_at_pr_time(attributed('settings-anchor', 'monorepo', 'wrong_when_written', attribution.SETTINGS_MAP))
    legacy = attribution.pr_gate_verdict(attributed('js-config', 'posthog-js', 'shipped_undocumented', 'src/types.ts'))
    assert legacy == (False, 'the change predates the packages/ layout that the posthog-js gate checks'), legacy
    print('PASS the site gate catches app routes and settings anchors written wrong; legacy posthog-js src/ changes are named as predating the gate layout')


def verify_generated_live_route_in_history(root):
    site = initialize_repository(root / 'generated-route-site')
    commit_file(site, 'vercel.json', '{"redirects": [{"source": "/docs/generated-old", "destination": "/docs/generated-reference"}]}', '2024-06-30')
    commit_file(site, 'contents/docs/generated-link.mdx', '[Reference](/docs/generated-old)\n', '2024-07-01')
    removal_revision = commit_file(site, 'vercel.json', '{"redirects": []}', '2024-07-05')
    entry = finding('generated-link.mdx', 'docs-link', '/docs/generated-old (missing: /docs/generated-old)')
    audit = attribution.checker()
    attribution.is_generated_live_route.cache_clear()
    with patch.object(attribution, 'REPOSITORIES', {'site': site}), patch.object(attribution, 'CACHE_FILE', root / 'generated-route-cache.json'), patch.object(audit, 'LIVE_ROUTES', audit.LIVE_ROUTES | {'/docs/generated-reference'}):
        attribution.attribute_findings([entry], root)
    attribution.is_generated_live_route.cache_clear()
    evidence = entry['attribution']
    assert evidence['category'] == 'introduced_by_docs_change' and evidence['source_changed_at']['revision'] == removal_revision, evidence
    assert evidence['before']['file'] == 'vercel.json', evidence
    print('PASS a link that redirected to a generated live route resolves in history until the redirect is removed')


def verify_snippet_anchor_in_history(root):
    site = initialize_repository(root / 'snippet-anchor-site')
    commit_file(site, 'contents/docs/_snippets/shared.mdx', '## Shared heading\n\nShared text.\n', '2024-08-01')
    commit_file(site, 'contents/docs/host.mdx', "import Shared from './_snippets/shared.mdx'\n\n# Host\n\n<Shared />\n", '2024-08-02')
    commit_file(site, 'contents/docs/snippet-link.mdx', '[Shared](/docs/host#shared-heading)\n', '2024-08-03')
    rename_revision = commit_file(site, 'contents/docs/_snippets/shared.mdx', '## Renamed heading\n\nShared text.\n', '2024-08-04')
    entry = finding('snippet-link.mdx', 'docs-fragment', '/docs/host#shared-heading (/docs/host)')
    with patch.object(attribution, 'REPOSITORIES', {'site': site}), patch.object(attribution, 'CACHE_FILE', root / 'snippet-anchor-cache.json'):
        attribution.attribute_findings([entry], root)
    evidence = entry['attribution']
    assert evidence['category'] == 'introduced_by_docs_change' and evidence['source_changed_at']['revision'] == rename_revision, evidence
    assert evidence['source_changed_at']['file'] == 'contents/docs/_snippets/shared.mdx', evidence
    assert evidence['before'] == {'revision': attribution.git(site, 'rev-parse', rename_revision + '^').strip(), 'file': 'contents/docs/_snippets/shared.mdx', 'line': 1}, evidence
    print('PASS an anchor defined in an imported snippet resolves in history until the snippet heading is renamed')


def verify_code_import_at_write_is_undecidable(root):
    site = initialize_repository(root / 'code-import-at-write-site')
    commit_file(site, 'contents/docs/_snippets/Generated.tsx', 'export const Generated = () => null\n', '2024-09-01')
    commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n<Generated />\n", '2024-09-02')
    commit_file(site, 'contents/docs/code-link.mdx', '[Generated](/docs/host#generated-heading)\n', '2024-09-03')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-04')
    entry = finding('code-link.mdx', 'docs-fragment', '/docs/host#generated-heading (/docs/host)')
    with patch.object(attribution, 'REPOSITORIES', {'site': site}), patch.object(attribution, 'CACHE_FILE', root / 'code-import-at-write-cache.json'):
        try:
            attribution.attribute_findings([entry], root)
            raise AssertionError('A fragment hidden behind a code import when the claim was written must not be classified')
        except ValueError as failure:
            assert 'undecidable' in str(failure) and "imports code './_snippets/Generated'" in str(failure), failure
    print('PASS a fragment whose page imported code when the claim was written fails as undecidable instead of reading as present')


def attribute_code_import_fixture(root, name):
    site = root / name
    entry = finding('code-link.mdx', 'docs-fragment', '/docs/host#generated-heading (/docs/host)')
    with patch.object(attribution, 'REPOSITORIES', {'site': site}), patch.object(attribution, 'CACHE_FILE', root / f'{name}-cache.json'):
        attribution.attribute_findings([entry], root)
    return entry['attribution']


def verify_code_import_removal_is_not_a_disappearance(root):
    site = initialize_repository(root / 'code-import-removal-site')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-01')
    commit_file(site, 'contents/docs/code-link.mdx', '[Generated](/docs/host#generated-heading)\n', '2024-09-02')
    commit_file(site, 'contents/docs/_snippets/Generated.tsx', 'export const Generated = () => null\n', '2024-09-03')
    commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n<Generated />\n", '2024-09-04')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-05')
    with patch.object(attribution, 'REPOSITORIES', {'site': site}):
        disappearance = attribution.site_disappearance('docs-fragment', '/docs/host#generated-heading')
    assert disappearance['status'] == 'not_found' and not disappearance['existed_in_history'], disappearance
    evidence = attribute_code_import_fixture(root, 'code-import-removal-site')
    assert evidence['category'] == 'wrong_when_written' and evidence['rule'] == 'source_never_existed_in_full_history', evidence
    assert evidence['source_changed_at'] is None, evidence
    print('PASS dropping a code import that only might have generated a fragment is neither a disappearance nor proof the fragment existed')


def verify_code_import_is_not_an_appearance(root):
    site = initialize_repository(root / 'code-import-appearance-site')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-01')
    commit_file(site, 'contents/docs/code-link.mdx', '[Generated](/docs/host#generated-heading)\n', '2024-09-02')
    commit_file(site, 'contents/docs/_snippets/Generated.tsx', 'export const Generated = () => null\n', '2024-09-03')
    commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n<Generated />\n", '2024-09-04')
    heading_revision = commit_file(site, 'contents/docs/host.mdx', '# Host\n\n## Generated heading\n', '2024-09-05')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-06')
    evidence = attribute_code_import_fixture(root, 'code-import-appearance-site')
    assert evidence['category'] == 'wrong_when_written' and evidence['rule'] == 'source_absent_when_claim_written', evidence
    assert evidence['source_changed_at']['revision'] == heading_revision, evidence
    assert evidence['before'] == {'revision': heading_revision, 'file': 'contents/docs/host.mdx', 'line': 3}, evidence
    print('PASS a fragment appears at the first verifiably present revision, not at an earlier code import that might generate it')


def verify_code_import_removed_before_write_is_never_existed(root):
    site = initialize_repository(root / 'code-import-before-write-site')
    commit_file(site, 'contents/docs/_snippets/Generated.tsx', 'export const Generated = () => null\n', '2024-09-01')
    commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n<Generated />\n", '2024-09-02')
    commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-03')
    commit_file(site, 'contents/docs/code-link.mdx', '[Generated](/docs/host#generated-heading)\n', '2024-09-04')
    evidence = attribute_code_import_fixture(root, 'code-import-before-write-site')
    assert evidence['category'] == 'wrong_when_written' and evidence['rule'] == 'source_never_existed_in_full_history', evidence
    assert 'before' not in evidence, evidence
    print('PASS a fragment written after a code import was dropped never existed rather than being removed at the import line')


def verify_anchor_removed_behind_code_import_is_undecidable(root):
    site = initialize_repository(root / 'code-import-hides-removal-site')
    commit_file(site, 'contents/docs/_snippets/Generated.tsx', 'export const Generated = () => null\n', '2024-09-01')
    commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n## Generated heading\n\n<Generated />\n", '2024-09-02')
    commit_file(site, 'contents/docs/code-link.mdx', '[Generated](/docs/host#generated-heading)\n', '2024-09-03')
    heading_removal_revision = commit_file(site, 'contents/docs/host.mdx', "import Generated from './_snippets/Generated'\n\n# Host\n\n<Generated />\n", '2024-09-04')
    import_removal_revision = commit_file(site, 'contents/docs/host.mdx', '# Host\n', '2024-09-05')
    try:
        attribute_code_import_fixture(root, 'code-import-hides-removal-site')
        raise AssertionError('An anchor removed while the page imported code must not be blamed on the later import removal')
    except ValueError as failure:
        assert 'undecidable' in str(failure) and heading_removal_revision in str(failure), failure
        assert import_removal_revision not in str(failure), failure
    print('PASS an anchor removed while its page imports code fails as undecidable instead of blaming the later import removal')


def verify_index_snapshot_survives_refresh(root):
    checkout = initialize_repository(root / 'snapshot-refresh')
    base = commit_file(checkout, 'frontend/src/a.tsx', 'one\n', '2025-02-01')
    first_change = commit_file(checkout, 'frontend/src/a.tsx', 'two\n', '2025-02-02')

    def changes_since_base():
        return [change['revision'] for change in history_index.path_history('monorepo', checkout, ['frontend/src'], f'{base}..HEAD')]

    earlier = history_index.repository_index('monorepo', checkout)
    assert changes_since_base() == [first_change]
    later_change = commit_file(checkout, 'frontend/src/a.tsx', 'three\n', '2025-02-03')
    assert changes_since_base() == [later_change, first_change]
    assert earlier.state['head'] == first_change and len(earlier.state['commits']) == 2, earlier.state['head']
    earlier_touching = earlier.commit_ids_touching(['frontend/src'])
    assert history_index.revisions_in_order(earlier, earlier_touching, f'{base}..HEAD') == [first_change]
    try:
        history_index.range_pinned_to_head('main..feature', first_change)
        raise AssertionError('A range ending at a movable ref other than HEAD must not be cached')
    except ValueError as failure:
        assert 'main..feature' in str(failure), failure
    print('PASS an index snapshot taken before HEAD moved keeps its own head and commits; a ..HEAD range follows the refreshed HEAD')


def verify_missing_blobs(root):
    origin = initialize_repository(root / 'partial-origin')
    commit_file(origin, 'frontend/src/button.tsx', 'export const Button = () => <button>Save insight</button>\n', '2025-10-02')
    commit_file(origin, 'frontend/src/button.tsx', 'export const Button = () => <button>Store insight</button>\n', '2025-11-10')
    commit_file(origin, attribution.SETTINGS_MAP, settings_map_source(('project-first', 'project', ['example-anchor'])), '2025-11-11')
    commit_file(origin, attribution.SETTINGS_MAP, settings_map_source(('project-first', 'project', ['kept-anchor'])), '2025-11-12')
    subprocess.run(['git', '-C', str(origin), 'config', 'uploadpack.allowFilter', 'true'], check=True)
    subprocess.run(['git', '-C', str(origin), 'config', 'uploadpack.allowAnySHA1InWant', 'true'], check=True)
    clone = root / 'partial-clone'
    subprocess.run(['git', 'clone', '-q', '--no-checkout', '--filter=blob:none', origin.as_uri(), str(clone)], check=True)
    with patch.object(attribution, 'REPOSITORIES', {'monorepo': clone}):
        before = attribution.run_history(attribution.disappearance_search, 'monorepo', 'Save insight')
        assert before == {'status': 'error', 'reason': attribution.MISSING_BLOB_REASON}, before
        settings_before = attribution.run_history(attribution.disappearance_search, 'monorepo', 'settings/project-first#example-anchor', kind='settings-anchor')
        assert settings_before == {'status': 'error', 'reason': attribution.MISSING_BLOB_REASON}, settings_before
        assert attribution.blob_text(clone, 'HEAD', 'frontend/src/never-existed.tsx') == ''
        assert attribution.prefetch_history_blobs('monorepo') == 4
        assert attribution.prefetch_history_blobs('monorepo') == 0
        after = attribution.run_history(attribution.disappearance_search, 'monorepo', 'Save insight')
        settings_after = attribution.run_history(attribution.disappearance_search, 'monorepo', 'settings/project-first#example-anchor', kind='settings-anchor')
    assert after['status'] == 'found', after
    assert settings_after['status'] == 'found', settings_after
    monorepo = attribution.REPOSITORIES['monorepo']
    try:
        attribution.blob_text(monorepo, 'origin/(feat)billing-alerts', '.github/workflows/container-images-cd.yml')
        raise AssertionError('An unfetched blob present in the tree must not read as an empty file')
    except attribution.MissingBlobError as failure:
        assert 'container-images-cd.yml' in str(failure)
    print('PASS unfetched blobs fail loudly instead of reading as empty, including settings history; prefetch restores history search')


def verify_batched_label_search_missing_blobs(root):
    origin = initialize_repository(root / 'batched-partial-origin')
    commit_file(origin, 'frontend/src/button.tsx', 'export const Button = () => <button>Save insight</button>\n', '2025-10-02')
    commit_file(origin, 'frontend/src/button.tsx', 'export const Button = () => <button>Store insight</button>\n', '2025-11-10')
    subprocess.run(['git', '-C', str(origin), 'config', 'uploadpack.allowFilter', 'true'], check=True)
    clone = root / 'batched-partial-clone'
    subprocess.run(['git', 'clone', '-q', '--no-checkout', '--filter=blob:none', origin.as_uri(), str(clone)], check=True)
    findings = [finding('insights.mdx', 'ui-label', 'Save insight'), finding('insights.mdx', 'ui-label', 'Archive insight')]
    with patch.object(attribution, 'REPOSITORIES', {'monorepo': clone}), patch.object(attribution, 'CACHE_FILE', root / 'batched-partial-cache.json'):
        try:
            attribution.attribute_findings(findings, root)
            raise AssertionError('A batched UI label search over unfetched blobs must fail')
        except ValueError as failure:
            assert attribution.MISSING_BLOB_REASON in str(failure), failure
            assert isinstance(failure.__cause__, subprocess.CalledProcessError), failure.__cause__
    print('PASS batched UI label search over unfetched blobs fails with the prefetch remedy')


def verify_partial_clone_page_move(root):
    origin = initialize_repository(root / 'partial-site-origin')
    landing_lines = ''.join(f'Landing paragraph {number}\n' for number in range(12))
    claim_revision = commit_file(origin, 'src/pages/docs/landing.tsx', landing_lines + 'Click **Archive insight**.\n', '2025-10-10')
    (origin / 'contents/docs').mkdir(parents=True)
    subprocess.run(['git', '-C', str(origin), 'mv', 'src/pages/docs/landing.tsx', 'contents/docs/landing.mdx'], check=True)
    (origin / 'contents/docs/landing.mdx').write_text('# Landing\n' + landing_lines + 'Click **Archive insight**.\n')
    subprocess.run(['git', '-C', str(origin), 'add', '-A'], check=True)
    commit_staged(origin, 'Move landing page into contents/docs', '2025-10-25')
    subprocess.run(['git', '-C', str(origin), 'config', 'uploadpack.allowFilter', 'true'], check=True)
    subprocess.run(['git', '-C', str(origin), 'config', 'uploadpack.allowAnySHA1InWant', 'true'], check=True)
    clone = root / 'partial-site-clone'
    subprocess.run(['git', 'clone', '-q', '--filter=blob:none', origin.as_uri(), str(clone)], check=True)
    entry = finding('landing.mdx', 'ui-label', 'Archive insight')
    reference = attribution.finding_reference(entry)
    with patch.object(attribution, 'REPOSITORIES', {'site': clone}):
        before = attribution.run_history(attribution.claim_written_search, entry, reference)
        assert before == {'status': 'error', 'reason': attribution.MISSING_BLOB_REASON}, before
        assert attribution.prefetch_history_blobs('site') > 0
        assert attribution.prefetch_history_blobs('site') == 0
        after = attribution.run_history(attribution.claim_written_search, entry, reference)
    assert after['status'] == 'found' and after['event']['revision'] == claim_revision, after
    print('PASS partial-clone page moved from a non-docs path is attributable after prefetch')


def verify_unbounded_refresh(root, repositories):
    findings = [finding('newer.mdx', 'ui-label', f'Missing label {number}') for number in range(10)]
    source = {'status': 'not_found', 'existed_in_history': False, 'reason': 'never existed'}
    written = {'status': 'found', 'event': {'revision': 'fixture', 'timestamp': 1, 'date': '2025-01-01', 'file': 'contents/docs/newer.mdx'}}
    def search_history(search, *arguments, **options):
        return source if search == attribution.source_change_search else written
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'unbounded-cache.json'), patch.object(attribution, 'run_history', side_effect=search_history) as history:
        attribution.attribute_findings(findings, root)
    assert history.call_count == 20, history.call_count
    assert all(entry['attribution']['category'] == 'wrong_when_written' for entry in findings)
    print('PASS normal refresh computes every uncached claim without a budget')


def verify_total_categories(root, repositories):
    assert attribution.parse_json_with_trailing_commas('{"literal": ",}",}') == {'literal': ',}'}
    commit_file(repositories['site'], 'src/pages/docs/twice.tsx', 'Click escaped\\_label.\n', '2024-01-01')
    (repositories['site'] / 'contents/docs').mkdir(exist_ok=True)
    first_revision = attribution.git(repositories['site'], 'rev-parse', 'HEAD').strip()
    move_file(repositories['site'], 'src/pages/docs/twice.tsx', 'contents/docs/once.mdx', '2024-02-01')
    move_file(repositories['site'], 'contents/docs/once.mdx', 'contents/docs/twice.mdx', '2024-03-01')
    entry = finding('twice.mdx', 'ui-label', 'escaped_label')
    commit_file(repositories['site'], 'contents/docs/gap.mdx', '- `brand_new_option`: new.\n', '2024-01-02')
    commit_file(repositories['site'], 'contents/docs/gap.mdx', 'Other text\n', '2024-02-02')
    gap_path = 'packages/browser/src/config.ts'
    shipped_revision = commit_file(repositories['posthog-js'], gap_path, 'brand_new_option?: boolean\nsecond_new_option?: boolean\n', '2024-01-01', 'Add public options (#42)')
    commit_file(repositories['site'], 'vercel.json', '{"redirects": [{"source": "/docs/old-route", "destination": "/docs/twice",},],}', '2024-01-01')
    commit_file(repositories['site'], 'contents/docs/redirect.mdx', '[Old](/docs/old-route)\n', '2024-02-01')
    redirect_revision = commit_file(repositories['site'], 'vercel.json', '{"redirects": []}', '2024-03-01')
    redirect = finding('redirect.mdx', 'docs-link', '/docs/old-route (missing: /docs/old-route)')
    gaps = {'js-config': {'undocumented': [{'name': name, 'origin': str(repositories['posthog-js'] / gap_path)} for name in ('brand_new_option', 'second_new_option')], 'partially_documented': []}}
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'total-cache.json'):
        attribution.attribute_findings([entry, redirect], root)
        attribution.attribute_gaps(gaps)
        written = entry['attribution']['claim_written_at']
        assert written['event']['revision'] == first_revision, written
        assert written['rule'] == 'first_normalized_claim_in_page_lineage'
        assert redirect['attribution']['category'] == 'introduced_by_docs_change', redirect
        assert redirect['attribution']['source_changed_at']['revision'] == redirect_revision
        removed, shipped = gaps['js-config']['undocumented']
        assert removed['attribution']['category'] == 'docs_removed', removed
        assert shipped['attribution']['category'] == 'shipped_undocumented', shipped
        assert shipped['attribution']['evidence'][0]['revision'] == shipped_revision
        assert shipped['attribution']['evidence'][0]['pr'] == 42
        assert attribution.caught_at_pr_time({'kind': 'js-config', **shipped})
        assert not attribution.caught_at_pr_time({'kind': 'js-changelog', **shipped})
        commit_file(repositories['site'], 'contents/docs/tie-target.mdx', '# Original heading\n', '2024-01-01')
        (repositories['site'] / 'contents/docs/tie-target.mdx').write_text('# Replacement heading\n')
        (repositories['site'] / 'contents/docs/tie-link.mdx').write_text('[Target](/docs/tie-target#original-heading)\n')
        subprocess.run(['git', '-C', str(repositories['site']), 'add', 'contents/docs/tie-target.mdx', 'contents/docs/tie-link.mdx'], check=True)
        tied_revision = commit_staged(repositories['site'], 'Rename heading and introduce stale claim', '2024-02-01')
        tied_finding = finding('tie-link.mdx', 'docs-fragment', '/docs/tie-target#original-heading (/docs/tie-target)')
        attribution.attribute_findings([tied_finding], root)
        assert tied_finding['attribution']['category'] == 'wrong_when_written'
        assert tied_finding['attribution']['source_changed_at']['revision'] == tied_revision
        assert tied_finding['attribution']['claim_written_at']['event']['revision'] == tied_revision
        same_event = written['event']
        tied = attribution.classify_attribution('site', {'status': 'found', 'event': same_event, 'reason': 'removed', 'before': {'revision': 'e' * 40, 'file': same_event['file'], 'line': 1}}, written)
        assert tied['category'] == 'wrong_when_written' and tied['rule'] == 'source_removed_at_or_before_claim'
        commit_file(repositories['site'], 'contents/docs/fallback.mdx', 'The importantToken changed spelling.\n', '2024-01-01')
        fallback = finding('fallback.mdx', 'ui-label', 'importantToken changed')
        reference = {'claim': 'importantToken changed punctuation'}
        commit_file(repositories['site'], 'contents/docs/fallback.mdx', 'The importantToken changed punctuation is split.\n', '2024-02-01')
        reference['claim'] = 'importantToken\nchanged punctuation'
        normalized = attribution.claim_written_search(fallback, reference)
        assert normalized['rule'] == 'first_normalized_claim_in_page_lineage'
        reference['claim'] = 'importantToken **changed** punctuation'
        fallback_written = attribution.claim_written_search(fallback, reference)
        assert fallback_written['rule'] == 'earliest_page_lineage_commit_with_all_key_tokens'
    print('PASS coverage categories, redirect removal, two moves, markdown escape, whitespace, token fallback and tie rule')


def verify_real_written_route_and_sdk_packages():
    entry = finding('product-analytics/privacy.mdx', 'docs-link', '/docs/cdp/property-filter (missing: /docs/cdp/transformations/property-filter-plugin)')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['category'] == 'introduced_by_docs_change', evidence
    assert evidence['source_changed_at']['revision'].startswith('7d0e60e97'), evidence
    any_package = attribution.disappearance_search('posthog-js', 'js-any: posthog.get_distinct_id', kind='sdk-method')
    assert any_package['status'] == 'not_found' and any_package['existed_in_history'], any_package
    react_native = attribution.disappearance_search('posthog-js', 'js-react-native: posthog.get_distinct_id', kind='sdk-method')
    assert not (react_native['status'] == 'found' and react_native['event']['revision'].startswith('13ec7669f')), react_native
    print('PASS real /docs/cdp/property-filter: introduced_by_docs_change via 7d0e60e97; real js-any and js-react-native get_distinct_id not removed by the workspace migration')


def verify_real_docs_removed():
    from next_steps import coverage_gaps
    coverage = json.loads((attribution.doccoverage.OUTPUT_ROOT / 'coverage.json').read_text())
    entries = {(source, entry['name']): entry for source, statuses in coverage_gaps(coverage).items() for gaps in statuses.values() for entry in gaps}
    misattributed = [
        ('js-changelog', 'browser 1.299.0 #2642 EarlyAccessFeature,payload', '0b319f4a04', ('shipped_undocumented',)),
        ('python-changelog', 'posthog-python 7.47.0 d4a8f0a $ai_model_parameters', '704c3a6ad2', ('shipped_undocumented',)),
        ('python-changelog', 'posthog-python 7.49.0 e85647b $ai_model_parameters,$ai_service_tier', '704c3a6ad2', ('shipped_undocumented',)),
    ]
    for source, name, wrong_revision, allowed_categories in misattributed:
        evidence = attribution.gap_history_search(attribution.gap_repository(source), source, entries[(source, name)])
        assert not evidence['source_changed_at']['revision'].startswith(wrong_revision), (name, evidence)
        assert evidence['category'] in allowed_categories, (name, evidence)
        assert evidence['source_changed_at']['timestamp'] >= evidence['shipped_at']['timestamp'], (name, evidence)
    print('PASS real docs_removed gaps no longer credited to substring or pre-release removals')


def verify_real_output():
    subprocess.run(['python3', 'attribution.py', '--build'], cwd=TOOL_ROOT, check=True)
    from next_steps import main as build_next_steps
    with patch('sys.argv', ['next_steps.py']):
        build_next_steps()
    report = json.loads((attribution.doccoverage.OUTPUT_ROOT / 'next-steps.json').read_text())
    entries = list(report['attributed_findings'])
    entries.extend(entry for statuses in report['coverage_gaps'].values() for gaps in statuses.values() for entry in gaps)
    coverage = json.loads((attribution.doccoverage.OUTPUT_ROOT / 'coverage.json').read_text())
    coverage_entries = [entry for inventory in coverage.values() for entry in inventory if entry['status'] in ('undocumented', 'partially-documented')]
    assert len(coverage_entries) == len(entries) - len(report['attributed_findings'])
    assert all(entry['attribution']['category'] in ('shipped_undocumented', 'docs_removed') for entry in coverage_entries)
    entries_to_verify = [*entries, *coverage_entries]
    for entry in entries_to_verify:
        evidence = entry['attribution']
        assert evidence['category'] in attribution.CATEGORIES, entry
        assert evidence['rule'] and evidence['evidence'], entry
        assert all(event['revision'] and event['date'] and event['file'] for event in evidence['evidence']), entry
    for entry in report['attributed_findings']:
        evidence = entry['attribution']
        assert entry['source_url'].endswith(f"/contents/docs/{entry['page']}#L{entry['line']}"), entry
        assert evidence['source_changed_at'] is None or evidence['before']['line'] > 0, entry
    assert all(entry['definition']['line'] > 0 for entry in coverage_entries)
    assert 'unknown' not in (attribution.doccoverage.OUTPUT_ROOT / 'next-steps.md').read_text().casefold()
    undecided = [entry for entry in entries_to_verify if entry['attribution']['category'] not in attribution.CATEGORIES]
    assert not undecided, undecided
    print(f'PASS real current output: {len(entries)} items, 0 undecided, every finding with source line, live spot and evidence links')


def normalized_source_text(text, shows_link_text=False):
    if shows_link_text:
        text = re.sub(r'\[([^\]\n]+)\]\([^)\n]*\)', r'\1', text)
    text = re.sub(r'\\([\W_])', r'\1', text.replace('&nbsp;', ' '))
    return ' '.join(text.split()).casefold()


def claim_needles(kind, claim):
    if kind == 'sdk-method':
        return ['posthog.' + claim.split('posthog.', 1)[1]]
    if kind == 'settings-anchor':
        settings_path, anchor = claim.removeprefix('settings/').split('#', 1)
        return [f'settings/{settings_path}#', anchor]
    if kind == 'docs-fragment':
        link_path, fragment = claim.split('#', 1)
        return [needle for needle in (link_path if link_path.startswith('/') else '', '#' + fragment) if needle]
    return [claim]


def verify_recorded_lines():
    recorded = json.loads((attribution.doccoverage.OUTPUT_ROOT / 'finding-locations.json').read_text())
    site = attribution.REPOSITORIES['site']
    findings_file = (attribution.doccoverage.OUTPUT_ROOT / 'all-findings.txt').read_text().splitlines()
    assert len(recorded['findings']) == len(findings_file) > 0
    for location, finding_line in zip(recorded['findings'], findings_file):
        assert finding_line.split('\t')[5] == str(location['line']), (finding_line, location)
        page_lines = attribution.git(site, 'show', f"{recorded['site_revision']}:contents/docs/{location['page']}").splitlines()
        source_line = normalized_source_text(page_lines[location['line'] - 1], location['kind'] in ('ui-label', 'ui-label-minor'))
        for needle in claim_needles(location['kind'], location['claim']):
            assert normalized_source_text(needle) in source_line, (location, source_line)
    print(f"PASS {len(recorded['findings'])} recorded lines in the page source at {recorded['site_revision'][:12]} contain their claims")


def verify_real_history():
    entry = finding('product-analytics/trends/tips.mdx', 'ui-label', 'Hide weekend data')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['category'] == 'introduced_upstream', evidence
    assert evidence['source_changed_at']['revision'].startswith('1660f2a391b8'), evidence
    print('PASS real Hide weekend data: introduced_upstream via 1660f2a391b8')


def verify_claim_write_needs_whole_label_word(root, repositories):
    commit_file(repositories['site'], 'contents/docs/rebuild.mdx', 'Click **Rebuilding** to refresh.\n', '2024-07-01')
    whole_word_revision = commit_file(repositories['site'], 'contents/docs/rebuild.mdx', 'Click **Rebuilding** to refresh.\nClick **Rebuild**.\n', '2024-07-02')
    entry = finding('rebuild.mdx', 'ui-label', 'Rebuild')
    with patch.object(attribution, 'REPOSITORIES', repositories):
        written = attribution.claim_written_search(entry, attribution.finding_reference(entry))
    assert written['event']['revision'] == whole_word_revision, written
    print('PASS ui-label claim "Rebuild" is not written by "Rebuilding"')


def verify_generated_only_label_agrees_with_checker(root):
    monorepo = initialize_repository(root / 'generated-label-monorepo')
    commit_file(monorepo, 'frontend/src/kinds.generated.ts', "export const kinds = [{ label: 'Generated only kind' }]\n", '2025-05-01')
    commit_file(monorepo, 'frontend/src/visible.tsx', 'export const Visible = () => <button>Visible kind</button>\n', '2025-05-02')
    audit = attribution.checker()
    with patch.object(audit, 'MONOREPO', monorepo):
        label_index = audit.ui_label_index()
    with patch.object(attribution, 'REPOSITORIES', {'monorepo': monorepo}):
        for label, expected_status in (('Generated only kind', 'missing'), ('Visible kind', 'exact')):
            checker_status, _ = audit.search_label(label, label_index)
            location = attribution.source_location_at('monorepo', 'ui-label', label, 'HEAD')
            history = attribution.disappearance_search('monorepo', label)
            assert checker_status == expected_status, (label, checker_status)
            assert (location is None) == (checker_status == 'missing'), (label, location)
            assert history['existed_in_history'] == (checker_status != 'missing'), (label, history)
    print('PASS a label only in a generated file is missing for both the checker and attribution; a visible label is present for both')


def verify_non_utf8_blob_decodes_like_checker(root):
    monorepo = initialize_repository(root / 'non-utf8-monorepo')
    (monorepo / 'frontend/src').mkdir(parents=True)
    (monorepo / 'frontend/src/legacy.tsx').write_bytes(b'export const Legacy = () => <button>Rebuild \xff</button>\n')
    subprocess.run(['git', '-C', str(monorepo), 'add', 'frontend/src/legacy.tsx'], check=True)
    head = commit_staged(monorepo, 'Non-UTF-8 byte', '2025-05-02')
    expected_text = (monorepo / 'frontend/src/legacy.tsx').read_text(errors='ignore')
    assert attribution.blob_texts_at(monorepo, head, ['frontend/src/legacy.tsx']) == {'frontend/src/legacy.tsx': expected_text}
    object_id = attribution.git(monorepo, 'rev-parse', f'{head}:frontend/src/legacy.tsx').strip()
    assert attribution.blob_texts(monorepo, [object_id]) == {object_id: expected_text}
    print('PASS a non-UTF-8 historical blob decodes leniently, matching the checker read')


def verify_json_and_property_template_labels_agree_with_checker(root):
    monorepo = initialize_repository(root / 'shared-predicate-monorepo')
    commit_file(monorepo, 'frontend/src/kinds.ts', "export const kinds = [{ kindName: 'Funnel' }]\n", '2025-06-01')
    commit_file(monorepo, 'frontend/src/OpenButton.tsx', "export const OpenButton = ({ item }) => <button onClick={() => posthog.capture('Open Funnel')}>Open {item.kindName}</button>\n", '2025-06-02')
    commit_file(monorepo, 'frontend/src/strings.json', '{"title": "Json only label"}\n', '2025-06-03')
    commit_file(monorepo, 'products/desktop/frontend/Panel.tsx', 'export const Panel = () => <button>Desktop only action</button>\n', '2025-06-04')
    audit = attribution.checker()
    with patch.object(audit, 'MONOREPO', monorepo):
        label_index = audit.ui_label_index()
    with patch.object(attribution, 'REPOSITORIES', {'monorepo': monorepo}):
        for label, expected_status in (('Open Funnel', 'template'), ('Json only label', 'missing'), ('Desktop only action', 'exact')):
            checker_status, checker_files = audit.search_label(label, label_index)
            assert checker_status == expected_status, (label, checker_status)
            for is_desktop_page in (False, True):
                scoped_status, _ = audit.desktop_scoped_lookup((checker_status, checker_files), is_desktop_page)
                location = attribution.source_location_at('monorepo', 'ui-label', label, 'HEAD', is_desktop_page)
                history = attribution.disappearance_search('monorepo', label, is_desktop_page=is_desktop_page)
                assert (location is None) == (scoped_status == 'missing'), (label, is_desktop_page, location)
                assert history['existed_in_history'] == (scoped_status != 'missing'), (label, is_desktop_page, history)
    print('PASS a frontend/src JSON label, a property-value template label and a desktop-only label are present or absent alike for the checker and attribution')


def verify_absent_when_written_evidence_rendering():
    from next_steps import finding_evidence
    written = {'status': 'found', 'event': {'revision': 'a' * 40, 'timestamp': 10, 'date': '2026-01-01', 'file': 'contents/docs/absent.mdx'}}
    source = {'status': 'not_found', 'existed_in_history': True, 'reason': 'No disappearance found in full history'}
    appearance_event = {'revision': 'b' * 40, 'timestamp': 20, 'date': '2026-02-01', 'file': 'frontend/src/absent.tsx'}
    appearance = {'event': appearance_event, 'location': {'revision': 'b' * 40, 'file': 'frontend/src/absent.tsx', 'line': 3}}
    evidence = {'repository': 'monorepo', 'claim_written_at': written, 'source_changed_at': None, 'reason': source['reason']}
    appeared = attribution.classify_by_presence('monorepo', source, written, {'present': False, 'revision': 'c' * 40, 'appearance': appearance}, evidence)
    never_appeared = attribution.classify_by_presence('monorepo', source, written, {'present': False, 'revision': 'c' * 40, 'appearance': None}, evidence)
    appeared_text = finding_evidence(appeared)
    never_appeared_text = finding_evidence(never_appeared)
    checked = f"absent when written (checked at [{'c' * 12}](https://github.com/PostHog/posthog/commit/{'c' * 40}))"
    first_appeared = (
        f"first appeared in [{'b' * 12}](https://github.com/PostHog/posthog/commit/{'b' * 40}) 2026-02-01: "
        f"[frontend/src/absent.tsx#L3 @ {'b' * 12}](https://github.com/PostHog/posthog/blob/{'b' * 40}/frontend/src/absent.tsx#L3)"
    )
    assert checked in appeared_text and first_appeared in appeared_text, appeared_text
    assert checked in never_appeared_text and never_appeared_text.endswith('; never reappeared after the claim'), never_appeared_text
    for text in (appeared_text, never_appeared_text):
        assert 'removed' not in text and 'never resolved' not in text, text
    print('PASS absent-when-written evidence names the presence check and the first appearance, never a removal')


def verify_docs_removed_by_description_heading(root, repositories):
    origin_path = 'frontend/src/scenes/settings/widgets.tsx'
    commit_file(repositories['monorepo'], origin_path, "{ id: 'widget-toggles', title: 'Widget toggles' }\n", '2025-04-01')
    commit_file(repositories['site'], 'contents/docs/widgets/guide.mdx', '# Widgets guide\n\n## Controls whether widgets render on dashboards\n\nOpen the panel.\n', '2025-04-02')
    removal_revision = commit_file(repositories['site'], 'contents/docs/widgets/guide.mdx', '# Widgets guide\n\n## Dashboard layout\n\nOpen the panel.\n', '2025-04-03')
    entry = {
        'name': 'widget-toggles', 'title': 'Widget toggles', 'section': 'project-widgets', 'section_title': 'Widgets',
        'section_links': ['project-widgets'], 'keywords': [], 'descriptions': ['Controls whether widgets render on dashboards'],
        'origin': str(repositories['monorepo'] / origin_path),
    }
    gaps = {'settings': {'undocumented': [entry], 'partially_documented': []}}
    with patch.object(attribution, 'REPOSITORIES', repositories), patch.object(attribution, 'CACHE_FILE', root / 'description-heading-cache.json'):
        attribution.attribute_gaps(gaps)
    evidence = entry['attribution']
    assert evidence['category'] == 'docs_removed' and evidence['source_changed_at']['revision'] == removal_revision, evidence
    print('PASS removing a setting heading credited only by its description is docs_removed')


def verify_real_label_absent_when_written():
    generated_file = 'frontend/src/lib/components/AgentObjectTags/objectKinds.generated.ts'
    generated_text = attribution.blob_text(attribution.REPOSITORIES['monorepo'], 'HEAD', generated_file)
    assert attribution.label_line_count(generated_file, generated_text.splitlines(), generated_text, 'AI Evals') == 0
    entry = finding('ai-evals/index.mdx', 'ui-label', 'AI Evals')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['category'] == 'wrong_when_written' and evidence['rule'] == 'source_never_existed_in_full_history', evidence
    assert evidence['source_changed_at'] is None, evidence
    assert evidence['claim_written_at']['event']['date'] == '2026-05-21', evidence
    print('PASS real AI Evals: only generated and desktop-only files ever carried the label, so a non-desktop page never had it; wrong when written 2026-05-21')


def verify_real_redirect_in_force():
    entry = finding('how-posthog-works/ingestion-pipeline.mdx', 'docs-fragment', '/docs/integrate/identifying-users#considerations (/docs/product-analytics/identify)')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['source_changed_at']['revision'].startswith('5dfb1e0'), evidence
    assert evidence['source_changed_at']['file'] == 'vercel.json' and evidence['before']['file'] == 'vercel.json', evidence
    print('PASS real /docs/integrate/identifying-users#considerations: resolved through the vercel.json in force, broken by the 5dfb1e0 redirect retarget, not a0eba18')


def verify_real_sdk_property_removed_before_claim():
    entry = finding('surveys/troubleshooting.mdx', 'sdk-method', 'js: posthog.LIB_VERSION')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['category'] == 'wrong_when_written' and evidence['rule'] == 'source_removed_at_or_before_claim', evidence
    assert evidence['source_changed_at']['revision'].startswith('cdf0063'), evidence
    assert evidence['before']['file'] == 'src/posthog-core.js', evidence
    assert attribution.sdk_definition_count(["PostHogLib.prototype['LIB_VERSION'] = Config.LIB_VERSION"], 'LIB_VERSION', 'posthog-js') == 1
    print('PASS real posthog.LIB_VERSION: bracket prototype assignment removed in cdf0063 before the claim')


def verify_real_label_before_line():
    assert attribution.label_line_rank("                                'Calibration'", 'Calibration') < attribution.label_line_rank("    | 'calibration'", 'Calibration')
    entry = finding('replay-vision/calibration.mdx', 'ui-label', 'Calibration')
    attribution.attribute_findings([entry], attribution.doccoverage.OUTPUT_ROOT)
    evidence = entry['attribution']
    assert evidence['before']['file'] == 'products/replay_vision/frontend/replay_scanners/ReplayScanner.tsx', evidence
    assert evidence['source_changed_at']['file'] == evidence['before']['file'], evidence
    print('PASS real Calibration: before line is the tab label in ReplayScanner.tsx, not the DocsLink.tsx slug')


def verify_real_dotted_config_docs_removed():
    entry = {'name': 'session_recording.inlineStylesheet', 'origin': str(attribution.REPOSITORIES['posthog-js'] / 'packages/types/src/posthog-config.ts')}
    evidence = attribution.gap_history_search('posthog-js', 'js-config', entry)
    assert evidence['category'] == 'docs_removed' and evidence['source_changed_at']['revision'].startswith('5ea8596'), evidence
    assert evidence['shipped_at']['revision'].startswith('66080ec'), evidence
    print('PASS real session_recording.inlineStylesheet: shipped in 66080ec across the TypeScript rewrite, docs_removed by 5ea8596')


def verify_real_link_swap_not_docs_removal():
    entry = {
        'name': 'feature-previews', 'title': 'Feature previews', 'section': 'user-feature-previews',
        'origin': str(attribution.REPOSITORIES['monorepo'] / attribution.SETTINGS_MAP),
    }
    evidence = attribution.gap_history_search('monorepo', 'settings', entry)
    assert not evidence['source_changed_at']['revision'].startswith('7a6269c35d'), evidence
    assert evidence['source_changed_at']['timestamp'] >= evidence['shipped_at']['timestamp'], evidence
    print(f"PASS real feature-previews: the 7a6269c35d panel-link swap is not a docs removal ({evidence['category']} via {evidence['source_changed_at']['revision'][:10]})")


def pickaxe_cases(range_start, head):
    label_claims = ['Save insight', 'save INSIGHT', 'Café crème', 'Save (beta)', 'EarlyThing', 'Straße', 'Click Save insight now']
    cases = []
    for claim in label_claims:
        parameters = attribution.source_search_parameters('monorepo', 'ui-label', claim)
        removal_arguments = ['--format=%x1e%H:%ct', '-p', '-U3', *parameters['removal_log']]
        cases.append(('monorepo', parameters['removal_alternatives'], removal_arguments, parameters['paths'], None, False, 'date'))
        cases.append(('monorepo', parameters['alternatives'], ['--format=%H:%ct', *parameters['log']], parameters['paths'], None, True, 'date'))
        cases.append(('monorepo', parameters['alternatives'], ['--format=%H:%ct', *parameters['log']], parameters['paths'], f'{range_start}..{head}', False, 'default'))
    phrases = ['later_option', 'Widget toggles', 'Controls whether widgets render', 'Click Save insight now']
    phrase_pattern = '|'.join('[[:space:]]+'.join(re.escape(word) for word in phrase.split()) for phrase in phrases)
    docs_arguments = ['--no-renames', '--format=%x1e%H:%ct', '-p', '-U100000', '-G', phrase_pattern, '--regexp-ignore-case']
    cases.append(('site', [history_index.literal_constraints(phrase) for phrase in phrases], docs_arguments, attribution.DOCS_HISTORY_PATHS, None, False, 'default'))
    url = attribution.source_search_parameters('monorepo', 'app-url', 'insights/new')
    cases.append(('monorepo', url['alternatives'], ['--format=%H:%ct', *url['log']], url['paths'], None, True, 'date'))
    cases.append(('monorepo', url['removal_alternatives'], ['--format=%x1e%H:%ct', '-p', '-U3', *url['removal_log']], url['paths'], None, False, 'date'))
    return cases


def path_history_cases():
    return [
        ('monorepo', ['frontend/src'], options) for options in (
            {}, {'include_merges': False}, {'include_merges': False, 'status': 'A', 'order': 'default'}, {'oldest_first': True, 'order': 'default'},
        )
    ] + [('site', ['contents/docs/widgets.mdx', 'vercel.json'], {'oldest_first': True})]


def assert_index_matches_plain_git(checkout, range_start, matched_cases):
    head = attribution.git(checkout, 'rev-parse', 'HEAD').strip()
    for case in pickaxe_cases(range_start, head):
        repository, alternatives, arguments, pathspecs, revision_range, oldest_first, order = case
        indexed = history_index.pickaxe_log(repository, checkout, alternatives, arguments, pathspecs, revision_range, oldest_first, order)
        plain = attribution.git(checkout, 'log', *history_index.plain_walk_options(revision_range, oldest_first, order), *arguments, '--', *pathspecs, timeout=None)
        assert indexed == plain, (case, indexed, plain)
        if plain:
            matched_cases.add(json.dumps(case[:4], ensure_ascii=False))
    for repository, pathspecs, options in path_history_cases():
        indexed = history_index.path_history(repository, checkout, pathspecs, **options)
        plain = history_index.path_history_from_git(repository, checkout, pathspecs, **options)
        assert indexed == plain and plain, (repository, pathspecs, options, indexed, plain)


def drop_cached_indexes(checkout):
    resolved = str(Path(checkout).resolve())
    for key in [key for key in history_index.INDEXES_BY_CHECKOUT if key[1] == resolved]:
        history_index.INDEXES_BY_CHECKOUT.pop(key)


def verify_index_matches_plain_git(root):
    checkout = initialize_repository(root / 'index-differential')
    main_branch = attribution.git(checkout, 'symbolic-ref', '--short', 'HEAD').strip()
    commit_file(checkout, 'frontend/src/a.tsx', '<button>Save insight</button>\n', '2025-01-01')
    range_start = commit_file(checkout, 'frontend/src/a.tsx', '<button>SAVE INSIGHT</button>\n<p>Café crème</p>\n', '2025-01-03')
    commit_file(checkout, 'frontend/src/b.tsx', 'export const label = "Save (beta)"\nupdateEarlyThingEnrollment()\n', '2025-01-02')
    commit_file(checkout, 'contents/docs/widgets.mdx', '- `later_option`: later.\n## Controls whether   widgets render\n', '2025-01-04')
    force_push_base = commit_file(checkout, 'frontend/src/scenes/urls.ts', "insightNew: '/insights/new',\n", '2025-01-05')
    commit_file(checkout, 'README.md', 'Save insight\n', '2025-01-06')
    subprocess.run(['git', '-C', str(checkout), 'checkout', '-q', '-b', 'side', force_push_base], check=True)
    commit_file(checkout, 'frontend/src/side.tsx', '<p>CAFÉ CRÈME</p>\n<p>Straße</p>\n', '2024-12-30')
    commit_file(checkout, 'contents/docs/side.mdx', 'Widget toggles here\nClick Save insight now\nClick Save   insight now\n', '2025-01-08')
    subprocess.run(['git', '-C', str(checkout), 'checkout', '-q', main_branch], check=True)
    commit_file(checkout, 'frontend/src/a.tsx', '<p>Café crème</p>\n', '2025-01-07')
    subprocess.run(['git', '-C', str(checkout), 'merge', '-q', '--no-ff', '--no-commit', 'side'], check=True, capture_output=True, env=fixture_environment('2025-01-09'))
    (checkout / 'frontend/src/merge.tsx').write_text('Save insight in merge\n')
    subprocess.run(['git', '-C', str(checkout), 'add', 'frontend/src/merge.tsx'], check=True)
    commit_staged(checkout, 'Merge side with an evil change', '2025-01-09')
    matched_cases = set()
    assert_index_matches_plain_git(checkout, range_start, matched_cases)

    (checkout / 'frontend/src/c.tsx').write_bytes(b'<p>Save insight \xff</p>\nClick save\tinsight now\nClick Save insight now\n')
    subprocess.run(['git', '-C', str(checkout), 'add', 'frontend/src/c.tsx'], check=True)
    commit_staged(checkout, 'Invalid UTF-8 label line', '2025-01-10')
    for compound_label in ('autoSave insight', 'Save insightful'):
        commit_file(checkout, 'frontend/src/compound.tsx', compound_label + '\n', '2025-01-10')
        commit_file(checkout, 'frontend/src/compound.tsx', 'Other text\n', '2025-01-10')
    commit_file(checkout, 'frontend/src/scenes/urls.ts', '', '2025-01-11')
    commit_file(checkout, 'contents/docs/widgets.mdx', 'STRASSE\n', '2025-01-12')
    assert_index_matches_plain_git(checkout, range_start, matched_cases)

    with patch.dict(history_index.REPOSITORIES, {'monorepo': checkout, 'site': checkout}), patch.object(history_index, 'INDEX_ROOT', root / 'differential-index'):
        drop_cached_indexes(checkout)
        assert_index_matches_plain_git(checkout, range_start, matched_cases)
        drop_cached_indexes(checkout)
        assert_index_matches_plain_git(checkout, range_start, matched_cases)
        index_file, _ = history_index.index_files('monorepo')
        index_file.write_bytes(index_file.read_bytes()[:-1])
        drop_cached_indexes(checkout)
        with contextlib.redirect_stderr(io.StringIO()) as unreadable:
            assert_index_matches_plain_git(checkout, range_start, matched_cases)
        assert 'monorepo: history index unreadable' in unreadable.getvalue(), unreadable.getvalue()

    subprocess.run(['git', '-C', str(checkout), 'reset', '-q', '--hard', force_push_base], check=True)
    commit_file(checkout, 'frontend/src/a.tsx', 'Save insight again\nupdateEarlyThingEnrollment()\n<b>EarlyThing</b>\n', '2025-01-13')
    pruned_head = commit_file(checkout, 'contents/docs/widgets.mdx', 'Controls whether widgets render\n', '2025-01-14')
    assert_index_matches_plain_git(checkout, range_start, matched_cases)

    subprocess.run(['git', '-C', str(checkout), 'reset', '-q', '--hard', force_push_base], check=True)
    commit_file(checkout, 'frontend/src/d.tsx', 'Save (beta)\nStraße\n', '2025-01-15')
    subprocess.run(['git', '-C', str(checkout), 'branch', '-q', '-D', 'side'], check=True)
    subprocess.run(['git', '-C', str(checkout), 'reflog', 'expire', '--expire=now', '--expire-unreachable=now', '--all'], check=True)
    subprocess.run(['git', '-C', str(checkout), 'gc', '-q', '--prune=now'], check=True)
    assert not history_index.is_commit_present(checkout, pruned_head)
    assert_index_matches_plain_git(checkout, range_start, matched_cases)

    expected_cases = {json.dumps(case[:4], ensure_ascii=False) for case in pickaxe_cases(range_start, 'HEAD')}
    assert matched_cases == expected_cases, expected_cases - matched_cases
    for outside_pathspec in ('README.md', ':(glob)*/a.tsx', 'src'):
        try:
            history_index.path_history('monorepo', checkout, [outside_pathspec])
            raise AssertionError(f'Pathspec {outside_pathspec!r} outside the indexed paths must fail')
        except ValueError as failure:
            assert 'outside the monorepo history index paths' in str(failure), failure
    print('PASS index-narrowed pickaxe and path history match plain git log after full, incremental, persisted, corrupt, force-pushed and pruned-head refreshes; outside pathspecs fail')


def verify_replay_rejects_unrecorded_query():
    with history_index.query_fixture('replay'):
        try:
            history_index.path_history('site', attribution.REPOSITORIES['site'], ['contents/never-recorded'])
            raise AssertionError('Replay must fail on a query the fixture never recorded')
        except history_index.MissingFixtureAnswer as failure:
            assert 'contents/never-recorded' in str(failure), failure
    print('PASS fixture replay fails on an unrecorded query instead of building a live index')


def main():
    with tempfile.TemporaryDirectory(prefix='attribution-test-') as directory:
        root = Path(directory)
        repositories, findings = verify_chronology(root)
        verify_cache_reuse(root, repositories, findings)
        verify_full_history_and_errors(root, repositories)
        verify_settings_move(root, repositories)
        verify_sdk_and_deleted_target(root, repositories)
        verify_moved_page(root, repositories)
        verify_unbounded_refresh(root, repositories)
        verify_caught_replay()
        verify_site_gate_and_legacy_sdk_layout_verdicts()
        verify_total_categories(root, repositories)
        verify_cache_revalidation(root, repositories)
        verify_docs_removed_needs_whole_identifier_after_shipping(root, repositories)
        verify_docs_link_written_route(root, repositories)
        verify_docs_link_redirect_chain_loses_middle_hop(root, repositories)
        verify_missing_blobs(root)
        verify_batched_label_search_missing_blobs(root)
        verify_partial_clone_page_move(root)
        verify_claim_write_needs_whole_label_word(root, repositories)
        verify_docs_removed_by_description_heading(root, repositories)
        verify_generated_only_label_agrees_with_checker(root)
        verify_json_and_property_template_labels_agree_with_checker(root)
        verify_non_utf8_blob_decodes_like_checker(root)
        verify_generated_live_route_in_history(root)
        verify_snippet_anchor_in_history(root)
        verify_code_import_at_write_is_undecidable(root)
        verify_code_import_removal_is_not_a_disappearance(root)
        verify_code_import_is_not_an_appearance(root)
        verify_code_import_removed_before_write_is_never_existed(root)
        verify_anchor_removed_behind_code_import_is_undecidable(root)
        verify_index_snapshot_survives_refresh(root)
        verify_index_matches_plain_git(root)
    verify_absent_when_written_evidence_rendering()
    with history_index.query_fixture('record' if '--record-history-fixture' in sys.argv else 'replay'):
        verify_real_history()
        verify_real_label_absent_when_written()
        verify_real_redirect_in_force()
        verify_real_sdk_property_removed_before_claim()
        verify_real_label_before_line()
        verify_real_dotted_config_docs_removed()
        verify_real_link_swap_not_docs_removal()
        verify_real_written_route_and_sdk_packages()
        verify_real_docs_removed()
    verify_replay_rejects_unrecorded_query()
    verify_real_output()
    verify_recorded_lines()
    print('PASS')


if __name__ == '__main__':
    main()
