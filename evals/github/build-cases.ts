import { execFile } from 'node:child_process'
import { mkdirSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseArgs, promisify } from 'node:util'
import { stringify } from 'yaml'

import {
    caseFile,
    casesDir,
    repoRoot,
    type Case,
    type InkeepHistory,
    type Meta,
    type ReferenceKind,
} from '../load-cases.ts'

const runFile = promisify(execFile)
const codeRepo = 'PostHog/posthog'
const docsRepo = 'PostHog/posthog.com'
const inkeepLogin = 'inkeep[bot]'
const maxCodeDiffBytes = 400_000
const parallelRequests = 8
export const inkeepDraftsDir = join(repoRoot, 'evals/drafts/inkeep')
const buildDir = join(repoRoot, 'evals/.build')
const stagedCasesDir = join(buildDir, 'cases')
const stagedInkeepDraftsDir = join(buildDir, 'inkeep')

type SearchedPr = { number: number; body: string; state: string; author: { login: string } }
type PrCommit = {
    sha: string
    parents: unknown[]
    author: { login: string } | null
    commit: { author: { name: string } }
}
type DocsPr = {
    number: number
    authorLogin: string
    baseSha: string
    codePr: number
    commits: PrCommit[]
}
type Candidate = DocsPr & { referenceKind: ReferenceKind; section: string; mergeSha: string }

async function gh(args: string[]): Promise<string> {
    const { stdout } = await runFile('gh', args, { maxBuffer: 512 * 1024 * 1024 })
    return stdout
}
const ghJson = async <T>(args: string[]): Promise<T> => JSON.parse(await gh(args)) as T
const ghDiff = (path: string) => gh(['api', path, '-H', 'Accept: application/vnd.github.diff'])
const commitsByPr = new Map<number, Promise<PrCommit[]>>()
function prCommits(number: number): Promise<PrCommit[]> {
    const cached = commitsByPr.get(number)
    if (cached) return cached
    const commits = ghJson<PrCommit[][]>([
        'api',
        '--paginate',
        '--slurp',
        `repos/${docsRepo}/pulls/${number}/commits`,
    ]).then((pages) => pages.flat())
    commitsByPr.set(number, commits)
    return commits
}

async function mapInParallel<T, R>(items: T[], work: (item: T) => Promise<R>): Promise<R[]> {
    const results: R[] = new Array(items.length)
    let nextIndex = 0
    const worker = async () => {
        while (nextIndex < items.length) {
            const index = nextIndex++
            results[index] = await work(items[index])
        }
    }
    await Promise.all(Array.from({ length: parallelRequests }, worker))
    return results
}

function linkedCodePrs(body: string): number[] {
    const links = body.matchAll(/(?:github\.com\/PostHog\/posthog\/pull\/|PostHog\/posthog#)(\d+)/g)
    return [...new Set([...links].map((link) => Number(link[1])))]
}

const isBotCommit = (commit: PrCommit) =>
    (commit.author?.login ?? '').endsWith('[bot]') || /\[bot\]|inkeep/i.test(commit.commit.author.name)
const isBranchUpdateMerge = (commit: PrCommit) => commit.parents.length > 1
const isCarbonUnitTextEdit = (commit: PrCommit) => !isBotCommit(commit) && !isBranchUpdateMerge(commit)

function preEditDraftSha(commits: PrCommit[]): string | null {
    const firstEditIndex = commits.findIndex(isCarbonUnitTextEdit)
    const commitsBeforeEdit = firstEditIndex === -1 ? commits : commits.slice(0, firstEditIndex)
    return commitsBeforeEdit.filter(isBotCommit).at(-1)?.sha ?? null
}

function referenceKindOf(docsPr: DocsPr): ReferenceKind | null {
    if (docsPr.authorLogin === inkeepLogin) {
        return docsPr.commits.some(isCarbonUnitTextEdit) ? 'edited-inkeep' : 'unedited-inkeep'
    }
    if (docsPr.authorLogin.endsWith('[bot]')) return null
    return 'carbon-unit'
}

const isDocsFile = (path: string) => /^contents\/.+\.mdx?$/.test(path)
const isTestOrGeneratedFile = (path: string) =>
    /(^|\/)(tests?|__tests__|__snapshots__|snapshots)\//.test(path) ||
    /(^|\/)test_[^/]*$|[._-](test|spec)\.[^/]+$|\.snap$/.test(path) ||
    /(^|\/)(pnpm-lock\.yaml|package-lock\.json|uv\.lock|yarn\.lock)$/.test(path)

function filterDiff(diff: string, keepPath: (path: string) => boolean): string {
    return diff
        .split(/(?=^diff --git )/m)
        .filter((fileDiff) => {
            const header = fileDiff.match(/^diff --git a\/(\S+) b\/(\S+)/)
            return header !== null && keepPath(header[2])
        })
        .join('')
}

const touchedPaths = (diff: string) =>
    [...diff.matchAll(/^diff --git a\/\S+ b\/(\S+)$/gm)].map((header) => header[1]).sort()

async function fetchDocsPr(number: number, body: string, authorLogin: string): Promise<DocsPr | null> {
    const codePrs = linkedCodePrs(body)
    if (codePrs.length !== 1) return null
    const [pull, commits] = await Promise.all([
        ghJson<{ base: { sha: string } }>(['api', `repos/${docsRepo}/pulls/${number}`]),
        prCommits(number),
    ])
    return { number, authorLogin, baseSha: pull.base.sha, codePr: codePrs[0], commits }
}

type CodePull = { merged_at: string | null; merge_commit_sha: string }
const isNotFound = (error: unknown) => String((error as { stderr?: string }).stderr).includes('HTTP 404')

async function fetchCodePull(number: number): Promise<CodePull | null> {
    try {
        return await ghJson<CodePull>(['api', `repos/${codeRepo}/pulls/${number}`])
    } catch (error) {
        if (isNotFound(error)) return null
        throw error
    }
}

async function toCandidate(docsPr: DocsPr): Promise<Candidate | null> {
    const referenceKind = referenceKindOf(docsPr)
    if (referenceKind === null) return null
    const [files, codePull] = await Promise.all([
        ghJson<string[]>([
            'pr',
            'view',
            String(docsPr.number),
            '-R',
            docsRepo,
            '--json',
            'files',
            '--jq',
            '[.files[].path]',
        ]),
        fetchCodePull(docsPr.codePr),
    ])
    const docsPage = files.find((path) => /^contents\/docs\/[^/]+\/.+\.mdx?$/.test(path))
    if (codePull === null || codePull.merged_at === null || docsPage === undefined) return null
    return { ...docsPr, referenceKind, section: docsPage.split('/')[2], mergeSha: codePull.merge_commit_sha }
}

function stratifiedOrder<T>(items: T[], stratumOf: (item: T) => string): T[] {
    const strata = new Map<string, T[]>()
    for (const item of items) strata.set(stratumOf(item), [...(strata.get(stratumOf(item)) ?? []), item])
    const queues = [...strata.keys()].sort().map((key) => strata.get(key)!)
    const ordered: T[] = []
    while (queues.some((queue) => queue.length > 0)) {
        for (const queue of queues) {
            const next = queue.shift()
            if (next !== undefined) ordered.push(next)
        }
    }
    return ordered
}

const stratumOf = (candidate: Candidate) => `${candidate.section}/${candidate.referenceKind}`

async function preEditDraftDiff(docsPr: DocsPr): Promise<string | null> {
    const draftSha = preEditDraftSha(docsPr.commits)
    if (draftSha === null) return null
    return filterDiff(await ghDiff(`repos/${docsRepo}/compare/${docsPr.baseSha}...${draftSha}`), isDocsFile)
}

async function writeCase(candidate: Candidate, inkeepPrForCodePr: Map<number, DocsPr>): Promise<boolean> {
    const fullCodeDiff = await ghDiff(`repos/${codeRepo}/commits/${candidate.mergeSha}`).catch(() => null)
    if (fullCodeDiff === null) return false
    const codeDiff = filterDiff(fullCodeDiff, (path) => !isTestOrGeneratedFile(path))
    if (codeDiff === '' || Buffer.byteLength(codeDiff) > maxCodeDiffBytes) return false
    const referenceDiff = filterDiff(await gh(['pr', 'diff', String(candidate.number), '-R', docsRepo]), isDocsFile)
    if (referenceDiff === '') return false

    const caseItem: Case = {
        id: `posthog-${candidate.codePr}`,
        codePr: candidate.codePr,
        mergeSha: candidate.mergeSha,
        docsPr: candidate.number,
        docsBaseSha: candidate.baseSha,
        section: candidate.section,
        referenceKind: candidate.referenceKind,
        pages: touchedPaths(referenceDiff),
    }
    mkdirSync(join(stagedCasesDir, caseItem.id), { recursive: true })
    writeFileSync(caseFile(caseItem.id, 'case.yaml', stagedCasesDir), stringify(caseItem))
    writeFileSync(caseFile(caseItem.id, 'code.diff', stagedCasesDir), codeDiff)
    writeFileSync(caseFile(caseItem.id, 'reference.diff', stagedCasesDir), referenceDiff)

    const inkeepPr = candidate.authorLogin === inkeepLogin ? candidate : inkeepPrForCodePr.get(candidate.codePr)
    if (inkeepPr === undefined) return true
    const inkeepDraft = await preEditDraftDiff(inkeepPr)
    if (inkeepDraft !== null) writeFileSync(join(stagedInkeepDraftsDir, `${caseItem.id}.diff`), inkeepDraft)
    return true
}

async function readInkeepHistory(since: string): Promise<{ history: InkeepHistory; prs: DocsPr[] }> {
    const searched = await ghJson<SearchedPr[]>([
        'search',
        'prs',
        '--repo',
        docsRepo,
        '--author',
        'app/inkeep',
        '--created',
        `>=${since}`,
        '--limit',
        '1000',
        '--json',
        'number,body,state,author',
    ])
    const merged = searched.filter((pr) => pr.state === 'merged')
    const mergedWithCommits = await mapInParallel(merged, async (pr) => ({ pr, commits: await prCommits(pr.number) }))
    const mergedEditedCount = mergedWithCommits.filter(({ commits }) => commits.some(isCarbonUnitTextEdit)).length
    const mergedUnedited = merged.length - mergedEditedCount
    const linkedPrs = (
        await mapInParallel(
            searched.filter((pr) => linkedCodePrs(pr.body).length === 1),
            (pr) => fetchDocsPr(pr.number, pr.body, inkeepLogin)
        )
    ).filter((pr): pr is DocsPr => pr !== null)
    return {
        prs: linkedPrs,
        history: {
            opened: searched.length,
            mergedUnedited,
            mergedEdited: mergedEditedCount,
            closedUnmerged: searched.filter((pr) => pr.state === 'closed').length,
            stillOpen: searched.filter((pr) => pr.state === 'open').length,
            mergeReadyRate: Number((mergedUnedited / searched.length).toFixed(3)),
        },
    }
}

async function main() {
    const ninetyDaysAgo = new Date(Date.now() - 90 * 24 * 3600 * 1000).toISOString().slice(0, 10)
    const { values } = parseArgs({
        options: { since: { type: 'string', default: ninetyDaysAgo }, count: { type: 'string', default: '40' } },
    })
    const since = values.since
    const caseCount = Number(values.count)
    const until = new Date().toISOString().slice(0, 10)

    console.log(`Reading Inkeep PRs created since ${since}`)
    const { history, prs: inkeepPrs } = await readInkeepHistory(since)
    const inkeepPrForCodePr = new Map(inkeepPrs.map((pr) => [pr.codePr, pr]))

    console.log(`Reading merged ${docsRepo} PRs since ${since} that link one ${codeRepo} PR`)
    const searched = await ghJson<SearchedPr[]>([
        'search',
        'prs',
        '--repo',
        docsRepo,
        '--merged',
        '--merged-at',
        `>=${since}`,
        'PostHog/posthog',
        '--limit',
        '1000',
        '--json',
        'number,body,state,author',
    ])
    const docsPrs = (await mapInParallel(searched, (pr) => fetchDocsPr(pr.number, pr.body, pr.author.login))).filter(
        (pr): pr is DocsPr => pr !== null
    )
    const candidates = (await mapInParallel(docsPrs, toCandidate))
        .filter((candidate): candidate is Candidate => candidate !== null)
        .sort((first, second) => second.number - first.number)
    const candidateByCodePr = new Map<number, Candidate>()
    for (const candidate of candidates) {
        if (!candidateByCodePr.has(candidate.codePr)) candidateByCodePr.set(candidate.codePr, candidate)
    }
    const uniqueCandidates = [...candidateByCodePr.values()]

    rmSync(buildDir, { recursive: true, force: true })
    mkdirSync(stagedCasesDir, { recursive: true })
    mkdirSync(stagedInkeepDraftsDir, { recursive: true })

    const picked: Candidate[] = []
    for (const candidate of stratifiedOrder(uniqueCandidates, stratumOf)) {
        if (picked.length === caseCount) break
        if (await writeCase(candidate, inkeepPrForCodePr)) picked.push(candidate)
    }

    const casesByStratum: Record<string, number> = {}
    for (const candidate of picked) {
        casesByStratum[stratumOf(candidate)] = (casesByStratum[stratumOf(candidate)] ?? 0) + 1
    }
    const meta: Meta = { since, until, candidates: uniqueCandidates.length, casesByStratum, inkeep: history }
    writeFileSync(join(stagedCasesDir, 'meta.yaml'), stringify(meta))
    rmSync(casesDir, { recursive: true, force: true })
    rmSync(inkeepDraftsDir, { recursive: true, force: true })
    renameSync(stagedCasesDir, casesDir)
    renameSync(stagedInkeepDraftsDir, inkeepDraftsDir)
    rmSync(buildDir, { recursive: true, force: true })
    console.log(`Wrote ${picked.length} cases from ${uniqueCandidates.length} candidates`)
    console.log(`Inkeep merge-ready rate ${history.mergeReadyRate} (${history.mergedUnedited}/${history.opened})`)
}

if (import.meta.url === `file://${process.argv[1]}`) await main()
