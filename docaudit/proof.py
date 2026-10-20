import argparse
import difflib
import functools
import html
import json
import re
import traceback
from pathlib import Path
from unittest.mock import patch

import impact

SOURCE_MARKER = re.compile(r'<!-- docs-freshness-source: ([\w.-]+/[\w.-]+)#(\d+) ([0-9a-f]{40}) -->')
CLAIM_MARKER = re.compile(r'<!-- docs-freshness-claim: (\{.*?\}) -->')
DOCS_PREFIX = 'contents/docs/'
EDITABLE_SUFFIXES = ('.md', '.mdx')
NON_PROSE_LINE = re.compile(r'^\s*(?:import|export)\b|</?[A-Za-z]|[{}]')
ESM_STATEMENT_LINE = re.compile(r'^\s*(?:import|export)\b')
MARKUP_OPENER = re.compile(r'<!--|</?[A-Za-z]|\{')
INLINE_CODE_SPAN = re.compile(r'(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)')
SETEXT_UNDERLINE_LINE = re.compile(r'^\s{0,3}(?:=+|-+)\s*$')
THEMATIC_BREAK_LINE = re.compile(r'^\s{0,3}([-*_])(?:\s*\1){2,}\s*$')
INLINE_LINK_DESTINATION = re.compile(r'\]\(\s*(?:<([^>\n]*)>|([^\s)]*))')
REFERENCE_DEFINITION_DESTINATION = re.compile(r'^[ ]{0,3}\[[^\]\n]+\]:\s*(?:<([^>\n]*)>|(\S+))', flags=re.M)
MARKDOWN_ESCAPE = re.compile(r'\\([!-/:-@\[-`{-~])')
CONTROL_OR_SPACE = re.compile(r'[\x00-\x20\x7f]')
BARE_URL = re.compile(r'https?://[^\s<>()\[\]"\'`]+')
BARE_URL_TRAILING_PUNCTUATION = re.compile(r'[.,;:!?*_~]+$')
REPEATABLE_FINDING_KINDS = ('docs-link', 'docs-fragment', 'app-url', 'settings-anchor')
UNSAFE_LINK_SCHEMES = ('javascript:', 'data:', 'vbscript:')
ALLOWED_LINK_DESTINATION = re.compile(r'^(?:/docs(?:[/?#]|$)|https://(?:(?:us|eu|app)\.)?posthog\.com/)')


def comparable_kind(kind):
    return 'ui-label' if kind == 'ui-label-minor' else kind


def read_markers(body):
    sources = SOURCE_MARKER.findall(body)
    if len(sources) != 1:
        return None, [], [f'expected exactly one docs-freshness-source marker, found {len(sources)}']
    repository, number, head_sha = sources[0]
    claims = [json.loads(marker) for marker in CLAIM_MARKER.findall(body)]
    if not claims:
        return None, [], ['no docs-freshness-claim marker names a triggering claim']
    return {'repository': repository, 'number': int(number), 'head_sha': head_sha}, claims, []


def frontmatter_line_count(lines):
    if not lines or lines[0].strip() != '---':
        return 0
    closing_index = next((index for index in range(1, len(lines)) if lines[index].strip() == '---'), len(lines) - 1)
    return closing_index + 1


def section_spans(audit, text):
    lines = text.splitlines()
    prose_lines = audit.strip_code(text).splitlines()
    heading_indexes = [index for index, line in enumerate(prose_lines) if audit.HEADING_LINE.match(line)]
    start_indexes = [frontmatter_line_count(lines), *heading_indexes]
    end_indexes = [*heading_indexes, len(lines)]
    return list(zip(start_indexes, end_indexes))


def section_holding(spans, line_index):
    return next(span_index for span_index, (start, end) in enumerate(spans) if start <= line_index < end)


def edit_is_within(base_start, base_end, section_start, section_end):
    if base_start == base_end:
        return section_start < base_start <= section_end
    return section_start <= base_start and base_end <= section_end


def comment_closing_offset(text, offset):
    closing_offset = text.find('-->', offset)
    return len(text) if closing_offset < 0 else closing_offset + 2


def markup_closing_offset(text, offset, opener):
    if opener == '<!--':
        return comment_closing_offset(text, offset)
    is_expression = opener == '{'
    open_quote, brace_depth = None, int(is_expression)
    for position in range(offset, len(text)):
        character = text[position]
        if open_quote:
            open_quote = None if character == open_quote else open_quote
            continue
        if character in '"\'`':
            open_quote = character
            continue
        if character == '{':
            brace_depth += 1
            continue
        if character == '}':
            brace_depth = max(0, brace_depth - 1)
            if is_expression and brace_depth == 0:
                return position
            continue
        if character == '>' and brace_depth == 0 and not is_expression:
            return position
    return len(text)


def markup_line_indexes(scannable_text):
    indexes = set()
    scanned_offset = 0
    for opener in MARKUP_OPENER.finditer(scannable_text):
        if opener.start() < scanned_offset:
            continue
        scanned_offset = markup_closing_offset(scannable_text, opener.end(), opener.group())
        first_line_index = scannable_text.count('\n', 0, opener.start())
        indexes.update(range(first_line_index, scannable_text.count('\n', 0, scanned_offset) + 1))
    return indexes


def esm_statement_line_indexes(lines, markup_indexes):
    indexes = set()
    is_inside_statement = False
    for index, line in enumerate(lines):
        is_inside_statement = bool(line.strip()) and (is_inside_statement or (index not in markup_indexes and bool(ESM_STATEMENT_LINE.match(line))))
        if is_inside_statement:
            indexes.add(index)
    return indexes


def setext_and_break_line_indexes(lines, markup_indexes):
    indexes = set()
    for index, line in enumerate(lines):
        if index in markup_indexes:
            continue
        if THEMATIC_BREAK_LINE.match(line):
            indexes.add(index)
        if index > 0 and lines[index - 1].strip() and SETEXT_UNDERLINE_LINE.match(line):
            indexes.update((index - 1, index))
    return indexes


def non_prose_line_indexes(audit, text):
    lines = text.splitlines()
    fences, _ = audit.code_fences(text)
    indexes = set(range(frontmatter_line_count(lines)))
    indexes.update(index for _, _, start, end in fences for index in range(start, end))
    scannable_lines = ['' if index in indexes else INLINE_CODE_SPAN.sub(lambda span: ' ' * len(span.group()), line) for index, line in enumerate(lines)]
    scannable_text = '\n'.join(scannable_lines)
    markup_indexes = set(indexes)
    indexes.update(markup_line_indexes(scannable_text))
    indexes.update(esm_statement_line_indexes(lines, markup_indexes))
    indexes.update(setext_and_break_line_indexes(lines, markup_indexes))
    indexes.update(index for index, line in enumerate(lines) if audit.HEADING_LINE.match(line))
    indexes.update(index for index, line in enumerate(scannable_lines) if NON_PROSE_LINE.search(line))
    return indexes


def link_destinations(text):
    matches = [*INLINE_LINK_DESTINATION.finditer(text), *REFERENCE_DEFINITION_DESTINATION.finditer(text)]
    return {match.group(1) if match.group(1) is not None else match.group(2) for match in matches}


def is_unsafe_link_destination(destination):
    normalized_destination = CONTROL_OR_SPACE.sub('', html.unescape(MARKDOWN_ESCAPE.sub(r'\1', destination))).lower()
    return normalized_destination.startswith(UNSAFE_LINK_SCHEMES)


def is_allowed_new_link_destination(destination):
    normalized_destination = CONTROL_OR_SPACE.sub('', html.unescape(MARKDOWN_ESCAPE.sub(r'\1', destination)))
    return bool(ALLOWED_LINK_DESTINATION.match(normalized_destination))


def bare_urls(text):
    return {BARE_URL_TRAILING_PUNCTUATION.sub('', url) for url in BARE_URL.findall(text)}


def link_failures(page, base_text, head_text, changed_head_text):
    base_destinations = link_destinations(base_text) | bare_urls(base_text)
    return [
        f'docs/{page}: link destination {destination} is not in the docs base and is not a /docs path or posthog.com URL'
        for destination in sorted(link_destinations(head_text) | bare_urls(changed_head_text))
        if is_unsafe_link_destination(destination) or (destination not in base_destinations and not is_allowed_new_link_destination(destination))
    ]


def claim_sections(audit, page, text, claim):
    spans = section_spans(audit, text)
    return {section_holding(spans, line_number - 1) for line_number in audit.claim_line_numbers(text, page, claim['kind'], claim['claim'])}


def scope_failures(audit, page, base_text, head_text, page_claims):
    spans = section_spans(audit, base_text)
    allowed_sections = set().union(*(claim_sections(audit, page, base_text, claim) for claim in page_claims))
    base_non_prose, head_non_prose = non_prose_line_indexes(audit, base_text), non_prose_line_indexes(audit, head_text)
    failures = []
    head_lines = head_text.splitlines()
    changed_head_lines = []
    matcher = difflib.SequenceMatcher(None, base_text.splitlines(), head_lines, autojunk=False)
    for tag, base_start, base_end, head_start, head_end in matcher.get_opcodes():
        if tag == 'equal':
            failures.extend(
                f'docs/{page}: edit changes whether base line {base_index + 1} is prose'
                for base_index, head_index in zip(range(base_start, base_end), range(head_start, head_end))
                if (base_index in base_non_prose) != (head_index in head_non_prose)
            )
            continue
        changed_head_lines.extend(head_lines[head_start:head_end])
        is_in_allowed_section = any(
            edit_is_within(base_start, base_end, *spans[index]) for index in allowed_sections
        )
        if not is_in_allowed_section:
            failures.append(f'docs/{page}: edit at base line {base_start + 1} is outside the sections holding the triggering claims')
            continue
        if base_non_prose.intersection(range(base_start, base_end)) or head_non_prose.intersection(range(head_start, head_end)):
            failures.append(f'docs/{page}: edit at base line {base_start + 1} changes a non-prose line')
    return failures + link_failures(page, base_text, head_text, '\n'.join(changed_head_lines))


def claim_occurrence_count(audit, text, page, kind, claim):
    prose = audit.strip_code(text)
    searched_text = audit.inline_code_removed(prose) if kind == 'app-url' else prose
    pattern = audit.claim_location_pattern(kind, claim)
    is_claim_on_page = any(pattern.search(line) for line in searched_text.splitlines())
    return len(audit.claim_line_numbers(text, page, kind, claim)) if is_claim_on_page else 0


def repeated_claim_failures(audit, page, base_text, head_text, head_audit):
    repeatable_failing_claims = sorted(
        {(kind, claim) for (kind, _, _), claim in zip(head_audit.findings, head_audit.failed_claims) if kind in REPEATABLE_FINDING_KINDS}
    )
    return [
        f'docs/{page}: new copy of failing {kind} claim {claim}'
        for kind, claim in repeatable_failing_claims
        if claim_occurrence_count(audit, head_text, page, kind, claim) > claim_occurrence_count(audit, base_text, page, kind, claim)
    ]


def failing_claims(page_audit):
    return {(comparable_kind(kind), claim) for (kind, _, _), claim in zip(page_audit.findings, page_audit.failed_claims)}


def reverification_failures(audit, page, claim, base_audit, head_audit, base_text, head_text):
    kind = comparable_kind(claim['kind'])
    label = f"docs/{page}: {claim['kind']} '{claim['claim']}'"
    if (kind, claim['claim']) not in failing_claims(base_audit):
        return [f'{label} does not fail at the docs base, so it cannot trigger this PR']
    if (kind, claim['claim']) in failing_claims(head_audit):
        return [f'{label} still fails on the docs head']
    sections = claim_sections(audit, page, base_text, claim)
    has_passing_claim_in_sections = any(
        comparable_kind(reference['kind']) == kind and claim_sections(audit, page, head_text, reference) & sections
        for reference in head_audit.references
    )
    if not has_passing_claim_in_sections:
        return [f'{label} has no passing {kind} claim left in its section on the docs head']
    return []


def prove(base, head, body, references=None):
    source, claims, failures = read_markers(body)
    if failures:
        return {'passed': False, 'source': None, 'docs_head': head, 'failures': failures}
    site, monorepo = impact.REPOSITORIES['site'], impact.REPOSITORIES['monorepo']
    head = impact.git(site, 'rev-parse', '--verify', f'{head}^{{commit}}').strip()
    base = impact.git(site, 'merge-base', base, head).strip()
    if not impact.worktree_holds_revision(site, head):
        raise ValueError(f'The site checkout {site} must be a clean checkout of the docs head {head}')
    report = {'passed': False, 'source': source, 'docs_head': head, 'failures': failures}
    monorepo_head = impact.git(monorepo, 'rev-parse', 'HEAD').strip()
    if monorepo_head != source['head_sha']:
        failures.append(f"The monorepo checkout is at {monorepo_head}, not the source head {source['head_sha']}")
        return report
    claims_by_page = {}
    for claim in claims:
        claims_by_page.setdefault(claim['page'], []).append(claim)
    touched_paths = [path for path in impact.git(site, 'diff', '--name-only', '-z', '--no-renames', base, head).split('\0') if path]
    for path in touched_paths:
        page = path.removeprefix(DOCS_PREFIX)
        if not path.startswith(DOCS_PREFIX) or not path.endswith(EDITABLE_SUFFIXES) or page not in claims_by_page:
            failures.append(f'{path} is not a docs page holding a triggering claim')
    if failures:
        return report
    audit = impact.checker()
    with patch.object(audit, 'SITE', site), patch.object(audit, 'MONOREPO', monorepo), patch.object(audit, 'build_context', functools.cache(audit.build_context)):
        texts_by_page = {
            page: (impact.git(site, 'show', f'{base}:{DOCS_PREFIX}{page}'), (site / DOCS_PREFIX / page).read_text())
            for page in claims_by_page if f'{DOCS_PREFIX}{page}' in touched_paths
        }
        untouched_pages = sorted(set(claims_by_page) - set(texts_by_page))
        failures.extend(f'docs/{page} holds a triggering claim but this PR does not edit it' for page in untouched_pages)
        for page, (base_text, head_text) in texts_by_page.items():
            failures.extend(scope_failures(audit, page, base_text, head_text, claims_by_page[page]))
        if failures:
            return report
        failures.extend(f"New finding: {entry['guard']}" for entry in impact.check_impact('site', f'{base}..{head}', references=references))
        context = audit.build_context()
        for page, (base_text, head_text) in texts_by_page.items():
            base_audit, head_audit = audit.audit_page(base_text, context, page), audit.audit_page(head_text, context, page)
            base_findings = {(kind, detail) for kind, detail, _ in base_audit.findings}
            failures.extend(f'docs/{page}: new {kind} finding {detail}' for kind, detail, _ in head_audit.findings if (kind, detail) not in base_findings)
            failures.extend(repeated_claim_failures(audit, page, base_text, head_text, head_audit))
            for claim in claims_by_page[page]:
                failures.extend(reverification_failures(audit, page, claim, base_audit, head_audit, base_text, head_text))
    report['passed'] = not failures
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--body-file', type=Path, required=True)
    parser.add_argument('--json', type=Path)
    arguments = parser.parse_args()
    try:
        report = prove(arguments.base, arguments.head, arguments.body_file.read_text())
    except Exception as failure:
        traceback.print_exc()
        parser.exit(2, f'proof: {type(failure).__name__}: {failure}\n')
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=2) + '\n')
    for failure in report['failures']:
        print(failure)
    if report['passed']:
        source = report['source']
        print(f"docs-proof passed on {report['docs_head']} for {source['repository']}#{source['number']} at {source['head_sha']}")
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
