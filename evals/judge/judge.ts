import Anthropic from '@anthropic-ai/sdk'
import { betaZodOutputFormat } from '@anthropic-ai/sdk/helpers/beta/zod'
import { z } from 'zod'

import type { Case } from '../load-cases.ts'

export const VerdictSchema = z.object({
    facts: z.object({
        pass: z.boolean(),
        wrongFacts: z.array(z.object({ claim: z.string(), why: z.string() })),
        missingFacts: z.array(z.string()),
    }),
    style: z.object({
        pass: z.boolean(),
        violations: z.array(z.object({ quote: z.string(), rule: z.string() })),
    }),
    reason: z.string(),
})
export type Verdict = z.infer<typeof VerdictSchema>
export type JudgeConfig = {
    model: string
    effort: 'low' | 'medium' | 'high' | 'xhigh' | 'max'
    maxTokens: number
    criterion: string
}
export type JudgeInput = { case: Case; codeDiff: string; draftDiff: string }
export type Judge = (input: JudgeInput) => Promise<Verdict>

export class JudgeError extends Error {}

const caseContext = ({ case: caseItem, codeDiff }: JudgeInput) =>
    [
        `<case id="${caseItem.id}" section="${caseItem.section}">`,
        `<code_change pr="PostHog/posthog#${caseItem.codePr}">\n${codeDiff}\n</code_change>`,
        '</case>',
    ].join('\n')

export function createJudge(config: JudgeConfig, criteria: Record<string, string>, styleGuide: string): Judge {
    const rubric = criteria[config.criterion]
    if (rubric === undefined) throw new Error(`criteria.yaml has no rubric named ${config.criterion}`)
    const client = new Anthropic()

    return async (input) => {
        const response = await client.beta.messages.parse({
            model: config.model,
            max_tokens: config.maxTokens,
            betas: ['server-side-fallback-2026-07-01'],
            fallbacks: 'default',
            system: [
                { type: 'text', text: rubric },
                {
                    type: 'text',
                    text: `<style_guide>\n${styleGuide}\n</style_guide>`,
                    cache_control: { type: 'ephemeral' },
                },
            ],
            messages: [
                {
                    role: 'user',
                    content: [
                        { type: 'text', text: caseContext(input), cache_control: { type: 'ephemeral' } },
                        { type: 'text', text: `<draft_docs_change>\n${input.draftDiff}\n</draft_docs_change>` },
                    ],
                },
            ],
            output_config: { effort: config.effort, format: betaZodOutputFormat(VerdictSchema) },
        })
        if (response.stop_reason === 'refusal') throw new JudgeError(`${response.model} refused`)
        if (response.stop_reason === 'max_tokens') throw new JudgeError(`hit max_tokens ${config.maxTokens}`)
        if (response.model !== config.model) throw new JudgeError(`served by ${response.model}, not ${config.model}`)
        if (response.parsed_output === null) throw new JudgeError('the verdict did not parse')
        return response.parsed_output
    }
}
