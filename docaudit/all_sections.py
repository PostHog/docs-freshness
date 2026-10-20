import collections, sys
from doccoverage import github_blob_url, run_at_background_priority
run_at_background_priority()
sys.argv = ['docaudit.py', 'x']
exec(compile(open('docaudit.py').read().replace('\nmain()\n', '\n'), 'docaudit.py', 'exec'))
context = build_context()
docs_root = SITE / 'contents/docs'
site_revision = run(['git', 'rev-parse', 'HEAD'], SITE).strip()
uncommitted_docs = run(['git', 'status', '--porcelain', '--', 'contents'], SITE).strip()
if uncommitted_docs:
    raise SystemExit(f'Refusing to audit: {SITE}/contents has uncommitted changes, so line links pinned to {site_revision} would lie')
locations = []
OUTPUT_ROOT.mkdir(exist_ok=True)
findings_candidate = OUTPUT_ROOT / 'all-findings.tmp'
failures = open(findings_candidate, 'w')
rows = []
references_by_claim = {}
pages_by_section = {
    '(top-level)': sorted(p for p in docs_root.glob('*.md*') if p.is_file()),
    **{
        section_dir.name: sorted(section_dir.rglob('*.md*'))
        for section_dir in sorted(p for p in docs_root.iterdir() if p.is_dir() and not p.name.startswith('_'))
    },
}
for section_name, pages in pages_by_section.items():
    checked, failed = collections.Counter(), collections.Counter()
    for page in pages:
        page_text = page.read_text(errors='ignore')
        page_path = str(page.relative_to(docs_root))
        audit = audit_page(page_text, context, page_path)
        checked.update(audit.checked_by_kind)
        for reference in audit.references:
            key = reference['kind'], reference['claim']
            aggregate = references_by_claim.setdefault(key, {**reference, 'pages': set(), 'sources': []})
            aggregate['pages'].add(str(page.relative_to(docs_root)))
            for source in reference['sources']:
                if source not in aggregate['sources']:
                    aggregate['sources'].append(source)
        for (kind, detail, suggestion), claim in zip(audit.findings, audit.failed_claims):
            failed[kind] += 1
            location = claim_location(page_path, page_text, kind, claim)
            source_urls = [github_blob_url('site', site_revision, f'contents/docs/{page_path}', line) for line in location['lines']]
            locations.append({
                'page': page_path, 'kind': kind, 'detail': detail, 'claim': claim,
                'source_url': source_urls[0], 'source_urls': source_urls, **location,
            })
            failures.write(f"{page_path}\t{kind}\t{detail}\t{suggestion!s}\t{suggestion.confidence}\t{location['line']}\n")
    rows.append((section_name, len(pages), sum(checked.values()), sum(failed.values()), dict(failed)))
failures.close()
references = []
for key in sorted(references_by_claim):
    reference = references_by_claim[key]
    reference['pages'] = sorted(reference['pages'])
    reference['sources'] = sorted(reference['sources'], key=lambda source: (source['repo'], source['path']))
    if reference['kind'] == 'ui-label':
        reference['sources'] = reference['sources'][:5]
    references.append(reference)
locations_candidate = OUTPUT_ROOT / 'finding-locations.tmp'
locations_candidate.write_text(json.dumps({'site_revision': site_revision, 'findings': locations}, indent=2) + '\n')
references_candidate = OUTPUT_ROOT / 'doc-references.tmp'
references_candidate.write_text(json.dumps(references, indent=2) + '\n')
findings_candidate.replace(OUTPUT_ROOT / 'all-findings.txt')
references_candidate.replace(OUTPUT_ROOT / 'doc-references.json')
locations_candidate.replace(OUTPUT_ROOT / 'finding-locations.json')
rows.sort(key=lambda r: -(r[3] / r[2]) if r[2] else 0)
print(f"{'section':24}{'pages':>6}{'checked':>8}{'failed':>7}{'fail%':>7}  by kind")
for name, pages, checked_count, failed_count, by_kind in rows:
    rate = 100 * failed_count / checked_count if checked_count else 0
    print(f'{name:24}{pages:6}{checked_count:8}{failed_count:7}{rate:7.1f}  {by_kind or ""}')
