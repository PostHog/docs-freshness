import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import docs_inventory

HUMAN = ('Ann Writer', 'ann@example.invalid')
AGENT = ('Claude', 'noreply@anthropic.com')
TEMPLATED_PAGE = 'import Steps from "components/Docs/Steps"\nimport {{ Tab }} from "components/Tab"\n\n## Setup\n\nSource {index} setup.\n\n## Sync\n\nSource {index} syncs.\n'
GUIDE_PAGE = '---\ntitle: Guide\n---\n\n## Install\n\nInstall the guide.\n\n## Use\n\nUse the guide.\n'
REWRITTEN_GUIDE_PAGE = '---\ntitle: Guide\n---\n\n## Install\n\nInstall everything differently.\n\n## Use\n\nUse it differently.\n'
SOURCES_NAV_HOOK = (
    "import { useStaticQuery, graphql } from 'gatsby'\n\n"
    "export default function useSourcesNav(basePath = '/docs/cdp/sources'): { url?: string }[] {\n"
    "    const { allPostHogSource } = useStaticQuery(graphql`query { allPostHogSource { nodes { slug } } }`)\n"
    "    const managed = allPostHogSource.nodes.map((node) => ({ url: `${basePath}/${node.slug}` }))\n"
    "    return [...managed, ...SELF_HOSTED_SOURCES.map((s) => ({ url: `${basePath}/${s.slug}` }))]\n}\n"
)
DATA_PIPELINES_NAV_HOOK = (
    "export default function useDataPipelinesNav({ type }: { type?: string }) {\n"
    "    return nodes.map((node) => ({ url: `/docs/cdp/${type}s/${node.slug}` }))\n}\n"
)
APP_CONTEXT = (
    "import useDataPipelinesNav from '../navs/useDataPipelinesNav'\nimport useSourcesNav from '../navs/useSourcesNav'\n"
    "const destinationNav = useDataPipelinesNav({ type: 'destination' })\n"
    "const cdpSourcesNav = useSourcesNav()\nconst dwSourcesNav = useSourcesNav('/docs/data-warehouse/sources')\n"
)


def run_git(checkout, *arguments, author=HUMAN, date='2024-01-01'):
    name, email = author
    environment = {
        **os.environ, 'GIT_AUTHOR_NAME': name, 'GIT_AUTHOR_EMAIL': email, 'GIT_COMMITTER_NAME': name, 'GIT_COMMITTER_EMAIL': email,
        'GIT_AUTHOR_DATE': f'{date}T12:00:00Z', 'GIT_COMMITTER_DATE': f'{date}T12:00:00Z',
    }
    return subprocess.run(
        ['git', '-C', str(checkout), '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *arguments],
        check=True, capture_output=True, text=True, env=environment,
    ).stdout


def commit_files(checkout, files_by_path, message, author=HUMAN, date='2024-01-01'):
    for path, text in files_by_path.items():
        destination = checkout / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    run_git(checkout, 'add', '--all')
    run_git(checkout, 'commit', '-q', '-m', message, author=author, date=date)


def initialize_repository(checkout):
    checkout.mkdir()
    run_git(checkout, 'init', '-q', '-b', 'master')


def make_site(site_root):
    initialize_repository(site_root)
    files_by_path = {
        'contents/docs/index.mdx': '---\ntitle: Docs\n---\n\nWelcome.\n',
        'contents/docs/guide.md': GUIDE_PAGE,
        'contents/docs/old-name.md': '---\ntitle: Shared Title\n---\n\nOld words.\n',
        'contents/docs/partial.mdx': "---\ntitle: '  shared title '\n---\n\nA partial.\n",
        'contents/docs/empty.md': '\n\n',
        'contents/docs/_snippets/hidden.md': 'Hidden snippet.\n',
        'contents/blog/post.md': 'Read [the guide](https://posthog.com/docs/guide#install) and [old](/docs/old-name.md).\nNot ours: https://github.com/example/docs/guide\n',
        'src/components/Card.tsx': 'export const href = "/docs/guide?tab=1"\n',
        'src/navs/index.js': "export default [{ url: '/docs/guide' }, { url: '/docs/sources/s1#setup' }]\n",
        'src/navs/activeMenu.test.ts': "assert(resolve('/docs/empty'), `${url} resolves to /docs/${section}`)\n",
        'src/navs/useSourcesNav.ts': SOURCES_NAV_HOOK,
        'src/navs/useDataPipelinesNav.ts': DATA_PIPELINES_NAV_HOOK,
        'src/context/App.tsx': APP_CONTEXT,
        'contents/docs/shared.md': 'Site words for the shared page.\n',
        'vercel.json': json.dumps({'redirects': [
            {'source': '/docs/moved:ext(\\.md)?', 'destination': '/docs/guide:ext?'},
            {'source': '/docs/legacy/:path*', 'destination': 'https://posthog.com/docs/guide/'},
            {'source': '/docs/empty', 'destination': '/docs', 'has': [{'type': 'query', 'key': 'x'}]},
            {'source': '/docs/partial', 'destination': '/docs/guide#use'},
        ]}),
        **{f'contents/docs/sources/s{index}.md': TEMPLATED_PAGE.format(index=index) for index in range(1, 6)},
        'contents/docs/legacy/thing.md': '## Thing\n\nLegacy thing.\n',
    }
    commit_files(site_root, files_by_path, 'Add docs', date='2024-01-01')
    commit_files(site_root, {'contents/docs/guide.md': GUIDE_PAGE + '\n## Agent section\n\nAdded by an agent.\n\nMore agent words.\n'},
                 'Extend guide\n\nCo-authored-by: Claude <noreply@anthropic.com>', date='2024-02-01')
    return site_root


def make_monorepo(monorepo_root):
    initialize_repository(monorepo_root)
    sources = docs_inventory.CONNECTOR_SOURCES_DIRECTORY
    files_by_path = {
        f'{sources}/alpha/source.py': 'class Alpha:\n    def config(self):\n        return SourceConfig(name="alpha", docsUrl="https://posthog.com/docs/guide")\n',
        f'{sources}/beta/constants.py': 'API_DOCS_URL = "https://docs.beta.example/api"\n',
        f'{sources}/beta/source.py': 'from .constants import API_DOCS_URL\n\nclass Beta:\n    def config(self):\n        return SourceConfig(name="beta", docsUrl=API_DOCS_URL)\n',
        f'{sources}/gamma/source.py': 'class Gamma:\n    def config(self):\n        return SourceConfig(name="gamma", unreleasedSource=True)\n',
        f'{sources}/delta/source.py': 'class Delta:\n    def config(self):\n        return SourceConfig(name="delta", docsUrl=None)\n',
        f'{sources}/epsilon/source.py': 'class Epsilon:\n    api_docs_url = "https://epsilon.example/docs"\n\n    def config(self):\n        return SourceConfig(name="epsilon", docsUrl=self.api_docs_url)\n',
        'frontend/src/Panel.tsx': 'const docs = "https://posthog.com/docs/guide#use"\nconst other = "https://posthog.com/docs/guide"\n',
        'frontend/README.md': 'See https://posthog.com/docs/guide\n',
        'frontend/__snapshots__/panel.snap': 'https://posthog.com/docs/guide\n',
        'docs/published/docs/logs/index.mdx': '---\ntitle: Logs\n---\n\nLogs overview.\n',
        'docs/published/docs/logs/setup.mdx': '---\ntitle: Logs setup\n---\n\nSet up logs.\n',
        'docs/published/docs/shared.mdx': 'Monorepo words for the shared page.\n',
        'docs/published/docs/_snippets/note.mdx': 'Snippet.\n',
        'docs/published/handbook/engineering/stack.md': '---\ntitle: Stack\n---\n\nOur stack.\n',
    }
    commit_files(monorepo_root, files_by_path, 'Add sources')
    commit_files(monorepo_root, {'docs/published/docs/logs/setup.mdx': '---\ntitle: Logs setup\n---\n\nSet up logs.\n\nAgent step one.\n\nAgent step two.\n\nAgent step three.\n\nAgent step four.\n'},
                 'Extend logs setup', author=AGENT, date='2024-02-01')
    run_git(monorepo_root, 'update-ref', 'refs/remotes/origin/master', 'HEAD~1')
    return monorepo_root


def write_live_routes(root):
    routes_path = root / 'live-routes.txt'
    routes = [
        '/docs', '/docs/guide', '/docs/old-name', '/docs/new-name', '/docs/empty', '/docs/legacy/thing/', *(f'/docs/sources/s{index}' for index in range(1, 6)),
        '/docs/logs', '/docs/logs/setup', '/docs/shared', '/docs/api/flags', '/docs/data-warehouse/sources/stripe',
        '/docs/data-warehouse/sources/stripe/advanced', '/docs/cdp/destinations/slack', '/docs/cdp/sources/hubspot', '/docs/product-analytics/learn/[...chapter]', '/blog/post',
    ]
    routes_path.write_text('\n'.join(routes) + '\n')
    return routes_path


def build(fixture, cache_name):
    return docs_inventory.build_inventory(
        fixture['site'], fixture['monorepo'], fixture['routes'], fixture['root'] / cache_name, fixture['root'] / f'monorepo-{cache_name}',
    )


def pages_by_path(inventory):
    return {page['path']: page for page in inventory['pages'] if page['path'] is not None}


def sourceless_pages_by_url(inventory):
    return {page['url']: page for page in inventory['pages'] if page['path'] is None}


def change_history_with_rename_revert_and_merge(site_root):
    run_git(site_root, 'mv', 'contents/docs/old-name.md', 'contents/docs/new-name.md')
    run_git(site_root, 'commit', '-q', '-m', 'Rename old-name', date='2024-03-01')
    commit_files(site_root, {'contents/docs/guide.md': REWRITTEN_GUIDE_PAGE}, 'Rewrite guide', date='2024-03-02')
    commit_files(site_root, {'contents/docs/guide.md': GUIDE_PAGE + '\n## Agent section\n\nAdded by an agent.\n\nMore agent words.\n'},
                 'Revert "Rewrite guide"', author=AGENT, date='2024-03-03')
    run_git(site_root, 'checkout', '-q', '-b', 'side')
    commit_files(site_root, {'contents/docs/sources/s1.md': TEMPLATED_PAGE.format(index=1) + '\nSide branch note.\n'}, 'Side note', date='2024-03-04')
    run_git(site_root, 'checkout', '-q', 'master')
    commit_files(site_root, {'contents/docs/index.mdx': '---\ntitle: Docs\n---\n\nWelcome back.\n'}, 'Index tweak', date='2024-03-05')
    run_git(site_root, 'merge', '-q', '--no-ff', '-m', 'Merge side', 'side', date='2024-03-06')


def verify_page_url_drops_contents_extension_index_and_case():
    assert docs_inventory.page_url('contents/docs/Product/Index.mdx') == '/docs/product/index'
    assert docs_inventory.page_url('contents/docs/product/index.mdx') == '/docs/product'
    assert docs_inventory.page_url('contents/docs/index.md') == '/docs'
    assert docs_inventory.page_url('contents/docs/a/B.md') == '/docs/a/b'


def verify_monorepo_page_url_strips_published_prefix_like_on_create_node():
    assert docs_inventory.monorepo_page_url('docs/published/docs/logs/AWS-Cloudwatch.mdx') == '/docs/logs/aws-cloudwatch'
    assert docs_inventory.monorepo_page_url('docs/published/docs/logs/index.mdx') == '/docs/logs'
    assert docs_inventory.monorepo_page_url('docs/published/handbook/engineering/stack.md') == '/handbook/engineering/stack'
    assert not docs_inventory.is_docs_route('/handbook/engineering/stack')
    assert not docs_inventory.is_docs_route('/docs-like')


def verify_underscore_segments_and_non_markdown_are_not_pages():
    assert not docs_inventory.is_docs_page_path('contents/docs/_snippets/a.md')
    assert not docs_inventory.is_docs_page_path('contents/docs/a/_partial.mdx')
    assert not docs_inventory.is_docs_page_path('contents/docs/a/image.png')
    assert docs_inventory.is_docs_page_path('contents/docs/a/b.mdx')


def verify_agent_commit_rules():
    is_agent = docs_inventory.is_agent_commit
    assert is_agent('Claude', 'claude@example.invalid', 'Fix')
    assert is_agent('Ann', 'ann@example.invalid', 'Fix\n\nCo-authored-by: Claude Opus <noreply@anthropic.com>')
    assert is_agent('github-actions[bot]', 'actions@github.com', 'Sync')
    assert is_agent('Cursor Agent', 'cursoragent@cursor.com', 'Fix')
    assert is_agent('Copilot', '198982749+Copilot@users.noreply.github.com', 'Fix')
    assert is_agent('Ann', 'ann@example.invalid', 'Fix\n\nCo-authored-by: codex <codex@example.invalid>')
    assert is_agent('Ann', 'ann@openai.com', 'Fix')
    assert is_agent('Ann', 'ann@example.invalid', 'Fix\n\n🤖 Generated with something')
    assert is_agent('Ann', 'ann@example.invalid', 'Fix\n\nGenerated with [Claude Code](https://claude.com/claude-code)')
    assert not is_agent('imgbot[bot]', 'imgbot@example.invalid', 'Compress images')
    assert not is_agent('dependabot[bot]', 'support@github.com', 'Bump')
    assert not is_agent('Ann Claude', 'ann@example.invalid', 'Fix')
    assert not is_agent('Ann', 'ann@example.invalid', 'Explain how Claude and Copilot differ')


def verify_co_author_trailers_parse_name_and_optional_email():
    message = (
        'Fix\n\nCo-authored-by:  Claude Opus  <noreply@anthropic.com>  \nco-authored-by: Ann\t\nCo-Authored-By: <bare@example.invalid>\n'
        'Co-authored-by: Open <unclosed\nCo-authored-by: Trailing <a@b> junk\n  Co-authored-by: Indented <c@d>\n'
    )
    assert docs_inventory.co_authors(message) == [('Claude Opus', 'noreply@anthropic.com'), ('Ann', ''), ('', 'bare@example.invalid')], docs_inventory.co_authors(message)


def verify_hostile_whitespace_parses_in_under_half_a_second():
    padding = ' \t' * 25_000
    hostile_inputs = {
        'co-author trailer': lambda: docs_inventory.is_agent_commit('Ann', 'ann@example.invalid', f'Fix\n\nCo-authored-by: Ann{padding}<unclosed{padding}x'),
        'second-level heading': lambda: docs_inventory.second_level_headings(f'## x{padding}y{padding}\n'),
        'frontmatter title': lambda: docs_inventory.frontmatter_title('a.md', f'---\ntitle: x{padding}y{padding}z\n---\n'),
        'title comment': lambda: docs_inventory.without_trailing_comment(f'x{padding}y'),
        'template literal': lambda: docs_inventory.template_literals('`${a}' + padding + '${b}' * 10_000),
    }
    for parser_name, parse in hostile_inputs.items():
        started_at = time.monotonic()
        parse()
        assert time.monotonic() - started_at < 0.5, parser_name


def verify_headings_titles_and_template_literals_keep_regex_semantics():
    assert docs_inventory.second_level_headings('## One  \n###  Three\n##  \n## \n##\tTab\r\n##x\n') == ['One', ' ', 'Tab\r']
    assert docs_inventory.title_value('sidebar: Docs\ntitle :  Hello  \r\ntitleFoo: no\n') == 'Hello'
    assert docs_inventory.without_trailing_comment('A#B # comment') == 'A#B'
    assert docs_inventory.template_literals('`a`${c}` x `${d}`') == ['${c}', '${d}']


def verify_doc_kind_thresholds():
    assert docs_inventory.doc_kind(True, 0, 10) == 'templated spec'
    assert docs_inventory.doc_kind(False, 0, 0) == 'empty page'
    assert docs_inventory.doc_kind(False, 5, 10) == 'agent-written prose'
    assert docs_inventory.doc_kind(False, 2, 10) == 'mixed prose'
    assert docs_inventory.doc_kind(False, 1, 10) == 'human-written prose'


def verify_fingerprint_ignores_fenced_code_and_unstructured_pages_have_none():
    fenced = 'import A from "x/A"\n\n## One\n\n```\n## Not a heading\nimport B from "x/B"\n```\n'
    assert docs_inventory.structural_fingerprint(fenced) == (('A',), ('one',))
    assert docs_inventory.structural_fingerprint('Just prose.\n\n### Third level\n') is None


def verify_import_sources_match_from_clauses_only():
    text = 'import A from "x/A"\nimport {\n  B,\n  C\n} from \'y/B\'\nimport   from "z"\nimport A from"q"\nimportA from "q"\nimport Afrom "q"\n  import D from "d"\n'
    assert docs_inventory.import_sources(text) == ['x/A', 'y/B', 'z'], docs_inventory.import_sources(text)


def verify_import_sources_stay_fast_after_fifty_thousand_whitespace_characters():
    for padding in (' ' * 50_000, '\n' * 50_000, ' \n\t' * 16_667):
        started_at = time.monotonic()
        assert docs_inventory.import_sources(f'import{padding}x') == []
        assert docs_inventory.structural_fingerprint(f'import{padding}from "slow/Path"\n## Heading\n') == (('Path',), ('heading',))
        assert time.monotonic() - started_at < 0.5, repr(padding[:3])


def verify_frontmatter_title_forms():
    title = docs_inventory.frontmatter_title
    assert title('a.md', '---\ntitle: Plain # comment\n---\n') == 'Plain'
    assert title('a.md', '---\ntitle: "Quoted \\"x\\""\n---\n') == 'Quoted "x"'
    assert title('a.md', "---\ntitle: 'It''s'\n---\n") == "It's"
    assert title('a.md', 'No frontmatter\n') is None
    assert title('a.md', '---\nsidebar: Docs\n---\n') is None
    for unreadable in ('---\ntitle: >\n  folded\n---\n', '---\ntitle: "open\n---\n', '---\ntitle:\n---\n'):
        try:
            title('a.md', unreadable)
            raise AssertionError(f'Undecidable title accepted: {unreadable!r}')
        except ValueError as failure:
            assert 'a.md: frontmatter title' in str(failure), failure


def verify_redirect_sources_expand_extension_wildcard_and_choice_patterns():
    redirects = docs_inventory.Redirects(json.dumps({'redirects': [
        {'source': '/docs/A:ext(\\.md)?', 'destination': '/docs/b:ext?'},
        {'source': '/docs/old/:path*', 'destination': 'https://posthog.com/docs/b/'},
        {'source': '/docs/integrate/(client|server)/:path*', 'destination': '/docs/b?tab=1'},
        {'source': '/docs/api/environments-:n(\\d+)', 'destination': 'https://example.com/docs/b'},
        {'source': '/docs/conditional', 'destination': '/docs/b', 'has': [{'type': 'query', 'key': 'x'}]},
    ]}))
    assert redirects.is_redirected_away('/docs/a')
    assert redirects.is_redirected_away('/docs/old')
    assert redirects.is_redirected_away('/docs/old/deep/page')
    assert not redirects.is_redirected_away('/docs/older')
    assert redirects.is_redirected_away('/docs/integrate/server/node')
    assert redirects.is_redirected_away('/docs/api/environments-42')
    assert not redirects.is_redirected_away('/docs/conditional')
    assert redirects.redirects_into('/docs/b') == 4


def verify_unsupported_redirect_source_raises():
    try:
        docs_inventory.Redirects(json.dumps({'redirects': [{'source': '/docs/*', 'destination': '/docs'}]}))
        raise AssertionError('Unsupported source pattern accepted')
    except ValueError as failure:
        assert "'/docs/*'" in str(failure), failure


def verify_site_links_need_posthog_origin_or_site_relative_path():
    linking_files = docs_inventory.linking_files_by_route({
        'a.md': '[x](/docs/One.md#top) https://www.posthog.com/docs/two/ posthog.com/docs/three.',
        'b.md': 'https://example.com/docs/one us.posthog.com/docs/one contents/docs/one /docs-like',
    })
    assert dict(linking_files) == {'/docs/one': {'a.md'}, '/docs/two': {'a.md'}, '/docs/three': {'a.md'}}, linking_files


def verify_nav_files_are_top_level_src_navs_files_other_than_tests():
    assert docs_inventory.is_nav_file('src/navs/index.js')
    assert not docs_inventory.is_nav_file('src/navs/activeMenu.test.ts')
    assert not docs_inventory.is_nav_file('src/navs/nested/index.js')
    assert not docs_inventory.is_nav_file('src/components/Nav.tsx')


def verify_nav_routes_match_whole_quoted_routes_only():
    routes = docs_inventory.nav_routes(["{ url: '/docs/a/b#x' }, \"https://posthog.com/docs/c\", `/docs/d/`, `/docs/cdp/${type}s/${node.slug}`"])
    assert routes == {'/docs/a/b', '/docs/c', '/docs/d'}, routes


def verify_nav_hooks_put_every_live_route_one_segment_below_each_called_base_path_in_nav():
    nav_texts = {docs_inventory.SOURCES_NAV_HOOK_FILE: SOURCES_NAV_HOOK, docs_inventory.DATA_PIPELINES_NAV_HOOK_FILE: DATA_PIPELINES_NAV_HOOK}
    live_routes = {
        '/docs/cdp/sources/hubspot', '/docs/data-warehouse/sources/stripe', '/docs/data-warehouse/sources/stripe/advanced',
        '/docs/cdp/destinations/slack', '/docs/cdp/transformations/geoip', '/docs/data-warehouse/sources',
    }
    routes = docs_inventory.nav_hook_routes(nav_texts, {'src/context/App.tsx': APP_CONTEXT}, live_routes)
    assert routes == {'/docs/cdp/sources/hubspot', '/docs/data-warehouse/sources/stripe', '/docs/cdp/destinations/slack'}, routes


def verify_unresolved_nav_url_template_raises():
    nav_texts = {'src/navs/useTopicsNav.js': 'const a = `/questions/topic/${slug}`\nconst b = `/docs/${section}/${slug}`\n'}
    try:
        docs_inventory.nav_hook_routes(nav_texts, {}, {'/docs/a/b'})
        raise AssertionError('Unresolved nav url template accepted')
    except ValueError as failure:
        assert 'src/navs/useTopicsNav.js' in str(failure) and '/docs/${section}/${slug}' in str(failure), failure
        assert '/questions/topic/' not in str(failure), failure


def verify_nav_hook_call_with_non_literal_argument_raises():
    nav_texts = {docs_inventory.SOURCES_NAV_HOOK_FILE: SOURCES_NAV_HOOK}
    for caller_text, hook_name in (('useSourcesNav(basePathFor(product))', 'useSourcesNav'), ('useDataPipelinesNav({ type })', 'useDataPipelinesNav')):
        try:
            docs_inventory.nav_hook_routes(nav_texts, {'src/components/Caller.tsx': caller_text}, {'/docs/a/b'})
            raise AssertionError(f'Non-literal {hook_name} argument accepted')
        except ValueError as failure:
            assert 'src/components/Caller.tsx' in str(failure) and hook_name in str(failure), failure


def verify_sitemap_template_segments_are_recognized_and_raise_in_source_urls():
    for template_route in ('/docs/a/learn/[...chapter]', '/docs/a/[slug]', '/docs/a/:slug', '/docs/a/*'):
        assert docs_inventory.is_sitemap_template_route(template_route), template_route
    assert not docs_inventory.is_sitemap_template_route('/docs/api/ai%20observability-2')
    try:
        docs_inventory.require_no_template_segments_in_source_urls(['/docs/a', '/docs/a/[slug]'])
        raise AssertionError('Source url with a template segment accepted')
    except ValueError as failure:
        assert "['/docs/a/[slug]']" in str(failure), failure


def verify_live_docs_route_without_row_raises():
    try:
        docs_inventory.require_one_row_per_live_docs_route({'/docs/a', '/docs/b'}, [{'url': '/docs/a', 'page_class': 'live-route'}])
        raise AssertionError('Live route without a row accepted')
    except ValueError as failure:
        assert "['/docs/b']" in str(failure), failure
    try:
        docs_inventory.require_one_row_per_live_docs_route({'/docs/a'}, [{'url': '/docs/a', 'page_class': 'live-route'}, {'url': '/docs/a', 'page_class': 'generated-route'}])
        raise AssertionError('Live route with two uncontested rows accepted')
    except ValueError as failure:
        assert "['/docs/a']" in str(failure), failure


def verify_blame_commit_missing_from_head_history_raises():
    history = {'creation_commit': 'a' * 40, 'nonblank_lines_by_commit': {'b' * 40: 3}}
    try:
        docs_inventory.line_shares('contents/docs/x.md', history, {'a' * 40: {'authored_date': '2024-01-01', 'is_agent': False}})
        raise AssertionError('Blame commit outside HEAD history accepted')
    except ValueError as failure:
        assert 'contents/docs/x.md' in str(failure) and 'b' * 40 in str(failure), failure


def verify_unresolvable_connector_docs_url_raises():
    module_reader = docs_inventory.ModuleReader('/nonexistent', 'HEAD')
    try:
        docs_inventory.connector_record('sources/x/source.py', 'class X:\n    def c(self):\n        return SourceConfig(docsUrl=base + "/x")\n', module_reader)
        raise AssertionError('Unresolvable docsUrl accepted')
    except ValueError as failure:
        assert 'sources/x/source.py' in str(failure) and 'base + ' in str(failure), failure


def verify_splatted_source_config_without_docs_url_raises():
    module_reader = docs_inventory.ModuleReader('/nonexistent', 'HEAD')
    try:
        docs_inventory.connector_record('sources/x/source.py', 'class X:\n    def c(self):\n        return SourceConfig(name="x", **shared_options)\n', module_reader)
        raise AssertionError('Splatted SourceConfig without docsUrl accepted')
    except ValueError as failure:
        assert 'sources/x/source.py' in str(failure) and '**shared_options' in str(failure), failure


def verify_page_facts(fixture):
    inventory, reblamed_paths = build(fixture, 'facts-cache.json')
    pages = pages_by_path(inventory)
    assert sorted(pages) == sorted(reblamed_paths)
    assert 'contents/docs/_snippets/hidden.md' not in pages
    guide = pages['contents/docs/guide.md']
    assert guide == {
        'url': '/docs/guide', 'source_repo': 'posthog.com', 'path': 'contents/docs/guide.md', 'title': 'Guide', 'page_class': 'live-route', 'doc_kind': 'mixed prose',
        'is_route': True, 'redirected_away': False, 'created_at': '2024-01-01', 'agent_line_share': 0.3, 'nonblank_lines': 10,
        'template_family_size': 1, 'site_inbound_links': 2, 'in_nav': True, 'redirects_into': 3, 'app_links': 2,
    }, guide
    assert set(guide) == set(inventory['page_columns'])
    assert pages['contents/docs/partial.mdx']['page_class'] == 'not-a-route'
    assert pages['contents/docs/partial.mdx']['redirected_away'] is True
    assert pages['contents/docs/legacy/thing.md']['page_class'] == 'redirected-away'
    assert pages['contents/docs/empty.md']['doc_kind'] == 'empty page'
    assert pages['contents/docs/empty.md']['redirected_away'] is False
    assert pages['contents/docs/old-name.md']['site_inbound_links'] == 1
    assert pages['contents/docs/sources/s1.md']['in_nav'] is True
    assert pages['contents/docs/sources/s1.md']['site_inbound_links'] == 0
    assert {pages[f'contents/docs/sources/s{index}.md']['doc_kind'] for index in range(1, 6)} == {'templated spec'}
    assert pages['contents/docs/sources/s3.md']['template_family_size'] == 5
    assert 'docs/published/docs/_snippets/note.mdx' not in pages
    assert 'docs/published/handbook/engineering/stack.md' not in pages
    logs_setup = pages['docs/published/docs/logs/setup.mdx']
    assert (logs_setup['url'], logs_setup['source_repo'], logs_setup['title'], logs_setup['page_class']) == ('/docs/logs/setup', 'posthog', 'Logs setup', 'live-route'), logs_setup
    assert (logs_setup['doc_kind'], logs_setup['agent_line_share'], logs_setup['created_at']) == ('agent-written prose', 0.5, '2024-01-01'), logs_setup
    assert pages['contents/docs/empty.md']['in_nav'] is False
    assert pages['docs/published/docs/logs/index.mdx']['url'] == '/docs/logs'
    assert {pages[path]['page_class'] for path in ('contents/docs/shared.md', 'docs/published/docs/shared.mdx')} == {'contested-route'}
    sourceless_pages = sourceless_pages_by_url(inventory)
    assert sorted(sourceless_pages) == [
        '/docs/api/flags', '/docs/cdp/destinations/slack', '/docs/cdp/sources/hubspot', '/docs/data-warehouse/sources/stripe',
        '/docs/data-warehouse/sources/stripe/advanced', '/docs/new-name', '/docs/product-analytics/learn/[...chapter]',
    ], sorted(sourceless_pages)
    assert sourceless_pages['/docs/data-warehouse/sources/stripe'] == {
        'url': '/docs/data-warehouse/sources/stripe', 'source_repo': None, 'path': None, 'title': None, 'page_class': 'generated-route',
        'doc_kind': 'generated page', 'is_route': True, 'redirected_away': False, 'created_at': None, 'agent_line_share': None,
        'nonblank_lines': None, 'template_family_size': None, 'site_inbound_links': 0, 'in_nav': True, 'redirects_into': 0, 'app_links': 0,
    }, sourceless_pages['/docs/data-warehouse/sources/stripe']
    assert {url: page['in_nav'] for url, page in sourceless_pages.items() if url.startswith(('/docs/cdp', '/docs/data-warehouse'))} == {
        '/docs/cdp/destinations/slack': True, '/docs/cdp/sources/hubspot': True, '/docs/data-warehouse/sources/stripe': True,
        '/docs/data-warehouse/sources/stripe/advanced': False,
    }
    template_page = sourceless_pages['/docs/product-analytics/learn/[...chapter]']
    assert (template_page['page_class'], template_page['doc_kind']) == ('sitemap-template-route', 'not a page'), template_page
    assert inventory['sitemap_template_routes'] == ['/docs/product-analytics/learn/[...chapter]']
    assert inventory['counts'] == {
        'pages_total': 22,
        'by_doc_kind': {
            'templated spec': 5, 'agent-written prose': 1, 'mixed prose': 1, 'human-written prose': 7, 'empty page': 1, 'generated page': 6,
            'not a page': 1,
        },
        'by_page_class': {'live-route': 11, 'redirected-away': 1, 'not-a-route': 1, 'contested-route': 2, 'generated-route': 6, 'sitemap-template-route': 1},
        'redirected_away': 2, 'template_families': 1,
    }, inventory['counts']
    routed_docs_rows = [page for page in inventory['pages'] if docs_inventory.is_docs_route(page['url']) and page['page_class'] != 'not-a-route']
    live_docs_route_count = sum(1 for route in fixture['routes'].read_text().split() if docs_inventory.is_docs_route(route))
    contested_extra_rows = inventory['counts']['by_page_class']['contested-route'] // 2
    assert len(routed_docs_rows) - contested_extra_rows == live_docs_route_count == 20, (len(routed_docs_rows), live_docs_route_count)
    assert inventory['duplicate_title_groups'] == [{'title': 'shared title', 'urls': ['/docs/old-name', '/docs/partial']}], inventory['duplicate_title_groups']
    assert inventory['connectors'] == {
        'source_files': 5,
        'by_bucket': {'posthog-docs': 1, 'docs-url-elsewhere': 2, 'docs-url-none': 1, 'no-docs-url': 1},
        'docs_url_elsewhere': [{'connector': 'beta', 'docs_url': 'https://docs.beta.example/api'}, {'connector': 'epsilon', 'docs_url': 'https://epsilon.example/docs'}],
        'docs_url_none': ['delta'],
        'no_docs_url': ['gamma'],
    }, inventory['connectors']
    assert inventory['site_commit'] == run_git(fixture['site'], 'rev-parse', 'HEAD').strip()
    assert inventory['monorepo_commit'] == run_git(fixture['monorepo'], 'rev-parse', 'HEAD').strip() != run_git(fixture['monorepo'], 'rev-parse', 'origin/master').strip()


def verify_warm_cache_matches_cold_run_after_rename_revert_and_merge(fixture):
    cached_inventory, _ = build(fixture, 'warm-cache.json')
    guide_share_before_revert = pages_by_path(cached_inventory)['contents/docs/guide.md']['agent_line_share']
    created_at_before_edits = {path: page['created_at'] for path, page in pages_by_path(cached_inventory).items()}
    change_history_with_rename_revert_and_merge(fixture['site'])
    recomputed_creation_paths = []
    compute_creation_commit = docs_inventory.creation_commit

    def recording_creation_commit(checkout, revision, path, **options):
        recomputed_creation_paths.append(path)
        return compute_creation_commit(checkout, revision, path, **options)

    docs_inventory.creation_commit = recording_creation_commit
    try:
        warm_inventory, reblamed_paths = build(fixture, 'warm-cache.json')
    finally:
        docs_inventory.creation_commit = compute_creation_commit
    assert recomputed_creation_paths == ['contents/docs/new-name.md'], recomputed_creation_paths
    cold_inventory, _ = build(fixture, 'cold-cache.json')
    assert sorted(reblamed_paths) == ['contents/docs/guide.md', 'contents/docs/index.mdx', 'contents/docs/new-name.md', 'contents/docs/sources/s1.md'], reblamed_paths
    assert docs_inventory.render_inventory(warm_inventory) == docs_inventory.render_inventory(cold_inventory)
    assert (fixture['root'] / 'warm-cache.json').read_bytes() == (fixture['root'] / 'cold-cache.json').read_bytes()
    assert (fixture['root'] / 'monorepo-warm-cache.json').read_bytes() == (fixture['root'] / 'monorepo-cold-cache.json').read_bytes()
    warm_pages = pages_by_path(warm_inventory)
    for edited_in_place_path in ('contents/docs/guide.md', 'contents/docs/index.mdx', 'contents/docs/sources/s1.md'):
        assert warm_pages[edited_in_place_path]['created_at'] == created_at_before_edits[edited_in_place_path], edited_in_place_path
    assert '/docs/new-name' not in sourceless_pages_by_url(warm_inventory)
    assert warm_pages['contents/docs/guide.md']['agent_line_share'] == 0.5 != guide_share_before_revert
    assert warm_pages['contents/docs/new-name.md']['created_at'] == '2024-01-01'
    assert 'contents/docs/old-name.md' not in json.loads((fixture['root'] / 'warm-cache.json').read_text())['pages']
    _, unchanged_rerun_reblamed_paths = build(fixture, 'warm-cache.json')
    assert unchanged_rerun_reblamed_paths == []


PUBLISHED_LOGS_PAGE = 'docs/published/docs/logs.mdx'
LOGS_PAGE_BEFORE_AGENT = '---\ntitle: Logs\n---\n\nDraft about logs.\n\nPublished note.\n'


def move_file(checkout, source, destination):
    (checkout / destination).parent.mkdir(parents=True, exist_ok=True)
    run_git(checkout, 'mv', source, destination)


def make_partial_clone(root, name, history_with_renames):
    origin = root / f'{name}-origin'
    initialize_repository(origin)
    history_with_renames(origin)
    for setting in ('uploadpack.allowFilter', 'uploadpack.allowAnySHA1InWant'):
        run_git(origin, 'config', setting, 'true')
    clone = root / f'{name}-clone'
    subprocess.run(['git', 'clone', '-q', '--no-checkout', '--filter=blob:none', origin.as_uri(), str(clone)], check=True, capture_output=True)
    return clone


def logs_page_renamed_once_into_published(origin):
    commit_files(origin, {'notes/drafts/logs.mdx': '---\ntitle: Logs\n---\n\nDraft about logs.\n', 'README.md': 'Readme.\n'}, 'Draft logs page', date='2024-01-01')
    move_file(origin, 'notes/drafts/logs.mdx', PUBLISHED_LOGS_PAGE)
    commit_files(origin, {PUBLISHED_LOGS_PAGE: LOGS_PAGE_BEFORE_AGENT, 'notes/other.md': 'Unrelated.\n'}, 'Publish logs page', date='2024-02-01')
    agent_lines = ''.join(f'\nAgent step {index}.\n' for index in range(1, 7))
    commit_files(origin, {PUBLISHED_LOGS_PAGE: LOGS_PAGE_BEFORE_AGENT + agent_lines}, 'Extend logs page', author=AGENT, date='2024-03-01')


def logs_page_renamed_twice_into_published(origin):
    commit_files(origin, {'drafts/logs.mdx': '---\ntitle: Logs\n---\n\nDraft about logs.\n'}, 'Draft logs page', date='2023-06-01')
    move_file(origin, 'drafts/logs.mdx', 'notes/logs.mdx')
    commit_files(origin, {'notes/logs.mdx': '---\ntitle: Logs\n---\n\nDraft about logs.\n\nReviewed.\n'}, 'Review logs page', date='2023-09-01')
    move_file(origin, 'notes/logs.mdx', PUBLISHED_LOGS_PAGE)
    commit_files(origin, {PUBLISHED_LOGS_PAGE: '---\ntitle: Logs\n---\n\nDraft about logs.\n\nReviewed.\n\nPublished.\n'}, 'Publish logs page', date='2024-02-01')


def blame_partial_clone_page(root, clone, cache_name):
    histories, _ = docs_inventory.page_histories(clone, 'origin/master', docs_inventory.MONOREPO_PUBLISHED_DIRECTORY, [PUBLISHED_LOGS_PAGE], root / cache_name)
    commits = docs_inventory.read_commit_index(clone, 'origin/master')
    history = histories[PUBLISHED_LOGS_PAGE]
    return commits[history['creation_commit']]['authored_date'], docs_inventory.line_shares(PUBLISHED_LOGS_PAGE, history, commits)


def verify_page_renamed_into_published_gets_true_creation_after_prefetch(root):
    clone = make_partial_clone(root, 'renamed-once', logs_page_renamed_once_into_published)
    try:
        docs_inventory.creation_commit(clone, 'origin/master', PUBLISHED_LOGS_PAGE)
        raise AssertionError('Creation lookup over unfetched blobs succeeded without lazy fetch')
    except subprocess.CalledProcessError as failure:
        assert b'could not fetch' in failure.stderr, failure.stderr
    fetched_blob_count, tracked_paths = docs_inventory.prefetch_follow_history_blobs(clone, 'origin/master', [PUBLISHED_LOGS_PAGE])
    assert tracked_paths == {PUBLISHED_LOGS_PAGE, 'notes/drafts/logs.mdx'}, tracked_paths
    assert fetched_blob_count == 5, fetched_blob_count
    assert blame_partial_clone_page(root, clone, 'renamed-once-after-prefetch.json') == ('2024-01-01', (6, 11))


def verify_two_hop_rename_chain_resolves_and_fixed_point_terminates(root):
    clone = make_partial_clone(root, 'renamed-twice', logs_page_renamed_twice_into_published)
    fetched_blob_count, tracked_paths = docs_inventory.prefetch_follow_history_blobs(clone, 'origin/master', [PUBLISHED_LOGS_PAGE])
    assert tracked_paths == {PUBLISHED_LOGS_PAGE, 'notes/logs.mdx', 'drafts/logs.mdx'}, tracked_paths
    assert fetched_blob_count == 3, fetched_blob_count
    assert docs_inventory.prefetch_follow_history_blobs(clone, 'origin/master', [PUBLISHED_LOGS_PAGE]) == (0, tracked_paths)
    assert blame_partial_clone_page(root, clone, 'renamed-twice.json')[0] == '2023-06-01'


def monorepo_page_histories(root, clone, cache_name):
    return docs_inventory.page_histories(clone, 'origin/master', docs_inventory.MONOREPO_PUBLISHED_DIRECTORY, [PUBLISHED_LOGS_PAGE], root / cache_name)


def site_with_one_docs_page(origin):
    commit_files(origin, {'contents/docs/existing.md': '## Existing\n\nExisting page.\n', 'src/widget.ts': 'export const widget = 1\n'}, 'Add existing page', date='2024-01-01')
    commit_files(origin, {'src/widget.ts': 'export const widget = 2\n'}, 'Change widget', date='2024-02-01')


def verify_site_page_added_over_unfetched_parent_tree_is_prefetched_then_warm_run_fetches_nothing(root):
    clone = make_partial_clone(root, 'site-shaped', site_with_one_docs_page)
    existing_page, new_page = 'contents/docs/existing.md', 'contents/docs/cisco-meraki.md'

    def site_page_histories(page_paths):
        return docs_inventory.page_histories(clone, 'origin/master', docs_inventory.DOCS_DIRECTORY, page_paths, root / 'site-shaped-cache.json')

    record_fetches_and_git_arguments(lambda: site_page_histories([existing_page]))
    origin = root / 'site-shaped-origin'
    commit_files(origin, {new_page: '## Setup\n\nConnect Cisco Meraki.\n'}, 'Add Cisco Meraki page', author=AGENT, date='2024-03-01')
    run_git(clone, 'fetch', '-q', 'origin')
    try:
        docs_inventory.creation_commit(clone, 'origin/master', new_page)
        raise AssertionError('Creation lookup over an unfetched parent tree succeeded without lazy fetch')
    except subprocess.CalledProcessError as failure:
        assert b'could not fetch' in failure.stderr, failure.stderr
    (histories, reblamed), fetched, _ = record_fetches_and_git_arguments(lambda: site_page_histories([existing_page, new_page]))
    assert reblamed == [new_page] and fetched, (reblamed, fetched)
    commits = docs_inventory.read_commit_index(clone, 'origin/master')
    assert commits[histories[new_page]['creation_commit']]['authored_date'] == '2024-03-01'
    (_, warm_reblamed), warm_fetched, warm_git_arguments = record_fetches_and_git_arguments(lambda: site_page_histories([existing_page, new_page]))
    assert (warm_reblamed, warm_fetched) == ([], []), (warm_reblamed, warm_fetched)
    assert not any('--follow' in arguments for arguments in warm_git_arguments), warm_git_arguments


def record_fetches_and_git_arguments(action):
    fetched_object_ids = []
    git_arguments = []
    fetch_blobs = docs_inventory.fetch_blobs
    git_text = docs_inventory.git_text

    def recording_fetch_blobs(checkout, object_ids):
        fetched_object_ids.extend(object_ids)
        return fetch_blobs(checkout, object_ids)

    def recording_git_text(checkout, *arguments, **options):
        git_arguments.append(arguments)
        return git_text(checkout, *arguments, **options)

    docs_inventory.fetch_blobs = recording_fetch_blobs
    docs_inventory.git_text = recording_git_text
    try:
        result = action()
    finally:
        docs_inventory.fetch_blobs = fetch_blobs
        docs_inventory.git_text = git_text
    return result, fetched_object_ids, git_arguments


def verify_warm_monorepo_run_fetches_nothing_and_never_follows_until_a_page_is_added(root):
    clone = make_partial_clone(root, 'warm-monorepo', logs_page_renamed_once_into_published)
    (cold_histories, cold_reblamed), cold_fetched, cold_git_arguments = record_fetches_and_git_arguments(lambda: monorepo_page_histories(root, clone, 'warm-monorepo.json'))
    assert cold_reblamed == [PUBLISHED_LOGS_PAGE] and cold_fetched and any('--follow' in arguments for arguments in cold_git_arguments)
    (_, warm_reblamed), warm_fetched, warm_git_arguments = record_fetches_and_git_arguments(lambda: monorepo_page_histories(root, clone, 'warm-monorepo.json'))
    assert (warm_reblamed, warm_fetched) == ([], []), (warm_reblamed, warm_fetched)
    assert not any('--follow' in arguments for arguments in warm_git_arguments), warm_git_arguments
    origin = root / 'warm-monorepo-origin'
    commit_files(origin, {PUBLISHED_LOGS_PAGE: LOGS_PAGE_BEFORE_AGENT + '\nEdited in place.\n'}, 'Edit logs page in place', date='2024-04-01')
    run_git(clone, 'fetch', '-q', 'origin')
    (edited_histories, edited_reblamed), edited_fetched, edited_git_arguments = record_fetches_and_git_arguments(lambda: monorepo_page_histories(root, clone, 'warm-monorepo.json'))
    assert edited_reblamed == [PUBLISHED_LOGS_PAGE] and len(edited_fetched) == 1, edited_fetched
    assert not any('--follow' in arguments for arguments in edited_git_arguments), edited_git_arguments
    assert edited_histories[PUBLISHED_LOGS_PAGE]['creation_commit'] == cold_histories[PUBLISHED_LOGS_PAGE]['creation_commit']


def verify_blame_at_last_change_commit_equals_blame_at_head(fixture):
    histories = json.loads((fixture['root'] / 'cold-cache.json').read_text())['pages']
    for path, history in histories.items():
        assert docs_inventory.nonblank_lines_by_commit(fixture['site'], 'HEAD', path) == history['nonblank_lines_by_commit'], path


def main():
    verify_page_url_drops_contents_extension_index_and_case()
    verify_monorepo_page_url_strips_published_prefix_like_on_create_node()
    verify_underscore_segments_and_non_markdown_are_not_pages()
    verify_agent_commit_rules()
    verify_co_author_trailers_parse_name_and_optional_email()
    verify_hostile_whitespace_parses_in_under_half_a_second()
    verify_headings_titles_and_template_literals_keep_regex_semantics()
    verify_doc_kind_thresholds()
    verify_fingerprint_ignores_fenced_code_and_unstructured_pages_have_none()
    verify_import_sources_match_from_clauses_only()
    verify_import_sources_stay_fast_after_fifty_thousand_whitespace_characters()
    verify_frontmatter_title_forms()
    verify_redirect_sources_expand_extension_wildcard_and_choice_patterns()
    verify_unsupported_redirect_source_raises()
    verify_site_links_need_posthog_origin_or_site_relative_path()
    verify_nav_files_are_top_level_src_navs_files_other_than_tests()
    verify_nav_routes_match_whole_quoted_routes_only()
    verify_nav_hooks_put_every_live_route_one_segment_below_each_called_base_path_in_nav()
    verify_unresolved_nav_url_template_raises()
    verify_nav_hook_call_with_non_literal_argument_raises()
    verify_sitemap_template_segments_are_recognized_and_raise_in_source_urls()
    verify_live_docs_route_without_row_raises()
    verify_blame_commit_missing_from_head_history_raises()
    verify_unresolvable_connector_docs_url_raises()
    verify_splatted_source_config_without_docs_url_raises()
    with tempfile.TemporaryDirectory(prefix='docs-inventory-') as temporary_directory:
        root = Path(temporary_directory)
        fixture = {'root': root, 'site': make_site(root / 'site'), 'monorepo': make_monorepo(root / 'monorepo'), 'routes': write_live_routes(root)}
        verify_page_facts(fixture)
        verify_warm_cache_matches_cold_run_after_rename_revert_and_merge(fixture)
        verify_blame_at_last_change_commit_equals_blame_at_head(fixture)
        verify_page_renamed_into_published_gets_true_creation_after_prefetch(root)
        verify_two_hop_rename_chain_resolves_and_fixed_point_terminates(root)
        verify_warm_monorepo_run_fetches_nothing_and_never_follows_until_a_page_is_added(root)
        verify_site_page_added_over_unfetched_parent_tree_is_prefetched_then_warm_run_fetches_nothing(root)
    print('PASS')


if __name__ == '__main__':
    main()
