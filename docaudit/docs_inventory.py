import ast
import collections
import concurrent.futures
import fnmatch
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

import doccoverage
from attribution import GITLINK_MODE, PREFETCH_BATCH_SIZE
from impact import git, git_environment

INVENTORY_FILE_NAME = 'docs-inventory.json'
INVENTORY_FORMAT_VERSION = 1
CACHE_FILE = doccoverage.TOOL_ROOT / 'data/docs-inventory-cache.json'
MONOREPO_CACHE_FILE = doccoverage.TOOL_ROOT / 'data/docs-inventory-monorepo-cache.json'
CACHE_FORMAT_VERSION = 1
GIT_TIMEOUT_SECONDS = 600
HISTORY_TIMEOUT_SECONDS = 6 * 60 * 60
BLAME_WORKERS = min(8, os.cpu_count() or 1)
DOCS_DIRECTORY = 'contents/docs'
SITE_CONTENT_PREFIX = 'contents'
MONOREPO_PUBLISHED_DIRECTORY = 'docs/published'
SITE_REPOSITORY = 'posthog.com'
MONOREPO_REPOSITORY = 'posthog'
CONNECTOR_SOURCES_DIRECTORY = 'products/warehouse_sources/backend/temporal/data_imports/sources'
CONNECTOR_SOURCE_PATH_PATTERN = re.compile(re.escape(CONNECTOR_SOURCES_DIRECTORY) + r'/[^/]+/source\.py')
TEMPLATE_FAMILY_MIN_SIZE = 5
DOC_KINDS = (
    'templated spec', 'agent-written prose', 'mixed prose', 'human-written prose', 'empty page', 'generated page', 'not a page',
)
PAGE_CLASSES = ('live-route', 'redirected-away', 'not-a-route', 'contested-route', 'generated-route', 'sitemap-template-route')
FOLLOW_SOURCE_STATUS_PATTERN = re.compile(r'[RC]\d*')
SITEMAP_TEMPLATE_SEGMENT_PATTERN = re.compile(r'^\[.*\]$|^:|\*')
CONNECTOR_BUCKETS = ('posthog-docs', 'docs-url-elsewhere', 'docs-url-none', 'no-docs-url')
PAGE_COLUMNS = (
    'url', 'source_repo', 'path', 'title', 'page_class', 'doc_kind', 'is_route', 'redirected_away', 'created_at', 'agent_line_share',
    'nonblank_lines', 'template_family_size', 'site_inbound_links', 'in_nav', 'redirects_into', 'app_links',
)

NON_AUTHORING_BOT_PATTERN = re.compile(r'imgbot|dependabot', re.IGNORECASE)
AGENT_IDENTITY_PATTERN = re.compile(r'\[bot\]|noreply@anthropic\.com|cursor ?agent|cursoragent@|\bcodex\b|openai', re.IGNORECASE)
AGENT_NAME_PREFIX_PATTERN = re.compile(r'\s*(?:claude|copilot)\b', re.IGNORECASE)
AGENT_MESSAGE_PATTERN = re.compile(r'Generated with \[Claude Code|🤖 Generated with')
CO_AUTHOR_TRAILER_KEY = 'co-authored-by:'

FENCED_CODE_PATTERN = re.compile(r'```.*?```', re.DOTALL)
IMPORT_CLAUSE_PATTERN = re.compile(r'^import(?=\s)([\w*{}\s,]*)["\']', re.MULTILINE)
QUOTED_SOURCE_PATTERN = re.compile(r'([^"\']+)["\']')
FRONTMATTER_PATTERN = re.compile(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)', re.DOTALL)

SITE_ORIGIN_PATTERN = re.compile(r'^https?://(?:www\.)?posthog\.com(?=/|$)', re.IGNORECASE)
EXTENSION_PARAMETER_PATTERN = re.compile(r':ext(?:\(\\\.md\))?\?$')
SOURCE_TOKEN_PATTERN = re.compile(
    r'(?P<wildcard>/:\w+\*)|(?P<constrained>:\w+\((?P<constraint>[^()]+)\))'
    r'|\((?P<choices>[\w-]+(?:\|[\w-]+)+)\)|(?P<parameter>:\w+)|(?P<literal>(?:[^:()*/]|/(?!:\w+\*))+)'
)
SITE_LINK_PATTERN = re.compile(
    r'(?:(?<![\w.-])(?:https?://)?(?:www\.)?posthog\.com|(?<![\w.\-/:~@]))(/docs(?:/[\w./-]*)?)(?![\w-])', re.IGNORECASE
)
NAV_LINK_PATTERN = re.compile(r'["\'`](?:https?://(?:www\.)?posthog\.com)?(/docs(?:/[^"\'`#?\s]*)?)(?:[#?][^"\'`]*)?["\'`]', re.IGNORECASE)
APP_LINK_GREP_PATTERN = r'posthog\.com/docs/[A-Za-z0-9/_.-]+'
POSTHOG_DOCS_URL_PATTERN = re.compile(r'^https?://(?:www\.)?posthog\.com/docs(?:[/?#]|$)', re.IGNORECASE)
LINKING_FILE_GLOBS = {'contents/': '*.md*', 'src/': '*.[jt]s*'}
NAVS_DIRECTORY = 'src/navs/'
SOURCES_NAV_HOOK_FILE = 'src/navs/useSourcesNav.ts'
DATA_PIPELINES_NAV_HOOK_FILE = 'src/navs/useDataPipelinesNav.ts'
RESOLVED_NAV_URL_TEMPLATES = {
    (SOURCES_NAV_HOOK_FILE, '${basePath}/${node.slug}'),
    (SOURCES_NAV_HOOK_FILE, '${basePath}/${s.slug}'),
    (DATA_PIPELINES_NAV_HOOK_FILE, '/docs/cdp/${type}s/${node.slug}'),
}
SOURCES_NAV_DEFAULT_BASE_PATH_PATTERN = re.compile(r"function useSourcesNav\(basePath = '([^']+)'\)")
SOURCES_NAV_CALL_PATTERN = re.compile(r'\buseSourcesNav\(([^)]*)\)')
DATA_PIPELINES_NAV_CALL_PATTERN = re.compile(r'\buseDataPipelinesNav\(([^)]*)\)')
QUOTED_DOCS_PATH_PATTERN = re.compile(r"""\s*(['"])(/docs[^'"]*)\1\s*""")
PIPELINE_TYPE_ARGUMENT_PATTERN = re.compile(r"""\s*\{\s*type:\s*(['"])(\w+)\1\s*\}\s*""")


def git_text(checkout, *arguments, timeout=GIT_TIMEOUT_SECONDS):
    return git(checkout, *arguments, timeout=timeout)


def read_blobs(checkout, revision, paths):
    request = ''.join(f'{revision}:{path}\n' for path in paths).encode()
    output = subprocess.run(
        ['git', '-C', str(checkout), 'cat-file', '--batch'], input=request, capture_output=True, check=True,
        timeout=GIT_TIMEOUT_SECONDS, env=git_environment(),
    ).stdout
    texts = {}
    position = 0
    for path in paths:
        header_end = output.index(b'\n', position)
        header = output[position:header_end].decode()
        fields = header.split(' ')
        if len(fields) != 3 or fields[1] != 'blob':
            raise ValueError(f'{checkout}: cannot read {revision}:{path}: {header}')
        content_start = header_end + 1
        content_end = content_start + int(fields[2])
        texts[path] = output[content_start:content_end].decode('utf-8', errors='replace')
        position = content_end + 1
    return texts


def tree_paths(checkout, revision, *directories):
    listing = git_text(checkout, 'ls-tree', '-r', '-z', '--name-only', revision, '--', *directories)
    return [path for path in listing.split('\0') if path]


def is_docs_page_path(path):
    if not path.endswith(('.md', '.mdx')):
        return False
    return not any(segment.startswith('_') for segment in path.split('/'))


def page_url(path, source_prefix=SITE_CONTENT_PREFIX):
    route = re.sub(r'\.mdx?$', '', path.removeprefix(source_prefix))
    return route.removesuffix('/index').lower()


def monorepo_page_url(path):
    return page_url(path, MONOREPO_PUBLISHED_DIRECTORY)


def is_docs_route(route):
    return route == '/docs' or route.startswith('/docs/')


def normalized_route(route):
    route = route.rstrip('.')
    route = route.removesuffix('.mdx').removesuffix('.md')
    return route.rstrip('/').lower() or '/'


def is_agent_identity(name, email):
    identity = f'{name} <{email}>'
    if NON_AUTHORING_BOT_PATTERN.search(identity):
        return False
    return bool(AGENT_IDENTITY_PATTERN.search(identity) or AGENT_NAME_PREFIX_PATTERN.match(name))


def co_author_identity(line):
    if line[:len(CO_AUTHOR_TRAILER_KEY)].lower() != CO_AUTHOR_TRAILER_KEY:
        return None
    trailer = line[len(CO_AUTHOR_TRAILER_KEY):].lstrip(' \t')
    name, opening_bracket, after_name = trailer.partition('<')
    if not opening_bracket:
        return trailer.rstrip(' \t'), ''
    email, closing_bracket, after_email = after_name.partition('>')
    if not closing_bracket or after_email.strip(' \t'):
        return None
    return name.rstrip(' \t'), email


def co_authors(message):
    return [identity for identity in map(co_author_identity, message.split('\n')) if identity is not None]


def is_agent_commit(author_name, author_email, message):
    co_authors_of_commit = co_authors(message)
    if any(is_agent_identity(name, email) for name, email in [(author_name, author_email), *co_authors_of_commit]):
        return True
    return bool(AGENT_MESSAGE_PATTERN.search(message))


def read_commit_index(checkout, revision):
    log = git_text(checkout, 'log', '--format=%x1e%H%x1f%an%x1f%ae%x1f%aI%x1f%B', revision)
    commits = {}
    for record in log.split('\x1e')[1:]:
        fields = record.split('\x1f', 4)
        if len(fields) != 5:
            raise ValueError(f'{checkout}: unparseable commit record {record[:80]!r}')
        revision, author_name, author_email, authored_at, message = fields
        commits[revision] = {'authored_date': authored_at[:10], 'is_agent': is_agent_commit(author_name, author_email, message)}
    return commits


def last_change_commit_by_path(checkout, revision, directory):
    log = git_text(checkout, 'log', '--first-parent', '--name-only', '-z', '--format=%x1e%H', revision, '--', directory)
    last_change_by_path = {}
    for record in log.split('\x1e')[1:]:
        revision, _, names = record.partition('\0')
        for name in names.split('\0'):
            path = name.strip('\n')
            if path:
                last_change_by_path.setdefault(path, revision)
    return last_change_by_path


def nonblank_lines_by_commit(checkout, revision, path, timeout=GIT_TIMEOUT_SECONDS):
    porcelain = git_text(checkout, 'blame', '--line-porcelain', '-w', revision, '--', path, timeout=timeout)
    line_counts = collections.Counter()
    expects_header = True
    line_commit = None
    for line in porcelain.split('\n'):
        if expects_header and line:
            line_commit = line.split(' ', 1)[0]
            expects_header = False
            continue
        if not line.startswith('\t'):
            continue
        expects_header = True
        if line[1:].strip():
            line_counts[line_commit] += 1
    return dict(sorted(line_counts.items()))


def file_statuses(name_status_tokens):
    statuses = []
    position = 0
    while position < len(name_status_tokens):
        status = name_status_tokens[position]
        statuses.append(status)
        position += 3 if status[:1] in ('R', 'C') else 2
    return statuses


def creation_commit(checkout, revision, path, timeout=GIT_TIMEOUT_SECONDS):
    log = git_text(checkout, 'log', '--follow', '--name-status', '-z', '--format=%x1e%H', revision, '--', path, timeout=timeout)
    creations = []
    for record in log.split('\x1e')[1:]:
        revision_id, *name_status_tokens = [token.strip('\n') for token in record.split('\0') if token.strip('\n')]
        if 'A' in file_statuses(name_status_tokens):
            creations.append(revision_id)
    if not creations:
        raise ValueError(f'{path}: no creation commit in the history of {revision}')
    return creations[-1]


def is_ancestor(checkout, earlier_commit, later_commit):
    completed = subprocess.run(
        ['git', '-C', str(checkout), 'merge-base', '--is-ancestor', earlier_commit, later_commit], capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SECONDS, env=git_environment(),
    )
    if completed.returncode > 1:
        raise ValueError(f'{checkout}: cannot decide whether {earlier_commit} precedes {later_commit}: {completed.stderr.strip()}')
    return completed.returncode == 0


def keeps_file_identity(checkout, path, earlier_commit, later_commit):
    if not is_ancestor(checkout, earlier_commit, later_commit):
        return False
    additions_or_deletions = git_text(checkout, 'log', '-m', '--no-renames', '--diff-filter=AD', '--format=%H', f'{earlier_commit}..{later_commit}', '--', path)
    return not additions_or_deletions.strip()


def reusable_creation_commit(checkout, path, last_change_commit, cached_history):
    if cached_history and keeps_file_identity(checkout, path, cached_history['last_change_commit'], last_change_commit):
        return cached_history['creation_commit']
    return None


def page_history(checkout, path, last_change_commit, reused_creation_commit, timeout):
    try:
        return page_history_at(checkout, path, last_change_commit, reused_creation_commit, timeout)
    except subprocess.CalledProcessError as failure:
        raise ValueError(f'{checkout}: history of {path} at {last_change_commit} failed: {failure.stderr.decode(errors="replace").strip()}') from failure
    except subprocess.TimeoutExpired as failure:
        raise ValueError(f'{checkout}: history of {path} at {last_change_commit} timed out: {failure.cmd}') from failure


def page_history_at(checkout, path, last_change_commit, reused_creation_commit, timeout):
    return {
        'last_change_commit': last_change_commit,
        'creation_commit': reused_creation_commit or creation_commit(checkout, last_change_commit, path, timeout=timeout),
        'nonblank_lines_by_commit': nonblank_lines_by_commit(checkout, last_change_commit, path, timeout=timeout),
    }


def read_cache(cache_path):
    if not cache_path.exists():
        return {}
    cache = json.loads(cache_path.read_text(encoding='utf-8'))
    if cache.get('format_version') != CACHE_FORMAT_VERSION:
        return {}
    return cache['pages']


def write_cache(cache_path, histories):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps({'format_version': CACHE_FORMAT_VERSION, 'pages': histories}, sort_keys=True, separators=(',', ':'))
    with tempfile.NamedTemporaryFile('w', dir=cache_path.parent, prefix=cache_path.name, suffix='.partial', delete=False, encoding='utf-8') as partial:
        partial.write(encoded)
    os.replace(partial.name, cache_path)


def page_histories(checkout, revision, directory, page_paths, cache_path):
    last_change_by_path = last_change_commit_by_path(checkout, revision, directory)
    uncommitted_paths = [path for path in page_paths if path not in last_change_by_path]
    if uncommitted_paths:
        raise ValueError(f'No first-parent commit touches these pages: {uncommitted_paths[:5]}')
    cached_histories = read_cache(cache_path)
    histories = {}
    stale_paths = []
    for path in page_paths:
        cached = cached_histories.get(path)
        if cached and cached['last_change_commit'] == last_change_by_path[path]:
            histories[path] = cached
            continue
        stale_paths.append(path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=BLAME_WORKERS) as pool:
        reused_creation_commits = dict(zip(stale_paths, pool.map(
            lambda path: reusable_creation_commit(checkout, path, last_change_by_path[path], cached_histories.get(path)), stale_paths,
        )))
        prefetch_stale_page_blobs(checkout, revision, stale_paths, reused_creation_commits)
        fresh_histories = pool.map(
            lambda path: page_history(checkout, path, last_change_by_path[path], reused_creation_commits[path], HISTORY_TIMEOUT_SECONDS), stale_paths,
        )
        histories.update(zip(stale_paths, fresh_histories))
    write_cache(cache_path, histories)
    return histories, stale_paths


def blob_ids_in_raw_diff(raw_diff):
    object_ids = set()
    for field in raw_diff.split('\0'):
        if not field.startswith(':'):
            continue
        old_mode, new_mode, old_object_id, new_object_id = field[1:].split()[:4]
        for mode, object_id in ((old_mode, old_object_id), (new_mode, new_object_id)):
            if mode != GITLINK_MODE and object_id.strip('0'):
                object_ids.add(object_id)
    return object_ids


def missing_object_ids(checkout, object_ids):
    if not object_ids:
        return []
    object_checks = subprocess.run(
        ['git', '-C', str(checkout), 'cat-file', '--batch-check'], input=''.join(f'{object_id}\n' for object_id in sorted(object_ids)).encode(),
        capture_output=True, check=True, timeout=GIT_TIMEOUT_SECONDS, env=git_environment(),
    ).stdout.decode()
    return [line.split()[0] for line in object_checks.splitlines() if line.endswith(' missing')]


def path_history_blob_ids(checkout, revision, tracked_paths):
    raw_history = git_text(checkout, 'log', '--full-history', '-m', '--format=', '--raw', '-z', '--no-abbrev', '--no-renames', revision, '--', *tracked_paths)
    return blob_ids_in_raw_diff(raw_history)


def added_paths_by_commit(checkout, revision, tracked_paths):
    log = git_text(checkout, 'log', '--full-history', '-m', '--no-renames', '--diff-filter=A', '--name-only', '-z', '--format=%x1e%H', revision, '--', *tracked_paths)
    added_paths = collections.defaultdict(set)
    for record in log.split('\x1e')[1:]:
        commit, *paths = [token.strip('\n') for token in record.split('\0') if token.strip('\n')]
        added_paths[commit].update(paths)
    return added_paths


def tree_blob_ids(checkout, tree_ishes):
    blob_ids = set()
    for tree_ish in tree_ishes:
        for entry in git_text(checkout, 'ls-tree', '-r', '-z', tree_ish).split('\0'):
            metadata = entry.split('\t', 1)[0].split()
            if len(metadata) == 3 and metadata[1] == 'blob':
                blob_ids.add(metadata[2])
    return blob_ids


def commit_with_parents(checkout, commit):
    return git_text(checkout, 'rev-list', '--parents', '--no-walk', commit).split()


def follow_sources(checkout, commit, path):
    output = git_text(checkout, 'log', '--no-walk', '-m', '--follow', '--name-status', '-z', '--format=', commit, '--', path, timeout=HISTORY_TIMEOUT_SECONDS)
    tokens = [token.strip('\n') for token in output.split('\0')]
    return {tokens[index + 1] for index, token in enumerate(tokens[:-2]) if FOLLOW_SOURCE_STATUS_PATTERN.fullmatch(token) and tokens[index + 2] == path}


def fetch_blobs(checkout, object_ids):
    for batch_start in range(0, len(object_ids), PREFETCH_BATCH_SIZE):
        subprocess.run(
            ['git', '-C', str(checkout), '-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '-q', 'origin', '--no-tags', '--no-write-fetch-head',
             '--recurse-submodules=no', '--filter=blob:none', *object_ids[batch_start:batch_start + PREFETCH_BATCH_SIZE]],
            capture_output=True, check=True, timeout=1800,
        )


def fetch_missing(checkout, object_ids):
    missing = missing_object_ids(checkout, object_ids)
    fetch_blobs(checkout, missing)
    return len(missing)


def prefetch_follow_history_blobs(checkout, revision, page_paths):
    tracked_paths = set(page_paths)
    examined_additions = set()
    fetched_blob_count = 0
    while True:
        fetched_blob_count += fetch_missing(checkout, path_history_blob_ids(checkout, revision, sorted(tracked_paths)))
        additions = {
            (commit, path) for commit, paths in added_paths_by_commit(checkout, revision, sorted(tracked_paths)).items() for path in paths
        } - examined_additions
        addition_trees = {tree for commit in {commit for commit, _ in additions} for tree in commit_with_parents(checkout, commit)}
        fetched_blob_count += fetch_missing(checkout, tree_blob_ids(checkout, sorted(addition_trees)))
        earlier_paths = {source for commit, path in sorted(additions) for source in follow_sources(checkout, commit, path)} - tracked_paths
        examined_additions |= additions
        if not earlier_paths:
            return fetched_blob_count, tracked_paths
        tracked_paths |= earlier_paths


def prefetch_stale_page_blobs(checkout, revision, stale_paths, reused_creation_commits):
    edited_in_place_paths = sorted(path for path in stale_paths if reused_creation_commits[path])
    created_or_moved_paths = sorted(path for path in stale_paths if not reused_creation_commits[path])
    fetched_blob_count = 0
    if edited_in_place_paths:
        fetched_blob_count += fetch_missing(checkout, path_history_blob_ids(checkout, revision, edited_in_place_paths))
    if created_or_moved_paths:
        fetched_blob_count += prefetch_follow_history_blobs(checkout, revision, created_or_moved_paths)[0]
    return fetched_blob_count


def ends_with_from_clause(import_clause):
    clause_before_trailing_space = import_clause.rstrip()
    specifier_and_space = clause_before_trailing_space.removesuffix('from')
    has_from_keyword = specifier_and_space != clause_before_trailing_space
    has_space_after_from = clause_before_trailing_space != import_clause
    return has_from_keyword and has_space_after_from and len(specifier_and_space) >= 3 and specifier_and_space[-1].isspace()


def import_sources(text):
    sources = []
    for clause in IMPORT_CLAUSE_PATTERN.finditer(text):
        source = QUOTED_SOURCE_PATTERN.match(text, clause.end())
        if source and ends_with_from_clause(clause[1]):
            sources.append(source[1])
    return sources


def second_level_heading(line):
    if not line.startswith('##'):
        return None
    after_marker = line[2:]
    content = after_marker.lstrip(' \t')
    if content == after_marker:
        return None
    heading = content.rstrip(' \t')
    if heading:
        return heading
    if len(after_marker) >= 2:
        return after_marker[-1]
    return None


def second_level_headings(text):
    return [heading for heading in map(second_level_heading, text.split('\n')) if heading is not None]


def structural_fingerprint(text):
    body = FENCED_CODE_PATTERN.sub('', text)
    import_stems = tuple(sorted(PurePosixPath(source).stem for source in import_sources(body)))
    headings = tuple(heading.lower() for heading in second_level_headings(body))
    if not import_stems and not headings:
        return None
    return import_stems, headings


def doc_kind(is_templated, agent_lines, nonblank_lines):
    if is_templated:
        return 'templated spec'
    if nonblank_lines == 0:
        return 'empty page'
    if agent_lines * 2 >= nonblank_lines:
        return 'agent-written prose'
    if agent_lines * 5 >= nonblank_lines:
        return 'mixed prose'
    return 'human-written prose'


def page_class(is_route, is_contested, is_redirected_away):
    if not is_route:
        return 'not-a-route'
    if is_contested:
        return 'contested-route'
    if is_redirected_away:
        return 'redirected-away'
    return 'live-route'


def title_value(frontmatter):
    for line in frontmatter.split('\n'):
        after_key = line.removeprefix('title').lstrip(' \t')
        if not line.startswith('title') or not after_key.startswith(':'):
            continue
        return after_key[1:].lstrip(' \t').removesuffix('\r').rstrip(' \t')
    return None


def without_trailing_comment(value):
    for index, character in enumerate(value):
        if character == '#' and index > 0 and value[index - 1] in ' \t':
            return value[:index].rstrip(' \t')
    return value


def frontmatter_title(path, text):
    frontmatter = FRONTMATTER_PATTERN.match(text)
    if not frontmatter:
        return None
    value = title_value(frontmatter[1])
    if value is None:
        return None
    if not value or value[0] in '|>':
        raise ValueError(f'{path}: frontmatter title {value!r} is not a single-line scalar')
    quote = value[0]
    if quote not in '"\'':
        return without_trailing_comment(value)
    if len(value) < 2 or value[-1] != quote:
        raise ValueError(f'{path}: frontmatter title {value!r} has an unbalanced quote')
    if quote == "'":
        return value[1:-1].replace("''", "'")
    return re.sub(r'\\(.)', r'\1', value[1:-1])


def read_live_routes(live_routes_path):
    routes = {normalized_route(route) for route in Path(live_routes_path).read_text(encoding='utf-8').split()}
    if not routes:
        raise ValueError(f'{live_routes_path}: no live routes')
    return routes


def source_route_matcher(source):
    route = EXTENSION_PARAMETER_PATTERN.sub('', source)
    if not re.search(r'[:()*]', route):
        return normalized_route(route), None
    pattern_parts = []
    position = 0
    while position < len(route):
        token = SOURCE_TOKEN_PATTERN.match(route, position)
        if not token:
            raise ValueError(f'vercel.json redirect source {source!r}: unsupported pattern syntax at {route[position:]!r}')
        position = token.end()
        pattern_parts.append(source_token_regex(token))
    return None, re.compile(''.join(pattern_parts).rstrip('/'), re.IGNORECASE)


def source_token_regex(token):
    if token['wildcard']:
        return '(?:/.*)?'
    if token['constrained']:
        return f'(?:{token["constraint"]})'
    if token['choices']:
        return f'(?:{token["choices"]})'
    if token['parameter']:
        return '[^/]+'
    return re.escape(token['literal'].lower())


def destination_route(destination):
    route = EXTENSION_PARAMETER_PATTERN.sub('', SITE_ORIGIN_PATTERN.sub('', destination))
    if not route.startswith('/'):
        return None
    return normalized_route(re.split(r'[?#]', route, maxsplit=1)[0])


class Redirects:
    def __init__(self, vercel_config_text):
        rules = json.loads(vercel_config_text)['redirects']
        self.exact_sources = set()
        self.source_patterns = []
        for rule in rules:
            if 'has' in rule or 'missing' in rule:
                continue
            exact_source, source_pattern = source_route_matcher(rule['source'])
            if source_pattern is None:
                self.exact_sources.add(exact_source)
                continue
            self.source_patterns.append(source_pattern)
        self.destination_counts = collections.Counter(destination_route(rule['destination']) for rule in rules)

    def is_redirected_away(self, url):
        return url in self.exact_sources or any(pattern.fullmatch(url) for pattern in self.source_patterns)

    def redirects_into(self, url):
        return self.destination_counts[url]


def is_nav_file(path):
    file_name = path.removeprefix(NAVS_DIRECTORY)
    return path.startswith(NAVS_DIRECTORY) and '/' not in file_name and '.test.' not in file_name


def is_linking_file(path):
    if path.startswith(NAVS_DIRECTORY):
        return False
    return any(path.startswith(prefix) and fnmatch.fnmatchcase(path.rsplit('/', 1)[-1], pattern) for prefix, pattern in LINKING_FILE_GLOBS.items())


def linking_files_by_route(file_texts):
    linking_files = collections.defaultdict(set)
    for path, text in file_texts.items():
        for route in SITE_LINK_PATTERN.findall(text):
            linking_files[normalized_route(route)].add(path)
    return linking_files


def nav_routes(nav_texts):
    return {normalized_route(route) for text in nav_texts for route in NAV_LINK_PATTERN.findall(text) if '${' not in route}


def template_literals(text):
    backtick_positions = [index for index, character in enumerate(text) if character == '`']
    literals = []
    cursor = 0
    while cursor + 1 < len(backtick_positions):
        content = text[backtick_positions[cursor] + 1:backtick_positions[cursor + 1]]
        if '${' not in content:
            cursor += 1
            continue
        literals.append(content)
        cursor += 2
    return literals


def builds_docs_url(template):
    return '/docs' in template or template.startswith('${')


def require_resolved_nav_url_templates(nav_texts_by_path):
    unresolved = sorted(
        (path, template) for path, text in nav_texts_by_path.items() for template in template_literals(text)
        if builds_docs_url(template) and (path, template) not in RESOLVED_NAV_URL_TEMPLATES
    )
    if unresolved:
        raise ValueError(f'Nav files build /docs urls from unresolved template literals: {unresolved[:5]}')


def hook_call_arguments(call_pattern, hook_file, source_texts_by_path):
    return [
        (path, argument) for path, text in sorted(source_texts_by_path.items()) if path != hook_file
        for argument in call_pattern.findall(text)
    ]


def sources_nav_base_paths(hook_text, source_texts_by_path):
    default_base_path = SOURCES_NAV_DEFAULT_BASE_PATH_PATTERN.search(hook_text)
    if not default_base_path:
        raise ValueError(f'{SOURCES_NAV_HOOK_FILE}: no default basePath literal')
    base_paths = set()
    for path, argument in hook_call_arguments(SOURCES_NAV_CALL_PATTERN, SOURCES_NAV_HOOK_FILE, source_texts_by_path):
        if not argument.strip():
            base_paths.add(default_base_path[1])
            continue
        quoted_base_path = QUOTED_DOCS_PATH_PATTERN.fullmatch(argument)
        if not quoted_base_path:
            raise ValueError(f'{path}: useSourcesNav({argument}) base path is not a /docs string literal')
        base_paths.add(quoted_base_path[2])
    return base_paths


def data_pipelines_nav_base_paths(source_texts_by_path):
    base_paths = set()
    for path, argument in hook_call_arguments(DATA_PIPELINES_NAV_CALL_PATTERN, DATA_PIPELINES_NAV_HOOK_FILE, source_texts_by_path):
        pipeline_type = PIPELINE_TYPE_ARGUMENT_PATTERN.fullmatch(argument)
        if not pipeline_type:
            raise ValueError(f'{path}: useDataPipelinesNav({argument}) type is not a string literal')
        base_paths.add(f'/docs/cdp/{pipeline_type[2]}s')
    return base_paths


def nav_hook_routes(nav_texts_by_path, source_texts_by_path, live_routes):
    require_resolved_nav_url_templates(nav_texts_by_path)
    base_paths = data_pipelines_nav_base_paths(source_texts_by_path)
    if SOURCES_NAV_HOOK_FILE in nav_texts_by_path:
        base_paths |= sources_nav_base_paths(nav_texts_by_path[SOURCES_NAV_HOOK_FILE], source_texts_by_path)
    normalized_base_paths = {normalized_route(base_path) for base_path in base_paths}
    return {route for route in live_routes if route.rsplit('/', 1)[0] in normalized_base_paths}


def app_link_files_by_route(monorepo_root, revision):
    try:
        output = git_text(monorepo_root, 'grep', '-I', '-o', '--null', '-E', APP_LINK_GREP_PATTERN, revision, '--', '.', ':!*.md', ':!*.mdx', ':!**/__snapshots__/**')
    except subprocess.CalledProcessError as failure:
        if failure.returncode == 1 and not failure.stderr:
            return {}
        raise
    linking_files = collections.defaultdict(set)
    for line in output.splitlines():
        name, separator, match = line.partition('\0')
        if not separator or not name.startswith(f'{revision}:'):
            raise ValueError(f'{monorepo_root}: unparseable git grep line {line!r}')
        linking_files[normalized_route(match.removeprefix('posthog.com'))].add(name.removeprefix(f'{revision}:'))
    return linking_files


def call_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def assigned_value(body, name):
    for statement in reversed(body):
        if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
            return statement.value
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == name and statement.value:
            return statement.value
    return None


def imported_module_paths(import_node, importing_path):
    module_parts = import_node.module.split('.') if import_node.module else []
    if import_node.level == 0:
        base = PurePosixPath(*module_parts)
        return [f'{base}.py', f'{base}/__init__.py']
    package = PurePosixPath(importing_path).parents[import_node.level - 1]
    base = package.joinpath(*module_parts)
    return [f'{base}.py', f'{base}/__init__.py']


def imported_name_origin(module, name):
    for statement in module.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        for alias in statement.names:
            if (alias.asname or alias.name) == name:
                return statement, alias.name
    return None, None


class ModuleReader:
    def __init__(self, checkout, revision):
        self.checkout = checkout
        self.revision = revision

    def parsed(self, candidate_paths):
        for candidate_path in candidate_paths:
            shown = subprocess.run(
                ['git', '-C', str(self.checkout), 'cat-file', '-p', f'{self.revision}:{candidate_path}'], capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS, env=git_environment(),
            )
            if shown.returncode == 0:
                return candidate_path, ast.parse(shown.stdout.decode('utf-8'), filename=candidate_path)
        return None, None


def resolved_name(name, module, path, module_reader):
    value = assigned_value(module.body, name)
    if value is not None:
        return resolved_docs_url(value, module, None, path, module_reader)
    import_node, original_name = imported_name_origin(module, name)
    if import_node is None:
        raise ValueError(f'{path}: cannot resolve {name!r}: neither assigned nor imported at module level')
    imported_path, imported_module = module_reader.parsed(imported_module_paths(import_node, path))
    if imported_module is None:
        raise ValueError(f'{path}: cannot resolve {name!r}: module {import_node.module!r} is not in the tree')
    return resolved_name(original_name, imported_module, imported_path, module_reader)


def resolved_docs_url(expression, module, enclosing_class, path, module_reader):
    if isinstance(expression, ast.Constant) and (expression.value is None or isinstance(expression.value, str)):
        return expression.value
    if isinstance(expression, ast.Name):
        return resolved_name(expression.id, module, path, module_reader)
    is_self_attribute = isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name) and expression.value.id == 'self'
    class_value = assigned_value(enclosing_class.body, expression.attr) if is_self_attribute and enclosing_class is not None else None
    if class_value is not None:
        return resolved_docs_url(class_value, module, None, path, module_reader)
    raise ValueError(f'{path}: cannot resolve docsUrl expression {ast.unparse(expression)!r} to a string or None')


def connector_record(path, text, module_reader):
    module = ast.parse(text, filename=path)
    connector = path.split('/')[-2]
    classes_and_configs = [
        (class_node, node) for class_node in ast.walk(module) if isinstance(class_node, ast.ClassDef)
        for node in ast.walk(class_node) if isinstance(node, ast.Call) and call_name(node) == 'SourceConfig'
    ]
    if len(classes_and_configs) != 1:
        raise ValueError(f'{path}: expected one SourceConfig call inside a class, found {len(classes_and_configs)}')
    enclosing_class, config = classes_and_configs[0]
    docs_url_keyword = next((keyword for keyword in config.keywords if keyword.arg == 'docsUrl'), None)
    splatted_keywords = [ast.unparse(keyword.value) for keyword in config.keywords if keyword.arg is None]
    if docs_url_keyword is None and splatted_keywords:
        raise ValueError(f'{path}: SourceConfig receives **{splatted_keywords[0]}, so its docsUrl cannot be decided')
    if docs_url_keyword is None:
        return {'connector': connector, 'bucket': 'no-docs-url', 'docs_url': None}
    docs_url = resolved_docs_url(docs_url_keyword.value, module, enclosing_class, path, module_reader)
    if docs_url is None:
        return {'connector': connector, 'bucket': 'docs-url-none', 'docs_url': None}
    if POSTHOG_DOCS_URL_PATTERN.match(docs_url):
        return {'connector': connector, 'bucket': 'posthog-docs', 'docs_url': docs_url}
    return {'connector': connector, 'bucket': 'docs-url-elsewhere', 'docs_url': docs_url}


def connector_facts(monorepo_root, revision):
    source_paths = [path for path in tree_paths(monorepo_root, revision, CONNECTOR_SOURCES_DIRECTORY) if CONNECTOR_SOURCE_PATH_PATTERN.fullmatch(path)]
    if not source_paths:
        raise ValueError(f'{monorepo_root}: no connector source.py files under {CONNECTOR_SOURCES_DIRECTORY} at {revision}')
    source_texts = read_blobs(monorepo_root, revision, source_paths)
    module_reader = ModuleReader(monorepo_root, revision)
    records = [connector_record(path, source_texts[path], module_reader) for path in source_paths]
    bucket_counts = collections.Counter(record['bucket'] for record in records)
    return {
        'source_files': len(source_paths),
        'by_bucket': {bucket: bucket_counts[bucket] for bucket in CONNECTOR_BUCKETS},
        'docs_url_elsewhere': [{'connector': record['connector'], 'docs_url': record['docs_url']} for record in records if record['bucket'] == 'docs-url-elsewhere'],
        'docs_url_none': [record['connector'] for record in records if record['bucket'] == 'docs-url-none'],
        'no_docs_url': [record['connector'] for record in records if record['bucket'] == 'no-docs-url'],
    }


def duplicate_title_groups(pages):
    urls_by_title = collections.defaultdict(list)
    for page in pages:
        if page['title'] is not None:
            urls_by_title[page['title'].strip().lower()].append(page['url'])
    return [{'title': title, 'urls': sorted(urls)} for title, urls in sorted(urls_by_title.items()) if len(urls) > 1]


def line_shares(path, history, commits):
    referenced_commits = {history['creation_commit'], *history['nonblank_lines_by_commit']}
    commits_outside_head = sorted(referenced_commits - commits.keys())
    if commits_outside_head:
        raise ValueError(f'{path}: blame or creation names commits missing from the revision history: {commits_outside_head[:3]}')
    line_counts = history['nonblank_lines_by_commit']
    agent_lines = sum(count for revision, count in line_counts.items() if commits[revision]['is_agent'])
    return agent_lines, sum(line_counts.values())


def require_unique_urls(url_by_path):
    paths_by_url = collections.defaultdict(list)
    for path, url in url_by_path.items():
        paths_by_url[url].append(path)
    colliding = {url: paths for url, paths in paths_by_url.items() if len(paths) > 1}
    if colliding:
        raise ValueError(f'Several docs pages share one url: {dict(sorted(colliding.items())[:5])}')


def is_sitemap_template_route(route):
    return any(SITEMAP_TEMPLATE_SEGMENT_PATTERN.search(segment) for segment in route.split('/'))


def require_no_template_segments_in_source_urls(urls):
    templated_urls = sorted(url for url in urls if is_sitemap_template_route(url))
    if templated_urls:
        raise ValueError(f'Source files map to urls with template segments: {templated_urls[:5]}')


def require_one_row_per_live_docs_route(live_docs_routes, pages):
    rows_by_url = collections.defaultdict(list)
    for page in pages:
        rows_by_url[page['url']].append(page)
    unaccounted_routes = sorted(route for route in live_docs_routes if route not in rows_by_url)
    if unaccounted_routes:
        raise ValueError(f'{len(unaccounted_routes)} live /docs routes have no inventory row: {unaccounted_routes[:5]}')
    ambiguous_routes = sorted(
        route for route in live_docs_routes
        if len(rows_by_url[route]) > 1 and any(row['page_class'] != 'contested-route' for row in rows_by_url[route])
    )
    if ambiguous_routes:
        raise ValueError(f'Live /docs routes with several uncontested rows: {ambiguous_routes[:5]}')
    routed_urls = {page['url'] for page in pages if is_docs_route(page['url']) and page['page_class'] != 'not-a-route'}
    if routed_urls != live_docs_routes:
        raise ValueError(f'Routed /docs rows and live /docs routes differ: {sorted(routed_urls ^ live_docs_routes)[:5]}')


def source_page_row(source, path, url, text, history, commits, family_sizes, route_facts):
    fingerprint = structural_fingerprint(text)
    template_family_size = family_sizes[fingerprint] if fingerprint is not None else 1
    is_templated = fingerprint is not None and template_family_size >= TEMPLATE_FAMILY_MIN_SIZE
    agent_lines, nonblank_lines = line_shares(path, history, commits)
    return {
        **source_route_facts(source, path, url, text, route_facts),
        'doc_kind': doc_kind(is_templated, agent_lines, nonblank_lines),
        'created_at': commits[history['creation_commit']]['authored_date'],
        'agent_line_share': round(agent_lines / nonblank_lines, 4) if nonblank_lines else 0.0,
        'nonblank_lines': nonblank_lines,
        'template_family_size': template_family_size,
    }


def source_route_facts(source, path, url, text, route_facts):
    is_route = url in route_facts['live_routes']
    is_redirected_away = route_facts['redirects'].is_redirected_away(url)
    return {
        'url': url,
        'source_repo': source,
        'path': path,
        'title': frontmatter_title(path, text),
        'page_class': page_class(is_route, url in route_facts['contested_urls'], is_redirected_away),
        'is_route': is_route,
        'redirected_away': is_redirected_away,
        **route_link_facts(url, path, route_facts),
    }


def sourceless_route_row(url, route_facts):
    is_template = is_sitemap_template_route(url)
    return {
        'url': url,
        'source_repo': None,
        'path': None,
        'title': None,
        'page_class': 'sitemap-template-route' if is_template else 'generated-route',
        'doc_kind': 'not a page' if is_template else 'generated page',
        'is_route': True,
        'redirected_away': route_facts['redirects'].is_redirected_away(url),
        'created_at': None,
        'agent_line_share': None,
        'nonblank_lines': None,
        'template_family_size': None,
        **route_link_facts(url, None, route_facts),
    }


def route_link_facts(url, path, route_facts):
    return {
        'site_inbound_links': len(route_facts['site_linking_files'].get(url, set()) - {path}),
        'in_nav': url in route_facts['routes_in_nav'],
        'redirects_into': route_facts['redirects'].redirects_into(url),
        'app_links': len(route_facts['app_linking_files'].get(url, ())),
    }


def ordered_page_row(row):
    return {column: row[column] for column in PAGE_COLUMNS}


def build_inventory(site_root, monorepo_root, live_routes_path, cache_path, monorepo_cache_path):
    site_commit = git_text(site_root, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    monorepo_commit = git_text(monorepo_root, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
    site_paths = tree_paths(site_root, site_commit, 'contents', 'src', 'vercel.json')
    site_url_by_path = {path: page_url(path) for path in sorted(site_paths) if path.startswith(DOCS_DIRECTORY + '/') and is_docs_page_path(path)}
    monorepo_url_by_path = {
        path: monorepo_page_url(path) for path in sorted(tree_paths(monorepo_root, monorepo_commit, MONOREPO_PUBLISHED_DIRECTORY))
        if is_docs_page_path(path) and is_docs_route(monorepo_page_url(path))
    }
    for url_by_path in (site_url_by_path, monorepo_url_by_path):
        require_unique_urls(url_by_path)
        require_no_template_segments_in_source_urls(url_by_path.values())
    linking_paths = [path for path in site_paths if is_linking_file(path)]
    nav_paths = [path for path in site_paths if is_nav_file(path)]
    site_texts = read_blobs(site_root, site_commit, sorted({*site_url_by_path, *linking_paths, *nav_paths, 'vercel.json'}))
    monorepo_texts = read_blobs(monorepo_root, monorepo_commit, list(monorepo_url_by_path))
    live_routes = read_live_routes(live_routes_path)
    site_source_texts = {path: site_texts[path] for path in [*linking_paths, *nav_paths] if path.startswith('src/')}
    nav_texts = {path: site_texts[path] for path in nav_paths}
    route_facts = {
        'live_routes': live_routes,
        'contested_urls': set(site_url_by_path.values()) & set(monorepo_url_by_path.values()),
        'redirects': Redirects(site_texts['vercel.json']),
        'site_linking_files': linking_files_by_route({path: site_texts[path] for path in linking_paths}),
        'routes_in_nav': nav_routes(nav_texts.values()) | nav_hook_routes(nav_texts, site_source_texts, live_routes),
        'app_linking_files': app_link_files_by_route(monorepo_root, monorepo_commit),
    }
    site_histories, reblamed_site_paths = page_histories(site_root, site_commit, DOCS_DIRECTORY, list(site_url_by_path), cache_path)
    monorepo_histories, reblamed_monorepo_paths = page_histories(
        monorepo_root, monorepo_commit, MONOREPO_PUBLISHED_DIRECTORY, list(monorepo_url_by_path), monorepo_cache_path,
    )
    sources = [
        (SITE_REPOSITORY, site_url_by_path, site_texts, site_histories, read_commit_index(site_root, site_commit)),
        (MONOREPO_REPOSITORY, monorepo_url_by_path, monorepo_texts, monorepo_histories, read_commit_index(monorepo_root, monorepo_commit)),
    ]
    fingerprints = [structural_fingerprint(texts[path]) for _, url_by_path, texts, _, _ in sources for path in url_by_path]
    family_sizes = collections.Counter(fingerprint for fingerprint in fingerprints if fingerprint is not None)
    pages = [
        source_page_row(source, path, url, texts[path], histories[path], commits, family_sizes, route_facts)
        for source, url_by_path, texts, histories, commits in sources for path, url in url_by_path.items()
    ]
    live_docs_routes = {route for route in live_routes if is_docs_route(route)}
    source_urls = {page['url'] for page in pages}
    pages.extend(sourceless_route_row(url, route_facts) for url in sorted(live_docs_routes - source_urls))
    pages = [ordered_page_row(page) for page in pages]
    require_one_row_per_live_docs_route(live_docs_routes, pages)
    doc_kind_counts = collections.Counter(page['doc_kind'] for page in pages)
    page_class_counts = collections.Counter(page['page_class'] for page in pages)
    inventory = {
        'format_version': INVENTORY_FORMAT_VERSION,
        'site_commit': site_commit,
        'monorepo_commit': monorepo_commit,
        'page_columns': list(PAGE_COLUMNS),
        'pages': pages,
        'counts': {
            'pages_total': len(pages),
            'by_doc_kind': {kind: doc_kind_counts[kind] for kind in DOC_KINDS},
            'by_page_class': {name: page_class_counts[name] for name in PAGE_CLASSES},
            'redirected_away': sum(page['redirected_away'] for page in pages),
            'template_families': sum(1 for size in family_sizes.values() if size >= TEMPLATE_FAMILY_MIN_SIZE),
        },
        'sitemap_template_routes': sorted(page['url'] for page in pages if page['page_class'] == 'sitemap-template-route'),
        'connectors': connector_facts(monorepo_root, monorepo_commit),
        'duplicate_title_groups': duplicate_title_groups(pages),
    }
    return inventory, [*reblamed_site_paths, *reblamed_monorepo_paths]


def render_inventory(inventory):
    return json.dumps(inventory, indent=1, sort_keys=True, ensure_ascii=False) + '\n'


def main():
    started_at = time.monotonic()
    inventory, reblamed_paths = build_inventory(
        doccoverage.SITE_ROOT, doccoverage.MONOREPO_ROOT, doccoverage.LIVE_ROUTES_FILE, CACHE_FILE, MONOREPO_CACHE_FILE,
    )
    output_path = doccoverage.OUTPUT_ROOT / INVENTORY_FILE_NAME
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_inventory(inventory), encoding='utf-8')
    page_count = inventory['counts']['pages_total']
    source_page_count = sum(1 for page in inventory['pages'] if page['source_repo'] is not None)
    print(
        f'Wrote {page_count} docs rows to {output_path}; re-blamed {len(reblamed_paths)}, '
        f'reused {source_page_count - len(reblamed_paths)} from cache, {time.monotonic() - started_at:.1f}s'
    )


if __name__ == '__main__':
    main()
