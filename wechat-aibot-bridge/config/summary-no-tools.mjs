/** Execution-level isolation for the dedicated history summarizer. */
export const name = 'wecom-summary-no-tools';
export const inject = ['tools', 'agents'];

export function apply(ctx) {
  if (typeof ctx.tools.guard !== 'function') {
    throw new Error('History summarization requires tools.guard; refusing an unguarded runtime');
  }
  ctx.tools.guard(() => 'History summarization is text-only; all tool execution is denied.');
  ctx.on('agent/created', ({ agent }) => {
    agent.ctx.tools.restrict({ allow: [] });
  });
}
