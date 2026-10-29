import { mkdirSync, writeFileSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'
import { parseArgs } from 'node:util'

import { type Case, loadCases, loadDrafts, readCaseDiff, type ReferenceKind, repoRoot } from './load-cases.ts'
import { diffStats } from './metrics/diff-stats.ts'

export type CaseScore = {
    caseId: string
    rep: number
    writer: string
    referenceKind: ReferenceKind
    hasDraft: boolean
    pageOverlap: number
    editSizeLines: number
}

const resultsDir = join(repoRoot, 'evals/results')

export function scoreDrafts(draftsDir: string, cases: Case[] = loadCases()): CaseScore[] {
    const writer = basename(resolve(draftsDir))
    const draftByCaseId = new Map(loadDrafts(draftsDir, cases).map((draft) => [draft.caseId, draft.diff]))
    return cases.map((caseItem) => {
        const draftDiff = draftByCaseId.get(caseItem.id)
        return {
            caseId: caseItem.id,
            rep: 1,
            writer,
            referenceKind: caseItem.referenceKind,
            hasDraft: draftDiff !== undefined,
            ...diffStats(draftDiff ?? '', readCaseDiff(caseItem.id, 'reference.diff')),
        }
    })
}

const mean = (values: number[]) => values.reduce((total, value) => total + value, 0) / Math.max(values.length, 1)
const formatRate = (value: number) => value.toFixed(2)

function summaryRows(scores: CaseScore[]): string[] {
    return [
        `| drafts | ${scores.filter((score) => score.hasDraft).length}/${scores.length} |`,
        `| mean page overlap | ${formatRate(mean(scores.map((score) => score.pageOverlap)))} |`,
        `| mean edit size (lines) | ${mean(scores.map((score) => score.editSizeLines)).toFixed(1)} |`,
    ]
}

function renderSummary(scores: CaseScore[], skippedMetrics: string[]): string {
    const writer = scores[0]?.writer ?? 'none'
    const referenceKinds = [...new Set(scores.map((score) => score.referenceKind))].sort()
    const mergeReadyLine = `Merge-ready rate not scored: ${skippedMetrics.join(' and ')} skipped.`
    const kindSections = referenceKinds.flatMap((referenceKind) => [
        '',
        `### ${referenceKind}`,
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores.filter((score) => score.referenceKind === referenceKind)),
    ])
    const caseRows = scores.map(
        (score) =>
            `| ${score.caseId} | ${score.referenceKind} | ${score.hasDraft ? 'yes' : 'no'} | ${formatRate(score.pageOverlap)} | ${score.editSizeLines} |`
    )
    return [
        `# ${writer}`,
        '',
        mergeReadyLine,
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores),
        ...kindSections,
        '',
        '## Cases',
        '',
        '| case | kind | draft | pages | edit |',
        '| --- | --- | --- | --- | --- |',
        ...caseRows,
        '',
    ].join('\n')
}

function score(args: string[]) {
    const { values } = parseArgs({
        args,
        options: {
            drafts: { type: 'string' },
            'skip-judge': { type: 'boolean', default: false },
            'skip-vale': { type: 'boolean', default: false },
        },
    })
    if (values.drafts === undefined) throw new Error('score needs --drafts <dir>')
    if (!values['skip-vale']) throw new Error('The Vale metric is not built yet; pass --skip-vale')
    if (!values['skip-judge']) throw new Error('The judge is not built yet; pass --skip-judge')

    const scores = scoreDrafts(values.drafts)
    const runDir = join(resultsDir, new Date().toISOString().replace(/[:.]/g, '-'))
    mkdirSync(runDir, { recursive: true })
    writeFileSync(join(runDir, 'results.jsonl'), scores.map((caseScore) => JSON.stringify(caseScore)).join('\n') + '\n')
    writeFileSync(join(runDir, 'errors.jsonl'), '')
    const summary = renderSummary(scores, ['Vale', 'judge'])
    writeFileSync(join(runDir, 'summary.md'), summary)
    console.log(summary)
    console.log(`Results in ${runDir}`)
}

const commands: Record<string, (args: string[]) => void> = { score }

function main() {
    const [commandName, ...args] = process.argv.slice(2)
    const command = commands[commandName ?? '']
    if (command === undefined) throw new Error(`Usage: pnpm evals <${Object.keys(commands).join('|')}> [options]`)
    command(args)
}

if (import.meta.url === `file://${process.argv[1]}`) main()
