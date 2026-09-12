// Isolated browser form contract: no production credentials or network traffic.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const assets = path.resolve(__dirname, '../src/wechat_agent/admin/static');
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [], posts = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), endpoint = url.pathname.replace('/api/admin/v1', '');
      if (url.pathname.startsWith('/api/')) {
        let body = {};
        if (endpoint === '/setup/status') body = { setup_required: false };
        if (endpoint === '/auth/me') body = { csrf_token: 'fixture', user: { username: 'test', permissions: ['configs.read', 'configs.write', 'configs.publish'], roles: ['admin'] } };
        if (endpoint === '/config-profiles') body = { items: [{ id: 'profile', name: '隔离测试配置' }], total: 1 };
        if (endpoint === '/config-profiles/profile/revisions' && route.request().method() === 'POST') {
          posts.push(route.request().postDataJSON()); body = { id: 'revision', version: 1 };
        }
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      }
      const name = path.basename(url.pathname) || 'index.html';
      if (!['index.html', 'app.js', 'api.js', 'styles.css'].includes(name)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(assets, name)) });
    });
    await page.goto('http://fixture.invalid/#configs');
    await page.locator('[data-action="new-config-revision"]').first().click();
    await page.locator('[name="model"]').fill('test-model');
    await page.locator('[name="system_prompt"]').fill('test prompt');
    await page.locator('[name="confirmation_timeout"]').fill('30');
    await page.locator('[name="policy_rules"]').fill('bash=allow');
    await page.locator('[data-modal-confirm]').click();
    await page.getByText('规则格式无效、工具重复或试图修改确认通道。', { exact: true }).waitFor({ timeout: 3000 }).catch(async e => {
      console.log(await page.locator('#modal').innerText(), await page.locator('#toast-region').innerText()); throw e;
    });
    assert.equal(posts.length, 0);
    await page.locator('[name="policy_rules"]').fill('bash=deny\nmcp__desktop__invoke=ask');
    const output = path.resolve(__dirname, '../../.harness-sessions/regression-artifacts/policy-ui.png');
    await fs.mkdir(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output, fullPage: true });
    await page.locator('[data-modal-confirm]').click();
    await page.locator('#modal').waitFor({ state: 'hidden' });
    assert.equal(posts.length, 1);
    assert.equal(posts[0].tool_policy.rules.bash, 'deny');
    assert.equal(posts[0].tool_policy.confirmation_timeout_seconds, 30);
    assert.equal(posts[0].tool_policy.rules.mcp__desktop__list_windows, 'allow');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ checked: 'policy form validation and exact submitted payload', screenshot: output, errors }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
