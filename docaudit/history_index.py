import argparse
import array
import contextlib
import bisect
import fnmatch
import functools
import hashlib
import heapq
import itertools
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

import doccoverage
from impact import REPOSITORIES, git, git_environment

INDEX_ROOT = doccoverage.TOOL_ROOT / 'data/history-index'
INDEX_FORMAT_VERSION = 3
HISTORY_PATHS = {
    'monorepo': ['frontend/src', 'products', 'nodejs/src/cdp/templates'],
    'posthog-js': ['packages', 'src', 'CHANGELOG.md'],
    'posthog-python': ['posthog', 'CHANGELOG.md'],
    'site': ['contents', 'src', 'vercel.json'],
}
COMMIT_HEADER = b'\x1e'
TOKEN_PATTERN = re.compile(r'[a-z0-9_]+')
ASCII_TOKEN_PATTERN = re.compile(rb'[a-z0-9_]+')
MAX_EXPANDED_TOKENS = 4096
PAIR_BUCKET_MASK = (1 << 22) - 1
MERGE_STATUS = 'MERGE'
STATUS_CODES = {'A': 1, 'D': 2, 'M': 3, 'T': 4, 'OTHER': 5, MERGE_STATUS: 7}
STATUS_BITS = 3
STATUS_MASK = (1 << STATUS_BITS) - 1
POSTING_SIZE = array.array('I').itemsize
GLOB_MAGIC = ':(glob)'
DEFAULT_PATHSPEC_WILDCARDS = re.compile(r'[*?\[\\]')
INDEXES_BY_CHECKOUT = {}
FIXTURE_FILE = doccoverage.TOOL_ROOT / 'data/history-index-fixture.json'
QUERY_FIXTURE = {'mode': None, 'answers': {}}
INDEXES_LOCK = threading.Lock()
PICKAXE_VALUE_OPTIONS = ('-G', '-S')
PICKAXE_FLAGS = ('--regexp-ignore-case',)
PRESENTATION_OPTION = re.compile(r'--format=.*|-p|-U\d+|--no-renames')
FULL_REVISION = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


class TooManyTokens(Exception):
    pass


class MissingFixtureAnswer(LookupError):
    pass


def index_files(repository):
    return INDEX_ROOT / f'{repository}.bin', INDEX_ROOT / f'{repository}.json'


def checkout_identity(checkout):
    common_directory = Path(git(checkout, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip())
    status = common_directory.stat()
    return f'{status.st_dev}:{status.st_ino}:{getattr(status, "st_birthtime", "")}'


def is_persisted_checkout(repository, checkout):
    return Path(checkout).resolve() == Path(REPOSITORIES[repository]).resolve()


def streamed_git_lines(checkout, arguments):
    command = ['git', '-C', str(checkout), '-c', 'core.quotepath=off', *arguments]
    with tempfile.TemporaryFile() as error_output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_output, env=git_environment())
        yield from process.stdout
        process.stdout.close()
        return_code = process.wait()
        if return_code != 0:
            error_output.seek(0)
            raise subprocess.CalledProcessError(return_code, command, None, error_output.read())


def changed_line_tokens(line):
    if line.isascii():
        return ASCII_TOKEN_PATTERN.findall(line[1:].lower())
    return [token.encode() for token in TOKEN_PATTERN.findall(line[1:].decode('utf-8', errors='replace').casefold())]


def adjacent_pair_bucket(first_token, second_token):
    return zlib.crc32(first_token + b'\0' + second_token) & PAIR_BUCKET_MASK


def commit_header_fields(header):
    revision, timestamp, *parents = header.decode().rstrip('\n').split(' ')
    return revision, int(timestamp), ' '.join(parent for parent in parents if parent)


def scanned_commits(checkout, history_paths, revision_range):
    arguments = [
        'log', '--full-history', '--no-renames', '-p', '-U0', '--no-color', '--no-ext-diff', '--no-textconv',
        '--format=%x1e%H %ct %P', revision_range, '--', *history_paths,
    ]
    commit = None
    is_in_hunk = False
    for line in streamed_git_lines(checkout, arguments):
        if line.startswith(COMMIT_HEADER):
            if commit is not None:
                yield commit
            revision, timestamp, parents = commit_header_fields(line[1:])
            commit = {'revision': revision, 'timestamp': timestamp, 'parents': parents, 'tokens': set(), 'pairs': set()}
            is_in_hunk = False
            continue
        if line.startswith(b'diff --git '):
            is_in_hunk = False
            continue
        if line.startswith(b'@@'):
            is_in_hunk = True
            continue
        if is_in_hunk and line[:1] in (b'+', b'-'):
            tokens = changed_line_tokens(line.rstrip(b'\n'))
            commit['tokens'].update(tokens)
            commit['pairs'].update(map(adjacent_pair_bucket, tokens, tokens[1:]))
    if commit is not None:
        yield commit


def scanned_changes(checkout, history_paths, revision_range):
    arguments = ['log', '--full-history', '--no-renames', '--name-status', '-z', '-m', '--format=%x1e%H %ct %P', revision_range, '--', *history_paths]
    output = b''.join(streamed_git_lines(checkout, arguments))
    changes_by_revision = {}
    for commit_text in output.split(COMMIT_HEADER)[1:]:
        header, _, body = commit_text.partition(b'\0')
        revision, _, parents = commit_header_fields(header)
        is_merge = len(parents.split()) > 1
        fields = body.lstrip(b'\n').split(b'\0')
        changes = changes_by_revision.setdefault(revision, {})
        for status, path in zip(fields[0::2], fields[1::2]):
            status_name = MERGE_STATUS if is_merge else status.decode()[:1]
            changes[path.decode('utf-8', errors='surrogateescape')] = STATUS_CODES.get(status_name, STATUS_CODES['OTHER'])
    return changes_by_revision


def decoded_array(type_code, encoded):
    decoded = array.array(type_code)
    decoded.frombytes(encoded)
    return decoded


def byte_planes(values):
    raw = values.tobytes()
    return b''.join(raw[plane::values.itemsize] for plane in range(values.itemsize))


def from_byte_planes(type_code, planes):
    itemsize = array.array(type_code).itemsize
    if len(planes) % itemsize:
        raise ValueError(f'Byte planes of {len(planes)} bytes do not hold whole {type_code!r} items')
    count = len(planes) // itemsize
    raw = bytearray(len(planes))
    for plane in range(itemsize):
        raw[plane::itemsize] = planes[plane * count:(plane + 1) * count]
    return decoded_array(type_code, bytes(raw))


class PostingTable:
    def __init__(self, keys, offsets, postings):
        self.keys = keys
        self.offsets = offsets
        self.postings = postings

    @classmethod
    def empty(cls, key_type_code):
        return cls(array.array(key_type_code) if key_type_code else [], array.array('Q', [0]), b'')

    def position(self, key):
        position = bisect.bisect_left(self.keys, key)
        return position if position < len(self.keys) and self.keys[position] == key else None

    def count(self, positions):
        return sum(self.offsets[position + 1] - self.offsets[position] for position in positions)

    def deltas(self, position):
        return decoded_array('I', self.postings[self.offsets[position] * POSTING_SIZE:self.offsets[position + 1] * POSTING_SIZE])

    def commit_ids(self, positions):
        commit_ids = set()
        for position in positions:
            commit_ids.update(itertools.accumulate(self.deltas(position)))
        return commit_ids

    def merged(self, new_postings):
        old_keys, old_offsets = self.keys, self.offsets
        chunks = []
        offsets = array.array('Q', [0])

        def copy_unchanged(start, end):
            chunks.append(self.postings[old_offsets[start] * POSTING_SIZE:old_offsets[end] * POSTING_SIZE])
            shift = offsets[-1] - old_offsets[start]
            offsets.extend(map(shift.__add__, old_offsets[start + 1:end + 1]))

        old_cursor = 0
        new_only_keys = []
        for key in sorted(new_postings):
            old_position = bisect.bisect_left(old_keys, key)
            is_existing = old_position < len(old_keys) and old_keys[old_position] == key
            copy_unchanged(old_cursor, old_position)
            old_deltas = self.deltas(old_position) if is_existing else array.array('I')
            appended = new_postings[key]
            deltas = array.array('I', map(int.__sub__, appended, [sum(old_deltas), *appended[:-1]]))
            chunks.append(old_deltas.tobytes() + deltas.tobytes())
            offsets.append(offsets[-1] + len(old_deltas) + len(deltas))
            old_cursor = old_position + 1 if is_existing else old_position
            if not is_existing:
                new_only_keys.append(key)
        copy_unchanged(old_cursor, len(old_keys))
        merged_keys = heapq.merge(old_keys, new_only_keys)
        keys = array.array(old_keys.typecode, merged_keys) if isinstance(old_keys, array.array) else list(merged_keys)
        return PostingTable(keys, offsets, b''.join(chunks))

    def sections(self, prefix):
        counts = array.array('I', map(int.__sub__, self.offsets[1:], self.offsets[:-1]))
        keys = compressed_array(self.keys) if isinstance(self.keys, array.array) else compressed_texts(self.keys)
        return {f'{prefix}_keys': keys, f'{prefix}_counts': compressed_array(counts), f'{prefix}_postings': compressed_array(decoded_array('I', self.postings))}

    @classmethod
    def from_sections(cls, sections, prefix, key_count, posting_count):
        keys_section = sections[f'{prefix}_keys']
        keys = decompressed_texts(keys_section, key_count) if prefix == 'token' else decompressed_array('I', keys_section, key_count)
        counts = decompressed_array('I', sections[f'{prefix}_counts'], key_count)
        offsets = array.array('Q', itertools.accumulate(counts, initial=0))
        if offsets[-1] != posting_count:
            raise ValueError(f'{prefix} counts sum to {offsets[-1]} postings, header says {posting_count}')
        return cls(keys, offsets, decompressed_array('I', sections[f'{prefix}_postings'], posting_count).tobytes())


def compressed_array(values):
    return zlib.compress(byte_planes(values), 6)


def decompressed_array(type_code, section, expected_count):
    values = from_byte_planes(type_code, zlib.decompress(section))
    if len(values) != expected_count:
        raise ValueError(f'Section holds {len(values)} {type_code!r} items, header says {expected_count}')
    return values


def compressed_texts(values):
    joined = '\0'.join(values)
    if joined.count('\0') != max(len(values) - 1, 0):
        raise ValueError('A persisted history index string contains a NUL byte')
    return zlib.compress(joined.encode('utf-8', errors='surrogateescape'), 6)


def decompressed_texts(section, expected_count):
    values = zlib.decompress(section).decode('utf-8', errors='surrogateescape').split('\0') if expected_count else []
    if len(values) != expected_count:
        raise ValueError(f'Section holds {len(values)} strings, header says {expected_count}')
    return values


def empty_state(repository):
    return {
        'version': INDEX_FORMAT_VERSION, 'repository': repository, 'history_paths': HISTORY_PATHS[repository], 'head': None,
        'commits': [], 'timestamps': [], 'parents': [], 'changes': [], 'paths': [],
        'tokens': PostingTable.empty(None), 'pairs': PostingTable.empty('I'),
    }


class IndexSnapshot:
    def __init__(self, repository, checkout, state):
        self.repository = repository
        self.checkout = str(checkout)
        self.state = state
        self.commit_ids = {revision: commit_id for commit_id, revision in enumerate(state['commits'])}
        self.path_ids = {path: path_id for path_id, path in enumerate(state['paths'])}
        self.sorted_paths = sorted(state['paths'])
        self.vocabulary = None
        self.vocabulary_lock = threading.Lock()
        commits_by_path_id = {}
        for commit_id, encoded_changes in enumerate(state['changes']):
            for code in encoded_changes:
                commits_by_path_id.setdefault(code >> STATUS_BITS, []).append((commit_id, code & STATUS_MASK))
        self.commits_by_path_id = commits_by_path_id
        self.touching_cache = {}
        self.token_position_cache = {}

    def merged(self, revision_range, head):
        state = {
            **self.state, 'head': head, 'commits': list(self.state['commits']), 'timestamps': list(self.state['timestamps']),
            'parents': list(self.state['parents']), 'changes': list(self.state['changes']), 'paths': list(self.state['paths']),
        }
        changes_by_revision = scanned_changes(self.checkout, state['history_paths'], revision_range)
        scanned = []
        positions_by_token = {}
        positions_by_pair = {}
        for position, commit in enumerate(scanned_commits(self.checkout, state['history_paths'], revision_range)):
            scanned.append((commit['revision'], commit['timestamp'], commit['parents']))
            for positions_by_key, keys in ((positions_by_token, commit['tokens']), (positions_by_pair, commit['pairs'])):
                for key in keys:
                    positions = positions_by_key.get(key)
                    if positions is None:
                        positions = positions_by_key[key] = array.array('I')
                    positions.append(position)
        unscanned_revisions = set(changes_by_revision) - {revision for revision, _, _ in scanned}
        if unscanned_revisions:
            raise ValueError(f'History index scan of {self.repository} saw path changes without a commit header: {sorted(unscanned_revisions)[:3]}')
        path_ids = dict(self.path_ids)
        newest_commit_id = len(state['commits']) + len(scanned) - 1
        for revision, timestamp, parents in reversed(scanned):
            state['commits'].append(revision)
            state['timestamps'].append(timestamp)
            state['parents'].append(parents)
            encoded_changes = array.array('I')
            for path, status_code in changes_by_revision.get(revision, {}).items():
                if path not in path_ids:
                    path_ids[path] = len(state['paths'])
                    state['paths'].append(path)
                encoded_changes.append(path_ids[path] << STATUS_BITS | status_code)
            state['changes'].append(encoded_changes)

        def commit_ids(positions):
            return array.array('I', map(newest_commit_id.__sub__, reversed(positions)))

        state['tokens'] = state['tokens'].merged({token.decode(): commit_ids(positions) for token, positions in positions_by_token.items()})
        state['pairs'] = state['pairs'].merged({bucket: commit_ids(positions) for bucket, positions in positions_by_pair.items()})
        return IndexSnapshot(self.repository, self.checkout, state)

    def commit_ids_touching(self, pathspecs, include_merges=True, status=None):
        cache_key = (tuple(pathspecs), include_merges, status)
        if cache_key not in self.touching_cache:
            merge_code = STATUS_CODES[MERGE_STATUS]
            self.touching_cache[cache_key] = frozenset(
                commit_id
                for path_id in self.matching_path_ids(pathspecs)
                for commit_id, status_code in self.commits_by_path_id.get(path_id, ())
                if (include_merges or status_code != merge_code) and (status is None or status_code == STATUS_CODES[status])
            )
        return self.touching_cache[cache_key]

    def matching_path_ids(self, pathspecs):
        return set().union(*(self.path_ids_for_pathspec(pathspec) for pathspec in pathspecs))

    def require_indexed_pathspec(self, pathspec):
        pattern = pathspec.removeprefix(GLOB_MAGIC)
        wildcard = DEFAULT_PATHSPEC_WILDCARDS.search(pattern)
        fixed_directory = pattern[:wildcard.start()].rpartition('/')[0] if wildcard else pattern.rstrip('/')
        history_paths = self.state['history_paths']
        if not any(fixed_directory == history_path or fixed_directory.startswith(history_path + '/') for history_path in history_paths):
            raise ValueError(f'Pathspec {pathspec!r} reaches outside the {self.repository} history index paths {history_paths}')

    def path_ids_for_pathspec(self, pathspec):
        if pathspec.startswith(':') and not pathspec.startswith(GLOB_MAGIC):
            raise ValueError(f'Unsupported pathspec magic in {pathspec!r}')
        self.require_indexed_pathspec(pathspec)
        if pathspec.startswith(GLOB_MAGIC):
            pattern = glob_pathspec_regex(pathspec.removeprefix(GLOB_MAGIC))
            return {path_id for path, path_id in self.path_ids.items() if pattern.fullmatch(path)}
        literal = pathspec.rstrip('/')
        path_ids = {self.path_ids[literal]} if literal in self.path_ids else set()
        directory_prefix = literal + '/'
        start = bisect.bisect_left(self.sorted_paths, directory_prefix)
        path_ids.update(self.path_ids[path] for path in itertools.takewhile(lambda path: path.startswith(directory_prefix), self.sorted_paths[start:]))
        if DEFAULT_PATHSPEC_WILDCARDS.search(pathspec):
            path_ids.update(path_id for path, path_id in self.path_ids.items() if fnmatch.fnmatchcase(path, pathspec))
        return path_ids

    def token_positions(self, run, mode):
        cache_key = (run, mode)
        if cache_key not in self.token_position_cache:
            self.token_position_cache[cache_key] = self.uncached_token_positions(run, mode)
        return self.token_position_cache[cache_key]

    def uncached_token_positions(self, run, mode):
        tokens = self.state['tokens'].keys
        start = bisect.bisect_left(tokens, run)
        if mode == 'exact':
            return [start] if start < len(tokens) and tokens[start] == run else []
        if mode == 'prefix':
            end = bisect.bisect_left(tokens, run + '\x7f', lo=start)
            if end - start > MAX_EXPANDED_TOKENS:
                raise TooManyTokens(run)
            return list(range(start, end))
        vocabulary, token_starts = self.token_vocabulary()
        positions = set()
        offset = vocabulary.find(run)
        while offset != -1:
            position = bisect.bisect_right(token_starts, offset) - 1
            token_end = token_starts[position + 1] - 1
            if offset + len(run) <= token_end and (mode == 'contains' or offset + len(run) == token_end):
                positions.add(position)
            if len(positions) > MAX_EXPANDED_TOKENS:
                raise TooManyTokens(run)
            offset = vocabulary.find(run, offset + 1)
        return sorted(positions)

    def token_vocabulary(self):
        vocabulary = self.vocabulary
        if vocabulary is not None:
            return vocabulary
        with self.vocabulary_lock:
            if self.vocabulary is None:
                tokens = self.state['tokens'].keys
                token_starts = array.array('Q', itertools.accumulate((len(token) + 1 for token in tokens), initial=0))
                self.vocabulary = ('\n'.join(tokens), token_starts)
            return self.vocabulary

    def commit_ids_matching(self, constraints):
        tables_and_positions = []
        for run, mode in constraints:
            try:
                tables_and_positions.append((self.state['tokens'], self.token_positions(run, mode)))
            except TooManyTokens:
                continue
        pairs = self.state['pairs']
        for (first_run, first_mode), (second_run, second_mode) in zip(constraints, constraints[1:]):
            if first_mode != 'exact' or second_mode != 'exact':
                continue
            position = pairs.position(adjacent_pair_bucket(first_run.encode(), second_run.encode()))
            tables_and_positions.append((pairs, [] if position is None else [position]))
        if not tables_and_positions:
            return None
        tables_and_positions.sort(key=lambda table_and_positions: table_and_positions[0].count(table_and_positions[1]))
        matched = None
        for table, positions in tables_and_positions:
            matched = table.commit_ids(positions) if matched is None else matched & table.commit_ids(positions)
            if not matched:
                break
        return matched

    def revision(self, commit_id):
        return self.state['commits'][commit_id]

    def commit_line(self, revision):
        commit_id = self.commit_ids[revision]
        return {'revision': revision, 'timestamp': self.state['timestamps'][commit_id], 'parents': self.state['parents'][commit_id]}


class HistoryIndex:
    def __init__(self, repository, checkout, state):
        self.repository = repository
        self.checkout = str(checkout)
        self.lock = threading.Lock()
        self.snapshot = IndexSnapshot(repository, checkout, state)

    def refresh(self):
        with self.lock:
            head = git(self.checkout, 'rev-parse', 'HEAD').strip()
            base = self.snapshot
            previous_head = base.state['head']
            if previous_head == head:
                return False
            if previous_head is not None and not (is_commit_present(self.checkout, previous_head) and is_ancestor(self.checkout, previous_head, head)):
                base = IndexSnapshot(self.repository, self.checkout, empty_state(self.repository))
                previous_head = None
            self.snapshot = base.merged(f'{previous_head}..{head}' if previous_head else head, head)
            return True


def is_commit_present(checkout, revision):
    found = subprocess.run(
        ['git', '-C', str(checkout), 'cat-file', '--batch-check'], input=f'{revision}\n'.encode(), capture_output=True, check=True, env=git_environment(),
    ).stdout.decode().split()
    if found[1:2] == ['missing']:
        return False
    if found[1:2] == ['commit']:
        return True
    raise ValueError(f'Previously indexed head {revision} of {checkout} is not a commit: {found}')


def is_ancestor(checkout, ancestor, descendant):
    completed = subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', ancestor, descendant], capture_output=True, env=git_environment())
    if completed.returncode not in (0, 1):
        raise subprocess.CalledProcessError(completed.returncode, completed.args, completed.stdout, completed.stderr)
    return completed.returncode == 0


def glob_pathspec_regex(pattern):
    replacements = {'**/': '(?:.*/)?', '/**': '/.*', '*': '[^/]*', '?': '[^/]'}
    translated = re.sub(r'\*\*/|/\*\*$|\*|\?|[^*?]+', lambda match: replacements.get(match.group(0)) or re.escape(match.group(0)), pattern)
    return re.compile(translated + '(?:/.*)?')


def literal_constraints(literal, is_word_bounded=False):
    folded = literal.casefold()
    modes = {(True, True): 'exact', (True, False): 'prefix', (False, True): 'suffix', (False, False): 'contains'}
    return [
        (run.group(), modes[(is_word_bounded or run.start() > 0, is_word_bounded or run.end() < len(folded))])
        for run in TOKEN_PATTERN.finditer(folded)
    ]


def load_index(repository, checkout):
    if not is_persisted_checkout(repository, checkout):
        return HistoryIndex(repository, checkout, empty_state(repository))
    try:
        state = persisted_state(repository, checkout)
    except (OSError, ValueError, KeyError, TypeError, zlib.error) as failure:
        print(f'{repository}: history index unreadable ({failure!r}); rebuilding from full history', file=sys.stderr, flush=True)
        state = None
    return HistoryIndex(repository, checkout, state or empty_state(repository))


def current_header(repository, checkout):
    _, header_file = index_files(repository)
    if not header_file.exists():
        return None
    header = parsed_header(repository, header_file)
    is_current = header is not None and (
        header.get('version') == INDEX_FORMAT_VERSION and header.get('repository') == repository
        and header.get('history_paths') == HISTORY_PATHS[repository] and header.get('checkout_identity') == checkout_identity(checkout)
    )
    return header if is_current else None


def parsed_header(repository, header_file):
    try:
        header = json.loads(header_file.read_text())
    except ValueError as failure:
        print(f'{repository}: history index header unreadable ({failure!r}); treating the index as absent', file=sys.stderr, flush=True)
        return None
    if not isinstance(header, dict):
        print(f'{repository}: history index header is not a JSON object; treating the index as absent', file=sys.stderr, flush=True)
        return None
    return header


def persisted_state(repository, checkout):
    header = current_header(repository, checkout)
    if header is None:
        return None
    index_file, _ = index_files(repository)
    payload = index_file.read_bytes()
    if hashlib.sha256(payload).hexdigest() != header['sha256']:
        raise ValueError(f'{index_file} does not match the SHA-256 recorded in its header')
    sections = {name: payload[offset:offset + length] for name, (offset, length) in header['sections'].items()}
    commit_count = header['commits']
    change_counts = decompressed_array('I', sections['change_counts'], commit_count)
    flat_changes = decompressed_array('I', sections['changes'], sum(change_counts))
    change_offsets = list(itertools.accumulate(change_counts, initial=0))
    return {
        'version': INDEX_FORMAT_VERSION, 'repository': repository, 'history_paths': HISTORY_PATHS[repository], 'head': header['head'],
        'commits': decompressed_texts(sections['commits'], commit_count),
        'timestamps': decompressed_array('q', sections['timestamps'], commit_count).tolist(),
        'parents': decompressed_texts(sections['parents'], commit_count),
        'changes': [flat_changes[start:end] for start, end in zip(change_offsets, change_offsets[1:])],
        'paths': decompressed_texts(sections['paths'], header['paths']),
        'tokens': PostingTable.from_sections(sections, 'token', header['tokens'], header['postings']),
        'pairs': PostingTable.from_sections(sections, 'pair', header['pair_keys'], header['pair_postings']),
    }


def save_index(index):
    index_file, header_file = index_files(index.repository)
    INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    state = index.state
    sections = {
        'commits': compressed_texts(state['commits']), 'timestamps': compressed_array(array.array('q', state['timestamps'])),
        'parents': compressed_texts(state['parents']), 'paths': compressed_texts(state['paths']),
        'change_counts': compressed_array(array.array('I', map(len, state['changes']))),
        'changes': compressed_array(array.array('I', itertools.chain.from_iterable(state['changes']))),
        **state['tokens'].sections('token'), **state['pairs'].sections('pair'),
    }
    section_offsets = list(itertools.accumulate(map(len, sections.values()), initial=0))
    payload = b''.join(sections.values())
    candidate = index_file.with_suffix('.partial')
    candidate.write_bytes(payload)
    candidate.replace(index_file)
    header = {
        'version': INDEX_FORMAT_VERSION, 'repository': index.repository, 'history_paths': state['history_paths'],
        'checkout_identity': checkout_identity(index.checkout), 'head': state['head'],
        'commits': len(state['commits']), 'paths': len(state['paths']), 'tokens': len(state['tokens'].keys), 'postings': state['tokens'].offsets[-1],
        'pair_keys': len(state['pairs'].keys), 'pair_postings': state['pairs'].offsets[-1],
        'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
        'sections': {name: [offset, len(section)] for (name, section), offset in zip(sections.items(), section_offsets)},
    }
    header_candidate = header_file.with_suffix('.partial-header')
    header_candidate.write_text(json.dumps(header, indent=2, sort_keys=True) + '\n')
    header_candidate.replace(header_file)


def indexed_head(repository, checkout):
    if not is_persisted_checkout(repository, checkout):
        return None
    header = current_header(repository, checkout)
    return header['head'] if header else None


def repository_index(repository, checkout):
    cache_key = (repository, str(Path(checkout).resolve()))
    with INDEXES_LOCK:
        index = INDEXES_BY_CHECKOUT.get(cache_key)
        if index is None:
            index = load_index(repository, checkout)
            INDEXES_BY_CHECKOUT[cache_key] = index
        if index.refresh() and is_persisted_checkout(repository, checkout):
            save_index(index.snapshot)
        return index.snapshot


ORDER_OPTIONS = {'date': ['--date-order'], 'default': []}


@functools.lru_cache(maxsize=16)
def walk_positions(checkout, head, order):
    return {revision: position for position, revision in enumerate(git(checkout, 'rev-list', *ORDER_OPTIONS[order], head, timeout=None).split())}


@functools.lru_cache(maxsize=256)
def range_revisions(checkout, revision_range):
    return frozenset(git(checkout, 'rev-list', revision_range, timeout=None).split())


def range_pinned_to_head(revision_range, head):
    start, separator, end = revision_range.rpartition('..')
    if separator and end == 'HEAD':
        return f'{start}..{head}'
    if separator and FULL_REVISION.fullmatch(end):
        return revision_range
    raise ValueError(f'Revision range {revision_range!r} must end at HEAD or a full commit id so its rev-list can be cached')


def revisions_in_order(index, commit_ids, revision_range=None, oldest_first=False, order='date'):
    revisions = [index.revision(commit_id) for commit_id in commit_ids]
    if revision_range is not None:
        allowed = range_revisions(index.checkout, range_pinned_to_head(revision_range, index.state['head']))
        revisions = [revision for revision in revisions if revision in allowed]
    positions = walk_positions(index.checkout, index.state['head'], order)
    unordered = [revision for revision in revisions if revision not in positions]
    if unordered:
        raise ValueError(f'History index of {index.repository} holds commits outside HEAD: {unordered[:3]}')
    return sorted(revisions, key=positions.__getitem__, reverse=oldest_first)


def json_ready(value):
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


def plain_walk_options(revision_range=None, oldest_first=False, order='date'):
    return ['--full-history', *ORDER_OPTIONS[order], *(['--reverse'] if oldest_first else []), revision_range or 'HEAD']


def pickaxe_selection(arguments):
    selection = []
    position = 0
    while position < len(arguments):
        argument = arguments[position]
        position += 1
        if argument in PICKAXE_VALUE_OPTIONS and position < len(arguments):
            selection += [argument, arguments[position]]
            position += 1
            continue
        if argument in PICKAXE_FLAGS:
            selection.append(argument)
            continue
        if not PRESENTATION_OPTION.fullmatch(argument):
            raise ValueError(f'Unsupported pickaxe log argument {argument!r} in {arguments!r}')
    if not any(argument in PICKAXE_VALUE_OPTIONS for argument in selection):
        raise ValueError(f'Pickaxe log arguments {arguments!r} carry no -G or -S')
    return selection


def path_history_from_git(repository, checkout, pathspecs, revision_range=None, include_merges=True, status=None, oldest_first=False, order='date'):
    filters = [*([] if include_merges else ['--no-merges']), *([f'--diff-filter={status}'] if status else [])]
    output = git(checkout, 'log', *plain_walk_options(revision_range, oldest_first, order), '--no-renames', '--format=%H %ct %P', *filters, '--', *pathspecs, timeout=None)
    return [dict(zip(('revision', 'timestamp', 'parents'), commit_header_fields(line.encode()))) for line in output.splitlines()]


def pickaxe_candidates_from_git(repository, checkout, selection, pathspecs, revision_range=None, oldest_first=False, order='date'):
    return git(checkout, 'log', *plain_walk_options(revision_range, oldest_first, order), '--format=%H', *selection, '--', *pathspecs, timeout=None).split()


def replayable(record_from_git, unkeyed_options=()):
    def replaying(query):
        @functools.wraps(query)
        def answered(repository, checkout, *arguments, **options):
            if QUERY_FIXTURE['mode'] is None or not is_persisted_checkout(repository, checkout):
                return query(repository, checkout, *arguments, **options)
            keyed_options = {name: value for name, value in options.items() if name not in unkeyed_options}
            key = json.dumps([query.__name__, repository, json_ready(arguments), json_ready(sorted(keyed_options.items()))])
            if QUERY_FIXTURE['mode'] == 'record':
                QUERY_FIXTURE['answers'][key] = record_from_git(repository, checkout, *arguments, **keyed_options)
            if key not in QUERY_FIXTURE['answers']:
                raise MissingFixtureAnswer(f'History fixture {FIXTURE_FILE} has no answer for {key}; rerun attribution_test.py --record-history-fixture')
            return QUERY_FIXTURE['answers'][key]
        return answered
    return replaying


@contextlib.contextmanager
def query_fixture(mode):
    answers = json.loads(FIXTURE_FILE.read_text()) if mode == 'replay' else {}
    QUERY_FIXTURE.update(mode=mode, answers=answers)
    try:
        yield
    finally:
        if mode == 'record':
            FIXTURE_FILE.write_text(json.dumps(QUERY_FIXTURE['answers'], indent=1, sort_keys=True) + '\n')
        QUERY_FIXTURE.update(mode=None, answers={})


@replayable(path_history_from_git)
def path_history(repository, checkout, pathspecs, revision_range=None, include_merges=True, status=None, oldest_first=False, order='date'):
    index = repository_index(repository, checkout)
    revisions = revisions_in_order(index, index.commit_ids_touching(pathspecs, include_merges, status), revision_range, oldest_first, order)
    return [index.commit_line(revision) for revision in revisions]


@replayable(pickaxe_candidates_from_git, unkeyed_options=('alternatives',))
def pickaxe_candidates(repository, checkout, selection, pathspecs, revision_range=None, oldest_first=False, order='date', alternatives=()):
    index = repository_index(repository, checkout)
    touching = index.commit_ids_touching(pathspecs, include_merges=False)
    candidates = set()
    for constraints in alternatives:
        matched = index.commit_ids_matching(constraints)
        candidates |= touching if matched is None else touching & matched
    return revisions_in_order(index, candidates, revision_range, oldest_first, order)


def log_for_revisions(checkout, revisions, arguments, pathspecs, timeout=None):
    if not revisions:
        return ''
    return subprocess.run(
        ['git', '-C', str(checkout), '-c', 'core.quotepath=off', 'log', '--no-walk=unsorted', '--stdin', *arguments, '--', *pathspecs],
        input=''.join(f'{revision}\n' for revision in revisions).encode(), capture_output=True, check=True, timeout=timeout, env=git_environment(),
    ).stdout.decode('utf-8', errors='replace')


def pickaxe_log(repository, checkout, alternatives, arguments, pathspecs, revision_range=None, oldest_first=False, order='date', timeout=None):
    selection = pickaxe_selection(arguments)
    revisions = pickaxe_candidates(repository, checkout, selection, pathspecs, revision_range, oldest_first, order, alternatives=alternatives)
    return log_for_revisions(checkout, revisions, arguments, pathspecs, timeout)


def refresh_all(repositories):
    for repository in repositories:
        started = time.monotonic()
        checkout = REPOSITORIES[repository]
        previous_head = indexed_head(repository, checkout)
        head = git(checkout, 'rev-parse', 'HEAD').strip()
        if previous_head != head:
            repository_index(repository, checkout)
        _, header_file = index_files(repository)
        header = json.loads(header_file.read_text())
        scanned = {None: 'full history', head: 'nothing new'}.get(previous_head, f'{str(previous_head)[:12]}..{head[:12]}')
        print(
            f'{repository}: indexed {scanned} in {time.monotonic() - started:.2f}s; {header["commits"]} commits, '
            f'{header["tokens"]} tokens, {header["postings"]} token postings, {header["pair_postings"]} pair postings, {header["bytes"] / 1e6:.1f} MB',
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', choices=HISTORY_PATHS, action='append')
    arguments = parser.parse_args()
    refresh_all(arguments.repository or list(HISTORY_PATHS))


if __name__ == '__main__':
    doccoverage.run_at_background_priority()
    main()
