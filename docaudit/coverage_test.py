import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from collections import defaultdict

from doccoverage import (
    POSTHOG_PYTHON_ROOT, anchor_and_code_token_index, is_existing_site_route, is_publishable_identifier,
    js_changelog_inventory, reference_index, setting_mention_paths, structured_text, code_token_pattern, corpus_index,
    MONOREPO_ROOT, POSTHOG_JS_ROOT, SITE_ROOT, WORD_PATTERN, changelog_identifiers, interface_members, js_method_inventory,
    normalized_setting_title, onboarding_files_for_page, onboarding_text_for_page, run_coverage, settings_map_entries,
    TOOL_ROOT, classify_entry, format_summary, symbol_status, changelog_identifier_bucket, section_link_paths,
)

REAL_JS_ROOT = POSTHOG_JS_ROOT


def write_file(root, relative_path, content):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def verify_next_steps(root, partial_entry):
    import attribution
    from doccoverage import github_blob_url, text_fragment_url
    from next_steps import build_report, coverage_gaps, docs_page_url, format_markdown, read_findings

    previous_directory = root / 'previous-output'
    current_directory = root / 'current-output'
    previous_directory.mkdir()
    current_directory.mkdir()
    (previous_directory / 'all-findings.txt').write_text(
        'guide/index.mdx\tdocs-fragment\t#same\nremoved.md\tui-label\tRemoved label\n'
    )
    (current_directory / 'all-findings.txt').write_text(
        'guide/index.mdx\tdocs-fragment\t#same\t#correct\texact\t3\nindex.md\tui-label\tNew label\t\tnone\t1\n'
        'guide/index.mdx\tdocs-link\t/docs/old (missing: /docs/old)\t/docs/new\tcandidate\t4\n'
        'guide/_snippets/orphan.mdx\tdocs-fragment\t#orphan (same page)\t#orphaned\texact\t2\n'
    )
    locations = [
        {'page': page, 'kind': kind, 'detail': detail, 'claim': detail, 'line': line,
         'source_url': github_blob_url('site', 'c' * 40, f'contents/docs/{page}', line),
         'live_urls': [text_fragment_url(docs_page_url(page), 'heading', detail)]}
        for page, kind, detail, line in (
            ('guide/index.mdx', 'docs-fragment', '#same', 3), ('index.md', 'ui-label', 'New label', 1),
            ('guide/index.mdx', 'docs-link', '/docs/old (missing: /docs/old)', 4),
        )
    ]
    orphan_source_urls = [github_blob_url('site', 'c' * 40, 'contents/docs/guide/_snippets/orphan.mdx', line) for line in (2, 9)]
    locations.append({
        'page': 'guide/_snippets/orphan.mdx', 'kind': 'docs-fragment', 'detail': '#orphan (same page)', 'claim': 'guide/_snippets/orphan.mdx#orphan',
        'line': 2, 'lines': [2, 9], 'source_url': orphan_source_urls[0], 'source_urls': orphan_source_urls, 'live_urls': [],
        'is_unpublished_snippet': True,
    })
    (current_directory / 'finding-locations.json').write_text(json.dumps({'site_revision': 'c' * 40, 'findings': locations}))
    (previous_directory / 'coverage.json').write_text('{}')
    definition = {'revision': 'd' * 40, 'file': 'CHANGELOG.md', 'line': 7}
    (current_directory / 'coverage.json').write_text(json.dumps({
        'js-changelog': [
            {**partial_entry, 'gap_bucket': 'public-api', 'origin': str(POSTHOG_JS_ROOT / 'packages/browser/CHANGELOG.md'), 'definition': {**definition, 'repository': 'posthog-js'}},
            {
                'name': 'browser 1.2.0 #45', 'status': 'undocumented', 'identifiers': ['documentedOption'], 'gap_bucket': 'config-option',
                'origin': str(POSTHOG_JS_ROOT / 'packages/browser/CHANGELOG.md'), 'definition': {**definition, 'repository': 'posthog-js'},
            },
        ],
        'settings': [{
            'name': 'missing-setting', 'section': 'project-replay', 'status': 'undocumented', 'origin': str(MONOREPO_ROOT / attribution.SETTINGS_MAP),
            'definition': {**definition, 'repository': 'monorepo'},
        }],
    }))
    cache_file = root / 'next-steps-attribution.json'
    with patch.object(attribution, 'CACHE_FILE', cache_file):
        cache = attribution.load_cache()
        for finding in read_findings(current_directory):
            reference = attribution.finding_reference(finding)
            repository = attribution.source_repository(reference['kind'], reference['claim'])
            cache['sources'][attribution.source_cache_key(repository, reference)] = {
                'status': 'not_found', 'existed_in_history': False, 'reason': 'Absent throughout fixture history',
                'computed_at_head': attribution.repository_head(repository),
            }
            cache['claims'][attribution.claim_cache_key(finding, reference)] = {
                'status': 'found', 'rule': 'first_normalized_claim_in_page_lineage',
                'event': {'revision': 'a' * 40, 'timestamp': 1, 'date': '1970-01-01', 'file': 'contents/docs/' + finding['page']},
            }
        for source, statuses in coverage_gaps(json.loads((current_directory / 'coverage.json').read_text())).items():
            repository = 'posthog-js' if source.startswith('js-') else 'monorepo'
            for entries in statuses.values():
                for entry in entries:
                    event = {'revision': 'b' * 40, 'timestamp': 2, 'date': '1970-01-01', 'file': 'CHANGELOG.md'}
                    cache['gaps'][attribution.source_cache_key(repository, {'kind': source, 'claim': entry['name']})] = {
                        'repository': repository, 'category': 'shipped_undocumented', 'rule': 'first_source_identifier',
                        'evidence': [event], 'source_changed_at': event, 'shipped_at': event,
                        'computed_at_site_head': attribution.repository_head('site'),
                    }
        attribution.save_cache(cache)
        report = build_report(current_directory, previous_directory)
        report_without_previous = build_report(current_directory)
        stampless_gap = {'name': 'missing-setting', 'status': 'undocumented'}
        cache['gaps'][attribution.source_cache_key('monorepo', {'kind': 'settings', 'claim': 'missing-setting'})] = {'category': 'shipped_undocumented'}
        attribution.save_cache(cache)
        try:
            attribution.attribute_gaps({'settings': {'undocumented': [stampless_gap], 'partially_documented': []}})
            raise AssertionError('A stamp-less gap without an origin must fail attribution')
        except ValueError as failure:
            assert 'missing-setting' in str(failure) and 'no origin' in str(failure), failure
    assert report['delta'] == {'new': 3, 'resolved': 1, 'still_open': 1}, report['delta']
    assert [finding['suggestion'] for finding in report['fix_now']['guide/index.mdx']] == ['#correct']
    assert list(report['fix_now']) == ['guide/index.mdx'], report['fix_now']
    assert [finding['detail'] for finding in report['unpublished_snippets']['guide/_snippets/orphan.mdx']] == ['#orphan (same page)']
    assert [finding['confidence'] for finding in report['candidate_fixes']['guide/index.mdx']] == ['candidate']
    assert report['totals_by_confidence'] == {'exact': 2, 'candidate': 1, 'none': 1}, report['totals_by_confidence']
    assert report['needs_judgment']['ui-label']['index.md'][0]['detail'] == 'New label'
    assert list(report['resolved_since_last_run']) == ['removed.md']
    assert list(report['new_since_last_run']) == ['guide/_snippets/orphan.mdx', 'guide/index.mdx', 'index.md']
    assert report['coverage_gaps']['js-changelog']['partially_documented'][0]['missing_identifiers'] == ['missingOption']
    markdown = format_markdown(report)
    assert 'docs-fragment: #same -> #correct' in markdown
    fix_now_markdown = markdown.split('## Candidate fixes (check before applying)')[0]
    candidate_markdown = markdown.split('## Candidate fixes (check before applying)')[1].split('## Needs judgment')[0]
    assert '/docs/new' not in fix_now_markdown and 'docs-link: /docs/old (missing: /docs/old) -> /docs/new' in candidate_markdown
    assert '#orphaned' not in fix_now_markdown
    unpublished_markdown = markdown.split('## Unpublished snippets (fix optional)')[1].split('## Needs judgment')[0]
    assert 'docs-fragment: #orphan (same page) -> #orphaned' in unpublished_markdown, unpublished_markdown
    assert '[contents/docs/guide/_snippets/orphan.mdx#L2](' in unpublished_markdown and '[contents/docs/guide/_snippets/orphan.mdx#L9](' in unpublished_markdown
    assert 'missing-setting (section: project-replay)' in markdown
    assert 'https://posthog.com/docs/guide' in markdown
    assert f"Source: [contents/docs/guide/index.mdx#L3](https://github.com/PostHog/posthog.com/blob/{'c' * 40}/contents/docs/guide/index.mdx#L3)" in markdown, markdown
    assert 'Live: [/docs/guide#heading](https://posthog.com/docs/guide#heading:~:text=%23same)' in markdown, markdown
    assert f"definition: [CHANGELOG.md#L7 @ {'d' * 12}](https://github.com/PostHog/posthog/blob/{'d' * 40}/CHANGELOG.md#L7)" in markdown, markdown
    assert f"introduced by [{'b' * 12}](https://github.com/PostHog/posthog-js/commit/{'b' * 40}) 1970-01-01" in markdown, markdown
    assert '## Resolved since last run' in markdown and '## New since last run' in markdown
    assert docs_page_url('index.mdx') == 'https://posthog.com/docs'
    assert docs_page_url('settings/guide.md') == 'https://posthog.com/docs/settings/guide'
    assert 'delta' not in report_without_previous
    assert '## Resolved since last run' not in format_markdown(report_without_previous)
    assert json.loads(json.dumps(report)) == report
    assert report['attribution_totals']['shipped_undocumented'] == 2, report['attribution_totals']
    sdk_guard = next(guard for guard in report['upstream_guards'] if guard['name'] == 'SDK PR check')
    assert sdk_guard['by_category']['shipped_undocumented']['total'] == 1, sdk_guard
    assert report['changelog_gaps_outside_totals'] == {'config-option': {'shipped_undocumented': 1}, 'property': {}, 'type-or-internal': {}}, report['changelog_gaps_outside_totals']
    assert 'Changelog gaps outside these totals and every guard: config option (counted once, in the config gaps): shipped undocumented 1;' in markdown, markdown
    unbucketed_gap = {key: value for key, value in report['coverage_gaps']['js-changelog']['undocumented'][0].items() if key != 'gap_bucket'}
    unbucketed_gaps = {'js-changelog': {'undocumented': [unbucketed_gap], 'partially_documented': []}}
    for render in (lambda: format_markdown({**report, 'coverage_gaps': unbucketed_gaps}), lambda: attribution.guarded_items([], unbucketed_gaps)):
        try:
            render()
            raise AssertionError('A changelog gap without a gap_bucket must fail loudly')
        except ValueError as failure:
            assert 'browser 1.2.0 #45' in str(failure) and 'gap_bucket' in str(failure), failure


def verify_refresh_guard(root):
    network_fixture = root / 'network-fixture'
    network_fixture.mkdir()
    (network_fixture / 'sitecustomize.py').write_text(
        "import io\nimport urllib.request\n"
        "urllib.request.urlopen = lambda *arguments, **options: io.BytesIO(b'<urlset><url><loc>https://posthog.com/docs</loc></url></urlset>')\n"
    )
    git_fixture = network_fixture / 'git'
    git_fixture.write_text(
        '#!/bin/bash\n'
        'case "$*" in\n'
        '  *"rev-list --count"*) echo 0 ;;\n'
        '  *refs/remotes/origin/HEAD*|*"@{upstream}"*) echo origin/master ;;\n'
        '  *"symbolic-ref --short HEAD"*) echo master ;;\n'
        '  *"status --porcelain"*) exit 0 ;;\n'
        '  *) exec /usr/bin/git "$@" ;;\n'
        'esac\n'
    )
    git_fixture.chmod(0o755)
    routes_path = TOOL_ROOT / 'data/live-routes.txt'
    previous_routes = routes_path.read_bytes()
    findings_path = TOOL_ROOT / 'output/all-findings.txt'
    previous_findings = findings_path.read_bytes() if findings_path.exists() else None
    refresh_process = subprocess.run(
        ['bash', 'refresh.sh', '--no-pull', '--no-report'], cwd=TOOL_ROOT,
        env={**os.environ, 'PYTHONPATH': str(network_fixture), 'PATH': str(network_fixture) + os.pathsep + os.environ['PATH']}, capture_output=True, text=True,
    )
    assert refresh_process.returncode != 0, refresh_process.stdout
    assert 'fetched only 1 routes; old file retained' in refresh_process.stderr, refresh_process.stderr
    assert routes_path.read_bytes() == previous_routes
    assert (findings_path.read_bytes() if findings_path.exists() else None) == previous_findings


def main():
    with tempfile.TemporaryDirectory(prefix='doccoverage-') as temporary_directory:
        root = Path(temporary_directory)
        docs_root = root / 'contents'
        js_root = root / 'posthog-js'
        python_root = root / 'posthog-python'
        mono_root = root / 'mono'
        write_file(docs_root, 'docs/guide.md', '''| Option | Example |
| documentedOption | `session_recording: { maskAllInputs: true }` |
| metrics | `metrics: { flushInterval: 5000 }` and `capture_exceptions: { capture_unhandled_errors: true }` |

See #sample-setting. The network tab shows requests.
Turn on Titled setting option. Pick a Business model.
Enable `proseOnlyOption` when you need it.
''')
        write_file(docs_root, 'docs/replay.md', '`startSessionRecording({ sampling: true })`' + '\n' * 12 + 'Configure `session_recording: {}`.\n')
        write_file(docs_root, 'docs/settings/general.md', 'In Settings > Sample section you can also toggle Lonely.\n')
        write_file(docs_root, 'docs/settings/unrelated.md', 'More resource types coming soon, and Lonely is a word in Settings.\n')
        write_file(docs_root, 'docs/onboarded.mdx', "import { Widget } from 'onboarding/widget'\n\n<Widget />\n")
        write_file(mono_root, 'docs/onboarding/widget.tsx', "export const Widget = () => <p>Set onboardingOnlyOption to true</p>\n")
        write_file(docs_root, 'blog/launch.mdx', '`elsewhereOption` appeared here.\n')
        write_file(js_root, 'packages/types/src/posthog-config.ts', '''export interface SessionRecordingOptions {
    maskAllInputs?: boolean
    missingNested?: boolean
}
export interface MetricsConfig {
    flushInterval?: number
}
export interface BrowserMetricsConfig extends MetricsConfig {
    network?: boolean
}
export type ExceptionAutoCaptureConfig = {
    capture_unhandled_errors?: boolean
}
export interface SessionRecordingSamplingOptions {
    sampling?: boolean
}
export interface PostHogConfig {
    documentedOption?: boolean
    missingOption?: boolean
    elsewhereOption?: boolean
    /** @deprecated use documentedOption */
    deprecatedOption?: boolean
    __preview_x?: boolean
    referenceOption?: boolean
    session_recording?: SessionRecordingOptions & SessionRecordingSamplingOptions
    onboardingOnlyOption?: boolean
    proseOnlyOption?: boolean
    /** Only change this with PostHog support guidance. */
    supportOnlyOption?: boolean
    metrics?: BrowserMetricsConfig
    capture_exceptions?: boolean | ExceptionAutoCaptureConfig
}
''')
        write_file(js_root, 'packages/browser/src/posthog-core.ts', '''export class PostHog {
    public capture(): void {
        return this.config.documentedOption
    }
    public rateLimiter: RateLimiter
    private secret(): void {}
    _hidden(): void {}
}
''')
        write_file(js_root, 'packages/browser/src/features.ts', 'export function newUnmentionedFeature(): void {}\nexport function internalHelper(): void {}\n')
        write_file(js_root, 'packages/browser/src/index.ts', "export { newUnmentionedFeature } from './features'\n")
        write_file(js_root, 'packages/browser/CHANGELOG.md', '''## 1.2.0
### Minor Changes
- [#42](https://example.test/42) [`abc1234`](https://example.test/commit) Thanks someone! - Add `newUnmentionedFeature` to `posthog-js`.
- [#44](https://example.test/44) [`abc1235`](https://example.test/commit) Thanks someone! - Support `AsyncOpenAI` and `drawImage`.
- [#45](https://example.test/45) [`abc1236`](https://example.test/commit) Thanks someone! - Add `documentedOption` config.
### Patch Changes
- [#43](https://example.test/43) [`def1234`](https://example.test/commit) Thanks someone! - Fix `patchOnlyFeature`.
''')
        write_file(js_root, 'packages/browser/references/posthog-js-references-latest.json', '''{"id":"posthog-js","classes":[],"types":[{"id":"PostHogConfig","properties":[{"name":"referenceOption","type":"boolean"}]}]}''')
        write_file(python_root, 'posthog/client.py', '''class Client:
    def __init__(self, project_api_key, host=None, _private=None):
        pass
    def capture(self):
        pass
''')
        write_file(python_root, 'posthog/__init__.py', '''from posthog.features import pythonFeature


def identify():
    pass
''')
        write_file(python_root, 'posthog/features.py', '''def pythonFeature():
    pass
''')
        write_file(python_root, 'posthog/CHANGELOG.md', '''## 1.2.0 — 2026-01-01
### Minor changes
- [abc1234](https://example.test/commit) Added `pythonFeature`.
- [abc1235](https://example.test/commit) Handle `ImportError` in integrations.
### Patch changes
- [def1234](https://example.test/commit) Fixed `pythonPatch`.
''')
        write_file(mono_root, 'frontend/src/scenes/settings/SettingsMap.tsx', '''export const SETTINGS_MAP = [
    {
        level: 'project',
        id: 'sample-section',
        title: 'Sample section',
        settings: [
            {
                id: 'sample-setting',
                title: 'Sample setting',
            },
            {
                id: 'titled-setting',
                title: 'Titled setting option',
            },
            {
                id: 'lonely-setting',
                title: 'Lonely',
            },
            {
                id: 'unlinked-setting',
                title: 'Unmentioned knob',
            },
            {
                id: 'coming-soon-setting',
                title: 'Coming soon',
            },
            {
                id: 'business-model',
                title: 'Business model',
            },
            {
                id: 'flagged-setting',
                title: 'Flagged setting',
                flag: 'SOME_FEATURE',
            },
            {
                id: 'unflagged-setting',
                title: 'Documented elsewhere knob',
                flag: '!SOME_FEATURE',
                docsUrl: 'https://posthog.com/docs/guide',
            },
        ],
    },
]
''')
        results = run_coverage(docs_root, js_root, python_root, mono_root)
        options = {item['name']: item for item in results['js-config']}
        assert options['documentedOption']['status'] == 'documented'
        assert options['documentedOption']['hits'][0]['is_docs'] is True
        assert options['missingOption']['status'] == 'undocumented'
        assert options['elsewhereOption']['status'] == 'mentioned-elsewhere'
        assert options['elsewhereOption']['hits'][0]['is_docs'] is False
        assert options['referenceOption']['status'] == 'reference-only'
        assert options['referenceOption']['hits'][0]['path'].endswith('posthog-js-references-latest.json')
        assert options['session_recording.maskAllInputs']['status'] == 'documented'
        assert options['session_recording.missingNested']['status'] == 'undocumented'
        assert 'deprecatedOption' not in options
        assert '__preview_x' not in options
        js_changelog_rows = [row for row in results['js-changelog'] if row['status'] != 'duplicate-of-config']
        assert len(js_changelog_rows) == 1
        assert [row['identifiers'] for row in results['js-changelog'] if row['status'] == 'duplicate-of-config'] == [['documentedOption']]
        assert js_changelog_rows[0]['status'] == 'undocumented'
        assert options['onboardingOnlyOption']['status'] == 'documented'
        assert options['proseOnlyOption']['status'] == 'undocumented'
        assert 'supportOnlyOption' not in options
        assert options['session_recording.sampling']['status'] == 'undocumented'
        assert changelog_identifiers('Add `$ai_base_url` via `internalHelper`', {'newUnmentionedFeature'}) == ['$ai_base_url']
        assert normalized_setting_title('Default session timeout Beta') == 'session timeout'
        assert js_changelog_rows[0]['identifiers'] == ['newUnmentionedFeature']
        assert len(results['python-changelog']) == 1
        assert results['python-changelog'][0]['identifiers'] == ['pythonFeature']
        assert {item['name'] for item in results['js-methods']} == {'capture'}
        assert {item['name'] for item in results['python-config']} == {'project_api_key', 'host'}
        assert {item['name'] for item in results['python-methods']} == {'capture', 'identify'}
        settings = {item['name']: item for item in results['settings']}
        assert set(settings) >= {'sample-setting'} and 'sample-section' not in settings
        assert settings['sample-setting']['status'] == 'documented'
        assert settings['sample-setting']['title'] == 'Sample setting'
        assert settings['titled-setting']['status'] == 'documented'
        assert settings['lonely-setting']['status'] == 'documented'
        assert settings['unlinked-setting']['status'] == 'undocumented'
        assert settings['coming-soon-setting']['status'] == 'undocumented', settings['coming-soon-setting']
        assert [hit['path'] for hit in settings['lonely-setting']['hits']] == [str(docs_root / 'docs/settings/general.md')]
        assert [item['name'] for item in results['settings-sections']] == ['sample-section']
        assert settings['business-model']['status'] == 'undocumented'
        assert 'flagged-setting' not in settings
        assert [item['name'] for item in results['settings-gated']] == ['flagged-setting']
        assert settings['unflagged-setting']['status'] == 'documented'
        assert settings['unflagged-setting']['hits'][0]['path'] == '/docs/guide'
        assert options['metrics.network']['status'] == 'undocumented'
        assert options['metrics.flushInterval']['status'] == 'documented'
        assert options['capture_exceptions.capture_unhandled_errors']['status'] == 'documented'
        documents = {
            str(docs_root / 'docs/guide.md'): '`documentedOption` and `fullyDocumentedOption`',
            str(docs_root / 'blog/launch.mdx'): '`elsewhereOption`',
        }
        tokens_to_paths = defaultdict(set)
        for document_path, document_text in documents.items():
            for token in WORD_PATTERN.findall(document_text):
                tokens_to_paths[token].add(document_path)
        entry_cases = (
            (['documentedOption', 'fullyDocumentedOption'], 'documented'),
            (['documentedOption', 'missingOption'], 'partially-documented'),
            (['elsewhereOption', 'missingOption'], 'partially-documented'),
            (['missingOption', 'missingOtherOption'], 'undocumented'),
            (['documentedOption', 'elsewhereOption'], 'mentioned-elsewhere'),
            (['documentedOption', 'referenceOption'], 'documented'),
            ([], 'undocumented'),
        )
        for identifiers, expected_status in entry_cases:
            status, hits, identifier_statuses = classify_entry(
                {'identifiers': identifiers}, documents, tokens_to_paths,
                {'referenceOption': {'reference.json'}}, docs_root,
            )
            assert status == expected_status, (identifiers, status)
            assert set(identifier_statuses) == set(identifiers)
            assert bool(hits) == (expected_status != 'undocumented')
        partial_entry = {
            'name': 'browser 1.2.0 #46', 'status': 'partially-documented',
            'identifier_statuses': {'documentedOption': 'documented', 'missingOption': 'undocumented'},
        }
        partial_summary = format_summary({'js-changelog': [partial_entry]})
        assert '1 / 0 / 1 / 0 / 0 / 0' in partial_summary, partial_summary
        assert 'Partially documented (missing identifiers):' in partial_summary
        assert 'browser 1.2.0 #46 | missing: missingOption' in partial_summary
        unbucketed_gap = {'name': 'browser 1.2.0 #47', 'status': 'undocumented', 'identifiers': ['missingOption'], 'pr': '#47', 'commit': 'e' * 7, 'package': 'browser', 'version': '1.2.0', 'text': 'Add `missingOption`'}
        try:
            format_summary({'js-changelog': [unbucketed_gap]})
            raise AssertionError('An undocumented changelog gap without a gap_bucket must fail loudly in the summary')
        except ValueError as failure:
            assert 'browser 1.2.0 #47' in str(failure) and 'gap_bucket' in str(failure), failure
        verify_next_steps(root, partial_entry)
        verify_refresh_guard(root)
        filtered = run_coverage(docs_root, js_root, python_root, mono_root, sources=['js-changelog'], since_versions={'browser': '2.0.0-beta'})
        assert filtered['js-changelog'] == []
    real_docs_root = SITE_ROOT / 'contents'
    folk_page = real_docs_root / 'docs/cdp/sources/folk.md'
    folk_text = folk_page.read_text()
    folk_tokens = defaultdict(set)
    for token in WORD_PATTERN.findall(folk_text):
        folk_tokens[token].add(str(folk_page))
    assert 'reminders' in folk_tokens
    folk_status, _ = symbol_status(
        'reminders', {str(folk_page): folk_text}, folk_tokens, {}, real_docs_root, True, 'Reminders', 'user-reminders'
    )
    assert folk_status == 'undocumented', folk_status
    gated_by_name = {entry['name']: entry['is_gated'] for entry in settings_map_entries(MONOREPO_ROOT)}
    assert gated_by_name['js-snippet-version'] and gated_by_name['ai-subscription-codex'] and gated_by_name['environment-ai-subscriptions']
    onboarding_root = MONOREPO_ROOT / 'docs/onboarding'
    analytics_page = real_docs_root / 'docs/product-analytics/installation/react.mdx'
    analytics_files = onboarding_files_for_page(analytics_page, analytics_page.read_text(), onboarding_root)
    analytics_directory_files = [path for path in (onboarding_root / 'product-analytics').rglob('*') if path.suffix in {'.ts', '.tsx'}]
    assert analytics_files and len(analytics_files) < len(analytics_directory_files), (len(analytics_files), len(analytics_directory_files))
    assert onboarding_root / 'product-analytics/react.tsx' in analytics_files
    assert onboarding_root / 'product-analytics/index.ts' not in analytics_files
    real_entries = settings_map_entries(MONOREPO_ROOT)
    entry_by_name = {entry['name']: entry for entry in real_entries}
    for integration, docs_route in (('integration-github', '/docs/libraries/github'), ('integration-linear', '/docs/error-tracking/integrations'), ('integration-slack', '/docs/libraries/slack')):
        assert entry_by_name[integration]['docs_url'].endswith(docs_route), entry_by_name[integration]['docs_url']
        assert is_existing_site_route(docs_route, real_docs_root, frozenset())
    webhooks_page = str(real_docs_root / 'docs/cdp/sources/incoming-webhooks.mdx')
    webhooks_tokens = anchor_and_code_token_index({webhooks_page: Path(webhooks_page).read_text()})
    assert webhooks_page in setting_mention_paths('user-feature-previews', webhooks_tokens)
    assert entry_by_name['organization-startup-program']['is_gated'] and entry_by_name['organization-startup-program']['kind'] == 'section'
    assert entry_by_name['organization-danger-zone']['kind'] == 'section'
    assert entry_by_name['customization-irl']['is_gated']
    real_js_changelog = js_changelog_inventory(POSTHOG_JS_ROOT, {})
    real_changelog_names = [entry['name'] for entry in real_js_changelog]
    assert not [name for name in real_changelog_names if name.startswith('core 1.5.0 ') and '$bot_pageview' in name]
    assert not [name for name in real_changelog_names if '$bot_pageview' in name], real_changelog_names
    assert not [entry for entry in real_js_changelog if entry['text'].lower().startswith(('fix:', 'fix('))]
    assert not [name for name in real_changelog_names if name.startswith('browser 1.312.0 ')]
    assert not [name for name in real_changelog_names if '$sdk_debug' in name or 'detectBrowser' in name]
    assert not [entry for entry in real_js_changelog if entry['package'] == 'ai' and entry['version'] == '8.2.0']
    assert not is_publishable_identifier('detectBrowser', None) and not is_publishable_identifier('$pageview', None)
    usage_page = str(real_docs_root / 'docs/libraries/js/usage.mdx')
    usage_structured = structured_text(usage_page, Path(usage_page).read_text())
    assert 'request_batching' in Path(usage_page).read_text() and 'request_batching' not in usage_structured
    assert 'groupIdentifyImmediate' in reference_index(POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT)
    real_onboarding_page = SITE_ROOT / 'contents/docs/error-tracking/installation/react.mdx'
    assert 'Set up error boundaries' in onboarding_text_for_page(
        real_onboarding_page, real_onboarding_page.read_text(), MONOREPO_ROOT / 'docs/onboarding'
    )
    real_interfaces = interface_members(REAL_JS_ROOT / 'packages/types/src')
    assert real_interfaces['ExceptionAutoCaptureConfig'] and real_interfaces['DeadClicksAutoCaptureConfig']
    for derived, base in (('ErrorTrackingOptions', 'ExceptionRateLimiterConfig'), ('LogsConfig', 'LogCaptureOptions'), ('BrowserMetricsConfig', 'MetricsConfig')):
        derived_names = {name for name, _, _ in real_interfaces[derived]}
        assert {name for name, _, _ in real_interfaces[base]} <= derived_names, (derived, base)
    real_methods = {item['name'] for item in js_method_inventory(REAL_JS_ROOT)}
    assert not real_methods & {'rateLimiter', 'scrollManager', 'pageViewManager', 'requestRouter', 'sessionPersistence'}, real_methods
    assert {'capture', 'identify', 'getFeatureFlag'} <= real_methods
    assert 'getExtension' not in real_methods
    real_page_statuses = (
        ('docs/libraries/js/config.mdx', 'rageclick.click_count', 'documented'),
        ('docs/libraries/js/config.mdx', 'rageclick.timeout_ms', 'documented'),
        ('docs/libraries/js/config.mdx', 'rageclick.css_selector_ignorelist', 'documented'),
        ('docs/web-analytics/web-vitals.mdx', 'capture_performance.web_vitals_allowed_metrics', 'documented'),
        ('docs/web-analytics/web-vitals.mdx', 'capture_performance.web_vitals_delayed_flush_ms', 'documented'),
        ('docs/libraries/js/usage.mdx', 'session_recording.maskTextClass', 'documented'),
        ('docs/health-checks/web-vitals.mdx', 'capture_performance.web_vitals', 'documented'),
        ('docs/libraries/next-js/posthog-next.mdx', '$route', 'documented'),
        ('docs/error-tracking/link-releases/_snippets/release-id-verify.mdx', '$release_id', 'documented'),
        ('tutorials/time-on-page.md', '$prev_pageview_duration', 'mentioned-elsewhere'),
        ('docs/libraries/js/config.mdx', 'rageclick.web_vitals_allowed_metrics', 'undocumented'),
    )
    for real_page, identifier, expected_status in real_page_statuses:
        with tempfile.TemporaryDirectory(prefix='coverage-page-') as page_directory:
            page_root = Path(page_directory)
            write_file(page_root, real_page, (real_docs_root / real_page).read_text())
            page_documents, page_tokens, _, page_structured, page_structured_tokens = corpus_index(page_root)
            page_status, _ = symbol_status(
                identifier, page_documents, page_tokens, {}, page_root, structured_corpus=(page_structured, page_structured_tokens)
            )
        assert page_status == expected_status, (real_page, identifier, page_status)
    assert not code_token_pattern('os').search('`$os: name`')
    verify_real_settings_and_changelog_gaps(real_docs_root)
    print('PASS')


def verify_real_settings_and_changelog_gaps(real_docs_root):
    real_results = run_coverage(
        real_docs_root, POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT, MONOREPO_ROOT, sources=['js-config', 'settings', 'js-changelog', 'python-changelog'],
    )
    settings = {entry['name']: entry for entry in real_results['settings']}
    expected_setting_hits = {
        'feature-previews': 'docs/getting-started/enable-betas.mdx',
        'passkeys': 'docs/settings/account-settings.mdx',
        'csp-reporting': 'docs/csp-tracking/index.mdx',
        'revenue-base-currency': 'docs/revenue-analytics/start-here.mdx',
        'enforce-verified-domains': 'docs/settings/authentication/sso.mdx',
    }
    for setting_name, expected_page in expected_setting_hits.items():
        assert settings[setting_name]['status'] == 'documented', (setting_name, settings[setting_name]['status'])
    real_documents, _, _, _, _ = corpus_index(real_docs_root)
    paths_by_settings_section = anchor_and_code_token_index(real_documents)[2]
    feature_previews = settings['feature-previews']
    feature_preview_pages = section_link_paths(
        'feature-previews', feature_previews['title'], feature_previews['section'], feature_previews['section_links'], real_documents,
        paths_by_settings_section,
    )
    assert str(real_docs_root / expected_setting_hits['feature-previews']) in feature_preview_pages
    for setting_name in ('passkeys', 'csp-reporting', 'revenue-base-currency', 'enforce-verified-domains'):
        hit_paths = [hit['path'] for hit in settings[setting_name]['hits']]
        assert str(real_docs_root / expected_setting_hits[setting_name]) in hit_paths, (setting_name, hit_paths)
    assert settings['feature-previews-coming-soon']['status'] == 'mentioned-elsewhere', settings['feature-previews-coming-soon']
    assert settings['organization-mcp-access']['status'] == 'undocumented', settings['organization-mcp-access']
    js_changelog = [entry for entry in real_results['js-changelog'] if entry['status'] != 'duplicate-of-config']
    pull_requests = [entry['pr'] for entry in js_changelog]
    assert len(pull_requests) == len(set(pull_requests)), [pr for pr in pull_requests if pull_requests.count(pr) > 1]
    entry_by_pr = {entry['pr']: entry for entry in js_changelog}
    assert entry_by_pr['#4159']['packages'] == ['browser', 'core', 'node', 'types'], entry_by_pr['#4159']['packages']
    assert {release['package'] for release in entry_by_pr['#3921']['releases']} == {'browser', 'core', 'next', 'types', 'web'}
    assert entry_by_pr['#4464']['releases'] == [{'package': 'mcp', 'version': '0.11.2', 'origin': str(POSTHOG_JS_ROOT / 'packages/mcp/CHANGELOG.md')}]
    assert entry_by_pr['#4464']['identifier_statuses']['getMoreToolsResult'] == 'documented'
    expected_gap_buckets = {
        '#4159': 'property', '#3063': 'public-api', '#3499': 'public-api', '#4101': 'type-or-internal',
        '#4149': 'config-option', '#4320': 'config-option',
    }
    for pull_request, expected_bucket in expected_gap_buckets.items():
        assert entry_by_pr[pull_request]['gap_bucket'] == expected_bucket, (pull_request, entry_by_pr[pull_request].get('gap_bucket'))
    js_config_gaps = {entry['name'] for entry in real_results['js-config'] if entry['status'] == 'undocumented'}
    for pull_request in ('#4149', '#4320'):
        config_identifiers = [identifier for identifier, bucket in entry_by_pr[pull_request]['identifier_buckets'].items() if bucket == 'config-option']
        assert config_identifiers and all(
            any(gap == identifier or gap.endswith('.' + identifier) for gap in js_config_gaps) for identifier in config_identifiers
        ), (pull_request, config_identifiers)
    assert entry_by_pr['#4149']['identifier_buckets']['$dead_swipe'] == 'property'
    assert changelog_identifier_bucket('__internal_option', set(), set()) == 'type-or-internal'
    assert changelog_identifier_bucket('SessionTokenPayload', set(), {'decodeSessionId'}) == 'type-or-internal'
    python_buckets = {entry['commit']: entry['gap_bucket'] for entry in real_results['python-changelog'] if 'gap_bucket' in entry}
    assert python_buckets['556c134'] == 'property', python_buckets
    summary = format_summary(real_results)
    assert summary.startswith('Changelog rule: Major, Minor and Patch headings are all inventoried'), summary[:200]
    assert 'gaps by bucket: public API (undocumented feature)=' in summary


if __name__ == '__main__':
    main()
