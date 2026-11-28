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
const imageDir = path.join(work, 'shots');
const binary = process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const delay = ms => new Promise(done => setTimeout(done, ms));
const fixture = pathToFileURL(path.join(here, 'fixture.html')).href;
const runFile = promisify(execFile);

async function eventually(fn, message = '조건을 만족하지 못했습니다.') {
  for (let i = 0; i < 120; i++) {
    const v = await fn();
    if (v) return v;
    await delay(40);
  }
  throw Error(message);
}

async function launch() {
  await fs.mkdir(imageDir, { recursive: true });
  const profile = await fs.mkdtemp(path.join(work, 'chrome-'));
  const child = spawn(binary, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--disable-background-networking', '--disable-breakpad', 'about:blank'], {
    windowsHide: true, stdio: 'ignore', env: { ...process.env, TEMP: work, TMP: work, TMPDIR: work }
  });
  let exited = false, spawnError;
  child.once('error', error => { spawnError = error; exited = true; });
  const exit = new Promise(done => child.once('exit', () => { exited = true; done(); }));
  const pending = new Map(), events = [], requests = [];
  let socket, seq = 0;
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timeout = setTimeout(() => { pending.delete(id); reject(Error(`CDP 응답 시간 초과: ${method}`)); }, 10000);
    pending.set(id, { method, timeout, resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const stop = async () => {
    try {
      if (!exited && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ id: ++seq, method: 'Browser.close' }));
        const timeout = AbortSignal.timeout(2000);
        await Promise.race([exit, once(timeout, 'abort')]);
      }
    } finally {
      socket?.close();
      if (!exited) {
        if (process.platform === 'win32') await runFile('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }).catch(() => child.kill());
        else child.kill();
        await Promise.race([exit, once(AbortSignal.timeout(4000), 'abort')]);
      }
      if (!exited) throw Error(`Chrome이 종료되지 않았습니다. PID ${child.pid}`);
      if (process.platform === 'win32') {
        await runFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($env:PBJ_TEST_PROFILE, [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)'], {
          env: { ...process.env, PBJ_TEST_PROFILE: profile }, windowsHide: true, timeout: 20000
        }).catch(error => console.error(`임시 프로필 휴지통 이동 실패: ${error.message}`));
      }
      console.log(`Chrome 종료 확인: ${child.pid ?? '실행되지 않음'}`);
    }
  };
  try {
    const port = await eventually(async () => {
      if (spawnError || exited) throw spawnError || Error('Chrome 시작 실패');
      try { return Number((await fs.readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); }
      catch { return false; }
    }, 'Chrome 디버깅 포트가 열리지 않았습니다.');
    const tabs = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
    socket = new WebSocket(tabs.find(t => t.type === 'page').webSocketDebuggerUrl);
    await once(socket, 'open', { signal: AbortSignal.timeout(5000) });
    socket.addEventListener('close', () => {
      for (const p of pending.values()) { clearTimeout(p.timeout); p.reject(Error(`CDP 연결 종료: ${p.method}`)); }
      pending.clear();
    });
    socket.addEventListener('message', ({ data }) => {
      const m = JSON.parse(data), p = pending.get(m.id);
      if (p) {
        pending.delete(m.id); clearTimeout(p.timeout);
        if (m.error) p.reject(Error(`${p.method}: ${m.error.message}`));
        else p.resolve(m.result);
      } else if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url);
      else if (m.method === 'Runtime.exceptionThrown') events.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
      else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') events.push(m.params.args.map(a => a.value ?? a.description).join(' '));
      else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') events.push(m.params.entry.text);
      else if (m.method === 'Page.javascriptDialogOpening') send('Page.handleJavaScriptDialog', { accept: true }).catch(() => {});
    });
    for (const domain of ['Page', 'Runtime', 'Network', 'Log', 'DOM']) await send(`${domain}.enable`);
    return { send, stop, events, requests };
  } catch (error) { await stop(); throw error; }
}

const cdp = await launch();
const { send } = cdp;
async function js(expression, awaitPromise = false) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
  if (r.exceptionDetails) throw Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
const quote = JSON.stringify;
const selector = s => s.startsWith('@') ? `document.querySelector('#deck-ui').shadowRoot.querySelector(${quote(s.slice(1))})` : `document.querySelector(${quote(s)})`;
const rect = s => js(`${selector(s)}.getBoundingClientRect().toJSON()`);
const view = (width = 1600, height = 900) => send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
async function open(url = fixture, scripts = true) {
  await send('Emulation.setScriptExecutionDisabled', { value: !scripts });
  await send('Page.navigate', { url });
  await eventually(async () => {
    try { return await js(`location.href===${quote(url)} && document.readyState==='complete'`); } catch { return false; }
  }, `파일을 열지 못했습니다: ${url}`);
  if (scripts) await js('window.deck.ready.then(() => true)', true);
}
const mouse = (type, x, y, count = 1, modifiers = 0) => send('Input.dispatchMouseEvent', {
  type, x, y, modifiers, button: type === 'mouseMoved' ? 'none' : 'left', buttons: type === 'mousePressed' ? 1 : 0, clickCount: count
});
async function point(s, fx = 0.5, fy = 0.5) {
  if (s.startsWith('@')) await js(`${selector(s)}.scrollIntoView({block:'nearest'})`);
  const r = await rect(s);
  assert(r.width > 0 && r.height > 0, `보이지 않는 요소: ${s}`);
  return [r.left + r.width * fx, r.top + r.height * fy];
}
async function click(s, { fx = .5, fy = .5, count = 1, modifiers = 0 } = {}) {
  const [x, y] = await point(s, fx, fy);
  await mouse('mouseMoved', x, y);
  // CDP의 clickCount:2가 실제 dblclick 이벤트를 생성한다.
  for (let n = 1; n <= count; n++) {
    await mouse('mousePressed', x, y, n, modifiers); await mouse('mouseReleased', x, y, n, modifiers);
  }
}
async function drag(s, dx, dy, fx, fy) {
  const [x, y] = await point(s, fx, fy);
  await mouse('mouseMoved', x, y); await mouse('mousePressed', x, y);
  for (let n = 1; n <= 6; n++) await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x + dx * n / 6, y: y + dy * n / 6, buttons: 1 });
  await mouse('mouseReleased', x + dx, y + dy);
  await delay(60);
}
const codes = { ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Enter: 13, Escape: 27, Tab: 9, PageUp: 33, PageDown: 34, Home: 36, End: 35, Delete: 46, Backspace: 8, F2: 113, F5: 116, ' ': 32 };
async function key(key, modifiers = 0) {
  const code = key.length === 1 && key !== ' ' ? `Key${key.toUpperCase()}` : key === ' ' ? 'Space' : key;
  const params = { key, code, modifiers, windowsVirtualKeyCode: codes[key] ?? key.toUpperCase().charCodeAt(0) };
  await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...params });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...params });
}
const type = text => send('Input.insertText', { text });
const saveShot = async name => {
  const { data } = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
  await fs.writeFile(path.join(imageDir, `${name}.png`), Buffer.from(data, 'base64'));
};
function closeTo(actual, expected, delta = 1) { assert(Math.abs(actual - expected) <= delta, `${actual} ≠ ${expected} (±${delta}px)`); }
function sameRect(actual, expected, delta = 1) { for (const side of ['x', 'y', 'width', 'height']) closeTo(actual[side], expected[side], delta); }
const state = () => js('[deck.page,deck.step,document.querySelectorAll(".slide:not([hidden]) [data-waiting]").length]');
const turn = async (s, keyName = 'ArrowRight', count = 1) => {
  await js(`${selector(s)}.focus()`);
  for (let n = 0; n < count; n++) await key(keyName);
  return js(`${selector(s)}.value`);
};
async function setFile(s, file) {
  const { result: { objectId } } = await send('Runtime.evaluate', { expression: selector(s) });
  await send('DOM.setFileInputFiles', { objectId, files: [file] });
  await send('Runtime.releaseObject', { objectId });
}
const order = () => js('deck.slides.map(s=>s.dataset.title || s.querySelector("h1,h2")?.textContent)');
const selectedSize = () => js('parseFloat(getComputedStyle(deck.selection).fontSize)');
const enginePath = file => path.join(here, '../../assets/engine', file);
async function inlineFixture(name = 'fixture') {
  const [html, css, script] = await Promise.all([fs.readFile(path.join(here,`${name}.html`),'utf8'), fs.readFile(enginePath('deck.css'),'utf8'), fs.readFile(enginePath('deck.js'),'utf8')]);
  const widgetTag = '<script data-deck="engine" src="../../assets/engine/widgets.js"></script>';
  const widgets = html.includes(widgetTag) ? await fs.readFile(enginePath('widgets.js'),'utf8') : '';
  assert.doesNotMatch(script + widgets, /<\/script/i);
  const built = html.replace('<link data-deck="engine" rel="stylesheet" href="../../assets/engine/deck.css">', () => `<style data-deck="engine">${css}</style>`)
    .replace('<script data-deck="engine" src="../../assets/engine/deck.js"></script>', () => `<script data-deck="engine">${script}\n${widgets}</script>`)
    .replace(widgetTag, '');
  const file = path.join(work, `inline-${name}.html`);
  await fs.writeFile(file, built);
  return pathToFileURL(file).href;
}
const SKIP = Symbol('skip');
let passed = 0, failed = 0, skipped = 0;
async function test(name, fn) {
  try {
    if (await fn() === SKIP) { skipped++; console.log(`미실행 ${name}`); }
    else { passed++; console.log(`PASS ${name}`); }
  } catch (error) { failed++; console.error(`FAIL ${name}\n${error.stack || error}`); }
}

try {
  await test('1. CSS 배율과 스크립트 없는 첫 장', async () => {
    for (const [w, h] of [[1600, 900], [800, 600]]) {
      await view(w, h); await open();
      sameRect(await rect('.slide'), await rect('#deck'));
      await js("deck.setMode('present')");
      const fullscreen = await rect('.slide');
      sameRect(fullscreen, await rect('#deck'));
      await open(`${fixture}?static`, false);
      assert.equal(await js('typeof window.deck.go'), 'undefined');
      assert.equal(await js('document.documentElement.classList.contains("deck-on")'), false);
      sameRect(await rect('.slide'), fullscreen);
    }
    await view(); await open();
    assert.equal(await js(`getComputedStyle(${selector('@#view-bar')}).backgroundColor`), await js('getComputedStyle(document.body).backgroundColor'));
    await saveShot('01-view');
  });
  await test('2. 장 이동·단계·해시·건너뛰기', async () => {
    await open();
    assert.deepEqual(await state(), [0,0,0]);
    for (const expected of [[1,0,3],[1,1,1],[1,2,0],[2,0,2]]) {
      await key('ArrowRight'); assert.deepEqual(await state(), expected);
    }
    await key('ArrowLeft'); assert.deepEqual(await state(), [1,2,0]);
    await key('ArrowLeft'); assert.deepEqual(await state(), [1,1,1]);
    await open(`${fixture}#3.1`);
    assert.deepEqual(await state(), [2,1,1]);
    await key('ArrowRight'); await key('ArrowRight'); assert.equal(await js('deck.page'), 4);
    await key('e'); await key('PageUp'); assert.equal(await js('deck.page'), 3);
    await key('e'); assert.equal(await js('deck.page'), 4);
    await js("deck.setMode('present'); deck.go(3,'all')");
    assert.equal(await js('deck.page'),3, '검사 도구는 건너뛰기 장에 직접 접근할 수 있음');
  });
  await test('3. 모아보기 썸네일과 이동', async () => {
    await open(); await key('g');
    assert.equal(await js('document.querySelectorAll("#deck-overview .deck-thumbnail").length'), 6);
    assert.equal(await js('document.querySelectorAll("#deck-overview [data-waiting]").length'), 0);
    await saveShot('05-overview');
    await click('#deck-overview .deck-thumbnail:nth-child(3) button');
    assert.equal(await js('deck.page'), 2);
    assert.equal(await js('!!document.querySelector("#deck-overview")'), false);
  });
  await test('4a. 고르기·끌기·되돌리기·모서리·방향키', async () => {
    await open(`${fixture}?edit`);
    assert.equal(await js('deck.mode'), 'edit');
    await click('#cover-title');
    assert.equal(await js('deck.selection?.id'), 'cover-title');
    await key('Escape');
    const start = await rect('#cover-title');
    await drag('#cover-title', 120, 60);
    const moved = await rect('#cover-title');
    sameRect(moved, { ...start, x: start.x + 120, y: start.y + 60 });
    assert.equal(await js('deck.dirty'), true);
    await key('z', 2); sameRect(await rect('#cover-title'), start);
    assert.equal(await js('deck.dirty'), false);
    await key('y', 2); sameRect(await rect('#cover-title'), moved);
    await drag('@[data-handle="se"]', 97, 24);
    const larger = await rect('#cover-title');
    closeTo(larger.x, moved.x, 2); closeTo(larger.y, moved.y, 2);
    assert(larger.width > moved.width + 80);
    closeTo(larger.width / moved.width, larger.height / moved.height, .01);
    await key('ArrowRight'); closeTo((await rect('#cover-title')).x - larger.x, (await rect('#deck')).width / 960, .2);
    await key('z', 2); sameRect(await rect('#cover-title'), larger);
    await mouse('mousePressed', 1150, 730); await mouse('mouseReleased', 1150, 730);
    assert.equal(await js('deck.selection'), null, 'data-lock 배경을 선택하지 않음');
    const authored = await rect('#authored');
    const transform = await js('getComputedStyle(document.querySelector("#authored")).transform');
    await drag('#authored', -63, 24);
    sameRect(await rect('#authored'), { ...authored, x: authored.x - 63, y: authored.y + 24 });
    assert.equal(await js('getComputedStyle(deck.selection).transform'), transform);
  });
  await test('4b. 인라인 서식·SVG·MathML 직접 수정과 폭', async () => {
    await open(`${fixture}?edit`);
    await click('#cover-title'); await key('Escape'); await saveShot('02-edit-selected');
    await click('#rich strong');
    assert.equal(await js('document.activeElement.id'), 'rich');
    await js(`getSelection().selectAllChildren(${selector('#rich strong')})`);
    await type('직접 본 사실'); await saveShot('04-text-edit'); await key('Escape');
    assert.equal(await js('document.querySelector("#rich").innerHTML'), '먼저 <strong>직접 본 사실</strong>을 적으세요.');
    await key('z', 2); assert.equal(await js('document.querySelector("#rich strong").textContent'), '관찰한 사실');
    await click('#rich');
    const before = await rect('#rich');
    await drag('@[data-handle="e"]', -100, 0);
    const narrow = await rect('#rich');
    closeTo(narrow.x, before.x); closeTo(narrow.width, before.width - 100);
    await key('z', 2);
    await click('#svg-label');
    await type('세 변'); await key('Escape');
    assert.equal(await js('document.querySelector("#svg-label").textContent'), '세 변');
    await key('z', 2);
    assert.equal(await js('document.querySelector("#svg-label").textContent'), '삼각형');
    await key('PageDown'); await key('PageDown'); await click('#math-label');
    await type('b'); await key('Escape');
    assert.equal(await js('document.querySelector("#math-label").textContent'), 'b');
  });
  await test('4c. 요소 복제·삭제와 되돌리기', async () => {
    await open(`${fixture}?edit`); await click('#cover-title'); await key('Escape');
    const original = await rect('#cover-title'), offset = (await rect('#deck')).width / 96;
    await key('d', 2);
    assert.equal(await js('deck.slides[0].querySelectorAll("h1").length'), 2);
    assert.equal(await js('deck.selection.id'), 'cover-title', '복제는 작성자 id를 보존');
    sameRect(await js('deck.selection.getBoundingClientRect().toJSON()'), { ...original, x: original.x + offset, y: original.y + offset });
    await key('z', 2); assert.equal(await js('deck.slides[0].querySelectorAll("h1").length'), 1);
    await key('Delete'); assert.equal(await js('deck.slides[0].querySelectorAll("h1").length'), 0);
    await key('z', 2); assert.equal(await js('deck.slides[0].querySelector("h1").id'), 'cover-title');
  });
  const editorState = () => js('[deck.page,deck.step,deck.mode,document.documentElement.dataset.theme,deck.selection?.id,deck.selection?.getAttribute("style")]');
  const editingTitle = () => js('document.querySelector("#cover-title").isContentEditable');
  const title = () => js('document.querySelector("#cover-title").textContent');
  await test('4g. 한 번 눌러 정확한 글자 사이에 커서 놓기와 기본 글자 선택', async () => {
    await open(`${fixture}?edit&caret`);
    const [x, y] = await js(`{
      const node = document.querySelector('#cover-title').firstChild, r = document.createRange();
      r.setStart(node, 2); r.setEnd(node, 2);
      const box = r.getBoundingClientRect(); [box.x, box.y + box.height / 2];
    }`);
    await mouse('mousePressed', x, y);
    assert.equal(await editingTitle(), false, '떼기 전에는 편집을 시작하지 않음');
    await mouse('mouseReleased', x + 3, y);
    assert.deepEqual(await js(`{const s=getSelection();[document.activeElement.id, s.isCollapsed, s.anchorNode.parentElement.id, s.anchorOffset];}`), ['cover-title', true, 'cover-title', 2]);
    await type('!'); assert.equal(await title(), '관찰!하고 설명하기');
    await key('Escape'); await key('z', 2);
    await click('#cover-title', { fx: .7 });
    const anchor = await js('getSelection().anchorOffset');
    await click('#cover-title', { fx: .25 });
    assert.equal(await js('getSelection().isCollapsed'), true);
    assert.notEqual(await js('getSelection().anchorOffset'), anchor);
    await js(`${selector('#cover-title')}.addEventListener('dblclick', () => { window.slideDoubleClick = true; })`);
    await click('#cover-title', { fx: .75, count: 2 });
    assert.equal(await js('getSelection().toString()'), '설명하기');
    assert.equal(await js('Boolean(window.slideDoubleClick)'), false);
    await click('#cover-title', { fx: .75, count: 3 });
    assert.equal(await js('getSelection().toString().trim()'), '관찰하고 설명하기');
  });
  await test('4h. 글자를 끌면 편집 대신 요소 이동만', async () => {
    await open(`${fixture}?edit&drag-text`);
    const before = await rect('#cover-title');
    await drag('#cover-title', 60, 30);
    sameRect(await rect('#cover-title'), { ...before, x: before.x + 60, y: before.y + 30 });
    assert.equal(await editingTitle(), false);
    assert.equal(await js('deck.selection?.id'), 'cover-title');
    await key('z', 2); sameRect(await rect('#cover-title'), before);
  });
  await test('4i. Esc로 글자 편집만 끝내고 Delete로 요소 삭제', async () => {
    await open(`${fixture}?edit&escape`); await click('#cover-title');
    assert.equal(await editingTitle(), true);
    await key('Escape');
    assert.equal(await editingTitle(), false);
    assert.equal(await js('deck.selection?.id'), 'cover-title');
    await key('Delete'); assert.equal(await js('document.querySelector("#cover-title")'), null);
    await key('z', 2); assert.equal(await title(), '관찰하고 설명하기');
    await click('#cover-title'); await key('Escape'); await key('Escape');
    assert.equal(await js('deck.selection'), null);
  });
  await test('4j. 수식과 SVG 조각을 한 번 눌러 입력하고 수식은 통째로 이동', async () => {
    await open(`${fixture}?edit&token#3`);
    await click('#math-label');
    assert.equal(await js(`${selector('@#floating-text')}.value`), 'a');
    assert.equal(await js('deck.selection?.id'), 'math-label');
    await type('b'); await key('Escape');
    assert.equal(await js(`${selector('#math-label')}.textContent`), 'b');
    const formula = 'p > math', before = await rect(formula), token = await rect('#math-label');
    await drag('#math-label', 45, 20);
    assert.equal(await js('deck.selection?.localName'), 'math');
    assert.equal(await js(`${selector('@#floating-text')}===null`), true);
    sameRect(await rect(formula), { ...before, x: before.x + 45, y: before.y + 20 });
    sameRect(await rect('#math-label'), { ...token, x: token.x + 45, y: token.y + 20 });
    await key('PageUp'); await key('PageUp');
    await js(`${selector('#svg-label')}.innerHTML='<tspan id="svg-word">삼각형</tspan>'`);
    await click('#svg-word');
    assert.equal(await js(`${selector('@#floating-text')}.value`), '삼각형');
    assert.equal(await js('deck.selection?.id'), 'svg-word');
  });
  await test('4k. 글자·수식에 들어갔다 수정 없이 벗어나면 이력 없음', async () => {
    await open(`${fixture}?edit&empty-edit#3`);
    await click('#math-label'); assert(await js(`!!${selector('@#floating-text')}`));
    await click('section:not([hidden]) > h2'); assert(await js('document.activeElement.matches("h2[contenteditable]")'));
    await click('#picture'); assert.equal(await js('deck.selection?.id'), 'picture');
    assert.deepEqual(await js(`[deck.dirty, ${selector('@#undo')}.disabled, ${selector('@#redo')}.disabled]`), [false, true, true]);
  });
  await test('4l. 글자 편집 중에도 바깥 손잡이로 크기 조절', async () => {
    await open(`${fixture}?edit&resize-text`);
    await click('#rich'); assert.equal(await js('document.activeElement.id'), 'rich');
    const before = await rect('#rich'), handle = await rect('@[data-handle="e"]');
    assert(handle.width > 0 && handle.x >= before.right, '손잡이는 글자 상자 바깥에 있음');
    await drag('@[data-handle="e"]', -70, 0);
    assert.equal(await js(`${selector('#rich')}.isContentEditable`), false);
    assert.equal(await js('deck.selection?.id'), 'rich');
    const resized = await rect('#rich'); closeTo(resized.x, before.x); closeTo(resized.width, before.width - 70);
    await key('z', 2); sameRect(await rect('#rich'), before);
  });
  await test('4m. 선택한 글자 상자는 Enter와 F2로 전체 선택하며 편집', async () => {
    await open(`${fixture}?edit&edit-key`); await click('#cover-title');
    for (const shortcut of ['Enter', 'F2']) {
      await key('Escape'); await key(shortcut);
      assert.equal(await editingTitle(), true, shortcut);
      assert.equal(await js('getSelection().toString()'), await title(), shortcut);
    }
  });
  const inputKeys = ['Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','e','d','g','f','Enter'];
  // Dispatch browser-shaped events for keys that could open a native picker.
  const scopedKey = (s, keyName, options = {}) => js(`${selector(s)}.dispatchEvent(new KeyboardEvent('keydown',${quote({key:keyName,bubbles:true,composed:true,cancelable:true,...options})}))`);
  await test('4d. 패널 입력과 뜬 입력칸의 키는 슬라이드로 새지 않음', async () => {
    await open(`${fixture}?edit&keys`); await click('#cover-title');
    const base = await editorState(), box = await rect('#cover-title');
    const scale = Number(await js(`${selector('@#body-scale')}.value`));
    assert.equal(Number(await turn('@#body-scale','ArrowRight')), scale + 1);
    sameRect(await rect('#cover-title'),box);
    assert.deepEqual(await editorState(),base);
    for (const control of ['@#body-scale','@#element-step','@#accent','@#font-files']) {
      await js(`{const control=${selector(control)}; control.hidden=false; control.focus();}`);
      assert.equal(await js(`document.querySelector('#deck-ui').shadowRoot.activeElement===${selector(control)}`),true);
      for (const k of inputKeys) assert.equal(await scopedKey(control,k),true,`${control}: ${k}를 엔진이 막음`);
      assert.deepEqual(await editorState(),base);
      sameRect(await rect('#cover-title'),box);
      assert.equal(await js('!!document.querySelector("#deck-overview")'),false);
    }
    await click('#svg-label');
    const svgBase = await editorState();
    for (const k of inputKeys.slice(0,-1)) await key(k);
    assert.deepEqual(await editorState(),svgBase);
    assert.equal(await js(`document.querySelector('#deck-ui').shadowRoot.activeElement===${selector('@#floating-text')}`),true);
    await type('새 도형 이름'); await key('Enter');
    assert.deepEqual(await editorState(),svgBase);
    assert.equal(await js(`${selector('@#floating-text')}===null`),true);
    assert.equal(await js('document.querySelector("#svg-label").textContent'),'새 도형 이름');
  });
  await test('4e. 조합 입력 중 Enter·Esc와 전역 키를 넘겨 줌', async () => {
    await open(`${fixture}?edit&ime`);
    const base = await editorState();
    for (const flags of [{isComposing:true},{keyCode:229}]) {
      for (const k of ['ArrowRight','e','d','g','f','Enter','Escape']) assert.equal(await scopedKey('#deck',k,flags),true);
      assert.deepEqual(await editorState(),base);
    }
    await click('#cover-title');
    assert.equal(await editingTitle(),true);
    for (const flags of [{isComposing:true},{keyCode:229}]) {
      for (const k of ['Enter','Escape','e','d','g','f']) assert.equal(await scopedKey('#cover-title',k,flags),true);
      assert.equal(await editingTitle(),true);
    }
    await js(`${selector('#cover-title')}.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}))`);
    assert.equal(await scopedKey('#cover-title','Escape'),true);
    assert.equal(await js(`${selector('#cover-title')}.dispatchEvent(new InputEvent('beforeinput',{bubbles:true,cancelable:true,inputType:'insertParagraph'}))`),true);
    assert.equal(await editingTitle(),true);
    await js(`${selector('#cover-title')}.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}))`);
    await key('Escape'); assert.equal(await editingTitle(),false);
    await click('#svg-label');
    for (const flags of [{isComposing:true},{keyCode:229}]) {
      for (const k of ['Enter','Escape','e','d','g','f']) assert.equal(await scopedKey('@#floating-text',k,flags),true);
      assert.equal(await js(`!!${selector('@#floating-text')}`),true);
    }
    await key('Escape'); assert.equal(await js(`!!${selector('@#floating-text')}`),false);
    assert.equal(await js('deck.dirty'),false);
  });
  await test('4f. 글자 편집 중 되돌리기·장 이동·발표 시작은 편집부터 확정', async () => {
    const original = '관찰하고 설명하기';
    for (const [action, run] of [
      ['undo-button', () => click('@#undo')], ['undo-key', () => key('z',2)], ['next-button', () => click('@#slide-nav-next')],
      ['next-key', () => key('PageDown')], ['present', () => click('@#edit-present')]
    ]) {
      await open(`${fixture}?edit&finish=${action}`);
      await click('#cover-title'); await key('a', 2);
      await type('새 제목'); await type(' 완성');
      assert.equal(await editingTitle(),true);
      assert.equal(await title(),'새 제목 완성');
      assert.equal(await js(`${selector('@#undo')}.disabled`),false);
      await run();
      assert.equal(await editingTitle(),false,action);
      if (action.startsWith('undo')) {
        assert.equal(await title(),original,action);
        assert.equal(await js('deck.dirty'),false,action);
        assert.equal(await js(`${selector('@#undo')}.disabled`),true,action);
        await click('@#redo');
        assert.equal(await title(),'새 제목 완성',action);
        assert.equal(await js(`${selector('@#redo')}.disabled`),true,action);
      } else {
        assert.equal(await title(),'새 제목 완성',action);
        if (action.startsWith('next')) assert.equal(await js('deck.page'),1,action);
        if (action === 'present') {
          assert.equal(await js('deck.mode'),'present');
          await key('Escape'); await eventually(() => js('deck.mode === "edit"'));
        }
        await click('@#undo');
        assert.equal(await title(),original,action);
        assert.equal(await js('deck.dirty'),false,action);
        assert.equal(await js(`${selector('@#undo')}.disabled`),true,action);
      }
    }
  });
  await test('8. 정답 열기·타이머와 편집 중 동작 차단', async () => {
    await open(`${fixture}#2`);
    await click('[data-peek]'); assert.equal(await js('document.querySelector("[data-peek]").classList.contains("is-open")'), true);
    await click('[data-peek]'); assert.equal(await js('document.querySelector("[data-peek]").classList.contains("is-open")'), false);
    await click('[data-timer]'); assert.equal(await js('document.querySelector("[data-timer]").textContent'), '0:02');
    await eventually(() => js('document.querySelector("[data-timer]").textContent === "0:01"'));
    await eventually(() => js('document.querySelector("[data-timer].is-done")?.textContent === "0:00"'));
    assert.match(await js('deck.serialize()'), />타이머<\/button>/);
    await key('e'); await click('[data-peek]'); await click('[data-timer]');
    assert.equal(await js('document.querySelector("[data-timer]").textContent'), '타이머');
    assert.equal(await js('document.querySelector("[data-peek]").classList.contains("is-open")'), false);
  });
  await test('5. 장 추가·복제·삭제·순서·건너뛰기와 각각의 되돌리기', async () => {
    await open(`${fixture}?edit#2`);
    const initial = await order();
    await click('@#slide-new'); assert.equal(await js('deck.page'), 2);
    assert.deepEqual(await order(), [initial[0], initial[1], '제목', ...initial.slice(2)]);
    await click('@#undo'); assert.deepEqual(await order(), initial);
    await click('@#slide-copy'); assert.deepEqual(await order(), [initial[0], initial[1], initial[1], ...initial.slice(2)]);
    await click('@#undo'); assert.deepEqual(await order(), initial);
    await click('@#slide-delete'); assert.deepEqual(await order(), [initial[0], ...initial.slice(2)]);
    await click('@#undo'); assert.deepEqual(await order(), initial);
    await click('@#slide-earlier'); assert.deepEqual(await order(), [initial[1], initial[0], ...initial.slice(2)]);
    await click('@#undo'); assert.deepEqual(await order(), initial);
    await click('@#slide-later'); assert.deepEqual(await order(), [initial[0], initial[2], initial[1], ...initial.slice(3)]);
    await click('@#undo'); assert.deepEqual(await order(), initial);
    await click('@#skip-slide'); assert.equal(await js('deck.slides[1].hasAttribute("data-skip")'), true);
    await click('@#undo'); assert.equal(await js('deck.slides[1].hasAttribute("data-skip")'), false);
  });
  await test('6a. 전역·요소 서식과 글자 크기 연동', async () => {
    await open(`${fixture}?edit`);
    await click('@#theme-dark'); assert.equal(await js('document.documentElement.dataset.theme'), 'dark');
    await click('@#theme-light'); assert.equal(await js('document.documentElement.dataset.theme'), 'light');
    await turn('@#title-scale','ArrowRight',20);
    assert.equal(await js('document.documentElement.style.getPropertyValue("--title-scale")'), '1.2');
    await click('#rich');
    const before = await selectedSize();
    await turn('@#font-size','ArrowRight',30); closeTo(await selectedSize(), before * 1.3, .02);
    await turn('@#body-scale','ArrowRight',20);
    assert.equal(await js('document.documentElement.style.getPropertyValue("--body-scale")'), '1.2');
    closeTo(await selectedSize(), before * 1.3 * 1.2, .02);
    await turn('@#title-font', 'ArrowDown'); await turn('@#body-font', 'ArrowDown');
    assert(await js('["--font-title","--font-body"].every(k=>document.documentElement.style.getPropertyValue(k))'));
    // Chrome의 OS 색 선택창 대신 확정된 색 입력 이벤트를 전달한다.
    await js(`{ const input=${selector('@#accent')}; input.value='#c64051'; input.dispatchEvent(new Event('change')); }`);
    assert.equal(await js('document.documentElement.style.getPropertyValue("--accent")'), '#c64051');
    await click('@#bold'); assert.equal(await js('getComputedStyle(deck.selection).fontWeight'), '700');
    await click('@#align-center'); assert.equal(await js('getComputedStyle(deck.selection).textAlign'), 'center');
    await click('@#color-accent'); assert.equal(await js('getComputedStyle(deck.selection).color'), 'rgb(198, 64, 81)');
    await turn('@#element-step', 'ArrowDown'); assert.equal(await js('deck.selection.dataset.step'), '1');
    await click('@#undo'); assert.equal(await js('deck.selection.hasAttribute("data-step")'), false);
    await click('@#add-text'); assert.equal(await js('deck.selection.matches(".added-text")'), true);
    await click('@#undo'); assert.equal(await js('!!document.querySelector(".added-text")'), false);
    await click('#cover-title');
    await js(`${selector('@#panel-scroll')}.scrollTop=0`); await saveShot('02-edit-selected');
    await js(`${selector('@#panel-scroll')}.scrollTop=10000`); await saveShot('03-edit-panel-bottom');
  });
  await test('6b. 글꼴 파일 추가·선택·삭제와 그림 교체·넣기', async () => {
    await open(`${fixture}?edit`);
    await setFile('@#font-files', process.env.PBJ_TEST_FONT || 'C:/Windows/Fonts/marlett.ttf');
    await eventually(() => js('document.querySelector(\'style[data-deck="fonts"]\').textContent.includes("@font-face")'));
    const family = await js(`${selector('@#title-font')} .querySelector('optgroup[label="파일에 포함"] option').value`);
    await turn('@#title-font','ArrowDown');
    assert.equal(await js(`${selector('@#title-font')}.value`), family);
    assert.match(await js('document.documentElement.style.getPropertyValue("--font-title")'), new RegExp(family));
    await saveShot('07-font-added');
    await click('@#remove-font-0');
    assert.equal(await js('document.querySelector(\'style[data-deck="fonts"]\').textContent.includes("@font-face")'), false);
    await click('@#undo'); assert(await js('document.querySelector(\'style[data-deck="fonts"]\').textContent.includes("@font-face")'));
    await click('@#slide-nav-next'); await click('@#slide-nav-next');
    await click('#picture');
    const old = await js('deck.selection.getAttribute("src")');
    const picture = path.join(work, 'upload.svg');
    await fs.writeFile(picture, '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="navy"/></svg>');
    await send('Page.setInterceptFileChooserDialog', { enabled: true });
    await click('@#picture-replace');
    await setFile('@#picture-file', picture);
    await eventually(() => js('deck.selection.getAttribute("src").startsWith("data:image/svg+xml;base64,")'), '그림 교체 미반영');
    await click('@#undo'); assert.equal(await js('deck.selection.getAttribute("src")'), old);
    await click('@#add-picture');
    await setFile('@#picture-file', picture);
    await eventually(() => js('deck.selection.matches(".added-image")'));
    closeTo((await rect('.added-image')).width, (await rect('#deck')).width / 3);
  });
  await test('7. 저장본 재열기·편집 이어가기·원래대로·반복 저장', async () => {
    const runtime = await inlineFixture();
    await open(`${runtime}?edit`);
    const start = await rect('#cover-title');
    await drag('#cover-title',120,60); await drag('@[data-handle="se"]',84,17);
    await click('#cover-title'); await key('a', 2); await type('고친 제목'); await key('Escape');
    await setFile('@#font-files', process.env.PBJ_TEST_FONT || 'C:/Windows/Fonts/marlett.ttf');
    await eventually(() => js(`${selector('@#element-font')}.querySelector('optgroup[label="파일에 포함"] option')` + '!==null'));
    await turn('@#element-font','ArrowDown');
    await click('@#slide-later');
    const expectedOrder = await order(), expectedRect = await rect('#cover-title');
    const expectedFont = await js('document.querySelector("#cover-title").style.fontFamily');
    const html = await js('deck.serialize()');
    const clean = await js(`(()=>{const d=new DOMParser().parseFromString(${quote(html)},'text/html');return !d.querySelector('[data-deck-rt],[hidden],[data-waiting],[contenteditable],[data-deck-mode],.deck-on') && !d.documentElement.style.getPropertyValue('--bar') && !d.documentElement.style.getPropertyValue('--panel') && !!d.querySelector('meta[name="deck-saved"]');})()`);
    assert.equal(clean, true);
    const out = path.join(work,'saved.html'); await fs.writeFile(out,html);
    await open(pathToFileURL(out).href);
    assert.equal(await js('deck.mode'), 'view');
    await key('e'); await key('PageDown');
    assert.deepEqual(await order(), expectedOrder);
    sameRect(await rect('#cover-title'), expectedRect);
    assert.equal(await js('document.querySelector("#cover-title").textContent'), '고친 제목');
    assert.equal(await js('document.querySelector("#cover-title").style.fontFamily'), expectedFont);
    const second = await js('deck.serialize()');
    assert.equal(second.length, html.length);
    const slides = value => value.match(/<main id="deck">[\s\S]*?<\/main>/)[0];
    assert.equal(slides(second), slides(html));
    await drag('#cover-title',35,24); const moved = await rect('#cover-title');
    sameRect(moved,{...expectedRect, x:expectedRect.x+35, y:expectedRect.y+24});
    await click('@#reset-element');
    const reset = await rect('#cover-title');
    closeTo(reset.x,start.x); closeTo(reset.y,start.y); closeTo(reset.height,start.height);
    assert.equal(await js('deck.selection.hasAttribute("data-deck-e") || deck.selection.hasAttribute("data-deck-o") || deck.selection.hasAttribute("style")'), false);
    await click('@#undo'); sameRect(await rect('#cover-title'),moved);
  });
  await test('8b. 발표 가장자리, 편집으로 복귀, 좁은 화면·터치', async () => {
    await open(`${fixture}?edit`); await click('@#edit-present');
    assert.equal(await js('deck.mode'), 'present');
    await mouse('mouseMoved',800,899);
    assert.equal(await js(`${selector('@#present-edge')}.hidden`), false);
    await saveShot('06-present-edge');
    await key('Escape'); await eventually(() => js('deck.mode === "edit"'));
    await view(390,700); await delay(100);
    const panel = await rect('@#edit-panel'), stage = await rect('#deck');
    assert(panel.top >= stage.bottom - 1 && panel.width === 390);
    await key('e');
    const swipe = async (dx, dy) => {
      await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:210,y:300}]});
      await send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:210+dx,y:300+dy}]});
      await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    };
    await swipe(-85,5); assert.equal(await js('deck.page'),1);
    await swipe(85,5); assert.equal(await js('deck.page'),0);
    await view();
  });
  await test('8c. 캡처 크기에서 포인터가 가장자리를 깨워도 화면 도구는 안 보임', async () => {
    await view(640,360); await open();
    const visible = s => js(`${selector(s)}.checkVisibility()`);
    const pageMarker = () => js('getComputedStyle(deck.slides[deck.page],"::after").content');
    const shot = on => js(`document.documentElement.toggleAttribute('data-deck-shot',${on});void 0`);
    try {
      await js(`{const css=document.createElement('style'); css.textContent='.slide[data-deck-page]::after{content:attr(data-deck-page)}';document.head.append(css);}`);
      assert.equal(await visible('@#view-bar'), true);
      assert.notEqual(await pageMarker(), 'none');
      await js("deck.setMode('present');deck.go(2,'all')");
      await mouse('mouseMoved',320,359);
      assert.equal(await visible('@#present-edge'), true);
      await shot(true);
      await mouse('mouseMoved',321,359);
      assert.equal(await visible('@#present-edge'), false);
      assert.equal(await pageMarker(), 'none');
      assert.equal(await visible('#deck > .slide:not([hidden])'), true);
      await js("deck.setMode('view')");
      assert.equal(await visible('@#view-bar'), false);
      await js("deck.setMode('edit')");
      assert.equal(await visible('@#edit-panel'), false);
      sameRect(await rect('#deck'), { x:0, y:0, width:640, height:360 });
      await shot(false);
      assert.equal(await visible('@#edit-panel'), true);
      assert.notEqual(await pageMarker(), 'none');
    } finally { await shot(false); await view(); }
  });
  await test('9. 배치 검사와 현재 화면 보존', async () => {
    await view(); await open(`${fixture}#2.1`);
    const before = await state();
    const findings = await js('deck.audit(4)');
    for (const code of ['overflow-canvas','small-text','image-broken','text-overlap']) {
      assert(findings.some(issue => issue.code === code), `누락: ${code}\n${JSON.stringify(findings)}`);
    }
    assert(findings.every(({slide,target,rect,message})=>slide===5 && target && rect.width>=0 && message));
    assert.deepEqual(await js('deck.audit(5)'), []);
    assert.deepEqual(await state(), before);
    const other = await js(`(()=>{
      const section=document.createElement('section'); section.className='slide'; section.dataset.slide='';
      section.innerHTML='<p style="height:10px;overflow:hidden;color:#f8f8ed">x^2</p><a href="https://example.invalid/">참조</a>';
      document.querySelector('#deck').append(section);
      const marked=deck.audit(6);section.replaceChildren();const empty=deck.audit(6);section.remove();return [marked,empty];
    })()`);
    for (const code of ['text-clipped','low-contrast','plain-math','external-resource','no-title']) assert(other[0].some(issue=>issue.code===code), `누락: ${code}`);
    assert(other[1].some(issue=>issue.code==='empty-slide'));
  });
  await test('10. PDF 인쇄: 그리드 배치, 모든 장 출력, 화면 복원', async () => {
    await view(); await open(`${fixture}#6`);
    const printLayout = () => js(`(()=>{
      const slide=document.querySelector('#print-layout'), origin=slide.getBoundingClientRect();
      const zoom=origin.width/1920;
      const children=[...slide.children].map(child=>{
        const r=child.getBoundingClientRect();
        return {x:(r.x-origin.x)/zoom,y:(r.y-origin.y)/zoom,width:r.width/zoom,height:r.height/zoom};
      });
      return {display:getComputedStyle(slide).display,children};
    })()`);
    const screen = await printLayout();
    assert.equal(screen.display,'grid');
    assert(screen.children[0].x + screen.children[0].width < screen.children[1].x);
    closeTo(screen.children[0].y,screen.children[1].y);
    try {
      await send('Emulation.setEmulatedMedia',{media:'print'});
      const printed = await printLayout();
      assert.equal(printed.display,'grid');
      for (const [index,box] of printed.children.entries()) sameRect(box,screen.children[index]);
    } finally { await send('Emulation.setEmulatedMedia',{media:''}); }
    await js(`deck.go(1,1);
      window.printStates=[];
      for (const name of ['beforeprint','afterprint']) addEventListener(name,()=>printStates.push([name,deck.slides.map(s=>s.hidden)]));
    `);
    const expected = await js('deck.count'), visibility = await js('deck.slides.map(s=>s.hidden)'), before = await state();
    const { data } = await send('Page.printToPDF', { preferCSSPageSize:true, printBackground:true, displayHeaderFooter:false });
    const pdf = Buffer.from(data,'base64').toString('latin1');
    assert(pdf.startsWith('%PDF-'));
    assert.equal((pdf.match(/\/Type\s*\/Page\b/g) || []).length,expected);
    assert.deepEqual(await js('printStates'),[['beforeprint',Array(expected).fill(false)],['afterprint',visibility]]);
    assert.deepEqual(await js('deck.slides.map(s=>s.hidden)'),visibility);
    assert.deepEqual(await state(),before);
    assert.equal(await js('deck.dirty'),false);
  });
  await test('10b. PDF 인쇄: 등장 효과를 꺼서 다른 장의 단계 내용도 찍힌다', async () => {
    await view(); await open(`${fixture}#1`);
    const probe = () => js(`(()=>{
      const style=document.createElement('style');
      style.textContent='@keyframes probe-in{from{opacity:0}} #deck [data-step]:not([data-waiting]){animation:probe-in 30s backwards}';
      document.head.append(style);
      const el=[...document.querySelectorAll('#deck > .slide[hidden] [data-step]')][0];
      const slide=el.closest('.slide'), hidden=slide.getAttribute('hidden');
      slide.removeAttribute('hidden');
      const cs=getComputedStyle(el), out={name:cs.animationName,opacity:cs.opacity,visibility:cs.visibility};
      slide.setAttribute('hidden',hidden ?? ''); style.remove();
      return out;
    })()`);
    const screen = await probe();
    assert.equal(screen.name,'probe-in');
    assert.equal(screen.opacity,'0');
    try {
      await send('Emulation.setEmulatedMedia',{media:'print'});
      const printed = await probe();
      assert.equal(printed.name,'none');
      assert.equal(printed.opacity,'1');
      assert.equal(printed.visibility,'visible');
    } finally { await send('Emulation.setEmulatedMedia',{media:''}); }
  });
  await test('7b. 저장 단축키·선택기 핸들 재사용·저장 상태', async () => {
    await open(`${await inlineFixture()}?edit`);
    await js(`{
      window.saveProbe = { picks:0, writes:[], closes:0, changes:0 };
      deck.on('change',()=>saveProbe.changes++);
      window.showSaveFilePicker = async options => {
        saveProbe.picks++; saveProbe.name = options.suggestedName;
        return { createWritable:async () => ({ write:async text => saveProbe.writes.push(text), close:async () => { saveProbe.closes++; } }) };
      };
    }`);
    await click('#cover-title'); await key('a', 2);
    await type('편집 중 저장'); await type(' 확인');
    assert.equal(await editingTitle(),true);
    assert.equal(await js('saveProbe.changes'),0);
    await key('s',2);
    await eventually(() => js('saveProbe.closes===1 && !deck.dirty'));
    assert.equal(await editingTitle(),false);
    assert.equal(await title(),'편집 중 저장 확인');
    assert.equal(await js('saveProbe.changes'),1);
    assert(await js('saveProbe.writes[0].includes(">편집 중 저장 확인</h1>")'));
    assert.equal(await js('saveProbe.name'),'inline-fixture.html');
    await key('ArrowDown'); await click('@#save');
    await eventually(() => js('saveProbe.closes===2 && !deck.dirty'));
    assert.equal(await js('saveProbe.picks'),1);
    assert(await js('saveProbe.writes[1].includes("data-deck-e=") && saveProbe.writes[0]!==saveProbe.writes[1]'));
    await key('z',2); assert.equal(await js('deck.dirty'),true);
  });
  await test('7c. 선택기가 없는 브라우저의 HTML 내려받기', async () => {
    const runtime = await inlineFixture(), downloadDir = await fs.mkdtemp(path.join(work,'download-'));
    await open(`${runtime}?edit`);
    await send('Browser.setDownloadBehavior', { behavior:'allow', downloadPath:downloadDir });
    await js('Object.defineProperty(window,"showSaveFilePicker",{configurable:true,value:undefined}); void 0');
    await click('#cover-title'); await key('Escape'); await key('ArrowRight'); await click('@#save');
    const downloaded = path.join(downloadDir,'inline-fixture.html');
    const text = await eventually(() => fs.readFile(downloaded,'utf8').catch(error => {
      if (error.code === 'ENOENT') return false;
      throw error;
    }));
    assert.match(text, /^<!doctype html>/i);
    assert.match(text, /<meta name="deck-saved" content="[^"]+">/);
    assert.match(text, /<h1[^>]*data-deck-e=/);
    assert.equal(await js('deck.dirty'),false);
  });
  const review = await inlineFixture('regression');
  let reviewRun = 0;
  const editReview = () => open(`${review}?edit&run=${++reviewRun}#2`);
  const savedDOM = expr => js(`(()=>{const d=new DOMParser().parseFromString(deck.serialize(),'text/html');return ${expr}})()`);
  const reopenSaved = async (name, suffix = '') => {
    const file = path.join(work, `${name}.html`);
    await fs.writeFile(file, await js('deck.serialize()'));
    await open(pathToFileURL(file).href + suffix);
  };
  const field = (id, value, slider = false) => js(`{const el=${selector('@#'+id)};el.value=${quote(value)};${slider ? "el.dispatchEvent(new Event('input'));" : ''}el.dispatchEvent(new Event('change'));}`);
  await test('A1. 작성자 속성을 모아보기·저장본에서 보존', async () => {
    await open(`${review}#3`); await click('#timer'); await click('#peek'); await js('deck.overview(true)');
    for (const [via, query] of [['모아보기', expr => js(`(()=>{const d=document.querySelectorAll('.deck-thumb-canvas')[2];return ${expr}})()`)], ['저장', savedDOM]]) {
      assert.deepEqual(await query(`[d.querySelector('#answer').hidden,d.querySelector('#author-edit').getAttribute('contenteditable'),d.querySelector('#author-inert').inert,d.querySelector('#peek').classList.contains('is-open'),d.querySelector('#timer').innerHTML,d.querySelector('#early').hasAttribute('data-waiting')]`), [true,'plaintext-only',true,false,'<b>모둠 토의</b>',false],via);
    }
    await reopenSaved('author-attributes','#3');
    assert.equal(await js('document.querySelector("#answer").checkVisibility()'),false);
    await editReview(); await click('#caption'); await key('a', 2); await type('편집 중');
    assert.deepEqual(await savedDOM(`[d.querySelector('#caption').textContent,d.querySelector('#caption').hasAttribute('contenteditable')]`),['편집 중',false]);
    assert.equal(await js('document.querySelector("#caption").isContentEditable'),true);
  });
  await test('A2. 제목 글자 수정과 강조 요소 삭제·이동의 이력 왕복', async () => {
    for (const operation of ['Delete','ArrowRight']) {
      await editReview();
      const before = await js('document.querySelector("#lesson-title").innerHTML');
      await click('#token',{modifiers:1}); await key('Escape'); await key(operation);
      const afterOperation = await js('document.querySelector("#lesson-title").innerHTML');
      assert.notEqual(afterOperation,before);
      await click('#lesson-title',{fx:.01,fy:.75}); await key('Escape'); await key('F2'); await key('End'); await type('!'); await key('Escape');
      const afterText = await js('document.querySelector("#lesson-title").innerHTML');
      assert.match(afterText,/!$/);
      await js('deck.undo()'); assert.equal(await js('document.querySelector("#lesson-title").innerHTML'),afterOperation);
      await js('deck.undo()'); assert.equal(await js('document.querySelector("#lesson-title").innerHTML'),before);
      await js("deck.setMode('view');document.querySelector('#token').click()");
      assert.equal(await js('document.querySelector("#token").dataset.clicks'),'1');
      await js("delete document.querySelector('#token').dataset.clicks;deck.setMode('edit');deck.redo();deck.redo()");
      assert(await js(`{const expected=document.createElement('h2');expected.innerHTML=${quote(afterText)};[...expected.childNodes].length===document.querySelector('#lesson-title').childNodes.length && [...expected.childNodes].every((child,i)=>child.isEqualNode(document.querySelector('#lesson-title').childNodes[i]));}`), '글자·속성·요소 구조가 동일해야 하며 속성 순서는 무관');
    }
  });
  await test('A3. SVG 그라디언트 픽셀은 복제와 원본 삭제 뒤에도 같음', async () => {
    await editReview();
    const pixel = async () => {
      const {x,y,width,height} = await rect('#deck > .slide:not([hidden]) svg rect');
      return (await send('Page.captureScreenshot',{format:'png',clip:{x:Math.round(x+width/2),y:Math.round(y+height/2),width:4,height:4,scale:1}})).data;
    };
    const original = await pixel(); await click('@#slide-copy');
    const copied = await pixel();
    const ids = await js('deck.slides.slice(1,3).map(s=>s.querySelector("linearGradient").id)');
    await js('deck.go(1)'); await click('@#slide-delete');
    assert.equal(copied,original,'복사본 그라디언트 픽셀');
    assert.equal(await pixel(),original,'원본을 삭제한 뒤의 픽셀');
    assert.deepEqual(ids,['chalk','chalk']);
  });
  await test('A4. 붙여넣기 뒤 처음·중간·끝 줄바꿈, 정리와 이력', async () => {
    for (const [line, offset, expected] of [[1,2,'첫 줄<br>둘째<br>셋째'],[0,0,'<br>셋째첫 줄<br>둘째'],[1,1,'첫 줄<br>둘<br>셋째째']]) {
      await editReview(); await click('#caption'); await key('a', 2);
      await js(`{const e=document.querySelector('#caption'),cb=new DataTransfer();cb.setData('text/plain','첫 줄\\n둘째');cb.setData('text/html','<b>서식</b>');e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:cb,bubbles:true,cancelable:true}));const w=document.createTreeWalker(e,NodeFilter.SHOW_TEXT),runs=[];while(w.nextNode())if(w.currentNode.length)runs.push(w.currentNode);getSelection().collapse(runs[${line}],${offset});}`);
      await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'});
      await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
      await type('셋째'); await key('Escape');
      const markup = () => js('document.querySelector("#caption").innerHTML');
      assert.equal((await markup()).replaceAll('&nbsp;',' '),expected);
      await js('deck.undo()'); assert.equal(await markup(),'그림 설명');
      await js('deck.redo()'); assert.equal((await markup()).replaceAll('&nbsp;',' '),expected);
    }
    await editReview(); await click('#caption'); await key('End');
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
    await key('Escape');
    assert.equal(await js('document.querySelector("#caption").innerHTML'),'그림 설명<br>');
    await js('deck.undo()'); assert.equal(await js('document.querySelector("#caption").innerHTML'),'그림 설명');
    await js('deck.redo()'); assert.equal(await js('document.querySelector("#caption").innerHTML'),'그림 설명<br>');
  });
  await test('A5. 모아보기 중 저장본을 다시 열면 정답을 누를 수 있음', async () => {
    await open(`${review}#3`); await js('deck.overview(true)');
    await reopenSaved('saved-overview','#3');
    assert.equal(await js('document.querySelector("#deck").hasAttribute("inert")'),false);
    await click('#peek'); assert.equal(await js('document.querySelector("#peek").classList.contains("is-open")'),true);
  });
  await test('A6. 첫 일반 장으로 시작하고 건너뛰는 장에도 직접 이동', async () => {
    await open(review);
    assert.equal(await js('deck.page'),1);
    await js("deck.setMode('present')"); assert.equal(await js('deck.page'),1);
    await open(`${review}#1`); assert.equal(await js('deck.page'),0);
    await js('deck.go(1);deck.overview(true)'); await click('#deck-overview .deck-thumbnail:first-child button'); assert.equal(await js('deck.page'),0);
    const file = path.join(work,'all-skipped.html');
    await fs.writeFile(file,(await fs.readFile(fileURLToPath(review),'utf8')).replace(/data-slide(?! data-skip)(?=[ >])/g,'data-slide data-skip'));
    await open(pathToFileURL(file).href); assert.equal(await js('deck.page'),0);
    await open(`${review}?edit`); assert.equal(await js('deck.page'),0);
  });
  await test('B7. 덱 전용 스크립트의 실행 중 버튼은 재열어도 한 개', async () => {
    await open(`${review}#3`); await reopenSaved('runtime-button','#3');
    assert.equal(await js('document.querySelectorAll("#generated > button").length'),1);
    await reopenSaved('runtime-button-again','#3');
    assert.equal(await js('document.querySelectorAll("#generated > button").length'),1);
  });
  await test('B8. 한글 자판 물리 키와 실제 조합·입력 구분', async () => {
    await open(`${review}#2`);
    for (const [keyName,code,condition] of [['ㄷ','KeyE',"deck.mode==='edit'"],['ㅇ','KeyD',"document.documentElement.dataset.theme==='dark'"],['ㅎ','KeyG',"!!document.querySelector('#deck-overview')"],['Process','KeyG',"!document.querySelector('#deck-overview')"],['ㄹ','KeyF',"deck.mode==='present'"]]) {
      assert.equal(await scopedKey('#deck',keyName,{code}),false,code);
      assert.equal(await js(condition),true,code);
    }
    await key('Escape'); await click('#caption'); await key('Escape');
    assert.equal(await scopedKey('#deck','ㅇ',{code:'KeyD',ctrlKey:true}),false);
    assert.equal(await js('deck.slides[1].querySelectorAll("figcaption").length'),2);
    await scopedKey('#deck','ㅋ',{code:'KeyZ',ctrlKey:true}); assert.equal(await js('deck.slides[1].querySelectorAll("figcaption").length'),1);
    await scopedKey('#deck','ㅛ',{code:'KeyY',ctrlKey:true}); assert.equal(await js('deck.slides[1].querySelectorAll("figcaption").length'),2);
    assert.equal(await scopedKey('@#element-font','ㄷ',{code:'KeyE'}),true);
    assert.equal(await scopedKey('#deck','Process',{code:'KeyE',isComposing:true}),true);
    assert.equal(await js('deck.mode'),'edit');
  });
  await test('B9. 가로 flex 글 덩어리 폭을 끌고 원래대로 복구', async () => {
    await editReview(); await click('#flex-item'); const before=await rect('#flex-item');
    await drag('@[data-handle="e"]',-80,0); const after=await rect('#flex-item');
    closeTo(after.width,before.width-80); closeTo(after.x,before.x);
    assert.equal(await js('getComputedStyle(deck.selection).flex'),'0 0 auto');
    await click('@#reset-element'); sameRect(await rect('#flex-item'),before);
    assert.equal(await js('getComputedStyle(deck.selection).flex'),'1 1 0px');
    await js('deck.undo()'); sameRect(await rect('#flex-item'),after);
  });
  await test('B10. 인라인 링크 한 번 눌러 글자를 고치되 링크는 열지 않음', async () => {
    await editReview(); await click('#link-box span');
    assert.equal(await js('document.activeElement.id'),'link-box');
    await key('a', 2); await type('설명으로'); await key('Escape');
    assert.deepEqual(await js('[document.querySelector("#link-box a").getAttribute("href"),document.querySelector("#link-box span").textContent,deck.page]'),['#5','설명으로',1]);
  });
  await test('B11. 복제 장이 강조색·테마·글자 배율을 따라감', async () => {
    await editReview(); await click('@#slide-copy');
    const colors = () => js(`deck.slides.slice(1,3).map(s=>{const title=getComputedStyle(s.querySelector('h2')),body=getComputedStyle(s);return [title.color,title.fontSize,body.backgroundColor,body.fontSize]})`);
    const before = await colors(); assert.deepEqual(before[0],before[1]);
    await field('accent','#ff3333'); await field('title-scale','150',true); await field('body-scale','125',true); await click('@#theme-dark');
    assert.deepEqual(await colors(),Array(2).fill(['rgb(255, 51, 51)','75px','rgb(36, 36, 36)','40px']));
  });
  await test('B12. 숨겨진 단계 제외 및 늦은 조상과 동시 공개', async () => {
    await open(`${review}#3`);
    const stage = () => js(`[deck.page,deck.step,...['late','early','last'].map(id=>document.getElementById(id).checkVisibility({visibilityProperty:true}))]`);
    assert.deepEqual(await stage(),[2,0,false,false,false]);
    await key('ArrowRight'); assert.deepEqual(await stage(),[2,1,true,true,false]);
    await key('ArrowRight'); assert.deepEqual(await stage(),[2,2,true,true,true]);
    await key('ArrowRight'); assert.deepEqual(await stage(),[3,0,false,false,false]);
  });
  await test('B13. 순서표 3인 단독 단계의 패널과 공개 동작', async () => {
    await open(`${review}#4`); await key('ArrowRight');
    assert.deepEqual(await js('[deck.step,location.hash,document.querySelector("#solo").checkVisibility({visibilityProperty:true})]'),[1,'#4.1',true]);
    await key('e'); await click('#solo',{fx:.02});
    assert.deepEqual(await js(`{const el=${selector('@#element-step')};[el.parentElement.firstChild.textContent,el.value,[...el.options].map(x=>x.textContent)]}`),['나타나는 순서','3',['처음부터','3단계','4단계']]);
  });
  const audit = () => js('deck.audit(4)');
  for (const [name,code,targets] of [
    ['B14. SVG·그라디언트 대비 제외, 단색 낮은 대비 검출','low-contrast',['#low']],
    ['B15. 장식 넘침은 제외하고 실제 글자 잘림 검출','text-clipped',['#clip-text']],
    ['B16. 줄 사이 메모는 제외하고 실제 겹침은 한 쌍만 보고','text-overlap',['#overlay']],
    ['B17. 숨은 조상 안에서 다시 보이는 작은 글자 검출','small-text',['#revived']],
  ]) await test(name, async () => {
    await open(`${review}#5`); assert.deepEqual((await audit()).filter(x=>x.code===code).map(x=>x.target),targets);
  });
  const bubbleOverlaps = markup => js(`(() => {
    const page = deck.page, step = deck.step, slide = document.createElement('section');
    slide.className = 'slide'; slide.dataset.slide = '';
    slide.innerHTML = '<style>.audit-bubbles .bubble{position:absolute;top:300px;left:160px;margin:0;font:48px sans-serif;display:block}</style>'
      + '<div class="audit-bubbles">' + ${quote(markup)} + '</div>';
    document.querySelector('#deck').append(slide);
    try {
      deck.go(deck.count - 1, 1);
      return deck.audit(deck.page).filter(issue => issue.code === 'text-overlap').map(issue => issue.target);
    } finally { deck.go(page, step); slide.remove(); }
  })()`);
  await test('B18. 단계가 바뀔 때 같은 자리에 나타나는 말풍선은 겹침 아님', async () => {
    await open(`${fixture}?audit=steps`);
    assert.deepEqual(await bubbleOverlaps(`<div class="says">
      <p class="bubble" data-until="1">첫 대사</p>
      <div data-step="1" data-until="2"><p class="bubble" data-until="3">둘째 대사</p></div>
      <p class="bubble"><span data-step="2">셋째 대사</span></p>
    </div>`), []);
  });
  await test('B19. 한 단계라도 함께 보이는 글의 실제 겹침은 보고', async () => {
    await open(`${fixture}?audit=shared-step`);
    assert.deepEqual(await bubbleOverlaps(`
      <p class="bubble" data-step="1" data-until="3">먼저 나타나는 글</p>
      <p class="bubble" id="shared-step" data-step="2">나중에 나타나는 글</p>`), ['#shared-step']);
  });
  await test('B20. 같은 마스코트의 서로 다른 반응은 겹침 아님', async () => {
    await open(`${fixture}?audit=moods`);
    // 세 반응을 모두 측정할 수 있게 표시해, 겹침 판정 자체를 확인한다.
    const reactions = `<figure class="buddy" data-buddy>
      <p class="bubble" data-when="idle">도전해 보세요</p>
      <p class="bubble" data-when="correct done">잘했습니다!</p>
      <p class="bubble" id="other-reaction" data-when="wrong">다시 해 볼까요?</p>
    </figure>`;
    assert.deepEqual(await bubbleOverlaps(reactions), []);
    assert.deepEqual(await bubbleOverlaps(reactions.replace('data-when="wrong"', 'data-when="done"')), ['#other-reaction']);
  });
  await test('B21. 함께 보이는 그림과 SVG 글자만 text-on-image 경고', async () => {
    await open(`${pathToFileURL(path.join(here, 'lint.html')).href}#1`);
    const result = await js(`(async () => {
      const pixel=document.createElement('canvas'); pixel.width=pixel.height=240;
      const ctx=pixel.getContext('2d'); ctx.fillRect(0,0,240,240);
      const slide=document.createElement('section'); slide.className='slide'; slide.dataset.slide='';
      slide.innerHTML='<div data-until="1"><img id="old-pic" style="position:absolute;left:200px;top:200px;width:240px;height:240px" src="'+pixel.toDataURL()+'"></div>'
        + '<div data-step="1"><svg width="240" height="240" style="position:absolute;left:200px;top:200px">'
        + '<text id="step-text" x="20" y="120" font-size="36">그래프 설명</text></svg></div>';
      const original=deck.page;
      document.querySelector('#deck').append(slide);
      const picture=slide.querySelector('#old-pic'); await picture.decode();
      try {
        deck.go(deck.count-1, 'all');
        const audit=()=>deck.audit(deck.page).filter(issue=>issue.code==='text-on-image').map(issue=>issue.target);
        const disjoint=audit(), restored=picture.parentElement.hasAttribute('data-gone');
        picture.parentElement.dataset.until='2'; deck.go(deck.page, 1);
        return {disjoint, restored, shared:audit()};
      } finally { deck.go(original); slide.remove(); }
    })()`,true);
    assert.deepEqual(result, {disjoint:[], restored:true, shared:['#step-text']});
  });
  await test('C1. 보기·발표의 Ctrl+S 저장 및 기본 동작 차단', async () => {
    await open(`${review}#2`);
    await js(`{window.writes=[];window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async text=>writes.push(text),close:async()=>{}})});}`);
    for (const [i,mode] of ['view','present'].entries()) {
      await js(`deck.setMode('${mode}')`);
      assert.equal(await scopedKey('#deck','ㄴ',{code:'KeyS',ctrlKey:true}),false,mode);
      await eventually(()=>js(`writes.length===${i+1}`));
      assert(await js(`writes.at(-1).includes('name="deck-saved"')`));
    }
  });
  await test('C2. 최근 편집 300건 되돌리기와 다시 하기', async () => {
    await editReview(); await click('#caption'); await key('Escape'); const initial=await rect('#caption');
    await js(`{const key=()=>document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));for(let i=0;i<301;i++)key();}`);
    for (let i=0;i<300;i++) assert.equal(await js('deck.undo()'),undefined);
    assert.equal(await js(`${selector('@#undo')}.disabled`),true);
    closeTo((await rect('#caption')).x-initial.x,2*(await rect('#deck')).width/1920,.1);
    await js('deck.redo()'); closeTo((await rect('#caption')).x-initial.x,4*(await rect('#deck')).width/1920,.1);
  });
  await test('C3. 따옴표와 역슬래시가 든 요소 글꼴 이름 왕복', async () => {
    await editReview(); await click('#caption');
    const family = `수학 "교실" 'A' \\62, B`;
    await js(`{const el=${selector('@#element-font')};el.add(new Option(${quote(family)},${quote(family)}));}`);
    await field('element-font',family);
    assert.equal(await js(`${selector('@#element-font')}.value`),family);
    const css = await js('getComputedStyle(deck.selection).fontFamily');
    await reopenSaved('quoted-font','?edit#2'); await click('#caption');
    assert.equal(await js(`${selector('@#element-font')}.value`),family);
    assert.equal(await js('getComputedStyle(deck.selection).fontFamily'),css);
  });
  await test('C4. 큰 글꼴을 바꿀 때만 용량 원문 다시 읽기', async () => {
    await editReview();
    await setFile('@#font-files',process.env.PBJ_TEST_LARGE_FONT || 'C:/Windows/Fonts/NotoSansKR-VF.ttf');
    await eventually(()=> js(`!!${selector('@#remove-font-0')}`));
    await js(`{window.fontSrcReads=0;const read=CSSStyleDeclaration.prototype.getPropertyValue;CSSStyleDeclaration.prototype.getPropertyValue=function(name){const value=read.call(this,name);if(name==='src'&&value.length>1e7)fontSrcReads++;return value;};}`);
    await js('for(let i=0;i<6;i++)deck.go(1)'); assert.equal(await js('fontSrcReads'),0);
    await click('@#remove-font-0'); await js('deck.undo()');
    assert.equal(await js('fontSrcReads'),1); assert(await js(`!!${selector('@#remove-font-0')}`));
    await js('deck.go(1)'); assert.equal(await js('fontSrcReads'),1);
  });
  await test('D2. 테마 내장 기본 글꼴은 사용자 글꼴 목록에서 제외', async () => {
    await editReview();
    assert.equal(await js(`${selector('@#body-font')}.selectedOptions[0].textContent`),'테마 기본');
    assert.equal(await js(`${selector('@#embedded-fonts')}.hidden`),true);
    assert.equal(await js(`[...${selector('@#body-font')}.options].some(o=>o.value==='Deck Sans')`),false);
    assert.match(await savedDOM(`d.querySelector('style[data-deck="basefont"]').textContent`),/Deck Sans/);
  });
  await test('D3. 3차 29구도 부품 편집·복제·저장 재열기', async () => {
    // 2차 카탈로그는 3차 kit.css의 구도가 아니다. 실제 3차 콘티와 틀로 만든다.
    const catalogDir = path.join(work,'kit-sample'), catalog = path.join(catalogDir,'견본.html');
    await fs.mkdir(path.join(catalogDir,'그림'), {recursive:true});
    const story = JSON.parse(await fs.readFile(path.join(here,'../tools/fixtures-kit/콘티.json'),'utf8'));
    story.slides.find(slide => slide.id === 'ask').mascot = 'mascot_think';
    story.assets.push({id:'mascot_think',kind:'mascot',prompt:'thinking'});
    const lab = story.slides.find(slide => slide.id === 'lab');
    lab.readouts[0] = '두 막대 3+4=7';
    const sort = story.slides.find(slide => slide.id === 'sort');
    for (const chip of sort.chips) chip.text = chip.text.replaceAll(', ', ',');
    await fs.writeFile(path.join(catalogDir,'콘티.json'), JSON.stringify(story));
    const pixel = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/6b0AAAAASUVORK5CYII=','base64');
    for (const name of ['mascot','mascot_sticks','mascot_think','sticks']) await fs.writeFile(path.join(catalogDir,'그림',`${name}.png`),pixel);
    const options = {cwd:path.resolve(here,'../..'),env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}};
    await runFile('python',['scripts/deck.py','scaffold',catalogDir],options);
    await runFile('python',['scripts/deck.py','build',catalogDir,'-o',catalog],options);
    const errors = cdp.events.length;
    await open(`${pathToFileURL(catalog).href}#6`);
    assert.equal(await js('deck.count'),29);
    const issues = (await js('deck.audit()')).filter(i => i.level === 'error');
    assert.deepEqual(issues,[],`3차 견본 audit 오류: ${JSON.stringify(issues)}`);
    await key('e');
    const active = '#deck > .slide:not([hidden])', heading = `${active} h2`, item = `${active} ol li:first-child span`;
    await click(heading,{fx:.05}); assert.equal(await js('deck.selection?.localName'),'h2'); await key('Escape');
    const before = await rect(heading); await drag(heading,34,-16);
    const moved = await rect(heading); sameRect(moved,{...before,x:before.x+34,y:before.y-16});
    await click(item,{fx:.60});
    assert.equal(await js('document.activeElement.localName'),'span');
    await key('a', 2); await type('활동 수정'); await key('Escape');
    const text = await js(`${selector(item)}.textContent`); assert.equal(text,'활동 수정');
    await js('deck.go(8)');
    const label=`${active} svg text:first-of-type`;
    await click(label); assert.equal(await js('deck.selection?.localName'),'text');
    assert(await js(`!!${selector('@#floating-text')}`));
    await js('deck.go(5)');
    const count = await js('deck.count'); await click('@#slide-copy');
    assert.equal(await js('deck.count'),count+1); sameRect(await rect(heading),moved);
    await reopenSaved('catalog-saved','?edit#7');
    assert.equal(await js('deck.count'),count+1); assert.equal(await js(`${selector(item)}.textContent`),text);
    sameRect(await rect(heading),moved);
    assert.deepEqual(cdp.events.slice(errors),[]);
    await saveShot('08-catalog-reopened');
  });
  await test('E1. 쪽 번호 속성과 숨긴 장 비활성화', async () => {
    await open(`${fixture}?edit#2`);
    const pages = () => js('deck.slides.map(s => s.getAttribute("data-deck-page")).join()');
    assert.equal(await pages(), '1,2,3,4,5,6');
    assert.equal(await js('deck.slides.map(s => s.inert).join()'), 'true,false,true,true,true,true');
    await click('@#slide-copy'); assert.equal(await pages(), '1,2,3,4,5,6,7');
    assert.equal(await js('deck.slides.map(s => s.inert).join()'), 'true,true,false,true,true,true,true');
    await click('@#slide-earlier'); assert.equal(await pages(), '1,2,3,4,5,6,7');
    await click('@#undo'); await click('@#undo'); assert.equal(await pages(), '1,2,3,4,5,6');
    await js('deck.overview(true)');
    assert.equal(await js('[...document.querySelectorAll("#deck-overview .deck-thumb-canvas > *")].map(s => s.getAttribute("data-deck-page") + (s.hasAttribute("hidden") || !s.inert ? "!" : "")).join()'), '1,2,3,4,5,6');
    await js('deck.overview(false)');
    const saved = await js('deck.serialize()');
    assert.doesNotMatch(saved, /<section[^>]*\sinert[\s=>]/);
    assert.doesNotMatch(saved, /<section[^>]*\shidden[\s=>]/);
    assert.match(saved, /<section[^>]*data-deck-page="6"/);
  });
  await test('E2. 담긴 칸보다 넓은 수식 검출', async () => {
    await open(`${fixture}#1`);
    const index = await js(`(() => {
      const s = document.createElement('section'); s.className = 'slide'; s.dataset.slide = '';
      const row = n => '<mtr><mtd><mn>'+n+'</mn><mo>+</mo><mn>'+n+'</mn></mtd><mtd><mo>=</mo><mn>'+n+'</mn></mtd></mtr>';
      const block = (id, width, n, extra = '') => '<div style="width:'+width+'px;font-size:60px;'+extra+'"><math display="block" id="'+id+'"><mtable>'+row(n)+'</mtable></math></div>';
      s.innerHTML = '<h2>수식</h2>' + block('wide', 300, '123456789') + block('squeezed', 300, '123456789', 'overflow-wrap:anywhere')
        + block('fits', 1500, '12') + '<p style="width:900px;font-size:40px">글 속 <math id="inline"><mi>x</mi><mo>+</mo><mn>1</mn></math> 수식</p>';
      document.querySelector('#deck').append(s); return deck.count - 1; })()`);
    const found = (await js(`deck.audit(${index})`)).filter(i => i.code === 'math-overflow');
    assert.deepEqual(found.map(i => i.level), ['error','error'], JSON.stringify(found));
    assert.deepEqual(found.map(i => i.target).sort(), ['#squeezed','#wide']);
  });
  await test('E3. 장 이름: data-title 먼저, 제목 속 수식은 한 줄 글자로', async () => {
    await view(); await open(`${fixture}#1`);
    const names = await js(`(()=>{
      const make = html => {
        const s=document.createElement('section'); s.className='slide'; s.dataset.slide=''; s.innerHTML=html;
        document.querySelector('#deck').append(s); return s;
      };
      const M = inner => '<math>'+inner+'</math>';
      const added = [
        make('<h2>이차함수 '+M('<mi>y</mi><mo>=</mo><mi>a</mi><msup><mi>x</mi><mn>2</mn></msup>')+'의<br>그래프</h2>'),
        make('<h2>'+M('<mi>y</mi><mo>=</mo><mo>−</mo><mfrac><mn>1</mn><mn>2</mn></mfrac><msup><mi>x</mi><mn>2</mn></msup>')+'과 '
          +M('<msqrt><mn>3</mn></msqrt>')+', '+M('<mfrac><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><mn>2</mn></mfrac>')+'</h2>'),
        make('<h2>점 '+M('<mrow><mo stretchy="false">(</mo><mn>2</mn><mo>,</mo><mn>12</mn><mo stretchy="false">)</mo></mrow>')+'과 '
          +M('<mover class="seg"><mi>AB</mi><mo>¯</mo></mover>')+', '
          +M('<msub><mi>a</mi><mn>1</mn></msub><mo>+</mo><msup><mi>x</mi><mi>k</mi></msup>')+'</h2>'),
        make('<h2>'+M('<mi>x</mi>')+'</h2>'),
      ];
      added[3].dataset.title=' 이름  먼저 ';
      const n = deck.count;
      const out = [deck.title(n-4), deck.title(n-3), deck.title(n-2), deck.title(n-1), deck.title(99), deck.title() === deck.title(deck.page)];
      deck.overview(true);
      const captions = [...document.querySelectorAll('#deck-overview .deck-thumbnail>button')].slice(-4).map(b => b.textContent);
      deck.overview(false);
      for (const s of added) s.remove();
      return [out, captions, n];
    })()`);
    assert.deepEqual(names[0], ['이차함수 y=ax²의 그래프', 'y=−½x²과 √3, (x+1)/2', '점 (2, 12)과 AB, a₁+x^(k)', '이름 먼저', '', true]);
    const n = names[2];
    assert.deepEqual(names[1], [`${n-3} · 이차함수 y=ax²의 그래프`, `${n-2} · y=−½x²과 √3, (x+1)/2`, `${n-1} · 점 (2, 12)과 AB, a₁+x^(k)`, `${n} · 이름 먼저`]);
  });
  await test('E4. 검사는 등장 효과가 끝난 자리에서 잰다', async () => {
    await view(); await open(`${fixture}#1`);
    const result = await js(`(()=>{
      const index = deck.slides.findIndex(s => s.hidden && s.querySelector('[data-step]'));
      const slide = deck.slides[index], el = slide.querySelector('[data-step]');
      const base = deck.audit(index).map(issue => issue.code);
      const style = document.createElement('style');
      style.textContent = '@keyframes probe-drop{from{transform:translateY(900px)}} #deck [data-step]:not([data-waiting]){animation:probe-drop 30s backwards}';
      document.head.append(style);
      // 전제 확인: 펼친 순간에는 효과의 첫 자리에 있고, 끝까지 보내면 바로 제자리로 온다.
      const hidden = slide.getAttribute('hidden');
      slide.removeAttribute('hidden');
      const started = el.getBoundingClientRect().top;
      for (const animation of el.getAnimations()) animation.finish();
      const settled = el.getBoundingClientRect().top;
      slide.setAttribute('hidden', hidden ?? '');
      const during = deck.audit(index).map(issue => issue.code);
      const whole = deck.audit().filter(issue => issue.slide === index + 1).map(issue => issue.code);
      style.remove();
      return { index, base, during, whole, drop: started - settled, hiddenAgain: slide.hidden };
    })()`);
    assert(result.index >= 0);
    assert(result.drop > 100, `효과가 요소를 옮기지 않았습니다: ${result.drop}`);
    assert.deepEqual(result.during, result.base);
    assert.deepEqual(result.whole, result.base);
    assert.equal(result.hiddenAgain, true);
  });
  const widgetFixture = pathToFileURL(path.join(here, 'widgets.html')).href;
  await test('F1. 확장 초기화·장 진입/퇴장·모드·키·초기화·검사', async () => {
    await open(`${widgetFixture}?probe#1`);
    assert.deepEqual(await js('calls'), ['init','enter:0:false']);
    await key('ArrowRight'); assert.deepEqual(await js('calls'), ['init','enter:0:false']);
    await key('r'); assert.equal(await js('deck.step'),1);
    await click('@#pen'); assert.equal(await js('calls.at(-1)'),'pen');
    await js('deck.go(2)'); assert.deepEqual(await js('calls.slice(-2)'),['leave:0:false','enter:2:false']);
    await js('deck.reset(0);deck.audit(0)'); assert.deepEqual(await js('calls.slice(-2)'),['reset:0','validate:0']);
    await js('deck.selftest(0)',true);
    assert(await js('calls.includes("selftest:0") && deck.page === 2'));
    await key('e'); assert.deepEqual(await js('calls.slice(-7)'),['leave:2:false','reset:0','reset:1','reset:2','reset:3','reset:4','mode:view:edit']);
  });
  await test('F2. 사라짐 단계·현재 단계·뛰어가기·확대', async () => {
    await open(widgetFixture);
    const steps = () => js(`[deck.step, ...['intro','middle','summary','until-only','jump'].map(id=>{const e=document.getElementById(id);return [e.hasAttribute('data-waiting'),e.hasAttribute('data-gone'),e.classList.contains('is-now')]})]`);
    assert.deepEqual(await steps(),[0,[false,false,false],[true,false,false],[true,false,false],[false,false,false],[false,false,false]]);
    await key('ArrowRight');
    assert.deepEqual(await steps(),[1,[false,true,false],[false,false,true],[true,false,false],[false,false,false],[false,false,false]]);
    await click('#jump');
    assert.deepEqual(await steps(),[2,[false,true,false],[false,true,false],[false,false,true],[false,false,false],[false,false,true]]);
    await key(' '); assert.equal((await steps())[0],3); assert.equal((await steps())[4][1],true);
    await click('#zoom'); assert(await js('!!document.querySelector("#deck-zoom[open]")'));
    assert((await rect('#deck-zoom .deck-zoom-content')).width > (await rect('#zoom')).width * 2);
    await saveShot('09-widget-zoom');
    await key('Escape'); assert.equal(await js('!!document.querySelector("#deck-zoom")'),false);
    await click('#zoom'); await click('#deck-zoom'); assert.equal(await js('!!document.querySelector("#deck-zoom")'),false);
    await key('ArrowRight'); assert.equal(await js('deck.page'),1);
  });
  const hasState = (s, state) => js(`!!${selector(s)}?.classList.contains(${quote(state)})`);
  const textOf = s => js(`${selector(s)}.textContent`);
  const holder = s => js(`${selector(s)}.parentElement.getAttributeNames().find(name=>['data-bin','data-lane','data-pool'].includes(name))`);
  const dragTo = async (chip, zone, touch = false) => {
    const [x,y] = await point(chip), [toX,toY] = await point(zone, .9, .85);
    if (!touch) return drag(chip, toX-x, toY-y);
    const touchAt = (type, x, y) => send('Input.dispatchTouchEvent',{type,touchPoints:x == null ? [] : [{x,y,id:1}]});
    await touchAt('touchStart', x, y);
    for (let i = 1; i <= 6; i++) await touchAt('touchMove', x+(toX-x)*i/6, y+(toY-y)*i/6);
    await touchAt('touchEnd');
  };
  await test('F3. 고르기: 오답·정답·풀이와 마스코트 반응 이벤트', async () => {
    await open(`${widgetFixture}#2`);
    await js(`{ window.answers=[]; deck.on('answer',({type,correct,solved,page})=>answers.push([type,correct,solved,page])); }`);
    await click('#wrong'); assert(await hasState('#wrong','is-wrong'));
    assert.equal(await js('document.querySelector("#buddy").dataset.mood'),'wrong');
    await click('#right');
    for (const [s, state] of [['#right','is-correct'],['#wrong','is-wrong'],['#other','is-dim'],['#quiz','is-solved'],['[data-explain]','is-open']]) assert(await hasState(s,state),state);
    assert.equal(await js('document.querySelector("#buddy").dataset.mood'),'correct');
    assert.deepEqual(await js('answers'),[['quiz',false,false,1],['quiz',true,true,1]]);
  });
  await test('F4. 한 번만 고르기와 여러 보기 확인', async () => {
    await open(`${widgetFixture}#2`);
    await click('#once [data-opt]:first-child');
    assert(await hasState('#once','is-missed')); assert(await hasState('#once','is-solved'));
    assert(await hasState('#once [data-correct]','is-correct'));
    await click('#once [data-correct]'); assert(await hasState('#once','is-missed'));
    const options = [1,2,3].map(i => `#multi [data-opt]:nth-child(${i})`);
    await click(options[0]); await click(options[1]); assert(await hasState(options[0],'is-picked'));
    await click('#multi [data-check]'); assert(await hasState(options[1],'is-wrong')); assert(!(await hasState('#multi','is-solved')));
    await click(options[1]); await click(options[2]); await click('#multi [data-check]');
    assert(await hasState('#multi','is-solved')); assert(await hasState(options[2],'is-correct'));
  });
  await test('F5. 뒤집기와 가리개는 각각 두 상태를 오감', async () => {
    await open(`${widgetFixture}#2`);
    for (const [s,state] of [['#flip','is-flipped'],['#peek','is-open']]) {
      await click(s); assert(await hasState(s,state)); await click(s); assert(!(await hasState(s,state)));
    }
  });
  await test('F6. 그룹 토글과 탭의 시작·숨김 상태', async () => {
    await open(`${widgetFixture}#2`);
    await click('#toggle-a'); assert(await hasState('#toggle-a','is-on'));
    await click('#toggle-b'); assert(await hasState('#toggle-b','is-on')); assert(!(await hasState('#toggle-a','is-on')));
    await click('#toggle-b'); assert(!(await hasState('#toggle-b','is-on')));
    assert(await hasState('[data-tab=b]','is-on')); assert(await hasState('[data-pane=a]','is-off'));
    await click('[data-tab=a]'); assert(await hasState('[data-tab=a]','is-on')); assert(await hasState('[data-pane=b]','is-off'));
    assert.deepEqual(await js(`[...document.querySelectorAll('[data-pane], [data-pane] span')].map(el=>el.hidden)`),[false,false,true]);
  });
  await test('F7. 포인터 클릭 뒤 방향키·Space, Tab 뒤 Enter·Space', async () => {
    await open(`${widgetFixture}#2`);
    await click('#wrong'); await key('ArrowRight'); assert.equal(await js('deck.page'),2);
    await click('#blue'); await key(' '); assert.equal(await js('deck.page'),3);
    await open(`${widgetFixture}?keyboard#2`);
    await key('Tab'); assert.equal(await js('document.activeElement.id'),'wrong');
    const activate = async (key, code, windowsVirtualKeyCode, text) => {
      await send('Input.dispatchKeyEvent',{type:'keyDown',key,code,windowsVirtualKeyCode,text});
      await send('Input.dispatchKeyEvent',{type:'keyUp',key,code,windowsVirtualKeyCode});
    };
    await activate('Enter','Enter',13,'\r'); assert(await hasState('#wrong','is-wrong')); assert.equal(await js('document.activeElement.id'),'wrong');
    await key('Tab'); assert.equal(await js('document.activeElement.id'),'right');
    await activate(' ','Space',32,' '); assert(await hasState('#quiz','is-solved')); assert.equal(await js('deck.page'),1);
  });
  await test('F8. 끌어 놓기 진행 표시·분류·잘못 놓은 칩 복귀', async () => {
    await open(`${widgetFixture}#3`);
    const [x,y] = await point('#blue'), [toX,toY] = await point('[data-bin=r]',.9,.8);
    await mouse('mousePressed',x,y);
    await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:toX,y:toY,buttons:1});
    assert(await hasState('#blue','is-drag')); assert(await hasState('[data-bin=r]','is-over'));
    await mouse('mouseReleased',toX,toY);
    assert.equal(await holder('#blue'),'data-pool'); assert(await hasState('#blue','is-wrong')); assert(!(await hasState('#blue','is-drag')));
    await dragTo('#blue','[data-bin=b]');
    assert.equal(await holder('#blue'),'data-bin'); assert(await hasState('#blue','is-placed')); assert(await hasState('#blue','is-correct'));
    await click('#red'); assert(await hasState('#red','is-picked')); await click('[data-bin=r]');
    assert(await hasState('#sort','is-solved'));
    await click('@#reset');
    assert.deepEqual(await js(`[...document.querySelector('#sort [data-pool]').children].map(el=>el.id||el.localName)`),['blue','em','red']);
  });
  await test('F8b. 장 안의 꾸밈에 가려져도 실제 칸에 놓기', async () => {
    await open(`${widgetFixture}#3`);
    await js(`{const bin=document.querySelector('[data-bin=b]'),slide=bin.closest('.slide');
      const r=bin.getBoundingClientRect(),s=slide.getBoundingClientRect(),scale=s.width/1920;
      const overlay=document.createElement('div');overlay.id='covering-decoration';
      Object.assign(overlay.style,{position:'absolute',left:(r.left-s.left)/scale+'px',top:(r.top-s.top)/scale+'px',width:r.width/scale+'px',height:r.height/scale+'px',zIndex:'1000'});
      slide.append(overlay);}`);
    try {
      const [x,y] = await point('[data-bin=b]',.9,.85);
      assert.deepEqual(await js(`document.elementsFromPoint(${x},${y}).slice(0,2).map(e=>e.id||e.getAttribute('data-bin'))`), ['covering-decoration','b']);
      await dragTo('#blue','[data-bin=b]');
      assert.equal(await holder('#blue'),'data-bin');
      assert(await hasState('#blue','is-correct'));
    } finally { await js("document.querySelector('#covering-decoration')?.remove();deck.reset()"); }
  });
  await test('F8c. 장 바깥의 떠 있는 장식 아래 칸에 놓기', async () => {
    await open(`${widgetFixture}#3`);
    await js(`{const bin=document.querySelector('[data-bin=b]'),r=bin.getBoundingClientRect();
      const overlay=document.createElement('div');overlay.id='covering-decoration';
      Object.assign(overlay.style,{position:'fixed',left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px',zIndex:'2147483647'});
      document.body.append(overlay);}`);
    try {
      const [x,y] = await point('[data-bin=b]',.9,.85);
      assert.deepEqual(await js(`document.elementsFromPoint(${x},${y}).slice(0,2).map(e=>e.id||e.getAttribute('data-bin'))`), ['covering-decoration','b']);
      await dragTo('#blue','[data-bin=b]');
      assert.equal(await holder('#blue'),'data-bin');
      assert(await hasState('#blue','is-correct'));
    } finally { await js("document.querySelector('#covering-decoration')?.remove();deck.reset()"); }
  });
  await test('F9. 800px 창 터치 끌기와 위젯 시작 밀기 차단', async () => {
    try {
      await view(800,600); await open(`${widgetFixture}#3`);
      closeTo((await rect('#deck')).width,800);
      await dragTo('#blue','[data-bin=b]',true);
      assert(await hasState('#blue','is-correct')); assert.equal(await js('deck.page'),2);
      await dragTo('#red','[data-bin=r]',true);
      assert(await hasState('#sort','is-solved')); assert.equal(await js('deck.page'),2);
      const [x,y] = await point('[data-bin=b]',.87,.8);
      await send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
      await send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:x-120,y}]});
      await send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
      assert.equal(await js('deck.page'),2);
    } finally { await view(); }
  });
  await test('F10. 순서 맞추기 확인과 data-keep, R로 DOM 순서 복원', async () => {
    await open(`${widgetFixture}#3`);
    await dragTo('#second','[data-lane]'); await click('#order [data-check]');
    assert(await hasState('#second','is-wrong')); assert.equal(await holder('#second'),'data-pool');
    await dragTo('#first','[data-lane]'); await dragTo('#second','[data-lane]'); await click('#order [data-check]');
    assert(await hasState('#order','is-solved')); assert(await hasState('#second','is-correct'));
    await key('ArrowRight'); await key('ArrowLeft'); assert(await hasState('#order','is-solved'));
    await key('r'); assert(!(await hasState('#order','is-solved')));
    assert.deepEqual(await js(`[...document.querySelector('#order [data-pool]').children].map(el=>el.id)`),['second','first']);
  });
  await test('F11. 숫자 허용 오차·여러 답·Enter·메모 초기화', async () => {
    await open(`${widgetFixture}#4`);
    await click('#answer'); await type('8'); await key('Enter'); assert(await hasState('#answer','is-wrong'));
    await click('#answer'); await key('a',2); await type('7.008');
    await click('#word'); await type('세변'); await click('#inputs [data-check]');
    assert(await hasState('#answer','is-correct')); assert(await hasState('#word','is-correct')); assert(await hasState('#inputs','is-solved'));
    await click('#note'); await type('수업 메모'); assert.equal(await js('document.querySelector("#note").value'),'수업 메모');
    await click('@#view-bar-next'); await key('ArrowLeft');
    assert.deepEqual(await js(`[...document.querySelectorAll('#answer,#word,#note')].map(el=>el.value)`),['','','']);
  });
  await test('F12. 타이머 비율·완료·다시 시작과 초기화', async () => {
    await open(`${widgetFixture}#4`);
    await click('#timer'); assert(await hasState('#timer','is-running'));
    assert.equal(await textOf('#timer'),'0:02');
    await eventually(() => js(`{const x=Number(document.querySelector('#timer').style.getPropertyValue('--timer-left')); 0<x && x<1;}`));
    await eventually(() => hasState('#timer','is-done'));
    assert.equal(await textOf('#timer'),'0:00'); assert(!(await hasState('#timer','is-running')));
    await click('#timer'); assert(await hasState('#timer','is-running'));
    await key('r'); assert.equal(await js('document.querySelector("#timer").innerHTML'),'<b>시간</b>');
  });
  await test('F13. 범위·목록 뽑기와 중복 없는 한 바퀴', async () => {
    await open(`${widgetFixture}#4`);
    const results = [];
    for (let i = 0; i < 3; i++) {
      await click('#pick'); assert(await hasState('#pick','is-rolling'));
      await eventually(async () => !(await hasState('#pick','is-rolling')));
      results.push(await textOf('#pick + [data-pick-out]'));
    }
    assert.deepEqual(results.sort(),['1','2','3']);
    await click('[data-pick="가,나"] button');
    await eventually(async () => ['가','나'].includes(await textOf('[data-pick="가,나"] output')) && !(await hasState('[data-pick="가,나"]','is-rolling')));
    await key('r'); assert.equal(await textOf('#pick + [data-pick-out]'),'');
  });
  await test('F14. 세기·Shift 빼기와 나갔다 오면 초기화', async () => {
    await open(`${widgetFixture}#4`);
    await click('#count'); await click('#count'); assert.equal(await textOf('[data-count-out]'),'2');
    await click('#count',{modifiers:8}); assert.equal(await textOf('[data-count-out]'),'1');
    await key('ArrowRight'); await key('ArrowLeft'); assert.equal(await textOf('[data-count-out]'),'0');
  });
  await test('F15. 퇴장·전체 편집 진입 초기화, 보통 클릭은 선택, Ctrl 클릭은 위젯', async () => {
    await open(`${widgetFixture}#2`);
    await click('#kept'); await click('#flip'); await click('#peek'); await click('[data-tab=a]');
    await key('ArrowRight'); await key('ArrowLeft');
    assert(await hasState('#kept','is-on')); assert(!(await hasState('#flip','is-flipped'))); assert(!(await hasState('#peek','is-open'))); assert(await hasState('[data-tab=b]','is-on'));
    await key('ArrowRight'); await key('e'); assert(!(await hasState('#kept','is-on')));
    await key('PageUp'); await click('#flip'); assert.equal(await js('deck.selection.id'),'flip'); assert(!(await hasState('#flip','is-flipped')));
    await click('#flip',{modifiers:2}); assert(await hasState('#flip','is-flipped'));
    await click('[data-tab=a]',{modifiers:2}); assert(await hasState('[data-tab=a]','is-on'));
    assert.equal(await js('deck.dirty'),false); await key('z',2); assert(await hasState('#flip','is-flipped'));
  });
  await test('F15b. 퀴즈 보기·분류 칩 편집과 Ctrl로 공개한 해설 수식', async () => {
    await open(`${widgetFixture}?edit&quiz-text#2`);
    const formula = ['2','×','2','×','2','×','3','×','3','=','72'];
    await js(`${selector('#quiz [data-explain]')}.innerHTML = ${quote(`<math>${formula.map((word,i) => i % 2 ? `<mo>${word}</mo>` : `<mn>${word}</mn>`).join('')}</math>`)}`);
    await click('#right'); assert.equal(await js('document.activeElement.id'), 'right');
    assert.equal(await hasState('#quiz','is-solved'), false);
    assert.equal(await js(`${selector('#quiz [data-explain]')}.checkVisibility()`), false);
    await click('#right', { modifiers: 2 }); assert(await hasState('#quiz','is-solved'));
    for (const [i, word] of formula.entries()) {
      const token = `#quiz [data-explain] math > :nth-child(${i + 1})`;
      await click(token);
      assert.equal(await js(`${selector('@#floating-text')}.value`), word);
      assert.equal(await js(`deck.selection === ${selector(token)}`), true);
      await key('Escape');
    }
    await key('PageDown'); await click('#blue');
    assert.equal(await js('document.activeElement.id'), 'blue');
    assert.equal(await hasState('#blue','is-picked'), false);
    await key('a', 2); await type('파란색'); await key('Escape');
    assert.equal(await textOf('#blue'), '파란색');
    await click('#blue', { modifiers: 2 }); assert(await hasState('#blue','is-picked'));
    await click('[data-bin=b]', { modifiers: 2 }); assert.equal(await holder('#blue'),'data-bin');
  });
  await test('F16. 편집기 장 복제·삭제·되돌리기 뒤 위젯 위임 유지', async () => {
    await open(`${widgetFixture}?edit#2`);
    const current = '#deck > .slide:not([hidden])';
    await click('@#slide-copy'); assert.equal(await js('deck.page'),2);
    await click(`${current} #flip`,{modifiers:2}); assert(await hasState(`${current} #flip`,'is-flipped'));
    await click('@#slide-delete'); await key('z',2);
    assert.equal(await js('deck.page'),2);
    await click(`${current} #peek`,{modifiers:2}); assert(await hasState(`${current} #peek`,'is-open'));
  });
  await test('F17. 직렬화·저장이 모든 장의 런타임 흔적을 빼고 재열기', async () => {
    const runtime = await inlineFixture('widgets'); await open(`${runtime}#2`);
    await click('#kept'); await key('ArrowRight');
    await dragTo('#first','[data-lane]'); await dragTo('#second','[data-lane]'); await click('#order [data-check]');
    await key('ArrowRight'); await click('#timer'); await click('#count'); await click('#note'); await type('사라질 메모');
    const serialized = await savedDOM(`(() => { const s=d.querySelector('#deck'); return [!!s.querySelector('[class*="is-"],[data-mood],[data-gone],[data-deck-rt]'),s.querySelector('#order [data-pool]').textContent,s.querySelector('#timer').innerHTML,s.querySelector('#note').value,s.querySelector('[data-count-out]').textContent]; })()`);
    assert.deepEqual(serialized,[false,'둘하나','<b>시간</b>','','0']);
    assert(!(await hasState('#order','is-solved'))); assert(!(await hasState('#kept','is-on')));
    await reopenSaved('widget-reopened','#3'); assert.equal(await holder('#first'),'data-pool');
    await dragTo('#blue','[data-bin=b]'); assert(await hasState('#blue','is-correct'));
    await key('e'); await key('PageUp'); await click('#right',{modifiers:2}); assert(await hasState('#quiz','is-solved'));
    await js('window.writes=[]; window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async text=>writes.push(text),close:async()=>{}})});');
    await key('s',2); await eventually(() => js('writes.length === 1'));
    assert(!(await hasState('#quiz','is-solved')));
    assert.equal(await js(`new DOMParser().parseFromString(writes[0],'text/html').querySelector('#quiz').classList.contains('is-solved')`),false);
  });
  await test('F18. 위젯 구조 오류 각각을 audit로 보고', async () => {
    await open(widgetFixture);
    assert.deepEqual((await js('deck.audit()')).filter(x=>x.code==='widget-invalid'),[]);
    const issues = await js(`{
      const slide=deck.slides[4];
      slide.insertAdjacentHTML('beforeend','<div id="qbad" data-quiz><button data-opt>보기</button></div><div data-sort><div data-pool><button id="noslot" data-chip data-to="x">칩</button></div></div><div data-tabs><button id="nopane" data-tab="x">탭</button><div data-pane="y">칸</div></div><button id="face" data-flip>앞</button><input id="emptyanswer" data-answer=""><button id="badtimer" data-timer="0">시간</button><button id="badpick" data-pick="7-3">뽑기</button><button id="badcount" data-count>세기</button>');
      deck.audit(4).filter(x=>x.code==='widget-invalid');
    }`);
    for (const id of ['qbad','noslot','nopane','face','emptyanswer','badtimer','badpick','badcount']) assert(issues.some(x=>x.target===`#${id}` && x.level==='error' && x.slide===5),id);
  });
  await test('F19. 정상 자체 시험, 실패 진단, hold와 R', async () => {
    await open(widgetFixture);
    assert.deepEqual(await js('deck.selftest()',true),[]);
    assert.equal(await js('deck.page'),0); assert(!(await hasState('#quiz','is-solved')));
    assert.deepEqual(await js('deck.selftest(2,{hold:true})',true),[]); assert(await hasState('#sort','is-solved'));
    await key('r'); assert(!(await hasState('#sort','is-solved')));
    await js('document.querySelector("#flip").disabled=true');
    const broken = await js('deck.selftest(1)',true);
    assert(broken.some(x=>x.code==='widget-broken' && x.target==='#flip' && x.level==='error'));
    await js('document.querySelector("#early-toggle").disabled=true');
    assert((await js('deck.selftest(0)',true)).some(x=>x.code==='widget-broken' && x.target==='#early-toggle'));
  });
  await test('F20. 저작권 표시와 편집 잠금 학생용 사본 내려받기·재열기', async () => {
    const runtime = await inlineFixture('widgets'); await open(`${runtime}#2`);
    assert(await js(`${selector('@#pen')}.hidden`));
    await key('g'); assert.equal(await textOf('#deck-overview .deck-credit'),'시험 글꼴 저작권 안내');
    await key('Escape'); await key('e'); assert.equal(await textOf('@.deck-credit'),'시험 글꼴 저작권 안내');
    const downloadDir = await fs.mkdtemp(path.join(work,'student-'));
    await send('Browser.setDownloadBehavior', { behavior:'allow', downloadPath:downloadDir });
    await click('@#save-student');
    const student = path.join(downloadDir,'inline-widgets (학생용).html');
    await eventually(() => fs.stat(student).catch(() => false));
    await open(`${pathToFileURL(student).href}?edit#2`);
    await key('e'); await js("deck.setMode('edit')");
    assert.deepEqual(await js(`[deck.mode,document.documentElement.dataset.lock,${selector('@#edit')}.hidden]`),['view','view',true]);
    await click('#flip'); assert(await hasState('#flip','is-flipped'));
  });
  await test('11. 외부 네트워크·콘솔 오류 없음', async () => {
    assert.deepEqual(cdp.requests.filter(url=>!url.startsWith('file:')&&!url.startsWith('data:')),[]);
    assert.deepEqual(cdp.events,[]);
  });
} finally {
  await cdp.stop();
  console.log(`합계: ${passed} 통과 / ${failed} 실패${skipped ? ` / ${skipped} 미실행` : ''}`);
  if (failed) process.exitCode = 1;
}
