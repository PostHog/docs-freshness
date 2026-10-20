import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import impact
from doccoverage import GITHUB_REPOSITORY_NAMES, TOOL_ROOT

REAL_RANGE_CASES = (
    ('posthog-js', 'fbfc84f56dda3cc4332cb8cecffe3da6ddfd5b32', '(coverage gap)', 'session_recording.trigger_pending_buffer_interval_millis'),
    ('site', 'd50d337b6307ba7736aec3e735b2f477d9f120ab', 'mcp-analytics/installation.mdx', '#typescript (same page)'),
)


def write_file(root, path, text):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def monorepo_fixture(root, section='project-first'):
    write_file(root, 'frontend/src/scenes/urls.ts', "export const urls = { old: '/old-route', kept: '/kept-route', settings: '/settings/:section' }\n")
    write_file(root, 'frontend/src/scenes/scenes.ts', 'export const redirects = {}\n')
    write_file(root, 'frontend/src/scenes/settings/types.ts', "export type SettingSectionId =\n    | 'project-first'\n    | 'project-second'\nexport const SettingLevelIds = ['project']\n")
    write_file(root, 'frontend/src/scenes/settings/settingsSceneLogic.ts', 'export const SETTINGS_SECTION_ALIASES = {\n}\n')
    write_file(root, impact.SETTINGS_MAP, f'''export const SettingsMap = [
    {{
        id: 'project-first',
        level: 'project',
        title: 'First',
        settings: [
            {{ id: 'kept-setting', title: 'Kept' }},
        ],
    }},
    {{
        id: '{section}',
        level: 'project',
        title: 'Section',
        settings: [
            {{ id: 'moved-setting', title: 'Move me' }},
        ],
    }},
]
''')
    write_file(root, 'frontend/src/scenes/widget.tsx', 'export const Widget = () => <button>Save insight</button>\n')


def reference(kind, claim, **attributes):
    return {'kind': kind, 'claim': claim, 'pages': ['synthetic/guide.mdx'], 'sources': [], **attributes}


@contextlib.contextmanager
def fake_revisions(base_root, head_root):
    roots = {'base': base_root, 'head': head_root}

    def paths_at_revision(checkout, revision):
        return [str(path.relative_to(roots[revision])) for path in roots[revision].rglob('*') if path.is_file()]

    @contextlib.contextmanager
    def snapshot_at_revision(checkout, revision, paths, use_worktree=False):
        yield roots[revision]

    with patch.object(impact, 'tree_paths', paths_at_revision), patch.object(impact, 'snapshot', snapshot_at_revision), patch.object(impact, 'head_label_paths', lambda checkout, revision, references: paths_at_revision(checkout, revision)):
        yield


def verify_monorepo(root):
    base_root, head_root = root / 'mono-base', root / 'mono-head'
    monorepo_fixture(base_root)
    monorepo_fixture(head_root, 'project-second')
    write_file(head_root, 'frontend/src/scenes/urls.ts', "export const urls = { kept: '/kept-route', settings: '/settings/:section' }\n")
    write_file(head_root, 'frontend/src/scenes/widget.tsx', 'export const Widget = () => <button>Store insight</button>\n')
    references = [
        reference('ui-label', 'Save insight'), reference('app-url', 'old-route'),
        reference('settings-anchor', 'settings/project-first#moved-setting'), reference('ui-label', 'Never existed'),
    ]
    touched_paths = ['frontend/src/scenes/widget.tsx', 'frontend/src/scenes/urls.ts', impact.SETTINGS_MAP]
    with fake_revisions(base_root, head_root):
        impacted = impact.monorepo_impact(base_root, 'base', 'head', touched_paths, references)
        assert {entry['kind'] for entry in impacted} == {'ui-label', 'app-url', 'settings-anchor'}, impacted
        assert all(entry['source_change']['line'] > 0 and entry['source_change']['revision'] == 'base' for entry in impacted)
        assert all('cited by docs/synthetic/guide.mdx' in entry['guard'] for entry in impacted)
        write_file(head_root, 'frontend/src/scenes/unmodified.tsx', 'export const Other = () => <button>Save insight</button>\n')
        retained = impact.monorepo_impact(base_root, 'base', 'head', touched_paths, references)
        assert 'ui-label' not in {entry['kind'] for entry in retained}, retained
        write_file(head_root, 'frontend/src/scenes/settings/types.ts', "export type SettingSectionId =\n    | 'project-second'\nexport const SettingLevelIds = ['project']\n")
        removed_section = impact.monorepo_impact(base_root, 'base', 'head', touched_paths, [reference('app-url', 'settings/project-first')])
        assert [entry['claim'] for entry in removed_section] == ['settings/project-first'], removed_section
    print('PASS synthetic label rename, label retained elsewhere, route removal, settings anchor moved')


def verify_sdk(root):
    base_root, head_root = root / 'sdk-base', root / 'sdk-head'
    for checkout in (base_root, head_root):
        write_file(checkout, 'packages/types/src/config.ts', 'export interface PostHogConfig {\n    existingOption?: boolean\n}\n')
        write_file(checkout, 'packages/browser/src/posthog-core.ts', 'export class PostHog {\n    capture() {}\n    removedMethod() {}\n}\n')
    write_file(head_root, 'packages/browser/src/posthog-core.ts', 'export class PostHog {\n    capture() {}\n    newUndocumentedMethod() {}\n}\n')
    write_file(head_root, 'packages/types/src/config.ts', 'export interface PostHogConfig {\n    existingOption?: boolean\n    newUndocumentedOption?: boolean\n}\n')
    write_file(base_root, 'packages/node/src/client.ts', 'export class PostHog {\n    capture() {}\n}\n')
    write_file(head_root, 'packages/node/src/client.ts', 'export class PostHog {\n    capture() {}\n    newUndocumentedNodeMethod() {}\n}\n')
    write_file(base_root, 'packages/browser/src/extensions/queue.ts', 'export class RequestQueue {\n    enqueue() {}\n}\n')
    write_file(head_root, 'packages/browser/src/extensions/queue.ts', 'export class RequestQueue {\n    enqueue() {}\n    drainInternalHelper() {}\n}\n')
    touched_paths = ['packages/browser/src/posthog-core.ts', 'packages/node/src/client.ts', 'packages/browser/src/extensions/queue.ts']
    with fake_revisions(base_root, head_root):
        impacted = impact.sdk_impact(base_root, 'posthog-js', 'base', 'head', touched_paths, [reference('sdk-method', 'js-browser: posthog.removedMethod', sdk='js-browser', identifier='removedMethod')])
    assert {entry['claim'] for entry in impacted} == {'js-browser: posthog.removedMethod', 'newUndocumentedMethod', 'newUndocumentedOption'}, impacted
    assert next(entry for entry in impacted if entry['kind'] == 'sdk-method')['source_change']['line'] == 3
    print('PASS synthetic SDK method removal and new undocumented config/method; internal and non-core classes ignored as coverage does')


def verify_python_sdk(root):
    base_root, head_root = root / 'python-base', root / 'python-head'
    for checkout in (base_root, head_root):
        write_file(checkout, 'posthog/__init__.py', 'def capture():\n    pass\n')
        write_file(checkout, 'posthog/client.py', 'class Client:\n    def __init__(self, api_key):\n        self.api_key = api_key\n\n    def removed_method(self):\n        pass\n')
    write_file(head_root, 'posthog/client.py', 'class Client:\n    def __init__(self, api_key):\n        self.api_key = api_key\n')
    with fake_revisions(base_root, head_root):
        impacted = impact.sdk_impact(base_root, 'posthog-python', 'base', 'head', ['posthog/client.py'], [reference('sdk-method', 'python: posthog.removed_method', sdk='python', identifier='removed_method')])
    assert [entry['claim'] for entry in impacted] == ['python: posthog.removed_method'], impacted
    for checkout in (base_root, head_root):
        write_file(checkout, 'posthog/__init__.py', 'from posthog.client import Client as Posthog\ndebug = False\nhost = None\n\ndef capture():\n    pass\n')
        write_file(checkout, 'posthog/flags.py', 'FLAG = 1\n')
    write_file(head_root, 'posthog/__init__.py', 'host = None\n\ndef capture():\n    pass\n')
    (head_root / 'posthog/flags.py').unlink()
    removed_names = ('debug', 'Posthog', 'api_key', 'flags')
    write_file(head_root, 'posthog/client.py', 'class Client:\n    def __init__(self, key):\n        self.key = key\n')
    references = [reference('sdk-method', f'python: posthog.{name}', sdk='python', identifier=name) for name in (*removed_names, 'host')]
    with fake_revisions(base_root, head_root):
        impacted = impact.sdk_impact(base_root, 'posthog-python', 'base', 'head', ['posthog/__init__.py', 'posthog/client.py', 'posthog/flags.py'], references)
    removals = {entry['claim']: entry['source_change'] for entry in impacted if entry['kind'] == 'sdk-method'}
    assert set(removals) == {f'python: posthog.{name}' for name in removed_names}, impacted
    assert removals['python: posthog.debug'] == {'file': 'posthog/__init__.py', 'line': 2, 'text': 'debug = False', 'revision': 'base'}, removals
    assert removals['python: posthog.flags']['file'] == 'posthog/flags.py', removals
    print('PASS synthetic Python SDK removal of method, module attribute, re-export, self attribute and submodule')


def verify_site(root):
    base_root, head_root = root / 'site-base', root / 'site-head'
    for checkout in (base_root, head_root):
        write_file(checkout, 'vercel.json', '{"redirects": []}')
        write_file(checkout, 'contents/docs/target.md', '# Existing heading\n')
        write_file(checkout, 'contents/docs/guide.md', '[Target](/docs/target#existing-heading)\n[Old broken](/docs/old-synthetic-missing)\n')
    write_file(head_root, 'contents/docs/guide.md', '[Target](/docs/target#synthetic-missing-heading)\n[Old broken](/docs/old-synthetic-missing)\n[New broken](/docs/new-synthetic-missing)\n')
    with fake_revisions(base_root, head_root):
        impacted = impact.site_impact(base_root, 'base', 'head', ['contents/docs/guide.md'], [])
    assert {entry['kind'] for entry in impacted} == {'docs-link', 'docs-fragment'}, impacted
    assert all('old-synthetic-missing' not in entry['claim'] for entry in impacted), impacted
    print('PASS changed site links and fragments; existing failures excluded')


def verify_site_app_routes(root):
    base_root, head_root = root / 'app-route-base', root / 'app-route-head'
    for checkout in (base_root, head_root):
        write_file(checkout, 'vercel.json', '{"redirects": []}')
        write_file(checkout, 'contents/docs/app.md', 'Open [old](https://us.posthog.com/synthetic-old-missing-route).\n')
    write_file(head_root, 'contents/docs/app.md', 'Open [old](https://us.posthog.com/synthetic-old-missing-route).\nOpen [new](https://us.posthog.com/synthetic-new-missing-route).\n')
    with fake_revisions(base_root, head_root):
        impacted = impact.site_impact(base_root, 'base', 'head', ['contents/docs/app.md'], [])
    assert [(entry['kind'], entry['claim']) for entry in impacted] == [('app-url', 'synthetic-new-missing-route')], impacted
    assert impacted[0]['source_change']['line'] == 2, impacted
    print('PASS the site gate re-checks app routes written in a changed page against the monorepo; existing failures excluded')


def site_reference(kind, claim, source, page):
    return {'kind': kind, 'claim': claim, 'pages': [page], 'sources': [{'repo': 'site', 'path': source}]}


def verify_site_inbound(root):
    base_root, head_root = root / 'inbound-base', root / 'inbound-head'
    for checkout in (base_root, head_root):
        write_file(checkout, 'vercel.json', '{"redirects": [{"source": "/docs/synthetic-redirected", "destination": "/docs/synthetic-heading-target"}]}')
        write_file(checkout, 'contents/docs/synthetic-heading-target.md', '# Kept heading\n\n## Synthetic old heading\n')
        write_file(checkout, 'contents/docs/synthetic-linker.md', '[Deleted](/docs/synthetic-deleted)\n[Heading](/docs/synthetic-heading-target#synthetic-old-heading)\n[Redirect](/docs/synthetic-redirected)\n[Landing](/docs/synthetic-landing)\n[Section](/docs/synthetic-section)\n')
    write_file(base_root, 'contents/docs/synthetic-deleted.md', '# Deleted page\n')
    write_file(base_root, 'src/pages/docs/synthetic-landing.tsx', 'export default function Landing() {}\n')
    write_file(base_root, 'src/pages/docs/synthetic-section/index.tsx', 'export default function Section() {}\n')
    write_file(head_root, 'contents/docs/synthetic-heading-target.md', '# Kept heading\n\n## Synthetic new heading\n')
    write_file(head_root, 'vercel.json', '{"redirects": []}')
    references = [
        site_reference('docs-link', '/docs/synthetic-deleted', 'contents/docs/synthetic-deleted.md', 'synthetic-linker.md'),
        site_reference('docs-fragment', '/docs/synthetic-heading-target#synthetic-old-heading', 'contents/docs/synthetic-heading-target.md', 'synthetic-linker.md'),
        site_reference('docs-link', '/docs/synthetic-redirected', 'contents/docs/synthetic-heading-target.md', 'synthetic-linker.md'),
        site_reference('docs-link', '/docs/synthetic-heading-target', 'contents/docs/synthetic-heading-target.md', 'synthetic-linker.md'),
        site_reference('docs-link', '/docs/synthetic-landing', 'src/pages/docs/synthetic-landing.tsx', 'synthetic-linker.md'),
        site_reference('docs-link', '/docs/synthetic-section', 'src/pages/docs/synthetic-section/index.tsx', 'synthetic-linker.md'),
    ]
    touched_paths = ['contents/docs/synthetic-deleted.md', 'contents/docs/synthetic-heading-target.md', 'vercel.json', 'src/pages/docs/synthetic-landing.tsx', 'src/pages/docs/synthetic-section/index.tsx']
    live_routes = impact.checker().LIVE_ROUTES | {'/docs/synthetic-deleted', '/docs/synthetic-landing', '/docs/synthetic-section'}
    with fake_revisions(base_root, head_root), patch.object(impact.checker(), 'LIVE_ROUTES', live_routes):
        impacted = impact.site_impact(base_root, 'base', 'head', touched_paths, references)
    changes = {entry['claim']: entry['source_change'] for entry in impacted}
    assert set(changes) == {'/docs/synthetic-deleted', '/docs/synthetic-heading-target#synthetic-old-heading', '/docs/synthetic-redirected', '/docs/synthetic-landing', '/docs/synthetic-section'}, impacted
    assert changes['/docs/synthetic-deleted']['file'] == 'contents/docs/synthetic-deleted.md', changes
    assert changes['/docs/synthetic-landing']['file'] == 'src/pages/docs/synthetic-landing.tsx', changes
    assert changes['/docs/synthetic-section']['file'] == 'src/pages/docs/synthetic-section/index.tsx', changes
    assert changes['/docs/synthetic-heading-target#synthetic-old-heading']['line'] == 3, changes
    assert all(entry['page'] == 'synthetic-linker.md' for entry in impacted), impacted
    print('PASS deleted live target page, deleted src/pages tsx pages, renamed heading and dropped redirect break untouched citing pages')


def run_git(checkout, *arguments):
    environment = {
        **os.environ, 'GIT_AUTHOR_NAME': 'Impact fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
        'GIT_COMMITTER_NAME': 'Impact fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
    }
    return subprocess.run(['git', '-C', str(checkout), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *arguments], check=True, capture_output=True, text=True, env=environment).stdout.strip()


def commit_all(checkout, message):
    run_git(checkout, 'add', '-A')
    run_git(checkout, 'commit', '-q', '-m', message)
    return run_git(checkout, 'rev-parse', 'HEAD')


def run_impact_main(repository, revision_range):
    output = io.StringIO()
    with patch('sys.argv', ['impact.py', '--repo', repository, revision_range]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        try:
            impact.main()
        except SystemExit as exit_status:
            return exit_status.code, output.getvalue()
    raise AssertionError('impact.main must exit with a status')


def verify_site_git(root):
    site = root / 'site-git'
    site.mkdir()
    run_git(site, 'init', '-q', '-b', 'master')
    write_file(site, 'vercel.json', '{"redirects": []}')
    write_file(site, 'contents/docs/synthetic-git-target.md', '# Target\n')
    write_file(site, 'contents/docs/synthetic-git-linker.md', '[Target](/docs/synthetic-git-target)\n[Later](/docs/synthetic-git-master-only)\n')
    branch_point = commit_all(site, 'initial')
    run_git(site, 'checkout', '-q', '-b', 'feature')
    (site / 'contents/docs/synthetic-git-target.md').unlink()
    feature_head = commit_all(site, 'delete linked target')
    run_git(site, 'checkout', '-q', 'master')
    write_file(site, 'contents/docs/synthetic-git-master-only.md', '# Added on master after the branch point\n')
    master_head = commit_all(site, 'master moves on')
    run_git(site, 'checkout', '-q', 'feature')
    write_file(site, 'contents/docs/synthetic-git-target.md', '# Restored only in the working tree\n')
    references = [
        site_reference('docs-link', '/docs/synthetic-git-target', 'contents/docs/synthetic-git-target.md', 'synthetic-git-linker.md'),
        site_reference('docs-link', '/docs/synthetic-git-master-only', 'contents/docs/synthetic-git-master-only.md', 'synthetic-git-linker.md'),
    ]
    with patch.dict(impact.REPOSITORIES, {'site': site}), patch.object(impact, 'load_references', return_value=references):
        exit_code, output = run_impact_main('site', f'{master_head}..{feature_head}')
        assert exit_code == 1, output
        assert '/docs/synthetic-git-target' in output and branch_point[:12] in output, output
        assert 'synthetic-git-master-only' not in output and output.count('cited by docs/') == 1, output
        with patch.object(impact, 'check_impact', side_effect=KeyError('synthetic crash')):
            exit_code, output = run_impact_main('site', f'{master_head}..{feature_head}')
        assert exit_code == 2, (exit_code, output)
    print('PASS real git site range: deleted linked page exits 1 against the merge-base despite a dirty worktree; crashes exit 2')


def verify_section_output(root):
    audit = impact.checker()
    context = audit.build_context()
    site_root = root / 'section-site'
    output_root = root / 'section-output'
    write_file(site_root, 'contents/docs/synthetic/page.md', '[Target](/docs/section-synthetic-missing)\n')
    with patch.object(audit, 'SITE', site_root), patch.object(audit, 'OUTPUT_ROOT', output_root), patch.object(audit, 'build_context', return_value=context), patch.object(audit, 'last_real_edit_timestamps', return_value={}), patch.object(audit, 'evidence_commit_history', return_value=[]), patch.object(audit.sys, 'argv', ['docaudit.py', 'synthetic']), contextlib.redirect_stdout(io.StringIO()):
        audit.main()
    findings = json.loads((output_root / 'audit-synthetic.json').read_text())[0]['findings']
    assert findings and all(set(finding) == {'kind', 'detail', 'suggestion', 'confidence'} for finding in findings), findings
    print('PASS per-section suggestion and confidence fields')


def verify_references(root):
    directory = root / 'reference-output'
    directory.mkdir()
    (directory / 'doc-references.json').write_text(json.dumps([reference('ui-label', 'Save insight')]))
    (directory / 'all-findings.txt').write_text('another/page.mdx\tui-label\tSave insight\t\tnone\nhistory/page.mdx\tui-label\tPreviously passing\t\tnone\n')
    references = impact.load_references(directory)
    save = next(entry for entry in references if entry['claim'] == 'Save insight')
    assert save['pages'] == ['another/page.mdx', 'synthetic/guide.mdx']
    assert any(entry['claim'] == 'Previously passing' for entry in references)
    try:
        impact.load_references(root / 'unpublished-output')
        raise AssertionError('Missing claims must not yield a passing impact check')
    except ValueError as failure:
        assert 'finish all_sections.py' in str(failure)
    print('PASS reverse-index aggregation, historical failure candidates and missing-input rejection')


def verify_attribution(root):
    from attribution_test import verify_chronology

    verify_chronology(root)


def assert_comment_links(stdout, repository, commit, page):
    assert 'Evidence: cited source https://github.com/PostHog/' in stdout, stdout
    assert f"change https://github.com/PostHog/{GITHUB_REPOSITORY_NAMES[repository]}/commit/{commit}" in stdout, stdout
    if page == '(coverage gap)':
        return
    assert 'Doc source: https://github.com/PostHog/posthog.com/blob/' in stdout and f'/contents/docs/{page}#L' in stdout, stdout
    assert 'Live: https://posthog.com/docs/' in stdout and ':~:text=' in stdout, stdout


def verify_real_ranges():
    for repository, commit, page, claim in REAL_RANGE_CASES:
        command = ['python3', 'impact.py', '--repo', repository, f'{commit}^..{commit}']
        completed = subprocess.run(command, cwd=TOOL_ROOT, capture_output=True, text=True, timeout=600)
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert f'{page}\t' in completed.stdout and claim in completed.stdout, completed.stdout
        assert_comment_links(completed.stdout, repository, commit, page)
        print(f'PASS real {repository} {commit[:12]}: {page} — {claim}')


def verify_history():
    history = json.loads((TOOL_ROOT / 'data/impact-history.json').read_text())
    assert len(history) >= 2
    for entry in history:
        command = ['python3', 'impact.py', '--repo', 'monorepo', f"{entry['commit']}^..{entry['commit']}"]
        completed = subprocess.run(command, cwd=TOOL_ROOT, capture_output=True, text=True, timeout=360)
        assert completed.returncode == 1, completed.stdout + completed.stderr
        assert entry['page'] in completed.stdout and entry['claim'] in completed.stdout, completed.stdout
        assert_comment_links(completed.stdout, 'monorepo', entry['commit'], entry['page'])
        print(f"PASS historical {entry['commit'][:12]}: {entry['page']} — {entry['claim']}")


def main():
    with tempfile.TemporaryDirectory(prefix='impact-test-') as directory:
        root = Path(directory)
        verify_monorepo(root)
        verify_sdk(root)
        verify_python_sdk(root)
        verify_site(root)
        verify_site_app_routes(root)
        verify_site_inbound(root)
        verify_site_git(root)
        verify_section_output(root)
        verify_references(root)
        verify_attribution(root)
    verify_real_ranges()
    verify_history()
    print('PASS')


if __name__ == '__main__':
    main()
