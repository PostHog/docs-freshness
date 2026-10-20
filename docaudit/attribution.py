import argparse
import collections
import datetime
import functools
import json
import posixpath
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import doccoverage
import history_index
from history_index import HISTORY_PATHS, literal_constraints
from next_steps import changelog_gap_bucket
from impact import REPOSITORIES, SETTINGS_MAP, SETTINGS_SCENE_LOGIC, SITE_GATE_KINDS, checker, gate_rechecks_change, git, git_environment, reference_from_finding

HISTORY_TIMEOUT_SECONDS = None
CACHE_FILE = doccoverage.TOOL_ROOT / 'data/attribution-cache.json'
CACHE_VERSION = 6
CACHE_SECTIONS = ('sources', 'claims', 'gaps', 'presence')
SETTINGS_SOURCE_PATHS = (SETTINGS_MAP, SETTINGS_SCENE_LOGIC)
CATEGORIES = ('introduced_upstream', 'introduced_by_docs_change', 'wrong_when_written', 'shipped_undocumented', 'docs_removed')
LABEL_PATHS = HISTORY_PATHS['monorepo']
LABEL_KINDS = ('ui-label', 'ui-label-minor')
COUNTED_CHANGELOG_GAP_BUCKET = 'public-api'
SITE_KINDS = ('docs-link', 'docs-fragment')
SETTING_GAP_SOURCES = ('settings', 'settings-sections')
STRUCTURED_GAP_SOURCES = ('js-config', 'python-config')
COMMENT_PREFIXES = ('//', '/*', '*', '#')
LEGACY_PAGE_SUFFIXES = ('.tsx', '.js', '.jsx', '.md', '.mdx')
HISTORY_MARKER = 'DOCAUDIT-COMMIT:'
DOCS_HISTORY_PATHS = ['contents/docs', 'src/pages/docs']
PAGE_SOURCE_PATHS = ('contents', 'src/pages')
PREFETCH_BATCH_SIZE = 2000
GITLINK_MODE = '160000'
MISSING_BLOB_REASON = 'blobs not prefetched; run python3 attribution.py --prefetch'
MISSING_BLOB_MESSAGES = ('lazy fetching disabled', 'from promisor remote')
REDIRECT_RULES_BY_BLOB = {}
TREE_OBJECTS_BY_REVISION = {}
BLOB_TEXTS = {}
PRELOADED_SETTINGS_TEXTS = {}
TREE_MODE = '40000'


class MissingBlobError(ValueError):
    pass


def source_repository(kind, claim):
    if kind in ('ui-label', 'ui-label-minor', 'app-url', 'settings-anchor'):
        return 'monorepo'
    if kind == 'sdk-method':
        return 'posthog-python' if claim.startswith('python:') else 'posthog-js'
    if kind in ('docs-link', 'docs-fragment'):
        return 'site'
    return None


def finding_reference(finding):
    reference = reference_from_finding(finding['page'], finding['kind'], finding['detail'])
    if reference is not None and finding['kind'] in LABEL_KINDS and finding['page'].startswith(checker().DESKTOP_DOCS_PREFIX):
        return {**reference, 'kind': finding['kind'], 'is_desktop_page': True}
    if reference is not None:
        return {**reference, 'kind': finding['kind']}
    if finding['kind'] not in ('docs-link', 'docs-fragment'):
        return None
    written_claim = finding['detail'].split(' (', 1)[0]
    target_claim = written_claim
    if target_claim.startswith('#'):
        target_claim = '/docs/' + str(Path(finding['page']).with_suffix('')).removesuffix('/index') + target_claim
    return {'kind': finding['kind'], 'claim': target_claim, 'written_claim': written_claim}


def single_range(revision_range):
    if len(revision_range) > 1:
        raise ValueError(f'Expected at most one revision range, got {revision_range!r}')
    return next(iter(revision_range), None)


def history_commits(history):
    for commit_text in history.split(HISTORY_MARKER)[1:]:
        header, _, patch_text = commit_text.partition('\n')
        revision, timestamp = header.strip().split(':')
        yield {'revision': revision, 'timestamp': int(timestamp)}, patch_text


def event_with_file(commit, file):
    return {**commit, 'date': datetime.datetime.fromtimestamp(commit['timestamp'], datetime.timezone.utc).date().isoformat(), 'file': file}


def patch_files(patch_text):
    for file_patch in patch_text.split('diff --git ')[1:]:
        header, _, contents = file_patch.partition('\n')
        file = header.split(' b/', 1)[-1].strip('"')
        removed_lines = [line[1:] for line in contents.splitlines() if line.startswith('-') and not line.startswith('---')]
        added_lines = [line[1:] for line in contents.splitlines() if line.startswith('+') and not line.startswith('+++')]
        previous_lines = [line[1:] for line in contents.splitlines() if line.startswith((' ', '-')) and not line.startswith('---')]
        current_lines = [line[1:] for line in contents.splitlines() if line.startswith((' ', '+')) and not line.startswith('+++')]
        yield file, contents, removed_lines, added_lines, '\n'.join(previous_lines), '\n'.join(current_lines)


def content_candidates(route):
    relative = route.removesuffix('.md').strip('/')
    if not relative:
        return []
    base = Path('contents') / relative
    return [str(base.parent / f'{base.name}.md'), str(base.parent / f'{base.name}.mdx'), str(base / 'index.md'), str(base / 'index.mdx')]


def route_candidate_paths(route):
    legacy_base = Path('src/pages') / route.strip('/')
    legacy_candidates = [str(legacy_base) + suffix for suffix in LEGACY_PAGE_SUFFIXES]
    legacy_candidates += [str(legacy_base / ('index' + suffix)) for suffix in LEGACY_PAGE_SUFFIXES]
    return content_candidates(route) + legacy_candidates


def normalized_site_route(route):
    route = re.sub(r'^https?://(?:www\.)?posthog\.com(?=[/#?]|$)', '', route)
    if re.match(r'^[a-z]+://', route):
        return None
    return route.split('#', 1)[0].split('?')[0].rstrip('/') or '/'


def cat_file_batch(checkout, requests, reads_contents=True):
    if not requests:
        return []
    found = subprocess.run(
        ['git', '-C', str(checkout), 'cat-file', '--batch' if reads_contents else '--batch-check'],
        input=''.join(f'{request}\n' for request in requests).encode(),
        capture_output=True, check=True, env=git_environment(),
    ).stdout
    objects = []
    position = 0
    for request in requests:
        header_end = found.index(b'\n', position)
        header = found[position:header_end].decode()
        position = header_end + 1
        if header.endswith(' missing'):
            objects.append((None, None, None))
            continue
        fields = header.split()
        if len(fields) != 3:
            raise ValueError(f'Invalid historical object {request}: {header!r}')
        object_id, object_type, size = fields
        contents = found[position:position + int(size)] if reads_contents else None
        position += int(size) + 1 if reads_contents else 0
        objects.append((object_id, object_type, contents))
    return objects


def blob_texts(checkout, object_ids):
    texts = {}
    for object_id, (_, object_type, contents) in zip(object_ids, cat_file_batch(checkout, object_ids)):
        if object_type is None:
            raise MissingBlobError(f'Blob {object_id} is not fetched')
        texts[object_id] = contents.decode('utf-8', errors='ignore')
    return texts


@functools.lru_cache(maxsize=None)
def compiled_redirect(source, destination):
    try:
        pattern, wildcard_names = checker().vercel_pattern_to_regex(source)
    except re.error:
        return None
    return checker().Redirect(pattern, destination, wildcard_names)


def redirect_rules(checkout, object_ids):
    unparsed_object_ids = [object_id for object_id in dict.fromkeys(object_ids) if object_id not in REDIRECT_RULES_BY_BLOB]
    for object_id, text in (blob_texts(checkout, unparsed_object_ids) if unparsed_object_ids else {}).items():
        redirects = parse_json_with_trailing_commas(text).get('redirects', [])
        rules = ((redirect['source'], compiled_redirect(redirect['source'], redirect['destination'])) for redirect in redirects)
        REDIRECT_RULES_BY_BLOB[object_id] = tuple((source, redirect) for source, redirect in rules if redirect is not None)
    return [REDIRECT_RULES_BY_BLOB[object_id] for object_id in object_ids]


@functools.lru_cache(maxsize=64)
def vercel_object_ids(checkout, head):
    changes = history_index.path_history('site', checkout, ['vercel.json'], include_merges=False)
    found = cat_file_batch(checkout, [f"{change['revision']}:vercel.json" for change in changes], reads_contents=False)
    return tuple(sorted({object_id for object_id, object_type, _ in found if object_type == 'blob'}))


def redirected_route(route, redirect):
    return normalized_site_route(checker().apply_redirect(route, [redirect]).destination)


@functools.lru_cache(maxsize=1024)
def site_candidate_paths(checkout, head, claim):
    every_rule = {rule for rules in redirect_rules(checkout, vercel_object_ids(checkout, head)) for rule in rules}
    routes = [normalized_site_route(claim.split('#', 1)[0])]
    frontier = [route for route in routes if route is not None]
    for _ in range(checker().MAX_REDIRECT_HOPS):
        destinations = [redirected_route(route, redirect) for route in frontier for _, redirect in every_rule if redirect.pattern.match(route)]
        frontier = [route for route in dict.fromkeys(destinations) if route is not None and route not in routes]
        routes += frontier
    route_paths = tuple(dict.fromkeys(['vercel.json', *(path for route in routes if route is not None for path in route_candidate_paths(route))]))
    if '#' not in claim:
        return route_paths
    return (*route_paths, *imported_snippet_paths(checkout, route_paths[1:]))


def snippet_candidate_paths(importing_file, specifier):
    audit = checker()
    joined = posixpath.join(posixpath.dirname(importing_file), specifier) if specifier.startswith('.') else posixpath.join('contents/docs', specifier)
    base = posixpath.normpath(joined)
    return (base, *(base + suffix for suffix in (*audit.MARKDOWN_SUFFIXES, *audit.CODE_SUFFIXES)))


def is_code_snippet(path):
    return path.endswith(checker().CODE_SUFFIXES)


def imported_snippet_paths(checkout, page_paths):
    seen = set(page_paths)
    found = []
    frontier = tuple(page_paths)
    while frontier:
        changes = history_index.path_history('site', checkout, list(frontier), include_merges=False)
        revisions = [change['revision'] for change in changes]
        preload_tree_objects(checkout, revisions, frontier)
        versions = sorted({(path, object_id) for revision in revisions for path, object_id in TREE_OBJECTS_BY_REVISION[(checkout, revision, frontier)].items()})
        texts = cached_blob_texts(checkout, [object_id for _, object_id in versions])
        imported = [
            candidate for path, object_id in versions for specifier in checker().SNIPPET_IMPORT.findall(texts[object_id])
            for candidate in snippet_candidate_paths(path, specifier)
        ]
        new_paths = [path for path in dict.fromkeys(imported) if path not in seen]
        seen.update(new_paths)
        found.extend(new_paths)
        frontier = tuple(path for path in new_paths if not is_code_snippet(path))
    return found


def tree_entries(tree_contents, object_id_length):
    entries = {}
    position = 0
    while position < len(tree_contents):
        name_end = tree_contents.index(b'\0', position)
        mode, _, name = tree_contents[position:name_end].partition(b' ')
        object_id = tree_contents[name_end + 1:name_end + 1 + object_id_length].hex()
        entries[name.decode('utf-8')] = (mode.decode(), object_id)
        position = name_end + 1 + object_id_length
    return entries


def preload_tree_objects(checkout, revisions, paths):
    pending_revisions = [revision for revision in dict.fromkeys(revisions) if revision and (checkout, revision, paths) not in TREE_OBJECTS_BY_REVISION]
    if not pending_revisions:
        return
    directories = sorted({'', *(posixpath.dirname(path) for path in paths)})
    directory_requests = [(revision, directory) for revision in pending_revisions for directory in directories]
    directory_trees = cat_file_batch(checkout, [f'{revision}:{directory}' for revision, directory in directory_requests], reads_contents=False)
    tree_by_directory = {
        request: object_id for request, (object_id, object_type, _) in zip(directory_requests, directory_trees) if object_type == 'tree'
    }
    unreadable_revisions = [revision for revision in pending_revisions if (revision, '') not in tree_by_directory]
    if unreadable_revisions:
        raise ValueError(f'Root tree of {unreadable_revisions[:3]} is unreadable in {checkout}')
    tree_object_ids = list(dict.fromkeys(tree_by_directory.values()))
    entries_by_tree = {
        object_id: tree_entries(contents, len(object_id) // 2)
        for object_id, (_, _, contents) in zip(tree_object_ids, cat_file_batch(checkout, tree_object_ids))
    }
    for revision in pending_revisions:
        objects = {}
        for path in paths:
            directory, name = posixpath.split(path)
            tree_object_id = tree_by_directory.get((revision, directory))
            mode, object_id = entries_by_tree[tree_object_id].get(name, (TREE_MODE, None)) if tree_object_id else (TREE_MODE, None)
            if mode != TREE_MODE:
                objects[path] = object_id
        TREE_OBJECTS_BY_REVISION[(checkout, revision, paths)] = objects


def tree_objects(checkout, revision, paths):
    if not revision:
        return {}
    preload_tree_objects(checkout, [revision], paths)
    return TREE_OBJECTS_BY_REVISION[(checkout, revision, paths)]


def cached_blob_texts(checkout, object_ids):
    unread_object_ids = [object_id for object_id in dict.fromkeys(object_ids) if (checkout, object_id) not in BLOB_TEXTS]
    for object_id, text in (blob_texts(checkout, unread_object_ids) if unread_object_ids else {}).items():
        BLOB_TEXTS[(checkout, object_id)] = text
    return {object_id: BLOB_TEXTS[(checkout, object_id)] for object_id in object_ids}


@functools.lru_cache(maxsize=65536)
def own_anchor_ids_in_blob(checkout, object_id):
    return frozenset(checker().own_anchor_ids(cached_blob_texts(checkout, [object_id])[object_id]))


@functools.lru_cache(maxsize=65536)
def history_anchor_index(checkout, revision, candidate_paths, page_file, visited=()):
    audit = checker()
    objects = tree_objects(checkout, revision, candidate_paths)
    page_text = cached_blob_texts(checkout, [objects[page_file]])[objects[page_file]]
    anchors = dict.fromkeys(own_anchor_ids_in_blob(checkout, objects[page_file]), page_file)
    code_import = None
    consulted = [page_file]
    for specifier in audit.SNIPPET_IMPORT.findall(page_text):
        candidates = snippet_candidate_paths(page_file, specifier)
        unsearched = [candidate for candidate in candidates if candidate not in candidate_paths]
        if unsearched:
            raise ValueError(f'Snippet candidates {unsearched} of {page_file} at {revision} were not searched in history')
        consulted.extend(candidates)
        imported_file = next((candidate for candidate in candidates if candidate in objects), None)
        if imported_file is None or imported_file in visited:
            continue
        if is_code_snippet(imported_file):
            code_import = code_import or (page_file, specifier)
            continue
        imported = history_anchor_index(checkout, revision, candidate_paths, imported_file, (*visited, page_file))
        for anchor, defining_file in imported['anchors'].items():
            anchors.setdefault(anchor, defining_file)
        code_import = code_import or imported['code_import']
        consulted.extend(imported['consulted'])
    return {'anchors': anchors, 'code_import': code_import, 'consulted': tuple(dict.fromkeys(consulted))}


def site_target(kind, claim, checkout, revision, candidate_paths, content_file):
    if kind == 'docs-link':
        return {'resolves': True, 'is_unverifiable': False, 'file': content_file, 'specifier': None, 'consulted': (content_file,)}
    fragment = claim.split('#', 1)[1]
    index = history_anchor_index(checkout, revision, candidate_paths, content_file)
    if fragment in index['anchors']:
        return {'resolves': True, 'is_unverifiable': False, 'file': index['anchors'][fragment], 'specifier': None, 'consulted': index['consulted']}
    if index['code_import'] is not None:
        code_importer, specifier = index['code_import']
        return {'resolves': False, 'is_unverifiable': True, 'file': code_importer, 'specifier': specifier, 'consulted': index['consulted']}
    return {'resolves': False, 'is_unverifiable': False, 'file': None, 'specifier': None, 'consulted': index['consulted']}


@functools.lru_cache(maxsize=16)
def head_site_paths(checkout, head):
    return frozenset(path for path in git(checkout, 'ls-tree', '-r', '-z', '--name-only', head, '--', *PAGE_SOURCE_PATHS, timeout=None).split('\0') if path)


@functools.lru_cache(maxsize=None)
def is_generated_live_route(checkout, head, route):
    live_routes = checker().LIVE_ROUTES
    if route not in live_routes and route.removesuffix('.md') not in live_routes:
        return False
    head_paths = head_site_paths(checkout, head)
    return not any(path in head_paths for path in route_candidate_paths(route))


@functools.lru_cache(maxsize=65536)
def site_resolution(checkout, head, kind, claim, revision, candidate_paths):
    objects = tree_objects(checkout, revision, candidate_paths)
    route = normalized_site_route(claim.split('#', 1)[0])
    redirect_sources = []
    for _ in range(checker().MAX_REDIRECT_HOPS):
        if route is None:
            break
        content_file = next((path for path in route_candidate_paths(route) if path in objects), None)
        if content_file is not None:
            target = site_target(kind, claim, checkout, revision, candidate_paths, content_file)
            return {
                'objects': objects, 'content_file': content_file, 'redirect_sources': tuple(redirect_sources),
                'resolves': target['resolves'], 'is_unverifiable': target['is_unverifiable'], 'target': target,
            }
        if is_generated_live_route(checkout, head, route):
            return {'objects': objects, 'content_file': None, 'redirect_sources': tuple(redirect_sources), 'resolves': True, 'is_unverifiable': False, 'target': None}
        rule = first_redirect_rule(checkout, objects.get('vercel.json'), route)
        if rule is None:
            break
        redirect_sources.append(rule[0])
        route = redirected_route(route, rule[1])
    return {'objects': objects, 'content_file': None, 'redirect_sources': tuple(redirect_sources), 'resolves': False, 'is_unverifiable': False, 'target': None}


@functools.lru_cache(maxsize=262144)
def first_redirect_rule(checkout, vercel_object_id, route):
    if vercel_object_id is None:
        return None
    return next(((source, redirect) for source, redirect in redirect_rules(checkout, [vercel_object_id])[0] if redirect.pattern.match(route)), None)


def redirect_source_line(checkout, revision, resolution):
    if not resolution['redirect_sources']:
        raise ValueError(f'Target of a generated live route has no redirect line at {revision}')
    vercel_object_id = resolution['objects']['vercel.json']
    vercel_lines = cached_blob_texts(checkout, [vercel_object_id])[vercel_object_id].splitlines()
    line_number = next((
        number for number, line in enumerate(vercel_lines, start=1) if any(f'"{source}"' in line for source in resolution['redirect_sources'])
    ), None)
    if line_number is None:
        raise ValueError(f'Redirect sources {resolution["redirect_sources"]!r} have no line in vercel.json at {revision}')
    return line_number


def site_target_location(kind, claim, checkout, revision, resolution):
    target = resolution['target']
    if target is None:
        return 'vercel.json', redirect_source_line(checkout, revision, resolution)
    if kind == 'docs-link':
        return target['file'], 1
    object_id = resolution['objects'][target['file']]
    text = cached_blob_texts(checkout, [object_id])[object_id]
    line_number = anchor_definition_line(text, claim.split('#', 1)[1])
    if line_number is None:
        raise ValueError(f'Anchor of {claim!r} has no defining line in {target["file"]} blob {object_id}')
    return target['file'], line_number


def site_change_location(kind, claim, parent, previous, current):
    checkout = str(REPOSITORIES['site'])
    if previous['target'] is None:
        return 'vercel.json', {'revision': parent, 'file': 'vercel.json', 'line': redirect_source_line(checkout, parent, previous)}
    changed_paths = [path for path in previous['target']['consulted'] if current['objects'].get(path) != previous['objects'].get(path)]
    if changed_paths or not previous['redirect_sources']:
        before_file, line_number = site_target_location(kind, claim, checkout, parent, previous)
        changed_file = before_file if before_file in changed_paths else previous['content_file']
        return changed_file, {'revision': parent, 'file': before_file, 'line': line_number}
    return 'vercel.json', {'revision': parent, 'file': 'vercel.json', 'line': redirect_source_line(checkout, parent, previous)}


def site_change_reason(changed_file, current):
    if changed_file == 'vercel.json':
        return 'Redirect chain to the target changed or removed'
    if changed_file not in current['objects']:
        return 'Target content file deleted or moved'
    return 'Target heading or anchor removed or renamed'


def site_disappearance(kind, claim, revision_range=()):
    checkout = str(REPOSITORIES['site'])
    head = repository_head('site')
    candidate_paths = site_candidate_paths(checkout, head, claim)
    history = history_index.path_history('site', checkout, candidate_paths, single_range(revision_range))
    commits = [(change['revision'], change['timestamp'], change['parents']) for change in history]
    revisions = [revision for commit_revision, _, parents in commits for revision in (commit_revision, next(iter(parents.split()), ''))]
    preload_tree_objects(checkout, revisions, candidate_paths)
    if kind == 'docs-fragment':
        cached_blob_texts(checkout, [
            object_id for revision in dict.fromkeys(revisions) if revision
            for path, object_id in TREE_OBJECTS_BY_REVISION[(checkout, revision, candidate_paths)].items() if path != 'vercel.json'
        ])
    existed_in_history = False
    for revision, timestamp, parents in commits:
        parent = next(iter(parents.split()), '')
        current = site_resolution(checkout, head, kind, claim, revision, candidate_paths)
        previous = site_resolution(checkout, head, kind, claim, parent, candidate_paths)
        existed_in_history = existed_in_history or current['resolves'] or previous['resolves']
        if not previous['resolves'] or current['resolves']:
            continue
        if current['is_unverifiable']:
            raise ValueError(
                f'Disappearance of {claim!r} is undecidable: its anchor was last verifiably present at {parent}, '
                f'then {current["target"]["file"]} at {revision} imports code {current["target"]["specifier"]!r} that may generate it'
            )
        changed_file, before = site_change_location(kind, claim, parent, previous, current)
        commit = {'revision': revision, 'timestamp': int(timestamp)}
        return {'status': 'found', 'event': event_with_file(commit, changed_file), 'before': before, 'reason': site_change_reason(changed_file, current)}
    return {'status': 'not_found', 'existed_in_history': existed_in_history, 'reason': 'No disappearance found in full history'}


def site_location_at(kind, claim, revision):
    checkout = str(REPOSITORIES['site'])
    head = repository_head('site')
    candidate_paths = site_candidate_paths(checkout, head, claim)
    resolution = site_resolution(checkout, head, kind, claim, revision, candidate_paths)
    if not resolution['resolves']:
        return None
    file, line_number = site_target_location(kind, claim, checkout, revision, resolution)
    return {'revision': revision, 'file': file, 'line': line_number}


def blob_texts_at(checkout, revision, paths):
    paths = list(dict.fromkeys(paths))
    if not revision:
        return {path: '' for path in paths}
    texts = {}
    absent_paths = []
    for path, (_, object_type, contents) in zip(paths, cat_file_batch(checkout, [f'{revision}:{path}' for path in paths])):
        if object_type is None:
            absent_paths.append(path)
            texts[path] = ''
            continue
        if object_type != 'blob':
            raise ValueError(f'Invalid historical object {revision}:{path}: {object_type}')
        texts[path] = contents.decode('utf-8', errors='ignore')
    unfetched_listing = git(checkout, 'ls-tree', '--full-tree', '--name-only', revision, '--', *absent_paths, timeout=None).strip() if absent_paths else ''
    if unfetched_listing:
        raise MissingBlobError(f'Blobs {unfetched_listing.splitlines()} at {revision} exist in the tree but are not fetched')
    return texts


def blob_text(checkout, revision, path):
    return blob_texts_at(checkout, revision, [path])[path]


def preload_settings_texts(checkout, revisions):
    pending_revisions = [revision for revision in dict.fromkeys(revisions) if revision and (str(checkout), revision) not in PRELOADED_SETTINGS_TEXTS]
    found = cat_file_batch(checkout, [f'{revision}:{path}' for revision in pending_revisions for path in SETTINGS_SOURCE_PATHS])
    for position, revision in enumerate(pending_revisions):
        objects = found[position * len(SETTINGS_SOURCE_PATHS):(position + 1) * len(SETTINGS_SOURCE_PATHS)]
        if any(object_type != 'blob' for _, object_type, _ in objects):
            continue
        PRELOADED_SETTINGS_TEXTS[(str(checkout), revision)] = {
            path: contents.decode('utf-8', errors='ignore') for path, (_, _, contents) in zip(SETTINGS_SOURCE_PATHS, objects)
        }


@functools.lru_cache(maxsize=2048)
def settings_texts_at(checkout, revision):
    preloaded = PRELOADED_SETTINGS_TEXTS.get((str(checkout), revision))
    if preloaded is not None:
        return preloaded
    return {path: blob_text(checkout, revision, path) for path in SETTINGS_SOURCE_PATHS}


@functools.lru_cache(maxsize=2048)
def settings_scope_at(checkout, revision):
    texts = settings_texts_at(checkout, revision)
    return settings_scope_from_texts(str(checkout), texts[SETTINGS_MAP], texts[SETTINGS_SCENE_LOGIC])


@functools.lru_cache(maxsize=4096)
def settings_scope_from_texts(checkout, settings_map_text, scene_logic_text):
    return checker().settings_anchor_scope_from_texts(settings_map_text, scene_logic_text, Path(checkout) / SETTINGS_MAP)


def anchor_line(texts, anchor):
    definitions = (
        (SETTINGS_MAP, re.compile(rf"\bid:\s*['\"]{re.escape(anchor)}['\"]")),
        (SETTINGS_SCENE_LOGIC, re.compile(rf"['\"]{re.escape(anchor)}['\"]")),
    )
    for path, pattern in definitions:
        for line_number, line in enumerate(texts[path].splitlines(), start=1):
            if pattern.search(line):
                return path, line_number
    raise ValueError(f'Settings anchor {anchor!r} resolved but has no defining line')


def settings_disappearance(checkout, claim, revision_range=()):
    settings_path, anchor = claim.removeprefix('settings/').split('#', 1)
    history = history_index.path_history('monorepo', checkout, list(SETTINGS_SOURCE_PATHS), single_range(revision_range))
    preload_settings_texts(checkout, [revision for change in history for revision in (change['revision'], next(iter(change['parents'].split()), None))])
    existed_in_history = False
    for change in history:
        revision, timestamp, parents = change['revision'], change['timestamp'], change['parents']
        parent = next(iter(parents.split()), None)
        previously_resolved = anchor in checker().settings_anchor_ids_in_scope(settings_path, settings_scope_at(checkout, parent))
        currently_resolves = anchor in checker().settings_anchor_ids_in_scope(settings_path, settings_scope_at(checkout, revision))
        existed_in_history = existed_in_history or previously_resolved or currently_resolves
        if not previously_resolved or currently_resolves:
            continue
        previous_texts = settings_texts_at(checkout, parent)
        current_texts = settings_texts_at(checkout, revision)
        changed_file = next(path for path in SETTINGS_SOURCE_PATHS if previous_texts[path] != current_texts[path])
        before_file, before_line = anchor_line(previous_texts, anchor)
        commit = {'revision': revision, 'timestamp': int(timestamp)}
        return {
            'status': 'found', 'event': event_with_file(commit, changed_file), 'reason': 'Settings anchor stopped resolving in the cited section',
            'before': {'revision': parent, 'file': before_file, 'line': before_line},
        }
    return {'status': 'not_found', 'existed_in_history': existed_in_history, 'reason': 'Cited settings anchor never resolved in the cited section in full history'}


def js_property_assignment_pattern(identifier):
    escaped = re.escape(identifier)
    return re.compile(rf'(?:\.prototype|\bthis)(?:\.{escaped}(?![\w$])|\[\s*([\'"]){escaped}\1\s*\])\s*=(?![=>])')


def sdk_history_pattern(identifier, repository):
    escaped = re.escape(identifier)
    definition = rf'(^|[^[:alnum:]_]){escaped}[[:space:]]*(<[^>]*>)?[[:space:]]*[(=:]'
    if repository == 'posthog-python':
        return definition
    return definition + rf"""|(\.prototype|this)(\.{escaped}|\[[[:space:]]*['"]{escaped}['"][[:space:]]*\])[[:space:]]*="""


def sdk_definition_count(lines, identifier, repository):
    if repository == 'posthog-python':
        return sum(bool(re.match(rf'\s*(?:async\s+)?def\s+{re.escape(identifier)}\s*\(', line)) for line in lines)
    assignment = js_property_assignment_pattern(identifier)
    definitions = [doccoverage.METHOD_PATTERN.match(line) or doccoverage.ARROW_METHOD_PATTERN.match(line) for line in lines]
    return sum(
        (definition is not None and definition.group(1) == identifier) or bool(assignment.search(line))
        for definition, line in zip(definitions, lines)
    )


@functools.lru_cache(maxsize=None)
def head_label_property_values(checkout):
    return checker().label_property_values(Path(checkout))


def label_line_count(file, changed_lines, source_text, label, is_desktop_page=False):
    audit = checker()
    source = audit.TemplateSource(file, source_text, head_label_property_values(str(REPOSITORIES['monorepo'])))
    status, _ = audit.label_lookup_in_sources(label, [source], is_desktop_page)
    if status == 'missing':
        return 0
    label_pattern = re.compile(rf'(?<!\w){re.escape(label)}(?!\w)', re.I)
    return sum(
        bool(label_pattern.search(line)) and not line.lstrip().startswith(COMMENT_PREFIXES)
        for line in changed_lines
    )


def label_line_rank(line, label):
    escaped = re.escape(label)
    if line.lstrip().startswith(COMMENT_PREFIXES) or not re.search(rf'(?<!\w){escaped}(?!\w)', line, re.I):
        return None
    label_props = '|'.join(sorted(checker().VISIBLE_LABEL_PROPS))
    visible_text = rf'(?:^|>)\s*{escaped}\s*(?:<|\{{|$)|\b(?:{label_props})\s*[:=]\s*\{{?\s*([\'"`]){escaped}\1'
    whole_unit = rf'([\'"`]){escaped}\1'
    for case_rank, flags in enumerate((0, re.I)):
        if re.search(visible_text, line, flags):
            return case_rank * 2
        if re.search(whole_unit, line, flags):
            return case_rank * 2 + 1
    return 4


def best_label_location(revision, texts_by_file, label):
    ranked_lines = [
        (rank, file_index, line_number, file)
        for file_index, (file, text) in enumerate(texts_by_file.items())
        for line_number, line in enumerate(text.splitlines(), start=1)
        for rank in [label_line_rank(line, label)] if rank is not None
    ]
    if not ranked_lines:
        return None
    _, _, line_number, file = min(ranked_lines)
    return {'revision': revision, 'file': file, 'line': line_number}


def sdk_search_paths(repository, claim):
    if repository == 'posthog-python':
        return ['posthog']
    sdk = claim.split(':', 1)[0]
    packages = checker().JS_PACKAGES_BY_SDK.get(sdk)
    package_sources = [f'packages/{package}/src' for package in packages] if packages else [':(glob)packages/*/src/**']
    legacy_browser_sources = ['src'] if sdk == 'js-browser' or not packages else []
    return [*package_sources, *legacy_browser_sources]


def source_search_parameters(repository, kind, claim):
    if kind == 'sdk-method':
        needle = claim.split('posthog.')[-1]
        pattern = sdk_history_pattern(needle, repository)
        return {
            'needle': needle, 'paths': sdk_search_paths(repository, claim), 'log': ['-G', pattern], 'removal_log': ['-G', pattern], 'grep': ['-E', '-e', pattern],
            'alternatives': [literal_constraints(needle, is_word_bounded=True)], 'removal_alternatives': [literal_constraints(needle, is_word_bounded=True)],
        }
    if kind == 'app-url':
        needle = '/' + claim.strip('/')
        paths = ['frontend/src/scenes/urls.ts', 'frontend/src/scenes/scenes.ts', ':(glob)products/*/manifest.tsx']
        return {'needle': needle, 'paths': paths, 'log': ['-S', needle], 'removal_log': ['-S', needle], 'grep': ['-i', '-F', '-e', needle],
            'alternatives': [literal_constraints(needle)], 'removal_alternatives': [literal_constraints(needle)],
        }
    word_bounded_claim = rf'(^|[^[:alnum:]_]){re.escape(claim)}([^[:alnum:]_]|$)'
    return {
        'needle': claim, 'paths': list(LABEL_PATHS), 'log': ['-G', re.escape(claim), '--regexp-ignore-case'],
        'removal_log': ['-G', word_bounded_claim, '--regexp-ignore-case'], 'grep': ['-i', '-F', '-e', claim],
        'alternatives': [literal_constraints(claim)], 'removal_alternatives': [literal_constraints(claim, is_word_bounded=True)],
    }


def grep_files(checkout, revision, grep_arguments, paths):
    completed = subprocess.run(
        ['git', '-C', str(checkout), '-c', 'core.quotepath=off', 'grep', '-l', '-z', *grep_arguments, revision, '--', *paths],
        capture_output=True, env=git_environment(),
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
    return [entry.split(':', 1)[1] for entry in completed.stdout.decode('utf-8', errors='replace').split('\0') if entry]


def source_location_at(repository, kind, claim, revision, is_desktop_page=False):
    if not revision:
        return None
    if kind in SITE_KINDS:
        return site_location_at(kind, claim, revision)
    checkout = REPOSITORIES[repository]
    if kind == 'settings-anchor':
        settings_path, anchor = claim.removeprefix('settings/').split('#', 1)
        if anchor not in checker().settings_anchor_ids_in_scope(settings_path, settings_scope_at(checkout, revision)):
            return None
        file, line_number = anchor_line(settings_texts_at(checkout, revision), anchor)
        return {'revision': revision, 'file': file, 'line': line_number}
    parameters = source_search_parameters(repository, kind, claim)
    texts_by_file = blob_texts_at(checkout, revision, grep_files(checkout, revision, parameters['grep'], parameters['paths']))
    if kind in LABEL_KINDS:
        texts_by_file = {file: text for file, text in texts_by_file.items() if label_line_count(file, text.splitlines(), text, claim, is_desktop_page)}
        return best_label_location(revision, texts_by_file, claim)
    matches = before_line_matcher(repository, kind, claim)
    return next((
        {'revision': revision, 'file': file, 'line': line_number}
        for file, text in sorted(texts_by_file.items()) for line_number, line in enumerate(text.splitlines(), start=1) if matches(line)
    ), None)


def appearance_candidates(repository, kind, claim, revision_range):
    checkout = REPOSITORIES[repository]
    if kind in SITE_KINDS or kind == 'settings-anchor':
        paths = list(site_candidate_paths(str(REPOSITORIES['site']), repository_head('site'), claim)) if kind in SITE_KINDS else list(SETTINGS_SOURCE_PATHS)
        changes = history_index.path_history(repository, checkout, paths, revision_range, oldest_first=True)
        if kind in SITE_KINDS:
            preload_tree_objects(str(checkout), [change['revision'] for change in changes], tuple(paths))
        return [f"{change['revision']}:{change['timestamp']}" for change in changes]
    parameters = source_search_parameters(repository, kind, claim)
    history = history_index.pickaxe_log(
        repository, checkout, parameters['alternatives'], ['--format=%H:%ct', *parameters['log']], parameters['paths'], revision_range, oldest_first=True,
    )
    return history.splitlines()


def appearance_after(repository, kind, claim, revision, is_desktop_page=False):
    for line in appearance_candidates(repository, kind, claim, f'{revision}..HEAD' if revision else None):
        candidate, timestamp = line.split(':')
        location = source_location_at(repository, kind, claim, candidate, is_desktop_page)
        if location is None:
            continue
        return {'event': event_with_file({'revision': candidate, 'timestamp': int(timestamp)}, location['file']), 'location': location}
    return None


def revision_at(repository, timestamp):
    moment = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat()
    return git(REPOSITORIES[repository], 'rev-list', '-1', '--first-parent', f'--before={moment}', 'HEAD', timeout=None).strip()


def require_verifiable_fragment_at(kind, claim, revision):
    checkout = str(REPOSITORIES['site'])
    head = repository_head('site')
    resolution = site_resolution(checkout, head, kind, claim, revision, site_candidate_paths(checkout, head, claim))
    if not resolution['is_unverifiable']:
        return
    target = resolution['target']
    raise ValueError(
        f'Presence of {claim!r} when the claim was written is undecidable: at {revision} its anchor is absent '
        f'but {target["file"]} imports code {target["specifier"]!r} that may generate it'
    )


def presence_search(repository, kind, claim, written, is_desktop_page=False):
    revision = written['event']['revision'] if repository == 'site' else revision_at(repository, written['event']['timestamp'])
    head = repository_head(repository)
    location = source_location_at(repository, kind, claim, revision, is_desktop_page)
    if location is not None:
        return {'status': 'found', 'present': True, 'revision': revision, 'location': location, 'computed_at_head': head}
    appearance = appearance_after(repository, kind, claim, revision, is_desktop_page)
    return {'status': 'found', 'present': False, 'revision': revision, 'appearance': appearance, 'computed_at_head': head}


def removal_history(repository, kind, claim, revision_range=(), timeout=None):
    parameters = source_search_parameters(repository, kind, claim)
    return history_index.pickaxe_log(
        repository, REPOSITORIES[repository], parameters['removal_alternatives'], [f'--format={HISTORY_MARKER}%H:%ct', '-p', '-U3', *parameters['removal_log']],
        parameters['paths'], single_range(revision_range), timeout=timeout,
    )


def disappearance_search(repository, claim, kind='ui-label', timeout=HISTORY_TIMEOUT_SECONDS, history=None, revision_range=(), is_desktop_page=False):
    checkout = REPOSITORIES[repository]
    if kind == 'settings-anchor':
        return settings_disappearance(checkout, claim, revision_range)
    if kind in SITE_KINDS:
        return site_disappearance(kind, claim, revision_range)
    parameters = source_search_parameters(repository, kind, claim)
    needle = parameters['needle']
    if history is None:
        history = removal_history(repository, kind, claim, revision_range, timeout)
    existed_in_history = False
    for commit, patch_text in history_commits(history):
        matching_files = []
        removed_count = 0
        added_count = 0
        for file, contents, removed_lines, added_lines, previous_text, current_text in patch_files(patch_text):
            removed = sum(needle.casefold() in line.casefold() for line in removed_lines)
            added = sum(needle.casefold() in line.casefold() for line in added_lines)
            if kind in LABEL_KINDS:
                removed = label_line_count(file, removed_lines, previous_text, needle, is_desktop_page)
                added = label_line_count(file, added_lines, current_text, needle, is_desktop_page)
            if kind == 'sdk-method':
                removed = sdk_definition_count(removed_lines, needle, repository)
                added = sdk_definition_count(added_lines, needle, repository)
            existed_in_history = existed_in_history or bool(removed or added)
            removed_count += removed
            added_count += added
            if removed:
                matching_files.append(file)
        if removed_count <= added_count:
            continue
        if kind == 'sdk-method' and source_location_at(repository, kind, claim, commit['revision']) is not None:
            continue
        return {'status': 'found', 'event': event_with_file(commit, matching_files[0]), 'files': matching_files, 'reason': 'More matching source lines removed than added'}
    return {'status': 'not_found', 'existed_in_history': existed_in_history, 'reason': 'No disappearance found in full history'}


def anchor_definition_line(page_text, fragment):
    audit = checker()
    slug_counts = collections.Counter()
    for line_number, line in enumerate(audit.strip_code(page_text).splitlines(), start=1):
        heading = audit.HEADING_LINE.match(line)
        if not heading:
            if fragment in audit.own_anchor_ids(line):
                return line_number
            continue
        slug = audit.slugify_heading(heading.group(1))
        heading_anchor = f'{slug}-{slug_counts[slug]}' if slug_counts[slug] else slug
        slug_counts[slug] += 1
        if heading_anchor == fragment:
            return line_number
    return None


def before_line_matcher(repository, kind, claim):
    if kind == 'sdk-method':
        identifier = claim.split('posthog.')[-1]
        return lambda line: sdk_definition_count([line], identifier, repository) > 0
    if kind == 'app-url':
        needle = ('/' + claim.strip('/')).casefold()
        return lambda line: needle in line.casefold()
    label_pattern = re.compile(rf'(?<!\w){re.escape(claim)}(?!\w)', re.I)
    return lambda line: bool(label_pattern.search(line)) and not line.lstrip().startswith(COMMENT_PREFIXES)


def before_location(repository, kind, claim, found):
    checkout = REPOSITORIES[repository]
    parent = git(checkout, 'rev-parse', found['event']['revision'] + '^').strip()
    files = list(dict.fromkeys([found['event']['file'], *found.get('files', [])]))
    texts_by_file = {file: blob_text(checkout, parent, file) for file in files}
    if kind in LABEL_KINDS:
        location = best_label_location(parent, texts_by_file, claim)
        if location is None:
            raise ValueError(f'{kind} claim {claim!r} has no line in {files} at {parent}')
        return location
    matches = before_line_matcher(repository, kind, claim)
    for line_number, line in enumerate(texts_by_file[found['event']['file']].splitlines(), start=1):
        if matches(line):
            return {'revision': parent, 'file': found['event']['file'], 'line': line_number}
    raise ValueError(f'{kind} claim {claim!r} has no line in {found["event"]["file"]} at {parent}')


def source_change_search(repository, claim, kind='ui-label', history=None, revision_range=(), is_desktop_page=False):
    found = disappearance_search(repository, claim, kind=kind, history=history, revision_range=revision_range, is_desktop_page=is_desktop_page)
    if found['status'] != 'found' or 'before' in found:
        return found
    before = before_location(repository, kind, claim, found)
    return {**found, 'event': {**found['event'], 'file': before['file']}, 'before': before}


def parse_json_with_trailing_commas(text):
    tokens = re.compile(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])')
    normalized = tokens.sub(lambda match: match.group(1) or '', text)
    return json.loads(normalized)


def normalize_claim(text):
    return re.sub(r'\s+', ' ', re.sub(r'\\([\W_])', r'\1', text)).casefold().strip()


@functools.lru_cache(maxsize=512)
def page_history(checkout, page_file, head):
    changes = history_index.path_history('site', checkout, [page_file], include_merges=False, order='default')
    revisions = [change['revision'] for change in changes]
    return history_index.log_for_revisions(checkout, revisions, ['--no-renames', f'--format={HISTORY_MARKER}%H:%ct', '-p', '-U100000'], [page_file])


def claim_text_matcher(kind, normalized_claim):
    if kind not in LABEL_KINDS:
        return lambda normalized_text: normalized_claim in normalized_text
    leading_boundary = r'(?<!\w)' if re.match(r'\w', normalized_claim) else ''
    trailing_boundary = r'(?!\w)' if re.search(r'\w$', normalized_claim) else ''
    claim_pattern = re.compile(leading_boundary + re.escape(normalized_claim) + trailing_boundary)
    return lambda normalized_text: bool(claim_pattern.search(normalized_text))


def claim_written_search(finding, reference, timeout=HISTORY_TIMEOUT_SECONDS):
    written = claim_write_search(finding, reference, timeout)
    if finding['kind'] == 'docs-fragment':
        require_verifiable_fragment_at(finding['kind'], reference['claim'], written['event']['revision'])
    return written


def claim_write_search(finding, reference, timeout):
    page_file = 'contents/docs/' + finding['page']
    claim = reference.get('written_claim', reference['claim'])
    if finding['kind'] == 'sdk-method':
        claim = 'posthog.' + reference['claim'].split('posthog.')[-1]
    normalized_claim = normalize_claim(claim)
    exact = claim_write_event(page_file, claim_text_matcher(finding['kind'], normalized_claim), timeout)
    if exact is not None:
        return {**exact, 'claim_text': claim, 'rule': 'first_normalized_claim_in_page_lineage'}
    tokens = re.findall(r'[\w$]+', normalized_claim)
    if not tokens:
        raise ValueError(f'Claim has no key tokens: {claim!r}')
    key_tokens = [token for token in tokens if token not in {'docs', 'posthog', 'settings', 'project', 'organization', 'python', 'js'}]
    key_tokens = key_tokens or tokens
    fallback = claim_write_event(page_file, lambda normalized_text: set(key_tokens) <= set(re.findall(r'[\w$]+', normalized_text)), timeout)
    if fallback is None:
        raise ValueError(f'No page-lineage commit contains claim key tokens {key_tokens!r}')
    return {**fallback, 'claim_text': claim, 'key_tokens': key_tokens, 'rule': 'earliest_page_lineage_commit_with_all_key_tokens'}


def rename_source(revision, destination, timeout):
    return renamed_from('site', revision, destination, PAGE_SOURCE_PATHS, timeout)


def renamed_from(repository, revision, destination, searched_paths, timeout=None, follows_copies=False):
    checkout = REPOSITORIES[repository]
    source_filter = 'DM' if follows_copies else 'D'
    candidate_sources = [path for path in git(checkout, 'show', '--format=', '--name-only', '-z', '--no-renames', f'--diff-filter={source_filter}', revision, '--', *searched_paths, timeout=timeout).split('\0') if path]
    if not candidate_sources:
        return None
    detection = ['-M', '-C'] if follows_copies else ['-M']
    name_status = git(checkout, 'show', '--format=', '--name-status', '-z', *detection, revision, '--', destination, *candidate_sources, timeout=timeout)
    for status, paths in name_status_entries(name_status):
        if status.startswith(('R', 'C') if follows_copies else 'R') and paths[-1] == destination:
            return paths[0]
    return None


def name_status_entries(name_status):
    fields = name_status.lstrip('\n').split('\0')
    position = 0
    while position < len(fields) and fields[position]:
        status = fields[position]
        path_count = 2 if status.startswith(('R', 'C')) else 1
        yield status, fields[position + 1:position + 1 + path_count]
        position += 1 + path_count


def source_lineage_paths(repository, path, visited=()):
    if path in visited:
        raise ValueError(f'Source lineage cycle: {path}')
    paths = [path]
    additions = history_index.path_history(repository, REPOSITORIES[repository], [path], include_merges=False, status='A', order='default')
    for revision in [addition['revision'] for addition in additions]:
        previous_path = renamed_from(repository, revision, path, HISTORY_PATHS[repository], follows_copies=True)
        if previous_path is not None:
            paths += source_lineage_paths(repository, previous_path, (*visited, path))
    return paths


def claim_write_event(page_file, claim_matches, timeout, visited=()):
    if page_file in visited:
        raise ValueError(f'Page lineage cycle: {page_file}')
    history = page_history(REPOSITORIES['site'], page_file, git(REPOSITORIES['site'], 'rev-parse', 'HEAD').strip())
    for commit, patch_text in reversed(list(history_commits(history))):
        for file, contents, _, _, _, current_text in patch_files(patch_text):
            if not claim_matches(normalize_claim(current_text)):
                continue
            if 'new file mode' in contents:
                previous_path = rename_source(commit['revision'], file, timeout)
                if previous_path is not None:
                    previous_event = claim_write_event(previous_path, claim_matches, timeout, (*visited, page_file))
                    if previous_event is not None:
                        return previous_event
            return {'status': 'found', 'event': event_with_file(commit, file)}
    return None


def load_cache():
    cache = {'version': CACHE_VERSION}
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text())
    if cache.get('version') != CACHE_VERSION:
        cache = {'version': CACHE_VERSION}
    for section in CACHE_SECTIONS:
        cache.setdefault(section, {})
    return cache


def save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    candidate = CACHE_FILE.with_suffix('.partial')
    candidate.write_text(json.dumps(cache, indent=2, sort_keys=True) + '\n')
    candidate.replace(CACHE_FILE)


def repository_head(repository):
    return git(REPOSITORIES[repository], 'rev-parse', 'HEAD').strip()


def merged_source(cached, newer):
    if newer['status'] == 'error':
        return newer
    if newer['status'] == 'found' and (cached['status'] != 'found' or newer['event']['timestamp'] >= cached['event']['timestamp']):
        return newer
    if cached['status'] == 'found':
        return cached
    return {**cached, 'existed_in_history': cached['existed_in_history'] or newer['existed_in_history']}


def revalidated_source(repository, claim, cached, kind='ui-label', is_desktop_page=False):
    newer = source_change_search(repository, claim, kind=kind, revision_range=(f"{cached['computed_at_head']}..HEAD",), is_desktop_page=is_desktop_page)
    return merged_source(cached, newer)


def label_scope_key(reference):
    return ['desktop-page'] if reference.get('is_desktop_page') else []


def source_cache_key(repository, reference):
    return json.dumps([repository, reference['kind'], reference['claim'], *label_scope_key(reference)])


def claim_cache_key(finding, reference):
    return json.dumps(['site', reference['kind'], finding['page'], reference.get('written_claim', reference['claim'])])


def history_failure_reason(failure):
    stderr = getattr(failure, 'stderr', '') or ''
    message = stderr.decode(errors='replace') if isinstance(stderr, bytes) else str(stderr)
    if isinstance(failure, MissingBlobError) or any(marker in message for marker in MISSING_BLOB_MESSAGES):
        return MISSING_BLOB_REASON
    return f'History evidence unavailable: {failure}; {message.strip()}'


def run_history(search, *arguments, **options):
    try:
        return search(*arguments, **options)
    except (OSError, ValueError, KeyError, TypeError, IndexError, re.error, subprocess.SubprocessError) as failure:
        return {'status': 'error', 'reason': history_failure_reason(failure)}


def missing_history_blobs(checkout, paths, verified_head=None):
    revision_range = f'{verified_head}..HEAD' if verified_head else 'HEAD'
    raw_history = git(checkout, 'log', '--full-history', '--format=', '--raw', '-z', '--no-abbrev', '--no-renames', revision_range, '--', *paths, timeout=1800)
    object_ids = set()
    for field in raw_history.split('\0'):
        if not field.startswith(':'):
            continue
        old_mode, new_mode, old_object_id, new_object_id = field[1:].split()[:4]
        for mode, object_id in ((old_mode, old_object_id), (new_mode, new_object_id)):
            if mode != GITLINK_MODE and object_id.strip('0'):
                object_ids.add(object_id)
    if not object_ids:
        return []
    object_checks = subprocess.run(
        ['git', '-C', str(checkout), 'cat-file', '--batch-check'], input=''.join(f'{object_id}\n' for object_id in sorted(object_ids)).encode(),
        capture_output=True, check=True, timeout=1800, env=git_environment(),
    ).stdout.decode()
    return [line.split()[0] for line in object_checks.splitlines() if line.endswith(' missing')]


def prefetch_history_blobs(repository):
    checkout = REPOSITORIES[repository]
    history_paths = list(HISTORY_PATHS[repository])
    missing_object_ids = missing_history_blobs(checkout, history_paths, history_index.indexed_head(repository, checkout))
    for batch_start in range(0, len(missing_object_ids), PREFETCH_BATCH_SIZE):
        subprocess.run(
            ['git', '-C', str(checkout), '-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '-q', 'origin', '--no-tags', '--no-write-fetch-head',
             '--recurse-submodules=no', '--filter=blob:none', *missing_object_ids[batch_start:batch_start + PREFETCH_BATCH_SIZE]],
            capture_output=True, check=True, timeout=1800,
        )
    return len(missing_object_ids)


def classify_attribution(repository, source, written, presence=None):
    for history in (source, written):
        if history['status'] == 'error':
            raise ValueError(history['reason'])
    evidence = {'repository': repository, 'claim_written_at': written, 'source_changed_at': source.get('event'), 'reason': source['reason']}
    if source['status'] == 'found' and 'before' not in source:
        raise ValueError('Source change lacks its before-change location')
    if source['status'] == 'found' and source['event']['timestamp'] <= written['event']['timestamp']:
        return {
            **evidence, 'before': source['before'], 'category': 'wrong_when_written', 'rule': 'source_removed_at_or_before_claim',
            'evidence': [written['event'], source['event']],
        }
    if source['status'] == 'found' or source.get('existed_in_history'):
        return classify_by_presence(repository, source, written, presence, evidence)
    return {**evidence, 'category': 'wrong_when_written', 'rule': 'source_never_existed_in_full_history', 'evidence': [written['event']]}


def classify_by_presence(repository, source, written, presence, evidence):
    if presence is None:
        raise ValueError('Presence of the cited source when the claim was written was not computed')
    if presence['present'] and source['status'] == 'found':
        category = 'introduced_by_docs_change' if repository == 'site' else 'introduced_upstream'
        return {
            **evidence, 'before': source['before'], 'category': category, 'rule': 'source_removed_after_claim',
            'evidence': [written['event'], source['event']],
        }
    if presence['present']:
        raise ValueError('Source existed when the claim was written but no disappearance was found in full history')
    appearance = presence.get('appearance')
    if appearance is None and source['status'] == 'found':
        raise ValueError('Source was absent when the claim was written and no later appearance precedes its removal')
    absent = {
        **evidence, 'category': 'wrong_when_written', 'rule': 'source_absent_when_claim_written',
        'reason': 'Cited source absent when the claim was written', 'presence_checked_at': presence['revision'],
        'source_changed_at': None, 'evidence': [written['event']],
    }
    if appearance is None:
        return absent
    return {**absent, 'source_changed_at': appearance['event'], 'before': appearance['location'], 'evidence': [written['event'], appearance['event']]}


def needs_presence(source, written):
    if source['status'] == 'found':
        return source['event']['timestamp'] > written['event']['timestamp']
    return bool(source.get('existed_in_history'))


def presence_cache_key(repository, reference, written):
    return json.dumps([repository, reference['kind'], reference['claim'], written['event']['revision'], *label_scope_key(reference)])


def is_settled_presence(cached_presence, head):
    if cached_presence is None:
        return False
    return cached_presence['present'] or cached_presence.get('appearance') is not None or cached_presence['computed_at_head'] == head


def attribute_presence(cache, prepared_findings, heads, workers):
    pending_searches = {}
    for _, reference, repository, source_key, written_key in prepared_findings:
        source = cache['sources'][source_key]
        written = cache['claims'][written_key]
        if 'error' in (source['status'], written['status']) or not needs_presence(source, written):
            continue
        presence_key = presence_cache_key(repository, reference, written)
        if not is_settled_presence(cache['presence'].get(presence_key), heads[repository]):
            pending_searches[presence_key] = (repository, reference['kind'], reference['claim'], written, reference.get('is_desktop_page', False))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_history, presence_search, *arguments): key for key, arguments in pending_searches.items()}
        for future in as_completed(futures):
            presence = future.result()
            if presence['status'] == 'error':
                executor.shutdown(wait=False, cancel_futures=True)
                raise ValueError(f'Cannot attribute finding {futures[future]}: {presence["reason"]}')
            cache['presence'][futures[future]] = presence
            save_cache(cache)


def attribute_findings(findings, output_directory, build=False, workers=6):
    cache = load_cache()
    checker()
    heads = {}
    prepared_findings = []
    pending_searches = {}
    for finding in findings:
        finding.pop('attribution', None)
        reference = finding_reference(finding)
        if reference is None:
            raise ValueError(f'Cannot attribute {finding!r}: finding kind has no history search')
        repository = source_repository(reference['kind'], reference['claim'])
        source_key = source_cache_key(repository, reference)
        written_key = claim_cache_key(finding, reference)
        prepared_findings.append((finding, reference, repository, source_key, written_key))
        heads.setdefault(repository, repository_head(repository))
        cached_source = cache['sources'].get(source_key)
        search_options = {'kind': reference['kind'], 'is_desktop_page': reference.get('is_desktop_page', False)}
        if cached_source is None or 'computed_at_head' not in cached_source:
            pending_searches[('sources', source_key)] = (source_change_search, (repository, reference['claim']), search_options)
        if cached_source is not None and cached_source.get('computed_at_head') not in (None, heads[repository]):
            pending_searches[('sources', source_key)] = (revalidated_source, (repository, reference['claim'], cached_source), search_options)
        if written_key not in cache['claims']:
            pending_searches[('claims', written_key)] = (claim_written_search, (finding, reference), {})
    label_searches = [
        arguments[1] for search, arguments, options in pending_searches.values()
        if search == source_change_search and options['kind'] in ('ui-label', 'ui-label-minor')
    ]
    if len(label_searches) > 1:
        label_claims = sorted(set(label_searches))
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                histories = dict(zip(label_claims, executor.map(lambda claim: removal_history('monorepo', 'ui-label', claim), label_claims)))
        except subprocess.SubprocessError as failure:
            raise ValueError(f'Cannot attribute UI claims {label_searches!r}: {history_failure_reason(failure)}') from failure
        completed_keys = []
        for (section, key), (search, arguments, options) in pending_searches.items():
            if search != source_change_search or options['kind'] not in ('ui-label', 'ui-label-minor'):
                continue
            history_result = run_history(search, *arguments, **options, history=histories[arguments[1]])
            if history_result['status'] == 'error':
                raise ValueError(f'Cannot attribute finding {key}: {history_result["reason"]}')
            cache[section][key] = {**history_result, 'computed_at_head': heads[arguments[0]]}
            completed_keys.append((section, key))
            save_cache(cache)
        for key in completed_keys:
            pending_searches.pop(key)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run_history, search, *arguments, **options): (section, key)
            for (section, key), (search, arguments, options) in pending_searches.items()
        }
        for completed_count, future in enumerate(as_completed(futures), start=1):
            section, key = futures[future]
            history_result = future.result()
            if history_result['status'] == 'error':
                executor.shutdown(wait=False, cancel_futures=True)
                raise ValueError(f'Cannot attribute finding {key}: {history_result["reason"]}')
            cache[section][key] = history_result
            if section == 'sources':
                cache[section][key] = {**history_result, 'computed_at_head': heads[pending_searches[(section, key)][1][0]]}
            save_cache(cache)
            if build:
                print(f'[{completed_count}/{len(futures)}] {section} {key}: {history_result["status"]}', flush=True)
    attribute_presence(cache, prepared_findings, heads, workers)
    categories = {}
    for finding, reference, repository, source_key, written_key in prepared_findings:
        written = cache['claims'][written_key]
        presence = cache['presence'].get(presence_cache_key(repository, reference, written)) if written['status'] == 'found' else None
        try:
            evidence = classify_attribution(repository, cache['sources'][source_key], written, presence)
        except ValueError as failure:
            raise ValueError(f'Cannot attribute {finding["page"]} {finding["kind"]} {finding["detail"]}: {failure}') from failure
        finding['attribution'] = evidence
        categories[(finding['page'], finding['kind'], finding['detail'])] = evidence['category']
    return categories


def event_metadata(repository, event):
    message = git(REPOSITORIES[repository], 'log', '-1', '--no-walk', '--format=%B', event['revision'], timeout=None)
    pull_request = re.search(r'\(#(\d+)\)', message)
    return {**event, 'pr': int(pull_request.group(1)) if pull_request else None}


def gap_identifiers(entry):
    return entry.get('missing_identifiers') or doccoverage.undocumented_identifiers(entry) or [entry['name']]


def coverage_credits_page(source, entry, identifier, page_file, page_text):
    docs_root = REPOSITORIES['site'] / 'contents'
    page_path = str(docs_root / page_file.removeprefix('contents/').removeprefix('src/pages/'))
    documents = {page_path: page_text}
    tokens_to_paths = {token: {page_path} for token in doccoverage.WORD_PATTERN.findall(page_text)}
    is_setting = source in SETTING_GAP_SOURCES
    structured_corpus = None
    if source in STRUCTURED_GAP_SOURCES or source.endswith('changelog'):
        structured_text = doccoverage.structured_text(page_path, page_text)
        structured_corpus = ({page_path: structured_text}, {token: {page_path} for token in doccoverage.WORD_PATTERN.findall(structured_text)})
    title = entry.get('title', '') if is_setting else ''
    status, _ = doccoverage.symbol_status(
        identifier, documents, tokens_to_paths, {}, docs_root, is_setting, title, entry.get('section', ''), structured_corpus=structured_corpus,
        setting_context=entry if is_setting else None,
    )
    return status == 'documented'


def setting_history_phrases(entry):
    title = doccoverage.normalized_setting_title(entry.get('title', ''))
    title_words = [word for word in doccoverage.WORD_PATTERN.findall(title.lower()) if word not in doccoverage.SETTING_TITLE_FILLER_WORDS]
    section = entry.get('section', '')
    phrases = [title, doccoverage.normalized_setting_title(entry.get('section_title', '')), *entry.get('descriptions', []), *entry.get('keywords', []), *title_words[-1:]]
    section_links = entry.get('section_links', [section] if section else [])
    return [phrase for phrase in phrases if phrase.split()] + [f'settings/{link}' for link in section_links]


def docs_history_phrases(source, entry, identifier):
    phrases = [identifier.split('.')[-1]]
    if source in SETTING_GAP_SOURCES:
        phrases += setting_history_phrases(entry)
    return phrases


def docs_history_pattern(source, entry, identifier):
    return '|'.join('[[:space:]]+'.join(re.escape(word) for word in phrase.split()) for phrase in docs_history_phrases(source, entry, identifier))


def docs_removal_event(source, entry, shipped, site_revision_range=()):
    for identifier in gap_identifiers(entry):
        history = history_index.pickaxe_log(
            'site', REPOSITORIES['site'], [literal_constraints(phrase) for phrase in docs_history_phrases(source, entry, identifier)],
            ['--no-renames', f'--format={HISTORY_MARKER}%H:%ct', '-p', '-U100000', '-G', docs_history_pattern(source, entry, identifier), '--regexp-ignore-case'],
            DOCS_HISTORY_PATHS, single_range(site_revision_range), order='default',
        )
        for commit, patch_text in history_commits(history):
            if commit['timestamp'] <= shipped['timestamp']:
                continue
            credits_by_file = [
                (file, coverage_credits_page(source, entry, identifier, file, previous_text), coverage_credits_page(source, entry, identifier, file, current_text))
                for file, _, _, _, previous_text, current_text in patch_files(patch_text)
            ]
            if sum(was_credited for _, was_credited, _ in credits_by_file) <= sum(is_credited for _, _, is_credited in credits_by_file):
                continue
            file = next(file for file, was_credited, is_credited in credits_by_file if was_credited and not is_credited)
            return identifier, event_metadata('site', event_with_file(commit, file))
    return None


def docs_removed_evidence(repository, shipped, removal):
    identifier, event = removal
    return {'repository': repository, 'category': 'docs_removed', 'rule': 'identifier_removed_from_docs_after_shipping', 'identifier': identifier, 'evidence': [event], 'source_changed_at': event, 'shipped_at': shipped}


def gap_history_search(repository, source, entry):
    shipped = gap_shipped_event(repository, source, entry)
    removal = docs_removal_event(source, entry, shipped)
    if removal is not None:
        return docs_removed_evidence(repository, shipped, removal)
    rule = 'first_release_heading' if source.endswith('changelog') else 'first_source_identifier'
    return {'repository': repository, 'category': 'shipped_undocumented', 'rule': rule, 'evidence': [shipped], 'source_changed_at': shipped, 'shipped_at': shipped}


def revalidated_gap(repository, source, entry, cached):
    removal = docs_removal_event(source, entry, cached['shipped_at'], (f"{cached['computed_at_site_head']}..HEAD",))
    if removal is None:
        return cached
    if cached['category'] == 'docs_removed' and removal[1]['timestamp'] < cached['source_changed_at']['timestamp']:
        return cached
    return docs_removed_evidence(repository, cached['shipped_at'], removal)


def shipped_needle_and_pattern(source, entry):
    if source.endswith('changelog'):
        return f"## {entry['version']}", re.compile(rf'^##\s+{re.escape(entry["version"])}(?:\s|$)', re.M)
    needle = entry['name'].split('.')[-1]
    return needle, re.compile(rf'(?<![\w$]){re.escape(needle)}(?![\w$])')


def moved_line_sources(repository, revision, identifier_pattern, current_text):
    checkout = REPOSITORIES[repository]
    deleted_files = [path for path in git(checkout, 'show', '--format=', '--name-only', '-z', '--no-renames', '--diff-filter=D', revision, '--', *HISTORY_PATHS[repository], timeout=None).split('\0') if path]
    if not deleted_files:
        return []
    identifier_lines = {line.strip() for line in current_text.splitlines() if identifier_pattern.search(line)}
    parent = git(checkout, 'rev-parse', revision + '^').strip()
    return [deleted_file for deleted_file in deleted_files if identifier_lines & {line.strip() for line in blob_text(checkout, parent, deleted_file).splitlines()}]


def identifier_introduction(repository, needle, identifier_pattern, path, follows_moved_lines, visited=()):
    lineage_paths = sorted(set(source_lineage_paths(repository, path)))
    history = history_index.pickaxe_log(
        repository, REPOSITORIES[repository], [literal_constraints(needle)],
        ['--no-renames', f'--format={HISTORY_MARKER}%H:%ct', '-p', '-U100000', '-S', needle], lineage_paths, order='default',
    )
    for commit, patch_text in reversed(list(history_commits(history))):
        for file, _, _, _, previous_text, current_text in patch_files(patch_text):
            if not identifier_pattern.search(current_text) or identifier_pattern.search(previous_text):
                continue
            event = event_metadata(repository, event_with_file(commit, file))
            if not follows_moved_lines:
                return event
            searched_paths = (*visited, *lineage_paths)
            predecessors = [deleted_file for deleted_file in moved_line_sources(repository, commit['revision'], identifier_pattern, current_text) if deleted_file not in searched_paths]
            earlier_events = [identifier_introduction(repository, needle, identifier_pattern, predecessor, True, searched_paths) for predecessor in predecessors]
            return min([event, *filter(None, earlier_events)], key=lambda found: found['timestamp'])
    return None


def gap_shipped_event(repository, source, entry):
    origin = str(Path(entry['origin']).relative_to(REPOSITORIES[repository]))
    needle, identifier_pattern = shipped_needle_and_pattern(source, entry)
    event = identifier_introduction(repository, needle, identifier_pattern, origin, not source.endswith('changelog'))
    if event is None:
        raise ValueError(f'No introducing commit found for {source} {entry["name"]!r} in {origin}')
    if source.endswith('changelog') and not event['pr'] and entry.get('pr'):
        event['pr'] = int(entry['pr'].removeprefix('#'))
    return event


def gap_repository(source):
    if source.startswith('js-'):
        return 'posthog-js'
    if source.startswith('python-'):
        return 'posthog-python'
    return 'monorepo'


def attribute_gaps(gaps, workers=6):
    cache = load_cache()
    site_head = repository_head('site')
    pending = {}
    entries_and_keys = []
    for source, statuses in gaps.items():
        repository = gap_repository(source)
        for entries in statuses.values():
            for entry in entries:
                key = source_cache_key(repository, {'kind': source, 'claim': entry['name']})
                entries_and_keys.append((entry, key))
                cached = cache['gaps'].get(key)
                if cached is None or 'computed_at_site_head' not in cached or 'shipped_at' not in cached:
                    if 'origin' not in entry:
                        raise ValueError(f'Cannot attribute coverage gap {key}: its cache entry lacks computed_at_site_head or shipped_at and the gap has no origin file to search history from')
                    pending[key] = (gap_history_search, (repository, source, entry))
                    continue
                if cached['computed_at_site_head'] != site_head:
                    pending[key] = (revalidated_gap, (repository, source, entry, cached))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_history, search, *arguments): key for key, (search, arguments) in pending.items()}
        for future in as_completed(futures):
            key = futures[future]
            evidence = future.result()
            if evidence.get('status') == 'error':
                executor.shutdown(wait=False, cancel_futures=True)
                raise ValueError(f'Cannot attribute coverage gap {key}: {evidence["reason"]}')
            cache['gaps'][key] = {**evidence, 'computed_at_site_head': site_head}
            save_cache(cache)
    for entry, key in entries_and_keys:
        entry['attribution'] = cache['gaps'][key]


def gap_definition_patterns(source, entry):
    if source.endswith('changelog'):
        return [re.compile(rf'^##\s+{re.escape(entry["version"])}(?:\s|$)')]
    if source.startswith('settings'):
        return [re.compile(rf"\bid:\s*['\"]{re.escape(entry['name'])}['\"]")]
    identifier = re.escape(entry['name'].split('.')[-1])
    word = re.compile(rf'(?<![\w$]){identifier}(?![\w$])')
    if source == 'python-methods':
        return [re.compile(rf'^\s*(?:async\s+)?def\s+{identifier}\s*\('), word]
    if source == 'js-methods':
        return [re.compile(rf'^\s*(?:(?:public|private|protected|static|async|readonly|override)\s+)*{identifier}\s*(?:<[^>]*>)?\s*[(=]'), word]
    return [re.compile(rf'(?<![\w$]){identifier}\??\s*[:=]'), word]


def gap_definition(source, entry):
    repository = gap_repository(source)
    checkout = REPOSITORIES[repository]
    head = git(checkout, 'rev-parse', 'HEAD').strip()
    file = str(Path(entry['origin']).relative_to(checkout))
    lines = blob_text(checkout, head, file).splitlines()
    for pattern in gap_definition_patterns(source, entry):
        line_number = next((number for number, line in enumerate(lines, start=1) if pattern.search(line)), None)
        if line_number is not None:
            return {'repository': repository, 'revision': head, 'file': file, 'line': line_number}
    raise ValueError(f'Cannot locate the definition of {source} {entry["name"]!r} in {file} at {head}')


LEGACY_JS_SOURCE_PREFIX = 'src/'
SDK_COVERAGE_GATE_SOURCES = {'js-config', 'js-methods', 'python-config', 'python-methods'}
GUARDS = (
    {
        'name': 'posthog.com PR check', 'command': 'python3 impact.py --repo site <base>..<head>',
        'kinds': SITE_GATE_KINDS, 'finding_categories': ('introduced_by_docs_change', 'wrong_when_written'),
        'gap_prefixes': (), 'gap_categories': (),
    },
    {
        'name': 'monorepo PR check', 'command': 'python3 impact.py --repo monorepo <base>..<head>',
        'kinds': {'ui-label', 'ui-label-minor', 'app-url', 'settings-anchor'}, 'finding_categories': ('introduced_upstream',),
        'gap_prefixes': ('settings',), 'gap_categories': ('shipped_undocumented',),
    },
    {
        'name': 'SDK PR check', 'command': 'python3 impact.py --repo posthog-js|posthog-python <base>..<head>',
        'kinds': {'sdk-method'}, 'finding_categories': ('introduced_upstream',),
        'gap_prefixes': ('js-', 'python-'), 'gap_categories': ('shipped_undocumented',),
    },
)


def pr_gate_verdict(item):
    evidence = item['attribution']
    repository = evidence['repository']
    category = evidence['category']
    kind = item['kind']
    changed_file = (evidence.get('source_changed_at') or {}).get('file', '')
    if category == 'wrong_when_written' and kind in SITE_GATE_KINDS:
        return True, f'the site gate flags new failing {kind} claims in the PR that writes them'
    if category == 'wrong_when_written':
        return False, f'no gate re-checks {kind} claims when a docs PR writes them'
    if category == 'docs_removed':
        return False, 'no gate checks coverage when docs text is removed'
    if category == 'shipped_undocumented' and kind not in SDK_COVERAGE_GATE_SOURCES:
        return False, f'no gate checks {kind} coverage'
    if gate_rechecks_change(repository, changed_file):
        return True, f'the {repository} gate re-checks the changed file'
    if repository == 'posthog-js' and changed_file.startswith(LEGACY_JS_SOURCE_PREFIX):
        return False, 'the change predates the packages/ layout that the posthog-js gate checks'
    return False, f'the {repository} gate does not re-check the changed file'


def caught_at_pr_time(item):
    return pr_gate_verdict(item)[0]


def counts_toward_totals(source, entry):
    if not source.endswith('changelog'):
        return True
    return changelog_gap_bucket(entry) == COUNTED_CHANGELOG_GAP_BUCKET


def guarded_items(findings, gaps):
    items = [{'kind': finding['kind'], 'page': finding['page'], 'attribution': finding['attribution']} for finding in findings]
    items.extend(
        {'kind': source, 'page': None, 'attribution': entry['attribution']}
        for source, statuses in gaps.items() for entries in statuses.values() for entry in entries if counts_toward_totals(source, entry)
    )
    return items


def changelog_gaps_outside_totals(gaps):
    counts_by_bucket = {bucket: collections.Counter() for bucket in doccoverage.CHANGELOG_GAP_BUCKETS if bucket != COUNTED_CHANGELOG_GAP_BUCKET}
    for source, statuses in gaps.items():
        for entries in statuses.values():
            for entry in entries:
                if counts_toward_totals(source, entry):
                    continue
                counts_by_bucket[changelog_gap_bucket(entry)][entry['attribution']['category']] += 1
    return {bucket: {category: counts[category] for category in CATEGORIES if counts[category]} for bucket, counts in counts_by_bucket.items()}


def guard_owns(guard, item):
    category = item['attribution']['category']
    if item['page'] is None:
        return item['kind'].startswith(guard['gap_prefixes']) and category in guard['gap_categories']
    return item['kind'] in guard['kinds'] and category in guard['finding_categories']


def upstream_guards(findings, gaps, output_directory):
    attribute_findings(findings, output_directory)
    attribute_gaps(gaps)
    items = guarded_items(findings, gaps)
    guards = []
    for guard in GUARDS:
        owned = [item for item in items if guard_owns(guard, item)]
        by_category = {}
        for category in dict.fromkeys((*guard['finding_categories'], *guard['gap_categories'])):
            category_items = [item for item in owned if item['attribution']['category'] == category]
            verdicts = [pr_gate_verdict(item) for item in category_items]
            missed_reasons = collections.Counter(reason for caught, reason in verdicts if not caught)
            by_category[category] = {
                'total': len(category_items), 'caught': sum(caught for caught, _ in verdicts),
                'audit_findings': sum(item['page'] is not None for item in category_items),
                'coverage_gaps': sum(item['page'] is None for item in category_items),
                'missed_reasons': dict(sorted(missed_reasons.items())),
            }
        guards.append({
            'name': guard['name'], 'command': guard['command'], 'by_category': by_category,
            'total': len(owned), 'caught_at_pr_time': sum(entry['caught'] for entry in by_category.values()),
            'example_pages': sorted({item['page'] for item in owned if item['page']})[:3],
        })
    outside = collections.Counter(
        (item['attribution']['category'], item['kind']) for item in items if not any(guard_owns(guard, item) for guard in GUARDS)
    )
    return {
        'guards': guards,
        'outside_every_gate': [{'category': category, 'kind': kind, 'count': count} for (category, kind), count in sorted(outside.items())],
        'totals': {category: sum(item['attribution']['category'] == category for item in items) for category in CATEGORIES},
        'changelog_gaps_outside_totals': changelog_gaps_outside_totals(gaps),
    }


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--build', action='store_true')
    mode.add_argument('--prefetch', action='store_true')
    parser.add_argument('--workers', type=int, default=6)
    arguments = parser.parse_args()
    if arguments.workers < 1:
        parser.error('--workers must be positive')
    if arguments.prefetch:
        for repository in HISTORY_PATHS:
            started = time.monotonic()
            print(f'{repository}: fetched {prefetch_history_blobs(repository)} missing history blobs in {time.monotonic() - started:.2f}s', flush=True)
        return
    from next_steps import read_findings

    findings = read_findings(doccoverage.OUTPUT_ROOT)
    started = time.monotonic()
    attribute_findings(findings, doccoverage.OUTPUT_ROOT, build=True, workers=arguments.workers)
    from next_steps import coverage_gaps
    coverage_file = doccoverage.OUTPUT_ROOT / 'coverage.json'
    coverage = json.loads(coverage_file.read_text())
    gaps = coverage_gaps(coverage)
    attribute_gaps(gaps, workers=arguments.workers)
    for source, statuses in gaps.items():
        attributed_by_name = {entry['name']: entry for entries in statuses.values() for entry in entries}
        for entry in coverage.get(source, []):
            if entry['name'] not in attributed_by_name:
                continue
            entry['attribution'] = attributed_by_name[entry['name']]['attribution']
            entry['definition'] = gap_definition(source, entry)
    coverage_file.write_text(json.dumps(coverage, indent=2, sort_keys=True) + '\n')
    counts = collections.Counter(item['attribution']['category'] for item in guarded_items(findings, gaps))
    print(f'Build completed in {time.monotonic() - started:.2f}s: ' + ', '.join(f'{category}={counts[category]}' for category in CATEGORIES), flush=True)


if __name__ == '__main__':
    doccoverage.run_at_background_priority()
    main()
