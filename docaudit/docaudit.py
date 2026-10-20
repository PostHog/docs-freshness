import ast, bisect, collections, difflib, functools, json, os, re, subprocess, sys
from pathlib import Path, PurePosixPath

from doccoverage import (
    LIVE_ROUTES_FILE, MONOREPO_ROOT, POSTHOG_JS_ROOT, POSTHOG_PYTHON_ROOT, OUTPUT_ROOT, SITE_ROOT, docs_page_url,
    settings_map_entries_from_text, text_fragment_url,
)

SITE = SITE_ROOT
MONOREPO = MONOREPO_ROOT
MONOREPO_HISTORY = MONOREPO_ROOT
JS_PACKAGES = POSTHOG_JS_ROOT / 'packages'
PYTHON_PACKAGE = POSTHOG_PYTHON_ROOT / 'posthog'
JS_PACKAGES_BY_SDK = {
    'js-browser': ('browser', 'browser-common', 'types', 'react'),
    'js-node': ('node', 'core'),
    'js-react-native': ('react-native', 'core'),
}
LANG_TO_SDK = {
    'js': 'js-browser', 'js-web': 'js-browser', 'javascript': 'js-browser', 'react': 'js-browser',
    'typescript': 'js-browser', 'ts': 'js-browser', 'jsx': 'js-browser', 'tsx': 'js-browser',
    'node': 'js-node', 'react-native': 'js-react-native', 'python': 'python',
}
BULK_COMMIT_FILE_THRESHOLD = 40
MAX_REDIRECT_HOPS = 5
LIVE_ROUTES = set(LIVE_ROUTES_FILE.read_text().split())
BACKEND_PATH_PREFIXES = (
    'api', '.well-known', 'complete', 'capture', 'decide', 'flags', 'batch', 'static', 'e', 'i', 's',
    'embedded', 'shared', 'oauth', 'auth.md',
)
THIRD_PARTY_UI_SECTIONS = (
    'cdp/sources/', 'cdp/destinations/', 'libraries/', 'integrate/', 'migrate/', 'ai-observability/integrations/',
    'model-context-protocol/', 'logs/installation/', 'metrics/installation/', 'error-tracking/installation/',
    'advanced/proxy/', 'data-warehouse/sources/', 'integrations/', 'vscode-extension/',
)
UI_ACTION_BOLD_PATTERN = (
    r'\b(?:click|clicking|select|selecting|go to|navigate to|toggle|choose|press|tap|open|enable|disable'
    r'|check|uncheck)\s+(?:on\s+)?(?:the\s+)?\*\*([^*\n.:]{2,40})\*\*'
)
BOLD_STOPWORDS = {'note', 'tip', 'warning', 'important', 'not', 'only', 'and', 'or', 'new', 'example', 'on', 'off'}


def run(args, cwd, input_text=None):
    if args[:1] == ['git']:
        args = ['git', '-c', 'core.quotepath=off', *args[1:]]
    return subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, text=True, check=True).stdout


SKIPPED_SOURCE_PARTS = {'node_modules', 'test', 'tests', '__tests__', 'dist', '__snapshots__'}


def is_skipped_source_path(relative_path):
    path = PurePosixPath(relative_path)
    if any(part in SKIPPED_SOURCE_PARTS or part.startswith('.') for part in path.parts):
        return True
    return bool(re.search(r'\.(test|spec|stories)\.', path.name))


def source_files(root, suffixes):
    for path in root.rglob('*'):
        if path.suffix not in suffixes or not path.is_file():
            continue
        if is_skipped_source_path(path.relative_to(root).as_posix()):
            continue
        yield path


TS_MEMBER_BODY_OPENER = re.compile(
    r'^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:class|interface)\s+\w+'
    r'|^\s*(?:export\s+)?(?:declare\s+)?type\s+\w+(?:<.*>)?\s*=\s*\{'
)
TS_MEMBER_DEFINITION = re.compile(
    r'^\s*((?:(?:public|private|protected|static|readonly|async|override|abstract|declare|get|set)\s+)*)'
    r'\*?([A-Za-z_$][\w$]*)\s*[?!]?\s*(?:<[^>]*>\s*)?[(:=;]'
)
TS_EXPORTED_DEFINITION = re.compile(
    r'^export\s+(?:default\s+)?(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)'
)


def strip_ts_strings_and_comments(text):
    text = re.sub(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"", "''", text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def ts_public_definitions(text):
    definitions = set()
    open_blocks = []
    paren_depth = 0
    is_member_body_pending = False
    for line in strip_ts_strings_and_comments(text).splitlines():
        is_inside_member_body = bool(open_blocks) and open_blocks[-1] == 'members' and paren_depth == 0
        member = TS_MEMBER_DEFINITION.match(line) if is_inside_member_body else None
        if member and not re.search(r'\b(?:private|protected)\b', member.group(1)):
            definitions.add(member.group(2))
        exported = TS_EXPORTED_DEFINITION.match(line) if not open_blocks else None
        if exported:
            definitions.add(exported.group(1))
        if TS_MEMBER_BODY_OPENER.match(line):
            is_member_body_pending = True
        for character in line:
            if character == '(':
                paren_depth += 1
            if character == ')':
                paren_depth = max(paren_depth - 1, 0)
            if character == '{':
                open_blocks.append('members' if is_member_body_pending else 'other')
                is_member_body_pending = False
            if character == '}' and open_blocks:
                open_blocks.pop()
    return definitions


SDK_DEFINITION_FILES = collections.defaultdict(set)


def js_sdk_identifiers(package_names):
    identifiers = set()
    for package_name in package_names:
        for path in source_files(JS_PACKAGES / package_name / 'src', {'.ts', '.tsx'}):
            definitions = ts_public_definitions(path.read_text(errors='ignore'))
            identifiers.update(definitions)
            for identifier in definitions:
                SDK_DEFINITION_FILES[identifier].add(str(path.relative_to(JS_PACKAGES.parent)))
    return identifiers


def python_scope_definitions(statements):
    names = set()
    for node in statements:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        if isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
    return names


def python_self_attributes(class_node):
    return {
        node.attr for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name) and node.value.id == 'self'
    }


PYTHON_CLIENT_ROOT_CLASSES = {('posthog.client', 'Client'), ('posthog.async_client', 'AsyncClient')}


def python_module_name(path):
    parts = ('posthog', *path.relative_to(PYTHON_PACKAGE).with_suffix('').parts)
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def python_imported_module(module_name, is_package, import_node):
    if not import_node.level:
        return import_node.module
    package_parts = module_name.split('.') if is_package else module_name.split('.')[:-1]
    base_parts = package_parts[:len(package_parts) - (import_node.level - 1)]
    return '.'.join([*base_parts, *([import_node.module] if import_node.module else [])])


def python_package_classes():
    classes_by_key = {}
    imported_names_by_module = {}
    for path in source_files(PYTHON_PACKAGE, {'.py'}):
        module_name = python_module_name(path)
        tree = ast.parse(path.read_text(errors='ignore'))
        imported_names_by_module[module_name] = {
            alias.asname or alias.name: (python_imported_module(module_name, path.name == '__init__.py', node), alias.name)
            for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names
        }
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes_by_key[(module_name, node.name)] = node
    return classes_by_key, imported_names_by_module


def python_class_lineage(class_key, classes_by_key, imported_names_by_module):
    lineage = [class_key]
    module_name = class_key[0]
    for base in classes_by_key[class_key].bases:
        if not isinstance(base, ast.Name):
            continue
        local_key = (module_name, base.id)
        base_key = local_key if local_key in classes_by_key else imported_names_by_module[module_name].get(base.id)
        if base_key in classes_by_key:
            lineage.extend(python_class_lineage(base_key, classes_by_key, imported_names_by_module))
    return lineage


def python_client_class_members():
    classes_by_key, imported_names_by_module = python_package_classes()
    members = set()
    for class_key in classes_by_key:
        lineage = python_class_lineage(class_key, classes_by_key, imported_names_by_module)
        if class_key[1].startswith('_') or not PYTHON_CLIENT_ROOT_CLASSES.intersection(lineage):
            continue
        for lineage_key in lineage:
            class_node = classes_by_key[lineage_key]
            members.update(python_scope_definitions(class_node.body) | python_self_attributes(class_node))
    return {member for member in members if not member.startswith('_')}


def python_sdk_identifiers():
    package_module = ast.parse((PYTHON_PACKAGE / '__init__.py').read_text())
    identifiers = python_scope_definitions(package_module.body) | python_client_class_members()
    identifiers.update(
        path.stem for path in PYTHON_PACKAGE.iterdir()
        if (path.is_dir() and (path / '__init__.py').exists()) or path.suffix == '.py'
    )
    return identifiers


def sdk_identifiers():
    identifiers_by_sdk = {sdk: js_sdk_identifiers(packages) for sdk, packages in JS_PACKAGES_BY_SDK.items()}
    all_js_packages = sorted(path.name for path in JS_PACKAGES.iterdir() if (path / 'src').is_dir())
    identifiers_by_sdk['js-any'] = js_sdk_identifiers(all_js_packages)
    identifiers_by_sdk['python'] = python_sdk_identifiers()
    return identifiers_by_sdk


GENERIC_JS_SDK_BY_PAGE_SEGMENT = {'node': 'js-node', 'nodejs': 'js-node', 'react-native': 'js-react-native'}
SDK_CALL_NOISE_BY_SDK_FAMILY = {
    'python': re.compile(r'(?P<noise>"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|\'(?:\\.|[^\'\\\n])*\'|"(?:\\.|[^"\\\n])*"|#[^\n]*)'),
    'js': re.compile(
        r'(?P<noise>\'(?:\\.|[^\'\\\n])*\'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*[\s\S]*?\*/)'
    ),
}


BROWSER_JS_FENCE_LANGUAGES = ('js', 'js-web', 'react')
SDK_PINNED_FENCE_LANGUAGES = ('node', 'react-native', 'js-web', 'react')
TYPED_CLIENT_MARKER = re.compile(r'posthog[-_]typed|PosthogTyped\(')
NAMED_INSTANCE_INIT = re.compile(r"""posthog\.init\((?:[^()]|\([^()]*\))*?,\s*['"]([A-Za-z_$][\w$]*)['"]\s*\)""")


def sdks_for_fence(language, body, page_path, page_import_sdks):
    sdks = fence_sdks(language, page_path)
    if not sdks or sdks[0] == 'python':
        return sdks
    own_import_sdks = fence_import_sdks(body)
    is_pinned = language.lower() in SDK_PINNED_FENCE_LANGUAGES
    imported_sdks = own_import_sdks or ([] if is_pinned else page_import_sdks)
    return [*sdks, *(sdk for sdk in dict.fromkeys(imported_sdks) if sdk not in sdks)]


def label_appears_in_code(label, code_text):
    return bool(re.search(rf'(?<!\w){re.escape(label.casefold())}(?!\w)', code_text))


def fence_sdks(language, page_path):
    sdk = LANG_TO_SDK.get(language.lower())
    if sdk != 'js-browser':
        return [sdk] if sdk else []
    page_segments = re.sub(r'\.mdx?$', '', page_path).split('/')
    path_sdks = [GENERIC_JS_SDK_BY_PAGE_SEGMENT[segment] for segment in page_segments if segment in GENERIC_JS_SDK_BY_PAGE_SEGMENT]
    if language.lower() in BROWSER_JS_FENCE_LANGUAGES:
        return ['js-browser', *path_sdks[-1:]]
    return path_sdks[-1:] or ['js-any']


FENCE_IMPORTED_PACKAGE = re.compile(r"""(?:\bfrom\s+|\brequire\(\s*|\bimport\s+)['"]([^'"]+)['"]""")


def fence_import_sdks(body):
    sdks = []
    for package in FENCE_IMPORTED_PACKAGE.findall(body):
        package_root = package.split('/')[0] if not package.startswith('@') else '/'.join(package.split('/')[:2])
        if package_root == 'posthog-node':
            sdks.append('js-node')
        if package_root == 'posthog-react-native':
            sdks.append('js-react-native')
        if package_root == 'posthog-js':
            sdks.append('js-browser')
        if package_root.startswith('@posthog/'):
            sdks.append('js-any')
    return sdks


def page_named_instances(fenced_bodies):
    return {name for body in fenced_bodies for name in NAMED_INSTANCE_INIT.findall(body)}


def sdk_method_calls(body, sdk):
    if TYPED_CLIENT_MARKER.search(body):
        return []
    noise_pattern = SDK_CALL_NOISE_BY_SDK_FAMILY['python' if sdk == 'python' else 'js']
    code = noise_pattern.sub("''", body)
    code = re.sub(r'^[ \t]*(?:from|import)\b[^\n]*', '', code, flags=re.M)
    return sorted(set(re.findall(r'\bposthog\.(?!com\b)([A-Za-z_]\w*)', code)))


UiLabelIndex = collections.namedtuple(
    'UiLabelIndex',
    'files_by_unit templates_by_word units_by_loose_key units_by_shape tooltip_only_units visible_text_by_tooltip desktop_only_units'
    ' heading_only_units display_text_by_unit units_by_dropped_word',
)
LabelExtraction = collections.namedtuple(
    'LabelExtraction',
    'units templates tooltip_units visible_units visible_text_by_tooltip heading_units non_heading_visible_units',
    defaults=((), (), {}, (), ()),
)
VISIBLE_LABEL_PROPS = {'label', 'title', 'buttonCopy', 'children', 'placeholder'}
HEADING_LABEL_PROPS = {'title'}
HEADING_ELEMENTS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
OPENING_ELEMENT_BEFORE = re.compile(r'<([A-Za-z][\w.]*)[^<]*$')
PRODUCT_AREA_LABEL_SOURCES = {
    'session-replay': ('frontend/src/scenes/session-recordings/', 'products/replay/'),
    'product-analytics': ('frontend/src/scenes/insights/', 'frontend/src/scenes/saved-insights/'),
}
SHARED_LABEL_SOURCE_PREFIXES = ('frontend/src/lib/', 'frontend/src/layout/', 'frontend/src/queries/')
MIN_DROPPED_WORD_UNIT_WORDS = 3
MAX_DROPPED_WORD_UNIT_WORDS = 8
STRING_PROP_BEFORE = re.compile(r'([A-Za-z_$][\w$]*)\s*(?:=\s*\{?\s*|:\s*)$')
TOOLTIP_ELEMENT_TEXT = re.compile(r'[^<>]*?(?<!=)>\s*([^<>{}\n][^<>{}]*?)\s*<')
EXCLUDED_LABEL_SOURCE = re.compile(
    r'/migrations/|generated|api\.schemas\.ts$|/prompts/[^/]*\.(?:py|json|md|txt)$|prompt[^/]*\.py$'
)
DESKTOP_LABEL_SOURCE = 'products/desktop/'
DESKTOP_DOCS_PREFIX = 'posthog-desktop/'
MIN_NEAR_MISS_UNIT_LENGTH = 3
MIN_EDIT_DISTANCE_LABEL_LENGTH = 5
SHORT_LABEL_LENGTH = 8
NON_UI_STRING_SPANS = re.compile(
    r'\bkeywords:\s*\[[^\]]*\]'
    r'|\bposthog\.capture\(\s*(?:\'(?:\\.|[^\'\\\n])*\'|"(?:\\.|[^"\\\n])*"|`[^`]*`)'
    r'|\blemonToast\.\w+\(\s*(?:\'(?:\\.|[^\'\\\n])*\'|"(?:\\.|[^"\\\n])*"|`[^`]*`)'
)
CDP_TEMPLATE_NAME = re.compile(r"^\s*name:\s*['\"]([^'\"]+)['\"]", flags=re.M)
LOOSE_LABEL_DROPPED_WORDS = {'a', 'an', 'the'}
MAX_NEAR_MISS_EDIT_DISTANCE = 2


def loose_label_key(normalized_label):
    words = re.findall(r"[a-z0-9']+", normalized_label.replace('-', ' '))
    return ' '.join(word for word in words if word not in LOOSE_LABEL_DROPPED_WORDS)


def is_within_edit_distance(first, second, max_distance):
    if abs(len(first) - len(second)) > max_distance:
        return False
    previous_row = list(range(len(second) + 1))
    for first_index, first_character in enumerate(first, start=1):
        current_row = [first_index]
        for second_index, second_character in enumerate(second, start=1):
            current_row.append(min(
                previous_row[second_index] + 1, current_row[second_index - 1] + 1,
                previous_row[second_index - 1] + (first_character != second_character),
            ))
        if min(current_row) > max_distance:
            return False
        previous_row = current_row
    return previous_row[-1] <= max_distance


def near_miss_candidates(needle, label_index, allows_word_insertion):
    loose_match = label_index.units_by_loose_key.get(loose_label_key(needle))
    if loose_match:
        yield loose_match
    if len(needle) >= MIN_EDIT_DISTANCE_LABEL_LENGTH:
        word_count = len(needle.split())
        max_distance = 1 if len(needle) < SHORT_LABEL_LENGTH else MAX_NEAR_MISS_EDIT_DISTANCE
        for length in range(max(len(needle) - max_distance, MIN_NEAR_MISS_UNIT_LENGTH), len(needle) + max_distance + 1):
            for unit in label_index.units_by_shape.get((word_count, length), ()):
                if is_within_edit_distance(needle, unit, max_distance):
                    yield unit
    if allows_word_insertion:
        yield from label_index.units_by_dropped_word.get(needle, ())


@functools.lru_cache(maxsize=None)
def product_area_label_sources(docs_section):
    if not docs_section:
        return ()
    conventional_prefixes = (f'frontend/src/scenes/{docs_section}/', f'products/{docs_section.replace("-", "_")}/')
    prefixes = (*PRODUCT_AREA_LABEL_SOURCES.get(docs_section, ()), *conventional_prefixes)
    return tuple(prefix for prefix in prefixes if (MONOREPO / prefix).is_dir())


def near_miss_ui_label(label, label_index, excluded_units=frozenset(), area_prefixes=()):
    needle = normalize_label_text(label)
    unusable_unit_sets = (excluded_units, label_index.tooltip_only_units, label_index.heading_only_units)
    candidates = [
        unit for unit in near_miss_candidates(needle, label_index, bool(area_prefixes))
        if not any(unit in unusable_units for unusable_units in unusable_unit_sets)
    ]
    if not area_prefixes:
        return next(iter(candidates), None)
    for source_prefixes in (area_prefixes, SHARED_LABEL_SOURCE_PREFIXES):
        area_match = next(
            (unit for unit in candidates if any(file.startswith(source_prefixes) for file in label_index.files_by_unit[unit])), None
        )
        if area_match:
            return area_match
    return None
LabelTemplate = collections.namedtuple('LabelTemplate', 'pattern required_words file')
MAX_LABEL_UNIT_LENGTH = 80
HTML_ENTITY_REPLACEMENTS = {'&nbsp;': ' ', ' ': ' ', '&amp;': '&', '&apos;': "'", '&#39;': "'", '&quot;': '"'}
QUOTED_STRING = re.compile(r"'((?:\\.|[^'\\\n])*)'|\"((?:\\.|[^\"\\\n])*)\"")
TS_UI_TOKEN = re.compile(r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`|//[^\n]*|/\*[\s\S]*?\*/|(?<![=\-])>""")
BALANCED_BRACES = r'\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}'
JSX_CHILDREN_RUN = re.compile(rf'>((?:[^<>{{}}]|{BALANCED_BRACES})*)<(?=[/a-zA-Z>])')
JSX_EXPRESSION = re.compile(BALANCED_BRACES)
PYTHON_STRING = re.compile(r"""\b([rbuf]{0,2})('''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")""")
JSON_STRING = re.compile(r'"((?:\\.|[^"\\])*)"')


def display_label_text(text):
    for entity, replacement in HTML_ENTITY_REPLACEMENTS.items():
        text = text.replace(entity, replacement)
    text = text.replace('…', '').replace('...', '')
    text = re.sub(r'\\[nt]', ' ', text)
    text = re.sub(r'\\(.)', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip().rstrip('?:').strip()


def normalize_label_text(text):
    return display_label_text(text.lower())


def literal_regex(text):
    normalized = text.lower()
    for entity, replacement in HTML_ENTITY_REPLACEMENTS.items():
        normalized = normalized.replace(entity, replacement)
    normalized = re.sub(r'\\(.)', r'\1', normalized.replace('…', '').replace('...', ''))
    return ''.join(r'\s*' if piece.isspace() else re.escape(piece) for piece in re.split(r'(\s+)', normalized) if piece)


def template_literal_parts(text, start):
    parts = []
    literal = []
    position = start
    while position < len(text):
        if text[position] == '\\':
            literal.append(text[position:position + 2])
            position += 2
            continue
        if text[position] == '`':
            parts.append(('text', ''.join(literal)))
            return parts, position + 1
        if not text.startswith('${', position):
            literal.append(text[position])
            position += 1
            continue
        parts.append(('text', ''.join(literal)))
        literal = []
        depth = 1
        expression_start = position + 2
        position += 2
        while position < len(text) and depth:
            depth += {'{': 1, '}': -1}.get(text[position], 0)
            position += 1
        parts.append(('expression', text[expression_start:position - 1]))
    parts.append(('text', ''.join(literal)))
    return parts, position


TemplateSource = collections.namedtuple('TemplateSource', 'file text property_values')
TS_PROPERTY_STRING = re.compile(r"""(?<![\w$])([A-Za-z_$][\w$]*)\s*:\s*(?:'((?:\\.|[^'\\\n])*)'|"((?:\\.|[^"\\\n])*)")""")
MAX_ENUMERATED_VALUES = 200
NUMERIC_BUILTIN_PROPERTIES = {'length', 'size'}


def enumerated_expression_values(expression, source):
    expression = expression.strip()
    member = re.fullmatch(r'[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*|\[[^\]]*\])*\??\.([A-Za-z_$][\w$]*)', expression)
    if member and member.group(1) in NUMERIC_BUILTIN_PROPERTIES:
        return []
    if member:
        values = source.property_values.get(member.group(1), ())
        return sorted(values) if len(values) <= MAX_ENUMERATED_VALUES else []
    if not re.fullmatch(r'[A-Za-z_$][\w$]*', expression):
        return []
    declaration = re.search(rf'\b(?:const|let|var)\s+{re.escape(expression)}\b[^=\n]*=([^\n;]*)', source.text)
    return [first or second for first, second in QUOTED_STRING.findall(declaration.group(1))] if declaration else []


def expression_regex(expression, source):
    alternatives = []
    position = 0
    while True:
        literal = re.compile(r"""'((?:\\.|[^'\\\n])*)'|"((?:\\.|[^"\\\n])*)"|`""").search(expression, position)
        if not literal:
            break
        if literal.group(0) == '`':
            parts, position = template_literal_parts(expression, literal.end())
            alternatives.append(parts_regex(parts, source)[0])
            continue
        alternatives.append(literal_regex(literal.group(1) if literal.group(1) is not None else literal.group(2)))
        position = literal.end()
    alternatives = alternatives or [literal_regex(value) for value in enumerated_expression_values(expression, source)]
    if not alternatives:
        return '(.*?)', set()
    alternative_words = {word for alternative in alternatives for word in re.findall(r'[a-z0-9]{2,}', alternative)}
    return '(?:' + '|'.join(alternatives) + ')', alternative_words


def parts_regex(parts, source):
    pieces = []
    fixed_words = set()
    alternative_words = set()
    for kind, content in parts:
        if kind == 'text':
            pieces.append(literal_regex(content))
            fixed_words.update(re.findall(r'[a-z0-9]{2,}', normalize_label_text(content)))
            continue
        expression_pattern, expression_words = expression_regex(content, source)
        pieces.append(expression_pattern)
        alternative_words.update(expression_words)
    return ''.join(pieces), fixed_words, alternative_words


def label_template(parts, source):
    if not any(kind == 'expression' for kind, _ in parts):
        return None
    pattern, fixed_words, alternative_words = parts_regex(parts, source)
    if not (fixed_words or alternative_words) or len(pattern) > 600:
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    return LabelTemplate(compiled, frozenset(fixed_words), source.file), fixed_words or alternative_words


def jsx_run_parts(run):
    parts = []
    position = 0
    for expression in JSX_EXPRESSION.finditer(run):
        parts.append(('text', run[position:expression.start()]))
        parts.append(('expression', expression.group(0)[1:-1]))
        position = expression.end()
    parts.append(('text', run[position:]))
    return parts


def is_jsx_text(text):
    return not re.search(r'[;=]|&&|\|\||\)\s*\{', re.sub(r'&#?\w+;', ' ', text))


def string_prop_name(text, string_start):
    prop = STRING_PROP_BEFORE.search(text[max(string_start - 40, 0):string_start])
    return prop.group(1) if prop else ''


def ts_label_units_and_templates(source):
    text = NON_UI_STRING_SPANS.sub(' ', source.text)
    units = set()
    templates = []
    tooltip_units = set()
    visible_units = set()
    visible_text_by_tooltip = {}
    heading_units = set()
    non_heading_visible_units = set()
    position = 0
    while True:
        token = TS_UI_TOKEN.search(text, position)
        if not token:
            return LabelExtraction(
                units, templates, tooltip_units, visible_units, visible_text_by_tooltip, heading_units, non_heading_visible_units
            )
        position = token.end()
        lexeme = token.group(0)
        if lexeme[0] in '\'"':
            units.add(lexeme[1:-1])
            prop = string_prop_name(text, token.start())
            if prop in VISIBLE_LABEL_PROPS:
                visible_units.add(lexeme[1:-1])
                (heading_units if prop in HEADING_LABEL_PROPS else non_heading_visible_units).add(lexeme[1:-1])
            if prop == 'tooltip':
                tooltip_units.add(lexeme[1:-1])
                element_text = TOOLTIP_ELEMENT_TEXT.match(text, token.end())
                visible_text_by_tooltip.setdefault(lexeme[1:-1], element_text.group(1).strip() if element_text else '')
            continue
        if lexeme == '`':
            parts, position = template_literal_parts(text, token.end())
            units.update(content for kind, content in parts if kind == 'text' and len(parts) == 1)
            templates.append(label_template(parts, source))
            continue
        if lexeme != '>':
            continue
        run = JSX_CHILDREN_RUN.match(text, token.start())
        if not run:
            continue
        parts = jsx_run_parts(run.group(1))
        if not all(is_jsx_text(content) for kind, content in parts if kind == 'text'):
            continue
        units.update(content for kind, content in parts if kind == 'text')
        visible_units.update(content for kind, content in parts if kind == 'text')
        element = OPENING_ELEMENT_BEFORE.search(text, max(token.start() - MAX_LABEL_UNIT_LENGTH * 4, 0), token.start())
        is_heading_text = bool(element) and element.group(1) in HEADING_ELEMENTS
        (heading_units if is_heading_text else non_heading_visible_units).update(content for kind, content in parts if kind == 'text')
        templates.append(label_template(parts, source))


def python_label_units_and_templates(source):
    units = set()
    templates = []
    for prefix, literal in PYTHON_STRING.findall(source.text):
        quote_length = 3 if literal[:3] in ("'''", '"""') else 1
        content = literal[quote_length:-quote_length]
        if 'f' not in prefix or '{' not in content:
            units.add(content)
            continue
        parts = []
        position = 0
        for expression in re.finditer(r'\{([^{}]*)\}', content):
            parts.extend([('text', content[position:expression.start()]), ('expression', expression.group(1))])
            position = expression.end()
        parts.append(('text', content[position:]))
        templates.append(label_template(parts, source))
    return LabelExtraction(units, templates)


def json_label_units(source):
    return LabelExtraction(set(JSON_STRING.findall(source.text)), [])


def label_source_extractors():
    return (
        ('frontend/src', {'.tsx', '.ts'}, ts_label_units_and_templates),
        ('products', {'.tsx', '.ts'}, ts_label_units_and_templates),
        ('products', {'.py'}, python_label_units_and_templates),
        ('products', {'.json'}, json_label_units),
        ('nodejs/src/cdp/templates', {'.ts'}, cdp_template_name_units),
    )


def label_extractor(file):
    if EXCLUDED_LABEL_SOURCE.search(file) or is_skipped_source_path(file):
        return None
    suffix = PurePosixPath(file).suffix
    return next((extract for root, suffixes, extract in label_source_extractors() if suffix in suffixes and file.startswith(root + '/')), None)


def label_property_values(monorepo):
    property_values = collections.defaultdict(set)
    for root, suffixes, _ in label_source_extractors():
        for path in source_files(monorepo / root, suffixes) if suffixes & {'.ts', '.tsx'} else ():
            for property_name, single_quoted, double_quoted in TS_PROPERTY_STRING.findall(path.read_text(errors='ignore')):
                property_values[property_name].add(single_quoted or double_quoted)
    return property_values


def monorepo_label_sources(monorepo, property_values):
    for root, suffixes, _ in label_source_extractors():
        for path in source_files(monorepo / root, suffixes):
            file = path.relative_to(monorepo).as_posix()
            if label_extractor(file) is None:
                continue
            yield TemplateSource(file, path.read_text(errors='ignore'), property_values)


LABEL_EXTRACTIONS_BY_PROPERTY_VALUES = {'property_values': None, 'extractions': {}}


def ui_label_index():
    property_values = label_property_values(MONOREPO)
    if property_values != LABEL_EXTRACTIONS_BY_PROPERTY_VALUES['property_values']:
        LABEL_EXTRACTIONS_BY_PROPERTY_VALUES.update(property_values=property_values, extractions={})
    return label_index_from_sources(monorepo_label_sources(MONOREPO, property_values), LABEL_EXTRACTIONS_BY_PROPERTY_VALUES['extractions'])


def label_lookup_in_sources(label, sources, is_desktop_page):
    return desktop_scoped_lookup(search_label(label, label_index_from_sources(sources)), is_desktop_page)


def label_index_from_sources(sources, extractions_by_text=None):
    files_by_unit = collections.defaultdict(set)
    templates_by_word = collections.defaultdict(list)
    tooltip_units = set()
    visible_units = set()
    visible_text_by_tooltip = {}
    heading_units = set()
    non_heading_visible_units = set()
    display_text_by_unit = {}
    for source in sources:
        extract = label_extractor(source.file)
        if extract is None:
            continue
        file = source.file
        extraction = extracted_labels(extract, source, extractions_by_text)
        units, templates = extraction.units, extraction.templates
        tooltip_units.update(normalize_label_text(unit) for unit in extraction.tooltip_units)
        visible_units.update(normalize_label_text(unit) for unit in extraction.visible_units)
        heading_units.update(normalize_label_text(unit) for unit in extraction.heading_units)
        non_heading_visible_units.update(normalize_label_text(unit) for unit in extraction.non_heading_visible_units)
        for tooltip, element_text in extraction.visible_text_by_tooltip.items():
            visible_text_by_tooltip.setdefault(normalize_label_text(tooltip), element_text)
        for unit in units:
            normalized_unit = normalize_label_text(unit) if len(unit) <= MAX_LABEL_UNIT_LENGTH * 2 else ''
            if normalized_unit and len(normalized_unit) <= MAX_LABEL_UNIT_LENGTH:
                files_by_unit[normalized_unit].add(file)
                display_text_by_unit.setdefault(normalized_unit, display_label_text(unit))
        for template, index_words in filter(None, templates):
            for word in index_words:
                templates_by_word[word].append(template)
    units_by_loose_key = {}
    units_by_shape = collections.defaultdict(list)
    units_by_dropped_word = collections.defaultdict(list)
    for unit in files_by_unit:
        if re.search(r'[_/.]', unit):
            continue
        units_by_loose_key.setdefault(loose_label_key(unit), unit)
        units_by_shape[(len(unit.split()), len(unit))].append(unit)
        words = unit.split()
        if MIN_DROPPED_WORD_UNIT_WORDS <= len(words) <= MAX_DROPPED_WORD_UNIT_WORDS:
            for dropped_index in range(len(words)):
                units_by_dropped_word[' '.join(words[:dropped_index] + words[dropped_index + 1:])].append(unit)
    desktop_only_units = frozenset(
        unit for unit, files in files_by_unit.items() if all(file.startswith(DESKTOP_LABEL_SOURCE) for file in files)
    )
    return UiLabelIndex(
        dict(files_by_unit), dict(templates_by_word), units_by_loose_key, dict(units_by_shape),
        tooltip_units - visible_units, visible_text_by_tooltip, desktop_only_units,
        frozenset(heading_units - non_heading_visible_units), display_text_by_unit, dict(units_by_dropped_word),
    )


def extracted_labels(extract, source, extractions_by_text):
    if extractions_by_text is None:
        return extract(source)
    key = (source.file, source.text)
    if key not in extractions_by_text:
        extractions_by_text[key] = extract(source)
    return extractions_by_text[key]


def cdp_template_name_units(source):
    if not source.file.endswith('.template.ts'):
        return LabelExtraction(set(), [])
    return LabelExtraction(set(CDP_TEMPLATE_NAME.findall(source.text)), [])


LABEL_LOOKUP_CACHE = {}


def files_containing_label(label, label_index):
    if label not in LABEL_LOOKUP_CACHE:
        LABEL_LOOKUP_CACHE[label] = search_label(label, label_index)
    return LABEL_LOOKUP_CACHE[label]


def is_mostly_literal_match(match, needle):
    if not match:
        return False
    wildcard_word_counts = [len(re.findall(r'[a-z0-9]+', capture)) for capture in match.groups() if capture]
    wildcard_word_count = sum(wildcard_word_counts)
    literal_word_count = len(re.findall(r'[a-z0-9]+', needle)) - wildcard_word_count
    if any(count > 1 for count in wildcard_word_counts):
        return False
    return literal_word_count >= (3 if wildcard_word_count else 2)


def desktop_scoped_lookup(lookup, is_desktop_page):
    status, files = lookup
    if files and not is_desktop_page and all(file.startswith(DESKTOP_LABEL_SOURCE) for file in files):
        return 'missing', []
    return lookup


def search_label(label, label_index):
    needle = normalize_label_text(label)
    exact_files = label_index.files_by_unit.get(needle)
    if exact_files and needle in label_index.tooltip_only_units:
        return 'tooltip-only', sorted(exact_files)
    if exact_files:
        return 'exact', sorted(exact_files)
    label_words = set(re.findall(r'[a-z0-9]{2,}', needle))
    candidates = {template for word in label_words for template in label_index.templates_by_word.get(word, ())}
    template_files = sorted({
        template.file for template in candidates
        if template.required_words <= label_words and is_mostly_literal_match(template.pattern.fullmatch(needle), needle)
    })
    if template_files:
        return 'template', template_files
    return 'missing', []


ROUTE_PARAM_MARKER = '\0'


def template_literal_at(text, start):
    route = []
    interpolation_depth = 0
    position = start
    while position < len(text):
        character = text[position]
        if interpolation_depth == 0 and character == '`':
            return ''.join(route), position + 1
        if interpolation_depth == 0 and text.startswith('${', position):
            interpolation_depth = 1
            route.append(ROUTE_PARAM_MARKER)
            position += 2
            continue
        if interpolation_depth > 0 and character == '{':
            interpolation_depth += 1
        if interpolation_depth > 0 and character == '}':
            interpolation_depth -= 1
        if interpolation_depth == 0 and character != '}':
            route.append(character)
        position += 1
    return ''.join(route), position


def route_literals(text):
    consumed_until = 0
    for match in re.finditer(r"'(/[^'\n]*)'|\"(/[^\"\n]*)\"|`(?=/)", text):
        if match.start() < consumed_until:
            continue
        if match.group(0) != '`':
            yield match.group(1) or match.group(2), match.end()
            continue
        route, consumed_until = template_literal_at(text, match.end())
        yield route, consumed_until


def route_segment_pattern(segment):
    if segment.startswith((':', '*')):
        return re.compile(r'[^/]*')
    literal_parts = [re.escape(part) for part in segment.split(ROUTE_PARAM_MARKER)]
    return re.compile('[^/]*'.join(literal_parts))


def scene_redirect_sources():
    text = (MONOREPO / 'frontend/src/scenes/scenes.ts').read_text(errors='ignore')
    declaration = text.find('export const redirects')
    if declaration < 0:
        return []
    body_start = text.find('= {', declaration) + 2
    sources = []
    depth = 0
    for line in text[body_start:].splitlines():
        key = re.match(r'\s*[\'"](/[^\'"]*)[\'"]\s*:', line)
        if depth == 1 and key:
            sources.append(key.group(1))
        depth += line.count('{') - line.count('}')
        if depth <= 0:
            return sources
    return sources


def frontend_route_sources():
    route_files = [MONOREPO / 'frontend/src/scenes/urls.ts', *MONOREPO.glob('products/*/manifest.tsx')]
    scenes_path = 'frontend/src/scenes/scenes.ts'
    route_candidates = [(route, False, scenes_path) for route in scene_redirect_sources()]
    for path in route_files:
        source = path.read_text(errors='ignore')
        for route, end in route_literals(source):
            route_candidates.append((route, bool(re.match(r'\s*\+', source[end:])), str(path.relative_to(MONOREPO))))
    sources_by_shape = collections.defaultdict(set)
    for route, is_concatenated, source_path in route_candidates:
        path_only = re.split(r'[?#]', route)[0]
        has_open_suffix = path_only.endswith('*') or is_concatenated
        segments = tuple(segment for segment in path_only.rstrip('*').split('/') if segment)
        if segments and re.match(r'[\w-]', segments[0]):
            sources_by_shape[(segments, has_open_suffix)].add(source_path)
    return sources_by_shape


def backing_sources(repo, paths):
    return [{'repo': repo, 'path': str(path)} for path in sorted(paths)]


def sdk_backing_sources(sdk, identifier):
    if sdk == 'python':
        return backing_sources('posthog-python', ['posthog'])
    packages = JS_PACKAGES_BY_SDK.get(sdk, tuple(path.name for path in JS_PACKAGES.iterdir() if (path / 'src').is_dir()))
    definitions = [path for path in SDK_DEFINITION_FILES[identifier] if any(path.startswith(f'packages/{package}/src/') for package in packages)]
    return backing_sources('posthog-js', definitions or [f'packages/{package}/src' for package in packages])


def app_route_backing_sources(segments, context):
    matched_shapes = [
        (shape, paths) for shape, paths in context.get('route_sources', {}).items()
        if route_matches_pattern(segments, [route_segment_pattern(segment) for segment in shape[0]], shape[1])
    ]
    patterns = ['/' + '/'.join(shape[0]).replace(ROUTE_PARAM_MARKER, ':parameter') + ('*' if shape[1] else '') for shape, _ in matched_shapes]
    paths = set().union(*(paths for _, paths in matched_shapes))
    if segments[:1] == ['settings']:
        paths.update(('frontend/src/scenes/settings/SettingsMap.tsx', 'frontend/src/scenes/settings/types.ts', 'frontend/src/scenes/settings/settingsSceneLogic.ts'))
    return backing_sources('monorepo', paths), patterns


def docs_backing_sources(route, fragment=''):
    page_file = doc_content_file(route)
    if page_file:
        paths = {page_file.relative_to(SITE)}
        if fragment:
            evidence_by_anchor = collections.defaultdict(set)
            page_anchor_index(page_file.read_text(errors='ignore'), page_file, evidence_by_anchor=evidence_by_anchor)
            paths.update(evidence_by_anchor.get(fragment, set()))
        return backing_sources('site', paths)
    route_base = SITE / 'src/pages' / route.strip('/')
    candidates = [route_base.with_suffix('.tsx'), route_base / 'index.tsx']
    page_file = next((candidate for candidate in candidates if candidate.is_file()), None)
    if page_file:
        return backing_sources('site', [page_file.relative_to(SITE)])
    return [*backing_sources('site', ['vercel.json']), {'repo': 'docaudit', 'path': 'data/live-routes.txt'}]


def is_doc_placeholder(segment):
    return bool(re.fullmatch(r'<[^>]+>|\{[^}]+\}|:\w+', segment))


def route_matches_pattern(doc_segments, segment_patterns, has_open_suffix):
    if len(doc_segments) < len(segment_patterns):
        return False
    if len(doc_segments) > len(segment_patterns) and not has_open_suffix:
        return False
    return all(
        is_doc_placeholder(doc_segment) or segment_pattern.fullmatch(doc_segment)
        for doc_segment, segment_pattern in zip(doc_segments, segment_patterns)
    )


def settings_section_ids():
    settings_directory = MONOREPO / 'frontend/src/scenes/settings'
    types_text = (settings_directory / 'types.ts').read_text(errors='ignore')
    section_union = re.search(r'export type SettingSectionId =(.*?)\nexport ', types_text, flags=re.S)
    level_ids = re.search(r'export const SettingLevelIds = \[([^\]]*)\]', types_text)
    section_ids = set(re.findall(r"'([\w-]+)'", section_union.group(1))) if section_union else set()
    section_ids.update(re.findall(r"'([\w-]+)'", level_ids.group(1)) if level_ids else ())
    scene_logic_text = (settings_directory / 'settingsSceneLogic.ts').read_text(errors='ignore')
    aliases = re.search(r'SETTINGS_SECTION_ALIASES[^=]*=\s*\{(.*?)\n\}', scene_logic_text, flags=re.S)
    section_ids.update(re.findall(r"^\s*'([\w-]+)'\s*:", aliases.group(1), flags=re.M) if aliases else ())
    environment_sections = {
        section for section in section_ids
        if section.startswith('environment-') and not section.endswith(('-details', '-danger-zone'))
    }
    section_ids.update('project' + section.removeprefix('environment') for section in environment_sections)
    return section_ids


SETTINGS_LEVEL_IDS = ('environment', 'project', 'organization', 'user')
SETTINGS_MAP_PATH = 'frontend/src/scenes/settings/SettingsMap.tsx'
SETTINGS_SCENE_LOGIC_PATH = 'frontend/src/scenes/settings/settingsSceneLogic.ts'
SettingsAnchorScope = collections.namedtuple(
    'SettingsAnchorScope', 'ids_by_section first_section_by_level section_aliases moved_setting_ids all_ids'
)


def equivalent_settings_sections(section):
    equivalents = {section}
    if section.startswith('environment-'):
        equivalents.add('project-' + section.removeprefix('environment-'))
    if section.startswith('project-'):
        equivalents.add('environment-' + section.removeprefix('project-'))
    if section in ('environment', 'project'):
        equivalents.update(('environment', 'project'))
    return equivalents


def settings_anchor_scope():
    settings_map_file = MONOREPO / SETTINGS_MAP_PATH
    scene_logic_file = MONOREPO / SETTINGS_SCENE_LOGIC_PATH
    return settings_anchor_scope_from_texts(
        settings_map_file.read_text(encoding='utf-8'), scene_logic_file.read_text(errors='ignore'), settings_map_file,
    )


def settings_anchor_scope_from_texts(settings_map_text, scene_logic_text, settings_map_file):
    ids_by_section = collections.defaultdict(set)
    first_section_by_level = {}
    for entry in settings_map_entries_from_text(settings_map_text, settings_map_file):
        ids_by_section[entry['section']].add(entry['name'])
        effective_level = 'project' if entry['level'] == 'environment' else entry['level']
        first_section_by_level.setdefault(effective_level, entry['section'])
    constants = dict(re.findall(r"^const (\w+)(?::[^=]+)?=\s*'([\w-]+)'", scene_logic_text, flags=re.M))
    aliases = re.search(r'SETTINGS_SECTION_ALIASES[^=]*=\s*\{(.*?)\n\}', scene_logic_text, flags=re.S)
    alias_pairs = re.findall(r"^\s*'([\w-]+)'\s*:\s*'?([\w-]+)'?", aliases.group(1), flags=re.M) if aliases else []
    moved = re.search(r'MOVED_SETTINGS[^=]*=\s*\{(.*?)\n\}', scene_logic_text, flags=re.S)
    moved_setting_ids = set(re.findall(r"^\s*'([\w-]+)'\s*:", moved.group(1), flags=re.M)) if moved else set()
    return SettingsAnchorScope(
        dict(ids_by_section), first_section_by_level,
        {alias: constants.get(target, target) for alias, target in alias_pairs},
        moved_setting_ids, set().union(*ids_by_section.values()),
    )


def canonical_settings_section(section, section_aliases):
    if section in section_aliases:
        return section_aliases[section]
    if section.startswith('environment') and not section.endswith(('-details', '-danger-zone')):
        return 'project' + section.removeprefix('environment')
    return section


def linked_section_anchor_ids(settings_path, scope):
    linked_section = settings_path.split('/')[0]
    section = scope.section_aliases.get(linked_section, linked_section)
    if section in SETTINGS_LEVEL_IDS:
        return set()
    return set().union(*(scope.ids_by_section.get(candidate, set()) for candidate in equivalent_settings_sections(section)))


def settings_anchor_ids_in_scope(settings_path, scope):
    linked_section = settings_path.split('/')[0]
    section = scope.section_aliases.get(linked_section, linked_section)
    if section in SETTINGS_LEVEL_IDS:
        section = scope.first_section_by_level.get('project' if section == 'environment' else section, section)
    sections = equivalent_settings_sections(section)
    in_scope = set().union(*(scope.ids_by_section.get(candidate, set()) for candidate in sections))
    return (in_scope or scope.all_ids) | scope.moved_setting_ids


def app_route_exists(doc_segments, route_patterns):
    return any(route_matches_pattern(doc_segments, patterns, open_suffix) for patterns, open_suffix in route_patterns)


Redirect = collections.namedtuple('Redirect', 'pattern destination wildcard_names')
ResolvedRedirect = collections.namedtuple('ResolvedRedirect', 'destination discards_wildcard_capture')
VERCEL_GROUP = r'\((?:\\.|[^()\\]|\([^()]*\))*\)'
VERCEL_SOURCE_TOKEN = re.compile(
    rf'(?P<prefix>/?):(?P<name>\w+)(?P<custom>{VERCEL_GROUP})?(?P<modifier>[*?+])?'
    rf'|(?P<group>{VERCEL_GROUP})(?P<group_modifier>[*?+])?'
)
VERCEL_DESTINATION_PARAM = re.compile(r':(\w+)[*?+]?')


def vercel_parameter_regex(token):
    prefix = re.escape(token['prefix'])
    modifier = token['modifier'] or ''
    default_body = '.+' if modifier in ('*', '+') else '[^/]+'
    body = token['custom'][1:-1] if token['custom'] else default_body
    capture = f"{prefix}(?P<{token['name']}>{body})"
    if modifier in ('?', '*'):
        return f'(?:{capture})?'
    return capture


def vercel_pattern_to_regex(source):
    pieces = []
    wildcard_names = []
    position = 0
    for token in VERCEL_SOURCE_TOKEN.finditer(source):
        pieces.append(re.escape(source[position:token.start()]))
        position = token.end()
        if token['group']:
            pieces.append(f"(?:{token['group'][1:-1]}){token['group_modifier'] or ''}")
            continue
        if token['modifier'] in ('*', '+'):
            wildcard_names.append(token['name'])
        pieces.append(vercel_parameter_regex(token))
    pieces.append(re.escape(source[position:]))
    return re.compile(f"^{''.join(pieces)}$"), tuple(wildcard_names)


def site_redirects():
    compiled = []
    for redirect in json.loads((SITE / 'vercel.json').read_text())['redirects']:
        try:
            pattern, wildcard_names = vercel_pattern_to_regex(redirect['source'])
        except re.error:
            continue
        compiled.append(Redirect(pattern, redirect['destination'], wildcard_names))
    return compiled


def substitute_destination_params(destination, captured_values):
    def substitute(param):
        if param.group(1) not in captured_values:
            return param.group(0)
        return captured_values[param.group(1)] or ''
    return VERCEL_DESTINATION_PARAM.sub(substitute, destination)


def apply_redirect(route, redirects):
    for redirect in redirects:
        match = redirect.pattern.match(route)
        if not match:
            continue
        captured_values = match.groupdict()
        discards_wildcard_capture = any(
            captured_values.get(name) and not re.search(rf':{name}(?!\w)', redirect.destination)
            for name in redirect.wildcard_names
        )
        return ResolvedRedirect(substitute_destination_params(redirect.destination, captured_values), discards_wildcard_capture)
    return None


def doc_content_file(route):
    relative = route.removesuffix('.md').strip('/')
    if not relative:
        return None
    base = SITE / 'contents' / relative
    candidates = [base.parent / f'{base.name}.md', base.parent / f'{base.name}.mdx', base / 'index.md', base / 'index.mdx']
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def site_route_exists(route):
    return route in LIVE_ROUTES or route.removesuffix('.md') in LIVE_ROUTES or doc_content_file(route) is not None


def resolve_doc_route(link, redirects):
    route = link
    fragment = ''
    for _ in range(MAX_REDIRECT_HOPS):
        route = re.sub(r'^https?://(?:www\.)?posthog\.com(?=[/#?]|$)', '', route)
        if re.match(r'^[a-z]+://', route):
            return 'ok', route, ''
        route, _, route_fragment = route.partition('#')
        fragment = route_fragment or fragment
        route = route.split('?')[0].rstrip('/') or '/'
        if site_route_exists(route):
            return 'ok', route, fragment
        redirected = apply_redirect(route, redirects)
        if not redirected:
            return 'missing', route, fragment
        if redirected.discards_wildcard_capture:
            return 'fallback', redirected.destination, fragment
        route = redirected.destination
    return 'redirect-loop', route, fragment


SNIPPET_IMPORT = re.compile(r'^import\s[^\n]*?\bfrom\s+["\']([^"\']+)["\']', flags=re.M)
MARKDOWN_SUFFIXES = ('.md', '.mdx')
CODE_SUFFIXES = ('.tsx', '.ts', '.jsx', '.js')
PAGE_ANCHOR_CACHE = {}


def resolve_content_import(importing_file, specifier):
    unnormalized_base = importing_file.parent / specifier if specifier.startswith('.') else SITE / 'contents' / 'docs' / specifier
    bases = [Path(os.path.normpath(unnormalized_base))]
    for base in bases:
        candidates = [base, *(base.parent / f'{base.name}{suffix}' for suffix in MARKDOWN_SUFFIXES + CODE_SUFFIXES)]
        resolved = next((candidate for candidate in candidates if candidate.is_file()), None)
        if resolved:
            return resolved
    return None


def slugify_heading(heading_text):
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', heading_text)
    text = re.sub(r'<[^>]*>', '', text)
    return re.sub(r'[^\w\- ]', '', text.strip().lower()).replace(' ', '-')


def own_anchor_ids(page_text):
    anchor_ids = set(re.findall(r'\bid=["\']([^"\']+)["\']', page_text))
    anchor_ids.update(re.findall(r'\burl:\s*["\']([^"\'/\s:]+)["\']', page_text))
    anchor_ids.update(slugify_heading(title) for title in re.findall(r'<Step\b[^>]*?\btitle=["\']([^"\']+)["\']', page_text))
    anchor_ids.update(
        'quest-item-' + slugify_heading(title)
        for title in re.findall(r'<QuestLogItem\b[^>]*?\btitle=["\']([^"\']+)["\']', page_text)
    )
    slug_counts = collections.Counter()
    for heading in re.findall(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', strip_code(page_text), flags=re.M):
        slug = slugify_heading(heading)
        anchor_ids.add(f'{slug}-{slug_counts[slug]}' if slug_counts[slug] else slug)
        slug_counts[slug] += 1
    return anchor_ids


def page_anchor_index(page_text, page_file, visited_files=(), evidence_by_anchor=None):
    anchor_ids = own_anchor_ids(page_text)
    if evidence_by_anchor is not None and page_file:
        for anchor in anchor_ids:
            evidence_by_anchor[anchor].add(page_file.relative_to(SITE))
    has_code_generated_anchors = False
    for specifier in SNIPPET_IMPORT.findall(page_text) if page_file else ():
        imported_file = resolve_content_import(page_file, specifier)
        if imported_file is None or imported_file in visited_files:
            continue
        if imported_file.suffix in CODE_SUFFIXES:
            has_code_generated_anchors = True
            continue
        imported_ids, imported_has_code = page_anchor_index(
            imported_file.read_text(errors='ignore'), imported_file, (*visited_files, page_file), evidence_by_anchor
        )
        anchor_ids |= imported_ids
        has_code_generated_anchors = has_code_generated_anchors or imported_has_code
    return anchor_ids, has_code_generated_anchors


PAGE_ANCHOR_INDEX_CACHE = {}


def cached_page_anchor_index(page_text, page_file):
    if page_file is None:
        return page_anchor_index(page_text, page_file)
    cache_key = (Path(os.path.normpath(page_file)), page_text)
    if cache_key not in PAGE_ANCHOR_INDEX_CACHE:
        PAGE_ANCHOR_INDEX_CACHE[cache_key] = page_anchor_index(page_text, page_file)
    return PAGE_ANCHOR_INDEX_CACHE[cache_key]


TAB_PANEL_OPEN = re.compile(r'<Tab\.Panel\b(?![^>]*/>)')
TAB_PANEL_CLOSE = re.compile(r'</Tab\.Panel\s*>')
NUMBERED_SLUG_SUFFIX = re.compile(r'^(.+)-\d+$')


def is_inside_tab_panel(text, offset):
    return len(TAB_PANEL_OPEN.findall(text, 0, offset)) > len(TAB_PANEL_CLOSE.findall(text, 0, offset))


def rendered_heading_slugs(page_text, page_file, is_inside_tab=False, visited_files=()):
    prose = strip_code(page_text)
    slugs = [
        (slugify_heading(heading.group(1)), is_inside_tab or is_inside_tab_panel(prose, heading.start()))
        for heading in re.finditer(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', prose, flags=re.M)
    ]
    if page_file is None:
        return slugs
    for binding, specifier in IMPORT_BINDING.findall(page_text):
        imported_file = resolve_content_import(page_file, specifier)
        if imported_file is None or imported_file in visited_files or imported_file.suffix not in MARKDOWN_SUFFIXES:
            continue
        imported_text = imported_file.read_text(errors='ignore')
        for usage in re.finditer(rf'<{binding}\b', prose):
            slugs.extend(rendered_heading_slugs(
                imported_text, imported_file, is_inside_tab or is_inside_tab_panel(prose, usage.start()), (*visited_files, page_file),
            ))
    return slugs


def ambiguous_heading_slugs(page_text, page_file):
    if page_file is None:
        return uncached_ambiguous_heading_slugs(page_text, None)
    return cached_ambiguous_heading_slugs(Path(os.path.normpath(page_file)), page_text)


@functools.lru_cache(maxsize=None)
def cached_ambiguous_heading_slugs(page_file, page_text):
    return uncached_ambiguous_heading_slugs(page_text, page_file)


def uncached_ambiguous_heading_slugs(page_text, page_file):
    occurrences_by_slug = collections.defaultdict(list)
    for slug, is_in_tab in rendered_heading_slugs(page_text, page_file):
        occurrences_by_slug[slug].append(is_in_tab)
    return frozenset(slug for slug, tab_flags in occurrences_by_slug.items() if len(tab_flags) > 1 or all(tab_flags))


def is_ambiguous_anchor(anchor, ambiguous_slugs):
    numbered = NUMBERED_SLUG_SUFFIX.match(anchor)
    return anchor in ambiguous_slugs or bool(numbered and numbered.group(1) in ambiguous_slugs)


def fragment_status_in_page(page_text, page_file, fragment):
    anchor_ids, has_code_generated_anchors = cached_page_anchor_index(page_text, page_file)
    if fragment in anchor_ids:
        return 'ok'
    return 'unverifiable' if has_code_generated_anchors else 'missing'


@functools.lru_cache(maxsize=None)
def importers_by_content_file():
    importers = collections.defaultdict(list)
    for page in sorted((SITE / 'contents' / 'docs').rglob('*.md*')):
        for specifier in SNIPPET_IMPORT.findall(page.read_text(errors='ignore')):
            imported_file = resolve_content_import(page, specifier)
            if imported_file:
                importers[imported_file].append(page)
    return dict(importers)


def snippet_host_pages(snippet_file, visited_files=()):
    hosts = []
    for importer in importers_by_content_file().get(snippet_file, []):
        if importer in visited_files:
            continue
        if '_snippets' in importer.parts:
            hosts.extend(snippet_host_pages(importer, (*visited_files, snippet_file)))
            continue
        hosts.append(importer)
    return sorted(set(hosts))


def snippet_fragment_status(snippet_file, fragment):
    hosts = snippet_host_pages(snippet_file)
    if not hosts:
        return 'unverifiable', []
    statuses = {host: fragment_status_in_page(host.read_text(errors='ignore'), host, fragment) for host in hosts}
    hosts_lacking_fragment = [str(host.relative_to(SITE / 'contents' / 'docs')) for host, status in statuses.items() if status == 'missing']
    return ('missing' if hosts_lacking_fragment else 'ok'), hosts_lacking_fragment


def docs_fragment_status(route, fragment):
    content_file = doc_content_file(route)
    if content_file is None or not content_file.is_relative_to(SITE / 'contents' / 'docs'):
        return 'unverifiable'
    cache_key = (content_file, fragment)
    if cache_key not in PAGE_ANCHOR_CACHE:
        PAGE_ANCHOR_CACHE[cache_key] = fragment_status_in_page(content_file.read_text(errors='ignore'), content_file, fragment)
    return PAGE_ANCHOR_CACHE[cache_key]


CODE_FENCE_OPENER = re.compile(r'^[ \t]*(`{3,}|~{3,})[ \t]*([^\n]*)$')


def code_fences(text):
    lines = text.splitlines(keepends=True)
    fences = []
    line_index = 0
    while line_index < len(lines):
        opener = CODE_FENCE_OPENER.match(lines[line_index].rstrip('\n'))
        if not opener or (opener[1][0] == '`' and '`' in opener[2]):
            line_index += 1
            continue
        closer = re.compile(rf'^[ \t]*{re.escape(opener[1][0])}{{{len(opener[1])},}}[ \t]*$')
        body_start = line_index + 1
        body_end = next((i for i in range(body_start, len(lines)) if closer.match(lines[i].rstrip('\n'))), len(lines))
        language = re.match(r'[A-Za-z-]*', opener[2])[0]
        fences.append((language, ''.join(lines[body_start:body_end]), line_index, min(body_end + 1, len(lines))))
        line_index = body_end + 1
    return fences, lines


def strip_code(text):
    fences, lines = code_fences(text)
    fenced_line_indexes = {i for _, _, start, end in fences for i in range(start, end)}
    return ''.join('\n' if i in fenced_line_indexes else line for i, line in enumerate(lines))


def fenced_blocks(text):
    return [(language, body) for language, body, _, _ in code_fences(text)[0] if language]


def commit_file_counts(commit_ids):
    if not commit_ids:
        return {}
    log = run(['git', 'log', '--no-walk', '--stdin', '--format=@%H', '--name-only', '--no-renames'], SITE,
              input_text='\n'.join(commit_ids) + '\n')
    file_counts = collections.Counter()
    current_commit = None
    for line in log.splitlines():
        if line.startswith('@'):
            current_commit = line[1:]
            file_counts[current_commit] += 0
            continue
        if line.strip():
            file_counts[current_commit] += 1
    return file_counts


def last_real_edit_timestamps(pathspec):
    log = run(['git', 'log', '--format=@%ct %H', '--name-only', '--no-renames', '--', pathspec], SITE)
    commits = []
    for line in log.splitlines():
        if line.startswith('@'):
            timestamp, commit_id = line[1:].split()
            commits.append((int(timestamp), commit_id, []))
            continue
        if line.strip():
            commits[-1][2].append(line.strip())
    total_files_by_commit = commit_file_counts([commit_id for _, commit_id, _ in commits])
    timestamps = {}
    for timestamp, commit_id, section_files in commits:
        if total_files_by_commit[commit_id] > BULK_COMMIT_FILE_THRESHOLD:
            continue
        for file in section_files:
            timestamps.setdefault(file, timestamp)
    return timestamps


def evidence_commit_history(files):
    if not files:
        return []
    log = run(['git', 'log', '--format=@%H %ct', '--name-only', '--no-renames', '--', *sorted(files)], MONOREPO_HISTORY)
    commits = []
    for line in log.splitlines():
        if line.startswith('@'):
            _, timestamp = line[1:].split()
            commits.append((int(timestamp), set()))
            continue
        if line.strip():
            commits[-1][1].add(line.strip())
    return commits


def commits_since(files, since_timestamp, commit_history):
    return sum(
        1 for timestamp, changed_files in commit_history
        if changed_files & files and (since_timestamp is None or timestamp >= since_timestamp)
    )


class Suggestion(str):
    def __new__(cls, text='', confidence='none'):
        suggestion = super().__new__(cls, text)
        suggestion.confidence = confidence if text else 'none'
        return suggestion


NO_SUGGESTION = Suggestion()
SUGGESTION_ARTICLES = {'a', 'an', 'the'}
MIN_CANDIDATE_JACCARD = 0.5
MIN_EXACT_AFFIX_BASE_TOKENS = 2
MIN_FUZZY_TOKEN_LENGTH = 5
MIN_SHARED_LINK_TOKEN_LENGTH = 4
MIN_MOVED_PAGE_LINK_SEGMENTS = 3


class PageAudit:
    def __init__(self):
        self.checked_by_kind = collections.Counter()
        self.findings = []
        self.failed_claims = []
        self.evidence_files = set()
        self.references = []

    def check(self, kind, passed, detail, suggestion=NO_SUGGESTION, claim=None, sources=(), **attributes):
        self.checked_by_kind[kind] += 1
        if not passed:
            self.findings.append((kind, detail, suggestion))
            self.failed_claims.append(claim or detail)
            return
        self.references.append({'kind': kind, 'claim': claim or detail, 'sources': list(sources), **attributes})


DOCS_LINK_TARGET = r'((?:https?://(?:www\.)?posthog\.com)?/docs(?![\w.-])[^\s)"\'`>]*)'
DOCS_LINK_PATTERNS = (
    r'\]\(\s*<?' + DOCS_LINK_TARGET,
    r'(?m)^[ \t]*\[[^\]\n]+\]:[ \t]*<?' + DOCS_LINK_TARGET,
    r'\bhref=\{?\s*["\'`]' + DOCS_LINK_TARGET,
)


APP_HOST_URL = r'https?://(?:us|eu|app)\.posthog\.com/([^\s)"\'>#?`\]]*)'
APP_URL_LINK_PATTERNS = (
    r'\]\(\s*<?' + APP_HOST_URL,
    r'(?m)^[ \t]*\[[^\]\n]+\]:[ \t]*<?' + APP_HOST_URL,
    r'\b(?:href|src|to)=\{?\s*["\'`]' + APP_HOST_URL,
    r'<' + APP_HOST_URL,
    r'(?:^|(?<=[\s*_~(]))' + APP_HOST_URL,
)
VENDOR_NAME_ALTERNATION = (
    r'(?:Okta|OneLogin|Databricks|Slack|Customer\.io|Splunk|Google Cloud|Google|VS Code|Visual Studio Code|AWS|Azure'
    r'|Cursor|Claude|Zapier|Stripe|HubSpot|Salesforce|BigQuery|Snowflake|GitHub|Vercel|Cloudflare'
    r'|IAM|Retool|Mailgun|SendGrid|Sendgrid|GitLab)'
)
VENDOR_NAMES = re.compile(rf'\b{VENDOR_NAME_ALTERNATION}\b')
VENDOR_UI_CONTEXT = re.compile(
    rf'\b(?:in|from|open|opening|to|navigate to|go to|log in to|sign in to)\s+(?:your\s+|the\s+)?{VENDOR_NAME_ALTERNATION}\b'
    rf'|\b{VENDOR_NAME_ALTERNATION}\s+(?:admin|console|dashboard|workspace|portal|account|app|UI|settings|project|interface)\b'
    r'|https?://(?:app|console|portal|dashboard|admin)\.(?!posthog\.)[\w-]+\.',
    flags=re.I,
)
POSTHOG_INSTRUCTION_CUE = re.compile(
    r'\b(?:go back to|return to|back in|switch (?:back )?to|log in to|navigate to|head to|go to|open|in)\s+(?:your\s+)?PostHog\b',
    flags=re.I,
)
VENDOR_CONTEXT_MAX_LINES = 10
JSX_MARKUP_LINE = re.compile(r'\s*(?:</?[A-Za-z]|/?>|[\w-]+=)')
WIDENED_UI_LABEL_CLAIM_PATTERNS = (
    re.compile(r'\b[Ii]n the \*\*([^*\n]{2,40})\*\*\s+(?:section|field|tab|dropdown|menu)\b'),
    re.compile(r'\bthe \*\*([^*\n]{2,40})\*\*\s+(?:button|tab|field|section)\b'),
)
ACTION_VERB_BEFORE_CLAIM = re.compile(r'(?<![\w-])(?:click|select|tap|toggle|choose|open|press|go to|navigate)\b', flags=re.I)
UI_LABEL_CLAIM_PATTERNS = (
    re.compile(UI_ACTION_BOLD_PATTERN, flags=re.I),
    *WIDENED_UI_LABEL_CLAIM_PATTERNS,
    re.compile(
        r"\b(?:click|select|choose|toggle)(?:\s+on)?(?:\s+the)?\s+"
        r"(?:'([^'\n]{2,40})'(?![A-Za-z])|\"([^\"\n]{2,40})\"|‘([^’\n]{2,40})’(?![A-Za-z])|“([^”\n]{2,40})”)",
        flags=re.I,
    ),
)
UI_LABEL_TRAILING_KIND = re.compile(r'\s+(?:button|tab|dropdown|icon|link|menu|toggle|section|field)$', flags=re.I)
ICON_WORDS = {'pencil', 'emoji', '⋯', '⋮'}


def inline_code_removed(prose):
    return re.sub(r'(`+)[^`\n]*?\1', '', prose)


def app_url_paths(prose):
    linkable_prose = inline_code_removed(prose)
    return sorted({path for pattern in APP_URL_LINK_PATTERNS for path in re.findall(pattern, linkable_prose)})


def heading_index(prose):
    headings = list(re.finditer(r'^#{1,6}\s+(.*)$', prose, flags=re.M))
    return [heading.start() for heading in headings], [heading.group(1) for heading in headings]


def claim_context(prose, position, headings_by_offset):
    line_start = prose.rfind('\n', 0, position) + 1
    line_end = prose.find('\n', position)
    line = prose[line_start:line_end if line_end >= 0 else len(prose)]
    heading_offsets, heading_texts = headings_by_offset
    headings = heading_texts[:bisect.bisect_left(heading_offsets, line_start)]
    list_item = line if re.match(r'\s*(?:[-*+]|\d+[.)])\s', line) else ''
    offset = position - line_start
    sentence_start = max((match.end() for match in re.finditer(r'[.!?]\s+', line[:offset])), default=0)
    sentence_end = next((match.start() + 1 for match in re.finditer(r'[.!?](?:\s|$)', line[offset:])), len(line) - offset)
    return line[sentence_start:offset + sentence_end], list_item, headings[-1] if headings else ''


def vendor_context_window(prose, position):
    line_start = prose.rfind('\n', 0, position) + 1
    line_end = prose.find('\n', position)
    claim_line = prose[line_start:line_end if line_end >= 0 else len(prose)]
    window = [claim_line]
    prose_line_count = 0
    for line in reversed(prose[:line_start].split('\n')[:-1]):
        if re.match(r'#{1,2}\s', line) or POSTHOG_INSTRUCTION_CUE.search(line) or prose_line_count >= VENDOR_CONTEXT_MAX_LINES:
            break
        window.append(line)
        prose_line_count += bool(line.strip()) and not JSX_MARKUP_LINE.match(line)
    return window


def is_third_party_ui_claim(prose, position, page_path, headings_by_offset):
    sentence, list_item, heading = claim_context(prose, position, headings_by_offset)
    window = vendor_context_window(prose, position)
    claim_line_prefix = prose[prose.rfind('\n', 0, position) + 1:position]
    if POSTHOG_INSTRUCTION_CUE.search(claim_line_prefix):
        return False
    if VENDOR_NAMES.search(window[0]) or any(VENDOR_UI_CONTEXT.search(line) for line in window[1:]):
        return True
    if 'posthog' in sentence.lower():
        return False
    if page_path.startswith('settings/authentication/'):
        return True
    return any(VENDOR_NAMES.search(context) for context in (sentence, list_item, heading))


WRAPPING_QUOTE_PAIRS = {'"': '"', "'": "'", '“': '”', '‘': '’'}


def cleaned_claim_label(label):
    label = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', label)
    label = re.sub(r'^[^\w"\'“‘]+', '', label.strip())
    label = label.replace('&nbsp;', ' ').replace('…', '').replace('...', '').strip().rstrip('?:.,').strip()
    is_wrapped_in_quotes = len(label) > 2 and WRAPPING_QUOTE_PAIRS.get(label[0]) == label[-1]
    return label[1:-1].strip() if is_wrapped_in_quotes else label


def claim_label_variants(raw_label):
    with_kind_word = cleaned_claim_label(raw_label)
    return cleaned_claim_label(UI_LABEL_TRAILING_KIND.sub('', with_kind_word)), with_kind_word


def is_checkable_ui_label(label):
    if len(label) < 2 or label.lower() in BOLD_STOPWORDS or label.lower() in ICON_WORDS:
        return False
    return label != label.lower()


def ui_label_claim_occurrences(prose, page_path):
    prose = re.sub(r'\[([^\]\n]+)\]\([^)\n]*\)', r'\1', prose)
    headings_by_offset = heading_index(prose)
    for pattern in UI_LABEL_CLAIM_PATTERNS:
        for claim in pattern.finditer(prose):
            if is_third_party_ui_claim(prose, claim.start(), page_path, headings_by_offset):
                continue
            is_widened_claim = pattern in WIDENED_UI_LABEL_CLAIM_PATTERNS
            if is_widened_claim and not ACTION_VERB_BEFORE_CLAIM.search(prose[max(claim.start() - 40, 0):claim.start()]):
                continue
            raw_label = next(group for group in claim.groups() if group is not None)
            line_number = prose.count('\n', 0, claim.start()) + 1
            for segment in re.split(r'→|>', raw_label):
                label, with_kind_word = claim_label_variants(segment)
                yield label, with_kind_word, line_number


def ui_label_claims(prose, page_path):
    variants_by_label = {}
    for label, with_kind_word, _ in ui_label_claim_occurrences(prose, page_path):
        variants_by_label.setdefault(label, {label}).add(with_kind_word)
    return {label: variants for label, variants in sorted(variants_by_label.items()) if is_checkable_ui_label(label)}


def same_page_fragments(prose):
    return sorted(set(re.findall(r'\]\(\s*#([^)\s]+)\)', prose)) | set(re.findall(r'\bhref=["\']#([^"\']+)["\']', prose)))


def docs_links(prose):
    links = {
        re.sub(r'^https?://(?:www\.)?posthog\.com', '', url)
        for pattern in DOCS_LINK_PATTERNS for url in re.findall(pattern, prose)
    }
    return sorted(links)


def suggestion_tokens(text):
    word_separated = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text.lstrip('#'))
    return [token for token in re.split(r'[^a-z0-9]+', word_separated.lower()) if token and token not in SUGGESTION_ARTICLES]


def without_leading_step_number(tokens):
    if tokens[:1] == ['step'] and tokens[1:2] and tokens[1].isdigit():
        return tokens[2:]
    if tokens[:1] and tokens[0].isdigit():
        return tokens[1:]
    return tokens


def is_whole_token_affix(shorter, longer):
    if not MIN_EXACT_AFFIX_BASE_TOKENS <= len(shorter) < len(longer):
        return False
    return longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter


def keeps_distinguishing_tokens(original, suggestion, candidate_count_by_token):
    suggested_tokens = set(suggestion_tokens(suggestion))
    dropped_tokens = {token for token in without_leading_step_number(suggestion_tokens(original)) if token not in suggested_tokens}
    if len(dropped_tokens) > 1:
        return False
    return not any(candidate_count_by_token[token] for token in dropped_tokens)


def exact_rewrite_strength(original, suggestion, candidate_count_by_token=None):
    original_tokens, suggested_tokens = suggestion_tokens(original), suggestion_tokens(suggestion)
    renumbered_original, renumbered_suggestion = without_leading_step_number(original_tokens), without_leading_step_number(suggested_tokens)
    if original_tokens and original_tokens == suggested_tokens:
        return 3
    if renumbered_original and renumbered_original == renumbered_suggestion:
        return 2
    is_affix = any(
        is_whole_token_affix(first, second) or is_whole_token_affix(second, first)
        for first, second in ((original_tokens, suggested_tokens), (renumbered_original, renumbered_suggestion))
    )
    if not is_affix:
        return 0
    return 1 if keeps_distinguishing_tokens(original, suggestion, candidate_count_by_token or collections.Counter()) else 0


def are_similar_tokens(first, second):
    if first == second or first.removesuffix('s') == second.removesuffix('s'):
        return True
    return min(len(first), len(second)) >= MIN_FUZZY_TOKEN_LENGTH and is_within_edit_distance(first, second, 1)


def token_jaccard(first_tokens, second_tokens):
    first_set, second_set = set(first_tokens), set(second_tokens)
    unmatched_second_tokens = set(second_set)
    shared_count = 0
    for token in sorted(first_set, key=lambda first_token: (first_token not in second_set, first_token)):
        similar_token = next((other for other in sorted(unmatched_second_tokens) if are_similar_tokens(token, other)), None)
        if similar_token is None:
            continue
        unmatched_second_tokens.discard(token if token in unmatched_second_tokens else similar_token)
        shared_count += 1
    union_count = len(first_set) + len(second_set) - shared_count
    return shared_count / union_count if union_count else 0.0


def shares_long_similar_token(first, second, min_length):
    return any(
        are_similar_tokens(first_token, second_token)
        for first_token in suggestion_tokens(first) if len(first_token) >= min_length
        for second_token in suggestion_tokens(second) if len(second_token) >= min_length
    )


def graded_suggestion(original, candidates):
    candidates = sorted(set(candidates))
    candidate_count_by_token = collections.Counter(token for candidate in candidates for token in set(suggestion_tokens(candidate)))
    strength_by_candidate = {candidate: exact_rewrite_strength(original, candidate, candidate_count_by_token) for candidate in candidates}
    strongest = max(strength_by_candidate.values(), default=0)
    exact_matches = [candidate for candidate, strength in strength_by_candidate.items() if strongest and strength == strongest]
    if len(exact_matches) == 1:
        return Suggestion(exact_matches[0], 'exact')
    original_tokens = suggestion_tokens(original)
    scored = [(token_jaccard(original_tokens, suggestion_tokens(candidate)), candidate) for candidate in exact_matches or candidates]
    plausible = [(score, candidate) for score, candidate in scored if score >= MIN_CANDIDATE_JACCARD]
    if not plausible:
        return NO_SUGGESTION
    _, best_candidate = max(
        plausible, key=lambda scored_candidate: (scored_candidate[0], difflib.SequenceMatcher(None, original, scored_candidate[1]).ratio())
    )
    return Suggestion(best_candidate, 'candidate')


def prefixed_suggestion(prefix, suggestion):
    return Suggestion(prefix + suggestion, suggestion.confidence) if suggestion else NO_SUGGESTION


def fragment_suggestion(page_text, page_file, fragment):
    anchor_ids, _ = cached_page_anchor_index(page_text, page_file)
    suggestion = prefixed_suggestion('#', graded_suggestion(fragment, anchor_ids))
    if suggestion.confidence == 'exact' and is_ambiguous_anchor(suggestion.removeprefix('#'), ambiguous_heading_slugs(page_text, page_file)):
        return Suggestion(suggestion, 'candidate')
    return suggestion


def same_section_anchor_suggestion(anchor, section_anchor_ids, scope):
    graded = prefixed_suggestion('#', graded_suggestion(anchor, section_anchor_ids))
    if graded:
        return graded
    setting_ids = sorted(section_anchor_ids - set(scope.ids_by_section))
    if len(setting_ids) == 1:
        return Suggestion(f'#{setting_ids[0]}', 'candidate')
    return NO_SUGGESTION


def settings_anchor_suggestion(settings_path, anchor, scope):
    section_anchor_ids = linked_section_anchor_ids(settings_path, scope)
    if section_anchor_ids:
        return same_section_anchor_suggestion(anchor, section_anchor_ids, scope)
    owning_sections = sorted({
        canonical_settings_section(section, scope.section_aliases) for section, anchors in scope.ids_by_section.items() if anchor in anchors
    })
    if owning_sections:
        return Suggestion(f'settings/{owning_sections[0]}#{anchor}', 'exact' if len(owning_sections) == 1 else 'candidate')
    return prefixed_suggestion('#', graded_suggestion(anchor, settings_anchor_ids_in_scope(settings_path, scope)))


def route_suggestion_index():
    routes = set(LIVE_ROUTES)
    for page in (SITE / 'contents').rglob('*.md*'):
        if page.suffix not in MARKDOWN_SUFFIXES or not page.is_file():
            continue
        route = '/' + page.relative_to(SITE / 'contents').with_suffix('').as_posix()
        routes.add(route.removesuffix('/index'))
    routes_by_prefix = collections.defaultdict(list)
    for route in sorted(routes):
        routes_by_prefix[tuple(route.strip('/').split('/')[:2])].append(route)
    return sorted(routes), dict(routes_by_prefix)


def route_last_segment(route):
    return route.rstrip('/').rsplit('/', 1)[-1]


def docs_link_suggestion(route, context):
    segments = route.strip('/').split('/')
    last_segment = segments[-1]
    is_api_reference_link = segments[1:2] == ['api']
    is_section_root_link = len(segments) < MIN_MOVED_PAGE_LINK_SEGMENTS
    moved_routes = [
        candidate for candidate in context['docs_routes']
        if not is_section_root_link and candidate.startswith('/docs/') and candidate != route
        and route_last_segment(candidate) == last_segment and (is_api_reference_link or not candidate.startswith('/docs/api/'))
    ]
    if len(moved_routes) == 1:
        return Suggestion(moved_routes[0], 'candidate')
    same_section_routes = [
        candidate for candidate in context['docs_routes_by_prefix'].get(tuple(segments[:2]), ())
        if candidate != route and shares_long_similar_token(last_segment, route_last_segment(candidate), MIN_SHARED_LINK_TOKEN_LENGTH)
    ]
    last_segment_suggestion = graded_suggestion(last_segment, (route_last_segment(candidate) for candidate in same_section_routes))
    if not last_segment_suggestion:
        return NO_SUGGESTION
    matching_routes = sorted(candidate for candidate in same_section_routes if route_last_segment(candidate) == last_segment_suggestion)
    return Suggestion(matching_routes[0], last_segment_suggestion.confidence if len(matching_routes) == 1 else 'candidate')


def is_comparable_app_route(doc_segments, route_segments):
    if len(doc_segments) != len(route_segments):
        return False
    if len(doc_segments) > 1 and doc_segments[0] != route_segments[0]:
        return False
    differing_segments = [
        (doc_segment, route_segment) for doc_segment, route_segment in zip(doc_segments, route_segments)
        if doc_segment != route_segment and route_segment != ':id'
    ]
    return bool(differing_segments) and all(
        shares_long_similar_token(doc_segment, route_segment, 1) for doc_segment, route_segment in differing_segments
    )


def app_url_suggestion(segments, context):
    is_unknown_settings_section = (
        segments[0] == 'settings' and len(segments) >= 2 and not is_doc_placeholder(segments[1])
        and segments[1] not in context['settings_sections']
    )
    if is_unknown_settings_section:
        section_aliases = context['settings_anchor_scope'].section_aliases
        canonical_sections = {canonical_settings_section(section, section_aliases) for section in context['settings_sections']}
        return prefixed_suggestion('settings/', graded_suggestion(segments[1], canonical_sections))
    comparable_routes = [route for route in context['app_routes'] if is_comparable_app_route(segments, route.split('/'))]
    return graded_suggestion('/'.join(segments), comparable_routes)


def app_route_suggestions(route_shapes):
    return sorted({
        '/'.join(':id' if segment.startswith((':', '*')) else segment.replace(ROUTE_PARAM_MARKER, ':id') for segment in segments)
        + ('/:id' if has_open_suffix else '')
        for segments, has_open_suffix in route_shapes
    })


def audit_page(text, context, page_path=''):
    audit = PageAudit()
    checks_posthog_ui = not page_path.startswith(THIRD_PARTY_UI_SECTIONS)
    code_blocks = fenced_blocks(text)
    named_instances = page_named_instances(body for _, body in code_blocks)
    page_import_sdks = [sdk for _, body in code_blocks for sdk in fence_import_sdks(body)]
    for lang, body in code_blocks:
        sdks = sdks_for_fence(lang, body, page_path, page_import_sdks)
        for method in sdk_method_calls(body, sdks[0]) if sdks else ():
            is_defined = method in named_instances or any(method in context['sdk_identifiers'][sdk] for sdk in sdks)
            suggestion = NO_SUGGESTION
            if not is_defined:
                identifiers = set().union(*(context['sdk_identifiers'][sdk] for sdk in sdks))
                suggestion = graded_suggestion(method, identifiers)
            sdk = next((sdk for sdk in sdks if method in context['sdk_identifiers'][sdk]), sdks[0])
            sources = sdk_backing_sources(sdk, method)
            if method in named_instances:
                sources = backing_sources('site', [f'contents/docs/{page_path}'])
            audit.check('sdk-method', is_defined, f'{lang}: posthog.{method}', suggestion,
                        claim=f'{sdk}: posthog.{method}', sources=sources, sdk=sdk, identifier=method)
    prose = strip_code(text)
    for host_path in app_url_paths(prose):
        segments = [s for s in host_path.split('/') if s]
        if segments[:1] == ['project'] and len(segments) >= 2:
            segments = segments[2:]
        if not segments or segments[0] in BACKEND_PATH_PREFIXES:
            continue
        is_known_settings_section = (
            segments[0] != 'settings' or len(segments) < 2 or is_doc_placeholder(segments[1])
            or segments[1] in context['settings_sections']
        )
        is_route_valid = is_known_settings_section and app_route_exists(segments, context['route_patterns'])
        suggestion = NO_SUGGESTION
        if not is_route_valid:
            suggestion = app_url_suggestion(segments, context)
            if host_path.startswith('project/'):
                suggestion = prefixed_suggestion('/'.join(host_path.split('/')[:2]) + '/', suggestion)
        sources, patterns = app_route_backing_sources(segments, context)
        audit.check('app-url', is_route_valid, host_path, suggestion, sources=sources, patterns=patterns)
    settings_anchor_pattern = r'posthog\.com/(?:project/[^/\s)#]+/)?settings/([^\s)#]*)#([A-Za-z0-9-]+)(?:=([\w-]*))?'
    for settings_path, hash_key, hash_value in sorted(set(re.findall(settings_anchor_pattern, prose))):
        is_setting_hash_param = hash_key in ('setting', 'selectedSetting')
        if settings_path.startswith('user-feature-previews') or (hash_value and not is_setting_hash_param):
            continue
        anchor = hash_value if is_setting_hash_param and hash_value else hash_key
        is_anchor_in_scope = anchor in settings_anchor_ids_in_scope(settings_path, context['settings_anchor_scope'])
        suggestion = settings_anchor_suggestion(settings_path, anchor, context['settings_anchor_scope']) if not is_anchor_in_scope else NO_SUGGESTION
        audit.check('settings-anchor', is_anchor_in_scope, f'settings/{settings_path}#{anchor}', suggestion,
                    sources=backing_sources('monorepo', ['frontend/src/scenes/settings/SettingsMap.tsx']))
        audit.evidence_files.add('frontend/src/scenes/settings/SettingsMap.tsx')
    checked_link_paths = set()
    for link in docs_links(prose):
        status, final_route, fragment = resolve_doc_route(link, context['redirects'])
        link_path = re.split(r'[?#]', link)[0]
        if link_path not in checked_link_paths:
            checked_link_paths.add(link_path)
            suggestion = docs_link_suggestion(link_path, context) if status != 'ok' else NO_SUGGESTION
            audit.check('docs-link', status == 'ok', f'{link_path} ({status}: {final_route})', suggestion,
                        claim=link_path, sources=docs_backing_sources(final_route))
        fragment_status = docs_fragment_status(final_route, fragment) if status == 'ok' and fragment else 'unverifiable'
        if fragment_status != 'unverifiable':
            suggestion = NO_SUGGESTION
            if fragment_status == 'missing':
                target_file = doc_content_file(final_route)
                suggestion = fragment_suggestion(target_file.read_text(errors='ignore'), target_file, fragment)
            audit.check('docs-fragment', fragment_status == 'ok', f'{link_path}#{fragment} ({final_route})', suggestion,
                        claim=f'{link_path}#{fragment}', sources=docs_backing_sources(final_route, fragment))
    page_file = SITE / 'contents' / 'docs' / page_path if page_path else None
    page_file = page_file if page_file and page_file.is_file() else None
    for fragment in same_page_fragments(prose):
        if page_file and '_snippets' in page_file.parts:
            fragment_status, hosts_lacking_fragment = snippet_fragment_status(page_file, fragment)
            if fragment_status != 'unverifiable':
                suggestion = NO_SUGGESTION
                if hosts_lacking_fragment:
                    host_file = SITE / 'contents' / 'docs' / hosts_lacking_fragment[0]
                    suggestion = fragment_suggestion(host_file.read_text(errors='ignore'), host_file, fragment)
                audit.check('docs-fragment', fragment_status == 'ok', f'#{fragment} (snippet; missing in: {", ".join(hosts_lacking_fragment)})', suggestion,
                            claim=f'{page_path}#{fragment}', sources=backing_sources('site', [page_file.relative_to(SITE)]))
            continue
        fragment_status = fragment_status_in_page(text, page_file, fragment)
        if fragment_status != 'unverifiable':
            suggestion = fragment_suggestion(text, page_file, fragment) if fragment_status == 'missing' else NO_SUGGESTION
            audit.check('docs-fragment', fragment_status == 'ok', f'#{fragment} (same page)', suggestion,
                        claim=f'{page_path}#{fragment}', sources=backing_sources('site', [f'contents/docs/{page_path}']))
    code_text = '\n'.join(body for _, body in code_blocks).casefold()
    is_desktop_page = page_path.startswith(DESKTOP_DOCS_PREFIX)
    area_prefixes = product_area_label_sources(page_path.split('/')[0] if '/' in page_path else '')
    for label, variants in (ui_label_claims(prose, page_path) if checks_posthog_ui else {}).items():
        if label_appears_in_code(label, code_text):
            continue
        label_index = context['ui_label_index']
        lookups = [
            desktop_scoped_lookup(files_containing_label(variant, label_index), is_desktop_page) for variant in sorted(variants)
        ]
        status, files = next((lookup for lookup in lookups if lookup[0] not in ('missing', 'tooltip-only')), None) or next(
            (lookup for lookup in lookups if lookup[0] == 'tooltip-only'), ('missing', [])
        )
        excluded_units = frozenset() if is_desktop_page else label_index.desktop_only_units
        near_miss = near_miss_ui_label(label, label_index, excluded_units, area_prefixes) if status == 'missing' else None
        tooltip_match = normalize_label_text(label) if status == 'tooltip-only' else near_miss
        if tooltip_match in label_index.tooltip_only_units:
            visible_text = label_index.visible_text_by_tooltip.get(tooltip_match) or 'unresolved'
            suggestion = Suggestion(label_index.visible_text_by_tooltip.get(tooltip_match, ''), 'candidate')
            audit.check('ui-label', False, f'{label} (tooltip only: {tooltip_match}; visible: {visible_text})', suggestion, claim=label)
            continue
        if near_miss:
            is_loose_rewrite = loose_label_key(normalize_label_text(label)) == loose_label_key(near_miss)
            suggestion = Suggestion(label_index.display_text_by_unit.get(near_miss, near_miss), 'exact' if is_loose_rewrite else 'candidate')
            audit.check('ui-label-minor', False, f'{label} (UI: {near_miss})', suggestion, claim=label)
            continue
        audit.check('ui-label', status != 'missing', label, sources=backing_sources('monorepo', files[:5]))
        if len(files) <= 5:
            audit.evidence_files.update(files)
    return audit


HEADING_LINE = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$')
DOCS_LINK_PREFIX = r'(?:posthog\.com|(?<![\w/.-]))'
LINK_END = r'(?=[?#\s)"\'`>\]]|$)'
IMPORT_BINDING = re.compile(r'^import\s+(\w+)\s+from\s+["\']([^"\']+)["\']', flags=re.M)
MARKDOWN_LINK_SPAN = re.compile(r'\[([^\]\n]+)\]\(([^)\n]*)\)')
HTML_LINK_SPAN = re.compile(r'<(a|Link)\b[^>]*>(.*?)</\1>')


def claim_location_pattern(kind, claim):
    if kind == 'sdk-method':
        return re.compile(r'\bposthog\.' + re.escape(claim.split('posthog.', 1)[1]) + r'\b')
    if kind == 'app-url':
        return re.compile(r'(?:us|eu|app)\.posthog\.com/' + re.escape(claim) + r'(?=[\s)"\'>#?`\]]|$)')
    if kind == 'settings-anchor':
        settings_path, anchor = claim.removeprefix('settings/').split('#', 1)
        escaped_anchor = re.escape(anchor)
        return re.compile(
            r'posthog\.com/(?:project/[^/\s)#]+/)?settings/' + re.escape(settings_path)
            + rf'#(?:(?:setting|selectedSetting)={escaped_anchor}(?![\w-])|{escaped_anchor}(?![A-Za-z0-9-]))'
        )
    if kind == 'docs-link':
        return re.compile(DOCS_LINK_PREFIX + re.escape(claim) + LINK_END)
    if kind == 'docs-fragment' and claim.startswith('/'):
        link_path, fragment = claim.split('#', 1)
        return re.compile(DOCS_LINK_PREFIX + re.escape(link_path) + r'(?:\?[^#\s)"\'`>]*)?#' + re.escape(fragment) + r'(?=[\s)"\'`>\]]|$)')
    if kind == 'docs-fragment':
        return re.compile(r'(?:\]\(\s*|\bhref=["\'])#' + re.escape(claim.split('#', 1)[1]) + r'(?=[)\s"\'])')
    raise ValueError(f'No line locator for claim kind {kind!r}')


def sdk_fenced_code_text(page_text, page_path, sdk):
    fences, lines = code_fences(page_text)
    page_import_sdks = [imported for language, body, _, _ in fences if language for imported in fence_import_sdks(body)]
    code_line_indexes = {
        index for language, body, start, end in fences
        if language and sdk in sdks_for_fence(language, body, page_path, page_import_sdks)
        for index in range(start + 1, end)
    }
    return ''.join(line if index in code_line_indexes else '\n' for index, line in enumerate(lines))


def claim_line_numbers(page_text, page_path, kind, claim):
    prose = strip_code(page_text)
    if kind in ('ui-label', 'ui-label-minor'):
        line_numbers = sorted({line_number for label, _, line_number in ui_label_claim_occurrences(prose, page_path) if label == claim})
    if kind not in ('ui-label', 'ui-label-minor'):
        searched_text = {'app-url': inline_code_removed(prose)}.get(kind, prose)
        if kind == 'sdk-method':
            searched_text = sdk_fenced_code_text(page_text, page_path, claim.split(':', 1)[0])
        pattern = claim_location_pattern(kind, claim)
        line_numbers = [line_number for line_number, line in enumerate(searched_text.splitlines(), start=1) if pattern.search(line)]
    if not line_numbers:
        raise ValueError(f'Cannot locate {kind} claim {claim!r} in docs/{page_path}')
    return line_numbers


def claim_line_number(page_text, page_path, kind, claim):
    return claim_line_numbers(page_text, page_path, kind, claim)[0]


def displayed_link_text(line, pattern):
    for link in MARKDOWN_LINK_SPAN.finditer(line):
        if pattern.search(link.group(0)):
            return link.group(1)
    for link in HTML_LINK_SPAN.finditer(line):
        if pattern.search(link.group(0)):
            return re.sub(r'<[^>]*>', '', link.group(2))
    return pattern.search(line).group(0)


def claim_display_words(line, kind, claim):
    if kind in ('ui-label', 'ui-label-minor'):
        return claim
    if kind == 'sdk-method':
        return 'posthog.' + claim.split('posthog.', 1)[1]
    words = displayed_link_text(line, claim_location_pattern(kind, claim))
    return re.sub(r'\\([\W_])', r'\1', words.replace('**', '').replace('`', '')).strip()


def heading_anchor_before_line(page_text, line_number):
    slug_counts = collections.Counter()
    anchor = ''
    for index, line in enumerate(strip_code(page_text).splitlines()[:line_number], start=1):
        heading = HEADING_LINE.match(line)
        if not heading:
            continue
        slug = slugify_heading(heading.group(1))
        anchor = f'{slug}-{slug_counts[slug]}' if slug_counts[slug] else slug
        slug_counts[slug] += 1
    return anchor


def imports_transitively(content_file, target_file, visited_files=()):
    if content_file == target_file:
        return True
    if content_file in visited_files or content_file.suffix not in MARKDOWN_SUFFIXES:
        return False
    imported_files = [resolve_content_import(content_file, specifier) for specifier in SNIPPET_IMPORT.findall(content_file.read_text(errors='ignore'))]
    return any(imports_transitively(imported, target_file, (*visited_files, content_file)) for imported in imported_files if imported)


def snippet_usage_line(host_file, host_text, snippet_file):
    for binding, specifier in IMPORT_BINDING.findall(host_text):
        imported_file = resolve_content_import(host_file, specifier)
        if imported_file is None or not imports_transitively(imported_file, snippet_file):
            continue
        usage = re.search(rf'<{binding}\b', host_text)
        if usage:
            return host_text.count('\n', 0, usage.start()) + 1
    return None


def live_spot_urls(page_path, page_text, line_number, kind, claim):
    words = claim_display_words(page_text.splitlines()[line_number - 1], kind, claim)
    own_anchor = heading_anchor_before_line(page_text, line_number)
    if '_snippets' not in Path(page_path).parts:
        return [text_fragment_url(docs_page_url(page_path), own_anchor, words)]
    docs_root = SITE / 'contents' / 'docs'
    snippet_file = Path(os.path.normpath(docs_root / page_path))
    urls = []
    for host_file in snippet_host_pages(snippet_file):
        host_text = host_file.read_text(errors='ignore')
        usage_line = snippet_usage_line(host_file, host_text, snippet_file)
        if usage_line is None:
            continue
        anchor = own_anchor or heading_anchor_before_line(host_text, usage_line)
        urls.append(text_fragment_url(docs_page_url(str(host_file.relative_to(docs_root))), anchor, words))
    return urls


def is_unpublished_snippet(page_path):
    if '_snippets' not in Path(page_path).parts:
        return False
    return not snippet_host_pages(Path(os.path.normpath(SITE / 'contents' / 'docs' / page_path)))


def claim_location(page_path, page_text, kind, claim):
    line_numbers = claim_line_numbers(page_text, page_path, kind, claim)
    live_urls = [url for line_number in line_numbers for url in live_spot_urls(page_path, page_text, line_number, kind, claim)]
    return {
        'line': line_numbers[0], 'lines': line_numbers, 'live_urls': list(dict.fromkeys(live_urls)),
        'is_unpublished_snippet': is_unpublished_snippet(page_path),
    }


def build_context():
    route_sources = frontend_route_sources()
    route_shapes = sorted(route_sources)
    docs_routes, docs_routes_by_prefix = route_suggestion_index()
    return {
        'sdk_identifiers': sdk_identifiers(),
        'ui_label_index': ui_label_index(),
        'settings_sections': settings_section_ids(),
        'settings_anchor_scope': settings_anchor_scope(),
        'route_sources': route_sources,
        'route_patterns': [
            ([route_segment_pattern(segment) for segment in segments], has_open_suffix)
            for segments, has_open_suffix in route_shapes
        ],
        'app_routes': app_route_suggestions(route_shapes),
        'docs_routes': docs_routes,
        'docs_routes_by_prefix': docs_routes_by_prefix,
        'redirects': site_redirects(),
    }


def main():
    section = sys.argv[1]
    now = int(sys.argv[2]) if len(sys.argv) > 2 else 1790726400
    context = build_context()
    section_dir = f'contents/docs/{section}'
    edit_timestamps = last_real_edit_timestamps(section_dir)
    report = []
    checked_totals = collections.Counter()
    audits_by_page = {
        str(page.relative_to(SITE)): audit_page(page.read_text(), context, str(page.relative_to(SITE / 'contents/docs')))
        for page in sorted((SITE / section_dir).rglob('*.md*'))
    }
    commit_history = evidence_commit_history(set().union(*(audit.evidence_files for audit in audits_by_page.values())))
    for relative, audit in audits_by_page.items():
        last_edit = edit_timestamps.get(relative)
        checked_totals.update(audit.checked_by_kind)
        report.append({
            'page': relative.removeprefix(section_dir + '/'),
            'is_last_edit_unknown': last_edit is None,
            'days_since_edit': None if last_edit is None else (now - last_edit) // 86400,
            'evidence_files': sorted(audit.evidence_files),
            'evidence_commits_since_edit': commits_since(audit.evidence_files, last_edit, commit_history),
            'claims_checked': sum(audit.checked_by_kind.values()),
            'findings': [
                {'kind': kind, 'detail': detail, 'suggestion': str(suggestion), 'confidence': suggestion.confidence}
                for kind, detail, suggestion in audit.findings
            ],
        })
    OUTPUT_ROOT.mkdir(exist_ok=True)
    json.dump(report, open(OUTPUT_ROOT / f'audit-{section}.json', 'w'), indent=1)
    failed_totals = collections.Counter(finding['kind'] for row in report for finding in row['findings'])
    print(f'{section}: {len(report)} pages')
    for kind in sorted(checked_totals):
        print(f'  {kind:16} checked={checked_totals[kind]:4} failed={failed_totals[kind]:3}')
    ranked = sorted(report, key=lambda r: (-len(r['findings']), -r['evidence_commits_since_edit']))
    for row in ranked:
        age = '   ?' if row['is_last_edit_unknown'] else f"{row['days_since_edit']:4}"
        print(f"{row['page']:55} age={age}d evidence_files={len(row['evidence_files']):2} "
              f"drift={row['evidence_commits_since_edit']:4} checked={row['claims_checked']:3} failed={len(row['findings'])}")
        for finding in row['findings']:
            next_step = f" -> {finding['suggestion']} ({finding['confidence']})" if finding['suggestion'] else ''
            print(f"    {finding['kind']:16} {finding['detail']}{next_step}")


main()
