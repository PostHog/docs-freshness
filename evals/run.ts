import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { basename, join, resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { parse } from 'yaml'

import { createJudge, type Judge, type JudgeConfig, type Verdict } from './judge/judge.ts'
import {
    type Case,
    type Defect,
    defects,
    loadCases,
    loadDrafts,
    loadMeta,
    readCaseDiff,
    type ReferenceKind,
    repoRoot,
} from './load-cases.ts'
import { diffStats } from './metrics/diff-stats.ts'
import { prepareVale, type ValeRunner, type ValeStats } from './metrics/vale.ts'
import { minimumSharedWords, mutateReference, significantWords } from './mutate.ts'

export type CaseScore = {
    caseId: string
    rep: number
    writer: string
    referenceKind: ReferenceKind
    hasDraft: boolean
    pageOverlap: number
    editSizeLines: number
    vale: ValeStats | null
    verdict: Verdict | null
    isMergeReady: boolean | null
}
export type ScoreError = { caseId: string; rep: number; stage: string; message: string }
export type ScoreRun = { scores: CaseScore[]; errors: ScoreError[]; reps: number }
type ScoreOptions = { cases?: Case[]; vale?: ValeRunner; judge?: Judge; reps?: number; parallelRequests?: number }
type DeterministicScore = Omit<CaseScore, 'rep' | 'verdict' | 'isMergeReady'>

type RunConfig = {
    docsRepo: string
    vale: { version: string; configRef: string }
    judge: JudgeConfig & { reps: number; parallelRequests: number }
}

const resultsDir = join(repoRoot, 'evals/results')
const sharedDir = join(repoRoot, 'evals/shared')

const readYaml = <T>(name: string) => parse(readFileSync(join(sharedDir, name), 'utf8')) as T

async function mapInParallel<T>(items: T[], limit: number, handleItem: (item: T) => Promise<void>) {
    let nextIndex = 0
    const worker = async () => {
        while (nextIndex < items.length) await handleItem(items[nextIndex++])
    }
    await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
}

function mergeReadiness(score: Omit<CaseScore, 'isMergeReady'>, canScoreMergeReady: boolean): boolean | null {
    if (!canScoreMergeReady || !score.hasDraft) return null
    if (score.vale === null || score.verdict === null || !score.vale.draftApplies) return false
    return score.verdict.mergeReady && score.vale.valeErrorsOnAddedLines === 0
}

export async function scoreDrafts(
    draftsDir: string,
    { cases = loadCases(), vale, judge, reps = 1, parallelRequests = 8 }: ScoreOptions = {}
): Promise<ScoreRun> {
    const writer = basename(resolve(draftsDir))
    const draftByCaseId = new Map(loadDrafts(draftsDir, cases).map((draft) => [draft.caseId, draft.diff]))
    const repCount = judge === undefined ? 1 : reps
    const errors: ScoreError[] = []

    const scoredCases: { caseItem: Case; draftDiff: string | undefined; deterministic: DeterministicScore }[] = []
    for (const caseItem of cases) {
        const draftDiff = draftByCaseId.get(caseItem.id)
        const deterministic: DeterministicScore = {
            caseId: caseItem.id,
            writer,
            referenceKind: caseItem.referenceKind,
            hasDraft: draftDiff !== undefined,
            ...diffStats(draftDiff ?? '', readCaseDiff(caseItem.id, 'reference.diff')),
            vale: null,
        }
        if (vale !== undefined && draftDiff !== undefined) {
            try {
                deterministic.vale = await vale(draftDiff, caseItem.docsBaseSha)
            } catch (error) {
                errors.push({ caseId: caseItem.id, rep: 1, stage: 'vale', message: String(error) })
                continue
            }
        }
        scoredCases.push({ caseItem, draftDiff, deterministic })
    }

    const verdictByCaseRep = new Map<string, Verdict | 'error'>()
    if (judge !== undefined) {
        const judgeTasks = scoredCases
            .filter(
                ({ draftDiff, deterministic }) => draftDiff !== undefined && deterministic.vale?.draftApplies !== false
            )
            .flatMap((scoredCase) =>
                Array.from({ length: repCount }, (_, index) => ({ ...scoredCase, rep: index + 1 }))
            )
        await mapInParallel(judgeTasks, parallelRequests, async ({ caseItem, draftDiff, rep }) => {
            const caseRep = `${caseItem.id}#${rep}`
            try {
                const verdict = await judge({
                    case: caseItem,
                    codeDiff: readCaseDiff(caseItem.id, 'code.diff'),
                    referenceDiff: readCaseDiff(caseItem.id, 'reference.diff'),
                    draftDiff: draftDiff ?? '',
                })
                verdictByCaseRep.set(caseRep, verdict)
            } catch (error) {
                verdictByCaseRep.set(caseRep, 'error')
                errors.push({ caseId: caseItem.id, rep, stage: 'judge', message: String(error) })
            }
        })
    }

    const canScoreMergeReady = vale !== undefined && judge !== undefined
    const scores = scoredCases.flatMap(({ deterministic }) =>
        Array.from({ length: repCount }, (_, index) => index + 1).flatMap((rep) => {
            const verdict = verdictByCaseRep.get(`${deterministic.caseId}#${rep}`) ?? null
            if (verdict === 'error') return []
            const score = { ...deterministic, rep, verdict }
            return [{ ...score, isMergeReady: mergeReadiness(score, canScoreMergeReady) }]
        })
    )
    return { scores, errors, reps: repCount }
}

const total = (values: number[]) => values.reduce((sum, value) => sum + value, 0)
const mean = (values: number[]) => total(values) / Math.max(values.length, 1)
const formatRate = (value: number) => value.toFixed(2)
const isScoredForMergeReady = (score: CaseScore) => score.isMergeReady !== null

const mergeReadyRate = (scores: CaseScore[]) =>
    mean(scores.filter(isScoredForMergeReady).map((score) => (score.isMergeReady ? 1 : 0)))

function mergeReadyLine({ scores, reps }: ScoreRun, skippedMetrics: string[]): string {
    if (skippedMetrics.length > 0) return `Merge-ready rate not scored: ${skippedMetrics.join(' and ')} skipped.`
    const scoresByRep = Array.from({ length: reps }, (_, index) =>
        scores.filter((score) => score.rep === index + 1 && isScoredForMergeReady(score))
    ).filter((repScores) => repScores.length > 0)
    if (scoresByRep.length === 0) return 'Merge-ready rate not scored: no drafted cases.'
    const ratesByRep = scoresByRep.map(mergeReadyRate)
    const averageRate = mean(ratesByRep)
    const spread = Math.sqrt(mean(ratesByRep.map((rate) => (rate - averageRate) ** 2)))
    const historicalRate = loadMeta().inkeep.mergeReadyRate
    return `Merge-ready rate ${formatRate(averageRate)} ± ${formatRate(spread)} over ${ratesByRep.length} rep(s) (Inkeep historical ${formatRate(historicalRate)}).`
}

function summaryRows(scores: CaseScore[]): string[] {
    const oneScorePerCase = [...new Map(scores.map((score) => [score.caseId, score])).values()]
    const valeScores = oneScorePerCase.flatMap((score) => (score.vale === null ? [] : [score.vale]))
    const valeRows =
        valeScores.length === 0
            ? []
            : [
                  `| drafts that do not apply at the docs base | ${valeScores.filter((vale) => !vale.draftApplies).length} |`,
                  `| Vale errors on added lines | ${total(valeScores.map((vale) => vale.valeErrorsOnAddedLines))} |`,
                  `| Vale warnings on added lines | ${total(valeScores.map((vale) => vale.valeWarningsOnAddedLines))} |`,
              ]
    const mergeReadyRows = scores.some(isScoredForMergeReady)
        ? [`| merge-ready rate | ${formatRate(mergeReadyRate(scores))} |`]
        : []
    return [
        ...mergeReadyRows,
        `| drafts | ${oneScorePerCase.filter((score) => score.hasDraft).length}/${oneScorePerCase.length} |`,
        `| mean page overlap | ${formatRate(mean(oneScorePerCase.map((score) => score.pageOverlap)))} |`,
        `| mean edit size (lines) | ${mean(oneScorePerCase.map((score) => score.editSizeLines)).toFixed(1)} |`,
        ...valeRows,
    ]
}

function valeCell(vale: ValeStats | null, count: (vale: ValeStats) => number): string {
    if (vale === null) return '-'
    return vale.draftApplies ? String(count(vale)) : 'does not apply'
}

function verdictCells(caseScores: CaseScore[]): string[] {
    const verdicts = caseScores.flatMap((score) => (score.verdict === null ? [] : [score.verdict]))
    const readyCount = caseScores.filter((score) => score.isMergeReady === true).length
    const ready = caseScores.some(isScoredForMergeReady) ? `${readyCount}/${caseScores.length}` : '-'
    if (verdicts.length === 0) return [ready, '-', '-']
    return [
        ready,
        mean(verdicts.map((verdict) => verdict.wrongFacts.length)).toFixed(1),
        mean(verdicts.map((verdict) => verdict.missingFacts.length)).toFixed(1),
    ]
}

function renderSummary(scoreRun: ScoreRun, skippedMetrics: string[]): string {
    const { scores, errors } = scoreRun
    const writer = scores[0]?.writer ?? 'none'
    const referenceKinds = [...new Set(scores.map((score) => score.referenceKind))].sort()
    const errorLine = `${errors.length} case rep(s) hit infrastructure errors and are not scored; see errors.jsonl.`
    const kindSections = referenceKinds.flatMap((referenceKind) => [
        '',
        `### ${referenceKind}`,
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores.filter((score) => score.referenceKind === referenceKind)),
    ])
    const caseIds = [...new Set(scores.map((score) => score.caseId))]
    const caseRows = caseIds.map((caseId) => {
        const caseScores = scores.filter((score) => score.caseId === caseId)
        const [firstScore] = caseScores
        return [
            caseId,
            firstScore.referenceKind,
            ...verdictCells(caseScores),
            firstScore.hasDraft ? 'yes' : 'no',
            formatRate(firstScore.pageOverlap),
            firstScore.editSizeLines,
            valeCell(firstScore.vale, (vale) => vale.valeErrorsOnAddedLines),
            valeCell(firstScore.vale, (vale) => vale.valeWarningsOnAddedLines),
        ].join(' | ')
    })
    return [
        `# ${writer}`,
        '',
        mergeReadyLine(scoreRun, skippedMetrics),
        ...(errors.length > 0 ? ['', errorLine] : []),
        '',
        '| metric | value |',
        '| --- | --- |',
        ...summaryRows(scores),
        ...kindSections,
        '',
        '## Cases',
        '',
        '| case | kind | ready | wrong | missing | draft | pages | edit | vale errors | vale warnings |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |',
        ...caseRows.map((row) => `| ${row} |`),
        '',
    ].join('\n')
}

const toJsonLines = (rows: object[]) => rows.map((row) => JSON.stringify(row) + '\n').join('')

function selectCases(caseIdsArgument: string | undefined): Case[] {
    const requestedCaseIds = caseIdsArgument?.split(',')
    return loadCases().filter((caseItem) => requestedCaseIds?.includes(caseItem.id) ?? true)
}

function createRunDir(): string {
    const runDir = join(resultsDir, new Date().toISOString().replace(/[:.]/g, '-'))
    mkdirSync(runDir, { recursive: true })
    return runDir
}

const loadJudge = (runConfig: RunConfig) =>
    createJudge(runConfig.judge, readYaml<Record<string, string>>('criteria.yaml'))

async function score(args: string[]) {
    const { values } = parseArgs({
        args,
        options: {
            drafts: { type: 'string' },
            cases: { type: 'string' },
            reps: { type: 'string' },
            'skip-judge': { type: 'boolean', default: false },
            'skip-vale': { type: 'boolean', default: false },
        },
    })
    if (values.drafts === undefined) throw new Error('score needs --drafts <dir>')

    const runConfig = readYaml<RunConfig>('run-config.yml')
    const cases = selectCases(values.cases)
    const vale = values['skip-vale']
        ? undefined
        : await prepareVale({ docsRepo: runConfig.docsRepo, ...runConfig.vale })
    const judge = values['skip-judge'] ? undefined : loadJudge(runConfig)
    const scoreRun = await scoreDrafts(values.drafts, {
        cases,
        vale,
        judge,
        reps: Number(values.reps ?? runConfig.judge.reps),
        parallelRequests: runConfig.judge.parallelRequests,
    })
    const runDir = createRunDir()
    writeFileSync(join(runDir, 'results.jsonl'), toJsonLines(scoreRun.scores))
    writeFileSync(join(runDir, 'errors.jsonl'), toJsonLines(scoreRun.errors))
    const skippedMetrics = [...(values['skip-vale'] ? ['Vale'] : []), ...(values['skip-judge'] ? ['judge'] : [])]
    const summary = renderSummary(scoreRun, skippedMetrics)
    writeFileSync(join(runDir, 'summary.md'), summary)
    console.log(summary)
    console.log(`Results in ${runDir}`)
}

type GraderVariant = 'reference' | Defect
type GraderOutcome = { caseId: string; variant: GraderVariant; rep: number; isCaught: boolean; reason: string }

function namesDefect(verdict: Verdict, defect: Defect, marker: string): boolean {
    const verdictText = JSON.stringify([verdict.wrongFacts, verdict.missingFacts]).toLowerCase()
    if (defect !== 'dropped-removal') return verdictText.includes(marker.toLowerCase())
    const verdictWords = significantWords(verdictText)
    return [...significantWords(marker)].filter((word) => verdictWords.has(word)).length >= minimumSharedWords
}

function graderOutcome(score: CaseScore, variant: GraderVariant, marker: string | undefined): GraderOutcome {
    const outcome = { caseId: score.caseId, variant, rep: score.rep }
    const verdict = score.verdict
    if (verdict === null) return { ...outcome, isCaught: false, reason: 'no verdict' }
    if (variant === 'reference') {
        return { ...outcome, isCaught: verdict.mergeReady, reason: verdict.mergeReady ? 'merge-ready' : verdict.reason }
    }
    if (verdict.mergeReady) return { ...outcome, isCaught: false, reason: `judged merge-ready: ${verdict.reason}` }
    if (!namesDefect(verdict, variant, marker ?? '')) {
        return { ...outcome, isCaught: false, reason: `not merge-ready, but the verdict does not name \`${marker}\`` }
    }
    return { ...outcome, isCaught: true, reason: 'not merge-ready, defect named' }
}

function renderGraderSummary(outcomes: GraderOutcome[], errors: ScoreError[]): string {
    const variants: GraderVariant[] = ['reference', ...defects]
    const variantRows = variants.map((variant) => {
        const variantOutcomes = outcomes.filter((outcome) => outcome.variant === variant)
        const caughtCount = variantOutcomes.filter((outcome) => outcome.isCaught).length
        return `| ${variant} | ${caughtCount}/${variantOutcomes.length} |`
    })
    const misses = outcomes.filter((outcome) => !outcome.isCaught)
    const missRows = misses.map(
        (miss) => `| ${miss.caseId} | ${miss.variant} | ${miss.rep} | ${miss.reason.replace(/\|/g, '\\|')} |`
    )
    return [
        '# Grader check',
        '',
        'References must be judged merge-ready. Wrong variants must be judged not merge-ready with the defect named.',
        ...(errors.length > 0
            ? ['', `${errors.length} judge call(s) hit infrastructure errors; see errors.jsonl.`]
            : []),
        '',
        '| variant | passed |',
        '| --- | --- |',
        ...variantRows,
        '',
        `## Misses (${misses.length})`,
        '',
        ...(misses.length === 0
            ? ['None.']
            : ['| case | variant | rep | reason |', '| --- | --- | --- | --- |', ...missRows]),
        '',
    ].join('\n')
}

async function checkGrader(args: string[]) {
    const { values } = parseArgs({ args, options: { cases: { type: 'string' }, reps: { type: 'string' } } })
    const runConfig = readYaml<RunConfig>('run-config.yml')
    const cases = selectCases(values.cases)
    const judge = loadJudge(runConfig)
    const runDir = createRunDir()

    const markerByVariantCase = new Map<string, string>()
    const variants: GraderVariant[] = ['reference', ...defects]
    for (const variant of variants) {
        mkdirSync(join(runDir, 'drafts', variant), { recursive: true })
        for (const caseItem of cases) {
            const referenceDiff = readCaseDiff(caseItem.id, 'reference.diff')
            const mutation =
                variant === 'reference' ? { diff: referenceDiff, marker: '' } : mutateReference(referenceDiff, variant)
            if (mutation === null) continue
            markerByVariantCase.set(`${variant}/${caseItem.id}`, mutation.marker)
            writeFileSync(join(runDir, 'drafts', variant, `${caseItem.id}.diff`), mutation.diff)
        }
    }

    const outcomes: GraderOutcome[] = []
    const errors: ScoreError[] = []
    for (const variant of variants) {
        const variantCases = cases.filter((caseItem) => markerByVariantCase.has(`${variant}/${caseItem.id}`))
        const scoreRun = await scoreDrafts(join(runDir, 'drafts', variant), {
            cases: variantCases,
            judge,
            reps: Number(values.reps ?? 1),
            parallelRequests: runConfig.judge.parallelRequests,
        })
        errors.push(...scoreRun.errors)
        outcomes.push(
            ...scoreRun.scores.map((score) =>
                graderOutcome(score, variant, markerByVariantCase.get(`${variant}/${score.caseId}`))
            )
        )
    }
    writeFileSync(join(runDir, 'results.jsonl'), toJsonLines(outcomes))
    writeFileSync(join(runDir, 'errors.jsonl'), toJsonLines(errors))
    const summary = renderGraderSummary(outcomes, errors)
    writeFileSync(join(runDir, 'summary.md'), summary)
    console.log(summary)
    console.log(`Results in ${runDir}`)
    if (outcomes.some((outcome) => !outcome.isCaught) || errors.length > 0) process.exitCode = 1
}

const commands: Record<string, (args: string[]) => Promise<void>> = { score, 'check-grader': checkGrader }

async function main() {
    const [commandName, ...args] = process.argv.slice(2)
    const command = commands[commandName ?? '']
    if (command === undefined) throw new Error(`Usage: pnpm evals <${Object.keys(commands).join('|')}> [options]`)
    await command(args)
}

if (import.meta.url === `file://${process.argv[1]}`) await main()
