import argparse
import json
import os
import sys
from pathlib import Path

from control_plane import hog_flows
from control_plane.posthog_api import PostHogApi

REQUIRED_ENVIRONMENT = ('POSTHOG_HOST', 'POSTHOG_PROJECT_ID', 'POSTHOG_PERSONAL_API_KEY', 'DOCS_FRESHNESS_OWNER_UUID')


def main():
    parser = argparse.ArgumentParser(prog='control_plane', description='Reconcile the docs-freshness Loop hog flow with config/posthog.json.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true', help='check the config and render the Loop without calling PostHog')
    mode.add_argument('--apply', action='store_true', help='create or update the Loop')
    mode.add_argument('--fail-on-drift', action='store_true', help='exit 1 when the live Loop differs from the config')
    parser.add_argument('config', type=Path)
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text())
    problems = hog_flows.validate_config(config)
    if problems:
        parser.exit(2, f'{arguments.config}: {"; ".join(problems)}\n')
    if arguments.validate:
        hog_flows.desired_loop(config)
        print(f'{arguments.config} is valid and renders the {hog_flows.LOOP_KEY} Loop')
        return
    missing_environment = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing_environment:
        parser.exit(2, f'Missing environment: {", ".join(missing_environment)}\n')
    api = PostHogApi(os.environ['POSTHOG_HOST'], os.environ['POSTHOG_PROJECT_ID'], os.environ['POSTHOG_PERSONAL_API_KEY'])
    changes = hog_flows.plan(api, os.environ['DOCS_FRESHNESS_OWNER_UUID'], config)
    for change in changes:
        print(hog_flows.describe(change))
    if not changes:
        print('No changes')
    if arguments.apply:
        hog_flows.apply(api, changes)
        return
    if arguments.fail_on_drift and changes:
        sys.exit(1)


main()
