import sys
import tempfile
from unittest.mock import patch
sys.argv = ['docaudit.py', 'feature-flags']
source = open('docaudit.py').read().replace('\nmain()\n', '\n')
exec(compile(source, 'docaudit.py', 'exec'))
context = build_context()
page = '''
```js
// src/lib/posthog.ts and posthog.d.ts
import { PostHogService } from './posthog.service'
posthog.getFeatureFlag('x')
window.posthog.LIB_VERSION
posthog.version
posthog.getFeatureFlagzzz('x')
posthog.reset()
```
```python
from posthog.mcp import instrument
from posthog.ai.openai import OpenAI
client = posthog.ai.openai.OpenAI()  # see posthog.utils
posthog.feature_enabled('x', 'y')
posthog.capture_tool_call(tool_name='search')
await posthog.capture_immediate('event')
posthog.feature_enabledzzz('x', 'y')
posthog.get_feature_flag('x', 'y')
posthog.capture('event')
posthog.reset()
posthog.register({'a': 1})
```
```node
posthog.isFeatureEnabled('x', 'y')
posthog.startSessionRecording()
```
~~~js
posthog.tildeFencedMethodzzz()
~~~
``` js
posthog.spacedFenceMethodzzz()
```
~~~~python
posthog.capture('event')
~~~~
Go to [flags](https://us.posthog.com/feature_flags) and [bogus](https://us.posthog.com/frobnicator).
Open [settings](https://us.posthog.com/settings), [project settings](https://us.posthog.com/project/2/settings/) and [events](https://us.posthog.com/events).
Open [flag](https://us.posthog.com/project/1/feature_flags/<id>) and [new insight](https://us.posthog.com/project/<id>/insights/new).
Avoid [bad edit](https://us.posthog.com/project/1/feature_flags/does-not-exist/edit).
See [variables](https://us.posthog.com/project/123/settings/project#variables) and [bogus anchor](https://us.posthog.com/project/123/settings/project#bogus-anchor-zz).
Click **Release conditions**, then click **Frobnicate the widget**.
Click **Test all N distinct IDs**, click **Restore feature flag**, click **Rollout percentag**, click **Edit flag key**.
See [ok](/docs/feature-flags/testing), [redirected](/docs/integrate/server/python), [bad](/docs/feature-flags/does-not-exist).
See [outside ok](/docs/data/cookieless-tracking) and [outside bad](/docs/tutorials/does-not-exist).
See [absolute ok](https://posthog.com/docs/feature-flags/bootstrapping) and [absolute bad](https://posthog.com/docs/feature-flags/absolute-bad).
See [reference ok][ref-ok] and [reference bad][ref-bad].
<a href="/docs/feature-flags/local-evaluation">href ok</a> and <a href="/docs/feature-flags/href-bad">href bad</a>.
See [raw markdown](/docs/feature-flags/testing.md) and [bogus extension](/docs/feature-flags/testing.bogus).
See [punctuation redirect](/docs/session-replay/pricingXmd).
See [catch-all root](/docs/product-tours) and [catch-all fallback](/docs/product-tours/does-not-exist).
See [good heading](/docs/feature-flags/creating-feature-flags#release-conditions) and [bad heading](/docs/feature-flags/creating-feature-flags#no-such-heading).
See [redirect fragment](/docs/error-tracking/fix-with-ai-prompts).
See [step anchor](/docs/slack/setup#pick-a-project), [quest anchor](/docs/revenue-analytics/start-here#quest-item-choose-your-reporting-currency) and [generated anchor](/docs/error-tracking/installation/react#set-up-error-boundaries).
See [snippet anchor](/docs/product-analytics/group-analytics#how-to-set-group-properties).

## Local heading

See [local](#local-heading) and [local bad](#no-local-heading).
Open [auth](https://us.posthog.com/auth.md), [embed](https://us.posthog.com/embedded/abc), [shared](https://us.posthog.com/shared/abc) and [token](https://us.posthog.com/oauth/token/).
An iframe example is `https://app.posthog.com/iframe`.
Open [bad settings](https://us.posthog.com/settings/access-control), [environment access](https://us.posthog.com/settings/environment-access-control), [web analytics](https://us.posthog.com/settings/project-web-analytics) and [project](https://us.posthog.com/settings/project).
Enable the [feature preview](https://us.posthog.com/settings/user-feature-previews#cdp-hog-sources).
See [bogus replay setting](https://us.posthog.com/settings/project#session-replay-bogus), [dashboard setting](https://us.posthog.com/settings/project#dashboard) and [default release conditions](https://us.posthog.com/settings/environment-feature-flags#feature-flag-default-release-conditions).
Click **Create & Enable**, select **Property value median**, click **Add person property**, choose **Send and set pending**, click **Filter by response**, open **CSP Violation Reports**.
Click **+ Add filter**, click **Release conditions tab** and click the **pencil icon**.
In Okta admin, click **Frobnicate Okta widget**.

[ref-ok]: /docs/feature-flags/cutting-costs
[ref-bad]: /docs/feature-flags/reference-bad
'''
expected_findings = {
    ('sdk-method', 'js: posthog.getFeatureFlagzzz'),
    ('sdk-method', 'python: posthog.feature_enabledzzz'),
    ('sdk-method', 'python: posthog.reset'),
    ('sdk-method', 'python: posthog.register'),
    ('sdk-method', 'node: posthog.startSessionRecording'),
    ('sdk-method', 'js: posthog.tildeFencedMethodzzz'),
    ('sdk-method', 'js: posthog.spacedFenceMethodzzz'),
    ('app-url', 'frobnicator'),
    ('app-url', 'project/1/feature_flags/does-not-exist/edit'),
    ('settings-anchor', 'settings/project#bogus-anchor-zz'),
    ('settings-anchor', 'settings/project#session-replay-bogus'),
    ('settings-anchor', 'settings/project#dashboard'),
    ('ui-label', 'Frobnicate the widget'),
    ('sdk-method', 'js: posthog.LIB_VERSION'),
    ('docs-fragment', '/docs/product-analytics/group-analytics#how-to-set-group-properties (/docs/product-analytics/group-analytics)'),
    ('docs-fragment', '#no-local-heading (same page)'),
    ('app-url', 'settings/access-control'),
    ('ui-label-minor', 'Rollout percentag (UI: rollout percentage)'),
    ('ui-label', 'Edit flag key'),
    ('docs-link', '/docs/feature-flags/does-not-exist (missing: /docs/feature-flags/does-not-exist)'),
    ('docs-link', '/docs/tutorials/does-not-exist (missing: /tutorials/does-not-exist)'),
    ('docs-link', '/docs/feature-flags/absolute-bad (missing: /docs/feature-flags/absolute-bad)'),
    ('docs-link', '/docs/feature-flags/reference-bad (missing: /docs/feature-flags/reference-bad)'),
    ('docs-link', '/docs/feature-flags/href-bad (missing: /docs/feature-flags/href-bad)'),
    ('docs-link', '/docs/feature-flags/testing.bogus (missing: /docs/feature-flags/testing.bogus)'),
    ('docs-link', '/docs/session-replay/pricingXmd (missing: /docs/session-replay/pricingXmd)'),
    ('docs-link', '/docs/product-tours/does-not-exist (fallback: /docs)'),
    ('docs-fragment', '/docs/feature-flags/creating-feature-flags#no-such-heading (/docs/feature-flags/creating-feature-flags)'),
}
audit = audit_page(page, context)
print(dict(audit.checked_by_kind))
for finding in audit.findings:
    print(finding)
actual_findings = {(kind, detail) for kind, detail, _ in audit.findings}
missed = expected_findings - actual_findings
unexpected = actual_findings - expected_findings
assert not missed and not unexpected, f'missed={sorted(missed)} unexpected={sorted(unexpected)}'
assert audit.checked_by_kind['docs-fragment'] == 8, audit.checked_by_kind
assert audit.checked_by_kind['app-url'] == 14, audit.checked_by_kind
assert audit.checked_by_kind['settings-anchor'] == 5, audit.checked_by_kind


def real_page_audit(page_path):
    return audit_page((SITE / 'contents' / 'docs' / page_path).read_text(), context, page_path)


def findings_of_kind(page_audit, kind):
    return {detail for finding_kind, detail, _ in page_audit.findings if finding_kind == kind}


proxy_node_audit = real_page_audit('advanced/proxy/node.mdx')
assert proxy_node_audit.checked_by_kind['sdk-method'] >= 1 and not findings_of_kind(proxy_node_audit, 'sdk-method')
assert not findings_of_kind(real_page_audit('libraries/js/index.mdx'), 'sdk-method')
schema_audit = real_page_audit('product-analytics/schema-management.mdx')
assert schema_audit.checked_by_kind['sdk-method'] == 0, schema_audit.checked_by_kind
assert real_page_audit('session-replay/iframes.mdx').checked_by_kind['app-url'] == 0
saml_labels = findings_of_kind(real_page_audit('settings/authentication/saml.mdx'), 'ui-label')
assert 'Add App' not in saml_labels, saml_labels
assert 'Set up 2FA' in findings_of_kind(real_page_audit('settings/account-settings.mdx'), 'ui-label')
assert 'pencil icon' not in findings_of_kind(real_page_audit('feature-flags/scheduled-flag-changes.mdx'), 'ui-label')
assert not findings_of_kind(real_page_audit('feature-flags/troubleshooting.mdx'), 'ui-label')
assert 'CSP Violation Reports' not in findings_of_kind(real_page_audit('csp-tracking/index.mdx'), 'ui-label')
assert '#aggregate-functions (same page)' in findings_of_kind(real_page_audit('data-warehouse/sql/index.mdx'), 'docs-fragment')
creating_flags_audit = real_page_audit('feature-flags/creating-feature-flags.mdx')
assert '#step-5-configure-evaluation-runtime-and-environments-optional (same page)' in findings_of_kind(creating_flags_audit, 'docs-fragment')
assert 'Served value' in findings_of_kind(creating_flags_audit, 'ui-label')
for unconfirmable_label in ('Set up nonexistent widget', 'Set up 2FA'):
    assert files_containing_label(unconfirmable_label, context['ui_label_index'])[0] == 'missing', unconfirmable_label
snippet_audit = real_page_audit('cdp/_snippets/source-setup-intro.mdx')
assert snippet_audit.checked_by_kind['docs-fragment'] == 1 and not findings_of_kind(snippet_audit, 'docs-fragment')
for server_page in ('integrate/_snippets/nextjs/app-router-server-setup.mdx', 'libraries/fastify.mdx'):
    server_audit = real_page_audit(server_page)
    assert server_audit.checked_by_kind['sdk-method'] >= 1 and not findings_of_kind(server_audit, 'sdk-method'), server_page
saml_audit = real_page_audit('settings/authentication/saml.mdx')
assert saml_audit.checked_by_kind['settings-anchor'] == 1 and not findings_of_kind(saml_audit, 'settings-anchor')
assert 'settings/project#project-delete' in findings_of_kind(real_page_audit('settings/account-settings.mdx'), 'settings-anchor')
assert 'Add to "Saved filters"' in findings_of_kind(real_page_audit('session-replay/how-to-watch-recordings.mdx'), 'ui-label')
skills_prose = strip_code((SITE / 'contents/docs/skills/index.mdx').read_text())
assert 'Skills' in ui_label_claims(skills_prose, 'skills/index.mdx')
assert 'Skills' not in findings_of_kind(real_page_audit('skills/index.mdx'), 'ui-label')
assert 'settings/project#person-display-name' in findings_of_kind(real_page_audit('product-analytics/person-properties.mdx'), 'settings-anchor')
assert 'settings/project#base-currency' in findings_of_kind(real_page_audit('web-analytics/marketing-analytics.mdx'), 'settings-anchor')
assert 'settings/user-api-keys#variables' in findings_of_kind(real_page_audit('error-tracking/_snippets/cli/authenticate.mdx'), 'settings-anchor')
assert 'variables' in settings_anchor_ids_in_scope('project', context['settings_anchor_scope'])
assert real_page_audit('vscode-extension/index.mdx').checked_by_kind['ui-label'] == 0
vendor_audit = audit_page('In Retool, click **Frobnicate Retool widget**.\n', context, 'cdp/example.mdx')
assert vendor_audit.checked_by_kind['ui-label'] == 0, vendor_audit.checked_by_kind
assert 'Test Crash' not in findings_of_kind(real_page_audit('error-tracking/upload-source-maps/ios.mdx'), 'ui-label')
anatomy_prose = strip_code((SITE / 'contents/docs/self-driving/anatomy-of-a-pr.mdx').read_text())
assert 'Confirm order' not in ui_label_claims(anatomy_prose, 'self-driving/anatomy-of-a-pr.mdx')
icon_prefix_audit = audit_page('Click **@ Add context** to attach data.\n', context, 'posthog-ai/example.mdx')
assert icon_prefix_audit.checked_by_kind['ui-label'] == 1 and not icon_prefix_audit.findings, icon_prefix_audit.findings
assert 'Follow window' not in findings_of_kind(real_page_audit('session-replay/how-to-watch-recordings.mdx'), 'ui-label')
assert 'LLM-as-a-judge (UI: llm as a judge)' in findings_of_kind(real_page_audit('ai-evals/index.mdx'), 'ui-label-minor')
cdp_minor = findings_of_kind(real_page_audit('advanced/cdp.md'), 'ui-label-minor')
assert any(detail.startswith("Can't find what your looking for") for detail in cdp_minor), cdp_minor
billing_audit = audit_page('Click **Set billing limit** to cap spend.\n', context, 'billing/example.mdx')
assert [kind for kind, _, _ in billing_audit.findings] == ['ui-label-minor'], billing_audit.findings
pinned_fence_page = "```js\nimport posthog from 'posthog-js'\nposthog.init('token')\n```\n```node\nposthog.startSessionRecording()\n```\n"
pinned_fence_audit = audit_page(pinned_fence_page, context, 'feature-flags/example.mdx')
assert 'node: posthog.startSessionRecording' in findings_of_kind(pinned_fence_audit, 'sdk-method'), pinned_fence_audit.findings
assert 'react-native: posthog.get_session_id' in findings_of_kind(real_page_audit('logs/link-session-replay.mdx'), 'sdk-method')
override_page_text = (SITE / 'contents/docs/feature-flags/property-overrides.mdx').read_text()
override_page_imports = [sdk for _, body in fenced_blocks(override_page_text) for sdk in fence_import_sdks(body)]
assert 'js-browser' not in sdks_for_fence('react-native', '', 'feature-flags/property-overrides.mdx', override_page_imports)
assert not findings_of_kind(real_page_audit('distributed-tracing/link-ai-observability.mdx'), 'sdk-method')
for code_label_page, code_label in (('prompt-management/index.mdx', 'New prompt'), ('product-analytics/sharing.mdx', 'Add'), ('posthog-desktop/worktrees.mdx', 'Worktree')):
    code_label_text = (SITE / 'contents/docs' / code_label_page).read_text()
    assert code_label in ui_label_claims(strip_code(code_label_text), code_label_page), code_label
    page_code_text = '\n'.join(body for _, body in fenced_blocks(code_label_text)).casefold()
    assert code_label.casefold() in page_code_text and not label_appears_in_code(code_label, page_code_text), code_label
assert label_appears_in_code('Test Crash', 'button("test crash") {')
vendor_screen_claims = (
    ('cdp/batch-exports/databricks.mdx', 'SQL Warehouses'), ('cdp/batch-exports/databricks.mdx', 'Generate secret'),
    ('settings/authentication/scim.mdx', 'Assign to People'), ('self-host/configure/email.md', 'Restricted Key'),
    ('workflows/import-customerio-optouts.mdx', 'Choose attributes'), ('cdp/batch-exports/azureblob.md', 'Security + networking'),
    ('cdp/batch-exports/bigquery.mdx', 'Grant access'),
)
for vendor_page, vendor_label in vendor_screen_claims:
    vendor_prose = strip_code((SITE / 'contents/docs' / vendor_page).read_text())
    assert vendor_label not in ui_label_claims(vendor_prose, vendor_page), (vendor_page, vendor_label)
databricks_prose = strip_code((SITE / 'contents/docs/cdp/batch-exports/databricks.mdx').read_text())
assert 'New destination' in ui_label_claims(databricks_prose, 'cdp/batch-exports/databricks.mdx')
join_prose = strip_code((SITE / 'contents/docs/data-warehouse/join.mdx').read_text())
assert 'View table schema' in ui_label_claims(join_prose, 'data-warehouse/join.mdx')
assert files_containing_label('Custom transformation', context['ui_label_index'])[0] == 'exact'
assert 'Custom transformation' not in findings_of_kind(real_page_audit('cdp/transformations/customizing-transformations.md'), 'ui-label')
assert 'Logs & metrics' in findings_of_kind(real_page_audit('cdp/troubleshooting.md'), 'ui-label')
assert near_miss_ui_label('Logs & metrics', context['ui_label_index']) is None
data_access_labels = findings_of_kind(real_page_audit('data-warehouse/sql/data-access.mdx'), 'ui-label')
assert any(detail.startswith('Edit SQL directly (tooltip only: edit sql directly;') for detail in data_access_labels), data_access_labels
trends_prose = strip_code((SITE / 'contents/docs/product-analytics/trends/overview.mdx').read_text())
assert {'Add graph series', 'New insight'} <= set(ui_label_claims(trends_prose, 'product-analytics/trends/overview.mdx'))

generic_fence_pages = {
    'libraries/node/index.mdx': '```ts\nposthog.withSpan(() => {})\n```\n',
    'libraries/next-js/_snippets/server-flags.mdx': '```tsx\nposthog.getAllFlags(distinctId)\n```\n',
    'libraries/react-native/index.mdx': '```js\nposthog.optIn()\nposthog.optedOut\n```\n',
}
for generic_page_path, generic_page in generic_fence_pages.items():
    generic_audit = audit_page(generic_page, context, generic_page_path)
    assert generic_audit.checked_by_kind['sdk-method'] >= 1 and not generic_audit.findings, (generic_page_path, generic_audit.findings)
strict_node_audit = audit_page('```ts\nposthog.startSessionRecording()\n```\n', context, 'libraries/node/index.mdx')
assert findings_of_kind(strict_node_audit, 'sdk-method') == {'ts: posthog.startSessionRecording'}, strict_node_audit.findings

optional_param_pattern, _ = vercel_pattern_to_regex('/docs/old/:slug?')
optional_redirect = [Redirect(optional_param_pattern, '/docs/new/:slug?', ())]
assert apply_redirect('/docs/old', optional_redirect).destination == '/docs/new/'
assert apply_redirect('/docs/old/page', optional_redirect).destination == '/docs/new/page'
prefix_sharing_pattern, _ = vercel_pattern_to_regex('/docs/:identity/:id')
prefix_sharing_redirect = [Redirect(prefix_sharing_pattern, '/docs/:id/:identity', ())]
assert apply_redirect('/docs/b/a', prefix_sharing_redirect).destination == '/docs/a/b'


fragment_typo_audit = audit_page('## Local heading\n\nSee [typo](#local-headin).\n', context)
assert fragment_typo_audit.findings == [('docs-fragment', '#local-headin (same page)', '#local-heading')]
cross_page_typo_audit = audit_page('See [typo](/docs/feature-flags/creating-feature-flags#release-condition).', context)
assert cross_page_typo_audit.findings == [
    ('docs-fragment', '/docs/feature-flags/creating-feature-flags#release-condition (/docs/feature-flags/creating-feature-flags)', '#release-conditions')
], cross_page_typo_audit.findings
wrong_section_audit = audit_page('See [settings](https://us.posthog.com/settings/user-api-keys#variables).', context)
assert wrong_section_audit.findings == [('settings-anchor', 'settings/user-api-keys#variables', '#personal-api-keys')], wrong_section_audit.findings
assert next(suggestion for kind, detail, suggestion in audit.findings if kind == 'ui-label-minor' and detail.startswith('Rollout percentag')) == 'Rollout percentage'
assert next(suggestion for kind, detail, suggestion in audit.findings if detail == 'Frobnicate the widget') == ''
assert next(suggestion for kind, detail, suggestion in audit.findings if detail == 'js: posthog.getFeatureFlagzzz') == 'getFeatureFlag'
settings_typo_audit = audit_page('See [settings](https://us.posthog.com/settings/project#variable).', context)
assert settings_typo_audit.findings == [('settings-anchor', 'settings/project#variable', '#variables')], settings_typo_audit.findings
app_typo_audit = audit_page('Open [flags](https://us.posthog.com/feature_flag).', context)
assert app_typo_audit.findings == [('app-url', 'feature_flag', 'feature-flags')], app_typo_audit.findings
link_typo_audit = audit_page('See [testing](/docs/feature-flags/testin).', context)
assert link_typo_audit.findings == [
    ('docs-link', '/docs/feature-flags/testin (missing: /docs/feature-flags/testin)', '/docs/feature-flags/testing')
], link_typo_audit.findings
assert graded_suggestion('zzzzzzzzzz', {'anchor'}) == ''


def real_page_suggestions(page_path, kind):
    return {detail: suggestion for finding_kind, detail, suggestion in real_page_audit(page_path).findings if finding_kind == kind}


def confidence_of(suggestion):
    return suggestion.confidence


assert f"{Suggestion('#x', 'exact')}" == '#x' and f'{NO_SUGGESTION}' == ''
assert Suggestion('#x', 'exact').confidence == 'exact' and NO_SUGGESTION.confidence == 'none'
assert [confidence_of(suggestion) for _, _, suggestion in fragment_typo_audit.findings] == ['candidate']
assert [confidence_of(suggestion) for _, _, suggestion in wrong_section_audit.findings] == ['candidate']
for api_key_page in ('error-tracking/_snippets/cli/authenticate.mdx', 'error-tracking/upload-source-maps/nextjs.mdx'):
    api_key_suggestion = real_page_suggestions(api_key_page, 'settings-anchor')['settings/user-api-keys#variables']
    assert (api_key_suggestion, confidence_of(api_key_suggestion)) == ('#personal-api-keys', 'candidate'), (api_key_page, api_key_suggestion)
level_link_suggestion = real_page_suggestions('settings/account-settings.mdx', 'settings-anchor')['settings/user#notifications']
assert (level_link_suggestion, confidence_of(level_link_suggestion)) == ('settings/user-notifications#notifications', 'exact')
privacy_suggestion = real_page_suggestions('privacy/data-storage.mdx', 'settings-anchor')['settings/project#datacapture']
assert (privacy_suggestion, confidence_of(privacy_suggestion)) == ('settings/project-privacy#datacapture', 'exact'), privacy_suggestion
display_name_suggestion = real_page_suggestions('product-analytics/person-properties.mdx', 'settings-anchor')['settings/project#person-display-name']
assert display_name_suggestion == 'settings/project-product-analytics#person-display-name', display_name_suggestion
assert real_page_suggestions('web-analytics/marketing-analytics.mdx', 'settings-anchor')['settings/project#base-currency'] == 'settings/project-customization#base-currency'
assert real_page_suggestions('settings/projects.mdx', 'settings-anchor')['settings/project#project-move'] == 'settings/environment-danger-zone#project-move'
assert canonical_settings_section('environment-toolbar', context['settings_anchor_scope'].section_aliases) == 'project-web-analytics'
screen_views_suggestion = real_page_suggestions('libraries/kmp/index.mdx', 'docs-fragment')[
    '/docs/product-analytics/capture-events#screen-views (/docs/product-analytics/capture-events)'
]
assert confidence_of(screen_views_suggestion) != 'exact', screen_views_suggestion
capture_events_file = SITE / 'contents/docs/product-analytics/capture-events.mdx'
assert is_ambiguous_anchor('capturing-screen-views', ambiguous_heading_slugs(capture_events_file.read_text(), capture_events_file))
assert is_ambiguous_anchor('capturing-screen-views-1', ambiguous_heading_slugs(capture_events_file.read_text(), capture_events_file))
tabbed_page = '## Outside heading\n\n<Tab.Panel>\n\n## Only in tab\n\n</Tab.Panel>\n\n## Twice here\n\n## Twice here\n'
assert ambiguous_heading_slugs(tabbed_page, None) == {'only-in-tab', 'twice-here'}
duplicate_heading_audit = audit_page(
    tabbed_page + '\nSee [a](#twice-here-optional), [b](#only-in-tab-optional) and [c](#outside-heading-optional).\n', context
)
assert {detail: (suggestion, confidence_of(suggestion)) for _, detail, suggestion in duplicate_heading_audit.findings} == {
    '#twice-here-optional (same page)': ('#twice-here', 'candidate'), '#only-in-tab-optional (same page)': ('#only-in-tab', 'candidate'),
    '#outside-heading-optional (same page)': ('#outside-heading', 'exact'),
}, duplicate_heading_audit.findings
react_component_suggestion = real_page_suggestions('libraries/js/usage.mdx', 'docs-fragment')[
    '/docs/libraries/react#feature-flags-react-component (/docs/libraries/react)'
]
assert confidence_of(react_component_suggestion) != 'exact', react_component_suggestion
react_page_file = SITE / 'contents/docs/libraries/react/index.mdx'
assert 'method-2-using-the-posthogfeature-component' in cached_page_anchor_index(react_page_file.read_text(), react_page_file)[0]
assert confidence_of(graded_suggestion('feature-flags-react-component', ['feature-flags', 'method-2-using-the-posthogfeature-component'])) == 'candidate'
assert confidence_of(graded_suggestion('sequential-testing-beta', ['sequential-testing', 'frequentist-statistics'])) == 'exact'
assert confidence_of(graded_suggestion('setup-client', ['setup', 'client-options'])) == 'candidate'
assert [confidence_of(suggestion) for _, _, suggestion in link_typo_audit.findings] == ['candidate']
wrong_guess_fragments = (
    ('advanced/content-security-policy.md', '/docs/libraries/js#option-1-install-via-snippet (/docs/libraries/js)'),
    ('api/capture.mdx', '/docs/product-analytics/group-analytics#groups-vs-group-types (/docs/product-analytics/group-analytics)'),
    ('libraries/flutter/index.mdx', '#sending-user-information (same page)'),
    ('settings/account-settings.mdx', '#authentication (same page)'),
    ('web-analytics/sending-http-logs.mdx', '/docs/api/capture#batch-events (/docs/api/capture)'),
)
for wrong_guess_page, wrong_guess_detail in wrong_guess_fragments:
    wrong_guess_suggestion = real_page_suggestions(wrong_guess_page, 'docs-fragment')[wrong_guess_detail]
    assert confidence_of(wrong_guess_suggestion) != 'exact', (wrong_guess_page, wrong_guess_suggestion)
    assert wrong_guess_suggestion not in ('#option-2-install-via-package-manager', '#groups-vs-cohorts', '#redacting-information-in-events', '#notifications', '#invalid-events'), wrong_guess_suggestion
renumbered_suggestion = real_page_suggestions('surveys/creating-surveys.mdx', 'docs-fragment')[
    '/docs/surveys/viewing-results#2-creating-your-own-insights (/docs/surveys/viewing-results)'
]
assert (renumbered_suggestion, confidence_of(renumbered_suggestion)) == ('#3-creating-your-own-insights', 'exact')
snake_case_suggestion = real_page_suggestions('logs/link-session-replay.mdx', 'sdk-method')['react-native: posthog.get_session_id']
assert (snake_case_suggestion, confidence_of(snake_case_suggestion)) == ('getSessionId', 'exact')
assert confidence_of(graded_suggestion('step-5-configure-runtime-optional', ['step-5-configure-runtime'])) == 'exact'
assert confidence_of(graded_suggestion('batch-events', ['events', 'invalid-events'])) == 'candidate'

nonsense_docs_links = (
    ('product-analytics/privacy.mdx', '/docs/cdp/property-filter', '/docs/cdp/sources/propertyware'),
    ('mcp-analytics/installation.mdx', '/docs/getting-started/project-token', '/docs/getting-started/next-steps'),
    ('session-replay/find-replays-ai.mdx', '/docs/session-replay/session-summaries', '/docs/session-replay/sharing'),
)
for docs_link_page, docs_link_path, nonsense_suggestion in nonsense_docs_links:
    docs_link_suggestions = {detail.split(' ')[0]: suggestion for detail, suggestion in real_page_suggestions(docs_link_page, 'docs-link').items()}
    assert docs_link_suggestions[docs_link_path] != nonsense_suggestion, (docs_link_page, docs_link_suggestions)
moved_page_suggestions = real_page_suggestions('model-context-protocol/faq.mdx', 'docs-link')
assert moved_page_suggestions['/docs/product-analytics/annotations (missing: /docs/product-analytics/annotations)'] == '/docs/data/annotations'
assert real_page_suggestions('feature-flags/scheduled-flag-changes.mdx', 'docs-link')['/docs/settings (missing: /docs/settings)'] != '/docs/posthog-desktop/settings'

project_access_suggestion = real_page_suggestions('settings/property-access-control.mdx', 'app-url')['settings/project-access']
assert (project_access_suggestion, confidence_of(project_access_suggestion)) == ('settings/project-access-control', 'exact')
assert real_page_suggestions('data/comments.mdx', 'app-url')['data-management/comments'] == ''

save_insight_suggestion = real_page_suggestions('data-warehouse/query.mdx', 'ui-label-minor')['Save insight (UI: save as insight)']
assert (save_insight_suggestion, confidence_of(save_insight_suggestion)) == ('Save as insight', 'candidate')
assert 'Create feature flag' in findings_of_kind(real_page_audit('feature-flags/dependencies.mdx'), 'ui-label')
assert 'Export to JSON' in findings_of_kind(real_page_audit('session-replay/recording-retention.mdx'), 'ui-label')
judge_suggestion = real_page_suggestions('ai-evals/index.mdx', 'ui-label-minor')['LLM-as-a-judge (UI: llm as a judge)']
assert (judge_suggestion, confidence_of(judge_suggestion)) == ('LLM as a judge', 'exact')
assert [str(suggestion) for _, _, suggestion in billing_audit.findings] == ['Set a billing limit']
assert app_route_suggestions([(('feature_flags', ROUTE_PARAM_MARKER), False)]) == ['feature_flags/:id']
with tempfile.TemporaryDirectory(prefix='mutation-') as temporary_directory:
    fixture_site = Path(temporary_directory)
    fixture_docs = fixture_site / 'contents/docs'
    fixture_snippet = fixture_docs / '_snippets/shared.mdx'
    fixture_snippet.parent.mkdir(parents=True)
    fixture_snippet.write_text('See [typo](#target-headin).')
    (fixture_docs / 'a.mdx').write_text("import Shared from './_snippets/shared.mdx'\n\n## Target heading\n\n<Shared />")
    (fixture_docs / 'b.mdx').write_text("import Shared from './_snippets/shared.mdx'\n\n## Completely other\n\n<Shared />")
    importers_by_content_file.cache_clear()
    with patch.object(sys.modules[__name__], 'SITE', fixture_site):
        snippet_typo_audit = audit_page(fixture_snippet.read_text(), context, '_snippets/shared.mdx')
        snippet_claim = snippet_typo_audit.failed_claims[0]
        (fixture_docs / 'c.mdx').write_text("import Shared from './_snippets/shared.mdx'\n\n## Imported only\n")
        importers_by_content_file.cache_clear()
        snippet_location = claim_location('_snippets/shared.mdx', fixture_snippet.read_text(), 'docs-fragment', snippet_claim)
    importers_by_content_file.cache_clear()
    assert snippet_location == {'line': 1, 'lines': [1], 'is_unpublished_snippet': False, 'live_urls': [
        'https://posthog.com/docs/a#target-heading:~:text=typo', 'https://posthog.com/docs/b#completely-other:~:text=typo',
    ]}, snippet_location
    assert snippet_typo_audit.findings == [
        ('docs-fragment', '#target-headin (snippet; missing in: a.mdx, b.mdx)', '#target-heading')
    ], snippet_typo_audit.findings


def fixture_history_output(arguments, cwd, input_text=None):
    assert arguments[:2] == ['git', 'log'], arguments
    if '--no-walk' in arguments:
        assert input_text == 'real\nbulk\n', input_text
        return '@real\ncontents/docs/section/real-edit.md\n@bulk\n' + '\n'.join(
            f'other/file-{file_number}.md' for file_number in range(BULK_COMMIT_FILE_THRESHOLD + 1)
        )
    return '@1700000100 real\ncontents/docs/section/real-edit.md\n@1700000000 bulk\ncontents/docs/section/bulk-only.md\n'


with patch.object(sys.modules[__name__], 'run', fixture_history_output):
    edit_timestamps = last_real_edit_timestamps('contents/docs/section')
assert edit_timestamps == {'contents/docs/section/real-edit.md': 1700000100}, edit_timestamps
located_page = '''---
title: Located
---

## Setup

Open [Settings](https://us.posthog.com/settings/project#variables) then click **Save insight**.

## Setup

```js
posthog.capture('x')
```

See [the guide](/docs/guide?tab=web#first-step) and [here](#setup-1).
'''
assert claim_line_number(located_page, 'located.mdx', 'settings-anchor', 'settings/project#variables') == 7
assert claim_line_number(located_page, 'located.mdx', 'ui-label', 'Save insight') == 7
assert claim_line_number(located_page, 'located.mdx', 'sdk-method', 'js-browser: posthog.capture') == 12
assert claim_line_number(located_page, 'located.mdx', 'docs-fragment', '/docs/guide#first-step') == 15
assert claim_line_number(located_page, 'located.mdx', 'docs-fragment', 'located.mdx#setup-1') == 15
multi_sdk_page = '```js-web\nposthog.get_distinct_id()\n```\n\n```react-native\nposthog.get_distinct_id()\n```\n'
assert claim_line_number(multi_sdk_page, 'product-analytics/identify.mdx', 'sdk-method', 'js-react-native: posthog.get_distinct_id') == 6
assert claim_line_number(multi_sdk_page, 'product-analytics/identify.mdx', 'sdk-method', 'js-browser: posthog.get_distinct_id') == 2
identify_text = (SITE / 'contents/docs/product-analytics/identify.mdx').read_text()
identify_line = claim_line_number(identify_text, 'product-analytics/identify.mdx', 'sdk-method', 'js-react-native: posthog.get_distinct_id')
assert identify_text.splitlines()[identify_line - 2].strip() == '```react-native', identify_line
assert heading_anchor_before_line(located_page, 15) == 'setup-1' and heading_anchor_before_line(located_page, 3) == ''
assert live_spot_urls('located.mdx', located_page, 7, 'settings-anchor', 'settings/project#variables') == [
    'https://posthog.com/docs/located#setup:~:text=Settings'
]
assert live_spot_urls('located.mdx', located_page, 15, 'docs-fragment', '/docs/guide#first-step') == [
    'https://posthog.com/docs/located#setup-1:~:text=the%20guide'
]
try:
    claim_line_number(located_page, 'located.mdx', 'ui-label', 'Never written')
    raise AssertionError('An unlocatable claim must raise rather than point at a guessed line')
except ValueError as failure:
    assert 'Never written' in str(failure)
repeated_claims = (
    ('product-analytics/identify.mdx', 'docs-fragment', 'product-analytics/identify.mdx#person-properties', [601, 638]),
    ('migrate/managed-migrations.mdx', 'docs-fragment', 'migrate/managed-migrations.mdx#direct-imports-mixpanel-amplitude', [36, 141]),
    ('settings/property-access-control.mdx', 'app-url', 'settings/project-access', [48, 63]),
)
for repeated_page, repeated_kind, repeated_claim, expected_lines in repeated_claims:
    repeated_text = (SITE / 'contents/docs' / repeated_page).read_text()
    repeated_location = claim_location(repeated_page, repeated_text, repeated_kind, repeated_claim)
    assert repeated_location['lines'] == expected_lines, (repeated_page, repeated_location['lines'])
    assert repeated_location['line'] == expected_lines[0] and len(repeated_location['live_urls']) >= 1, repeated_location
    repeated_pattern = claim_location_pattern(repeated_kind, repeated_claim)
    repeated_source_lines = strip_code(repeated_text).splitlines()
    assert all(repeated_pattern.search(repeated_source_lines[line - 1]) for line in expected_lines), repeated_page
assert is_unpublished_snippet('error-tracking/_snippets/nextjs-upload-source-maps.mdx')
assert not is_unpublished_snippet('error-tracking/_snippets/cli/authenticate.mdx')
assert not is_unpublished_snippet('error-tracking/upload-source-maps/nextjs.mdx')
unpublished_text = (SITE / 'contents/docs/error-tracking/_snippets/nextjs-upload-source-maps.mdx').read_text()
unpublished_location = claim_location(
    'error-tracking/_snippets/nextjs-upload-source-maps.mdx', unpublished_text, 'settings-anchor', 'settings/user-api-keys#variables'
)
assert unpublished_location['is_unpublished_snippet'] and unpublished_location['live_urls'] == [], unpublished_location
print('PASS')
