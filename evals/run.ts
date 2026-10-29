import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { parse } from 'yaml'

import { type Case, loadCases, loadDrafts, readCaseDiff, type ReferenceKind, repoRoot } from './load-cases.ts'
import { diffStats } from './metrics/diff-stats.ts'
import { prepareVale, type ValeRunner, type ValeStats } from './metrics/vale.ts'

export type CaseScore = {
    caseId: string
    rep: number
    writer: string
    referenceKind: ReferenceKind
    hasDraft: boolean
    pageOverlap: number
    editSizeLines: number
    vale: ValeStats | null
}
export type ScoreError = { caseId: string; rep: number; stage: string; message: string }
export type ScoreRun = { scores: CaseScore[]; errors: ScoreError[] }

type RunConfig = { docsRepo: string; vale: { version: string; configRef: string } }

const resultsDir = join(repoRoot, 'evals/results')
const runConfigPath = join(repoRoot, 'evals/shared/run-config.yml')

const loadRunConfig = () => parse(readFileSync(runConfigPath, 'utf8')) as RunConfig

export async function scoreDrafts(
    draftsDir: string,
    { cases = loadCases(), vale }: { cases?: Case[]; vale?: ValeRunner } = {}
): Promise<ScoreRun> {
    const writer = basename(resolve(draftsDir))
    const draftByCaseId = new Map(loadDrafts(draftsDir, cases).map((draft) => [draft.caseId, draft.diff]))
    const run: ScoreRun = { scores: [], errors: [] }
    for (const caseItem of cases) {
        const draftDiff = draftByCaseId.get(caseItem.id)
        const caseScore: CaseScore = {
            caseId: caseItem.id,
            rep: 1,
            writer,
            referenceKind: caseItem.referenceKind,
            hasDraft: draftDiff !== undefined,
            ...diffStats(draftDiff ?? '', readCaseDiff(caseItem.id, 'reference.diff')),
            vale: null,
        }
        if (vale !== undefined && draftDiff !== undefined) {
            try {
                caseScore.vale = await vale(draftDiff, caseItem.docsBaseSha)
            } catch (error) {
                run.errors.push({ caseId: caseItem.id, rep: 1, stage: 'vale', message: String(error) })
                continue
            }
        }
        run.scores.push(caseScore)
    }
    return run
}

const total = (values: number[]) => values.reduce((sum, value) => sum + value, 0)
const mean = (values: number[]) => total(values) / Math.max(values.length, 1)
const formatRate = (value: number) => value.toFixed(2)

function summaryRows(scores: CaseScore[]): string[] {
    const valeScores = scores.flatMap((score) => (score.vale === null ? [] : [score.vale]))
    const valeRows =
        valeScores.length === 0
            ? []
            : [
                  `| drafts that do not apply at the docs base | ${valeScores.filter((vale) => !vale.draftApplies).length} |`,
                  `| Vale errors on added lines | ${total(valeScores.map((vale) => vale.valeErrorsOnAddedLines))} |`,
                  `| Vale warnings on added lines | ${total(valeScores.map((vale) => vale.valeWarningsOnAddedLines))} |`,
              ]
    return [
        `| drafts | ${scores.filter((score) => score.hasDraft).length}/${scores.length} |`,
        `| mean page overlap | ${formatRate(mean(scores.map((score) => score.pageOverlap)))} |`,
        `| mean edit size (lines) | ${mean(scores.map((score) => score.editSizeLines)).toFixed(1)} |`,
        ...valeRows,
    ]
}

function valeCell(vale: ValeStats | null, count: (vale: ValeStats) => number): string {
    if (vale === null) return '-'
    return vale.draftApplies ? String(count(vale)) : 'does not apply'
}

function renderSummary({ scores, errors }: ScoreRun, skippedMetrics: string[]): string {
    const writer = scores[0]?.writer ?? 'none'
    const referenceKinds = [...new Set(scores.map((score) => score.referenceKind))].sort()
    const mergeReadyLine = `Merge-ready rate not scored: ${skippedMetrics.join(' and ')} skipped.`
    const errorLine = `${errors.length} case(s) hit infrastructure errors and are not scored; see errors.jsonl.`
    const kindSections = referenceKinds.flatMap((referenceKind) => [
        '',
        `### ${referenceKind}`,
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores.filter((score) => score.referenceKind === referenceKind)),
    ])
    const caseRows = scores.map((score) =>
        [
            score.caseId,
            score.referenceKind,
            score.hasDraft ? 'yes' : 'no',
            formatRate(score.pageOverlap),
            score.editSizeLines,
            valeCell(score.vale, (vale) => vale.valeErrorsOnAddedLines),
            valeCell(score.vale, (vale) => vale.valeWarningsOnAddedLines),
        ].join(' | ')
    )
    return [
        `# ${writer}`,
        '',
        mergeReadyLine,
        ...(errors.length > 0 ? ['', errorLine] : []),
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores),
        ...kindSections,
        '',
        '## Cases',
        '',
        '| case | kind | draft | pages | edit | vale errors | vale warnings |',
        '| --- | --- | --- | --- | --- | --- | --- |',
        ...caseRows.map((row) => `| ${row} |`),
        '',
    ].join('\n')
}

const toJsonLines = (rows: object[]) => rows.map((row) => JSON.stringify(row) + '\n').join('')

async function score(args: string[]) {
    const { values } = parseArgs({
        args,
        options: {
            drafts: { type: 'string' },
            'skip-judge': { type: 'boolean', default: false },
            'skip-vale': { type: 'boolean', default: false },
        },
    })
    if (values.drafts === undefined) throw new Error('score needs --drafts <dir>')
    if (!values['skip-judge']) throw new Error('The judge is not built yet; pass --skip-judge')

    const runConfig = loadRunConfig()
    const vale = values['skip-vale']
        ? undefined
        : await prepareVale({ docsRepo: runConfig.docsRepo, ...runConfig.vale })
    const scoreRun = await scoreDrafts(values.drafts, { vale })
    const runDir = join(resultsDir, new Date().toISOString().replace(/[:.]/g, '-'))
    mkdirSync(runDir, { recursive: true })
    writeFileSync(join(runDir, 'results.jsonl'), toJsonLines(scoreRun.scores))
    writeFileSync(join(runDir, 'errors.jsonl'), toJsonLines(scoreRun.errors))
    const summary = renderSummary(scoreRun, [...(values['skip-vale'] ? ['Vale'] : []), 'judge'])
    writeFileSync(join(runDir, 'summary.md'), summary)
    console.log(summary)
    console.log(`Results in ${runDir}`)
}

const commands: Record<string, (args: string[]) => Promise<void>> = { score }

async function main() {
    const [commandName, ...args] = process.argv.slice(2)
    const command = commands[commandName ?? '']
    if (command === undefined) throw new Error(`Usage: pnpm evals <${Object.keys(commands).join('|')}> [options]`)
    await command(args)
}

if (import.meta.url === `file://${process.argv[1]}`) await main()
