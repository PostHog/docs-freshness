import argparse
import ast
import bisect
import json
import os
import re
import shutil
import sys
from collections import defaultdict, namedtuple
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote


TOOL_ROOT = Path(__file__).resolve().parent
OUTPUT_ROOT = Path(os.environ.get('DOCAUDIT_OUTPUT_ROOT', TOOL_ROOT / 'output'))
SITE_ROOT = Path(os.environ.get('DOCAUDIT_SITE_ROOT', '/Users/johnw/projects/posthog.com-worktrees/master'))
MONOREPO_ROOT = Path(os.environ.get('DOCAUDIT_MONOREPO_ROOT', '/Users/johnw/projects/posthog/posthog'))
POSTHOG_JS_ROOT = Path(os.environ.get('DOCAUDIT_POSTHOG_JS_ROOT', '/Users/johnw/projects/posthog/posthog-js'))
POSTHOG_PYTHON_ROOT = Path(os.environ.get('DOCAUDIT_POSTHOG_PYTHON_ROOT', '/Users/johnw/projects/posthog/posthog-python'))
LIVE_ROUTES_FILE = TOOL_ROOT / 'data' / 'live-routes.txt'
BACKGROUND_PRIORITY_MARKER = 'DOCAUDIT_BACKGROUND_PRIORITY'
BACKGROUND_NICENESS = 19


def lower_cpu_priority():
    if os.getpriority(os.PRIO_PROCESS, 0) < BACKGROUND_NICENESS:
        os.setpriority(os.PRIO_PROCESS, 0, BACKGROUND_NICENESS)


def run_at_background_priority():
    taskpolicy = shutil.which('taskpolicy')
    if os.environ.get(BACKGROUND_PRIORITY_MARKER) or taskpolicy is None:
        lower_cpu_priority()
        return
    os.environ[BACKGROUND_PRIORITY_MARKER] = '1'
    os.execv(taskpolicy, [taskpolicy, '-b', sys.executable, *sys.argv])
GITHUB_REPOSITORY_NAMES = {'site': 'posthog.com', 'monorepo': 'posthog', 'posthog-js': 'posthog-js', 'posthog-python': 'posthog-python'}


def docs_page_url(page):
    route = Path(page).with_suffix('').as_posix()
    if route == 'index':
        return 'https://posthog.com/docs'
    return 'https://posthog.com/docs/' + quote(route.removesuffix('/index'), safe='/')


def github_commit_url(repository, revision):
    return f'https://github.com/PostHog/{GITHUB_REPOSITORY_NAMES[repository]}/commit/{revision}'


def github_blob_url(repository, revision, path, line):
    return f'https://github.com/PostHog/{GITHUB_REPOSITORY_NAMES[repository]}/blob/{revision}/{quote(path)}#L{line}'


def text_fragment_url(page_url, heading_anchor, claim_words):
    encoded_words = quote(' '.join(claim_words.split()), safe='').replace('-', '%2D')
    return f'{page_url}#{heading_anchor}:~:text={encoded_words}'

SOURCE_NAMES = (
    'js-config', 'js-methods', 'python-methods', 'python-config',
    'settings', 'settings-sections', 'settings-gated', 'js-changelog', 'python-changelog',
)
GENERIC_TITLE_MAX_WORDS = 2
CHANGELOG_BUMP_HEADING_PATTERN = re.compile(r'\b(?:major|minor|patch)\b', flags=re.I)
SETTING_LEVEL_PREFIX = re.compile(r'^(?:environment|project|organization|user)-')
MIN_SETTING_TERM_LENGTH = 3
SETTING_TITLE_FILLER_WORDS = {'a', 'an', 'and', 'default', 'for', 'in', 'my', 'new', 'of', 'on', 'or', 'the', 'to', 'with', 'your'}
FENCED_CODE_PATTERN = re.compile(r'^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[^\n]*$', flags=re.M | re.S)
SETTING_HEADING_SPANS = re.compile(r'^#{1,6}[ \t][^\n]*$|^title:[^\n]*$|\btitle=\{?["\'][^"\'\n]+["\']', flags=re.M)
UNPUBLISHED_IDENTIFIER_PATTERN = re.compile(r'^(?:resolve|detect|render)(?:[A-Z_]|$)|^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^\$sdk_debug_')
COMMON_DOCUMENTED_EVENTS = {'$exception', '$pageview', '$ai_trace', '$groupidentify'}
WARNING_ENTRY_PATTERN = re.compile(r'\bwarn(?:s|ing|ings)?\b', flags=re.I)
FIX_ENTRY_PATTERN = re.compile(r'^fix(?:\([^)]*\))?!?:', flags=re.I)
PREVIEW_ENTRY_PATTERN = re.compile(r'\bpreview flag\b', flags=re.I)
SOURCE_TOKEN_PATTERN = re.compile(r'\$?[A-Za-z_]\w*')
DISPLAY_ONLY_JSX_TAGS = {'div', 'span', 'p', 'strong', 'em', 'Link'}
DOTTED_PARENT_MAX_LINE_DISTANCE = 10
TITLE_STATUS_SUFFIX = re.compile(r'\s+\(?(?:beta|experimental|alpha)\)?$', flags=re.I)
SETTINGS_SECTION_DOCS_DIRECTORIES = {
    'replay': 'session-replay', 'tracing': 'distributed-tracing', 'error-tracking-configuration': 'error-tracking',
    'marketing-analytics': 'web-analytics', 'autocapture': 'product-analytics', 'heatmaps': 'toolbar',
    'max': 'posthog-ai', 'task-agents': 'posthog-ai', 'business-knowledge': 'posthog-ai', 'conversations': 'support',
    'csp-reporting': 'csp-tracking', 'data-quality': 'data', 'posthog-mcp': 'model-context-protocol',
    'mcp-servers': 'model-context-protocol',
}
EXPORTED_DECLARATION_PATTERN = re.compile(
    r'^export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?'
    r'(?:function\*?|class|interface|type|const|let|var|enum)\s+([A-Za-z_$][\w$]*)',
    flags=re.M,
)
EXPORT_LIST_PATTERN = re.compile(r'^export\s+(?:type\s+)?\{([^}]*)\}', flags=re.M)
EXPORT_FROM_PATTERN = re.compile(r'^export\s+(?:type\s+)?(?:\*|\{[^}]*\})\s+from\s+[\'"]([^\'"]+)[\'"]', flags=re.M)
WORD_PATTERN = re.compile(r'\b\w+\b')
IDENTIFIER_PATTERN = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')
BACKTICK_PATTERN = re.compile(r'`([^`]+)`')
VERSION_PATTERN = re.compile(r'(\d+)\.(\d+)\.(\d+)')
PROPERTY_PATTERN = re.compile(r'^    ([A-Za-z_]\w*|[\'\"][^\'\"]+[\'\"])[?!]?\s*:\s*(.*)')
MEMBER_MODIFIERS = r'(?:(?:public|private|protected|static|readonly|async|declare|override)\s+)*'
METHOD_PATTERN = re.compile(r'^    ' + MEMBER_MODIFIERS + r'(?:get\s+)?([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(')
ARROW_METHOD_PATTERN = re.compile(
    r'^    ' + MEMBER_MODIFIERS + r'([A-Za-z_]\w*)\s*(?::[^=]*)?=\s*(?:async\s*)?(?:\(|[A-Za-z_]\w*\s*=>)'
)
TYPE_DECLARATION_PATTERN = re.compile(
    r'^export (?:interface (\w+)(?:<[^>]*>)?(?:\s+extends\s+([^{]+))?\s*\{'
    r'|type (\w+)(?:<[^>]*>)?\s*=\s*((?:[A-Za-z_]\w*\s*&\s*)*)\{)'
)


def is_excluded(name, preceding_text=''):
    is_hidden_by_comment = bool(re.search(r'@(deprecated|internal)\b|posthog support guidance', preceding_text, flags=re.I))
    return name.startswith('_') or '__preview' in name or is_hidden_by_comment


def preceding_jsdoc(lines, line_number):
    previous_line = line_number - 1
    if previous_line < 0 or not lines[previous_line].strip().endswith('*/'):
        return ''
    first_line = previous_line
    while first_line >= 0 and '/**' not in lines[first_line]:
        first_line -= 1
    if first_line < 0:
        return ''
    return '\n'.join(lines[first_line:line_number])


def interface_members(types_root):
    own_members = {}
    bases_by_type = {}
    for path in sorted(types_root.glob('*.ts')):
        lines = path.read_text(encoding='utf-8').splitlines()
        interface_name = ''
        for line_number, line in enumerate(lines):
            declaration = TYPE_DECLARATION_PATTERN.match(line)
            if declaration:
                interface_name = declaration.group(1) or declaration.group(3)
                base_text = declaration.group(2) or declaration.group(4) or ''
                bases_by_type[interface_name] = re.findall(r'\b([A-Za-z_]\w*)\b(?![^<]*>)', base_text.replace('&', ','))
                own_members[interface_name] = []
                continue
            if interface_name and line.startswith('}'):
                interface_name = ''
                continue
            if not interface_name:
                continue
            property_match = PROPERTY_PATTERN.match(line)
            if not property_match:
                continue
            name = property_match.group(1).strip('\'"')
            if is_excluded(name, preceding_jsdoc(lines, line_number)):
                continue
            own_members[interface_name].append((name, property_match.group(2), str(path)))
    return {name: inherited_members(name, own_members, bases_by_type, ()) for name in own_members}


def inherited_members(type_name, own_members, bases_by_type, visited_types):
    members = list(own_members.get(type_name, []))
    member_names = {name for name, _, _ in members}
    for base in bases_by_type.get(type_name, []):
        if base in visited_types or base == type_name:
            continue
        for member in inherited_members(base, own_members, bases_by_type, (*visited_types, type_name)):
            if member[0] not in member_names:
                members.append(member)
                member_names.add(member[0])
    return members


def js_config_inventory(js_root):
    interfaces = interface_members(js_root / 'packages/types/src')
    options = {}
    for name, property_type, path in interfaces.get('PostHogConfig', []):
        options[name] = {'name': name, 'origin': path}
        for referenced_interface in sorted(set(re.findall(r'\b[A-Za-z_]\w*(?:Config|Options)\b', property_type))):
            if referenced_interface == 'PostHogConfig':
                continue
            for nested_name, _, nested_path in interfaces.get(referenced_interface, []):
                dotted_name = f'{name}.{nested_name}'
                options[dotted_name] = {'name': dotted_name, 'origin': nested_path}
    return list(options.values())


def js_method_inventory(js_root):
    path = js_root / 'packages/browser/src/posthog-core.ts'
    lines = path.read_text(encoding='utf-8').splitlines()
    class_start = next(index for index, line in enumerate(lines) if re.match(r'^export class PostHog\b', line))
    methods = {}
    for line_number in range(class_start + 1, len(lines)):
        line = lines[line_number]
        if line.startswith('}'):
            break
        member = METHOD_PATTERN.match(line) or ARROW_METHOD_PATTERN.match(line)
        if not member or re.match(r'^    (?:private|protected)\b', line):
            continue
        name = member.group(1)
        if name in {'constructor', 'get', 'set'} or is_excluded(name, preceding_jsdoc(lines, line_number)):
            continue
        if is_overload_implementation(lines, line_number, name):
            continue
        methods[name] = {'name': name, 'origin': str(path)}
    return list(methods.values())


def is_overload_implementation(lines, line_number, name):
    previous_line = lines[line_number - 1]
    previous_member = METHOD_PATTERN.match(previous_line)
    return bool(previous_member) and previous_member.group(1) == name and not previous_line.rstrip().endswith('{')


def python_tree(path):
    return ast.parse(path.read_text(encoding='utf-8'), filename=str(path))


def python_function_is_excluded(function):
    function_docstring = ast.get_docstring(function) or ''
    return is_excluded(function.name, function_docstring)


def python_client(python_root):
    path = python_root / 'posthog/client.py'
    client = next(node for node in python_tree(path).body if isinstance(node, ast.ClassDef) and node.name == 'Client')
    return path, client


def python_method_inventory(python_root):
    client_path, client = python_client(python_root)
    module_path = python_root / 'posthog/__init__.py'
    methods = {}
    for function in client.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or python_function_is_excluded(function):
            continue
        methods[function.name] = {'name': function.name, 'origin': str(client_path)}
    for function in python_tree(module_path).body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or python_function_is_excluded(function):
            continue
        methods[function.name] = {'name': function.name, 'origin': str(module_path)}
    return list(methods.values())


def python_config_inventory(python_root):
    path, client = python_client(python_root)
    initializer = next(node for node in client.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    parameters = initializer.args.posonlyargs + initializer.args.args + initializer.args.kwonlyargs
    return [{'name': parameter.arg, 'origin': str(path)} for parameter in parameters if not is_excluded(parameter.arg) and parameter.arg != 'self']


def settings_title(lines, line_number):
    for title_line_number in range(line_number, min(line_number + 12, len(lines))):
        next_line = lines[title_line_number]
        title_match = re.search(r"\btitle:\s*['\"]([^'\"]+)['\"]", next_line)
        if title_match:
            return title_match.group(1)
        if re.search(r'\btitle:\s*\(', next_line):
            title_lines = []
            for jsx_line in lines[title_line_number + 1:title_line_number + 12]:
                if jsx_line.strip() == '),':
                    break
                plain_text = re.sub(r'\{[^}]*\}', '', jsx_line).strip()
                if plain_text and '<' not in plain_text and '>' not in plain_text:
                    title_lines.append(plain_text)
            return ' '.join(title_lines)
        if re.search(r'\btitle:\s*null', next_line):
            return ''
    return ''


def settings_map_entries(mono_root):
    path = Path(mono_root) / 'frontend/src/scenes/settings/SettingsMap.tsx'
    return settings_map_entries_from_text(path.read_text(encoding='utf-8'), path)


def settings_map_entries_from_text(settings_map_text, path):
    lines = settings_map_text.splitlines()
    entries = []
    settings_array_indent = None
    current_section = ''
    current_section_title = ''
    current_section_index = 0
    current_level = ''
    for line_number, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if settings_array_indent is not None and indent <= settings_array_indent and line.strip().startswith(']'):
            settings_array_indent = None
            continue
        if re.match(r'\s*settings:\s*\[', line):
            settings_array_indent = None if re.match(r'\s*settings:\s*\[.*\]', line) else indent
            continue
        level = re.match(r"\s*level:\s*['\"]([^'\"]+)['\"]", line)
        if level and settings_array_indent is None:
            current_level = level.group(1)
            continue
        identifier = re.search(r"\bid:\s*['\"]([^'\"]+)['\"]", line)
        if not identifier:
            continue
        name = identifier.group(1)
        is_setting = settings_array_indent is not None
        properties = settings_entry_properties(lines, line_number, indent, is_setting)
        flag_array = re.search(r'^\s*flag:\s*\[([^\]]*)\]', properties, flags=re.M)
        is_gated = bool(
            re.search(r"^\s*flag:\s*['\"](?!!)", properties, flags=re.M)
            or (flag_array and any(not flag.startswith('!') for flag in re.findall(r"['\"]([^'\"]+)['\"]", flag_array.group(1))))
            or re.search(r'^\s*hideFromNavigation:\s*true', properties, flags=re.M)
            or re.search(r'^\s*title:\s*null', properties, flags=re.M)
        )
        title = settings_title(lines, line_number)
        if not is_setting:
            current_section = name
            current_section_title = title
            current_section_index = len(entries)
        entry_block = settings_entry_block(lines, line_number, indent)
        docs_url = re.search(r"^\s*docsUrl:\s*['\"]([^'\"]+)['\"]", properties, flags=re.M)
        docs_url_symbol = re.search(r'^\s*docsUrl:\s*([A-Za-z_$][\w$]*)\.docsUrl\b', properties, flags=re.M)
        is_display_only = is_setting and is_display_only_setting(properties, settings_entry_component(lines, line_number, indent))
        entries.append({
            'name': name, 'title': title, 'origin': str(path),
            'kind': 'setting' if is_setting else 'section',
            'section': current_section if is_setting else name, 'section_title': current_section_title, 'level': current_level,
            'keywords': settings_entry_keywords(entry_block, indent),
            'descriptions': [
                description for description in (settings_entry_string(entry_block, indent, key) for key in ('description', 'searchDescription'))
                if description
            ],
            'is_gated': is_gated or is_display_only,
            'docs_url': docs_url.group(1) if docs_url else symbol_docs_url(docs_url_symbol.group(1), lines, path) if docs_url_symbol else '',
            'section_index': current_section_index if is_setting else len(entries),
        })
    first_section_by_level = {}
    for entry in entries:
        entry['is_gated'] = entry['is_gated'] or entries[entry['section_index']]['is_gated']
        if entry['kind'] == 'section':
            first_section_by_level.setdefault('project' if entry['level'] == 'environment' else entry['level'], entry['name'])
    level_links_by_section = defaultdict(list)
    for level, section in first_section_by_level.items():
        level_links_by_section[section].extend(('environment', 'project') if level == 'project' else (level,))
    for entry in entries:
        entry['section_links'] = sorted(equivalent_section_ids(entry['section']) | set(level_links_by_section[entry['section']]))
    return entries


def equivalent_section_ids(section):
    level_prefix = SETTING_LEVEL_PREFIX.match(section)
    if level_prefix and level_prefix.group(0) in ('environment-', 'project-'):
        product = section.removeprefix(level_prefix.group(0))
        return {section, f'environment-{product}', f'project-{product}'}
    return {section}


def indentation(line):
    return len(line) - len(line.lstrip())


def settings_entry_block(lines, line_number, indent):
    start = line_number
    while start > 0 and (not lines[start - 1].strip() or indentation(lines[start - 1]) >= indent):
        start -= 1
    end = line_number
    while end < len(lines) and (not lines[end].strip() or indentation(lines[end]) >= indent):
        end += 1
    return '\n'.join(lines[start:end])


def settings_entry_keywords(entry_block, indent):
    keyword_array = re.search(rf'^ {{{indent}}}keywords:\s*\[(.*?)\]', entry_block, flags=re.M | re.S)
    if not keyword_array:
        return []
    return [single or double for single, double in re.findall(r"'([^'\n]+)'|\"([^\"\n]+)\"", keyword_array.group(1))]


def settings_entry_string(entry_block, indent, key):
    value = re.search(rf'^ {{{indent}}}{key}:\s*(?:\'((?:\\.|[^\'\\])*)\'|"((?:\\.|[^"\\])*)")', entry_block, flags=re.M)
    if not value:
        return ''
    return re.sub(r'\\(.)', r'\1', value.group(1) if value.group(1) is not None else value.group(2))


def settings_entry_component(lines, line_number, indent):
    component_lines = []
    for next_line in lines[line_number:]:
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_line.strip() and next_indent < indent:
            break
        if component_lines and next_line.strip() and next_indent == indent:
            break
        if component_lines or (next_indent == indent and re.match(r'\s*component:', next_line)):
            component_lines.append(next_line)
    return '\n'.join(component_lines)


def is_display_only_setting(properties, component_text):
    if not component_text or re.search(r'^\s*description:', properties, flags=re.M):
        return False
    jsx_tags = set(re.findall(r'</?([A-Za-z][\w.]*)', component_text))
    expressions = re.findall(r'\{([^{}]*)\}', component_text.split('component:', 1)[1])
    has_only_static_text = all(expression.strip() in ("' '", '" "') for expression in expressions)
    return 'Link' in jsx_tags and jsx_tags <= DISPLAY_ONLY_JSX_TAGS and has_only_static_text


def symbol_docs_url(symbol, settings_lines, settings_path):
    import_line = next((line for line in settings_lines if re.match(rf'^import\s*\{{[^}}]*\b{re.escape(symbol)}\b[^}}]*\}}\s*from', line)), '')
    specifier = re.search(r"from\s+['\"]([^'\"]+)['\"]", import_line)
    if not specifier:
        return ''
    frontend_root = settings_path.parents[2]
    module = resolve_frontend_module(frontend_root / specifier.group(1), symbol)
    docs_url = re.search(r"\bdocsUrl:\s*['\"]([^'\"]+)['\"]", module.read_text(encoding='utf-8')) if module else None
    return docs_url.group(1) if docs_url else ''


def resolve_frontend_module(base, symbol, depth=0):
    candidates = [base.with_name(base.name + suffix) for suffix in ('.tsx', '.ts')] + [base / 'index.ts', base / 'index.tsx']
    module = next((candidate for candidate in candidates if candidate.is_file()), None)
    if module is None or depth > 3:
        return module
    text = module.read_text(encoding='utf-8')
    if re.search(rf'^export\s+const\s+{re.escape(symbol)}\b', text, flags=re.M):
        return module
    imported = re.search(rf"^import\s*\{{[^}}]*\b{re.escape(symbol)}\b[^}}]*\}}\s*from\s+['\"](\.[^'\"]+)['\"]", text, flags=re.M)
    return resolve_frontend_module(module.parent / imported.group(1), symbol, depth + 1) if imported else module


def settings_entry_properties(lines, line_number, indent, is_setting):
    property_lines = []
    for previous_line in reversed(lines[:line_number]):
        previous_indent = len(previous_line) - len(previous_line.lstrip())
        if previous_line.strip() and previous_indent < indent:
            break
        property_lines.append(previous_line if previous_indent == indent else '')
    for next_line in lines[line_number:]:
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_line.strip() and next_indent < indent:
            break
        property_lines.append(next_line if next_indent == indent else '')
    return '\n'.join(property_lines)


def first_entry_per_name(entries, kinds, is_gated):
    entries_by_name = {}
    for entry in entries:
        if entry['kind'] in kinds and entry['is_gated'] == is_gated:
            entries_by_name.setdefault(entry['name'], entry)
    return list(entries_by_name.values())


def settings_inventory(mono_root):
    return first_entry_per_name(settings_map_entries(mono_root), ('setting',), False)


def settings_section_inventory(mono_root):
    return first_entry_per_name(settings_map_entries(mono_root), ('section',), False)


def settings_gated_inventory(mono_root):
    return first_entry_per_name(settings_map_entries(mono_root), ('setting', 'section'), True)


def semver(version):
    match = VERSION_PATTERN.search(version)
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def is_publishable_identifier(name, source_tokens):
    leaf = name.split('.')[-1]
    if UNPUBLISHED_IDENTIFIER_PATTERN.search(leaf) or name in COMMON_DOCUMENTED_EVENTS:
        return False
    return source_tokens is None or leaf in source_tokens


def changelog_identifiers(text, known_identifiers=None, source_tokens=None):
    identifiers = set()
    for backticked_text in BACKTICK_PATTERN.findall(text):
        if re.fullmatch(r'\$[A-Za-z_]\w{2,}', backticked_text):
            if is_publishable_identifier(backticked_text, source_tokens):
                identifiers.add(backticked_text)
            continue
        identifier = IDENTIFIER_PATTERN.match(backticked_text)
        if not identifier or len(identifier.group()) <= 3:
            continue
        trailing_text = backticked_text[identifier.end():]
        if trailing_text and not trailing_text.startswith(('(', '=')):
            continue
        name = identifier.group()
        if is_excluded(name.split('.')[-1]):
            continue
        if known_identifiers is not None and name.split('.')[-1] not in known_identifiers and name not in known_identifiers:
            continue
        if not is_publishable_identifier(name, source_tokens):
            continue
        identifiers.add(name)
    return sorted(identifiers)


def ts_module_base(importing_file, specifier, packages_root):
    if specifier.startswith('.'):
        return importing_file.parent / specifier
    if specifier.startswith('@posthog/'):
        return packages_root / specifier.split('/', 1)[1] / 'src' / 'index'
    if specifier == 'posthog-js':
        return packages_root / 'browser' / 'src' / 'index'
    return None


def resolve_ts_module(importing_file, specifier, packages_root):
    base = ts_module_base(importing_file, specifier, packages_root)
    if base is None:
        return None
    candidates = [base.with_name(base.name + suffix) for suffix in ('.ts', '.tsx')] + [base / 'index.ts', base / 'index.tsx']
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def js_entry_export_names(package_root, packages_root):
    entry_files = [path for path in [package_root / 'src/index.ts', *sorted((package_root / 'src/entrypoints').glob('*.ts'))] if path.is_file()]
    names = set()
    pending_files = list(entry_files)
    visited_files = set()
    while pending_files:
        path = pending_files.pop()
        if path in visited_files:
            continue
        visited_files.add(path)
        text = path.read_text(encoding='utf-8', errors='replace')
        names.update(EXPORTED_DECLARATION_PATTERN.findall(text))
        for export_list in EXPORT_LIST_PATTERN.findall(text):
            names.update(re.split(r'\s+as\s+', item.strip())[-1] for item in export_list.split(',') if item.strip())
        names.update(re.findall(r'^    ' + MEMBER_MODIFIERS + r'(?:get\s+)?([A-Za-z_$][\w$]*)\s*[?!]?\s*(?:<[^>]*>\s*)?[(:=]', text, flags=re.M))
        for specifier in EXPORT_FROM_PATTERN.findall(text):
            resolved = resolve_ts_module(path, specifier, packages_root)
            if resolved:
                pending_files.append(resolved)
    return {name.replace('type ', '').strip() for name in names}


def config_names_and_leaves(options):
    return {option['name'] for option in options} | {option['name'].rsplit('.', 1)[-1] for option in options}


def python_entry_export_names(python_root):
    tree = python_tree(Path(python_root) / 'posthog/__init__.py')
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
    return names


def strip_link_markup(text):
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip()


def changelog_entries(path, package, minimum_version, is_python, known_identifiers=None, source_tokens=None):
    entries = []
    version = ''
    heading = ''
    current_entry = []

    def flush_entry():
        if not current_entry or not CHANGELOG_BUMP_HEADING_PATTERN.search(heading):
            return
        raw_text = ' '.join(current_entry)
        commit_hash = ''
        commit_url = ''
        if is_python:
            prefix = re.match(r'^- \[([^]]+)\]\(([^)]+)\)\s*', raw_text)
            entry_text = raw_text[prefix.end():] if prefix else raw_text[2:]
            commit_hash = prefix.group(1) if prefix else ''
            commit_url = prefix.group(2) if prefix else ''
            pull_request = ''
            pull_request_url = ''
        if not is_python:
            prefix = re.match(r'^- \[(#\d+)\]\(([^)]+)\)', raw_text)
            pull_request = prefix.group(1) if prefix else ''
            pull_request_url = prefix.group(2) if prefix else ''
            entry_text = raw_text.split('! - ', 1)[-1] if '! - ' in raw_text else raw_text[2:]
        identifiers = changelog_identifiers(entry_text, known_identifiers, source_tokens)
        if not identifiers or WARNING_ENTRY_PATTERN.search(entry_text):
            return
        if FIX_ENTRY_PATTERN.match(entry_text) or PREVIEW_ENTRY_PATTERN.search(entry_text):
            return
        entries.append({
            'name': f'{package} {version} {pull_request or commit_hash} {",".join(identifiers)}',
            'package': package, 'version': version, 'pr': pull_request,
            'pr_url': pull_request_url, 'commit': commit_hash, 'commit_url': commit_url,
            'identifiers': identifiers,
            'text': strip_link_markup(entry_text), 'origin': str(path),
        })

    for line in path.read_text(encoding='utf-8').splitlines():
        version_match = re.match(r'^##\s+(\d+\.\d+\.\d+(?:[-+][^\s]+)?)', line)
        if version_match:
            flush_entry()
            current_entry = []
            version = version_match.group(1)
            heading = ''
            continue
        if line.startswith('### '):
            flush_entry()
            current_entry = []
            heading = line[4:]
            continue
        if minimum_version and semver(version) < semver(minimum_version):
            continue
        if line.startswith('- '):
            flush_entry()
            current_entry = [line]
            continue
        if current_entry and line.strip():
            current_entry.append(line.strip())
    flush_entry()
    return entries


def js_changelog_inventory(js_root, since_versions):
    shared_identifiers = config_names_and_leaves(js_config_inventory(js_root)) | {method['name'] for method in js_method_inventory(js_root)}
    entries = []
    for path in sorted((js_root / 'packages').glob('*/CHANGELOG.md')):
        package = path.parent.name
        known_identifiers = shared_identifiers | js_entry_export_names(path.parent, js_root / 'packages')
        source_tokens = source_token_set(path.parent / 'src')
        entries.extend(changelog_entries(path, package, since_versions.get(package), False, known_identifiers, source_tokens))
    return merged_changelog_entries(entries)


def merged_changelog_entries(entries):
    entries_by_change = {}
    for entry in sorted(entries, key=lambda entry: (entry['package'], tuple(-part for part in semver(entry['version'])), entry['name'])):
        change_id = entry['pr'] or entry['commit'] or entry['name']
        merged = entries_by_change.setdefault(change_id, {**entry, 'releases': []})
        merged['releases'].append({'package': entry['package'], 'version': entry['version'], 'origin': entry['origin']})
        merged['identifiers'] = sorted(set(merged['identifiers']) | set(entry['identifiers']))
    for merged in entries_by_change.values():
        merged['packages'] = sorted({release['package'] for release in merged['releases']})
        merged['name'] = f"{merged['package']} {merged['version']} {merged['pr'] or merged['commit']} {','.join(merged['identifiers'])}"
    return list(entries_by_change.values())


JS_INTERNAL_PACKAGES = ('core', 'browser-common', 'types')
JS_PUBLIC_FUNCTION_PATTERN = re.compile(r'^export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|class)\s+([A-Za-z_$][\w$]*)', flags=re.M)
JS_CLIENT_CLASS_PATTERN = re.compile(r'^export\s+(?:abstract\s+)?class\s+PostHog\w*[^\n]*\{\n(.*?)^\}', flags=re.M | re.S)
JS_CLASS_METHOD_PATTERN = re.compile(r'^    ' + MEMBER_MODIFIERS + r'(?:get\s+)?([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(', flags=re.M)
REACT_HOOK_PATTERN = re.compile(r'^use[A-Z]')


def is_source_file(source_path):
    return source_path.suffix in {'.ts', '.tsx'} and not re.search(r'(^|/)(__tests__|tests?)/|\.(test|spec)\.', str(source_path))


def js_public_api_names(js_root):
    names = {method['name'] for method in js_method_inventory(js_root)}
    for package_root in sorted(path for path in (Path(js_root) / 'packages').iterdir() if path.is_dir()):
        if package_root.name in JS_INTERNAL_PACKAGES or package_root.name == 'browser':
            continue
        entry_export_names = js_entry_export_names(package_root, Path(js_root) / 'packages')
        for source_path in sorted((package_root / 'src').rglob('*')):
            if not is_source_file(source_path):
                continue
            source_text = source_path.read_text(encoding='utf-8', errors='replace')
            names.update(name for name in JS_PUBLIC_FUNCTION_PATTERN.findall(source_text) if name in entry_export_names)
            for class_body in JS_CLIENT_CLASS_PATTERN.findall(source_text):
                names.update(name for name in JS_CLASS_METHOD_PATTERN.findall(class_body) if not is_excluded(name))
    return names


def python_public_api_names(python_root):
    return python_entry_export_names(python_root) | {method['name'] for method in python_method_inventory(python_root)}


CHANGELOG_GAP_BUCKETS = ('public-api', 'config-option', 'property', 'type-or-internal')
CHANGELOG_GAP_BUCKET_LABELS = {
    'public-api': 'public API (undocumented feature)',
    'config-option': 'config option (counted once, in the config gaps)',
    'property': 'event/person property (reference-only expected)',
    'type-or-internal': 'type name or internal (reference-only expected)',
}


def changelog_identifier_bucket(identifier, config_names, public_api_names):
    leaf = identifier.split('.')[-1]
    if identifier.startswith('$'):
        return 'property'
    if identifier in config_names or leaf in config_names:
        return 'config-option'
    if '.' not in identifier and (identifier in public_api_names or REACT_HOOK_PATTERN.match(identifier)):
        return 'public-api'
    return 'type-or-internal'


def annotate_changelog_gap(entry, config_names, public_api_names):
    if entry['status'] not in ('undocumented', 'partially-documented'):
        return entry
    missing_identifiers = undocumented_identifiers(entry) or entry['identifiers']
    identifier_buckets = {identifier: changelog_identifier_bucket(identifier, config_names, public_api_names) for identifier in missing_identifiers}
    gap_bucket = next(bucket for bucket in CHANGELOG_GAP_BUCKETS if bucket in identifier_buckets.values() or bucket == CHANGELOG_GAP_BUCKETS[-1])
    return {**entry, 'identifier_buckets': identifier_buckets, 'gap_bucket': gap_bucket}


def source_token_set(source_root):
    tokens = set()
    for source_path in sorted(Path(source_root).rglob('*')):
        if source_path.suffix in {'.ts', '.tsx', '.js', '.py'} and not re.search(r'(^|/)(__tests__|tests?)/|\.(test|spec)\.', str(source_path)):
            tokens.update(SOURCE_TOKEN_PATTERN.findall(source_path.read_text(encoding='utf-8', errors='replace')))
    return tokens


def python_changelog_inventory(python_root, since_versions):
    known_identifiers = (
        python_entry_export_names(python_root) | config_names_and_leaves(python_config_inventory(python_root))
        | {method['name'] for method in python_method_inventory(python_root)}
    )
    path = python_root / 'posthog/CHANGELOG.md'
    minimum_version = since_versions.get('python', since_versions.get('posthog-python'))
    return merged_changelog_entries(
        changelog_entries(path, 'posthog-python', minimum_version, True, known_identifiers, source_token_set(python_root / 'posthog'))
    )


def reference_identifiers(value):
    identifiers = set()
    if isinstance(value, list):
        for item in value:
            identifiers.update(reference_identifiers(item))
        return identifiers
    if not isinstance(value, dict):
        return identifiers
    for key in ('id', 'name'):
        name = value.get(key)
        if isinstance(name, str):
            identifiers.add(name)
    for key in ('classes', 'types', 'functions', 'properties', 'params'):
        identifiers.update(reference_identifiers(value.get(key, [])))
    return identifiers


def reference_index(js_root, python_root):
    references = defaultdict(set)
    paths = sorted((js_root / 'packages').glob('*/references/*-references-latest.json'))
    python_paths = [path for path in (python_root / 'references').glob('*.json') if VERSION_PATTERN.search(path.name)]
    if python_paths:
        paths.append(max(python_paths, key=lambda path: (semver(path.name), path.name)))
    for path in paths:
        for identifier in reference_identifiers(json.loads(path.read_text(encoding='utf-8'))):
            references[identifier].add(str(path))
    return references


ONBOARDING_IMPORT_PATTERN = re.compile(r'^import\b\s*(?:type\s+)?([^;\'"]*?)\s*\bfrom\s+[\'"]([^\'"]+)[\'"]', flags=re.M)
TS_MODULE_SUFFIXES = ('.tsx', '.ts')


def imported_names(import_clause):
    named = re.search(r'\{([^}]*)\}', import_clause)
    names = [re.split(r'\s+as\s+', item.strip())[0] for item in named.group(1).split(',') if item.strip()] if named else []
    has_default_import = bool(re.match(r'\s*[A-Za-z_$][\w$]*\s*(?:,|$)', import_clause))
    return [name.removeprefix('type ').strip() for name in names], has_default_import


def resolve_onboarding_module(base):
    candidates = [base] if base.suffix else []
    candidates += [base.with_name(base.name + suffix) for suffix in TS_MODULE_SUFFIXES + ('.mdx', '.md')]
    candidates += [base / f'index{suffix}' for suffix in TS_MODULE_SUFFIXES]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def module_defining_name(index_file, name):
    index_text = index_file.read_text(encoding='utf-8', errors='replace')
    if re.search(rf'^export\s+(?:default\s+)?(?:const|function|class|let)\s+{re.escape(name)}\b', index_text, flags=re.M):
        return index_file
    for export_list, specifier in re.findall(r'^export\s+(?:type\s+)?\{([^}]*)\}\s+from\s+[\'"]([^\'"]+)[\'"]', index_text, flags=re.M):
        exported = {re.split(r'\s+as\s+', item.strip())[-1] for item in export_list.split(',') if item.strip()}
        if name in exported:
            return resolve_onboarding_module(index_file.parent / specifier)
    for specifier in re.findall(r'^export\s+\*\s+from\s+[\'"]([^\'"]+)[\'"]', index_text, flags=re.M):
        reexported_file = resolve_onboarding_module(index_file.parent / specifier)
        if reexported_file and re.search(rf'\bexport\b[^\n]*\b{re.escape(name)}\b', reexported_file.read_text(encoding='utf-8', errors='replace')):
            return reexported_file
    return None


def onboarding_files(specifier, onboarding_root, import_clause=''):
    relative = specifier.split('onboarding', 1)[1].strip('/')
    module = resolve_onboarding_module(onboarding_root / relative)
    if module is None:
        return []
    if module.name.split('.')[0] != 'index':
        return [module]
    names, has_default_import = imported_names(import_clause)
    defining_files = {module_defining_name(module, name) for name in names}
    defining_files.discard(None)
    if has_default_import or not defining_files:
        defining_files.add(module)
    return sorted(defining_files)


def onboarding_text_for_page(page_path, content, onboarding_root):
    return '\n'.join(
        path.read_text(encoding='utf-8', errors='replace') for path in onboarding_files_for_page(page_path, content, onboarding_root)
    )


def onboarding_files_for_page(page_path, content, onboarding_root):
    if onboarding_root is None or not onboarding_root.is_dir():
        return []
    imports = ONBOARDING_IMPORT_PATTERN.findall(content)
    for _, specifier in list(imports):
        local_module = page_path.parent / specifier if specifier.startswith('.') else None
        if local_module and local_module.suffix in {'.tsx', '.ts'} and local_module.is_file():
            imports.extend(ONBOARDING_IMPORT_PATTERN.findall(local_module.read_text(encoding='utf-8', errors='replace')))
    onboarding_paths = {
        path for import_clause, specifier in imports if 'onboarding' in specifier
        for path in onboarding_files(specifier, onboarding_root, import_clause)
    }
    return sorted(onboarding_paths)


def live_docs_routes_without_local_file(docs_root, live_routes):
    return sorted(
        route for route in live_routes
        if route.startswith('/docs') and not site_route_file(docs_root, route)
    )


def site_route_file(docs_root, route):
    relative = route.split('#')[0].split('?')[0].strip('/')
    base = Path(docs_root) / relative
    candidates = [base.parent / f'{base.name}.md', base.parent / f'{base.name}.mdx', base / 'index.md', base / 'index.mdx']
    return next((candidate for candidate in candidates if relative and candidate.is_file()), None)


Corpus = namedtuple('Corpus', 'documents tokens_to_paths importing_pages_by_document structured_documents structured_tokens_to_paths')
STRUCTURED_MARKDOWN_PATTERN = re.compile(
    r'^[ \t]*```[^\n]*\n(?P<code>.*?)^[ \t]*```|^(?P<row>[ \t]*\|[^\n]*)$|^(?P<heading>#{1,6}[ \t][^\n]*)$'
    r'|^[ \t]*(?:[-*+]|\d+[.)])?[ \t]*(?P<leading_code>`[^`\n]+`)',
    flags=re.M | re.S,
)


def structured_text(path, content):
    if Path(path).suffix not in {'.md', '.mdx'}:
        return content
    return '\n'.join(next(group for group in match.groups() if group is not None) for match in STRUCTURED_MARKDOWN_PATTERN.finditer(content))


def corpus_index(docs_root, onboarding_root=None):
    documents = {}
    tokens_to_paths = defaultdict(set)
    importing_pages_by_document = defaultdict(set)
    for path in sorted(docs_root.rglob('*')):
        if path.suffix not in {'.md', '.mdx'}:
            continue
        content = path.read_text(encoding='utf-8', errors='replace')
        for onboarding_path in onboarding_files_for_page(path, content, onboarding_root):
            importing_pages_by_document[str(onboarding_path)].add(str(path))
        documents[str(path)] = content
    for onboarding_path in importing_pages_by_document:
        documents[onboarding_path] = Path(onboarding_path).read_text(encoding='utf-8', errors='replace')
    structured_documents = {path_string: structured_text(path_string, content) for path_string, content in documents.items()}
    structured_tokens_to_paths = defaultdict(set)
    for path_string, content in documents.items():
        for token in set(WORD_PATTERN.findall(content)):
            tokens_to_paths[token].add(path_string)
        for token in set(WORD_PATTERN.findall(structured_documents[path_string])):
            structured_tokens_to_paths[token].add(path_string)
    return Corpus(documents, tokens_to_paths, dict(importing_pages_by_document), structured_documents, structured_tokens_to_paths)


def credited_pages(paths, importing_pages_by_document):
    credited = set()
    for path in paths:
        credited.update(importing_pages_by_document.get(path, {path}))
    return credited


def code_token_pattern(name):
    escaped = re.escape(name)
    return re.compile(rf'`[^`\n]*(?<![\w.$]){escaped}(?![\w$])[^`\n]*`|(?<![\w.$]){escaped}\s*[:=]|["\']{escaped}["\']\s*:')


def inline_code_pattern(name):
    return re.compile(rf'`[^`\n]*(?<![\w.$]){re.escape(name)}(?![\w$])[^`\n]*`')


def object_literal_leaf_pattern(parent, leaf):
    return re.compile(rf'(?<![\w.$]){re.escape(parent)}\s*:\s*\{{[^{{}}]*?(?<![\w.$]){re.escape(leaf)}(?![\w$])')


def markdown_sections(content):
    lines = content.splitlines()
    sections = []
    fenced_line_numbers = set()
    open_sections = [(0, 0, '')]
    is_inside_fence = False
    for line_number, line in enumerate(lines):
        is_fence_marker = bool(re.match(r'^[ \t]*(?:```|~~~)', line))
        if is_inside_fence or is_fence_marker:
            fenced_line_numbers.add(line_number)
        is_inside_fence = is_inside_fence != is_fence_marker
        heading = None if line_number in fenced_line_numbers else re.match(r'^(#{1,6})[ \t]+(.*)$', line)
        if not heading:
            continue
        level = len(heading.group(1))
        while open_sections[-1][0] >= level:
            _, start, heading_text = open_sections.pop()
            sections.append((start, line_number, heading_text))
        open_sections.append((level, line_number, heading.group(2)))
    sections.extend((start, len(lines), heading_text) for _, start, heading_text in open_sections)
    return lines, sections, fenced_line_numbers


def is_structured_leaf_line(line, leaf):
    leaf_token = rf'(?<![\w.$-]){re.escape(leaf)}(?![\w$-])'
    if re.match(r'^[ \t]*(?:\||#{1,6}[ \t])', line):
        return bool(re.search(leaf_token, line))
    return bool(inline_code_pattern(leaf).search(line))


def has_leaf_in_parent_section(content, parent, leaf):
    lines, sections, fenced_line_numbers = markdown_sections(content)
    parent_token = re.compile(rf'(?<![\w.$-]){re.escape(parent)}(?![\w$-])')
    innermost_section_by_line = {}
    for start, end, heading_text in sorted(sections, key=lambda section: section[1] - section[0], reverse=True):
        for line_number in range(start, end):
            innermost_section_by_line[line_number] = (start, heading_text)
    for line_number, line in enumerate(lines):
        if line_number in fenced_line_numbers or not is_structured_leaf_line(line, leaf):
            continue
        section_start, heading_text = innermost_section_by_line[line_number]
        if parent_token.search(heading_text) or any(parent_token.search(earlier) for earlier in lines[section_start:line_number]):
            return True
    return False


def has_object_literal_leaf(content, parent, leaf):
    literal_pattern = object_literal_leaf_pattern(parent, leaf)
    code_spans = [body for _, body in re.findall(r'^[ \t]*(```|~~~)[^\n]*\n(.*?)^[ \t]*\1', content, flags=re.M | re.S)]
    code_spans.extend(re.findall(r'`([^`\n]+)`', content))
    return any(literal_pattern.search(span) for span in code_spans)


@lru_cache(maxsize=None)
def newline_offsets(content):
    return [match.start() for match in re.finditer('\n', content)]


def has_leaf_near_parent(content, leaf_pattern, parent_pattern):
    offsets = newline_offsets(content)
    parent_lines = sorted(bisect.bisect_left(offsets, match.start()) for match in parent_pattern.finditer(content))
    if not parent_lines:
        return False
    for leaf in leaf_pattern.finditer(content):
        leaf_line = bisect.bisect_left(offsets, leaf.start())
        nearest_index = bisect.bisect_left(parent_lines, leaf_line - DOTTED_PARENT_MAX_LINE_DISTANCE)
        if nearest_index < len(parent_lines) and parent_lines[nearest_index] <= leaf_line + DOTTED_PARENT_MAX_LINE_DISTANCE:
            return True
    return False


def candidate_paths(words, tokens_to_paths):
    candidate_sets = [tokens_to_paths.get(word, set()) for word in words]
    return set.intersection(*candidate_sets) if candidate_sets else set()


def mention_paths(name, documents, tokens_to_paths, raw_corpus=None):
    raw_documents, raw_tokens_to_paths = raw_corpus or (documents, tokens_to_paths)
    if IDENTIFIER_PATTERN.fullmatch(name) and '.' not in name:
        return sorted(tokens_to_paths.get(name, set()))
    segments = name.split('.')
    full_path_pattern = re.compile(r'(?<![\w.$])' + re.escape(name) + r'(?![\w$])')
    leaf_pattern = code_token_pattern(segments[-1])
    parent_pattern = code_token_pattern(segments[-2]) if len(segments) > 1 else None
    required_words = WORD_PATTERN.findall('.'.join(segments[-2:]) if parent_pattern else name)
    paths = {
        path for path in candidate_paths(required_words, tokens_to_paths)
        if full_path_pattern.search(documents[path])
        or (parent_pattern and has_leaf_near_parent(documents[path], leaf_pattern, parent_pattern))
    }
    raw_candidates = candidate_paths(required_words, raw_tokens_to_paths)
    if name.startswith('$'):
        paths.update(path for path in raw_candidates if inline_code_pattern(name).search(raw_documents[path]))
    if parent_pattern:
        paths.update(
            path for path in raw_candidates
            if has_leaf_in_parent_section(raw_documents[path], segments[-2], segments[-1])
            or has_object_literal_leaf(raw_documents[path], segments[-2], segments[-1])
        )
    return sorted(paths)


def normalized_setting_title(title):
    return re.sub(r'^Default\s+', '', TITLE_STATUS_SUFFIX.sub('', title.strip()), flags=re.I).strip()


def section_docs_directories(section, docs_root):
    product = re.sub(r'^(?:environment|project|organization|user)-', '', section or '')
    directory = SETTINGS_SECTION_DOCS_DIRECTORIES.get(product, product)
    return [directory] if directory and (Path(docs_root) / 'docs' / directory).is_dir() else ['settings']


def has_product_docs(section, docs_root):
    return section_docs_directories(section, docs_root) != ['settings']


def phrase_pattern(phrase):
    return re.compile(r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in phrase.split()) + r'(?!\w)', flags=re.I)


def has_section_context(content, section_title, section_links):
    if section_title and phrase_pattern(normalized_setting_title(section_title)).search(content):
        return True
    return any(re.search(rf'\bsettings/{re.escape(link)}(?![\w-])', content) for link in section_links)


@lru_cache(maxsize=None)
def setting_heading_sections(content):
    prose = FENCED_CODE_PATTERN.sub('', content)
    headings = list(SETTING_HEADING_SPANS.finditer(prose))
    section_ends = [heading.start() for heading in headings[1:]] + [len(prose)]
    return [(heading.group(0), prose[heading.start():section_end]) for heading, section_end in zip(headings, section_ends)]


def setting_heading_matcher(title, keywords, descriptions, section):
    product_words = set(SETTING_LEVEL_PREFIX.sub('', section or '').split('-'))
    specific_keywords = [
        keyword for keyword in keywords
        if len(keyword) >= MIN_SETTING_TERM_LENGTH and not set(WORD_PATTERN.findall(keyword.lower())) <= product_words
    ]
    normalized_title = normalized_setting_title(title)
    title_words = [word for word in WORD_PATTERN.findall(normalized_title.lower()) if word not in SETTING_TITLE_FILLER_WORDS]
    phrases = [phrase for phrase in (normalized_title, *descriptions) if phrase]
    phrases += [keyword for keyword in specific_keywords if len(WORD_PATTERN.findall(keyword)) > 1]
    phrase_patterns = [phrase_pattern(phrase) for phrase in phrases]
    head_noun_pattern = phrase_pattern(title_words[-1]) if title_words else None
    distinguishing_keyword_patterns = [
        phrase_pattern(keyword) for keyword in specific_keywords if not set(WORD_PATTERN.findall(keyword.lower())) & set(title_words)
    ]

    def is_setting_section(heading, section_text):
        if any(pattern.search(heading) for pattern in phrase_patterns):
            return True
        if head_noun_pattern is None or not head_noun_pattern.search(heading):
            return False
        return any(pattern.search(section_text) for pattern in distinguishing_keyword_patterns)

    return is_setting_section


def setting_term_paths(title, keywords, descriptions, section, section_title, section_links, documents, docs_root):
    is_setting_section = setting_heading_matcher(title, keywords, descriptions, section)
    product_prefixes = tuple(str(Path(docs_root) / 'docs' / directory) + '/' for directory in section_docs_directories(section, docs_root))
    needs_section_context = not has_product_docs(section, docs_root)
    matched_paths = set()
    for path, content in documents.items():
        if not path.startswith(product_prefixes):
            continue
        if not any(is_setting_section(heading, section_text) for heading, section_text in setting_heading_sections(content)):
            continue
        if not needs_section_context or has_section_context(content, section_title, section_links):
            matched_paths.add(path)
    return matched_paths


def section_link_paths(name, title, section, section_links, documents, paths_by_settings_section):
    linking_paths = set().union(*(paths_by_settings_section.get(link, set()) for link in section_links))
    is_section_named_setting = bool(section) and name == SETTING_LEVEL_PREFIX.sub('', section)
    if is_section_named_setting:
        return linking_paths
    normalized_title = normalized_setting_title(title)
    if not normalized_title:
        return set()
    title_pattern = phrase_pattern(normalized_title)
    return {path for path in linking_paths if title_pattern.search(documents.get(path, ''))}


def title_mention_paths(
    title, documents, tokens_to_paths, docs_root=None, section='', importing_pages_by_document=None, section_title='', section_links=(),
):
    importing_pages_by_document = importing_pages_by_document or {}
    title = normalized_setting_title(title)
    title_words = WORD_PATTERN.findall(title)
    if not title_words:
        return []
    product_prefixes = ()
    if len(title_words) <= GENERIC_TITLE_MAX_WORDS and docs_root is not None:
        product_prefixes = tuple(str(Path(docs_root) / 'docs' / directory) + '/' for directory in section_docs_directories(section, docs_root))
        documents = {
            path: content for path, content in documents.items()
            if path.startswith(product_prefixes)
            or any(page.startswith(product_prefixes) for page in importing_pages_by_document.get(path, ()))
        }
    matched_paths = title_matches(title, title_words, documents, tokens_to_paths)
    credited = credited_pages(matched_paths, importing_pages_by_document)
    if product_prefixes and not has_product_docs(section, docs_root):
        credited = {path for path in credited if has_section_context(documents.get(path, ''), section_title, section_links)}
    return sorted(path for path in credited if not product_prefixes or path.startswith(product_prefixes))


def title_matches(title, title_words, documents, tokens_to_paths):
    candidate_sets = [tokens_to_paths.get(word, set()) | tokens_to_paths.get(word.lower(), set()) | tokens_to_paths.get(word.capitalize(), set()) for word in title_words]
    candidates = set.intersection(*candidate_sets)
    title_pattern = re.compile(r'(?<!\w)' + r'\s+'.join(re.escape(word) for word in title.split()) + r'(?!\w)', flags=re.I)
    candidates = {path for path in candidates if path in documents}
    if len(title_words) >= 2:
        return sorted(path for path in candidates if title_pattern.search(documents[path]))
    return sorted(
        path for path in candidates
        if any(
            re.search(r'(?i)\bsettings\b', documents[path][max(match.start() - 200, 0):match.end() + 200])
            for match in title_pattern.finditer(documents[path])
        )
    )


def hit_details(paths, docs_root):
    ordered_paths = sorted(paths, key=lambda path: (Path(path).relative_to(docs_root).parts[:1] != ('docs',), path))
    return [{'path': path, 'is_docs': Path(path).relative_to(docs_root).parts[:1] == ('docs',)} for path in ordered_paths[:3]]


ANCHOR_TOKEN_PATTERN = re.compile(r'#([\w-]+)')


SETTINGS_PATH_PATTERN = re.compile(r'\bsettings/([\w-]+)')


def anchor_and_code_token_index(documents):
    paths_by_anchor = defaultdict(set)
    paths_by_backticked_text = defaultdict(set)
    paths_by_settings_section = defaultdict(set)
    for path, content in documents.items():
        for anchor in set(ANCHOR_TOKEN_PATTERN.findall(content)):
            paths_by_anchor[anchor].add(path)
        for backticked_text in set(BACKTICK_PATTERN.findall(content)):
            paths_by_backticked_text[backticked_text.strip()].add(path)
        for settings_section in set(SETTINGS_PATH_PATTERN.findall(content)):
            paths_by_settings_section[settings_section].add(path)
    return paths_by_anchor, paths_by_backticked_text, paths_by_settings_section


def setting_mention_paths(name, anchor_and_code_tokens):
    paths_by_anchor, paths_by_backticked_text, paths_by_settings_section = anchor_and_code_tokens
    return paths_by_anchor.get(name, set()) | paths_by_backticked_text.get(name, set()) | paths_by_settings_section.get(name, set())


def symbol_status(
    name, documents, tokens_to_paths, references, docs_root, is_setting=False, title='', section='',
    importing_pages_by_document=None, anchor_and_code_tokens=None, structured_corpus=None, setting_context=None,
):
    importing_pages_by_document = importing_pages_by_document or {}
    raw_corpus = (documents, tokens_to_paths)
    if structured_corpus is not None:
        documents, tokens_to_paths = structured_corpus
    if is_setting:
        anchor_and_code_tokens = anchor_and_code_tokens or anchor_and_code_token_index(documents)
        setting_context = setting_context or {}
        section_title = setting_context.get('section_title', '')
        section_links = setting_context.get('section_links', [section] if section else [])
        setting_paths = credited_pages(setting_mention_paths(name, anchor_and_code_tokens), importing_pages_by_document)
        title_paths = title_mention_paths(
            title, documents, tokens_to_paths, docs_root, section, importing_pages_by_document, section_title, section_links,
        ) if title else []
        linked_section_paths = credited_pages(
            section_link_paths(name, title, section, section_links, documents, anchor_and_code_tokens[2]), importing_pages_by_document,
        )
        term_paths = setting_term_paths(
            title, setting_context.get('keywords', []), setting_context.get('descriptions', []), section, section_title, section_links,
            documents, docs_root,
        ) if section else set()
        paths = sorted(setting_paths.union(title_paths, linked_section_paths, term_paths))
    if not is_setting:
        paths = sorted(credited_pages(mention_paths(name, documents, tokens_to_paths, raw_corpus), importing_pages_by_document))
    docs_paths = [path for path in paths if Path(path).relative_to(docs_root).parts[:1] == ('docs',)]
    if docs_paths:
        return 'documented', hit_details(paths, docs_root)
    if paths:
        return 'mentioned-elsewhere', hit_details(paths, docs_root)
    reference_paths = sorted(references.get(name, set()) | references.get(name.rsplit('.', 1)[-1], set()))
    if reference_paths:
        return 'reference-only', [{'path': path, 'is_docs': False} for path in reference_paths[:3]]
    return 'undocumented', []


def classify_entry(entry, documents, tokens_to_paths, references, docs_root, importing_pages_by_document=None, structured_corpus=None):
    identifier_statuses = {}
    hits_by_path = {}
    for identifier in entry['identifiers']:
        status, identifier_paths = symbol_status(
            identifier, documents, tokens_to_paths, references, docs_root,
            importing_pages_by_document=importing_pages_by_document, structured_corpus=structured_corpus,
        )
        identifier_statuses[identifier] = status
        hits_by_path.update({hit['path']: hit for hit in identifier_paths})
    statuses = set(identifier_statuses.values())
    hits = sorted(hits_by_path.values(), key=lambda hit: (not hit['is_docs'], hit['path']))[:3]
    if 'documented' in statuses and statuses <= {'documented', 'reference-only'}:
        return 'documented', hits, identifier_statuses
    if 'undocumented' in statuses and statuses.intersection(('documented', 'mentioned-elsewhere')):
        return 'partially-documented', hits, identifier_statuses
    for status in ('mentioned-elsewhere', 'reference-only', 'undocumented'):
        if status in statuses:
            return status, hits, identifier_statuses
    return 'undocumented', [], identifier_statuses


def inventory(source, js_root, python_root, mono_root, since_versions):
    if source == 'js-config':
        return js_config_inventory(js_root)
    if source == 'js-methods':
        return js_method_inventory(js_root)
    if source == 'python-methods':
        return python_method_inventory(python_root)
    if source == 'python-config':
        return python_config_inventory(python_root)
    if source == 'settings':
        return settings_inventory(mono_root)
    if source == 'settings-sections':
        return settings_section_inventory(mono_root)
    if source == 'settings-gated':
        return settings_gated_inventory(mono_root)
    if source == 'js-changelog':
        return js_changelog_inventory(js_root, since_versions)
    return python_changelog_inventory(python_root, since_versions)


def docs_url_route(docs_url):
    route = re.sub(r'^https?://(?:www\.)?posthog\.com', '', docs_url)
    return route if route.startswith('/') else ''


def is_existing_site_route(route, docs_root, live_routes):
    route = route.split('#')[0].split('?')[0].rstrip('/')
    return bool(route) and (route in live_routes or site_route_file(docs_root, route) is not None)


def is_config_duplicate(entry, config_names):
    return all(identifier in config_names for identifier in entry['identifiers'])


def run_coverage(docs_root, js_root, python_root, mono_root, sources=None, since_versions=None, live_routes=frozenset()):
    docs_root = Path(docs_root)
    js_root = Path(js_root)
    python_root = Path(python_root)
    mono_root = Path(mono_root)
    chosen_sources = [source for source in SOURCE_NAMES if sources is None or source in sources]
    version_limits = since_versions or {}
    documents, tokens_to_paths, importing_pages_by_document, structured_documents, structured_tokens_to_paths = corpus_index(
        docs_root, mono_root / 'docs' / 'onboarding'
    )
    structured_corpus = (structured_documents, structured_tokens_to_paths)
    anchor_and_code_tokens = anchor_and_code_token_index(documents)
    references = reference_index(js_root, python_root)
    config_names_by_changelog = {
        'js-changelog': lambda: config_names_and_leaves(js_config_inventory(js_root)),
        'python-changelog': lambda: config_names_and_leaves(python_config_inventory(python_root)),
    }
    public_api_names_by_changelog = {'js-changelog': lambda: js_public_api_names(js_root), 'python-changelog': lambda: python_public_api_names(python_root)}
    results = {}
    for source in chosen_sources:
        symbols = inventory(source, js_root, python_root, mono_root, version_limits)
        config_names = config_names_by_changelog[source]() if source in config_names_by_changelog else set()
        public_api_names = public_api_names_by_changelog[source]() if source in public_api_names_by_changelog else set()
        classified = []
        for symbol in symbols:
            if source == 'settings-gated':
                classified.append({**symbol, 'status': 'gated', 'hits': []})
                continue
            if source.endswith('changelog') and is_config_duplicate(symbol, config_names):
                classified.append({**symbol, 'status': 'duplicate-of-config', 'hits': [], 'identifier_statuses': {}})
                continue
            if source.endswith('changelog'):
                status, hits, identifier_statuses = classify_entry(
                    symbol, documents, tokens_to_paths, references, docs_root, importing_pages_by_document, structured_corpus
                )
                classified.append(annotate_changelog_gap(
                    {**symbol, 'status': status, 'hits': hits, 'identifier_statuses': identifier_statuses}, config_names, public_api_names,
                ))
                continue
            docs_url = docs_url_route(symbol.get('docs_url', ''))
            if docs_url and is_existing_site_route(docs_url, docs_root, live_routes):
                classified.append({**symbol, 'status': 'documented', 'hits': [{'path': docs_url, 'is_docs': True}]})
                continue
            is_setting = source in ('settings', 'settings-sections')
            status, hits = symbol_status(
                symbol['name'], documents, tokens_to_paths, references, docs_root, is_setting,
                symbol.get('title', ''), symbol.get('section', ''), importing_pages_by_document, anchor_and_code_tokens,
                structured_corpus if source in ('js-config', 'python-config') else None, symbol if is_setting else None,
            )
            classified.append({**symbol, 'status': status, 'hits': hits})
        if source.endswith('changelog'):
            classified.sort(key=lambda item: (item['package'], tuple(-part for part in semver(item['version'])), item['name']))
        if not source.endswith('changelog'):
            classified.sort(key=lambda item: item['name'])
        results[source] = classified
    return results


def undocumented_identifiers(entry):
    return sorted(identifier for identifier, status in entry.get('identifier_statuses', {}).items() if status == 'undocumented')


def format_summary(results):
    lines = [
        'Changelog rule: Major, Minor and Patch headings are all inventoried; one entry per PR (JS) or commit (Python), '
        'listing every package it shipped in; entries without backticked identifiers are omitted. '
        'Only the public-api bucket counts as an undocumented feature.'
    ]
    for source, symbols in results.items():
        if source == 'settings-gated':
            lines.append(f'{source}: {len(symbols)} gated (flag-gated, hidden from navigation, untitled, or display-only; not counted as settings)')
            continue
        duplicate_count = sum(symbol['status'] == 'duplicate-of-config' for symbol in symbols)
        symbols = [symbol for symbol in symbols if symbol['status'] != 'duplicate-of-config']
        counts = {status: sum(symbol['status'] == status for symbol in symbols) for status in ('documented', 'partially-documented', 'mentioned-elsewhere', 'reference-only', 'undocumented')}
        duplicate_note = f' +{duplicate_count} duplicate-of-config' if source.endswith('changelog') else ''
        lines.append(f"{source}: {len(symbols)} / {counts['documented']} / {counts['partially-documented']} / {counts['mentioned-elsewhere']} / {counts['reference-only']} / {counts['undocumented']} (total / documented / partially-documented / mentioned-elsewhere / reference-only / undocumented){duplicate_note}")
        if source.endswith('changelog'):
            gap_counts = {bucket: sum(symbol.get('gap_bucket') == bucket for symbol in symbols) for bucket in CHANGELOG_GAP_BUCKETS}
            lines.append('  gaps by bucket: ' + ', '.join(f'{CHANGELOG_GAP_BUCKET_LABELS[bucket]}={count}' for bucket, count in gap_counts.items()))
        for symbol in symbols:
            if symbol['status'] != 'undocumented':
                continue
            if source.endswith('changelog'):
                identifiers = ','.join(symbol['identifiers'])
                change_identifier = symbol['pr'] or '@' + symbol['commit']
                packages = ','.join(symbol.get('packages', [symbol['package']]))
                if 'gap_bucket' not in symbol:
                    raise ValueError(f"Changelog gap {symbol['name']!r} has no gap_bucket; classify it before formatting the summary")
                lines.append(f"  [{symbol['gap_bucket']}] {packages} {symbol['version']} {change_identifier} {identifiers} | {symbol['text'][:100]}")
                continue
            title = f" ({symbol['title']})" if symbol.get('title') else ''
            lines.append(f"  {symbol['name']}{title}")
        partial_entries = [symbol for symbol in symbols if symbol['status'] == 'partially-documented']
        if partial_entries:
            lines.append('  Partially documented (missing identifiers):')
        for entry in partial_entries:
            missing_identifiers = ', '.join(undocumented_identifiers(entry))
            lines.append(f"    {entry['name']} | missing: {missing_identifiers}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='append', choices=SOURCE_NAMES)
    parser.add_argument('--since-version', action='append', default=[], metavar='PKG=VERSION')
    parser.add_argument('--json', type=Path)
    arguments = parser.parse_args()
    version_limits = {}
    for limit in arguments.since_version:
        if '=' not in limit or not VERSION_PATTERN.match(limit.split('=', 1)[1]):
            parser.error('--since-version requires PKG=VERSION')
        package, version = limit.split('=', 1)
        version_limits[package] = version
    live_routes = frozenset(LIVE_ROUTES_FILE.read_text().split()) if LIVE_ROUTES_FILE.is_file() else frozenset()
    results = run_coverage(
        SITE_ROOT / 'contents', POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT, MONOREPO_ROOT,
        arguments.source, version_limits, live_routes,
    )
    print(format_summary(results))
    unmatched_live_routes = live_docs_routes_without_local_file(SITE_ROOT / 'contents', live_routes)
    print(f'live /docs routes without a local file (existing but unverifiable, not fetched): {len(unmatched_live_routes)}')
    if arguments.json:
        arguments.json.write_text(json.dumps(results, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    run_at_background_priority()
    main()
