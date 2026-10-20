import argparse
import collections
import datetime
import json
from pathlib import Path
from urllib.parse import urlsplit

from doccoverage import (
    CHANGELOG_GAP_BUCKET_LABELS, CHANGELOG_GAP_BUCKETS, GITHUB_REPOSITORY_NAMES, OUTPUT_ROOT, docs_page_url, github_blob_url, github_commit_url,
    undocumented_identifiers,
)

COVERAGE_SOURCES = (
    'js-config', 'settings', 'settings-sections', 'js-changelog', 'python-changelog', 'js-methods', 'python-config', 'python-methods',
)
SUGGESTION_CONFIDENCES = ('exact', 'candidate', 'none')
LOCATIONS_FILE = 'finding-locations.json'


def read_findings(output_directory):
    findings = []
    for line_number, line in enumerate((output_directory / 'all-findings.txt').read_text().splitlines(), start=1):
        if not line:
            continue
        fields = line.split('\t')
        if len(fields) not in (3, 4, 5, 6):
            raise ValueError(f'{output_directory}/all-findings.txt:{line_number}: expected three to six TSV columns')
        page, kind, detail = fields[:3]
        suggestion = fields[3] if len(fields) >= 4 else ''
        unrecorded_confidence = 'candidate' if suggestion else 'none'
        confidence = fields[4] if len(fields) >= 5 else unrecorded_confidence
        if confidence not in SUGGESTION_CONFIDENCES:
            raise ValueError(f'{output_directory}/all-findings.txt:{line_number}: invalid suggestion confidence {confidence!r}')
        finding = {
            'page': page, 'url': docs_page_url(page), 'kind': kind, 'detail': detail,
            'suggestion': suggestion, 'confidence': confidence if suggestion else 'none',
        }
        if len(fields) == 6:
            finding['line'] = int(fields[5])
        findings.append(finding)
    return sorted(findings, key=finding_key)


def finding_key(finding):
    return finding['page'], finding['kind'], finding['detail']


def attach_locations(findings, output_directory):
    recorded = json.loads((output_directory / LOCATIONS_FILE).read_text())
    location_by_key = {finding_key(location): location for location in recorded['findings']}
    for finding in findings:
        location = location_by_key.get(finding_key(finding))
        if location is None or location['line'] != finding.get('line'):
            raise ValueError(f'{output_directory}/{LOCATIONS_FILE} lacks the recorded line of {finding_key(finding)}')
        finding.update({
            'source_url': location['source_url'], 'live_urls': location['live_urls'],
            'lines': location.get('lines', [location['line']]), 'source_urls': location.get('source_urls', [location['source_url']]),
            'is_unpublished_snippet': location.get('is_unpublished_snippet', False),
        })
    return recorded['site_revision']


def group_by_page(findings):
    findings_by_page = collections.defaultdict(list)
    for finding in findings:
        findings_by_page[finding['page']].append(finding)
    return dict(sorted(findings_by_page.items()))


def coverage_gaps(coverage):
    gaps_by_source = {}
    for source in COVERAGE_SOURCES:
        gaps_by_source[source] = {
            'undocumented': [entry for entry in coverage.get(source, []) if entry['status'] == 'undocumented'],
            'partially_documented': [
                {**entry, 'missing_identifiers': undocumented_identifiers(entry)}
                for entry in coverage.get(source, []) if entry['status'] == 'partially-documented'
            ],
        }
    return gaps_by_source


def build_report(output_directory, previous_directory=None):
    findings = read_findings(output_directory)
    site_revision = attach_locations(findings, output_directory)
    coverage = json.loads((output_directory / 'coverage.json').read_text())
    published_findings = [finding for finding in findings if not finding['is_unpublished_snippet']]
    findings_needing_judgment = collections.defaultdict(list)
    for finding in published_findings:
        if finding['confidence'] != 'none':
            continue
        findings_needing_judgment[finding['kind']].append(finding)
    report = {
        'run_date': datetime.date.today().isoformat(),
        'site_revision': site_revision,
        'total_findings': len(findings),
        'totals_by_kind': dict(sorted(collections.Counter(finding['kind'] for finding in findings).items())),
        'totals_by_confidence': {
            confidence: sum(finding['confidence'] == confidence for finding in findings) for confidence in SUGGESTION_CONFIDENCES
        },
        'fix_now': group_by_page(finding for finding in published_findings if finding['confidence'] == 'exact'),
        'candidate_fixes': group_by_page(finding for finding in published_findings if finding['confidence'] == 'candidate'),
        'unpublished_snippets': group_by_page(finding for finding in findings if finding['is_unpublished_snippet']),
        'needs_judgment': {
            kind: group_by_page(kind_findings) for kind, kind_findings in sorted(findings_needing_judgment.items())
        },
        'coverage_gaps': coverage_gaps(coverage),
    }
    from attribution import upstream_guards

    guards = upstream_guards(findings, report['coverage_gaps'], output_directory)
    report['upstream_guards'] = guards['guards']
    report['outside_every_gate'] = guards['outside_every_gate']
    report['attribution_totals'] = guards['totals']
    report['changelog_gaps_outside_totals'] = guards['changelog_gaps_outside_totals']
    report['attributed_findings'] = findings
    if previous_directory is None:
        return report
    previous_findings = read_findings(previous_directory)
    previous_coverage = json.loads((previous_directory / 'coverage.json').read_text())
    current_by_key = {finding_key(finding): finding for finding in findings}
    previous_by_key = {finding_key(finding): finding for finding in previous_findings}
    current_keys = set(current_by_key)
    previous_keys = set(previous_by_key)
    new_findings = [current_by_key[key] for key in sorted(current_keys - previous_keys)]
    resolved_findings = [previous_by_key[key] for key in sorted(previous_keys - current_keys)]
    report.update({
        'previous_directory': str(previous_directory),
        'delta': {'new': len(new_findings), 'resolved': len(resolved_findings), 'still_open': len(current_keys & previous_keys)},
        'resolved_since_last_run': group_by_page(resolved_findings),
        'new_since_last_run': group_by_page(new_findings),
        'previous_coverage_gap_counts': {
            source: {status: len(entries) for status, entries in gaps.items()}
            for source, gaps in coverage_gaps(previous_coverage).items()
        },
    })
    return report


def page_link(page):
    if '_snippets' in Path(page).parts:
        return f'{page} (snippet; live links below point at the pages that render it)'
    return f'{page} ([docs]({docs_page_url(page)}))'


def category_name(category):
    return category.replace('_', ' ')


def commit_link(repository, event):
    return f"[{event['revision'][:12]}]({github_commit_url(repository, event['revision'])}) {event['date']}"


def blob_link(repository, location):
    url = github_blob_url(repository, location['revision'], location['file'], location['line'])
    return f"[{location['file']}#L{location['line']} @ {location['revision'][:12]}]({url})"


def live_link(url):
    parts = urlsplit(url)
    heading_anchor = parts.fragment.split(':~:', 1)[0]
    label = f'{parts.path}#{heading_anchor}' if heading_anchor else parts.path
    return f'[{label}]({url})'


def absent_when_written_evidence(attribution, written):
    repository = attribution['repository']
    checked_revision = attribution['presence_checked_at']
    checked = f"[{checked_revision[:12]}]({github_commit_url(repository, checked_revision)})"
    absent = f"{written}; absent when written (checked at {checked})"
    appearance = attribution['source_changed_at']
    if appearance is None:
        return f"{absent}; never reappeared after the claim"
    return f"{absent}; first appeared in {commit_link(repository, appearance)}: {blob_link(repository, attribution['before'])}"


def finding_evidence(attribution):
    repository = attribution['repository']
    written = f"claim written in {commit_link('site', attribution['claim_written_at']['event'])}"
    if attribution['rule'] == 'source_absent_when_claim_written':
        return absent_when_written_evidence(attribution, written)
    changed = attribution['source_changed_at']
    if changed is None:
        return f"{written}; the cited target never resolved in the full history of {GITHUB_REPOSITORY_NAMES[repository]}"
    verb = 'broken by' if attribution['category'] != 'wrong_when_written' else 'already removed by'
    before = blob_link(repository, attribution['before'])
    return f"{written}; {verb} {commit_link(repository, changed)}; before the change: {before}"


def finding_lines(finding):
    suggestion = f" -> {finding['suggestion']}" if finding['suggestion'] else ''
    if 'attribution' not in finding:
        return [f"- {finding['kind']}: {finding['detail']}{suggestion}"]
    live_links = ', '.join(live_link(url) for url in finding['live_urls']) or 'not published: no docs page renders this snippet'
    return [
        f"- {finding['kind']}: {finding['detail']}{suggestion} ({category_name(finding['attribution']['category'])})",
        '  - Source: ' + ', '.join(
            f"[contents/docs/{finding['page']}#L{line}]({source_url})" for line, source_url in zip(finding['lines'], finding['source_urls'])
        ),
        f"  - Live: {live_links}",
        f"  - Evidence: {finding_evidence(finding['attribution'])}",
    ]


def append_page_findings(lines, findings_by_page, heading_level=3):
    if not findings_by_page:
        lines.extend(['None.', ''])
        return
    for page, findings in findings_by_page.items():
        lines.extend([f'{"#" * heading_level} {page_link(page)}', ''])
        for finding in findings:
            lines.extend(finding_lines(finding))
        lines.append('')


def gap_evidence(entry):
    attribution = entry['attribution']
    event = attribution['source_changed_at']
    event_repository = 'site' if attribution['category'] == 'docs_removed' else attribution['repository']
    pull_request = f" (PR #{event['pr']})" if event.get('pr') else ''
    verb = 'removed from docs by' if attribution['category'] == 'docs_removed' else 'introduced by'
    definition = entry['definition']
    return (
        f"{category_name(attribution['category'])}: {verb} {commit_link(event_repository, event)}{pull_request}; "
        f"definition: {blob_link(definition['repository'], definition)}"
    )


def append_upstream_guards(lines, report):
    lines.extend(['## Upstream guards', ''])
    lines.extend(['Totals across audit findings and coverage gaps: ' + '; '.join(
        f'{category_name(category)}: {count}' for category, count in report['attribution_totals'].items()
    ) + '.', ''])
    lines.extend(['Changelog gaps outside these totals and every guard: ' + '; '.join(
        f"{CHANGELOG_GAP_BUCKET_LABELS[bucket]}: " + (', '.join(f'{category_name(category)} {count}' for category, count in counts.items()) or 'none')
        for bucket, counts in report['changelog_gaps_outside_totals'].items()
    ) + '.', ''])
    for guard in report['upstream_guards']:
        lines.extend([f"### {guard['name']}", '', f"- Check: `{guard['command']}`"])
        for category, counts in guard['by_category'].items():
            lines.append(
                f"- {category_name(category)}: {counts['caught']} of {counts['total']} caught "
                f"({counts['audit_findings']} audit findings, {counts['coverage_gaps']} coverage gaps)."
            )
            for reason, count in counts['missed_reasons'].items():
                lines.append(f'  - {count} not caught: {reason}.')
        examples = ', '.join(guard['example_pages']) or 'none'
        lines.extend([
            f"- {guard['caught_at_pr_time']} of {guard['total']} would have been caught at PR time.",
            f'- Example pages: {examples}.', '',
        ])
    lines.extend(['### Outside every gate', ''])
    if not report['outside_every_gate']:
        lines.extend(['None.', ''])
        return
    for entry in report['outside_every_gate']:
        lines.append(f"- {category_name(entry['category'])}, {entry['kind']}: {entry['count']}; no PR gate checks this category for this kind.")
    lines.append('')


def format_markdown(report):
    totals = ', '.join(f'{kind}={count}' for kind, count in report['totals_by_kind'].items()) or 'none'
    header = f"# Next steps — {report['run_date']} — {report['total_findings']} findings ({totals})"
    if 'delta' in report:
        delta = report['delta']
        header += f" — delta: {delta['new']} new, {delta['resolved']} resolved, {delta['still_open']} still open"
    lines = [header, '', f"Audited posthog.com revision: {report['site_revision']}.", '', '## Fix now', '']
    append_page_findings(lines, report['fix_now'])
    lines.extend(['## Candidate fixes (check before applying)', ''])
    append_page_findings(lines, report['candidate_fixes'])
    lines.extend(['## Unpublished snippets (fix optional)', '', 'No published docs page imports these snippets, so readers never see them.', ''])
    append_page_findings(lines, report['unpublished_snippets'])
    lines.extend(['## Needs judgment', ''])
    if not report['needs_judgment']:
        lines.extend(['None.', ''])
    for kind, findings_by_page in report['needs_judgment'].items():
        lines.extend([f'### {kind}', ''])
        append_page_findings(lines, findings_by_page, heading_level=4)
    if 'delta' in report:
        lines.extend(['## Resolved since last run', ''])
        append_page_findings(lines, report['resolved_since_last_run'])
        lines.extend(['## New since last run', ''])
        append_page_findings(lines, report['new_since_last_run'])
    append_upstream_guards(lines, report)
    lines.extend(['## Coverage gaps', '', CHANGELOG_RULE, ''])
    for source, gaps in report['coverage_gaps'].items():
        lines.extend([f'### {source}', ''])
        if not gaps['undocumented'] and not gaps['partially_documented']:
            lines.extend(['None.', ''])
            continue
        if source.endswith('changelog'):
            append_changelog_gaps(lines, gaps)
            continue
        for entry in gaps['undocumented']:
            section = f" (section: {entry.get('section') or entry['name']})" if source.startswith('settings') else ''
            identifiers = ', '.join(entry.get('identifiers', []))
            missing = f' | missing: {identifiers}' if identifiers else ''
            lines.append(f"- {entry['name']}{section}{missing} | {gap_evidence(entry)}")
        lines.append('')
        if gaps['partially_documented']:
            lines.extend(['#### Partially documented', ''])
        for entry in gaps['partially_documented']:
            lines.append(f"- {entry['name']} | missing: {', '.join(entry['missing_identifiers'])} | {gap_evidence(entry)}")
        if gaps['partially_documented']:
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


CHANGELOG_RULE = (
    'Changelog gaps: Major, Minor and Patch entries are all inventoried, one gap per PR (JS) or commit (Python) with every package it '
    'shipped in. Only the public API bucket counts as an undocumented feature; the other buckets are listed so nothing is hidden.'
)


def changelog_gap_bucket(entry):
    if 'gap_bucket' not in entry:
        raise ValueError(f"Changelog gap {entry['name']!r} has no gap_bucket; regenerate coverage.json with doccoverage.py")
    return entry['gap_bucket']


def append_changelog_gaps(lines, gaps):
    entries = [*gaps['undocumented'], *gaps['partially_documented']]
    for bucket in CHANGELOG_GAP_BUCKETS:
        bucket_entries = [entry for entry in entries if changelog_gap_bucket(entry) == bucket]
        lines.extend([f'#### {CHANGELOG_GAP_BUCKET_LABELS[bucket]}: {len(bucket_entries)}', ''])
        for entry in bucket_entries:
            missing = ', '.join(entry.get('missing_identifiers') or undocumented_identifiers(entry) or entry.get('identifiers', []))
            packages = ', '.join(entry.get('packages', [entry.get('package', '')]))
            lines.append(f"- {entry['name']} | packages: {packages} | {entry['status']} | missing: {missing} | {gap_evidence(entry)}")
        lines.append('')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--previous', type=Path)
    arguments = parser.parse_args()
    report = build_report(OUTPUT_ROOT, arguments.previous)
    (OUTPUT_ROOT / 'next-steps.md').write_text(format_markdown(report), encoding='utf-8')
    (OUTPUT_ROOT / 'next-steps.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
