import argparse
import ast
import contextlib
import difflib
import functools
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import tarfile
import traceback
import urllib.request
import types
from pathlib import Path
from unittest.mock import patch

import doccoverage

TOOL_ROOT = Path(__file__).resolve().parent
REPOSITORIES = {
    'monorepo': doccoverage.MONOREPO_ROOT,
    'posthog-js': doccoverage.POSTHOG_JS_ROOT,
    'posthog-python': doccoverage.POSTHOG_PYTHON_ROOT,
    'site': doccoverage.SITE_ROOT,
}
SETTINGS_MAP = 'frontend/src/scenes/settings/SettingsMap.tsx'
SETTINGS_DIRECTORY = 'frontend/src/scenes/settings/'
SETTINGS_SCENE_LOGIC = SETTINGS_DIRECTORY + 'settingsSceneLogic.ts'
ROUTE_FILES = ('frontend/src/scenes/urls.ts', 'frontend/src/scenes/scenes.ts')
SOURCE_SUFFIXES = {'.ts', '.tsx', '.py', '.json', '.md', '.mdx', '.js', '.jsx'}
LABEL_SOURCE_PREFIXES = ('frontend/src/', 'products/', 'nodejs/src/cdp/templates/')
SDK_SOURCE_PREFIXES = {'posthog-js': 'packages/', 'posthog-python': 'posthog/'}
SITE_REDIRECTS_FILE = 'vercel.json'
SITE_GATE_KINDS = ('docs-link', 'docs-fragment', 'settings-anchor', 'app-url')


@functools.lru_cache(maxsize=1)
def checker():
    module = types.ModuleType('impact_checker')
    module.__file__ = str(TOOL_ROOT / 'docaudit.py')
    source = Path(module.__file__).read_text().replace('\nmain()\n', '\n')
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def git_environment():
    return {**os.environ, 'GIT_NO_LAZY_FETCH': '1'}


def git(checkout, *arguments, timeout=30):
    return subprocess.run(
        ['git', '-C', str(checkout), '-c', 'core.quotepath=off', *arguments], capture_output=True, check=True, timeout=timeout,
        env=git_environment(),
    ).stdout.decode('utf-8', errors='replace')


@functools.lru_cache(maxsize=32)
def tree_entries(checkout, revision):
    entries = {}
    for entry in git(checkout, 'ls-tree', '-r', '-z', revision).split('\0'):
        if not entry:
            continue
        metadata, path = entry.split('\t', 1)
        mode, object_type, object_id = metadata.split()
        if object_type == 'blob' and mode != '120000':
            entries[path] = object_id
    return entries


def tree_paths(checkout, revision):
    return list(tree_entries(checkout, revision))


def head_label_paths(checkout, revision, references):
    return [path for path in tree_paths(checkout, revision) if is_label_source(path)]


def is_label_source(path):
    return path.startswith(LABEL_SOURCE_PREFIXES) and source_is_checkable(path) and not checker().EXCLUDED_LABEL_SOURCE.search(path)


def is_monorepo_inventory_source(path):
    settings_sources = (SETTINGS_MAP, SETTINGS_DIRECTORY + 'types.ts', SETTINGS_DIRECTORY + 'settingsSceneLogic.ts')
    return path in ROUTE_FILES or path in settings_sources or bool(re.fullmatch(r'products/[^/]+/manifest\.tsx', path))


def is_site_dependency(path):
    return path == SITE_REDIRECTS_FILE or (path.startswith('contents/') and Path(path).suffix in ('.md', '.mdx'))


def gate_rechecks_change(repository, path):
    if repository == 'monorepo':
        return is_label_source(path) or is_monorepo_inventory_source(path)
    if repository == 'site':
        return is_site_dependency(path)
    return path.startswith(SDK_SOURCE_PREFIXES[repository]) and source_is_checkable(path)


def source_is_checkable(path):
    parts = Path(path).parts
    if any(part.startswith('.') or part in {'node_modules', 'test', 'tests', '__tests__', 'dist', '__snapshots__'} for part in parts):
        return False
    return Path(path).suffix in SOURCE_SUFFIXES and not re.search(r'\.(test|spec|stories)\.', path)


def cache_missing_blobs(checkout, revision, missing_paths):
    cache_root = TOOL_ROOT / 'data/impact-blobs'
    cache_root.mkdir(exist_ok=True)
    entries = tree_entries(checkout, revision)
    missing_paths = {path for path in missing_paths if not (cache_root / entries[path]).exists()}
    if not missing_paths:
        return cache_root
    remote = git(checkout, 'config', '--get', 'remote.origin.url').strip()
    repository = re.fullmatch(r'(?:https://github.com/|git@github.com:)([\w.-]+/[\w.-]+?)(?:\.git)?', remote)
    if repository is None:
        raise ValueError('Historical blobs are missing; only public GitHub archive reads are supported')
    url = f'https://codeload.github.com/{repository.group(1)}/tar.gz/{revision}'
    with urllib.request.urlopen(url, timeout=60) as response, tarfile.open(fileobj=response, mode='r|gz') as archive:
        for member in archive:
            path = member.name.partition('/')[2]
            if not member.isfile() or path not in missing_paths:
                continue
            content = archive.extractfile(member).read()
            object_id = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
            if object_id != entries[path]:
                raise ValueError(f'Archive source does not match git blob: {revision}:{path}')
            (cache_root / object_id).write_bytes(content)
            missing_paths.remove(path)
            if not missing_paths:
                return cache_root
    raise ValueError(f'Archive lacks historical sources: {sorted(missing_paths)[:3]}')


def worktree_holds_revision(checkout, revision):
    if git(checkout, 'rev-parse', revision).strip() != git(checkout, 'rev-parse', 'HEAD').strip():
        return False
    return not git(checkout, 'status', '--porcelain', timeout=120).strip()


@contextlib.contextmanager
def snapshot(checkout, revision, paths, use_worktree=False):
    if use_worktree and worktree_holds_revision(checkout, revision):
        yield checkout
        return
    available_paths = set(tree_paths(checkout, revision))
    selected_paths = [path for path in sorted(set(paths)) if path in available_paths and source_is_checkable(path)]
    with tempfile.TemporaryDirectory(prefix='impact-snapshot-') as directory:
        root = Path(directory).resolve()
        if selected_paths:
            object_ids = tree_entries(checkout, revision)
            requests = ''.join(f'{object_ids[path]}\n' for path in selected_paths).encode()
            blobs = subprocess.run(
                ['git', '-C', str(checkout), 'cat-file', '--batch'], input=requests,
                capture_output=True, check=True, timeout=60, env=git_environment(),
            ).stdout
            stream = io.BytesIO(blobs)
            missing_paths = []
            for path in selected_paths:
                header = stream.readline().decode().strip().split()
                if len(header) == 2 and header[1] == 'missing':
                    missing_paths.append(path)
                    continue
                if len(header) != 3 or header[1] != 'blob':
                    raise ValueError(f'Cannot read source blob: {revision}:{path}')
                destination = root / path
                if not destination.resolve().is_relative_to(root):
                    raise ValueError(f'Unsafe source path: {path}')
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(stream.read(int(header[2])))
                stream.read(1)
            if missing_paths:
                cache_root = cache_missing_blobs(checkout, revision, missing_paths)
                for path in missing_paths:
                    destination = root / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((cache_root / object_ids[path]).read_bytes())
        yield root


def reference_from_finding(page, kind, detail):
    if kind in ('ui-label', 'ui-label-minor'):
        claim = re.split(r' \((?:UI:|tooltip only:)', detail)[0]
        return {'kind': 'ui-label', 'claim': claim, 'pages': [page], 'sources': []}
    if kind in ('app-url', 'settings-anchor'):
        return {'kind': kind, 'claim': detail, 'pages': [page], 'sources': []}
    if kind == 'sdk-method':
        language, identifier = detail.split(': posthog.', 1)
        sdk = checker().fence_sdks(language, page)[0]
        return {'kind': kind, 'claim': f'{sdk}: posthog.{identifier}', 'sdk': sdk, 'identifier': identifier, 'pages': [page], 'sources': []}
    return None


def load_references(output_directory):
    references_file = output_directory / 'doc-references.json'
    findings_file = output_directory / 'all-findings.txt'
    if not references_file.exists() and not findings_file.exists():
        raise ValueError('Claim outputs are unavailable; finish all_sections.py before checking impact')
    references = json.loads(references_file.read_text()) if references_file.exists() else []
    if findings_file.exists():
        for line in findings_file.read_text().splitlines():
            page, kind, detail = line.split('\t')[:3]
            reference = reference_from_finding(page, kind, detail)
            if reference:
                references.append(reference)
    merged = {}
    for reference in references:
        key = reference['kind'], reference['claim']
        aggregate = merged.setdefault(key, {**reference, 'pages': set(), 'sources': []})
        aggregate['pages'].update(reference['pages'])
        for source in reference['sources']:
            if source not in aggregate['sources']:
                aggregate['sources'].append(source)
    return [{**reference, 'pages': sorted(reference['pages'])} for reference in merged.values()]


def route_segments(claim):
    segments = claim.strip('/').split('/')
    if segments[:1] == ['project'] and len(segments) >= 2:
        return segments[2:]
    return segments


def source_location(root, paths, claim, route_claim=None):
    needle = claim.split('#')[-1].split('posthog.')[-1]
    exact_label = re.compile(r'[\'"]' + re.escape(needle) + r'[\'"]|>\s*' + re.escape(needle) + r'\s*<|^\s*' + re.escape(needle) + r'\s*$')
    for path in sorted(paths):
        source_path = root / path
        if not source_path.is_file():
            continue
        source = source_path.read_text(errors='ignore')
        lines = source.splitlines()
        if route_claim:
            audit = checker()
            segments = route_segments(route_claim)
            for route, end in audit.route_literals(source):
                path_only = re.split(r'[?#]', route)[0]
                route_parts = [segment for segment in path_only.rstrip('*').split('/') if segment]
                has_open_suffix = path_only.endswith('*') or bool(re.match(r'\s*\+', source[end:]))
                if not audit.route_matches_pattern(segments, [audit.route_segment_pattern(segment) for segment in route_parts], has_open_suffix):
                    continue
                line_number = source[:end].count('\n') + 1
                return {'file': path, 'line': line_number, 'text': lines[line_number - 1].strip()}
        for line_number, line in enumerate(lines, start=1):
            if exact_label.search(line):
                return {'file': path, 'line': line_number, 'text': line.strip()}
        for line_number, line in enumerate(lines, start=1):
            if needle.casefold() in line.casefold():
                return {'file': path, 'line': line_number, 'text': line.strip()}
        for line_number, line in enumerate(lines, start=1):
            if any(word.casefold() in line.casefold() for word in needle.split() if len(word) > 3):
                return {'file': path, 'line': line_number, 'text': line.strip()}
    raise ValueError(f'Cannot locate base evidence for {claim!r} in {paths}')


def first_changed_line(base_root, head_root, path):
    base_file, head_file = base_root / path, head_root / path
    base_lines = base_file.read_text(errors='ignore').splitlines() if base_file.is_file() else []
    head_lines = head_file.read_text(errors='ignore').splitlines() if head_file.is_file() else []
    for tag, base_start, _, _, _ in difflib.SequenceMatcher(None, base_lines, head_lines, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        text = base_lines[base_start] if base_start < len(base_lines) else ''
        return {'file': path, 'line': base_start + 1, 'text': text.strip()}
    return {'file': path, 'line': 1, 'text': ''}


def impacted_rows(reference, location, base, wording):
    return [{
        'page': page, 'kind': reference['kind'], 'claim': reference['claim'],
        'source_change': {**location, 'revision': base},
        'guard': f"This PR {wording} '{reference['claim']}' cited by docs/{page}; update the doc or keep the cited source",
    } for page in reference['pages']]


def monorepo_inventory(root):
    audit = checker()
    with patch.object(audit, 'MONOREPO', root):
        return {
            'routes': audit.frontend_route_sources(),
            'sections': audit.settings_section_ids(),
            'settings': audit.settings_anchor_scope(),
        }


def route_exists(reference, inventory):
    audit = checker()
    segments = route_segments(reference['claim'])
    if segments[:1] == ['settings'] and len(segments) > 1:
        if not audit.is_doc_placeholder(segments[1]) and segments[1] not in inventory['sections']:
            return False
    return any(audit.route_matches_pattern(segments, [audit.route_segment_pattern(segment) for segment in shape[0]], shape[1]) for shape in inventory['routes'])


def anchor_exists(reference, inventory):
    settings_path, anchor = reference['claim'].removeprefix('settings/').split('#', 1)
    return anchor in checker().settings_anchor_ids_in_scope(settings_path, inventory['settings'])


def monorepo_impact(checkout, base, head, touched_paths, references):
    audit = checker()
    base_paths = tree_paths(checkout, base)
    head_paths = tree_paths(checkout, head)
    inventory_paths = {path for path in (*base_paths, *head_paths) if is_monorepo_inventory_source(path)}
    impacted = []
    label_paths = {path for path in touched_paths if is_label_source(path)}
    with snapshot(checkout, base, inventory_paths | label_paths) as base_root:
        base_inventory = monorepo_inventory(base_root)
        label_candidates = []
        with patch.object(audit, 'MONOREPO', base_root):
            base_labels = audit.ui_label_index()
        for reference in references:
            if reference['kind'] != 'ui-label':
                continue
            paths = sorted({
                path for variant in audit.claim_label_variants(reference['claim'])
                for path in audit.search_label(variant, base_labels)[1] if path in touched_paths
            })
            if paths:
                label_candidates.append((reference, paths))
        selected_head_paths = inventory_paths
        if label_candidates:
            selected_head_paths = selected_head_paths | set(head_label_paths(checkout, head, label_candidates))
        with snapshot(checkout, head, selected_head_paths, use_worktree=True) as head_root:
            head_inventory = monorepo_inventory(head_root)
            if label_candidates:
                with patch.object(audit, 'MONOREPO', head_root):
                    head_labels = audit.ui_label_index()
                for reference, paths in label_candidates:
                    variants = audit.claim_label_variants(reference['claim'])
                    if any(audit.search_label(variant, head_labels)[0] not in ('missing', 'tooltip-only') for variant in variants):
                        continue
                    impacted.extend(impacted_rows(reference, source_location(base_root, paths, reference['claim']), base, 'removes UI label'))
            for reference in references:
                if reference['kind'] == 'app-url' and route_exists(reference, base_inventory) and not route_exists(reference, head_inventory):
                    segments = route_segments(reference['claim'])
                    paths = set().union(*(
                        paths for shape, paths in base_inventory['routes'].items()
                        if audit.route_matches_pattern(segments, [audit.route_segment_pattern(segment) for segment in shape[0]], shape[1])
                    ))
                    needle = reference['claim']
                    if segments[:1] == ['settings'] and len(segments) > 1:
                        paths.add('frontend/src/scenes/settings/types.ts')
                        needle = segments[1]
                    impacted.extend(impacted_rows(reference, source_location(base_root, paths, needle, route_claim=reference['claim'] if segments[:1] != ['settings'] else None), base, 'removes app route'))
                if reference['kind'] == 'settings-anchor' and anchor_exists(reference, base_inventory) and not anchor_exists(reference, head_inventory):
                    impacted.extend(impacted_rows(reference, source_location(base_root, [SETTINGS_MAP], reference['claim']), base, 'removes or moves settings anchor'))
    return impacted


def sdk_inventory(root, repository):
    audit = checker()
    if repository == 'posthog-js':
        with patch.object(audit, 'JS_PACKAGES', root / 'packages'):
            identifiers = {sdk: audit.js_sdk_identifiers(packages) for sdk, packages in audit.JS_PACKAGES_BY_SDK.items()}
            packages = [path.name for path in (root / 'packages').iterdir() if (path / 'src').is_dir()]
            identifiers['js-any'] = audit.js_sdk_identifiers(packages)
        entries = {'js-config': doccoverage.js_config_inventory(root), 'js-methods': doccoverage.js_method_inventory(root)}
        return identifiers, entries
    with patch.object(audit, 'PYTHON_PACKAGE', root / 'posthog'):
        identifiers = {'python': audit.python_sdk_identifiers()}
    entries = {'python-config': doccoverage.python_config_inventory(root), 'python-methods': doccoverage.python_method_inventory(root)}
    return identifiers, entries


def sdk_definition_paths(root, paths, identifier, repository):
    matches = []
    for path in paths:
        source_path = root / path
        if not source_path.is_file():
            continue
        source = source_path.read_text(errors='ignore')
        if repository == 'posthog-js' and Path(path).suffix in ('.ts', '.tsx') and identifier in checker().ts_public_definitions(source):
            matches.append(path)
        if repository == 'posthog-python' and Path(path).suffix == '.py' and identifier in python_defined_names(source):
            matches.append(path)
    return matches


def python_defined_names(source):
    audit = checker()
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    names |= audit.python_scope_definitions(tree.body)
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        names |= audit.python_scope_definitions(class_node.body) | audit.python_self_attributes(class_node)
    return names


def python_module_identifier(path):
    return Path(path).parent.name if Path(path).name == '__init__.py' else Path(path).stem


def removed_identifier_location(base_root, head_root, touched_paths, identifier, repository):
    definition_paths = sdk_definition_paths(base_root, touched_paths, identifier, repository)
    if definition_paths:
        return source_location(base_root, definition_paths, identifier)
    changed_paths = sorted(path for path in touched_paths if path.startswith(SDK_SOURCE_PREFIXES[repository]))
    module_paths = [path for path in changed_paths if repository == 'posthog-python' and path.endswith('.py') and python_module_identifier(path) == identifier]
    return first_changed_line(base_root, head_root, (module_paths or changed_paths)[0])


def sdk_impact(checkout, repository, base, head, touched_paths, references):
    prefix = SDK_SOURCE_PREFIXES[repository]
    paths = {path for revision in (base, head) for path in tree_paths(checkout, revision) if path.startswith(prefix)}
    impacted = []
    with snapshot(checkout, base, paths) as base_root, snapshot(checkout, head, paths, use_worktree=True) as head_root:
        base_identifiers, base_entries = sdk_inventory(base_root, repository)
        head_identifiers, head_entries = sdk_inventory(head_root, repository)
        for reference in references:
            if reference['kind'] != 'sdk-method':
                continue
            sdk = reference['sdk']
            identifier = reference['identifier']
            if identifier not in base_identifiers.get(sdk, set()) or identifier in head_identifiers.get(sdk, set()):
                continue
            location = removed_identifier_location(base_root, head_root, touched_paths, identifier, repository)
            impacted.extend(impacted_rows(reference, location, base, 'removes SDK identifier'))
        docs_root = REPOSITORIES['site'] / 'contents'
        corpus = doccoverage.corpus_index(docs_root, doccoverage.MONOREPO_ROOT / 'docs/onboarding')
        documents, tokens_to_paths = corpus.documents, corpus.tokens_to_paths
        for source, entries in head_entries.items():
            entries = list({entry['name']: entry for entry in entries}.values())
            previous_names = {entry['name'] for entry in base_entries[source]}
            for entry in entries:
                if entry['name'] in previous_names:
                    continue
                mentions = doccoverage.mention_paths(entry['name'], documents, tokens_to_paths)
                if any(Path(path).is_relative_to(docs_root / 'docs') for path in mentions):
                    continue
                source_path = str(Path(entry['origin']).relative_to(head_root))
                location = source_location(head_root, [source_path], entry['name'].split('.')[-1])
                impacted.append({
                    'page': '(coverage gap)', 'kind': source, 'claim': entry['name'],
                    'source_change': {**location, 'revision': head},
                    'guard': f"This PR adds public {source} '{entry['name']}' with no docs mention; document it before merging",
                })
    return impacted


def content_route(path):
    return '/' + str(Path(path).with_suffix('')).removeprefix('contents/').removesuffix('/index')


def site_source_route(path):
    if not path.startswith('src/pages/'):
        return content_route(path)
    route_parts = Path(path).with_suffix('').relative_to('src/pages').parts
    if route_parts[-1] == 'index':
        route_parts = route_parts[:-1]
    return '/' + '/'.join(route_parts)


def broken_dependency(reference, context):
    audit = checker()
    link_path, _, fragment = reference['claim'].partition('#')
    status, final_route, _ = audit.resolve_doc_route(link_path, context['redirects'])
    if status != 'ok':
        return f'{status}: {final_route}'
    if fragment and audit.docs_fragment_status(final_route, fragment) == 'missing':
        return final_route
    return None


def inbound_impact(references, context, dependency_paths, head_paths, reported, base_root, head_root, base):
    impacted = []
    for reference in references:
        if reference['kind'] not in ('docs-link', 'docs-fragment') or not reference['claim'].startswith(('/', 'http')):
            continue
        changed_sources = sorted({source['path'] for source in reference['sources'] if source['repo'] == 'site'} & dependency_paths)
        if SITE_REDIRECTS_FILE in dependency_paths:
            changed_sources.append(SITE_REDIRECTS_FILE)
        if not changed_sources:
            continue
        pages = [page for page in reference['pages'] if f'contents/docs/{page}' in head_paths and (page, reference['kind'], reference['claim']) not in reported]
        if not pages or broken_dependency(reference, context) is None:
            continue
        location = first_changed_line(base_root, head_root, changed_sources[0])
        impacted.extend(impacted_rows({**reference, 'pages': pages}, location, base, 'breaks the docs target of'))
    return impacted


def site_impact(checkout, base, head, touched_paths, references):
    audit = checker()
    base_paths = set(tree_paths(checkout, base))
    head_paths = set(tree_paths(checkout, head))
    paths = [path for path in head_paths if path.startswith('contents/') or path == SITE_REDIRECTS_FILE]
    recorded_site_sources = {source['path'] for reference in references for source in reference['sources'] if source['repo'] == 'site'}
    dependency_paths = {path for path in touched_paths if is_site_dependency(path) or path in recorded_site_sources}
    deleted_page_paths = {path for path in dependency_paths if path.startswith(('contents/', 'src/pages/')) and path in base_paths and path not in head_paths}
    deleted_routes = {site_source_route(path) for path in deleted_page_paths}
    impacted = []
    with snapshot(checkout, head, paths, use_worktree=True) as head_root, snapshot(checkout, base, touched_paths) as base_root:
        with patch.object(audit, 'SITE', head_root), patch.object(audit, 'LIVE_ROUTES', audit.LIVE_ROUTES - deleted_routes):
            audit.PAGE_ANCHOR_INDEX_CACHE.clear()
            audit.PAGE_ANCHOR_CACHE.clear()
            context = audit.build_context()
            for path in touched_paths:
                page_path = Path(path)
                if not path.startswith('contents/docs/') or page_path.suffix not in ('.md', '.mdx') or not (head_root / path).is_file():
                    continue
                relative_page = path.removeprefix('contents/docs/')
                current = audit.audit_page((head_root / path).read_text(), context, relative_page)
                previous_findings = set()
                if path in base_paths:
                    previous_text = (base_root / path).read_text()
                    previous = audit.audit_page(previous_text, context, relative_page)
                    previous_findings = {(kind, detail) for kind, detail, _ in previous.findings}
                head_text = (head_root / path).read_text()
                for (kind, detail, _), claim in zip(current.findings, current.failed_claims):
                    if kind not in SITE_GATE_KINDS or (kind, detail) in previous_findings:
                        continue
                    location = source_location(head_root, [path], detail.split(' (')[0])
                    doc_location = audit.claim_location(relative_page, head_text, kind, claim)
                    impacted.append({
                        'page': relative_page, 'kind': kind, 'claim': detail,
                        'source_change': {**location, 'revision': head},
                        'doc': {'revision': head, **doc_location},
                        'guard': f"This PR introduces failing {kind} '{detail}' in docs/{relative_page}; correct the cited target",
                    })
            reported = {(entry['page'], entry['kind'], entry['claim'].split(' (')[0]) for entry in impacted}
            impacted.extend(inbound_impact(references, context, dependency_paths, head_paths, reported, base_root, head_root, base))
    return impacted


def check_impact(repository, revision_range, output_directory=doccoverage.OUTPUT_ROOT, references=None):
    if revision_range.count('..') != 1 or '...' in revision_range:
        raise ValueError('Expected an explicit <base>..<head> range')
    base, head = revision_range.split('..')
    checkout = REPOSITORIES[repository]
    base = git(checkout, 'rev-parse', '--verify', f'{base}^{{commit}}').strip()
    head = git(checkout, 'rev-parse', '--verify', f'{head}^{{commit}}').strip()
    base = git(checkout, 'merge-base', base, head).strip()
    touched_paths = [path for path in git(checkout, 'diff', '--name-only', '-z', '--no-renames', base, head).split('\0') if path]
    if not touched_paths:
        return []
    references = load_references(output_directory) if references is None else references
    impact_by_repository = {'site': site_impact, 'monorepo': monorepo_impact}
    if repository in impact_by_repository:
        return annotate_links(impact_by_repository[repository](checkout, base, head, touched_paths, references), repository, head)
    return annotate_links(sdk_impact(checkout, repository, base, head, touched_paths, references), repository, head)


def annotate_links(impacted, repository, head):
    audit = checker()
    site_head = git(REPOSITORIES['site'], 'rev-parse', 'HEAD').strip()
    for entry in impacted:
        source = entry['source_change']
        entry['links'] = {
            'cited_source': doccoverage.github_blob_url(repository, source['revision'], source['file'], source['line']),
            'change': doccoverage.github_commit_url(repository, head),
        }
        if entry['page'] == '(coverage gap)':
            continue
        if 'doc' not in entry:
            page_text = (REPOSITORIES['site'] / 'contents/docs' / entry['page']).read_text(errors='ignore')
            entry['doc'] = {'revision': site_head, **audit.claim_location(entry['page'], page_text, entry['kind'], entry['claim'])}
        doc = entry['doc']
        entry['links']['doc_source'] = doccoverage.github_blob_url('site', doc['revision'], f"contents/docs/{entry['page']}", doc['line'])
        entry['links']['live'] = doc['live_urls']
    return impacted


def comment_lines(entry):
    source = entry['source_change']
    links = entry['links']
    lines = [f"{entry['page']}\t{entry['kind']}\t{entry['claim']}\t{source['file']}:{source['line']} ({source['revision'][:12]})", entry['guard']]
    if 'doc_source' in links:
        lines.append(f"  Doc source: {links['doc_source']}")
        lines.append(f"  Live: {', '.join(links['live']) or 'not published: no docs page renders this snippet'}")
    lines.append(f"  Evidence: cited source {links['cited_source']}; change {links['change']}")
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', choices=REPOSITORIES, required=True)
    parser.add_argument('revision_range')
    parser.add_argument('--json', type=Path)
    arguments = parser.parse_args()
    try:
        impacted = check_impact(arguments.repo, arguments.revision_range)
    except Exception as failure:
        traceback.print_exc()
        parser.exit(2, f'impact: {type(failure).__name__}: {failure}\n')
    if arguments.json:
        arguments.json.write_text(json.dumps(impacted, indent=2) + '\n')
    for entry in impacted:
        print('\n'.join(comment_lines(entry)))
    if not impacted:
        print('No impacted doc claims or new undocumented public SDK members.')
    raise SystemExit(1 if impacted else 0)


if __name__ == '__main__':
    doccoverage.run_at_background_priority()
    main()
