/** Test-only direct execution probe; no model or real side effect is needed. */
import { writeFile } from 'node:fs/promises';
export const name = 'wecom-summary-guard-probe';
export const inject = ['tools'];
export async function apply(ctx) {
  let executed = false;
  ctx.tools.register({
    name: 'wecom_summary_probe',
    description: 'test-only guard probe',
    parameters: { type: 'object', properties: {} },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: async () => { executed = true; return 'unexpected execution'; },
  });
  const result = await ctx.tools.execute({
    signal: new AbortController().signal, callId: 'guard-probe',
    name: 'wecom_summary_probe', arguments: {},
  });
  await writeFile(process.env.WECOM_GUARD_PROOF, JSON.stringify({ executed, result }), 'utf8');
  if (executed) throw new Error('summary execution guard did not reject the call');
}
