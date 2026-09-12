// Isolated browser fixture: no production API, credentials, or WeCom traffic.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const assets = path.resolve(__dirname, '../src/wechat_agent/admin/static');
  const task = {
    id: 'fixture-task', trace_id: 'fixture-trace', request_summary: '生成文档并发送',
    status: 'PARTIAL_SUCCEEDED', duration_ms: 2100, result_summary: '文档已生成',
    error_code: 'FINAL_RESPONSE_SEND_FAILED', error_message: '回复回执丢失，请勿自动重做任务。',
    outcome: { execution_state: 'succeeded', response_status: 'unknown', failure_stage: 'response' },
    deliveries: [{ id: 'fixture-delivery', status: 'SENT' }], tool_calls: [], events: [
      { event_type: 'task.policy', payload: { config_revision_id: 'fixture-policy', policy: { default_action: 'ask' } } },
      { event_type: 'task.waiting', payload: { confirmation_id: 'fixture-confirmation', operation: { action: '<img src=x onerror=alert(1)>', effect: 'fixture-operation' } } },
      { event_type: 'confirmation.resolved', payload: { confirmation_id: 'fixture-confirmation', status: 'approved', decided_by: 'fixture-owner' } },
    ],
  };
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const endpoint = url.pathname.replace('/api/admin/v1', '');
      if (url.pathname.startsWith('/api/')) {
        let body = {};
        if (endpoint === '/setup/status') body = { setup_required: false };
        if (endpoint === '/auth/me') body = { csrf_token: 'fixture-only', user: { username: 'test', permissions: ['tasks.read'], roles: ['viewer'] } };
        if (endpoint === '/tasks') body = { items: [task], total: 1, page: 1, page_size: 20 };
        if (endpoint === '/tasks/fixture-task') body = task;
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      }
      const name = path.basename(url.pathname) || 'index.html';
      if (!['index.html', 'app.js', 'api.js', 'styles.css'].includes(name)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(assets, name)) });
    });
    await page.goto('http://fixture.invalid/#tasks');
    await page.locator('[data-action="view-task"]').first().click();
    await page.locator('#drawer-body').getByText('Agent 执行结果', { exact: true }).waitFor();
    const content = await page.locator('#drawer-body').innerText();
    for (const value of ['部分成功', '最终回复回执', '结果未知', '文件交付回执', '已发送', '文档已生成']) assert.ok(content.includes(value), value);
    for (const value of ['权限与确认记录', 'fixture-owner', 'fixture-policy', 'fixture-operation']) assert.ok(content.includes(value), value);
    assert.equal(await page.locator('#drawer-body img').count(), 0);
    assert.deepEqual(errors, []);
    await page.locator('#drawer').evaluate(async element => {
      await Promise.all(element.getAnimations({ subtree: true }).map(animation => animation.finished));
    });
    const output = path.resolve(__dirname, '../../.harness-sessions/regression-artifacts/task-outcome-ui.png');
    await fs.mkdir(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output, fullPage: true });
    console.log(JSON.stringify({ checked: 'execution vs reply vs delivery', pageErrors: errors, screenshot: output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
