async (page) => {
  const fixture = __EMPTY_FIXTURE_HTML__;
  const results = [];
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const run = async (name, fn) => { try { await fn(); results.push({name, passed:true}); }
    catch (error) { results.push({name, passed:false, error:error.message}); } };
  const child = '<meta charset="utf-8"><style>body{margin:20px}a{display:block;width:180px;height:55px;background:green}</style><a id="frame-empty" href="/action?operation=collect"></a><script>document.querySelector("a").onclick=e=>{e.preventDefault();parent.postMessage({frameClick:location.hostname,trusted:e.isTrusted},"*")}</script>';
  await page.context().route('http://empty-audit.test/**', route => route.fulfill({contentType:'text/html; charset=utf-8',body:route.request().url().endsWith('/frame')?child:fixture}));
  await page.context().route('http://empty-child.test/**', route => route.fulfill({contentType:'text/html; charset=utf-8',body:child}));
  await page.goto('http://empty-audit.test/');
  await page.frameLocator('#cross-frame').locator('#frame-empty').waitFor({state:'attached'});
  await page.evaluate(()=>window.addEventListener('message',e=>{if(e.data.frameClick)auditEvents.push(e.data)}));
  const worker=page.context().serviceWorkers()[0];
  check(await worker.evaluate(()=>typeof safeLinkHint === 'function'), 'Stale extension runtime');
  const panel=await page.context().newPage();
  await panel.goto(worker.url().replace('background.js','sidepanel.html'));
  const observe=async()=>{
    const state=await worker.evaluate(async()=>{
      const tab=(await chrome.tabs.query({})).find(t=>t.url==='http://empty-audit.test/');
      return (await handleAgentObserve(tab.id,true)).pageState;
    });
    return panel.evaluate(state=>AgentObservation.annotate(state),state);
  };
  const execute=index=>worker.evaluate(async index=>{
    const tab=(await chrome.tabs.query({})).find(t=>t.url==='http://empty-audit.test/');
    return handleAgentExecute(tab.id,{type:'click',index});
  },index);
  await run('empty links retained; decoration excluded; route hints contain no credentials',async()=>{
    const state=await observe();
    for(const id of ['control-a','control-b','control-c'])check(state.interactive_elements.some(e=>e.html_id===id),'missing '+id);
    for(const id of ['decor-digit','decor-search'])check(!state.interactive_elements.some(e=>e.html_id===id),'decoration '+id);
    check(!JSON.stringify({...state,screenshot:''}).includes('TEST_CREDENTIAL'),'credential leaked');
    check(state.interactive_elements.find(e=>e.html_id==='control-a').text==='','hint became label');
  });
  await run('caption metadata truthful; screenshot targets tab even while panel is active',async()=>{
    const state=await observe();
    const imageSize=await panel.evaluate(async data=>{const img=new Image();img.src=data;await img.decode();return {w:img.width,h:img.height};},state.screenshot);
    check(state.screenshot_marked && imageSize.w>0,'missing annotated screenshot');
    for(const id of ['control-a','control-b','control-c'])check(state.screenshot_mark_ids.includes(state.interactive_elements.find(e=>e.html_id===id).id),'missing mark '+id);
    check(state.text_content_summary.includes('操作中心'),'captured wrong tab');
  });
  for(const zoom of [1,1.25,1.5,2])await run('zoom '+zoom+' matches CSS geometry and clicks exactly once',async()=>{
    await worker.evaluate(async zoom=>{const tab=(await chrome.tabs.query({})).find(t=>t.url==='http://empty-audit.test/');await chrome.tabs.setZoom(tab.id,zoom);},zoom);
    await page.locator('#control-a').scrollIntoViewIfNeeded();
    const state=await observe(),el=state.interactive_elements.find(e=>e.html_id==='control-a');
    const actual=await page.locator('#control-a').boundingBox();
    check(Math.abs(el.bounding_box.x-actual.x)<2 && Math.abs(el.bounding_box.y-actual.y)<2,'geometry mismatch');
    const before=await page.evaluate(()=>auditEvents.length);
    check((await execute(el.id)).success,'click failed');
    check(await page.evaluate(before=>auditEvents.length===before+1 && auditEvents.at(-1).id==='control-a' && auditEvents.at(-1).trusted,before),'event mismatch');
  });
  await worker.evaluate(async()=>{const tab=(await chrome.tabs.query({})).find(t=>t.url==='http://empty-audit.test/');await chrome.tabs.setZoom(tab.id,1);});
  for(const [selector,host] of [['#same-frame','empty-audit.test'],['#cross-frame','empty-child.test']])await run('frame annotation and click '+host,async()=>{
    await page.locator(selector).scrollIntoViewIfNeeded();
    const state=await observe();
    const el=await worker.evaluate(async({els,cross})=>{
      const tab=(await chrome.tabs.query({})).find(t=>t.url==='http://empty-audit.test/');
      const map=(await loadTabState(STATE_KEYS.indexMap,tab.id)).map;
      return els.find(e=>!!map[e.id].sessionId===cross);
    },{els:state.interactive_elements.filter(e=>e.html_id==='frame-empty'),cross:host==='empty-child.test'});
    // 独立用两份 DOM 矩形及 iframe 边框计算；Playwright 的 OOPIF boundingBox 在此版本漏计边框。
    const frameRect=await page.locator(selector).evaluate(e=>{const r=e.getBoundingClientRect();return {x:r.x+e.clientLeft,y:r.y+e.clientTop};});
    const childRect=await page.frameLocator(selector).locator('#frame-empty').evaluate(e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y};});
    const rect={x:frameRect.x+childRect.x,y:frameRect.y+childRect.y};
    check(state.screenshot_mark_ids.includes(el.id),'frame mark missing');
    check(Math.abs(el.bounding_box.x-rect.x)<2 && Math.abs(el.bounding_box.y-rect.y)<2,'frame position mismatch');
    check((await execute(el.id)).success,'frame click failed');
    await page.waitForFunction(host=>auditEvents.some(e=>e.frameClick===host&&e.trusted),host,{timeout:3000});
  });
  await run('invalid screenshot keeps an explicit unmarked fallback',async()=>{
    const fallback=await panel.evaluate(()=>AgentObservation.annotate({screenshot:'data:image/png;base64,invalid',viewport:{width:520,height:900},interactive_elements:[]}));
    check(!fallback.screenshot_marked && !fallback.screenshot_mark_ids.length,'false mark acknowledgement');
  });
  await run('reordered identical-link controls retain separate visual marks',async()=>{
    await page.evaluate(()=>{
      const parent=document.querySelector('.actions');parent.prepend(document.getElementById('control-c'));
      for(const el of parent.querySelectorAll('a'))el.setAttribute('href','/go?token=TEST_CREDENTIAL');
      scrollTo(0,0);
    });
    const current=await observe();
    const controls=current.interactive_elements.filter(e=>['control-a','control-b','control-c'].includes(e.html_id));
    check(controls.length===3 && controls.every(e=>e.text===''&&e.target_hint==='go'&&current.screenshot_mark_ids.includes(e.id)),'ambiguous control identity lost');
  });
  await page.evaluate(()=>{scrollTo(0,0);document.getElementById('result').textContent='尚未操作';auditEvents=[];});
  const state=await observe();
  await panel.close();
  // 合成观察仅存入隔离页面内存，供后续模型选点测试读取，不触碰真实会话库。
  await page.evaluate(state=>window.auditObservation=state,state);
  return {total:results.length,passed:results.filter(r=>r.passed).length,results};
}
