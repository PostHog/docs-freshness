import contextlib
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import impact
import proof
from impact_test import commit_all, monorepo_fixture, run_git, write_file

PAGE = 'synthetic-proof/guide.mdx'
BASE_PAGE = '''---
title: Guide
---
import Tip from './tip.mdx'

## Save

Click **Save insight** to keep it.

<ProductScreenshot
  imageLight="https://res.cloudinary.com/dmukukwp6/image/upload/save_light.png"
  alt="Save button"
/>

It stays in the list.

---

Insights that use `{filters}` under `<head>` stay editable.

{
  someCall()
}

<!--
Reviewer note.
-->

## Other

Some other prose. See [the old setup](/docs/synthetic-proof/absent).
'''
FIXED_PAGE = BASE_PAGE.replace('Click **Save insight**', 'Click **Store insight**')
DOCS_PR_CASES = (
    ('fixed-claim', FIXED_PAGE, {}, None),
    ('edit-outside-section', FIXED_PAGE.replace('Some other prose.', 'Some rewritten prose.'), {}, 'outside the sections holding the triggering claims'),
    ('jsx-in-section', FIXED_PAGE.replace('to keep it.\n', 'to keep it.\n\n<Tip />\n'), {}, 'changes a non-prose line'),
    ('jsx-attribute-edited', FIXED_PAGE.replace('save_light.png', 'other_light.png'), {}, 'changes a non-prose line'),
    ('jsx-attribute-inserted', FIXED_PAGE.replace('  alt="Save button"\n', '  alt="Save button"\n  href="javascript:alert(1)"\n'), {}, 'changes a non-prose line'),
    ('setext-heading-inserted', FIXED_PAGE.replace('to keep it.\n', 'to keep it.\n\nNew heading\n===========\n'), {}, 'changes a non-prose line'),
    ('thematic-break-inserted', FIXED_PAGE.replace('to keep it.\n', 'to keep it.\n\n* * *\n'), {}, 'changes a non-prose line'),
    ('setext-heading-by-deleted-blank-line', FIXED_PAGE.replace('in the list.\n\n---', 'in the list.\n---'), {}, 'edit changes whether base line 15 is prose'),
    ('javascript-link', FIXED_PAGE.replace('to keep it.', 'to keep it. [confirm](javascript&#58;alert(1))'), {}, 'link destination javascript&#58;alert(1 is not in the docs base'),
    ('foreign-link', FIXED_PAGE.replace('to keep it.', 'to keep it. [PostHog login](https://posthog-login.example.com)'), {}, 'link destination https://posthog-login.example.com is not in the docs base'),
    ('foreign-image', FIXED_PAGE.replace('to keep it.', 'to keep it. ![](https://evil.example/pixel.gif)'), {}, 'link destination https://evil.example/pixel.gif is not in the docs base'),
    ('foreign-reference-definition', FIXED_PAGE.replace('to keep it.', 'to keep it. [login][x]\n\n[x]: https://posthog-login.example.com'), {}, 'link destination https://posthog-login.example.com is not in the docs base'),
    ('docs-links-added', FIXED_PAGE.replace('to keep it.', 'to keep it. See [the guide](/docs/synthetic-proof/guide) and [the site](https://posthog.com/docs/synthetic-proof/guide).'), {}, None),
    ('expression-interior-edited', FIXED_PAGE.replace('  someCall()', '  otherCall()'), {}, 'changes a non-prose line'),
    ('comment-interior-edited', FIXED_PAGE.replace('Reviewer note.', 'Hidden reviewer note.'), {}, 'changes a non-prose line'),
    ('foreign-bare-url', FIXED_PAGE.replace('to keep it.', 'to keep it. Log in at https://posthog-login.example.com today.'), {}, 'link destination https://posthog-login.example.com is not in the docs base'),
    ('posthog-bare-url-added', FIXED_PAGE.replace('to keep it.', 'to keep it. Read https://posthog.com/docs/synthetic-proof/guide.'), {}, None),
    ('failing-link-copied', FIXED_PAGE.replace('to keep it.', 'to keep it. See [the old setup](/docs/synthetic-proof/absent).'), {}, 'new copy of failing docs-link claim /docs/synthetic-proof/absent'),
    ('inline-code-line-edited', FIXED_PAGE.replace('stay editable.', 'remain editable.'), {}, None),
    ('claim-deleted', BASE_PAGE.replace('Click **Save insight** to keep it.\n', ''), {}, 'has no passing ui-label claim left in its section'),
    ('new-broken-link', FIXED_PAGE.replace('to keep it.', 'to keep it. See [setup](/docs/synthetic-proof/missing).'), {}, 'New finding: This PR introduces failing docs-link'),
    ('unrelated-file', FIXED_PAGE, {'src/pages/other.tsx': 'export default () => null\n'}, 'src/pages/other.tsx is not a docs page holding a triggering claim'),
)


def docs_pr_body(source_sha):
    return (
        'Fixes the docs for the renamed button.\n'
        f'<!-- docs-freshness-source: PostHog/posthog#123 {source_sha} -->\n'
        f'<!-- docs-freshness-claim: {{"page": "{PAGE}", "kind": "ui-label", "claim": "Save insight"}} -->\n'
    )


def run_proof_main(base, head, body_file):
    output = io.StringIO()
    with patch('sys.argv', ['proof.py', '--base', base, '--head', head, '--body-file', str(body_file)]), contextlib.redirect_stdout(output):
        try:
            proof.main()
        except SystemExit as exit_status:
            return exit_status.code, output.getvalue()
    raise AssertionError('proof.main must exit with a status')


def main():
    with tempfile.TemporaryDirectory(prefix='proof-test-') as directory:
        root = Path(directory)
        monorepo, site = root / 'monorepo', root / 'site'
        monorepo_fixture(monorepo)
        write_file(monorepo, 'frontend/src/scenes/widget.tsx', 'export const Widget = () => <button>Store insight</button>\n')
        site.mkdir()
        run_git(monorepo, 'init', '-q', '-b', 'master')
        source_sha = commit_all(monorepo, 'Rename Save insight to Store insight')
        run_git(site, 'init', '-q', '-b', 'master')
        write_file(site, f'contents/docs/{PAGE}', BASE_PAGE)
        write_file(site, 'contents/docs/synthetic-proof/tip.mdx', 'A tip.\n')
        write_file(site, 'vercel.json', '{"redirects": []}\n')
        base = commit_all(site, 'Base docs')
        body_file = root / 'body.md'
        body_file.write_text(docs_pr_body(source_sha))
        with patch.dict(impact.REPOSITORIES, {'site': site, 'monorepo': monorepo}), patch.object(impact, 'load_references', return_value=[]):
            for case, page_text, extra_files, expected_failure in DOCS_PR_CASES:
                run_git(site, 'checkout', '-q', '-b', case, base)
                write_file(site, f'contents/docs/{PAGE}', page_text)
                for path, text in extra_files.items():
                    write_file(site, path, text)
                head = commit_all(site, case)
                exit_code, output = run_proof_main(base, head, body_file)
                if expected_failure is None:
                    assert exit_code == 0 and f'docs-proof passed on {head} for PostHog/posthog#123 at {source_sha}' in output, (case, output)
                    continue
                assert exit_code == 1 and expected_failure in output, (case, output)
            report = proof.prove(base, head, docs_pr_body('0' * 40), references=[])
            assert not report['passed'] and 'not the source head' in report['failures'][0], report
    print('PASS docs-proof passes a prose-only fix of the triggering claim and fails edits outside its section, JSX, MDX expressions, comments, setext headings, thematic breaks, foreign links and URLs, a copied failing link, a deleted claim, a new finding, an unrelated file and a stale source head')


if __name__ == '__main__':
    main()
