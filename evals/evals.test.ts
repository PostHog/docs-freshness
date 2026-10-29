import assert from 'node:assert/strict'
import { readdirSync } from 'node:fs'
import { test } from 'node:test'

import { inkeepDraftsDir } from './github/build-cases.ts'
import { loadCases, loadMeta } from './load-cases.ts'
import { scoreDrafts } from './run.ts'

test('the committed snapshot is valid and every Inkeep draft belongs to a case', () => {
    const cases = loadCases()
    const meta = loadMeta()
    assert.ok(cases.length > 0, 'the snapshot holds no cases')
    assert.equal(
        Object.values(meta.casesByStratum).reduce((total, count) => total + count, 0),
        cases.length,
        'meta.yaml strata do not add up to the cases on disk'
    )
    const caseIds = new Set(cases.map((caseItem) => caseItem.id))
    const orphanDrafts = readdirSync(inkeepDraftsDir)
        .map((name) => name.replace(/\.diff$/, ''))
        .filter((caseId) => !caseIds.has(caseId))
    assert.deepEqual(orphanDrafts, [])
})

test('every unedited Inkeep draft matches its reference on pages and lines', async () => {
    const { scores } = await scoreDrafts(inkeepDraftsDir)
    const uneditedScores = scores.filter((score) => score.referenceKind === 'unedited-inkeep')
    assert.ok(uneditedScores.length > 0, 'the snapshot holds no unedited Inkeep cases')
    for (const score of uneditedScores) {
        assert.deepEqual(
            [score.caseId, score.hasDraft, score.pageOverlap, score.editSizeLines],
            [score.caseId, true, 1, 0]
        )
    }
})
