import { type Defect } from './load-cases.ts'

export type Mutation = { diff: string; marker: string }

type LineEdit = { line: string; marker: string }

export const minimumSharedWords = 3
export const significantWords = (text: string) => new Set(text.toLowerCase().match(/[a-z][a-z0-9_-]{4,}/g) ?? [])

const isAddedLine = (line: string) => line.startsWith('+') && !line.startsWith('+++')
const withoutUrls = (line: string) => line.replace(/\]\([^)]*\)|https?:\/\/\S+/g, '')

const swappedDefaults: Record<string, string> = {
    on: 'off',
    off: 'on',
    enabled: 'disabled',
    disabled: 'enabled',
    true: 'false',
    false: 'true',
}

function changeDefault(line: string): LineEdit | null {
    if (!/default/i.test(line)) return null
    const toggle = line.match(/\b(on|off|enabled|disabled|true|false)( by)? default\b/i)
    if (toggle !== null) {
        const replacement = swappedDefaults[toggle[1].toLowerCase()]
        const isCapitalized = toggle[1][0] === toggle[1][0].toUpperCase()
        const casedReplacement = isCapitalized ? replacement[0].toUpperCase() + replacement.slice(1) : replacement
        const changedPhrase = casedReplacement + toggle[0].slice(toggle[1].length)
        return { line: line.replace(toggle[0], changedPhrase), marker: changedPhrase }
    }
    const quantity = withoutUrls(line).match(/\b(\d+)(\s+[A-Za-z]+)?/)
    if (quantity === null) return null
    const changedQuantity = String(Number(quantity[1]) * 2 + 1) + (quantity[2] ?? '')
    return { line: line.replace(quantity[0], changedQuantity), marker: changedQuantity }
}

function renameSetting(line: string): LineEdit | null {
    const setting = line.match(/`([A-Za-z][\w.-]{2,})`/)
    if (setting === null) return null
    const renamed =
        /[a-z][A-Z]/.test(setting[1]) || /^[A-Z]/.test(setting[1]) ? `${setting[1]}Setting` : `${setting[1]}_setting`
    return { line: line.replace(setting[0], `\`${renamed}\``), marker: renamed }
}

function changePath(line: string): LineEdit | null {
    const link = line.match(/\]\((\/[\w/-]*?)([\w-]+)([#?)])/)
    if (link === null) return null
    const changedSegment = `${link[2]}-settings`
    return {
        line: line.replace(link[0], `](${link[1]}${changedSegment}${link[3]}`),
        marker: `${link[1]}${changedSegment}`,
    }
}

const lineEditByDefect: Record<Exclude<Defect, 'dropped-removal'>, (line: string) => LineEdit | null> = {
    'changed-default': changeDefault,
    'renamed-setting': renameSetting,
    'wrong-path': changePath,
}

function editFirstAddedLine(diffLines: string[], editLine: (line: string) => LineEdit | null): Mutation | null {
    for (const [index, line] of diffLines.entries()) {
        if (!isAddedLine(line)) continue
        const edit = editLine(line)
        if (edit === null) continue
        const mutatedLines = diffLines.with(index, edit.line)
        return { diff: mutatedLines.join('\n'), marker: edit.marker }
    }
    return null
}

function recountHunks(fileLines: string[]): string[] {
    const recounted: string[] = []
    let lineShift = 0
    let headerIndex = -1
    let oldStart = 0
    let oldCount = 0
    let newCount = 0
    const closeHunk = () => {
        if (headerIndex === -1) return
        const newStart = oldCount === 0 ? oldStart + lineShift + 1 : oldStart + lineShift
        recounted[headerIndex] = recounted[headerIndex].replace(
            /^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/,
            `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`
        )
        lineShift += newCount - oldCount
    }
    for (const line of fileLines) {
        const hunk = line.match(/^@@ -(\d+)(?:,(\d+))? /)
        if (hunk !== null) {
            closeHunk()
            headerIndex = recounted.length
            oldStart = Number(hunk[1])
            oldCount = 0
            newCount = 0
            recounted.push(line)
            continue
        }
        recounted.push(line)
        if (headerIndex === -1) continue
        if (line.startsWith(' ') || line.startsWith('-')) oldCount++
        if (line.startsWith(' ') || line.startsWith('+')) newCount++
    }
    closeHunk()
    return recounted
}

function keepRemovedLines(referenceDiff: string): Mutation | null {
    let marker: string | null = null
    const fileDiffs = referenceDiff.split(/(?=^diff --git )/m).map((fileDiff) => {
        if (/^deleted file mode/m.test(fileDiff)) return fileDiff
        const fileLines = fileDiff.split('\n')
        const keptLines = fileLines.map((line) => {
            if (!line.startsWith('-') || line.startsWith('---')) return line
            if (marker === null && significantWords(line.slice(1)).size >= minimumSharedWords)
                marker = line.slice(1).trim()
            return ` ${line.slice(1)}`
        })
        return recountHunks(keptLines).join('\n')
    })
    if (marker === null) return null
    return { diff: fileDiffs.join(''), marker }
}

export function mutateReference(referenceDiff: string, defect: Defect): Mutation | null {
    if (defect === 'dropped-removal') return keepRemovedLines(referenceDiff)
    return editFirstAddedLine(referenceDiff.split('\n'), lineEditByDefect[defect])
}
