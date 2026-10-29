import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse } from 'yaml'

export const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
export const casesDir = join(repoRoot, 'evals/cases')

export const referenceKinds = ['unedited-inkeep', 'edited-inkeep', 'carbon-unit'] as const
export type ReferenceKind = (typeof referenceKinds)[number]
export const defects = ['changed-default', 'renamed-setting', 'wrong-path', 'dropped-removal'] as const
export type Defect = (typeof defects)[number]

export type Case = {
    id: string
    codePr: number
    mergeSha: string
    docsPr: number
    docsBaseSha: string
    section: string
    referenceKind: ReferenceKind
    pages: string[]
}
export type Draft = { caseId: string; diff: string }
export type WrongVariant = Draft & { variantId: string; defect: Defect }

export type InkeepHistory = {
    opened: number
    mergedUnedited: number
    mergedEdited: number
    closedUnmerged: number
    stillOpen: number
    mergeReadyRate: number
}
export type Meta = {
    since: string
    until: string
    candidates: number
    casesByStratum: Record<string, number>
    inkeep: InkeepHistory
}

const caseKeys = ['id', 'codePr', 'mergeSha', 'docsPr', 'docsBaseSha', 'section', 'referenceKind', 'pages']
const sha = /^[0-9a-f]{40}$/

export const caseFile = (caseId: string, name: 'case.yaml' | 'code.diff' | 'reference.diff', root = casesDir) =>
    join(root, caseId, name)

function caseProblems(raw: unknown, folderName: string): string[] {
    if (typeof raw !== 'object' || raw === null) return ['case.yaml is not a mapping']
    const fields = raw as Record<string, unknown>
    const problems = Object.keys(fields)
        .filter((key) => !caseKeys.includes(key))
        .map((key) => `unknown key ${key}`)
    if (fields.id !== folderName) problems.push(`id ${String(fields.id)} does not match its folder ${folderName}`)
    for (const key of ['codePr', 'docsPr']) {
        if (!Number.isInteger(fields[key])) problems.push(`${key} must be a PR number`)
    }
    for (const key of ['mergeSha', 'docsBaseSha']) {
        if (typeof fields[key] !== 'string' || !sha.test(fields[key]))
            problems.push(`${key} must be a 40-character sha`)
    }
    if (typeof fields.section !== 'string' || fields.section === '') problems.push('section must be a docs section')
    if (!referenceKinds.includes(fields.referenceKind as ReferenceKind)) {
        problems.push(`referenceKind must be one of ${referenceKinds.join(', ')}`)
    }
    const pages = fields.pages
    if (!Array.isArray(pages) || pages.length === 0 || !pages.every((page) => typeof page === 'string')) {
        problems.push('pages must list the docs files reference.diff touches')
    }
    return problems
}

export function loadCases(root = casesDir): Case[] {
    const caseIds = readdirSync(root, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => entry.name)
        .sort()
    const problems: string[] = []
    const cases: Case[] = []
    for (const caseId of caseIds) {
        const missingFiles = (['case.yaml', 'code.diff', 'reference.diff'] as const).filter(
            (name) => !existsSync(caseFile(caseId, name, root))
        )
        if (missingFiles.length > 0) {
            problems.push(`${caseId}: missing ${missingFiles.join(', ')}`)
            continue
        }
        const raw = parse(readFileSync(caseFile(caseId, 'case.yaml', root), 'utf8'))
        const caseProblemsFound = caseProblems(raw, caseId)
        if (caseProblemsFound.length > 0) {
            problems.push(...caseProblemsFound.map((problem) => `${caseId}: ${problem}`))
            continue
        }
        cases.push(raw as Case)
    }
    if (problems.length > 0) throw new Error(`Invalid cases in ${root}:\n${problems.join('\n')}`)
    return cases
}

export const readCaseDiff = (caseId: string, name: 'code.diff' | 'reference.diff', root = casesDir) =>
    readFileSync(caseFile(caseId, name, root), 'utf8')

export function loadMeta(root = casesDir): Meta {
    return parse(readFileSync(join(root, 'meta.yaml'), 'utf8')) as Meta
}

export function loadDrafts(dir: string, cases: Case[]): Draft[] {
    return cases
        .map((caseItem) => ({ caseId: caseItem.id, path: join(dir, `${caseItem.id}.diff`) }))
        .filter(({ path }) => existsSync(path))
        .map(({ caseId, path }) => ({ caseId, diff: readFileSync(path, 'utf8') }))
}
