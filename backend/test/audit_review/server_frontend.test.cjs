const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../..');
const panel = fs.readFileSync(path.join(root,'extension/sidepanel.js'),'utf8');
const background = fs.readFileSync(path.join(root,'extension/background.js'),'utf8');
function extract(source,start,end) {
  const a=source.indexOf(start),b=source.indexOf(end,a);
  assert.ok(a>=0&&b>a); return source.slice(a,b);
}
function element() { return {dataset:{},children:[],appendChild(node){this.children.push(node);},addEventListener(){}}; }

async function panelCase({meta=true,error=false,retry=null}={}) {
  const nodes=[],sent=[],recovery=[],timeouts=[];
  const listeners=new Set(); let id=0;
  const context={window:{_kbBoundId:'kb_test'},sessionSequences:new Map(),
    getOrCreateCurrentChatId:async()=> 'chat_test',
    callBackendApi:async()=>({last_seq:10}),buildBackendEndpointUrl:(base,p)=>base+p,
    createMessageId:()=>String(++id),document:{createElement:element},
    createMessageNode:role=>{const node=element();node.role=role;nodes.push(node);return node;},
    createMarkdownStreamer:()=>({update(){},finalize(){},cancel(){}}),
    scrollToBottom(){},renderSearchCitations(){},updateEnhancementCard(){},
    addRequestRecovery:(...args)=>recovery.push(args),
    setTimeout:(fn,ms)=>{timeouts.push(ms);return 1;},clearTimeout(){},
    chrome:{runtime:{onMessage:{addListener:fn=>listeners.add(fn),removeListener:fn=>listeners.delete(fn)},
      sendMessage(request,callback){
        sent.push(JSON.parse(request.options.body));
        queueMicrotask(()=>{
          const emit=message=>{for(const listener of [...listeners])listener({msgId:request.msgId,...message});};
          emit({type:'LLM_CHUNK',chunk:'answer'});
          if(meta)emit({type:'LLM_SESSION_META',session_meta:{persisted:true,last_seq:12}});
          emit(error?{type:'LLM_ERROR',error:'test failure'}:{type:'LLM_DONE'});
          callback();
        });
      }}}};
  vm.createContext(context);
  vm.runInContext(extract(panel,'  async function runServerChat(','  // ── 会话历史'),context);
  await context.runServerChat('only current',null,'',{apiKey:'',modelName:'test',safeApiUrl:'http://127.0.0.1:8000/v1'},retry);
  return {nodes,sent,recovery,timeouts,sequences:context.sessionSequences,listeners};
}
test('managed panel sends current input only and advances sequence only on persistence ack',async()=>{
  const r=await panelCase();assert.equal(r.sent[0].messages.length,1);
  assert.equal(r.sent[0].messages[0].content,'only current');
  assert.equal(r.sent[0].expected_last_seq,10);assert.equal(r.sent[0].context_mode,'server');
  assert.equal(r.sequences.values().next().value,12);assert.equal(r.recovery.length,0);
  assert.equal(r.listeners.size,0);assert.deepEqual(r.timeouts,[605000]);
});
for(const options of [{meta:false},{error:true}])test('unconfirmed or failed completion offers status recovery '+JSON.stringify(options),async()=>{
  const r=await panelCase(options);assert.equal(r.recovery.length,1);
  assert.equal(r.sequences.values().next().value,10);
});
test('retry reuses exact request and does not append another user bubble',async()=>{
  const retry={context_mode:'server',chat_id:'chat_test',request_id:'old',expected_last_seq:8,model:'test',stream:false,messages:[{role:'user',content:'original'}]};
  const r=await panelCase({retry});assert.deepEqual(r.sent[0],retry);
  assert.equal(r.nodes.filter(n=>n.role==='user').length,0);
});

async function backgroundCase(jsonResponse,frames) {
  const sent=[],timeouts=[];
  const context={URL,TextDecoder,MAX_LLM_BODY_BYTES:1000000,isAllowedChatUrl:async()=>true,isPrivateOrLocalHost:()=>true,
    chrome:{runtime:{sendMessage:message=>sent.push(message)}},
    AbortSignal:{timeout:ms=>{timeouts.push(ms);}},
    fetch:async()=>({ok:true,headers:{get:()=>jsonResponse?'application/json':'text/event-stream'},json:async()=>jsonResponse,
      body:{getReader:()=>({cancel:async()=>{},read:async()=>frames.length?{done:false,value:Buffer.from(frames.shift())}:{done:true}})}})};
  vm.createContext(context);
  vm.runInContext(extract(background,'function sendLlmMessage(','async function getResponseErrorMessage('),context);
  await context.handleCallLlmStream({url:'http://127.0.0.1:8000/v1/chat/completions',msgId:'x',options:{method:'POST',headers:{},body:JSON.stringify({context_mode:'server',stream:true})}});
  return {sent,timeouts};
}
test('non SSE replay forwards persistence and tool metadata before done',async()=>{
  const r=await backgroundCase({choices:[{message:{content:'replay'}}],session_meta:{persisted:true},enhancement_steps:[{status:'done'}]},[]);
  assert.deepEqual(r.sent.map(m=>m.type),['LLM_SESSION_META','LLM_ENHANCEMENT_STEP','LLM_CHUNK','LLM_DONE']);
  assert.deepEqual(r.timeouts,[600000]);
});
test('SSE failure is not mistaken for done',async()=>{
  const r=await backgroundCase(null,['data: {"error":{"code":"history_unavailable"},"session_meta":{"persisted":false}}\n\n','data: [DONE]\n\n']);
  assert.deepEqual(r.sent.map(m=>m.type),['LLM_SESSION_META','LLM_ERROR']);
});
