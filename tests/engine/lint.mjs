import assert from 'node:assert/strict';
import { spawn, execFile } from 'node:child_process';
import { once } from 'node:events';
import { promisify } from 'node:util';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const work = path.resolve(process.env.PBJ_ENGINE_TMP || path.join(os.tmpdir(), 'pbj-html-deck-engine'));
const chrome = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const runFile = promisify(execFile), delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const quote = JSON.stringify;
async function eventually(fn) {
  for (let n = 0; n < 160; n++) { const value = await fn(); if (value) return value; await delay(40); }
  throw Error('Chrome 준비 시간 초과');
}
async function launch() {
  await fs.mkdir(work, { recursive: true });
  const profile = await fs.mkdtemp(path.join(work, 'chrome-'));
  const child = spawn(chrome, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--disable-background-networking', '--disable-breakpad', 'about:blank'], {
    windowsHide: true, stdio: 'ignore', env: { ...process.env, TEMP: work, TMP: work, TMPDIR: work }
  });
  let exited = false, spawnError, socket, seq = 0;
  child.once('error', error => { spawnError = error; exited = true; });
  const exit = new Promise(resolve => child.once('exit', () => { exited = true; resolve(); }));
  const pending = new Map(), errors = [], requests = [];
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq, timer = setTimeout(() => { pending.delete(id); reject(Error(`CDP 시간 초과: ${method}`)); }, 20000);
    pending.set(id, { resolve, reject, timer, method }); socket.send(JSON.stringify({ id, method, params }));
  });
  async function stop() {
    try {
      if (!exited && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ id: ++seq, method: 'Browser.close' }));
        await Promise.race([exit, once(AbortSignal.timeout(2000), 'abort')]);
      }
    } finally {
      socket?.close();
      if (!exited) {
        if (process.platform === 'win32') await runFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }).catch(() => child.kill());
        else child.kill();
        await Promise.race([exit, once(AbortSignal.timeout(4000), 'abort')]);
      }
      if (!exited) throw Error(`Chrome 종료 실패: ${child.pid}`);
      if (process.platform === 'win32') await runFile('powershell.exe', ['-NoProfile','-NonInteractive','-Command','Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($env:PBJ_TEST_PROFILE, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)'], { env: { ...process.env, PBJ_TEST_PROFILE: profile }, windowsHide: true, timeout: 20000 }).catch(error => console.error(`프로필 정리 실패: ${error.message}`));
      console.log(`Chrome 종료 확인: ${child.pid ?? '실행되지 않음'}`);
    }
  }
  try {
    const port = await eventually(async () => {
      if (exited || spawnError) throw spawnError || Error('Chrome 시작 실패');
      try { return Number((await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); } catch { return false; }
    });
    const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
    socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
    await once(socket, 'open', { signal: AbortSignal.timeout(5000) });
    socket.addEventListener('message', ({ data }) => {
      const msg = JSON.parse(data), p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); clearTimeout(p.timer); if (msg.error) p.reject(Error(`${p.method}: ${msg.error.message}`)); else p.resolve(msg.result); }
      else if (msg.method === 'Network.requestWillBeSent') requests.push(msg.params.request.url);
      else if (msg.method === 'Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text);
      else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') errors.push(msg.params.args.map(a => a.value ?? a.description).join(' '));
      else if (msg.method === 'Log.entryAdded' && msg.params.entry.level === 'error') errors.push(msg.params.entry.text);
    });
    for (const domain of ['Page','Runtime','Network','Log']) await send(`${domain}.enable`);
    await send('Network.setBlockedURLs', { urls: ['http://*','https://*'] });
    await send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false });
    return { send, stop, errors, requests };
  } catch (error) { await stop(); throw error; }
}
const cdp = await launch(), { send } = cdp;
async function js(expression, awaitPromise = false) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
  if (result.exceptionDetails) throw Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  return result.result.value;
}
async function open(file) {
  const url = pathToFileURL(path.resolve(file)).href;
  await send('Page.navigate', { url });
  await eventually(async () => { try { return await js(`location.href===${quote(url)} && document.readyState==='complete' && !!window.deck`); } catch { return false; } });
  await js('deck.ready.then(()=>true)', true);
}
let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log(`PASS ${name}`); }
  catch (error) { failed++; console.error(`FAIL ${name}\n${error.stack || error}`); }
}
async function realDeck(file) {
  // 2차 덱의 사본에 확장 연결 어댑터와 검사 코드를 붙인다. 원본은 건드리지 않는다.
  const [original, source] = await Promise.all([fs.readFile(path.resolve(file), 'utf8'), fs.readFile(path.resolve(here, '../../assets/engine/lint.js'), 'utf8')]);
  const adapter = `<script>window.lintHooks=null;deck.ext=(name,hooks)=>{if(name==='lint')window.lintHooks=hooks};\n${source}\ndocument.addEventListener('DOMContentLoaded',()=>lintHooks.init?.(),{once:true});</script>`;
  assert.match(original, /<\/body\s*>/i);
  const copy = path.join(work, `lint-real-${path.basename(file)}`);
  await fs.writeFile(copy, original.replace(/<\/body\s*>/i, () => `${adapter}</body>`));
  await open(copy);
  assert.equal(await js('!!window.lintHooks?.validate'), true, '검사 확장 미연결');
  await js("deck.setMode('present')");
  const out = await js(`(()=>{const before=performance.now(),all=[];for(let i=0;i<deck.count;i++){deck.go(i,'all');const s=deck.slides[i];for(const a of s.getAnimations({subtree:true}))try{a.finish()}catch{};const t=performance.now();const found=lintHooks.validate(s);all.push({page:i+1,title:deck.title(i),ms:performance.now()-t,issues:found.map(x=>({code:x.code,message:x.message,target:x.target?.id||x.target?.className||x.target?.localName}))});}return {elapsed:performance.now()-before,slides:all}})()`);
  console.log(`실제 덱: ${file}`);
  const baseMs = await js('(()=>{const start=performance.now();deck.audit();return performance.now()-start})()');
  const lintMs = out.slides.reduce((a,b)=>a+b.ms,0);
  console.log(`검사 시간: 이동 포함 ${out.elapsed.toFixed(1)}ms / 기존 audit ${baseMs.toFixed(1)}ms + 확장 ${lintMs.toFixed(1)}ms = ${(baseMs+lintMs).toFixed(1)}ms`);
  assert(baseMs + lintMs < 3000, '19장 검사 시간이 3초를 넘었습니다.');
  console.log(JSON.stringify(out.slides));
  for (const slide of out.slides.filter(s => s.issues.length)) {
    await js(`deck.go(${slide.page - 1},'all');for(const a of deck.slides[${slide.page - 1}].getAnimations({subtree:true}))try{a.finish()}catch{};true`);
    const { data } = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
    const name = `${path.basename(file,'.html')}-${String(slide.page).padStart(2,'0')}.png`;
    await fs.writeFile(path.join(work, name), Buffer.from(data, 'base64'));
  }
}
async function modernDeck(file) {
  const engineDir = path.resolve(here, '../../assets/engine');
  const [original, core, lint] = await Promise.all([fs.readFile(file,'utf8'), fs.readFile(path.join(engineDir,'deck.js'),'utf8'), fs.readFile(path.join(engineDir,'lint.js'),'utf8')]);
  const engine = /<script\s+data-deck="engine"[^>]*>[\s\S]*?<\/script>/i;
  assert.match(original, engine);
  assert.doesNotMatch(core + lint, /<\/script/i);
  const copy = path.join(work, 'modern-catalog.html');
  await fs.writeFile(copy, original.replace(engine, () => `<script data-deck="engine">${core}\n${lint}</script>`));
  await open(copy);
  const measured = await js(`(()=>{deck.setMode('present');const start=performance.now(),issues=deck.audit();return {ms:performance.now()-start,count:deck.count,hasLint:issues.some(i=>i.code==='no-visual')}})()`);
  assert.equal(measured.count,19);
  assert(measured.hasLint, 'audit에 검사 확장이 합쳐지지 않았습니다.');
  assert(measured.ms < 3000, `통합 audit ${measured.ms}ms`);
  console.log(`3차 엔진 통합 audit: ${measured.count}장 ${measured.ms.toFixed(1)}ms`);
}
try {
  if (process.argv[2] === '--deck') await realDeck(process.argv[3]);
  else if (process.argv[2] === '--modern') await modernDeck(process.argv[3]);
  else {
    await open(path.join(here, 'lint.html'));
    const groups = [
      ['placeholder-left','placeholder-hit',['placeholder-ok1','placeholder-ok2']],
      ['image-distorted','distorted-hit',['distorted-ok1','distorted-ok2','distorted-ok3']],
      ['image-upscaled','upscaled-hit',['upscaled-ok1','upscaled-ok2','upscaled-ok3','upscaled-ok4']],
      ['slide-is-image','whole-hit',['whole-ok1','whole-ok2','whole-ok3','whole-ok4','whole-bg-ok','whole-bg-small','whole-bg-contain']],
      ['text-heavy','heavy-hit',['heavy-ok1','heavy-ok2','heavy-ok3','heavy-ok4']],
      ['empty-space','empty-hit',['empty-ok1','empty-ok2','empty-ok3','empty-ok4','empty-ok5','empty-ok6','empty-ok7','empty-ok8']],
      ['edge-crowding','edge-hit',['edge-ok1','edge-ok2','edge-ok3']],
      ['text-on-image','onimage-hit',['onimage-ok1','onimage-ok2','onimage-ok3','onimage-ok4','onimage-ok5']],
      ['near-miss','near-hit',['near-ok1','near-ok2','near-ok3','near-ok4']],
      ['no-visual','visual-hit',['visual-ok1','visual-ok2','visual-ok3','visual-ok4','visual-ok5']],
      ['font-fallback','font-hit',['font-ok1','font-ok2','font-ok3']]
    ];
    for (const [code, hit, safe] of groups) await test(`${code}: 검출과 정상 ${safe.length}개`, async () => {
      const result = await js(`(()=>{const ids=${quote([hit,...safe])};return Object.fromEntries(ids.map(id=>[id,deck.audit(cases[id]).filter(i=>i.code===${quote(code)})]))})()`);
      assert(result[hit].length, `${hit} 미검출: ${JSON.stringify(result)}`);
      for (const id of safe) assert.equal(result[id].length, 0, `${id} 오검출: ${JSON.stringify(result[id])}`);
      assert(result[hit].every(i => Number.isInteger(i.slide) && i.slide > 0 && i.target && i.message && i.rect && ['error','warn'].includes(i.level)), '결과 형식');
      if (code === 'placeholder-left') assert.match(result[hit][0].message, /star/);
      if (code === 'text-heavy') assert.match(result[hit][0].message, /93/);
      if (code === 'font-fallback') assert.match(result[hit][0].message, /−.*✓/);
    });
    await test('숨겨진 표는 시각 요소가 아님', async () => {
      assert((await js("deck.audit(cases['visual-hidden-table']).filter(i=>i.code==='no-visual')")).length);
    });
    await test('배경 이미지의 불투명 표본', async () => {
      assert((await js("deck.audit(cases['whole-bg-hit']).filter(i=>i.code==='slide-is-image')")).length);
    });
    await test('인라인 블록 카드의 어긋난 정렬', async () => {
      assert((await js("deck.audit(cases['near-hit-inline']).filter(i=>i.code==='near-miss')")).length);
    });
    await test('숨은 장과 공개 단계 측정 및 상태 복원', async () => {
      const out = await js(`(()=>{deck.go(0);const s=deck.slides[cases['step-hit']],e=s.querySelector('[data-step]'),before=[deck.page,deck.step,s.hidden];s.hidden=false;const started=e.getBoundingClientRect().top;for(const a of s.getAnimations({subtree:true}))a.finish();const ended=e.getBoundingClientRect().top;s.hidden=true;const issues=deck.audit(cases['step-hit']);return {before,after:[deck.page,deck.step,s.hidden],issues,drop:started-ended}})()`);
      assert.deepEqual(out.after,out.before);
      assert(out.drop>100, `등장 효과 전제 실패: ${out.drop}`);
      assert(out.issues.some(i=>i.code==='edge-crowding'));
    });
    await test('글꼴이 없는 덱은 보충 검사 생략', async () => {
      const issues = await js(`(()=>{const sheet=document.querySelector('#maple-faces');sheet.disabled=true;const found=deck.audit(cases['font-hit']).filter(i=>i.code==='font-fallback');sheet.disabled=false;return found})()`);
      assert.equal(issues.length,0);
      await js("Promise.all([...document.fonts].map(f=>f.load())).then(()=>document.fonts.ready).then(()=>true)", true);
    });
    await test('보충 글꼴에 있는 글자는 통과', async () => {
      const issues = await js(`(async()=>{const f=new FontFace('Deck Sans','local("Segoe UI Symbol")');await f.load();document.fonts.add(f);const out=deck.audit(cases['font-hit']).filter(i=>i.code==='font-fallback');document.fonts.delete(f);return out})()`, true);
      assert.equal(issues.length,0,JSON.stringify(issues));
    });
    await test('메이플스토리의 누락 문자와 fonts.check 함정', async () => {
      const out = await js(`(()=>({check:document.fonts.check('40px Maplestory','−✓'),issues:deck.audit(cases['font-hit']).filter(i=>i.code==='font-fallback')}))()`);
      assert(out.issues.length && /−/.test(out.issues[0].message) && /✓/.test(out.issues[0].message), JSON.stringify(out));
      console.log(`fonts.check=${out.check}, 누락 문자 감지됨`);
    });
  }
  await test('콘솔 오류와 외부 요청 없음', async () => {
    assert.deepEqual(cdp.errors, []);
    assert.deepEqual(cdp.requests.filter(url => /^https?:/i.test(url)), []);
  });
} finally {
  await cdp.stop();
  console.log(`합계: ${passed} 통과 / ${failed} 실패`);
  if (failed) process.exitCode = 1;
}
