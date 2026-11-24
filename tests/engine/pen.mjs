import assert from 'node:assert/strict';
import { spawn, execFile, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';

// Also imported by lab.mjs. No npm dependencies; use the project's real Chrome + CDP seam.
const here = path.dirname(fileURLToPath(import.meta.url));
const work = path.resolve(process.env.PBJ_ENGINE_TMP || path.join(os.tmpdir(), 'pbj-html-deck-engine'));
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const runFile = promisify(execFile);
async function until(fn, message = 'Chrome 준비 시간 초과') {
  const deadline = performance.now() + 10000;
  while (performance.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await delay(30);
  }
  throw Error(message);
}

export async function suite(name, run) {
  const shots = path.join(work, 'shots');
  await fs.mkdir(shots, { recursive: true });
  const profile = await fs.mkdtemp(path.join(work, `chrome-${name}-`));
  const chrome = spawn(process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--disable-background-networking', '--disable-breakpad', 'about:blank'
  ], { windowsHide: true, stdio: 'ignore', env: { ...process.env, TEMP: work, TMP: work, TMPDIR: work } });
  let socket, failure, exited = false, id = 0, passed = 0, failed = 0;
  const pending = new Map(), errors = [], requests = [];
  chrome.on('error', error => { failure = error; exited = true; });
  const exit = once(chrome, 'exit').catch(() => {});
  chrome.on('exit', () => { exited = true; });
  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const seq = ++id;
      const timer = setTimeout(() => { pending.delete(seq); reject(Error(`CDP: ${method} 응답 없음`)); }, 10000);
      pending.set(seq, { resolve, reject, timer });
      socket.send(JSON.stringify({ id: seq, method, params }));
    });
  }
  const js = async (expression, awaitPromise = false) => {
    const response = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
    if (response.exceptionDetails) throw Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };
  const selector = s => s.startsWith('@') ? `document.querySelector('#deck-ui').shadowRoot.querySelector(${JSON.stringify(s.slice(1))})` : `document.querySelector(${JSON.stringify(s)})`;
  const rect = s => js(`${selector(s)}.getBoundingClientRect().toJSON()`);
  const point = async s => { const r = await rect(s); assert(r.width && r.height, s); return [r.x + r.width / 2, r.y + r.height / 2]; };
  const mouse = (type, [x,y], modifiers, down = false) => send('Input.dispatchMouseEvent', {
    type, x, y, modifiers, buttons: down ? 1 : 0,
    // CDP needs the held button itself as well as the button mask; omitting it can lose pointer capture.
    button: type === 'mouseMoved' && !down ? 'none' : 'left', clickCount: type === 'mouseMoved' ? 0 : 1
  });
  const frame = () => js('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))', true);
  const waitExit = ms => Promise.race([exit, new Promise(resolve => setTimeout(resolve, ms).unref())]);
  const drag = async (points, { touch = false, modifiers = 0 } = {}) => {
    if (touch) {
      const dispatch = (type, p) => send('Input.dispatchTouchEvent', { type, touchPoints: p ? [{ x: p[0], y: p[1], id: 1 }] : [] });
      await dispatch('touchStart', points[0]);
      for (const p of points.slice(1)) { await dispatch('touchMove', p); await frame(); }
      await dispatch('touchEnd');
    } else {
      await mouse('mouseMoved', points[0], modifiers);
      await mouse('mousePressed', points[0], modifiers, true);
      for (const p of points.slice(1)) { await mouse('mouseMoved', p, modifiers, true); await frame(); }
      await mouse('mouseReleased', points.at(-1), modifiers);
    }
  };
  const click = async (s, modifiers = 0) => drag([await point(s)], { modifiers });
  const key = async (key, modifiers = 0) => {
    const code = key.length === 1 ? `Key${key.toUpperCase()}` : key;
    const windowsVirtualKeyCode = { ArrowLeft: 37, ArrowRight: 39, Escape: 27 }[key] ?? key.toUpperCase().charCodeAt(0);
    for (const type of ['rawKeyDown', 'keyUp']) await send('Input.dispatchKeyEvent', { type, key, code, modifiers, windowsVirtualKeyCode });
  };
  const view = (width, height) => send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
  const shot = async filename => {
    const { data } = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
    const bytes = Buffer.from(data, 'base64');
    assert.deepEqual([bytes.readUInt32BE(16), bytes.readUInt32BE(20)], [1920, 1080]);
    await fs.writeFile(path.join(shots, filename), bytes);
  };
  const test = async (label, body) => {
    try { await body(); passed++; console.log(`PASS ${label}`); }
    catch (error) { failed++; console.error(`FAIL ${label}\n${error.stack}`); }
  };
  try {
    const port = await until(async () => {
      if (failure || exited) throw failure || Error('Chrome 시작 실패');
      try { return Number((await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); }
      catch { return false; }
    });
    const pages = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
    socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
    await once(socket, 'open', { signal: AbortSignal.timeout(5000) });
    socket.addEventListener('close', () => {
      for (const { timer, reject } of pending.values()) { clearTimeout(timer); reject(Error('Chrome 연결 종료')); }
      pending.clear();
    });
    socket.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data), request = pending.get(message.id), p = message.params;
      if (request) {
        pending.delete(message.id); clearTimeout(request.timer);
        if (message.error) request.reject(Error(message.error.message)); else request.resolve(message.result);
      } else if (message.method === 'Runtime.exceptionThrown') errors.push(p.exceptionDetails.exception?.description || p.exceptionDetails.text);
      else if (message.method === 'Runtime.consoleAPICalled' && p.type === 'error') errors.push(p.args.map(arg => arg.value ?? arg.description).join(' '));
      else if (message.method === 'Log.entryAdded' && p.entry.level === 'error') errors.push(p.entry.text);
      else if (message.method === 'Network.requestWillBeSent') requests.push(p.request.url);
    });
    for (const domain of ['Page', 'Runtime', 'Network', 'Log']) await send(`${domain}.enable`);
    const url = pathToFileURL(path.join(here, `${name}.html`)).href;
    await view(1600, 950);
    await send('Page.navigate', { url });
    await until(() => js(`location.href.startsWith(${JSON.stringify(url)}) && typeof deck?.ready === 'object' && document.readyState === 'complete'`).catch(() => false));
    await js('deck.ready.then(() => true)', true);
    await run({ js, rect, point, drag, click, key, send, view, shot, test, delay, until });
    await test('콘솔 오류·외부 요청 0건', () => {
      assert.deepEqual(errors, []);
      assert.deepEqual(requests.filter(url => !/^(file|data):/.test(url)), []);
    });
  } finally {
    if (!exited && socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ id: ++id, method: 'Browser.close' }));
      await waitExit(5000);
    }
    socket?.close();
    if (!exited && chrome.pid) {
      if (process.platform === 'win32') {
        try { await runFile('taskkill', ['/PID', String(chrome.pid), '/T', '/F'], { windowsHide: true, timeout: 15000 }); }
        catch { if (!exited) chrome.kill(); }
      } else chrome.kill();
      await waitExit(10000);
    }
    assert(exited, `Chrome 미종료: ${chrome.pid}`);
    if (process.platform === 'win32') {
      try {
        execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($env:PBJ_TEST_PROFILE, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)'], { env: { ...process.env, PBJ_TEST_PROFILE: profile }, windowsHide: true, stdio: 'ignore' });
      } catch { console.warn(`임시 프로필을 보존했습니다: ${profile}`); }
    }
    console.log(`Chrome 종료 확인: ${chrome.pid ?? '실행되지 않음'}`);
    console.log(`${name} 합계: ${passed} 통과 / ${failed} 실패`);
    if (failed) process.exitCode = 1;
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await suite('pen', async ({ js, key, test, click, drag, view, point, rect, send, until }) => {
  const toolbar = () => js('!document.querySelector(".pen-tools").hidden');
  const paths = () => js('deck.slides.map(slide=>[...slide.querySelectorAll(".pen-stroke")].map(p=>p.getAttribute("d")))');
  const canvas = (x, y) => js(`{const r=document.querySelector('#deck').getBoundingClientRect(); [r.x+r.width*${x}/1920,r.y+r.width*${y}/1920];}`);
  const stroke = async (coordinates, options) => drag(await Promise.all(coordinates.map(([x,y]) => canvas(x,y))), options);
  const go = i => js(`deck.go(${i})`);
  await test('P·보기 막대·발표 가장자리와 44px 도구', async () => {
    await key('p'); assert.equal(await toolbar(), true);
    assert(await js('[...document.querySelectorAll(".pen-tools button")].every(e=>e.offsetHeight>=44 && e.offsetWidth>=44)'));
    await key('p'); assert.equal(await toolbar(), false);
    await click('@#pen'); assert.equal(await toolbar(), true);
    await click('.pen-close'); assert.equal(await toolbar(), false);
    await js("deck.setMode('present')");
    assert((await rect('.pen-open')).height >= 44);
    await drag([await point('.pen-open')], { touch:true }); assert.equal(await toolbar(), true);
    await click('.pen-close'); await js("deck.setMode('view')");
  });
  await test('발표 펜 단추는 평소 흐리고 캡처에서는 숨김', async () => {
    await js("deck.setMode('present')");
    await send('Input.dispatchMouseEvent', { type:'mouseMoved', x:1, y:1 });
    // 실제 투명도 전환이 끝났는지 기다린다.
    await until(() => js('getComputedStyle(document.querySelector(".pen-chrome")).opacity === "0.3"'), '발표 펜 투명도 전환 미완료');
    await js("document.documentElement.setAttribute('data-deck-shot','')");
    assert.equal(await js('getComputedStyle(document.querySelector(".pen-chrome")).display'), 'none');
    await js("document.documentElement.removeAttribute('data-deck-shot');deck.setMode('view')");
    assert.notEqual(await js('getComputedStyle(document.querySelector(".pen-chrome")).display'), 'none');
  });
  await test('부드러운 캔버스 획, 장별 유지, 축소 뒤 좌표', async () => {
    await go(0); await key('p');
    await stroke([[410,520],[475,490],[540,555]]);
    const original = await paths();
    assert.match(original[0][0], /^M4(?:09|10)[.\d]* 5(?:19|20)[.\d]*Q/);
    assert.deepEqual(original[1], []);
    await key('ArrowRight'); assert.equal(await js('deck.page'), 1);
    await click('.pen-blue'); await stroke([[720,340],[790,372],[850,412]], { touch:true });
    assert.equal(await js('document.querySelectorAll(".slide:not([hidden]) .pen-stroke-blue").length'), 1);
    const second = (await paths())[1]; assert.equal(second.length, 1);
    await key('ArrowLeft'); assert.equal(await js('deck.page'), 0);
    assert.deepEqual(await paths(), [original[0],second]);
    const box = () => js(`{const r=document.querySelector('.slide:not([hidden]) .pen-stroke').getBBox(); [r.x,r.y,r.width,r.height];}`);
    const before = await box(); await view(800, 620); assert.deepEqual(await box(), before);
    const matchesCanvas = await js(`{const path=document.querySelector('.slide:not([hidden]) .pen-stroke');
      const actual=path.getPointAtLength(0).matrixTransform(path.getScreenCTM());const r=document.querySelector('#deck').getBoundingClientRect();
      Math.hypot(actual.x-(r.x+r.width*410/1920),actual.y-(r.y+r.width*520/1920));}`);
    assert(matchesCanvas < .6);
  });
  await test('획 지우개, 마지막 획 취소, 현재 장만 모두 지우기', async () => {
    await go(0);
    await click('.pen-mark'); await stroke([[710,650],[780,650],[855,650]]);
    assert.equal((await paths())[0].length,2);
    assert.equal(await js('getComputedStyle(document.querySelector(".pen-stroke-mark")).opacity'),'.38'.replace(/^\./,'0.'));
    await key('z',2); assert.equal((await paths())[0].length,1);
    await click('.pen-erase');
    const hit = await js(`{const p=document.querySelector('.slide:not([hidden]) .pen-stroke'); const q=p.getPointAtLength(p.getTotalLength()/2).matrixTransform(p.getScreenCTM()); [q.x,q.y];}`);
    await drag([hit]); assert.equal((await paths())[0].length,0);
    await click('.pen-red'); await stroke([[490,620],[620,620]]);
    await click('.pen-clear'); assert.deepEqual((await paths()).map(p=>p.length),[0,1]);
  });
  await test('판서 중 가리개·위젯·밀기 차단, 키 넘김 및 편집 모드 복귀', async () => {
    await go(0);
    const peek = () => js('document.querySelector("#peek").classList.contains("is-open")');
    const toggle = () => js('document.querySelector("#toggle").classList.contains("is-on")');
    await click('#peek'); assert.equal(await peek(),false);
    await click('#toggle'); assert.equal(await toggle(),false);
    await stroke([[950,720],[650,720],[550,720]], { touch:true }); assert.equal(await js('deck.page'),0);
    await key('ArrowRight'); assert.equal(await js('deck.page'),1);
    await key('ArrowLeft'); assert.equal(await js('deck.page'),0);
    await key('e'); assert.equal(await js('deck.mode'),'edit'); assert.equal(await toolbar(),false);
    assert.equal(await js('getComputedStyle(document.querySelector(".pen-surface")).display'),'none');
    const before = await paths();
    await key('p'); assert.equal(await toolbar(),false);
    await key('z',2); assert.deepEqual(await paths(),before);
    await key('e'); await click('#peek'); assert.equal(await peek(),true);
    await click('#toggle'); assert.equal(await toggle(),true);
    assert.deepEqual(await paths(),before);
  });
  await test('저장 파일과 실제 Chrome PDF에 판서 제외', async () => {
    const before = await paths(); assert(before[0].length && before[1].length);
    assert.equal(await js(`{const copy=new DOMParser().parseFromString(deck.serialize(),'text/html'); !copy.querySelector('.pen-chrome,.pen-stroke,.pen-surface');}`),true);
    assert.deepEqual(await paths(),before);
    await key('p');
    try {
      await send('Emulation.setEmulatedMedia',{ media:'print' });
      assert(await js('[...document.querySelectorAll(".pen-chrome,.pen-surface")].every(e=>getComputedStyle(e).display==="none")'));
    } finally { await send('Emulation.setEmulatedMedia',{ media:'' }); }
    // Use identical focus and hover before printing so native buttons render alike.
    const pdf = async () => {
      await send('Input.dispatchMouseEvent', { type:'mouseMoved', x:1, y:1 });
      await js('document.activeElement.blur(); new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))', true);
      const result = await send('Page.printToPDF',{ preferCSSPageSize:true, printBackground:true, displayHeaderFooter:false });
      return Buffer.from(result.data,'base64').toString('latin1').replace(/\/((?:Creation|Mod)Date)\s*\([^)]*\)/g,'/$1');
    };
    const inked = await pdf(); assert(inked.startsWith('%PDF-'));
    await click('.pen-clear'); await go(1); await click('.pen-clear'); await go(0);
    assert.deepEqual((await paths()).map(p=>p.length),[0,0]);
    assert(await pdf() === inked, '획이 있는 때와 없는 때의 PDF가 다릅니다.');
  });
});
