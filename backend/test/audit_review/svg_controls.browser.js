async (page) => {
  const fixture = __SVG_FIXTURE_HTML__;
  const results = [];
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const run = async (name, fn) => {
    try { await fn(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, error: error.message }); }
  };
  const frameHtml = '<!doctype html><meta charset="utf-8"><body style="height:1200px;margin:0">' +
    '<button id="frame-control" style="position:absolute;left:30px;top:200px">子页面按钮</button>' +
    '<script>document.querySelector("button").onclick=e=>parent.postMessage({auditFrameClick:{id:location.hostname,trusted:e.isTrusted}},"*")</script>';
  await page.context().route('http://svg-audit.test/**', route => route.fulfill({
    contentType: 'text/html; charset=utf-8', body: route.request().url().endsWith('/frame') ? frameHtml : fixture,
  }));
  await page.context().route('http://svg-child.test/**', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: frameHtml }));
  await page.goto('http://svg-audit.test/');
  await page.frameLocator('#cross-frame').locator('#frame-control').waitFor({ state: 'attached' });
  const worker = page.context().serviceWorkers()[0];
  check(await worker.evaluate(() => typeof runtimeValue === 'function' && doClick.length === 7), 'Stale extension runtime: reload the unpacked extension or use a fresh profile');
  const observe = () => worker.evaluate(async () => {
    const tab = (await chrome.tabs.query({ active: true }))[0];
    return (await handleAgentObserve(tab.id)).pageState;
  });
  const execute = index => worker.evaluate(async index => {
    const tab = (await chrome.tabs.query({ active: true }))[0];
    return handleAgentExecute(tab.id, { type: 'click', index });
  }, index);
  const click = async (id, name = null) => {
    const state = await observe();
    const el = state.interactive_elements.find(e => e.html_id === id && (!name || e.text === name));
    check(el, `Missing candidate: ${id}`);
    return execute(el.id);
  };
  const count = id => page.evaluate(id => auditClicks.filter(e => e.id === id).length, id);
  const overlay = async (id, partial = false) => page.evaluate(({ id, partial }) => {
    document.getElementById('test-overlay')?.remove();
    const r = document.getElementById(id).getBoundingClientRect();
    const div = document.createElement('div');
    div.id = 'test-overlay';
    div.style.cssText = `position:fixed;z-index:2147483647;background:#f00;left:${r.x + (partial ? r.width * .35 : -2)}px;top:${r.y - 2}px;width:${r.width * (partial ? .65 : 1) + 4}px;height:${r.height + 4}px`;
    document.body.append(div);
  }, { id, partial });

  await run('inline wrapper named and decorative SVG deduplicated', async () => {
    const state = await observe();
    const el = state.interactive_elements.find(e => e.html_id === 'direct');
    check(el?.text === '签到', JSON.stringify(el));
    const detail = await worker.evaluate(async () => {
      const tab = (await chrome.tabs.query({ active: true }))[0];
      const { built } = await gatherAndConstructTarget({ tabId: tab.id }, {}, { x: 0, y: 0 });
      const n = built.allNodes.find(n => n.attributes.id === 'direct');
      return { visible: n.isVisible, snapshotClick: n.snapshot.isClickable, bounds: n.snapshot.bounds, client: n.snapshot.clientRects,
        svg: n.children.find(c => c.nodeName.toLowerCase() === 'svg').backendNodeId };
    });
    check(detail.visible && detail.snapshotClick, JSON.stringify(detail));
    check(!state.interactive_elements.some(e => e.backend_node_id === detail.svg), 'Decorative SVG still numbered');
  });
  await run('hidden, disabled and title-only elements excluded', async () => {
    const state = await observe();
    for (const id of ['hint', 'hidden', 'transparent', 'disabled']) {
      check(!state.interactive_elements.some(e => e.html_id === id), `Unexpected candidate: ${id}`);
    }
  });
  for (const [id, label] of [['direct', '签到'], ['pointer', '指针签到'], ['svg-only', '图标签到'], ['link', '设置'], ['independent', '独立操作']]) {
    await run(`real click once: ${id}`, async () => {
      const before = await count(id);
      const result = await click(id, label);
      check(result.success, JSON.stringify(result));
      check(await count(id) === before + 1, `Event count mismatch: ${id}`);
      check(await page.evaluate(id => auditClicks.filter(e => e.id === id).at(-1).trusted, id), 'Synthetic event');
    });
  }
  await run('independent child does not trigger parent handler', async () => {
    check(await count('parent') === 0, 'Parent handler triggered');
    const state = await observe();
    check(state.interactive_elements.some(e => e.html_id === 'parent'), 'Parent action was discarded');
  });
  await run('parent click avoids its independent child action', async () => {
    const parentBefore = await count('parent'), childBefore = await count('independent');
    const result = await click('parent');
    check(result.success, JSON.stringify(result));
    check(await count('parent') === parentBefore + 1, 'Parent handler did not run');
    check(await count('independent') === childBefore, 'Parent click triggered child action');
  });
  await run('full overlay blocks SVG without JS fallback', async () => {
    const state = await observe();
    const el = state.interactive_elements.find(e => e.html_id === 'svg-only');
    const before = await count('svg-only');
    await overlay('svg-only');
    try {
      const result = await execute(el.id);
      check(!result.success, JSON.stringify(result));
      check(await count('svg-only') === before, 'Click crossed overlay');
    } finally { await page.evaluate(() => document.getElementById('test-overlay')?.remove()); }
  });
  await run('partially covered control uses a free point and clicks once', async () => {
    const state = await observe();
    const el = state.interactive_elements.find(e => e.html_id === 'partial');
    const before = await count('partial');
    await overlay('partial', true);
    try {
      const result = await execute(el.id);
      check(result.success, JSON.stringify(result));
      check(await count('partial') === before + 1, 'Partial overlay event count mismatch');
    } finally { await page.evaluate(() => document.getElementById('test-overlay')?.remove()); }
  });
  await run('delayed checkbox update does not cause a second click', async () => {
    const result = await click('checkbox');
    check(result.success, JSON.stringify(result));
    await page.waitForFunction(() => checkbox.checked);
    check(await count('checkbox') === 1, 'Checkbox clicked twice');
  });
  for (const zoom of [1, 1.25, 1.5, 2]) {
    await run(`scroll and zoom ${zoom}: geometry and click`, async () => {
      await worker.evaluate(async zoom => {
        const tab = (await chrome.tabs.query({ active: true }))[0];
        await chrome.tabs.setZoom(tab.id, zoom);
      }, zoom);
      await page.locator('#scrolled').scrollIntoViewIfNeeded();
      const rect = await page.locator('#scrolled').boundingBox();
      const state = await observe();
      const el = state.interactive_elements.find(e => e.html_id === 'scrolled');
      check(el, 'Scrolled control missing');
      check(Math.abs(el.bounding_box.x - rect.x) <= 1 && Math.abs(el.bounding_box.y - rect.y) <= 1,
        `Geometry mismatch: ${JSON.stringify({ actual: rect, observed: el.bounding_box })}`);
      const before = await count('scrolled');
      const result = await execute(el.id);
      check(result.success && await count('scrolled') === before + 1, JSON.stringify(result));
    });
  }
  for (const zoom of [1, 1.5]) {
    await worker.evaluate(async zoom => {
      const tab = (await chrome.tabs.query({ active: true }))[0];
      await chrome.tabs.setZoom(tab.id, zoom);
    }, zoom);
    for (const [selector, hostname, cross] of [['#same-frame', 'svg-audit.test', false], ['#cross-frame', 'svg-child.test', true]]) {
      await run(`iframe ${hostname} zoom ${zoom}: geometry and click`, async () => {
        await page.locator(selector).scrollIntoViewIfNeeded();
        await page.frameLocator(selector).locator('#frame-control').scrollIntoViewIfNeeded();
        const state = await observe();
        const selected = await worker.evaluate(async ({ cross, candidates }) => {
          const tab = (await chrome.tabs.query({ active: true }))[0];
          const st = await loadTabState(STATE_KEYS.indexMap, tab.id);
          return candidates.find(e => !!st.map[e.id].sessionId === cross);
        }, { cross, candidates: state.interactive_elements.filter(e => e.html_id === 'frame-control') });
        check(selected, `Frame candidate missing: ${hostname}`);
        const before = await count(hostname);
        const result = await execute(selected.id);
        check(result.success, JSON.stringify(result));
        await page.waitForFunction(({ hostname, before }) => auditClicks.filter(e => e.id === hostname).length === before + 1,
          { hostname, before }, { timeout: 3000 });
      });
    }
  }
  await worker.evaluate(async () => {
    const tab = (await chrome.tabs.query({ active: true }))[0];
    await chrome.tabs.setZoom(tab.id, 1);
  });
  await run('parent-page overlay blocks cross-origin iframe click', async () => {
    await page.locator('#cross-frame').scrollIntoViewIfNeeded();
    await page.frameLocator('#cross-frame').locator('#frame-control').scrollIntoViewIfNeeded();
    const state = await observe();
    const index = await worker.evaluate(async candidates => {
      const tab = (await chrome.tabs.query({ active: true }))[0];
      const st = await loadTabState(STATE_KEYS.indexMap, tab.id);
      return candidates.find(e => st.map[e.id].sessionId)?.id;
    }, state.interactive_elements.filter(e => e.html_id === 'frame-control'));
    check(index, 'Cross-frame candidate missing');
    await overlay('cross-frame');
    const before = await count('svg-child.test');
    try {
      const result = await execute(index);
      check(!result.success && await count('svg-child.test') === before, JSON.stringify(result));
    } finally { await page.evaluate(() => document.getElementById('test-overlay')?.remove()); }
  });
  for (const change of ['disabled', 'hidden']) {
    await run(`state changes after observation: ${change}`, async () => {
      await page.locator('#partial').scrollIntoViewIfNeeded();
      const state = await observe();
      const el = state.interactive_elements.find(e => e.html_id === 'partial');
      const before = await count('partial');
      await page.evaluate(change => {
        const button = document.getElementById('partial');
        if (change === 'disabled') button.disabled = true;
        else button.style.display = 'none';
      }, change);
      try {
        const result = await execute(el.id);
        check(!result.success && await count('partial') === before, JSON.stringify(result));
      } finally {
        await page.evaluate(() => { const b = document.getElementById('partial'); b.disabled = false; b.style.display = ''; });
      }
    });
  }
  await run('open shadow-root SVG control uses a real click', async () => {
    await page.evaluate(() => {
      const host = document.createElement('div'); host.id = 'shadow-test';
      host.attachShadow({ mode: 'open' }).innerHTML = '<button id="shadow-control" title="阴影按钮"><svg width="24" height="24"><rect width="24" height="24"/></svg></button>';
      host.shadowRoot.querySelector('button').onclick = event => auditClicks.push({ id: 'shadow-control', trusted: event.isTrusted });
      document.body.prepend(host);
      host.scrollIntoView();
    });
    const result = await click('shadow-control');
    check(result.success && await count('shadow-control') === 1, JSON.stringify(result));
  });
  await run('display:contents wrapper labels its visible SVG', async () => {
    await page.evaluate(() => {
      const wrapper = document.createElement('span');
      wrapper.style.display = 'contents'; wrapper.title = '无框签到';
      wrapper.innerHTML = '<svg id="contents-svg" class="iconpark-icon"><use href="#plan"/></svg>';
      wrapper.onclick = event => auditClicks.push({ id: 'contents-svg', trusted: event.isTrusted });
      document.body.prepend(wrapper); scrollTo(0, 0);
    });
    const result = await click('contents-svg', '无框签到');
    check(result.success && await count('contents-svg') === 1, JSON.stringify(result));
  });
  return { total: results.length, passed: results.filter(r => r.passed).length, results };
}
