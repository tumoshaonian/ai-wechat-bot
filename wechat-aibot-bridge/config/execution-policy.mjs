/** Fail-closed per-dispatch policy. Trusted plugins/OS access are outside this boundary. */
export const name = 'wecom-execution-policy';
export const inject = ['tools', 'agents'];

export function apply(ctx) {
  if (typeof ctx.tools.guard !== 'function') throw new Error('Execution policy requires tools.guard');
  const url = process.env.DSH_EXECUTION_POLICY_URL;
  const token = process.env.DSH_DELIVERY_TOKEN;
  const nonce = process.env.DSH_EXECUTION_POLICY_NONCE;
  if (!url?.startsWith('http://127.0.0.1:') || !token || !nonce) {
    throw new Error('Execution policy broker is required');
  }
  const roots = new Map();
  const proofs = new WeakMap();
  ctx.on('agent/created', ({ agent }) => {
    const session = agent.session;
    const parent = session.header?.parentSession;
    roots.set(session.id, parent ? (roots.get(parent) ?? null) : session.id);
  });
  const fingerprint = exec => JSON.stringify([
    exec.agent?.session?.id, exec.callId, exec.name, exec.arguments,
  ]);
  const deny = () => ({ kind: 'deny', reason: 'EXECUTION_NOT_APPROVED: 本次工具调用未获执行授权，请勿绕过或自动重试。' });
  // A later waterfall listener cannot undo a missing proof: the monotonic guard checks it.
  ctx.on('tools/pre-execute', async (exec, next) => {
    const prior = await next();
    if (prior.kind !== 'allow') return prior;
    const session = exec.agent?.session?.id;
    const root = roots.get(session);
    if (!root || exec.signal.aborted) return deny();
    const before = fingerprint(exec);
    try {
      const response = await fetch(url, {
        method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        signal: AbortSignal.any([exec.signal, AbortSignal.timeout(105000)]),
        body: JSON.stringify({ runtime_nonce: nonce, session_id: root,
          caller_session_id: session, call_id: exec.callId, tool: exec.name, arguments: exec.arguments }),
      });
      const result = response.ok ? await response.json() : null;
      if (result?.approved !== true || exec.signal.aborted || fingerprint(exec) !== before) return deny();
      proofs.set(exec, before);
      return prior;
    } catch {
      return deny();
    }
  });
  ctx.tools.guard(exec => proofs.get(exec) === fingerprint(exec)
    ? undefined : 'EXECUTION_NOT_APPROVED: no exact live dispatch approval');
  ctx.on('tools/execute', async (exec, next) => {
    if (exec.signal.aborted || proofs.get(exec) !== fingerprint(exec)) {
      return { isError: true, content: [{ type: 'text', text: 'EXECUTION_NOT_APPROVED: changed or cancelled dispatch' }] };
    }
    // Consume before entering the body, including on failure. Never grant retries.
    proofs.delete(exec);
    return next();
  });
}
