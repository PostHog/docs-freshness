import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, mkdtempSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { arch, platform, tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { promisify } from 'node:util'

import { repoRoot } from '../load-cases.ts'

export type ValeConfig = { docsRepo: string; version: string; configRef: string }
export type ValeFinding = { path: string; line: number; severity: string; check: string; message: string }
export type ValeStats = {
    draftApplies: boolean
    valeErrorsOnAddedLines: number
    valeWarningsOnAddedLines: number
    valeFindings: ValeFinding[]
}
export type ValeRunner = (draftDiff: string, docsBaseSha: string) => Promise<ValeStats>

type ValeAlert = { Line: number; Severity: string; Check: string; Message: string }

const run = promisify(execFile)
const cacheDir = join(repoRoot, 'evals/.cache')
const cloneDir = join(cacheDir, 'posthog.com')
const valeAssetByPlatform: Record<string, string> = {
    'linux-x64': 'Linux_64-bit',
    'linux-arm64': 'Linux_arm64',
    'darwin-x64': 'macOS_64-bit',
    'darwin-arm64': 'macOS_arm64',
}
const isDocsPagePath = (path: string) =>
    /^contents\/(?:[\w.-]+\/)*\w[\w.-]*\.mdx?$/.test(path) && !path.split('/').includes('..')

async function download(url: string): Promise<Buffer> {
    const response = await fetch(url)
    if (!response.ok) throw new Error(`GET ${url} returned ${response.status}`)
    return Buffer.from(await response.arrayBuffer())
}

async function ensureValeBinary(version: string): Promise<string> {
    const binary = join(cacheDir, `vale-${version}`, 'vale')
    if (existsSync(binary)) return binary
    const assetSuffix = valeAssetByPlatform[`${platform()}-${arch()}`]
    if (assetSuffix === undefined) throw new Error(`Vale ${version} has no build for ${platform()}-${arch()}`)
    const asset = `vale_${version}_${assetSuffix}.tar.gz`
    const releaseUrl = `https://github.com/errata-ai/vale/releases/download/v${version}`
    const [tarball, checksums] = await Promise.all([
        download(`${releaseUrl}/${asset}`),
        download(`${releaseUrl}/vale_${version}_checksums.txt`),
    ])
    const expectedSha256 = checksums
        .toString()
        .split('\n')
        .find((line) => line.endsWith(`  ${asset}`))
        ?.split(' ')[0]
    const actualSha256 = createHash('sha256').update(tarball).digest('hex')
    if (actualSha256 !== expectedSha256) throw new Error(`${asset} checksum ${actualSha256} is not ${expectedSha256}`)
    mkdirSync(cacheDir, { recursive: true })
    const stagingDir = mkdtempSync(join(cacheDir, `vale-${version}-staging-`))
    const tarballPath = join(stagingDir, asset)
    writeFileSync(tarballPath, tarball)
    await run('tar', ['-xzf', tarballPath, '-C', stagingDir, 'vale'])
    rmSync(tarballPath)
    try {
        renameSync(stagingDir, dirname(binary))
    } catch (error) {
        rmSync(stagingDir, { recursive: true, force: true })
        if (!existsSync(binary)) throw error
    }
    return binary
}

const git = (args: string[]) =>
    run('git', ['--literal-pathspecs', '-C', cloneDir, ...args], { maxBuffer: 64 * 1024 * 1024 })

async function ensureCommit(docsRepo: string, sha: string) {
    if (!existsSync(cloneDir)) {
        mkdirSync(cacheDir, { recursive: true })
        await run('git', ['clone', '--bare', '--filter=blob:none', `https://github.com/${docsRepo}.git`, cloneDir])
    }
    const hasCommit = await git(['cat-file', '-e', `${sha}^{commit}`]).then(
        () => true,
        () => false
    )
    if (!hasCommit) await git(['fetch', '--filter=blob:none', 'origin', sha])
}

export async function readDocsRepoFile(docsRepo: string, sha: string, path: string): Promise<string> {
    await ensureCommit(docsRepo, sha)
    const { stdout } = await git(['show', `${sha}:${path}`])
    return stdout
}

async function extractPaths(sha: string, paths: string[], into: string) {
    const { stdout } = await git(['ls-tree', '-r', '--name-only', sha, '--', ...paths])
    const existingPaths = stdout.split('\n').filter((path) => path !== '')
    if (existingPaths.length === 0) return
    const archivePath = join(into, `${sha}.tar`)
    await git(['archive', `--output=${archivePath}`, sha, '--', ...existingPaths])
    await run('tar', ['-xf', archivePath, '-C', into])
    rmSync(archivePath)
}

const docsPageDiff = (diff: string) =>
    diff
        .split(/(?=^diff --git )/m)
        .filter((fileDiff) => {
            const header = fileDiff.match(/^diff --git a\/(\S+) b\/(\S+)/)
            return header !== null && isDocsPagePath(header[1]) && isDocsPagePath(header[2])
        })
        .join('')

function addedLinesByPath(diff: string): Map<string, Set<number>> {
    const addedByPath = new Map<string, Set<number>>()
    let currentLines: Set<number> | null = null
    let newLineNumber = 0
    for (const line of diff.split('\n')) {
        const header = line.match(/^diff --git a\/\S+ b\/(\S+)$/)
        if (header !== null) {
            currentLines = new Set()
            addedByPath.set(header[1], currentLines)
            newLineNumber = 0
            continue
        }
        const hunk = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
        if (hunk !== null) {
            newLineNumber = Number(hunk[1])
            continue
        }
        if (currentLines === null || newLineNumber === 0) continue
        if (line.startsWith('+')) currentLines.add(newLineNumber++)
        if (line.startsWith(' ')) newLineNumber++
    }
    return addedByPath
}

async function lint(binary: string, workDir: string, paths: string[]): Promise<Record<string, ValeAlert[]>> {
    const output = await run(binary, ['--output=JSON', ...paths], { cwd: workDir, maxBuffer: 64 * 1024 * 1024 }).catch(
        (error: { code?: number; stdout?: string }) => {
            if (error.code !== 1 || error.stdout === undefined) throw error
            return { stdout: error.stdout }
        }
    )
    return JSON.parse(output.stdout)
}

export async function prepareVale(config: ValeConfig): Promise<ValeRunner> {
    const binary = await ensureValeBinary(config.version)
    await ensureCommit(config.docsRepo, config.configRef)

    return async (draftDiff, docsBaseSha) => {
        const pagesDiff = docsPageDiff(draftDiff)
        const addedByPath = addedLinesByPath(pagesDiff)
        const lintedPaths = [...addedByPath.keys()].filter((path) => (addedByPath.get(path)?.size ?? 0) > 0)
        const noFindings = { valeErrorsOnAddedLines: 0, valeWarningsOnAddedLines: 0, valeFindings: [] }
        if (lintedPaths.length === 0) return { draftApplies: true, ...noFindings }

        await ensureCommit(config.docsRepo, docsBaseSha)
        const workDir = mkdtempSync(join(tmpdir(), 'docs-freshness-vale-'))
        try {
            await extractPaths(config.configRef, ['.vale.ini', '.vale'], workDir)
            await extractPaths(docsBaseSha, [...addedByPath.keys()], workDir)
            writeFileSync(join(workDir, 'draft.diff'), pagesDiff)
            const draftApplies = await run('git', ['apply', 'draft.diff'], { cwd: workDir }).then(
                () => true,
                () => false
            )
            if (!draftApplies) return { draftApplies, ...noFindings }
            const alertsByPath = await lint(binary, workDir, lintedPaths)
            const valeFindings = Object.entries(alertsByPath).flatMap(([path, alerts]) =>
                alerts
                    .filter((alert) => addedByPath.get(path)?.has(alert.Line))
                    .map((alert) => ({
                        path,
                        line: alert.Line,
                        severity: alert.Severity,
                        check: alert.Check,
                        message: alert.Message,
                    }))
            )
            return {
                draftApplies,
                valeErrorsOnAddedLines: valeFindings.filter((finding) => finding.severity === 'error').length,
                valeWarningsOnAddedLines: valeFindings.filter((finding) => finding.severity === 'warning').length,
                valeFindings,
            }
        } finally {
            rmSync(workDir, { recursive: true, force: true })
        }
    }
}
