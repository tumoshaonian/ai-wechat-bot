// Real DOM/renderer/event client, simulated SSE and API. No production network.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({ headless: true, channel: 'msedge' });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.clock.install();
    await page.addInitScript(() => {
      window.streams = [];
      window.EventSource = class {
        constructor(url) { this.url = url; this.handlers = {}; window.streams.push(this); }
        addEventListener(name, fn) { this.handlers[name] = fn; }
        close() {}
        send(name, data) { this.handlers[name]?.({ data: JSON.stringify(data), lastEventId: String(data.seq || 0) }); }
      };
    });
    let requests = 0, version = 1, failing = false, hold = null, held = null;
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const assets = path.resolve(__dirname, '../src/wechat_agent/admin/static');
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), endpoint = url.pathname.replace('/api/admin/v1', '');
      if (url.pathname.startsWith('/api/')) {
        let body = {};
        if (endpoint === '/setup/status') body = { setup_required: false };
        if (endpoint === '/auth/me') body = { csrf_token: 'fixture', user: { username: 'test', permissions: ['tasks.read', 'configs.read', 'dashboard.read'], roles: ['viewer'] } };
        if (endpoint === '/tasks') {
          requests++;
          if (hold) { held = route; return; }
          if (failing) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"offline"}' });
          body = { items: Array.from({ length: 25 }, (_, i) => ({ id: `t${i}`, request_summary: `任务 ${i} v${version}`, status: 'RUNNING' })), total: 25 };
        }
        if (endpoint === '/tasks/t0') body = { id: 't0', request_summary: '详情任务', status: 'RUNNING', events: [] };
        if (endpoint === '/config-profiles') body = { items: [], total: 0 };
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) });
      }
      const name = path.basename(url.pathname) || 'index.html';
      if (!['index.html', 'app.js', 'api.js', 'styles.css'].includes(name)) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: await fs.readFile(path.join(assets, name)) });
    });
    await page.goto('http://fixture.invalid/#tasks');
    await page.getByText('任务 0 v1', { exact: true }).waitFor({ timeout: 5000 }).catch(async e => {
      console.log({ errors, requests, body: await page.locator('body').innerText() }); throw e;
    });
    assert.equal(requests, 1);
    assert.match(await page.evaluate(() => streams[0].url), /tail=true/);
    await page.evaluate(() => {
      window.inputNode = document.querySelector('[name=q]');
      window.formNode = inputNode.closest('form');
      window.flashes = 0;
      new MutationObserver(() => {
        if (document.querySelector('#page-content .skeleton')) window.flashes++;
      }).observe(document.querySelector('#page-content'), { childList: true, subtree: true });
      streams[0].send('cursor', { seq: 10000 });
      for (let i = 1; i <= 1000; i++) streams[0].send('task.progress', { seq: i, event_type: 'task.progress' });
    });
    await page.clock.runFor(5500); // A single snapshot reconciliation, not history replay.
    await page.waitForFunction(() => document.querySelectorAll('[data-action="view-task"]').length > 0);
    assert.equal(requests, 2);
    const send = async (kind, count = 1) => page.evaluate(({ kind, count }) => {
      window.seq = Math.max(window.seq || 10000, 10000);
      for (let i = 0; i < count; i++) streams[0].send(kind, { seq: ++window.seq, event_type: kind });
    }, { kind, count });
    await send('log.created', 1000); await send('connection.heartbeat', 100);
    await page.clock.runFor(6000);
    assert.equal(requests, 2, 'irrelevant events must not refresh tasks');
    await page.locator('[name=q]').fill('尚未提交的搜索');
    version = 2;
    await send('task.progress', 200);
    await page.clock.runFor(6000);
    assert.equal(requests, 2, 'typing defers updates');
    assert.equal(await page.locator('[name=q]').inputValue(), '尚未提交的搜索');
    await page.locator('#page-content').focus();
    const scroll = await page.evaluate(() => { window.scrollTo(0, 400); return window.scrollY; });
    assert.ok(scroll > 0);
    await page.clock.runFor(5500);
    await page.getByText('任务 0 v2', { exact: true }).waitFor();
    assert.equal(requests, 3);
    assert.equal(await page.evaluate(() => window.scrollY), scroll, 'background updates preserve scroll');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'page-content');
    assert.equal(await page.evaluate(() => inputNode === document.querySelector('[name=q]') && formNode === inputNode.closest('form')), true);
    assert.equal(await page.locator('[name=q]').inputValue(), '尚未提交的搜索');
    await page.locator('[data-action="view-task"]').first().click();
    await page.getByText('详情任务', { exact: true }).first().waitFor();
    await send('task.completed', 100); await page.clock.runFor(6000);
    assert.equal(requests, 3, 'drawer must not be interrupted');
    await page.locator('#drawer-close').click(); await page.locator('#page-content').focus();
    await page.clock.runFor(5500);
    await page.evaluate(() => {});
    const baseline = requests;
    failing = true;
    await send('task.progress'); await page.clock.runFor(5500);
    await page.getByText('更新暂不可用', { exact: true }).waitFor();
    assert.ok((await page.locator('#page-content').innerText()).includes('任务 0 v2'));
    assert.equal(await page.evaluate(() => flashes), 0, 'no loading skeleton during automatic updates');
    failing = false;
    // In-flight refresh must not be replaced/aborted by the next event burst.
    hold = true;
    await page.clock.runFor(5500);
    await page.waitForFunction(() => true);
    assert.ok(held);
    const inFlightCount = requests;
    await send('task.progress', 100); await page.clock.runFor(6000);
    assert.equal(requests, inFlightCount);
    // Navigating away invalidates the pending response and all its refresh timers.
    await page.evaluate(() => { location.hash = '#configs'; });
    await page.getByText('尚未创建 Agent 配置', { exact: true }).waitFor();
    await held.fulfill({ contentType: 'application/json', body: '{"items":[],"total":0}' }).catch(() => {});
    held = null; hold = null;
    await page.clock.runFor(6000);
    assert.ok((await page.locator('#page-content').innerText()).includes('尚未创建 Agent 配置'));
    assert.deepEqual(errors, []);
    await page.evaluate(() => streams[0].onerror());
    await page.clock.runFor(1500);
    assert.equal(await page.evaluate(() => streams.length), 2);
    assert.match(await page.evaluate(() => streams[1].url), /tail=false/);
    assert.match(await page.evaluate(() => streams[1].url), new RegExp(`after=${await page.evaluate(() => window.seq)}`));
    console.log(JSON.stringify({ checks: 'history/duplicates, filtering, coalescing, drafts/focus/scroll, drawer, failures, in-flight/navigation, reconnect cursor', requests, baseline, errors }));
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
