import assert from 'node:assert/strict'
import { test } from 'node:test'

import { DOCS_PR_AUTHOR, reconcileDocsPullRequests } from './merge.js'

const DOCS_REPOSITORY = 'PostHog/posthog.com'
const PROVED_SHA = 'a'.repeat(40)
const OTHER_SHA = 'b'.repeat(40)

function sourceMarker(number, headSha = PROVED_SHA) {
    return `Docs update.\n<!-- docs-freshness-source: PostHog/posthog#${number} ${headSha} -->\n<!-- docs-freshness-claim: {"page":"a.mdx","kind":"ui-label","claim":"Save"} -->`
}

function proofRun(conclusion, { status = 'completed', app = 'github-actions', startedAt = '2026-10-20T10:00:00Z' } = {}) {
    return { name: 'docs proof / docs-proof', app: { slug: app }, status, conclusion, started_at: startedAt }
}

const scenarios = [
    { name: 'code PR missing', number: 9, checkRuns: [proofRun('success')], codeHead: PROVED_SHA, isCodeMissing: true, shouldMerge: false },
    { name: 'proved', number: 1, checkRuns: [proofRun('success')], codeHead: PROVED_SHA, shouldMerge: true },
    { name: 'proof failed with auto-merge armed', number: 2, checkRuns: [proofRun('failure')], codeHead: PROVED_SHA, isAutoMergeArmed: true, shouldMerge: false },
    { name: 'code merged at another commit', number: 3, checkRuns: [proofRun('success')], codeHead: OTHER_SHA, shouldMerge: false },
    { name: 'code not merged', number: 4, checkRuns: [proofRun('success')], codeHead: PROVED_SHA, isCodeUnmerged: true, shouldMerge: false },
    { name: 'proof from another app', number: 5, checkRuns: [proofRun('success', { app: 'mallory-app' })], codeHead: PROVED_SHA, shouldMerge: false },
    {
        name: 'newer proof still running',
        number: 6,
        checkRuns: [proofRun('success'), proofRun(null, { status: 'in_progress', startedAt: '2026-10-20T11:00:00Z' })],
        codeHead: PROVED_SHA,
        shouldMerge: false,
    },
    { name: 'not the docs bot', number: 7, author: 'someone', checkRuns: [proofRun('success')], codeHead: PROVED_SHA, shouldMerge: false },
    {
        name: 'docs PR changes a workflow',
        number: 10,
        changedFiles: [{ filename: '.github/workflows/docs-proof.yml' }],
        checkRuns: [proofRun('success')],
        codeHead: PROVED_SHA,
        shouldMerge: false,
    },
    { name: 'body edited after proof started', number: 11, lastEditedAt: '2026-10-20T10:30:00Z', checkRuns: [proofRun('success')], codeHead: PROVED_SHA, shouldMerge: false },
    { name: 'code merged into another branch', number: 12, codeBaseRef: 'release', checkRuns: [proofRun('success')], codeHead: PROVED_SHA, shouldMerge: false },
    { name: 'two source markers', number: 8, body: sourceMarker(8) + sourceMarker(9), checkRuns: [proofRun('success')], codeHead: PROVED_SHA, shouldMerge: false },
]

function docsHeadSha(scenario) {
    return `${scenario.number}`.padStart(40, '0')
}

function fakeGithub() {
    const responses = new Map()
    const missingPaths = new Set()
    responses.set(
        `/repos/${DOCS_REPOSITORY}/pulls?state=open&per_page=100`,
        scenarios.map((scenario) => ({
            number: 100 + scenario.number,
            html_url: `https://github.com/${DOCS_REPOSITORY}/pull/${100 + scenario.number}`,
            user: { login: scenario.author ?? DOCS_PR_AUTHOR },
            body: scenario.body ?? sourceMarker(scenario.number),
            draft: true,
            head: { sha: docsHeadSha(scenario) },
            auto_merge: scenario.isAutoMergeArmed ? { merge_method: 'squash' } : null,
        })),
    )
    for (const scenario of scenarios) {
        responses.set(`/repos/${DOCS_REPOSITORY}/commits/${docsHeadSha(scenario)}/check-runs?per_page=100`, { check_runs: scenario.checkRuns })
        responses.set(`/repos/${DOCS_REPOSITORY}/pulls/${100 + scenario.number}/files?per_page=100`, scenario.changedFiles ?? [{ filename: 'contents/docs/a.mdx' }])
        const codePullRequestPath = `/repos/PostHog/posthog/pulls/${scenario.number}`
        if (scenario.isCodeMissing) {
            missingPaths.add(codePullRequestPath)
            continue
        }
        responses.set(codePullRequestPath, {
            merged_at: scenario.isCodeUnmerged ? null : '2026-10-20T09:00:00Z',
            head: { sha: scenario.codeHead },
            base: { ref: scenario.codeBaseRef ?? 'master', repo: { default_branch: 'master' } },
        })
    }
    return async (path) => {
        if (missingPaths.has(path)) {
            throw new Error(`GET ${path} returned HTTP 404`)
        }
        assert.ok(responses.has(path), `unexpected GitHub request ${path}`)
        return responses.get(path)
    }
}

async function fakeGraphql(query, variables) {
    assert.deepEqual([variables.owner, variables.name], DOCS_REPOSITORY.split('/'))
    const scenario = scenarios.find((candidate) => 100 + candidate.number === variables.number)
    assert.ok(scenario, `unexpected GraphQL pull request ${variables.number}`)
    return { repository: { pullRequest: { lastEditedAt: scenario.lastEditedAt ?? null } } }
}

async function reconcile(autoMerge) {
    const captured = []
    const ghCalls = []
    const { failedPullRequestUrls } = await reconcileDocsPullRequests({
        docsRepository: DOCS_REPOSITORY,
        config: { capture_distinct_id: 'docs-audit', auto_merge: autoMerge },
        github: fakeGithub(),
        graphql: fakeGraphql,
        capture: async (payload) => captured.push(payload),
        runGh: (arguments_) => ghCalls.push(arguments_),
        log: () => {},
    })
    return { captured, ghCalls, failedPullRequestUrls }
}

test('only a proved docs PR whose code PR merged at the proved commit is merged, and only once auto_merge is on', async () => {
    const provedUrl = `https://github.com/${DOCS_REPOSITORY}/pull/101`
    const armedUrl = `https://github.com/${DOCS_REPOSITORY}/pull/102`
    const missingCodeUrl = `https://github.com/${DOCS_REPOSITORY}/pull/109`

    const dryRun = await reconcile(false)
    assert.deepEqual(dryRun.ghCalls, [])
    assert.deepEqual(dryRun.captured.map((payload) => [payload.event, payload.properties.docs_pr_url, payload.properties.source_head_sha]), [
        ['docs_merge_would_merge', provedUrl, PROVED_SHA],
    ])
    assert.deepEqual(dryRun.failedPullRequestUrls, [missingCodeUrl])
    assert.equal((await reconcile(false)).captured[0].uuid, dryRun.captured[0].uuid)

    const live = await reconcile(true)
    assert.deepEqual(live.captured, [])
    assert.deepEqual(live.ghCalls, [
        ['pr', 'ready', provedUrl],
        ['pr', 'merge', provedUrl, '--auto', '--squash', '--match-head-commit', '1'.padStart(40, '0')],
        ['pr', 'merge', armedUrl, '--disable-auto'],
    ])
    assert.deepEqual(live.failedPullRequestUrls, [missingCodeUrl])
})
