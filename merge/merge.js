import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

export const DOCS_PR_AUTHOR = 'posthog[bot]'
export const CODE_REPOSITORY = 'PostHog/posthog'
export const PROOF_CHECK_NAME = 'docs-proof'
const PROOF_CHECK_APP = 'github-actions'
const GITHUB_API_URL = 'https://api.github.com'
const CAPTURE_URL = 'https://us.i.posthog.com/i/v0/e/'
const CAPTURE_API_KEY = 'sTMFPsFhdP1Ssg'
const CONFIG_PATH = new URL('../config/posthog.json', import.meta.url)
const SOURCE_MARKER = /<!-- docs-freshness-source: ([A-Za-z0-9-]+\/[A-Za-z0-9._-]+)#([0-9]+) ([0-9a-f]{40}) -->/g
const NEXT_PAGE_LINK = /<([^>]+)>;\s*rel="next"/
const WORKFLOW_DIRECTORY_PREFIX = '.github/'
const LAST_EDITED_QUERY = 'query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) { pullRequest(number: $number) { lastEditedAt } } }'

export function readSourceMarker(body) {
    const markers = [...(body ?? '').matchAll(SOURCE_MARKER)]
    if (markers.length !== 1) {
        return null
    }
    const [, repository, number, headSha] = markers[0]
    return { repository, number: Number(number), headSha }
}

function isProofRun(checkRun) {
    return checkRun.app?.slug === PROOF_CHECK_APP && (checkRun.name === PROOF_CHECK_NAME || checkRun.name.endsWith(` / ${PROOF_CHECK_NAME}`))
}

export function mergeDecision({ source, checkRuns, codePullRequest, changedFiles, lastEditedAt }) {
    if (!source) {
        return { action: 'wait', reason: 'no single docs-freshness-source marker' }
    }
    if (source.repository !== CODE_REPOSITORY) {
        return { action: 'wait', reason: `source ${source.repository} is not ${CODE_REPOSITORY}` }
    }
    const workflowFile = changedFiles.find((changedFile) => changedFile.filename.startsWith(WORKFLOW_DIRECTORY_PREFIX))
    if (workflowFile) {
        return { action: 'wait', reason: `docs PR changes ${workflowFile.filename}, so its ${PROOF_CHECK_NAME} run cannot be trusted` }
    }
    const latestProofRun = checkRuns.filter(isProofRun).sort((left, right) => (right.started_at ?? '').localeCompare(left.started_at ?? ''))[0]
    if (!latestProofRun) {
        return { action: 'wait', reason: `no ${PROOF_CHECK_NAME} check run on the head commit` }
    }
    if (latestProofRun.status !== 'completed' || latestProofRun.conclusion !== 'success') {
        return { action: 'wait', reason: `${PROOF_CHECK_NAME} is ${latestProofRun.status}/${latestProofRun.conclusion ?? 'pending'}` }
    }
    if (lastEditedAt && Date.parse(latestProofRun.started_at ?? '') < Date.parse(lastEditedAt)) {
        return { action: 'wait', reason: `body edited at ${lastEditedAt}, after the latest ${PROOF_CHECK_NAME} started at ${latestProofRun.started_at}` }
    }
    if (!codePullRequest.merged_at) {
        return { action: 'wait', reason: `${CODE_REPOSITORY}#${source.number} is not merged` }
    }
    if (codePullRequest.base.ref !== codePullRequest.base.repo.default_branch) {
        return { action: 'wait', reason: `${CODE_REPOSITORY}#${source.number} merged into ${codePullRequest.base.ref}, not ${codePullRequest.base.repo.default_branch}` }
    }
    if (codePullRequest.head.sha !== source.headSha) {
        return { action: 'wait', reason: `${CODE_REPOSITORY}#${source.number} merged at ${codePullRequest.head.sha}, not the proved ${source.headSha}` }
    }
    return { action: 'merge', reason: `${PROOF_CHECK_NAME} passed and ${CODE_REPOSITORY}#${source.number} merged at ${source.headSha}` }
}

function eventUuid(name) {
    const hex = createHash('sha1').update(name).digest('hex')
    const variant = ((parseInt(hex[16], 16) & 0x3) | 0x8).toString(16)
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`
}

export function wouldMergePayload(pullRequest, source, decision, distinctId) {
    return {
        api_key: CAPTURE_API_KEY,
        event: 'docs_merge_would_merge',
        distinct_id: distinctId,
        uuid: eventUuid(`${pullRequest.html_url}@${pullRequest.head.sha}`),
        timestamp: new Date().toISOString(),
        properties: {
            $process_person_profile: false,
            docs_pr_url: pullRequest.html_url,
            docs_head_sha: pullRequest.head.sha,
            source_repository: source.repository,
            source_number: source.number,
            source_head_sha: source.headSha,
            reason: decision.reason,
        },
    }
}

export function githubClient(token, fetchImplementation = fetch) {
    const headers = { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' }
    return async function listOrGet(path) {
        let url = `${GITHUB_API_URL}${path}`
        const pages = []
        while (url) {
            const response = await fetchImplementation(url, { headers })
            if (!response.ok) {
                throw new Error(`GET ${url} returned HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`)
            }
            pages.push(await response.json())
            url = (response.headers.get('link') ?? '').match(NEXT_PAGE_LINK)?.[1]
        }
        return Array.isArray(pages[0]) ? pages.flat() : pages[0]
    }
}

export function githubGraphqlClient(token, fetchImplementation = fetch) {
    const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
    return async function queryGraphql(query, variables) {
        const response = await fetchImplementation(`${GITHUB_API_URL}/graphql`, { method: 'POST', headers, body: JSON.stringify({ query, variables }) })
        if (!response.ok) {
            throw new Error(`POST ${GITHUB_API_URL}/graphql returned HTTP ${response.status}: ${(await response.text()).slice(0, 500)}`)
        }
        const reply = await response.json()
        if (reply.errors?.length) {
            throw new Error(`POST ${GITHUB_API_URL}/graphql returned errors: ${JSON.stringify(reply.errors).slice(0, 500)}`)
        }
        return reply.data
    }
}

async function reconcileDocsPullRequest({ pullRequest, docsRepository, config, github, graphql, capture, runGh, log }) {
    const source = readSourceMarker(pullRequest.body)
    const [owner, name] = docsRepository.split('/')
    const checkRuns = (await github(`/repos/${docsRepository}/commits/${pullRequest.head.sha}/check-runs?per_page=100`)).check_runs
    const changedFiles = await github(`/repos/${docsRepository}/pulls/${pullRequest.number}/files?per_page=100`)
    const lastEditedAt = (await graphql(LAST_EDITED_QUERY, { owner, name, number: pullRequest.number })).repository.pullRequest.lastEditedAt
    const codePullRequest = source.repository === CODE_REPOSITORY ? await github(`/repos/${CODE_REPOSITORY}/pulls/${source.number}`) : null
    const decision = mergeDecision({ source, checkRuns, codePullRequest, changedFiles, lastEditedAt })
    log(`${pullRequest.html_url}: ${decision.action} (${decision.reason})`)
    if (decision.action !== 'merge') {
        if (config.auto_merge && pullRequest.auto_merge) {
            log(`${pullRequest.html_url}: disabling auto-merge`)
            runGh(['pr', 'merge', pullRequest.html_url, '--disable-auto'])
        }
        return
    }
    if (!config.auto_merge) {
        await capture(wouldMergePayload(pullRequest, source, decision, config.capture_distinct_id))
        return
    }
    if (pullRequest.draft) {
        runGh(['pr', 'ready', pullRequest.html_url])
    }
    runGh(['pr', 'merge', pullRequest.html_url, '--auto', '--squash', '--match-head-commit', pullRequest.head.sha])
}

export async function reconcileDocsPullRequests({ docsRepository, config, github, graphql, capture, runGh, log = console.log }) {
    const pullRequests = await github(`/repos/${docsRepository}/pulls?state=open&per_page=100`)
    const docsPullRequests = pullRequests.filter((pullRequest) => pullRequest.user?.login === DOCS_PR_AUTHOR && readSourceMarker(pullRequest.body))
    const failedPullRequestUrls = []
    for (const pullRequest of docsPullRequests) {
        try {
            await reconcileDocsPullRequest({ pullRequest, docsRepository, config, github, graphql, capture, runGh, log })
        } catch (error) {
            log(`${pullRequest.html_url}: error (${error.message})`)
            failedPullRequestUrls.push(pullRequest.html_url)
        }
    }
    return { failedPullRequestUrls }
}

async function capturePayload(payload) {
    const response = await fetch(process.env.DOCS_FRESHNESS_CAPTURE_URL ?? CAPTURE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
    if (!response.ok) {
        throw new Error(`capture returned HTTP ${response.status}`)
    }
}

async function main() {
    const missingEnvironment = ['GITHUB_TOKEN', 'DOCS_REPOSITORY'].filter((name) => !process.env[name])
    if (missingEnvironment.length) {
        throw new Error(`Missing environment: ${missingEnvironment.join(', ')}`)
    }
    const { failedPullRequestUrls } = await reconcileDocsPullRequests({
        docsRepository: process.env.DOCS_REPOSITORY,
        config: JSON.parse(readFileSync(CONFIG_PATH, 'utf8')),
        github: githubClient(process.env.GITHUB_TOKEN),
        graphql: githubGraphqlClient(process.env.GITHUB_TOKEN),
        capture: capturePayload,
        runGh: (arguments_) => execFileSync('gh', arguments_, { stdio: 'inherit' }),
    })
    if (failedPullRequestUrls.length) {
        process.exitCode = 1
    }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
    await main()
}
