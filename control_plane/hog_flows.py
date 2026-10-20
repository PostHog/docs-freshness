import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
LOOP_KEY = 'linked-docs-pr'
MANAGED_BY_MARKER = f'managed-by:docs-freshness key:{LOOP_KEY}'
TRIGGER_EVENT = 'docs_impact_flagged'
DOCS_REPOSITORY = 'PostHog/posthog.com'
CODE_REPOSITORY = 'PostHog/posthog'
PROMPT_PATH = REPOSITORY_ROOT / 'agents' / f'{LOOP_KEY}.md'
PROMPT_EVENT_PROPERTIES = {'pr_url', 'head_sha', 'claims', 'pages'}
TRIGGER_PROPERTY_FILTERS = [
    {'key': 'repository', 'value': [CODE_REPOSITORY], 'operator': 'exact', 'type': 'event'},
    {'key': 'pr_url', 'value': f'^https://github\\.com/{re.escape(CODE_REPOSITORY)}/pull/[0-9]+$', 'operator': 'regex', 'type': 'event'},
    {'key': 'head_sha', 'value': '^[0-9a-f]{40}$', 'operator': 'regex', 'type': 'event'},
]
TEMPLATE_PLACEHOLDER = re.compile(r'\{([^{}]*)\}')
TRIGGER_MASKING_TTL_SECONDS = 30 * 24 * 60 * 60
PULL_REQUEST_STATE_INSTRUCTIONS = {
    True: 'Open the pull request as a draft. During the shadow period a reviewer reads every one before anything merges.',
    False: 'Open the pull request ready for review.',
}
CONFIG_FLAG_TYPES = {'capture_distinct_id': str, 'shadow': bool, 'comments': bool, 'auto_merge': bool}


def validate_config(config):
    problems = [f'{key} must be a {expected.__name__}' for key, expected in CONFIG_FLAG_TYPES.items() if not isinstance(config.get(key), expected)]
    problems.extend(f'unknown key {key}' for key in sorted(set(config) - set(CONFIG_FLAG_TYPES)))
    return problems


def loop_prompt(config):
    prompt = PROMPT_PATH.read_text().strip()
    unknown_placeholders = sorted({
        placeholder for placeholder in TEMPLATE_PLACEHOLDER.findall(prompt)
        if placeholder.removeprefix('event.properties.') not in PROMPT_EVENT_PROPERTIES or not placeholder.startswith('event.properties.')
    })
    if unknown_placeholders or prompt.count('{') != prompt.count('}'):
        raise ValueError(f'{PROMPT_PATH.name} may only interpolate event.properties of {sorted(PROMPT_EVENT_PROPERTIES)}; found {unknown_placeholders}')
    return f"{prompt}\n\n{PULL_REQUEST_STATE_INSTRUCTIONS[config['shadow']]}"


def desired_loop(config):
    return {
        'name': 'Docs freshness: linked docs PR',
        'description': f'Opens or updates the posthog.com docs PR linked to a code PR that docs impact flagged. {MANAGED_BY_MARKER}',
        'status': 'active',
        'exit_condition': 'exit_only_at_end',
        'trigger_masking': {'hash': '{event.properties.pr_url}@{event.properties.head_sha}', 'ttl': TRIGGER_MASKING_TTL_SECONDS},
        'actions': [
            {
                'id': 'trigger', 'name': 'Trigger', 'type': 'trigger',
                'config': {'type': 'event', 'filters': {'events': [{'id': TRIGGER_EVENT, 'name': TRIGGER_EVENT, 'type': 'events'}], 'properties': TRIGGER_PROPERTY_FILTERS}},
            },
            {
                'id': 'create_task', 'name': 'Create AI task', 'type': 'function',
                'config': {
                    'template_id': 'template-posthog-create-task',
                    'inputs': {
                        'prompt': {'value': loop_prompt(config)},
                        'repository': {'value': DOCS_REPOSITORY},
                        'title': {'value': 'Docs for {event.properties.pr_url}'},
                    },
                },
            },
            {'id': 'exit', 'name': 'Exit', 'type': 'exit', 'config': {'reason': 'Task created'}},
        ],
        'edges': [
            {'from': 'trigger', 'to': 'create_task', 'type': 'continue'},
            {'from': 'create_task', 'to': 'exit', 'type': 'continue'},
        ],
    }


def holds(live, desired):
    if isinstance(desired, dict):
        return isinstance(live, dict) and all(key in live and holds(live[key], value) for key, value in desired.items())
    if isinstance(desired, list):
        return isinstance(live, list) and len(live) == len(desired) and all(holds(item, wanted) for item, wanted in zip(live, desired))
    return live == desired


def looks_like_loop(flow, desired_name):
    if flow.get('name') == desired_name:
        return True
    trigger_configs = [action.get('config') for action in flow.get('actions') or [] if action.get('type') == 'trigger']
    return any(TRIGGER_EVENT in json.dumps(trigger_config) for trigger_config in trigger_configs)


def owned_loops(api, owner_uuid, desired_name):
    candidates = api.list_all('hog_flows/', {'created_by': owner_uuid})
    flows = [api.request('GET', api.project_url(f"hog_flows/{candidate['id']}/")) for candidate in candidates]
    marked_flows = [flow for flow in flows if MANAGED_BY_MARKER in (flow.get('description') or '')]
    if marked_flows:
        return marked_flows
    unmarked_lookalikes = [flow for flow in flows if looks_like_loop(flow, desired_name)]
    if unmarked_lookalikes:
        flow_ids = ', '.join(flow['id'] for flow in unmarked_lookalikes)
        raise ValueError(f'hog flows {flow_ids} look like the {LOOP_KEY} Loop but lack {MANAGED_BY_MARKER!r}; restore the marker in the description of the one to keep, or delete them, before reconciling')
    return []


def plan(api, owner_uuid, config):
    desired = desired_loop(config)
    live_flows = owned_loops(api, owner_uuid, desired['name'])
    if len(live_flows) > 1:
        flow_ids = ', '.join(flow['id'] for flow in live_flows)
        raise ValueError(f'{len(live_flows)} hog flows carry {MANAGED_BY_MARKER!r} ({flow_ids}); delete all but one by hand')
    if not live_flows:
        return [{'action': 'create', 'payload': {**desired, 'origin_product': 'loops'}}]
    live = live_flows[0]
    drifted_fields = sorted(field for field, value in desired.items() if not holds(live.get(field), value))
    if not drifted_fields:
        return []
    return [{'action': 'update', 'id': live['id'], 'fields': drifted_fields, 'payload': {field: desired[field] for field in drifted_fields}}]


def apply(api, changes):
    for change in changes:
        if change['action'] == 'create':
            api.request('POST', api.project_url('hog_flows/'), change['payload'])
            continue
        api.request('PATCH', api.project_url(f"hog_flows/{change['id']}/"), change['payload'])


def describe(change):
    if change['action'] == 'create':
        return f'create hog_flow {LOOP_KEY}'
    return f"update hog_flow {LOOP_KEY} {change['id']}: {', '.join(change['fields'])}"
