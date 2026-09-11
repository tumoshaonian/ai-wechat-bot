/** Only used by isolated smoke tests. Side effects are confined to an in-memory counter. */
import { writeFile } from 'node:fs/promises';
export const name = 'wecom-policy-effect-probe';
export const inject = ['tools'];
export function apply(ctx) {
  let effects = 0;
  ctx.tools.register({
    name: 'policy_test_effect', description: 'For this smoke test only: increment a memory counter; no desktop operations.',
    parameters: { type: 'object', properties: { label: { type: 'string' } }, required: ['label'], additionalProperties: false },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: async () => {
      effects++;
      await writeFile(process.env.WECOM_POLICY_PROOF, JSON.stringify({ effects }), 'utf8');
      return 'virtual effect complete';
    },
  });
}
