export type DiffStats = { pageOverlap: number; editSizeLines: number }

function changedLinesByPath(diff: string): Map<string, string[]> {
    const linesByPath = new Map<string, string[]>()
    let currentLines: string[] | null = null
    let isInHunk = false
    for (const line of diff.split('\n')) {
        const header = line.match(/^diff --git a\/\S+ b\/(\S+)$/)
        if (header !== null) {
            currentLines = linesByPath.get(header[1]) ?? []
            linesByPath.set(header[1], currentLines)
            isInHunk = false
            continue
        }
        if (line.startsWith('@@')) isInHunk = true
        if (currentLines === null || !isInHunk) continue
        if (line.startsWith('+') || line.startsWith('-')) currentLines.push(line)
    }
    return linesByPath
}

function countUnmatched(lines: string[], against: string[]): number {
    const remainingByLine = new Map<string, number>()
    for (const line of against) remainingByLine.set(line, (remainingByLine.get(line) ?? 0) + 1)
    let unmatched = 0
    for (const line of lines) {
        const remaining = remainingByLine.get(line) ?? 0
        if (remaining === 0) {
            unmatched++
            continue
        }
        remainingByLine.set(line, remaining - 1)
    }
    return unmatched
}

export function diffStats(draftDiff: string, referenceDiff: string): DiffStats {
    const draftLines = changedLinesByPath(draftDiff)
    const referenceLines = changedLinesByPath(referenceDiff)
    const allPaths = new Set([...draftLines.keys(), ...referenceLines.keys()])
    const sharedPaths = [...draftLines.keys()].filter((path) => referenceLines.has(path))
    let editSizeLines = 0
    for (const path of allPaths) {
        const draftPathLines = draftLines.get(path) ?? []
        const referencePathLines = referenceLines.get(path) ?? []
        editSizeLines +=
            countUnmatched(draftPathLines, referencePathLines) + countUnmatched(referencePathLines, draftPathLines)
    }
    return { pageOverlap: allPaths.size === 0 ? 1 : sharedPaths.length / allPaths.size, editSizeLines }
}
