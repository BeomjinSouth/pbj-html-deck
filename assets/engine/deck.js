(() => {
  'use strict';

  // Public API is available while the document is still being parsed.
  const doc = document, html = doc.documentElement;
  const slideSelector = 'section.slide[data-slide]';
  const all = (root, css) => Array.from(root.querySelectorAll(css));
  const make = (tag, text, className) => {
    const el = doc.createElement(tag);
    if (text != null) el.textContent = text;
    if (className) el.className = className;
    if (tag === 'button') el.type = 'button';
    return el;
  };
  const clamp = (x, min, max) => Math.min(max, Math.max(min, x));
  const getSlides = () => stage ? Array.from(stage.children).filter(el => el.matches(slideSelector)) : [];
  const activeSlide = () => getSlides()[page];
  const events = new EventTarget();
  const emit = (type, detail) => events.dispatchEvent(new CustomEvent(type, { detail }));
  let resolveReady, rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  let stage, host, ui, page = 0, step = 0, mode = 'view', priorMode = 'view';
  let overviewNode, modalFocus, selected = null, editing = null;
  let idleClock, presentSession = 0;
  const timers = new Map(), peekStates = new WeakMap();
  const waiting = new Map(), gone = new Set(), now = new Set();
  const extensions = new Map(), layers = new WeakMap(), effects = new WeakMap();
  const interactiveSelectors = new Set(['[data-jump],[data-zoom]']);
  let modalInert, zoomNode, zoomFocus, visitedSlide, visitedPage, renderedSlide, extensionsReady;
  const api = window.deck = {
    version: '3.0.0', ready,
    get count() { return getSlides().length; }, get slides() { return getSlides(); },
    get page() { return page; }, get step() { return step; }, get mode() { return mode; },
    get selection() { return selected; }, get dirty() { return isDirty(); },
    on(type, fn) {
      const listener = event => fn(event.detail);
      events.addEventListener(type, listener);
      return () => events.removeEventListener(type, listener);
    },
    go, next: () => advance(1), prev: () => advance(-1), setMode, overview,
    undo: () => historyMove(-1), redo: () => historyMove(1), serialize, audit, ext, reset, selftest,
    title: (index = page) => slideTitle(getSlides()[index])
  };

  // Extensions own their state; keep/restore records are isolated per extension.
  const active = () => !!stage && mode !== 'edit' && !editing && !overviewNode && !zoomNode;
  const scale = () => stage?.getBoundingClientRect().width / 1920 || 1;
  function point(event) {
    const r = stage?.getBoundingClientRect(), s = scale();
    return { x: clamp((event.clientX - (r?.left ?? 0)) / s, 0, 1920), y: clamp((event.clientY - (r?.top ?? 0)) / s, 0, 1080) };
  }
  function layer(slide) {
    let node = layers.get(slide);
    if (!node?.parentElement) {
      node = make('div', null, 'deck-layer'); node.dataset.deckRt = '';
      slide.append(node); layers.set(slide, node);
    }
    return node;
  }
  function ext(name, hooks) {
    if (typeof name !== 'string' || !name || !hooks || extensions.has(name)) return;
    const saved = new WeakMap();
    function keep(el, attr) {
      let values = saved.get(el);
      if (!values) saved.set(el, values = new Map());
      if (!values.has(attr)) values.set(attr, attr == null ? { text: el.textContent, nodes: [...el.childNodes] } : el.getAttribute(attr));
    }
    function apply(source, target) {
      for (const [attr, value] of saved.get(source) || []) {
        if (attr != null) writeAttrs(target, [attr], [value]);
        else if (target.textContent !== value.text || source !== target || value.nodes.some((n,i) => target.childNodes[i] !== n)) {
          target.replaceChildren(...(source === target ? value.nodes : value.nodes.map(node => node.cloneNode(true))));
        }
      }
    }
    function restore(root) {
      for (const el of [root, ...all(root, '*')]) if (saved.has(el)) { apply(el, el); saved.delete(el); }
    }
    const entry = { hooks, apply, restore, api: Object.freeze({
      active, scale, point, layer, keep, restore, result, fx, toast: announce,
      interactive(selector) { interactiveSelectors.add(selector); }
    }) };
    extensions.set(name, entry);
    if (extensionsReady) {
      hooks.init?.(entry.api);
      if (visitedSlide) hooks.enter?.(visitedSlide, visitedPage);
      refreshUI();
    }
  }
  function runHooks(name, ...args) {
    for (const { hooks } of extensions.values()) hooks[name]?.(...args);
  }
  function extensionKey(event) {
    for (const { hooks } of extensions.values()) if (hooks.key?.(event) === true) return true;
    return false;
  }
  function leaveView(next) {
    if (!visitedSlide || visitedSlide === next) return;
    const old = visitedSlide, oldPage = visitedPage;
    visitedSlide = null;
    resetInteractions(old); clearEffects(old);
    runHooks('leave', old, oldPage);
  }
  function enterView(slide) {
    if (!slide || mode === 'edit' || visitedSlide === slide) return;
    visitedSlide = slide; visitedPage = page;
    runHooks('enter', slide, page);
  }
  function resetSlide(slide) {
    if (!slide) return;
    resetInteractions(slide); clearEffects(slide);
    // Restore before reset so hooks may reapply their initial presentation classes.
    for (const { restore } of extensions.values()) restore(slide);
    runHooks('reset', slide);
    for (const buddy of all(slide, '[data-buddy][data-mood]')) buddy.removeAttribute('data-mood');
  }
  function reset(index = page) {
    const slide = getSlides()[index];
    if (!slide) return;
    if (slide === activeSlide()) { finishEdit(); closeZoom(); }
    resetSlide(slide);
    if (slide === activeSlide()) go(page);
  }
  async function selftest(index, { hold = false } = {}) {
    await ready;
    const slides = getSlides(), results = [];
    const targets = index == null ? slides : [slides[index]].filter(Boolean);
    if (!targets.length) return results;
    const before = { page, step, mode };
    let last;
    try {
      if (mode === 'edit') setMode('view');
      overview(false); closeZoom();
      for (const slide of targets) {
        const i = slides.indexOf(slide);
        resetSlide(slide); last = slide;
        go(i, 'all', false);
        // Both later arrivals and widgets that disappear at the final stage are tested.
        const erased = [...gone].filter(el => slide.contains(el));
        for (const el of erased) el.removeAttribute('data-gone');
        try {
          settle(slide);
          for (const { hooks } of extensions.values()) if (hooks.selftest) {
            try {
              for (const problem of await hooks.selftest(slide) || []) results.push(extensionIssue(slide, i, problem, 'widget-broken'));
            } catch (error) {
              results.push(extensionIssue(slide, i, { message: String(error?.message || error) }, 'widget-broken'));
            }
          }
        } finally { for (const el of erased) if (gone.has(el)) el.setAttribute('data-gone', ''); }
        if (!hold || slide !== targets.at(-1)) resetSlide(slide);
      }
    } finally {
      if (!hold) {
        resetSlide(last);
        go(before.page, before.step, false);
        if (mode !== before.mode) setMode(before.mode);
      }
    }
    return results;
  }
  function result(el, { type, correct, solved }) {
    const slide = el.closest(slideSelector);
    if (!slide) return;
    const mood = correct === false ? 'wrong' : correct === true ? 'correct' : solved ? 'done' : null;
    if (mood) for (const buddy of all(slide, '[data-buddy]')) buddy.dataset.mood = mood;
    if (correct != null || solved) fx(el, correct === false ? 'shake' : 'burst');
    emit('answer', { el, slide, page: getSlides().indexOf(slide), type, correct, solved });
  }
  function clearEffects(slide) {
    for (const stop of effects.get(slide) || []) stop();
    effects.delete(slide);
  }
  function fx(el, kind) {
    const slide = el.closest(slideSelector);
    if (!slide || !['shake','burst'].includes(kind)) return;
    let release, stop;
    let pending = effects.get(slide);
    if (!pending) effects.set(slide, pending = new Set());
    if (kind === 'shake') {
      const animation = el.animate([-10,10,-7,7,0].map(x => ({ translate: `${x}px` })), { duration: 280, composite: 'add' });
      stop = () => animation.cancel();
      animation.finished.then(() => pending.delete(stop), () => pending.delete(stop));
    } else {
      const r = el.getBoundingClientRect(), s = slide.getBoundingClientRect(), zoom = scale();
      const burst = make('div', null, 'deck-burst');
      burst.style.left = `${(r.left + r.width / 2 - s.left) / zoom}px`;
      burst.style.top = `${(r.top + r.height / 2 - s.top) / zoom}px`;
      for (let i = 0; i < 8; i++) {
        const dot = make('i'), angle = i * Math.PI / 4;
        dot.style.setProperty('--dx', `${Math.cos(angle) * 90}px`);
        dot.style.setProperty('--dy', `${Math.sin(angle) * 90}px`);
        burst.append(dot);
      }
      stop = () => { clearTimeout(release); burst.remove(); pending.delete(stop); };
      layer(slide).append(burst); release = setTimeout(stop, 460);
    }
    pending.add(stop);
  }

  // Navigation. Step numbers are labels; the public step is the count revealed.
  function positiveLabel(value) {
    const n = Number(value);
    return Number.isSafeInteger(n) && n > 0 ? n : 0;
  }
  const stepLabel = el => positiveLabel(el.dataset.step);
  function stepList(slide) {
    if (!slide) return [];
    const effective = new Map([[slide, 0]]);
    function read(el) {
      if (effective.has(el)) return effective.get(el);
      const parent = read(el.parentElement);
      const value = parent < 0 || el.hasAttribute('hidden') || getComputedStyle(el).display === 'none'
        ? -1 : Math.max(parent, stepLabel(el));
      effective.set(el, value);
      return value;
    }
    // The engine's temporary concealment must not remove its own stages.
    const concealed = [...gone].filter(el => slide.contains(el));
    for (const el of concealed) el.removeAttribute('data-gone');
    try {
      return all(slide, '[data-step],[data-until]').flatMap(el => {
        const effective = read(el), until = positiveLabel(el.dataset.until);
        return effective < 0 ? [] : [[el, stepLabel(el) ? effective : 0, until]];
      });
    } finally { for (const el of concealed) el.setAttribute('data-gone', ''); }
  }
  function syncSet(previous, next, mark) {
    for (const el of previous) if (!next.has(el)) { mark(el, false); previous.delete(el); }
    for (const el of next) if (!previous.has(el)) { mark(el, true); previous.add(el); }
  }
  function renderSteps(slide) {
    const list = slide && mode !== 'edit' ? stepList(slide) : [];
    const steps = stepsOf(list), current = steps[step - 1] || 0;
    const pending = new Set(), erased = new Set(), highlighted = new Set();
    for (const [el, reveal, until] of list) {
      if (reveal > current) pending.add(el);
      if (until && until <= current) erased.add(el);
      else if (reveal && reveal === current) highlighted.add(el);
    }
    if (slide && mode !== 'edit') for (const el of all(slide, '[data-jump]')) {
      if (el.dataset.jump.trim() !== '' && Number(el.dataset.jump) === current) highlighted.add(el);
    }
    for (const [el, original] of waiting) if (!pending.has(el)) {
      writeAttrs(el, ['data-waiting'], [original]); waiting.delete(el);
    }
    for (const el of pending) if (!waiting.has(el)) {
      waiting.set(el, el.getAttribute('data-waiting')); el.setAttribute('data-waiting', '');
    }
    syncSet(gone, erased, (el, on) => el.toggleAttribute('data-gone', on));
    syncSet(now, highlighted, (el, on) => runtimeClass(el, 'is-now', on));
  }
  const stepsOf = list => [...new Set(list.flatMap(([, n, m]) => [n,m]).filter(Boolean))].sort((a, b) => a - b);
  const numSteps = slide => stepsOf(stepList(slide)).length;
  function nearest(target, direction) {
    const slides = getSlides();
    for (let i = target; i >= 0 && i < slides.length; i += direction) {
      if (mode === 'edit' || !slides[i].hasAttribute('data-skip')) return i;
    }
    return -1;
  }
  function renderSlide(writeHash = true) {
    const slides = getSlides(), current = slides[page];
    // 숨긴 장은 inert로도 막는다(SVG 정의 때문에 레이아웃에 남는 장이 눌리거나 포커스되지 않게).
    // 쪽 번호는 장마다 속성으로 적어 둔다: 복제본·낱장·정적 화면에서도 같은 번호가 보인다.
    slides.forEach((slide, i) => {
      slide.hidden = i !== page; slide.inert = i !== page;
      if (slide.getAttribute('data-deck-page') !== String(i + 1)) slide.setAttribute('data-deck-page', i + 1);
    });
    renderedSlide = current;
    renderSteps(current);
    if (writeHash && current) {
      const hash = `#${page + 1}${step ? `.${step}` : ''}`;
      if (location.hash !== hash) history.replaceState(null, '', hash);
    }
    refreshUI();
    return current;
  }
  function go(target, reveal = 0, writeHash = true) {
    if (!stage || !api.count) return;
    finishEdit();
    cancelDrag();
    // Explicit requests (links, thumbnails, and check) address the physical slide.
    // Skip only during sequential navigation, not when the caller names a page.
    target = clamp(Math.trunc(Number(target) || 0), 0, api.count - 1);
    const next = getSlides()[target];
    if (renderedSlide !== next) {
      closeZoom(); leaveView(next);
      if (mode === 'edit' && renderedSlide?.isConnected) resetSlide(renderedSlide);
      select(null);
    }
    page = target;
    next.hidden = false;
    const max = numSteps(next);
    step = reveal === 'all' ? max : clamp(Math.trunc(Number(reveal) || 0), 0, max);
    const slide = renderSlide(writeHash);
    enterView(slide);
    emit('slidechange', { page, step, slide });
  }
  function advance(direction) {
    if (!stage || !api.count) return;
    if (mode !== 'edit' && (direction > 0 ? step < numSteps(activeSlide()) : step > 0)) {
      go(page, step + direction); return;
    }
    jump(direction, direction < 0 ? 'all' : 0);
  }
  function jump(direction, reveal = 0) {
    const target = nearest(page + direction, direction);
    if (target >= 0) go(target, reveal);
  }
  function fromHash() {
    const match = /^#(\d+)(?:\.(\d+))?$/.exec(location.hash);
    const start = location.hash ? 0 : Math.max(0, nearest(0, 1));
    go(match ? Number(match[1]) - 1 : start, match?.[2] || 0, false);
  }

  // Presentation, edge controls and mobile fit.
  let narrow;
  const panelHeight = 'min(43dvh, 370px)';
  function fitMode() {
    html.style.setProperty('--bar', mode === 'view' ? '44px' : mode === 'edit' && narrow.matches ? panelHeight : '0px');
    html.style.setProperty('--panel', mode === 'edit' && !narrow.matches ? '340px' : '0px');
  }
  const viewLocked = () => html.dataset.lock === 'view';
  const credit = () => doc.querySelector('meta[name="deck-credit"]')?.content.trim();
  function setMode(nextMode) {
    if (!stage || !['view', 'edit', 'present'].includes(nextMode) || nextMode === mode || (nextMode === 'edit' && viewLocked())) return;
    finishEdit(); cancelDrag(); overview(false); closeZoom(); select(null);
    const old = mode, session = ++presentSession;
    if (nextMode === 'present') priorMode = old;
    if (nextMode === 'edit') { leaveView(); getSlides().forEach(resetSlide); }
    else if (old === 'edit') resetSlide(activeSlide());
    mode = nextMode;
    html.dataset.deckMode = mode;
    fitMode();
    runHooks('mode', mode, old);
    if (old === 'edit' && mode !== 'edit' && activeSlide()?.hasAttribute('data-skip')) {
      const after = nearest(page, 1), before = after < 0 ? nearest(page, -1) : -1;
      go(after >= 0 ? after : before >= 0 ? before : page);
    } else enterView(renderSlide(false));
    if (mode === 'present') {
      wakeCursor();
      if (navigator.userActivation?.isActive && html.requestFullscreen) {
        html.requestFullscreen().then(() => {
          if (session !== presentSession && doc.fullscreenElement) doc.exitFullscreen().catch(() => {});
        }).catch(() => {});
      }
    } else {
      clearTimeout(idleClock); doc.body.removeAttribute('data-deck-idle');
      ui.getElementById('present-edge').hidden = true;
      if (old === 'present' && doc.fullscreenElement) doc.exitFullscreen().catch(() => {});
    }
    emit('modechange', { mode });
  }
  function wakeCursor(event) {
    if (mode !== 'present') return;
    const edge = ui.getElementById('present-edge');
    if (event) edge.hidden = event.clientY < innerHeight - 52;
    doc.body.removeAttribute('data-deck-idle');
    clearTimeout(idleClock);
    idleClock = setTimeout(() => {
      if (edge.matches(':hover') || edge.contains(ui.activeElement)) return;
      edge.hidden = true;
      doc.body.setAttribute('data-deck-idle', '');
    }, 2000);
  }

  // Thumbnail clones keep authored styling but cannot run classroom actions.
  // 장 이름: data-title이 먼저, 없으면 첫 제목의 글자.
  // 제목의 줄바꿈(<br>)은 띄어쓰기로, 수식(MathML)은 한 줄 글자(y=ax², ½, √3)로 읽는다.
  const SUPER = { 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴', 5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹', '+': '⁺', '−': '⁻', '-': '⁻', n: 'ⁿ' };
  const LOWER = { 0: '₀', 1: '₁', 2: '₂', 3: '₃', 4: '₄', 5: '₅', 6: '₆', 7: '₇', 8: '₈', 9: '₉', '+': '₊', '−': '₋', '-': '₋' };
  const FRACTION = { '1/2': '½', '1/3': '⅓', '2/3': '⅔', '1/4': '¼', '3/4': '¾', '1/5': '⅕', '2/5': '⅖', '3/5': '⅗', '4/5': '⅘', '1/6': '⅙', '5/6': '⅚', '1/8': '⅛', '3/8': '⅜', '5/8': '⅝', '7/8': '⅞' };
  const scripted = (text, table, mark) => text && [...text].every(c => table[c]) ? [...text].map(c => table[c]).join('') : `${mark}(${text})`;
  const grouped = text => /^[\p{L}\p{N}.]*$/u.test(text) ? text : `(${text})`;
  function mathLine(node) {
    const kids = [...node.children].map(mathLine);
    switch (node.localName) {
      case 'mi': case 'mn': case 'mtext': return node.textContent.trim();
      case 'mo': { const sign = node.textContent.trim(); return sign === ',' ? ', ' : sign === '¯' ? '' : sign; }
      case 'mspace': return ' ';
      case 'msup': return (kids[0] || '') + scripted(kids[1] || '', SUPER, '^');
      case 'msub': return (kids[0] || '') + scripted(kids[1] || '', LOWER, '_');
      case 'msubsup': return (kids[0] || '') + scripted(kids[1] || '', LOWER, '_') + scripted(kids[2] || '', SUPER, '^');
      case 'mfrac': return FRACTION[`${kids[0]}/${kids[1]}`] || `${grouped(kids[0] || '')}/${grouped(kids[1] || '')}`;
      case 'msqrt': return '√' + grouped(kids.join(''));
      case 'mroot': return scripted(kids[1] || '', SUPER, '') + '√' + grouped(kids[0] || '');
      case 'mover': case 'munder': case 'munderover': return kids[0] || '';
      case 'mtr': return kids.join(' ');
      case 'mtable': return kids.join(', ');
      default: return kids.join('');
    }
  }
  function headingText(heading) {
    if (!heading) return '';
    const copy = heading.cloneNode(true);
    for (const br of all(copy, 'br')) br.replaceWith(' ');
    for (const math of all(copy, 'math')) math.replaceWith(mathLine(math));
    return copy.textContent.replace(/\s+/g, ' ').trim();
  }
  const slideTitle = slide => !slide ? ''
    : (slide.dataset.title || '').replace(/\s+/g, ' ').trim() || headingText(slide.querySelector('h1,h2'));
  function overview(open, { columns } = {}) {
    if (!stage) return;
    if (!open) {
      if (overviewNode) {
        overviewNode.remove(); overviewNode = null;
        writeAttrs(stage, ['inert'], [modalInert]); modalInert = null;
        host.inert = false;
        if (modalFocus?.isConnected) modalFocus.focus({ preventScroll: true });
        modalFocus = null;
      }
      return;
    }
    finishEdit(); cancelDrag(); closeZoom(); overview(false);
    modalFocus = ui.activeElement || doc.activeElement;
    const grid = make('div', null, 'deck-overview-grid');
    const cols = clamp(Math.trunc(Number(columns)) || (innerWidth >= 1000 ? 3 : innerWidth >= 600 ? 2 : 1), 1, 12);
    grid.style.setProperty('--columns', cols);
    getSlides().forEach((source, index) => {
      const tile = make('div', null, 'deck-thumbnail');
      const canvas = make('div', null, 'deck-thumb-canvas');
      const clone = source.cloneNode(true);
      cleanSlideClone(source, clone);
      clone.inert = true;
      for (const executable of all(clone, 'script, iframe, object, embed')) executable.remove();
      canvas.append(clone);
      const title = slideTitle(source);
      const button = make('button', `${index + 1}${title ? ` · ${title}` : ''}`);
      button.onclick = () => { overview(false); go(index); };
      tile.append(canvas, button);
      tile.setAttribute('aria-current', String(page === index));
      grid.append(tile);
    });
    overviewNode = make('div'); overviewNode.id = 'deck-overview'; overviewNode.dataset.deckRt = '';
    overviewNode.setAttribute('role', 'dialog'); overviewNode.setAttribute('aria-modal', 'true');
    overviewNode.setAttribute('aria-label', '모아보기');
    const heading = make('div', null, 'deck-overview-heading');
    const close = make('button', '닫기', 'deck-overview-close'); close.onclick = () => overview(false);
    heading.append(make('h2', '모아보기'), close);
    overviewNode.append(heading, grid);
    if (credit()) overviewNode.append(make('small', credit(), 'deck-credit'));
    doc.body.append(overviewNode);
    modalInert = stage.getAttribute('inert');
    stage.inert = true; host.inert = true; close.focus();
    overviewNode.addEventListener('keydown', event => {
      if (event.key !== 'Tab') return;
      const buttons = all(overviewNode, 'button'), first = buttons[0], last = buttons.at(-1);
      if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    return { count: api.count, rows: Math.ceil(api.count / cols) };
  }

  // Classroom controls never mutate authored content permanently.
  function resetInteractions(slide) {
    if (!slide) return;
    for (const el of all(slide, '[data-peek]')) if (peekStates.has(el)) {
      restoreClass(el, 'is-open', peekStates.get(el)); peekStates.delete(el);
    }
    for (const el of all(slide, '[data-timer]')) resetTimer(el);
  }
  const classState = (el, name) => [el.classList.contains(name), el.hasAttribute('class')];
  function restoreClass(el, name, [on, hadClass]) {
    el.classList.toggle(name, on);
    if (!hadClass && el.getAttribute('class') === '') el.removeAttribute('class');
  }
  function resetTimer(el) {
    const timer = timers.get(el);
    if (!timer) return;
    clearInterval(timer.id);
    el.replaceChildren(...timer.nodes);
    restoreClass(el, 'is-done', timer.doneClass);
    timers.delete(el);
  }
  function runTimer(el) {
    const seconds = Number(el.dataset.timer);
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    resetTimer(el);
    const stop = performance.now() + 1000 * seconds;
    const timer = { nodes: [...el.childNodes], doneClass: classState(el, 'is-done'), id: 0 };
    timers.set(el, timer);
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((stop - performance.now()) / 1000));
      const text = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}`;
      if (el.textContent !== text || el.firstChild?.nodeType !== Node.TEXT_NODE) el.textContent = text;
      if (!remaining) { el.classList.add('is-done'); clearInterval(timer.id); }
    };
    timer.id = setInterval(tick, 125); tick();
  }
  const interactive = 'a,button,input,textarea,select,label,[role="button"],[data-peek],[data-timer]';
  const widgetTarget = el => el.closest?.([...interactiveSelectors].join(','));
  const editInteraction = event => event.ctrlKey && !!widgetTarget(event.target);
  function runtimeClass(el, name, on) {
    if (!on && !el.classList.contains(name)) return;
    el.classList.toggle(name, on);
    if (!el.getAttribute('class')) el.removeAttribute('class');
  }
  function blurPointerButton(event) {
    if ((event.detail > 0 || event.pointerType) && doc.activeElement?.contains(event.target) && !doc.activeElement.isContentEditable) {
      doc.activeElement.closest('button,a,[role="button"],input[type="button"]')?.blur();
    }
  }
  function closeZoom() {
    if (!zoomNode) return;
    const dialog = zoomNode, focus = zoomFocus;
    zoomNode = zoomFocus = null;
    if (dialog.open) dialog.close();
    dialog.remove(); focus?.focus({ preventScroll: true });
  }
  function openZoom(source, event) {
    if (zoomNode || source.matches('script,iframe,object,embed')) return;
    const dialog = make('dialog'); dialog.id = 'deck-zoom'; dialog.dataset.deckRt = '';
    dialog.setAttribute('aria-label','확대');
    const clone = source.cloneNode(true);
    for (const el of [clone, ...all(clone, '*')]) {
      for (const attr of ['id','data-step','data-waiting','data-gone','data-zoom','inert','contenteditable']) el.removeAttribute(attr);
      if (el !== clone && el.matches('script,iframe,object,embed,[data-deck-rt]')) el.remove();
    }
    const r = source.getBoundingClientRect(), width = r.width / scale(), height = r.height / scale();
    if (!width || !height) return;
    const wrapper = make('div', null, 'deck-zoom-content');
    wrapper.style.setProperty('--zoom-width', `${width}px`); wrapper.style.setProperty('--zoom-height', `${height}px`);
    const canvas = make('div');
    // Copy computed typography and any ID-based styles before the copy loses its ID.
    const read = getComputedStyle(source);
    for (const attr of ['color','background','font','padding','border','border-radius','text-align','object-fit']) clone.style.setProperty(attr, read.getPropertyValue(attr));
    clone.inert = true;
    canvas.append(clone); wrapper.append(canvas);
    dialog.append(wrapper, button('deck-zoom-close','닫기', closeZoom));
    zoomFocus = event.detail || event.pointerType ? null : doc.activeElement;
    dialog.addEventListener('click', closeZoom);
    dialog.addEventListener('close', () => { if (zoomNode === dialog) closeZoom(); });
    zoomNode = dialog; doc.body.append(dialog); dialog.showModal();
  }
  let touch;
  function touchStart(event) {
    touch = active() && event.touches.length === 1 && !event.target.closest(interactive + ',[data-deck-rt],[data-lab],[data-drag]') && !widgetTarget(event.target) ? event.touches[0] : null;
  }
  function touchEnd(event) {
    if (!touch) return;
    const last = Array.from(event.changedTouches).find(t => t.identifier === touch.identifier);
    if (last) {
      const dx = last.clientX - touch.clientX, dy = last.clientY - touch.clientY;
      if (Math.abs(dx) >= 55 && Math.abs(dx) >= Math.abs(dy) * 2) {
        if (event.cancelable) event.preventDefault();
        advance(dx < 0 ? 1 : -1);
      }
    }
    touch = null;
  }
  function stageClick(event) {
    const target = event.target;
    if (!stage?.contains(target) || target.closest(slideSelector) !== activeSlide()) return;
    if (mode === 'edit' && !editInteraction(event)) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (editing || overviewNode || zoomNode) return;
    const cue = target.closest('[data-jump],[data-zoom]');
    if (cue) {
      blurPointerButton(event);
      event.preventDefault(); event.stopImmediatePropagation();
      if (cue.hasAttribute('data-zoom')) openZoom(cue, event);
      else if (mode !== 'edit') {
        const label = Number(cue.dataset.jump), labels = stepsOf(stepList(activeSlide()));
        if (cue.dataset.jump.trim() && (label === 0 || labels.includes(label))) go(page, label === 0 ? 0 : labels.indexOf(label) + 1);
      }
      return;
    }
    if (extensions.has('widgets')) return;
    const control = target.closest('[data-peek],[data-timer]');
    if (!control) return;
    blurPointerButton(event);
    if (control.hasAttribute('data-timer')) runTimer(control);
    else {
      if (!peekStates.has(control)) peekStates.set(control, classState(control, 'is-open'));
      control.classList.toggle('is-open');
    }
  }
  // Shadow DOM retargets window events to the host. Keep shortcuts out of the
  // actual form control and keep both modern and legacy IME keys native.
  const composingKey = event => event.isComposing || event.keyCode === 229 || editing?.composing;
  function keydown(event) {
    if (!stage || composingKey(event) || event.defaultPrevented) return;
    const key = event.key.toLowerCase(), command = event.ctrlKey || event.metaKey;
    // Use physical letter keys on Korean and other non-Latin layouts. The
    // fallback covers synthetic keyboard events with no physical key supplied.
    const letter = /^Key[A-Z]$/.test(event.code) ? event.code.slice(3).toLowerCase() : event.code ? '' : key;
    const target = event.composedPath()[0];
    const editingTarget = editing && (target === editing.input || editing.el.contains(target));
    const inputTarget = target.isContentEditable || target.matches?.('input,textarea,select');
    if (zoomNode?.open) {
      if (key === 'escape') { event.preventDefault(); closeZoom(); }
      return;
    }
    if (!overviewNode && extensionKey(event)) { event.preventDefault(); event.stopPropagation(); return; }
    if (command && !event.altKey && letter === 's' && (!inputTarget || editingTarget)) {
      event.preventDefault(); save(); return;
    }
    if (overviewNode) {
      if (key === 'escape' || (!command && letter === 'g')) { event.preventDefault(); overview(false); }
      return;
    }
    if (editing && key === 'escape') { event.preventDefault(); finishEdit(); return; }
    if (editingTarget) {
      if (!event.altKey && command && (letter === 'z' || letter === 'y')) {
        event.preventDefault();
        historyMove(letter === 'y' || event.shiftKey ? 1 : -1);
      } else if (target.isContentEditable && !command && !event.altKey && (key === 'pageup' || key === 'pagedown')) {
        event.preventDefault();
        jump(key === 'pageup' ? -1 : 1);
      }
      return;
    }
    if (inputTarget) return;
    if (target.closest?.(interactive) && [' ', 'enter'].includes(key)) return;
    if (event.altKey || (command && mode !== 'edit')) return;
    let action;
    if (mode === 'edit') action = editKey(event, command ? letter : key, command);
    if (!action && !command) {
      if (key === 'escape') action = mode === 'present' ? () => setMode(priorMode) : mode === 'edit' ? () => select(null) : null;
      else if (letter === 'f' || key === 'f5') action = () => setMode(mode === 'present' ? priorMode : 'present');
      else if (letter === 'e' && !viewLocked()) action = () => setMode(mode === 'edit' ? 'view' : 'edit');
      else if (letter === 'r') action = () => reset();
      else if (letter === 'd') action = () => theme(html.dataset.theme === 'dark' ? 'light' : 'dark');
      else if (letter === 'g') action = () => overview(true);
      else if (key === 'home' || key === 'end') action = () => { const i = nearest(key === 'home' ? 0 : api.count - 1, key === 'home' ? 1 : -1); if (i >= 0) go(i); };
      else if (key === 'pageup' || key === 'pagedown') action = () => mode === 'edit' ? jump(key === 'pageup' ? -1 : 1) : advance(key === 'pageup' ? -1 : 1);
      else if (['arrowright','arrowdown',' ','enter'].includes(key)) action = () => advance(1);
      else if (['arrowleft','arrowup','backspace'].includes(key)) action = () => advance(-1);
    }
    if (action) { event.preventDefault(); event.stopPropagation(); action(); }
  }

  // Shared interface: all editing chrome lives in an isolated shadow root.
  const uiCSS = `
    :host{all:initial;position:fixed;inset:0;z-index:2147483000;pointer-events:none;color:#222b3b;font:14px/1.4 system-ui,"Malgun Gothic",sans-serif;--blue:#2F5BEA;--navy:#20315b;--border:#e3e7ec;--panel-height:min(43dvh, 370px)}
    *,*::before,*::after{box-sizing:border-box}
    [hidden]{display:none!important}
    button,input,select{font:inherit;color:inherit}
    button{border:0;border-radius:8px;min-height:36px;padding:0 9px;background:#F1F3F5;font-weight:650;cursor:pointer;white-space:nowrap}
    button:hover:enabled{background:#e5eaf2}
    button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
    button:disabled{cursor:default;opacity:.42}
    button[aria-pressed=true]{background:var(--navy);color:white}
    button.primary{background:var(--blue);color:white}
    button.primary:hover:enabled,button[aria-pressed=true]:hover:enabled{background:#2549bd}
    #view-bar,#present-edge{display:flex;align-items:center;justify-content:center;gap:6px;pointer-events:auto}
    #view-bar{position:absolute;bottom:0;inset-inline:0;height:44px;background:var(--deck-backdrop,#15171c);color:#b8bec9}
    #view-bar button,#present-edge button{min-height:32px}
    #view-bar button{background:#272b33;color:#c7ccd5}
    #view-bar button:hover:enabled{background:#363b45;color:#e1e5ed}
    #present-edge{position:absolute;bottom:8px;left:50%;translate:-50% 0;padding:4px;background:#171e2c;color:white;border-radius:10px;box-shadow:0 2px 8px #0005}
    #present-edge button{background:#303b4d;color:white}
    output{font-variant-numeric:tabular-nums}
    .counter{min-width:56px;text-align:center;font-weight:750;white-space:nowrap}
    .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
    @media(max-width:450px){#view-bar{gap:4px}#view-bar button{padding:0 5px;font-size:12px}}
    @media(max-width:350px){#view-bar{gap:3px}#view-bar button{padding:0 4px}#view-bar .counter{min-width:42px;font-size:12px}}
  `;
  function button(id, text, action, label = text) {
    const el = make('button', text); el.id = id; el.setAttribute('aria-label', label); el.onclick = action; return el;
  }
  function addNavigation(id, textMode) {
    const bar = make('nav'); bar.id = id; bar.setAttribute('aria-label', '슬라이드 이동');
    const counter = make('output', null, 'counter'); counter.dataset.counter = '';
    bar.append(button(`${id}-prev`, textMode ? '이전' : '‹', api.prev, '이전'), counter,
      button(`${id}-next`, textMode ? '다음' : '›', api.next, '다음'));
    return bar;
  }
  function mountUI() {
    host = make('div'); host.id = 'deck-ui'; host.dataset.deckRt = '';
    ui = host.attachShadow({ mode: 'open' });
    const style = make('style', uiCSS);
    const bar = addNavigation('view-bar', true);
    const pen = id => button(id, '펜', event => {
      if (event.detail) event.currentTarget.blur();
      extensionKey(new KeyboardEvent('keydown', { key:'p', code:'KeyP', cancelable:true }));
    });
    bar.append(button('open-overview', '모아보기', () => overview(true)), pen('pen'),
      button('reset', '처음으로', event => { if (event.detail) event.currentTarget.blur(); reset(); }),
      button('edit', '편집', () => setMode('edit')), button('present', '발표', () => setMode('present')));
    const edge = addNavigation('present-edge', false); edge.hidden = true;
    edge.append(pen('present-pen'));
    ui.append(style, bar, edge);
    mountEditor(); doc.body.append(host);
  }
  function refreshUI() {
    if (!ui) return;
    for (const counter of all(ui, '[data-counter]')) counter.value = `${api.count ? page + 1 : 0} / ${api.count}`;
    ui.getElementById('view-bar').hidden = mode !== 'view';
    ui.getElementById('edit').hidden = viewLocked();
    ui.getElementById('pen').hidden = ui.getElementById('present-pen').hidden = !extensions.has('pen');
    for (const id of ['view-bar', 'present-edge']) {
      const prev = ui.getElementById(`${id}-prev`), next = ui.getElementById(`${id}-next`);
      prev.disabled = step === 0 && nearest(page - 1, -1) < 0;
      next.disabled = step === numSteps(activeSlide()) && nearest(page + 1, 1) < 0;
    }
    refreshEditor(); drawOverlay();
  }

  function cleanSlideClone(source, clone) {
    clone.removeAttribute('hidden'); clone.removeAttribute('inert');
    cleanClone(source, clone);
  }
  function cleanClone(source, clone) {
    if (source === stage && overviewNode) writeAttrs(clone, ['inert'], [modalInert]);
    if (waiting.has(source)) writeAttrs(clone, ['data-waiting'], [waiting.get(source)]);
    if (source.closest(slideSelector)) {
      if (source.hasAttribute('data-deck-rt')) { clone.remove(); return; }
      clone.removeAttribute('data-gone'); clone.removeAttribute('data-mood');
      for (const name of [...clone.classList]) if (name.startsWith('is-')) runtimeClass(clone, name, false);
    }
    if (editing?.el === source && !editing.input) writeAttrs(clone, ['contenteditable'], [editing.editable]);
    if (peekStates.has(source)) restoreClass(clone, 'is-open', peekStates.get(source));
    const timer = timers.get(source);
    if (timer) {
      clone.replaceChildren(...timer.nodes.map(node => node.cloneNode(true)));
      restoreClass(clone, 'is-done', timer.doneClass);
    }
    const sources = timer ? timer.nodes.filter(node => node.nodeType === Node.ELEMENT_NODE) : source.children;
    const copies = [...clone.children];
    copies.forEach((child, i) => {
      if (sources[i] === editing?.filler && trailingFiller(editing)) child.remove();
      else cleanClone(sources[i], child);
    });
    for (const { apply } of extensions.values()) apply(source, clone);
  }

  // Undo transactions change only the edited DOM state, preserving unrelated nodes and listeners.
  const undoStack = [], redoStack = [];
  let revision = 0, savedRevision = 0, sequence = 0, saveHandle, saving = false;
  let dragState = null, liveSlider = null;
  const geometryAttrs = ['style', 'data-deck-e', 'data-deck-o'];
  const isDirty = () => revision !== savedRevision || Boolean(dragState?.started || liveSlider?.changed || editing?.changed());
  const attrs = (el, names) => names.map(name => el.getAttribute(name));
  function writeAttrs(el, names, values) {
    names.forEach((name, i) => {
      if (el.getAttribute(name) === values[i]) return;
      if (values[i] == null) el.removeAttribute(name); else el.setAttribute(name, values[i]);
    });
  }
  function record(back, forward, before, after) {
    if (before === after) return;
    undoStack.push({ back, forward, before: revision, after: ++sequence });
    if (undoStack.length > 300) undoStack.shift();
    redoStack.length = 0; revision = sequence;
    refreshUI(); emit('change');
  }
  function recordAttrs(el, names, before) {
    const after = attrs(el, names);
    if (before.every((value, i) => value === after[i])) return;
    record(() => writeAttrs(el, names, before), () => writeAttrs(el, names, after), before, after);
  }
  function changeAttrs(el, names, apply) {
    const before = attrs(el, names);
    apply(); recordAttrs(el, names, before);
  }
  function historyMove(direction) {
    finishEdit(); finishSlider(); cancelDrag();
    const from = direction < 0 ? undoStack : redoStack, to = direction < 0 ? redoStack : undoStack;
    const entry = from.pop();
    if (!entry) return;
    const was = activeSlide();
    if (direction < 0) { entry.back(); revision = entry.before; }
    else { entry.forward(); revision = entry.after; }
    to.push(entry);
    fitMode(); // Global style undo may restore the previous mode's layout variables.
    if (!activeSlide()) page = api.count - 1;
    if (selected && !activeSlide()?.contains(selected)) select(null);
    go(page, step);
    if (activeSlide() !== was) select(null);
    emit('change');
  }

  // Per-element geometry. Preserve authored translate (including percentages) and scale.
  const SVG = 'http://www.w3.org/2000/svg';
  const MATH = 'http://www.w3.org/1998/Math/MathML';
  const svgPart = el => el.namespaceURI === SVG && el.localName !== 'svg';
  function editData(el) {
    try { return JSON.parse(el.dataset.deckE) || {}; } catch { return {}; }
  }
  function storeEdit(el, data) {
    if (!el.hasAttribute('data-deck-o')) el.dataset.deckO = el.getAttribute('style') ?? '';
    el.dataset.deckE = JSON.stringify(data);
  }
  function cssParts(value, fallback) {
    if (!value || value === 'none') return fallback.slice();
    return value.match(/(?:[^\s(]+\([^)]*\)|[^\s()]+)+/g) || fallback.slice();
  }
  function geometry(el) {
    const data = editData(el), computed = getComputedStyle(el);
    data.baseTranslate ??= cssParts(computed.translate, ['0px','0px']);
    data.baseScale ??= cssParts(computed.scale, ['1']).map(Number);
    data.x ??= 0; data.y ??= 0; data.size ??= 1;
    return data;
  }
  const round = number => Math.round(number * 1000000) / 1000000;
  function translate(el, data) {
    const [x, y = '0px', z] = data.baseTranslate;
    const append = (base, delta) => !delta ? base : `calc(${base} + ${round(delta)}px)`;
    el.style.setProperty('translate', [append(x, data.x), append(y, data.y), z].filter(Boolean).join(' '), 'important');
  }
  function applyScale(el, data) {
    const [x, y = x, z] = data.baseScale;
    el.style.setProperty('scale', [round(x * data.size), round(y * data.size), z].filter(v => v != null).join(' '), 'important');
  }
  function prepareSVG(el) {
    if (svgPart(el)) {
      el.style.setProperty('transform-box', 'fill-box', 'important');
      el.style.setProperty('transform-origin', 'center', 'important');
    }
  }
  // The transform's basis is measured once per gesture. This maps screen deltas
  // correctly even inside rotated/scaled HTML groups and nested SVG viewBoxes.
  function translationBasis(el, data) {
    const style = el.getAttribute('style'), r = el.getBoundingClientRect();
    translate(el, { ...data, x: data.x + 64 });
    const x = el.getBoundingClientRect();
    translate(el, { ...data, y: data.y + 64 });
    const y = el.getBoundingClientRect();
    writeAttrs(el, ['style'], [style]);
    const a = (x.left - r.left) / 64, b = (x.top - r.top) / 64;
    const c = (y.left - r.left) / 64, d = (y.top - r.top) / 64, det = a * d - b * c;
    const zoom = stage.getBoundingClientRect().width / 1920 || 1;
    return (dx, dy) => Math.abs(det) > 1e-10 ? [(d * dx - c * dy) / det, (a * dy - b * dx) / det] : [dx / zoom, dy / zoom];
  }
  function offset(el, data, basis, dx, dy) {
    const [x, y] = basis(dx, dy);
    data.x = round(data.x + x); data.y = round(data.y + y); translate(el, data);
  }
  function nudge(dx, dy) {
    const el = selected, zoom = stage.getBoundingClientRect().width / 1920;
    changeAttrs(el, geometryAttrs, () => {
      const data = geometry(el), basis = translationBasis(el, data);
      storeEdit(el, data); prepareSVG(el);
      offset(el, data, basis, dx * zoom, dy * zoom);
      storeEdit(el, data);
    });
  }
  function scaleAt(el, initial, basis, size, start, hx, hy) {
    const data = { ...initial, size: round(size) };
    storeEdit(el, data); prepareSVG(el); translate(el, data); applyScale(el, data);
    const r = el.getBoundingClientRect();
    const anchor = (box, sign, min, length) => box[min] + box[length] * (sign < 0 ? 1 : sign > 0 ? 0 : .5);
    offset(el, data, basis, anchor(start, hx, 'left', 'width') - anchor(r, hx, 'left', 'width'),
      anchor(start, hy, 'top', 'height') - anchor(r, hy, 'top', 'height'));
    storeEdit(el, data);
  }

  // Hit testing and selection overlay. No class or marker is added to selected elements.
  const inlineAtoms = 'img,svg,math,button,input,textarea,select,canvas,video,iframe';
  const mathText = 'mi,mn,mo,mtext,ms';
  const svgText = 'text,tspan,textPath';
  const nonContent = 'script,style,link,meta,template,defs,title,desc';
  let outline, hoverOutline, floating, hovered, redrawId;
  const handles = [['nw',-1,-1], ['ne',1,-1], ['sw',-1,1], ['se',1,1], ['w',-1,0], ['e',1,0]];
  const textBearing = el => Boolean(el?.textContent.trim() && !el.matches(nonContent));
  const local = (el, css) => el?.matches(css);
  const inlineDisplay = display => /^(?:inline(?:$|[- ])|contents$)/.test(display);
  function isTextBox(el) {
    if (el?.namespaceURI !== html.namespaceURI || !textBearing(el) || el.matches('img,input,textarea,select,canvas,video,iframe')) return false;
    function inlineContent(child) {
      if (child.matches(inlineAtoms + ',' + nonContent)) return true;
      const display = getComputedStyle(child).display;
      return display === 'none' || (inlineDisplay(display) && [...child.children].every(inlineContent));
    }
    return [...el.children].every(inlineContent);
  }
  function hit(target, alt = false) {
    const slide = activeSlide();
    if (!target || target === slide || !slide?.contains(target) || target.closest('[data-lock],' + nonContent)) return null;
    if (alt) return target;
    const atom = target.closest('svg,math');
    if (atom && slide.contains(atom)) {
      return (selected === atom || atom.contains(selected)) ? target : atom;
    }
    while (target !== slide && getComputedStyle(target).display === 'inline' && !target.matches(inlineAtoms)) target = target.parentElement;
    return target === slide ? null : target;
  }
  function select(el) {
    selected = el && activeSlide()?.contains(el) && el !== activeSlide() && !el.closest('[data-lock]') ? el : null;
    hovered = null;
    drawOverlay(); refreshEditor();
  }
  function drawOverlay() {
    if (!outline) return;
    const draw = (box, el) => {
      box.hidden = mode !== 'edit' || !el?.isConnected || Boolean(overviewNode);
      if (box.hidden) return;
      const r = el.getBoundingClientRect();
      for (const [prop, value] of [['left',r.left], ['top',r.top], ['width',r.width], ['height',r.height]]) {
        const px = `${value}px`;
        if (box.style[prop] !== px) box.style[prop] = px;
      }
    };
    draw(outline, selected); draw(hoverOutline, hovered === selected || dragState || editing ? null : hovered);
    if (!outline.hidden) {
      outline.classList.toggle('typing', Boolean(editing));
      const canWidth = isTextBox(selected) && getComputedStyle(selected).display !== 'inline';
      for (const el of all(outline, '[data-handle]')) el.hidden = Boolean(editing) || (!canWidth && el.dataset.y === '0');
    }
    if (editing?.input) {
      const r = editing.el.getBoundingClientRect();
      editing.input.style.left = `${clamp(r.left, 4, Math.max(4, innerWidth - 170))}px`;
      editing.input.style.top = `${clamp(r.top - 38, 4, Math.max(4, innerHeight - 40))}px`;
    }
  }
  function overlayLoop() {
    if (mode === 'edit') drawOverlay();
    redrawId = requestAnimationFrame(overlayLoop);
  }
  function beginDrag(event, el, handle = '') {
    if (!el) return;
    dragState = { el, handle, id: event.pointerId, x: event.clientX, y: event.clientY, started: false };
  }
  function dragMove(event) {
    const drag = dragState;
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.started && Math.hypot(dx, dy) <= 4) return;
    const { el, handle } = drag;
    if (!drag.started) {
      drag.before = attrs(el, geometryAttrs); drag.data = geometry(el);
      drag.rect = el.getBoundingClientRect(); drag.basis = translationBasis(el, drag.data);
      const cs = getComputedStyle(el);
      drag.width = parseFloat(cs.width);
      drag.started = true;
    }
    event.preventDefault(); event.stopImmediatePropagation();
    if (!handle) {
      const data = { ...drag.data };
      storeEdit(el, data); prepareSVG(el); translate(el, data);
      const r = el.getBoundingClientRect();
      offset(el, data, drag.basis, drag.rect.left - r.left + dx, drag.rect.top - r.top + dy);
      storeEdit(el, data);
    } else {
      const [, hx, hy] = handles.find(([name]) => name === handle);
      if (hy) {
        const { width, height } = drag.rect;
        const ratio = 1 + (hx * dx * width + hy * dy * height) / (width ** 2 + height ** 2 || 1);
        scaleAt(el, drag.data, drag.basis, clamp(drag.data.size * ratio, .1, 8), drag.rect, hx, hy);
      } else if (Number.isFinite(drag.width) && drag.rect.width) {
        const data = { ...drag.data };
        storeEdit(el, data); translate(el, data);
        const parent = getComputedStyle(el.parentElement), position = getComputedStyle(el).position;
        if (/^(inline-)?flex$/.test(parent.display) && parent.flexDirection.startsWith('row') && !/^(absolute|fixed)$/.test(position)) {
          el.style.setProperty('flex', 'none', 'important');
        }
        el.style.setProperty('width', `${Math.max(16, drag.width * (1 + hx * dx / drag.rect.width))}px`, 'important');
        const r = el.getBoundingClientRect();
        offset(el, data, drag.basis, hx > 0 ? drag.rect.left - r.left : drag.rect.right - r.right, drag.rect.top - r.top);
        storeEdit(el, data);
      }
    }
    drawOverlay();
  }
  function endDrag(event) {
    const drag = dragState;
    if (!drag || event.pointerId !== drag.id) return;
    dragState = null;
    if (drag.started) recordAttrs(drag.el, geometryAttrs, drag.before);
  }
  function cancelDrag() {
    if (dragState?.started) writeAttrs(dragState.el, geometryAttrs, dragState.before);
    dragState = null;
  }

  // Direct text editing. Native caret/IME, with plaintext paste and explicit line breaks.
  function insertPlain(el, text) {
    const sel = getSelection();
    if (!sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) return;
    // Delete within each text run instead of deleting its emphasis or colour element.
    if (!range.collapsed) {
      const walker = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      const runs = [];
      while (walker.nextNode()) if (range.intersectsNode(walker.currentNode)) runs.push(walker.currentNode);
      const { startContainer, startOffset, endContainer, endOffset } = range;
      for (const node of runs) {
        const start = node === startContainer ? startOffset : 0;
        const end = node === endContainer ? endOffset : node.length;
        if (end > start) node.deleteData(start, end - start);
      }
      for (const br of all(el, 'br')) if (range.intersectsNode(br)) br.remove();
      range.collapse(true);
    }
    const parts = text.split(/\r\n?|\n/), fragment = doc.createDocumentFragment();
    parts.forEach((part, i) => { if (i) fragment.append(make('br')); if (part) fragment.append(doc.createTextNode(part)); });
    const last = fragment.lastChild;
    if (last) {
      range.insertNode(fragment); range.setStartAfter(last); range.collapse(true);
      if (last.nodeName === 'BR' && !hasTail(el, last)) {
        // Chromium otherwise places the next keystroke before a terminal BR.
        const filler = make('br');
        range.insertNode(filler); range.setStartBefore(filler); range.collapse(true);
        editing.filler = filler;
      }
    }
    sel.removeAllRanges(); sel.addRange(range);
  }
  function hasTail(el, node) {
    const range = doc.createRange();
    range.selectNodeContents(el); range.setStartAfter(node);
    const tail = range.cloneContents();
    return Boolean(tail.textContent.trim() || tail.querySelector('br,' + inlineAtoms));
  }
  function trailingFiller(session) {
    const filler = session?.filler;
    return filler && session.el.contains(filler) && !hasTail(session.el, filler) ? filler : null;
  }
  function recordTextMutations(mutations) {
    if (!mutations.length) return;
    // The next change's old value is this change's new value. Read the live
    // value only for the last mutation of each text node.
    const values = new Map(), after = [];
    for (let i = mutations.length - 1; i >= 0; i--) {
      const { type, target, oldValue } = mutations[i];
      if (type !== 'characterData') continue;
      after[i] = values.get(target) ?? target.data;
      values.set(target, oldValue);
    }
    function replay(i, forward) {
      const { type, target, oldValue, addedNodes, removedNodes, nextSibling } = mutations[i];
      if (type === 'characterData') target.data = forward ? after[i] : oldValue;
      else {
        for (const node of forward ? removedNodes : addedNodes) target.removeChild(node);
        for (const node of forward ? addedNodes : removedNodes) target.insertBefore(node, nextSibling);
      }
    }
    record(() => { for (let i = mutations.length - 1; i >= 0; i--) replay(i, false); },
      () => mutations.forEach((_, i) => replay(i, true)), null, mutations);
  }
  function trackComposition(target, session, options) {
    target.addEventListener('compositionstart', () => { session.composing = true; }, options);
    target.addEventListener('compositionend', () => { session.composing = false; }, options);
  }
  function startText(el, position) {
    if (!el) return;
    if (local(el, 'svg,math')) {
      const token = position?.target.closest(svgText + ',' + mathText);
      if (!token || !el.contains(token)) return;
      el = token;
    }
    if (editing?.el === el) return;
    finishEdit(); select(el);
    if (el.namespaceURI !== html.namespaceURI) {
      if (!el.matches(svgText + ',' + mathText)) return;
      const input = make('input'); input.type = 'text'; input.id = 'floating-text';
      input.setAttribute('aria-label', '글자');
      // An SVG text container may contain formatted tspans. Edit only the clicked token.
      const node = [...el.childNodes].find(n => n.nodeType === Node.TEXT_NODE);
      if (!node && el.children.length) return;
      const before = node ? node.data : el.textContent;
      input.value = before;
      const write = value => { if (node) node.data = value; else el.textContent = value; };
      editing = { el, input, changed: () => input.value !== before, finish: () => {
        const after = input.value; input.remove(); write(after);
        record(() => write(before), () => write(after), before, after);
      } };
      trackComposition(input, editing);
      floating.append(input); drawOverlay(); input.focus(); input.select();
      input.addEventListener('input', () => { write(input.value); refreshSave(); });
      input.addEventListener('keydown', event => { if (event.key === 'Enter' && !composingKey(event)) { event.preventDefault(); finishEdit(); } });
      input.addEventListener('blur', finishEdit);
      return;
    }
    if (!isTextBox(el)) return;
    const editable = el.getAttribute('contenteditable');
    const caret = position && doc.caretRangeFromPoint?.(position.clientX, position.clientY);
    const clicked = caret?.startContainer;
    const controller = new AbortController(), { signal } = controller;
    const mutations = [], observer = new MutationObserver(records => { for (const entry of records) mutations.push(entry); });
    const collect = () => { for (const entry of observer.takeRecords()) mutations.push(entry); };
    el.setAttribute('contenteditable', 'true');
    observer.observe(el, {childList:true, characterData:true, characterDataOldValue:true, subtree:true});
    const session = editing = { el, editable, changed: () => { collect(); return mutations.length > 0; }, finish: () => {
      controller.abort(); trailingFiller(session)?.remove();
      collect(); observer.disconnect();
      writeAttrs(el, ['contenteditable'], [editable]);
      getSelection().removeAllRanges(); el.blur();
      recordTextMutations(mutations);
    } };
    trackComposition(el, editing, { signal });
    el.focus({ preventScroll: true });
    const sel = getSelection(), range = doc.createRange();
    if (el.children.length && clicked?.nodeType === Node.TEXT_NODE && el.contains(clicked)) range.selectNodeContents(clicked);
    else range.selectNodeContents(el);
    sel.removeAllRanges(); sel.addRange(range);
    el.addEventListener('beforeinput', event => {
      if (event.isComposing || editing?.composing) return;
      if (event.inputType === 'insertParagraph' || event.inputType === 'insertLineBreak') {
        event.preventDefault(); insertPlain(el, '\n'); refreshSave();
      } else if (event.inputType === 'insertText' && el.children.length && !getSelection().isCollapsed) {
        event.preventDefault(); insertPlain(el, event.data || ''); refreshSave();
      } else if (event.inputType.startsWith('insertFromDrop')) event.preventDefault();
    }, { signal });
    el.addEventListener('paste', event => { event.preventDefault(); insertPlain(el, event.clipboardData?.getData('text/plain') || ''); refreshSave(); }, { signal });
    el.addEventListener('input', refreshSave, { signal });
    el.addEventListener('blur', finishEdit, { signal });
    drawOverlay();
  }
  function finishEdit() {
    const current = editing;
    if (!current) return;
    editing = null; current.finish(); drawOverlay(); refreshEditor();
  }

  function bindEditor() {
    window.addEventListener('pointerdown', event => {
      if (mode !== 'edit' || overviewNode || event.button !== 0 || !event.isPrimary) return;
      const target = event.composedPath()[0];
      if (target === editing?.input || editing?.el.contains(target)) return;
      finishEdit(); finishSlider();
      if (stage.contains(target) && event.ctrlKey && widgetTarget(target)) return;
      if (target.dataset?.handle) {
        event.preventDefault(); beginDrag(event, selected, target.dataset.handle); return;
      }
      if (target.getRootNode() === ui) return;
      const hitElement = hit(target, event.altKey);
      select(hitElement);
      if (stage.contains(target)) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (doc.activeElement?.getRootNode() === doc) doc.activeElement.blur();
        beginDrag(event, hitElement);
      }
    }, true);
    window.addEventListener('pointermove', event => {
      if (mode !== 'edit') return;
      dragMove(event);
      if (!dragState && !editing) hovered = hit(event.composedPath()[0], event.altKey);
    }, true);
    window.addEventListener('pointerup', endDrag, true);
    window.addEventListener('pointercancel', cancelDrag, true);
    window.addEventListener('blur', cancelDrag);
    window.addEventListener('dblclick', event => {
      if (mode !== 'edit' || !stage.contains(event.target)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!editing) startText(hit(event.target, event.altKey), event);
    }, true);
    window.addEventListener('dragstart', event => { if (mode === 'edit' && stage.contains(event.target)) event.preventDefault(); }, true);
    window.addEventListener('beforeunload', event => { if (isDirty()) { event.preventDefault(); event.returnValue = true; } });
    window.addEventListener('resize', drawOverlay);
    window.addEventListener('scroll', drawOverlay, true);
    events.addEventListener('modechange', () => {
      if (mode === 'edit') overlayLoop();
      else { cancelAnimationFrame(redrawId); drawOverlay(); }
    });
  }
  function editKey(event, key, command) {
    if (command && key === 'z') return () => historyMove(event.shiftKey ? 1 : -1);
    if (command && key === 'y') return api.redo;
    if (!selected) return;
    if (command) return key === 'd' ? duplicateElement : null;
    if (key === 'delete') return deleteElement;
    if (key === 'enter' || key === 'f2') return () => startText(selected);
    const vectors = { arrowleft: [-1,0], arrowright: [1,0], arrowup: [0,-1], arrowdown: [0,1] };
    if (vectors[key]) {
      const [x,y] = vectors[key], amount = event.shiftKey ? 20 : 2;
      return () => nudge(x * amount, y * amount);
    }
  }

  // Structural actions retain live nodes in history and authored IDs in copies.
  function duplicateElement() {
    const el = selected;
    if (!el) return;
    const clone = el.cloneNode(true), parent = el.parentNode, next = el.nextSibling;
    cleanClone(el, clone);
    parent.insertBefore(clone, next);
    const g = geometry(clone); storeEdit(clone, g);
    const zoom = stage.getBoundingClientRect().width / 1920;
    const basis = translationBasis(clone, g);
    prepareSVG(clone); offset(clone,g,basis,20 * zoom,20 * zoom); storeEdit(clone,g);
    record(() => { clone.remove(); select(el); }, () => { parent.insertBefore(clone, next); select(clone); }, null, clone);
    select(clone);
  }
  function deleteElement() {
    if (!selected) return;
    const el = selected, parent = el.parentNode, next = el.nextSibling;
    el.remove(); select(null);
    record(() => { parent.insertBefore(el, next); select(el); }, () => { el.remove(); select(null); }, el, null);
  }
  function theme(value) { changeAttrs(html, ['data-theme'], () => { html.dataset.theme = value; }); }

  // Save the real document without the presentation or editing state.
  function serialize({ student = false } = {}) {
    if (!stage) return '';
    getSlides().forEach(resetSlide);
    const root = html.cloneNode(true);
    if (student) root.dataset.lock = 'view';
    cleanClone(html, root);
    for (const slide of all(root, `#deck > ${slideSelector}`)) { slide.removeAttribute('hidden'); slide.removeAttribute('inert'); }
    // Deck scripts mark generated elements data-runtime so reopening can recreate them.
    for (const node of all(root, '[data-deck-rt],[data-runtime]')) node.remove();
    root.removeAttribute('data-deck-mode'); root.classList.remove('deck-on');
    if (root.getAttribute('class') === '') root.removeAttribute('class');
    root.style.removeProperty('--bar'); root.style.removeProperty('--panel');
    if (!root.getAttribute('style')) root.removeAttribute('style');
    root.querySelector('body').removeAttribute('data-deck-idle');
    let saved = root.querySelector('meta[name="deck-saved"]');
    if (!saved) { saved = make('meta'); saved.name = 'deck-saved'; root.querySelector('head').append(saved); }
    saved.content = new Date().toISOString();
    return '<!doctype html>\n' + root.outerHTML;
  }
  function fileName() {
    try { return decodeURIComponent(location.pathname.split('/').pop()) || 'deck.html'; }
    catch { return 'deck.html'; }
  }
  function downloadHTML(data, name) {
    const url = URL.createObjectURL(new Blob([data], { type: 'text/html;charset=utf-8' }));
    const link = make('a'); link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  function saveStudent() {
    if (saving || mode !== 'edit') return;
    finishEdit(); finishSlider(); cancelDrag();
    downloadHTML(serialize({ student: true }), `${fileName().replace(/\.html?$/i, '')} (학생용).html`);
  }
  async function save() {
    if (saving) return;
    finishEdit(); finishSlider(); cancelDrag();
    const name = fileName();
    saving = true;
    try {
      let message;
      if (typeof window.showSaveFilePicker === 'function') {
        saveHandle ??= await window.showSaveFilePicker({ suggestedName: name, types: [{ description: 'HTML', accept: { 'text/html': ['.html', '.htm'] } }] });
      }
      const saved = revision, data = serialize();
      if (saveHandle) {
        const file = await saveHandle.createWritable();
        try { await file.write(data); await file.close(); }
        catch (error) { await file.abort().catch(() => {}); throw error; }
        message = '저장했습니다';
      } else {
        downloadHTML(data, name);
        message = '내려받기 폴더에 저장했습니다';
      }
      savedRevision = saved; announce(message); emit('save');
    } catch (error) {
      if (error.name !== 'AbortError') announce('저장하지 못했습니다. 수정 내용은 화면에 남아 있습니다.');
    } finally { saving = false; refreshSave(); }
  }

  // Slide actions keep node identity and use the same undo stack as element edits.
  function slideAction(action) {
    const slide = activeSlide(), oldPage = page;
    if (!slide) return;
    if (action === 'new' || action === 'copy') {
      if (action === 'copy') resetSlide(slide);
      const newSlide = action === 'copy' ? slide.cloneNode(true) : make('section', null, 'slide');
      if (action === 'copy') cleanSlideClone(slide, newSlide);
      else {
        newSlide.dataset.slide = '';
        newSlide.append(make('h2', '제목'), make('p', '내용'));
      }
      newSlide.hidden = true;
      const next = slide.nextSibling;
      const place = () => { stage.insertBefore(newSlide, next); page = oldPage + 1; step = 0; };
      place();
      record(() => { newSlide.remove(); page = oldPage; }, place, null, newSlide);
      select(null); go(page);
    } else if (action === 'delete' && api.count > 1) {
      const next = slide.nextSibling;
      const remove = () => { resetSlide(slide); slide.remove(); page = Math.min(oldPage, api.count - 1); step = 0; };
      remove();
      record(() => { stage.insertBefore(slide, next); page = oldPage; }, remove, slide, null);
      select(null); go(page);
    } else if (action === 'earlier' || action === 'later') {
      const earlier = action === 'earlier', other = getSlides()[oldPage + (earlier ? -1 : 1)];
      if (!other) return;
      const from = slide.nextSibling, to = earlier ? other : other.nextSibling;
      const move = (next, nextPage) => { stage.insertBefore(slide, next); page = nextPage; };
      move(to, oldPage + (earlier ? -1 : 1));
      record(() => move(from, oldPage), () => move(to, oldPage + (earlier ? -1 : 1)), from, to);
      go(page, step);
    }
  }
  function insertElement(el) {
    const slide = activeSlide();
    if (!slide) return;
    slide.append(el);
    record(() => { el.remove(); select(null); }, () => { slide.append(el); select(el); }, null, el);
    select(el);
  }
  function addText() {
    const el = make('p', '글자', 'added-text');
    el.style.cssText = 'position:absolute;left:50%;top:50%;translate:-50% -50%';
    insertElement(el);
  }
  function styleSelection(apply) {
    if (!selected) return;
    const el = selected;
    changeAttrs(el, geometryAttrs, () => { storeEdit(el, editData(el)); apply(el); });
  }
  function restoreElement() {
    if (!selected?.hasAttribute('data-deck-o')) return;
    const el = selected;
    changeAttrs(el, geometryAttrs, () => writeAttrs(el, geometryAttrs, [el.dataset.deckO || null, null, null]));
  }
  function stackElement(front) {
    const el = selected;
    if (!el) return;
    if (svgPart(el)) {
      const parent = el.parentElement, from = el.nextSibling, to = front ? null : parent.firstChild;
      if (to === from || to === el) return;
      parent.insertBefore(el, to);
      record(() => parent.insertBefore(el, from), () => parent.insertBefore(el, to), from, to);
      return;
    }
    const layers = [...el.parentElement.children].map(sibling => Number(getComputedStyle(sibling).zIndex) || 0);
    styleSelection(target => {
      if (getComputedStyle(target).position === 'static') target.style.position = 'relative';
      target.style.zIndex = front ? Math.max(0, ...layers) + 1 : Math.min(0, ...layers) - 1;
    });
  }

  // Inline styles and embedded assets: never fetch an external font or picture.
  const globals = ['--title-scale','--body-scale','--font-title','--font-body','--accent'];
  const globalStyle = name => getComputedStyle(html).getPropertyValue(name).trim();
  const fontList = ['Pretendard','Noto Sans KR','Noto Serif KR','Malgun Gothic','NanumGothic','NanumMyeongjo','NanumSquare','NanumSquareRound','NanumBarunGothic','Gmarket Sans','S-Core Dream','KoPubDotum','KoPubBatang','Source Han Sans K','Batang','Dotum','Gulim','Gungsuh','HCR Batang','HCR Dotum'];
  const koreanFonts = ['Pretendard','Noto Sans KR','Noto Serif KR','맑은 고딕','나눔고딕','나눔명조','나눔스퀘어','나눔스퀘어라운드','나눔바른고딕','G마켓 산스','에스코어 드림','KoPub돋움','KoPub바탕','본고딕','바탕','돋움','굴림','궁서','함초롬바탕','함초롬돋움'];
  const cssText = value => '"' + String(value).replace(/["\\<>\x00-\x1f\x7f]/g, char => `\\${char.codePointAt(0).toString(16)} `) + '"';
  function selectedFont(value) {
    // CSSOM may return either hex escapes or escaped quotes/backslashes.
    // Read a complete first family and unescape exactly once.
    const first = /^\s*(?:"((?:\\[\s\S]|[^"\\])*)"|'((?:\\[\s\S]|[^'\\])*)'|((?:\\[\s\S]|[^,\\])+))/.exec(value || '');
    return (first?.[1] ?? first?.[2] ?? first?.[3]?.trim() ?? '').replace(/\\(?:([a-f\d]{1,6})(?:\r\n|[\t\n\r\f ])?|(\r\n|[\n\r\f])|([\s\S]))/gi, (_, hex, newline, char) => {
      if (!hex) return newline ? '' : char;
      const cp = parseInt(hex, 16);
      return String.fromCodePoint(cp && cp <= 0x10ffff && !(cp >= 0xd800 && cp <= 0xdfff) ? cp : 0xfffd);
    });
  }
  let fontsStyle, installed, fontInventory, fontObserver, lastFontInventory;
  function detectFonts() {
    const ctx = make('canvas').getContext('2d');
    if (!ctx) return new Map();
    const samples = ['WwMmi0123 한국어글꼴 가나다', 'lIIIhH∑ 알파벳'];
    const fallbacks = ['monospace','serif','sans-serif'];
    const measure = (family, sample) => { ctx.font = `44px ${family}`; return ctx.measureText(sample).width; };
    const list = new Map();
    for (const [index, name] of fontList.entries()) {
      for (const candidate of new Set([name,koreanFonts[index]])) {
        if (fallbacks.some(base => samples.some(sample => Math.abs(measure(`${cssText(candidate)},${base}`,sample) - measure(base,sample)) > .02))) {
          list.set(candidate, koreanFonts[index]); break;
        }
      }
    }
    return list;
  }
  function styleRules(sheet) { try { return Array.from(sheet?.cssRules || []); } catch { return []; } }
  function embeddedFonts() {
    if (fontObserver.takeRecords().length) fontInventory = null;
    if (fontInventory) return fontInventory;
    const results = new Map();
    // Only the editor's user fonts are listed here; Deck Sans is a kit default.
    styleRules(fontsStyle.sheet).forEach((rule, index) => {
      if (rule.type !== CSSRule.FONT_FACE_RULE) return;
      const src = rule.style.getPropertyValue('src');
      if (!/\burl\(\s*["']?data:/i.test(src)) return;
      const name = selectedFont(rule.style.getPropertyValue('font-family'));
      const base64 = /;base64,([a-z0-9+/=]+)/i.exec(src)?.[1];
      const padding = base64?.endsWith('==') ? 2 : base64?.endsWith('=') ? 1 : 0;
      const bytes = base64 ? Math.floor((base64.length - padding) * 3 / 4) : 0;
      if (name) results.set(name, { name, bytes, index });
    });
    return fontInventory = results;
  }
  const fileFormats = { woff2: ['woff2','font/woff2'], woff: ['woff','font/woff'], ttf: ['truetype','font/ttf'], otf: ['opentype','font/otf'] };
  function readData(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result); reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }
  function changeFonts(after) {
    const before = fontsStyle.textContent;
    fontsStyle.textContent = after;
    record(() => { fontsStyle.textContent = before; }, () => { fontsStyle.textContent = after; }, before, after);
  }
  async function addFonts(files) {
    const taken = new Set(embeddedFonts().keys()), lines = [];
    let failed = false;
    for (const file of files) {
      const extension = /\.([^.]+)$/.exec(file.name)?.[1].toLowerCase();
      if (!Object.hasOwn(fileFormats, extension)) { failed = true; continue; }
      const [format, mime] = fileFormats[extension];
      const base = file.name.slice(0, -(extension.length + 1)).trim() || '글꼴';
      let name = base, n = 1;
      while (taken.has(name)) name = `${base} ${++n}`;
      try {
        await new FontFace(cssText(name), await file.arrayBuffer(), { weight: '100 900' }).load();
        const data = (await readData(file)).replace(/^data:[^,]*,/, `data:${mime};base64,`);
        taken.add(name);
        lines.push(`@font-face{font-family:${cssText(name)};src:url(${data}) format("${format}");font-weight:100 900;font-display:swap}`);
      } catch { failed = true; }
    }
    if (lines.length) changeFonts([fontsStyle.textContent.trimEnd(), ...lines].filter(Boolean).join('\n'));
    if (failed) announce('읽지 못한 글꼴 파일이 있습니다.');
  }
  function removeFont(index) {
    const next = styleRules(fontsStyle.sheet).filter((rule, i) => i !== index).map(rule => rule.cssText).join('\n');
    changeFonts(next);
  }
  async function localFonts() {
    try {
      const list = await window.queryLocalFonts();
      for (const { family } of list) if (!installed.has(family)) installed.set(family, family);
      lastFontInventory = null; refreshEditor();
    } catch (error) { if (error.name !== 'AbortError') announce('설치된 글꼴을 읽지 못했습니다.'); }
  }
  async function usePicture(file, target, destination) {
    try {
      if (!file.type.startsWith('image/')) throw Error('image');
      const src = await readData(file);
      const probe = new Image(); probe.src = src; await probe.decode();
      if (mode !== 'edit' || activeSlide() !== destination || (target && !destination.contains(target))) return;
      if (!target) {
        const el = make('img', null, 'added-image'); el.src = src; el.alt = file.name.replace(/\.[^.]+$/, '');
        el.style.cssText = 'position:absolute;left:50%;top:50%;translate:-50% -50%;width:640px';
        insertElement(el);
      } else if (target.localName === 'img') {
        const candidates = [target, ...(target.parentElement.matches('picture') ? all(target.parentElement, 'source') : [])];
        const names = ['src','srcset'], before = candidates.map(node => attrs(node, names));
        for (const node of candidates) node.removeAttribute('srcset');
        target.src = src;
        const after = candidates.map(node => attrs(node, names));
        const write = values => candidates.forEach((node, index) => writeAttrs(node, names, values[index]));
        record(() => write(before), () => write(after), before, after);
      } else changeAttrs(target, ['href'], () => target.setAttribute('href', src));
    } catch { announce('그림 파일을 읽지 못했습니다.'); }
  }
  function fontBasis(el, data) {
    if (data.fontBasis) return data.fontBasis;
    const current = parseFloat(getComputedStyle(el).fontSize) || 16;
    for (const name of globals.slice(0, 2)) {
      const original = html.getAttribute('style');
      const value = Number(globalStyle(name)) || 1;
      const probe = value * 1.25;
      html.style.setProperty(name, String(probe), 'important');
      const after = parseFloat(getComputedStyle(el).fontSize);
      writeAttrs(html, ['style'], [original]);
      if (Math.abs(after - current) > .02) return { px: round(current / value), variable: name };
    }
    return { px: current };
  }
  function fontSize(el, size) {
    const data = editData(el), basis = fontBasis(el, data);
    data.fontBasis = basis; data.fontSize = size; storeEdit(el, data);
    const value = basis.variable ? `calc(${basis.px}px * ${size} * var(${basis.variable},1))` : `${round(basis.px * size)}px`;
    el.style.setProperty('font-size', value, 'important');
  }

  // The panel builds controls only once; updates do not reset scroll or focused sliders.
  const control = id => ui.getElementById(id);
  const setPressed = (id, on) => control(id).setAttribute('aria-pressed', String(Boolean(on)));
  let panel, toast, toastClock, lastSelection, lastFontSelection, pictureTarget, pictureSlide;
  function announce(message) {
    if (!toast) return;
    clearTimeout(toastClock); toast.textContent = message; toast.hidden = false;
    toastClock = setTimeout(() => { toast.hidden = true; }, 3200);
  }
  function refreshSave() {
    const button = control('save');
    if (!button) return;
    button.classList.toggle('primary', isDirty());
    button.setAttribute('aria-label', isDirty() ? '저장 · 변경 사항 있음' : '저장');
    button.disabled = saving;
    control('undo').disabled = !undoStack.length && !editing?.changed() && !liveSlider?.changed;
  }
  function boundSlider(id, getEl, names, apply, prepare) {
    const range = control(id);
    range.addEventListener('input', () => {
      if (liveSlider?.range !== range) {
        finishSlider();
        const el = getEl();
        if (!el) return;
        liveSlider = { range, el, names, before: attrs(el, names), context: prepare?.(el) };
      }
      const s = liveSlider;
      apply(s.el, Number(range.value) / 100, s.context);
      s.changed = attrs(s.el, s.names).some((value, i) => value !== s.before[i]);
      control(`${id}-value`).value = `${range.value}%`;
      drawOverlay(); refreshSave();
    });
    range.addEventListener('change', () => { if (liveSlider?.range === range) finishSlider(); });
    range.addEventListener('blur', () => { if (liveSlider?.range === range) finishSlider(); });
  }
  function finishSlider() {
    if (!liveSlider) return;
    const s = liveSlider; liveSlider = null;
    recordAttrs(s.el, s.names, s.before);
  }
  function syncSlider(id, value) {
    if (liveSlider?.range.id === id) return;
    const range = control(id), percent = Math.round(value * 100);
    if (id === 'size') range.max = String(Math.max(400, percent));
    range.value = percent;
    control(`${id}-value`).value = `${percent}%`;
  }
  function fillFontSelect(select, inventory, value) {
    const option = (text, key) => { const n = make('option', text); n.value = key; return n; };
    select.replaceChildren(option('테마 기본', ''));
    if (inventory.size) {
      const group = make('optgroup'); group.label = '파일에 포함';
      for (const name of inventory.keys()) group.append(option(name, name));
      select.append(group);
    }
    if (installed.size) {
      const group = make('optgroup'); group.label = '이 컴퓨터에 설치';
      for (const [name, label] of installed) if (!inventory.has(name)) group.append(option(label, name));
      select.append(group);
    }
    if (value && ![...select.options].some(option => option.value === value)) select.append(option(value, value));
    select.value = value;
  }
  function refreshFonts() {
    const inventory = embeddedFonts();
    const values = [selectedFont(html.style.getPropertyValue('--font-title')), selectedFont(html.style.getPropertyValue('--font-body')), selectedFont(selected?.style.fontFamily)];
    if (inventory !== lastFontInventory || values.some((value, index) => value !== lastFontSelection?.[index])) {
      for (const [index, id] of ['title-font','body-font','element-font'].entries()) fillFontSelect(control(id), inventory, values[index]);
      lastFontSelection = values;
    }
    if (inventory !== lastFontInventory) {
      const list = control('embedded-fonts'); list.replaceChildren();
      for (const { name, bytes, index } of inventory.values()) {
        const row = make('div', null, 'font-row');
        row.append(make('span', name), make('output', bytes ? `${Math.max(1, Math.round(bytes / 1024))} KB` : ''),
          button(`remove-font-${index}`, '삭제', () => removeFont(index), `${name} 삭제`));
        list.append(row);
      }
      list.hidden = !inventory.size; lastFontInventory = inventory;
    }
    let hasLocal = false;
    for (const [i,id] of ['title-font','body-font','element-font'].entries()) {
      const show = Boolean(values[i] && !inventory.has(values[i]));
      if (i < 2) hasLocal ||= show;
      else control('element-font-fallback').hidden = !show;
    }
    control('font-fallback').hidden = !hasLocal;
  }
  function colorHex(value) {
    const context = colorHex.context ||= make('canvas').getContext('2d');
    if (!context) return '#000000';
    context.fillStyle = '#000000'; context.fillStyle = value;
    const color = context.fillStyle;
    if (/^#[a-f0-9]{6}$/i.test(color)) return color;
    context.fillRect(0,0,1,1);
    const rgb = [...context.getImageData(0,0,1,1).data].slice(0,3);
    context.clearRect(0,0,1,1);
    return '#' + rgb.map(part => part.toString(16).padStart(2,'0')).join('');
  }
  function updateCrumbs(el) {
    if (el === lastSelection) return;
    lastSelection = el;
    const path = control('selection-path'); path.replaceChildren();
    const chain = [];
    for (let n = el; n && n !== activeSlide(); n = n.parentElement) chain.unshift(n);
    const labels = { h1:'제목',h2:'제목',p:'문단',div:'묶음',img:'그림',image:'그림',svg:'도형',math:'수식',text:'글자',strong:'강조',button:'버튼' };
    path.append(button('path-slide', '장', () => select(null)));
    chain.forEach((node,i) => {
      const divider = make('span','›'); divider.setAttribute('aria-hidden','true');
      path.append(divider, button(`path-${i}`, node.dataset.name || labels[node.localName] || node.localName, () => select(node)));
    });
  }
  function refreshEditor() {
    if (!panel) return;
    panel.hidden = mode !== 'edit';
    if (panel.hidden) return;
    const slide = activeSlide(), el = selected;
    refreshSave();
    for (const [id, off] of [['redo',!redoStack.length],['slide-nav-prev',page===0],['slide-nav-next',page>=api.count-1],['slide-delete',api.count<=1],['slide-earlier',page===0],['slide-later',page>=api.count-1]]) control(id).disabled = off;
    control('skip-slide').checked = slide?.hasAttribute('data-skip') || false;
    setPressed('theme-light', html.dataset.theme !== 'dark'); setPressed('theme-dark', html.dataset.theme === 'dark');
    syncSlider('title-scale', Number(globalStyle('--title-scale')) || 1);
    syncSlider('body-scale', Number(globalStyle('--body-scale')) || 1);
    control('accent').value = colorHex(globalStyle('--accent') || '#2f5bea');
    control('selection-empty').hidden = Boolean(el);
    control('selection-fields').hidden = !el;
    if (el) {
      updateCrumbs(el);
      const data = editData(el), cs = getComputedStyle(el), text = textBearing(el);
      syncSlider('size', data.size || 1); syncSlider('font-size', data.fontSize || 1);
      control('text-controls').hidden = !text;
      control('picture-replace').hidden = !el.matches('img,image');
      control('reset-element').disabled = !el.hasAttribute('data-deck-o');
      if (text) {
        setPressed('bold', Number(cs.fontWeight) >= 600 || cs.fontWeight === 'bold');
        const alignment = cs.textAlign === 'start' ? (cs.direction === 'rtl' ? 'right' : 'left') : cs.textAlign;
        for (const type of ['left','center','right']) setPressed(`align-${type}`, alignment === type);
        const prop = el.style.getPropertyValue('color');
        setPressed('color-default', !prop); setPressed('color-accent', prop === 'var(--accent)');
        setPressed('color-muted', prop.startsWith('var(--muted'));
        control('color-custom').value = colorHex(cs.color);
      }
      const select = control('element-step'), own = Number(el.dataset.step) || 0;
      // Show authored labels, even when an ancestor determines the reveal.
      const numbers = [...new Set(all(slide, '[data-step]').map(stepLabel).filter(Boolean))].sort((a, b) => a - b);
      const choices = [0, ...new Set([...numbers, (numbers.at(-1) || 0) + 1])];
      if (select.dataset.steps !== choices.join()) {
        select.replaceChildren(...choices.map(number => { const option = make('option', number ? `${number}단계` : '처음부터'); option.value = number; return option; }));
        select.dataset.steps = choices.join();
      }
      select.value = own;
    } else lastSelection = null;
    refreshFonts();
  }
  // Layout audit. Measure the live slide briefly so ID selectors, authored CSS and
  // already-decoded assets are exactly the same as on stage. Restore in finally.
  const svgImages = new Map();
  const drawable = 'img,image,video,canvas,path,line,rect,circle,ellipse,polygon,polyline,use';
  function imageSource(el) { return el.localName === 'img' ? el.currentSrc || el.getAttribute('src') : el.getAttribute('href') || el.getAttributeNS('http://www.w3.org/1999/xlink','href'); }
  function preloadSVGImage(el) {
    const source = imageSource(el);
    if (!source || /^https?:|^\/\//i.test(source)) return Promise.resolve();
    if (svgImages.has(source)) return svgImages.get(source).ready;
    const image = new Image(), entry = {};
    image.src = source;
    entry.ready = image.decode().then(() => { entry.loaded = true; }, () => { entry.loaded = false; });
    svgImages.set(source, entry);
    return entry.ready;
  }
  function cssPath(el) {
    const pieces = [];
    for (let node = el; node !== stage && node?.parentElement; node = node.parentElement) {
      if (node.id && doc.getElementById(node.id) === node) { pieces.unshift(`#${CSS.escape(node.id)}`); return pieces.join(' > '); }
      const index = [...node.parentElement.children].indexOf(node) + 1;
      pieces.unshift(`${CSS.escape(node.localName)}:nth-child(${index})`);
    }
    if (stage.contains(el) || el === stage) pieces.unshift('#deck');
    return pieces.join(' > ');
  }
  function normalizedRect(r, origin, zoom) {
    return { x: round((r.left - origin.left) / zoom), y: round((r.top - origin.top) / zoom), width: round(r.width / zoom), height: round(r.height / zoom) };
  }
  const area = r => r.width * r.height;
  function overlap(a,b) {
    return Math.max(0, Math.min(a.x+a.width,b.x+b.width)-Math.max(a.x,b.x)) * Math.max(0, Math.min(a.y+a.height,b.y+b.height)-Math.max(a.y,b.y));
  }
  const channels = color => {
    if (!color) return null;
    const canvas = channels.context ||= make('canvas').getContext('2d', { willReadFrequently:true });
    if (!canvas) return null;
    canvas.clearRect(0,0,1,1); canvas.fillStyle = 'transparent'; canvas.fillStyle = color;
    canvas.fillRect(0,0,1,1);
    return [...canvas.getImageData(0,0,1,1).data].map(v=>v/255);
  };
  function composite(foreground, background) {
    const alpha = foreground[3] + background[3] * (1 - foreground[3]);
    return alpha ? [...foreground.slice(0,3).map((v,i) => (v * foreground[3] + background[i] * background[3] * (1-foreground[3])) / alpha), alpha] : [0,0,0,0];
  }
  function luminance(rgb) {
    return rgb.slice(0,3).reduce((sum,v,i) => sum + [0.2126,0.7152,0.0722][i] * (v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4), 0);
  }
  function contrast(el, style) {
    // The SVG painting order, not the CSS ancestry, determines its background.
    if (el.closest('svg')) return null;
    let back = [0,0,0,0];
    for (let node = el; node; node = node.parentElement) {
      const cs = style(node);
      if (cs.backgroundImage !== 'none' || Number(cs.opacity) !== 1 || cs.mixBlendMode !== 'normal') return null;
      const layer = channels(cs.backgroundColor);
      if (layer) back = composite(back, layer);
      if (back[3] === 1) break;
    }
    if (back[3] !== 1) return null;
    const rgb = channels(style(el).color);
    if (!rgb?.[3]) return null;
    const fg = luminance(composite(rgb,back)), bg = luminance(back);
    return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05);
  }
  function textScale(el, slide, style, zoom) {
    if (el.namespaceURI === SVG && typeof el.getScreenCTM === 'function') {
      const matrix = el.getScreenCTM();
      if (matrix) return Math.hypot(matrix.c, matrix.d) / zoom;
    }
    let matrix = new DOMMatrix();
    for (let node = el; node && node !== slide; node = node.parentElement) {
      const css = style(node), z = parseFloat(css.zoom) || 1;
      const [sx,sy = sx,sz = 1] = cssParts(css.scale,['1']).map(Number);
      const r = cssParts(css.rotate, ['0deg']);
      const angle = parseFloat(r.at(-1)) || 0;
      const parent = new DOMMatrix();
      if (r.length === 4) parent.rotateAxisAngleSelf(Number(r[0]),Number(r[1]),Number(r[2]),angle);
      else if (r[0] === 'x' || r[0] === 'y') parent.rotateAxisAngleSelf(Number(r[0] === 'x'),Number(r[0] === 'y'),0,angle);
      else parent.rotateSelf(angle);
      parent.scaleSelf(sx*z,sy*z,sz*z);
      if (css.transform !== 'none') parent.multiplySelf(new DOMMatrix(css.transform));
      matrix = parent.multiply(matrix);
    }
    return Math.hypot(matrix.m21, matrix.m22);
  }
  const remote = /(?:https?:)?\/\//i;
  function externalReference(el) {
    if (el.matches('style')) return /(?:url\(\s*['"]?|@import\s+['"])(?:https?:)?\/\//i.test(el.textContent);
    return [...el.attributes].some(({ localName, value }) => {
      if (localName === 'style') return /url\(\s*['"]?(?:https?:)?\/\//i.test(value);
      if (localName === 'srcset') return /(?:^|\s|,)\s*(?:https?:)?\/\//i.test(value);
      return ['src','poster','href','data'].includes(localName) && remote.test(value) && /^(?:https?:)?\/\//i.test(value.trim());
    });
  }
  function settle(root) {
    for (const animation of root.getAnimations({ subtree: true })) {
      try { animation.finish(); } catch { /* 끝이 없는 효과는 그대로 둔다 */ }
    }
  }
  function extensionIssue(slide, index, issue, fallback) {
    const el = issue.target instanceof Element ? issue.target : slide;
    return {
      slide: issue.slide ?? index + 1, level: issue.level ?? 'error', code: issue.code ?? fallback,
      message: issue.message ?? '위젯 오류', target: typeof issue.target === 'string' ? issue.target : cssPath(el),
      rect: issue.rect ?? normalizedRect(el.getBoundingClientRect(), slide.getBoundingClientRect(), scale())
    };
  }
  function audit(index) {
    const slides = getSlides(), results = [];
    for (const [i,slide] of slides.entries()) {
      if (index != null && Number(index) !== i) continue;
      const wasHidden = slide.getAttribute('hidden');
      const concealed = [...waiting].filter(([el]) => slide.contains(el));
      const erased = [...gone].filter(el => slide.contains(el));
      slide.removeAttribute('hidden');
      concealed.forEach(([el, original]) => writeAttrs(el, ['data-waiting'], [original]));
      erased.forEach(el => el.removeAttribute('data-gone'));
      // 방금 펼친 장과 단계는 등장 효과를 새로 시작한다. 끝난 자리에서 재야 하므로 효과를 끝까지 보낸다.
      settle(slide);
      try {
        runHooks('prepareAudit', slide);
        const checkTextOnImage = measureSlide(slide, i, results);
        for (const [name, { hooks }] of extensions) for (const problem of hooks.validate?.(slide) || []) {
          if (name === 'lint' && problem.code === 'text-on-image' &&
              !checkTextOnImage(problem.target, () => hooks.validate(slide))) continue;
          results.push(extensionIssue(slide, i, problem, 'widget-invalid'));
        }
      } finally {
        writeAttrs(slide, ['hidden'], [wasHidden]);
        concealed.forEach(([el]) => el.setAttribute('data-waiting', ''));
        erased.forEach(el => el.setAttribute('data-gone', ''));
      }
    }
    return results;
  }
  function measureSlide(slide, i, results) {
    const origin = slide.getBoundingClientRect(), zoom = origin.width / 1920 || 1;
    const cache = new Map(), visibility = new Map();
    const style = el => { if (!cache.has(el)) cache.set(el,getComputedStyle(el)); return cache.get(el); };
    const box = el => normalizedRect(el.getBoundingClientRect(), origin, zoom);
    const seen = new Set(), emitIssue = (el, level, code, message, rect = box(el), other = '') => {
      const target = cssPath(el), key = `${code}:${target}:${other}`;
      if (!seen.has(key)) results.push({ slide:i+1, level, code, message, target, rect });
      seen.add(key);
    };
    function rendered(el) {
      if (!el) return true;
      if (visibility.has(el)) return visibility.get(el);
      const cs = style(el);
      const yes = !el.matches(nonContent) && cs.display !== 'none' && Number(cs.opacity) > 0 && rendered(el.parentElement);
      visibility.set(el,yes); return yes;
    }
    const visible = el => el && style(el).visibility === 'visible' && rendered(el);
    const elements = all(slide,'*').filter(el => !el.closest('[data-deck-rt]')), blocks = new Map(), textElements = new Set(), clips = new Map();
    function boundary(el) {
      if (clips.has(el)) return clips.get(el);
      const cs = style(el), x = /^(hidden|clip)$/.test(cs.overflowX), y = /^(hidden|clip)$/.test(cs.overflowY);
      let edge = null;
      if ((x || y) && cs.display !== 'inline' && cs.display !== 'contents') {
        // Overflow clips at the padding box, not at the outside of the border.
        const {width, height, x:left, y:top} = box(el);
        const sx = width / (el.offsetWidth || width || 1), sy = height / (el.offsetHeight || height || 1);
        const l = left + (el.clientLeft || 0) * sx, t = top + (el.clientTop || 0) * sy;
        edge = { x, y, l, t, r:l+el.clientWidth*sx, b:t+el.clientHeight*sy };
      }
      clips.set(el,edge); return edge;
    }
    let content = false;
    const walker = doc.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode, el = node.parentElement;
      if (!node.data.trim() || el.closest('[data-deck-rt]') || !visible(el)) continue;
      const range = doc.createRange(); range.selectNodeContents(node);
      const lines = Array.from(range.getClientRects(), rect => normalizedRect(rect,origin,zoom)).filter(area);
      if (!lines.length) continue;
      const r = normalizedRect(range.getBoundingClientRect(),origin,zoom);
      content ||= lines.some(line => overlap(line,{ x:0,y:0,width:1920,height:1080 }) > 0);
      for (let node = el; node && node !== slide; node = node.parentElement) {
        const edge = boundary(node);
        if (edge && lines.some(line =>
          (edge.x && (line.x < edge.l-2 || line.x+line.width > edge.r+2)) ||
          (edge.y && (line.y < edge.t-2 || line.y+line.height > edge.b+2)))) {
          emitIssue(node,'error','text-clipped','상자의 경계에서 글자가 잘립니다.');
        }
      }
      textElements.add(el);
      if (!el.closest('math,svg') && /\^|\bsqrt\b|<=|>=|!=/.test(node.data)) emitIssue(el,'warn','plain-math','날것 수식 표기가 있습니다.',r);
      if (!el.closest('[data-bleed]') && (r.x < -4 || r.y < -4 || r.x+r.width > 1924 || r.y+r.height > 1084)) emitIssue(el,'error','overflow-canvas','글자가 캔버스를 벗어났습니다.',r);
      const fs = parseFloat(style(el).fontSize) * textScale(el,slide,style,zoom);
      if (fs < 26) emitIssue(el,fs<20?'error':'warn','small-text',`글자가 작습니다 (${Math.round(fs*10)/10}px).`,r);
      const ratio = contrast(el,style);
      if (ratio != null && ratio < 3) emitIssue(el,'warn','low-contrast',`글자와 배경의 대비가 낮습니다 (${ratio.toFixed(1)}:1).`,r);
      let block = el;
      if (el.namespaceURI === SVG) block = el.closest('text') || el;
      else if (el.namespaceURI === MATH) block = el.closest('math') || el;
      else while (block !== slide && inlineDisplay(style(block).display)) block = block.parentElement;
      if (block !== slide) {
        let fragments = blocks.get(block);
        if (!fragments) blocks.set(block, fragments = []);
        for (const rect of lines) fragments.push({ el, rect });
      }
    }
    for (const el of elements) {
      if (externalReference(el)) emitIssue(el,'warn','external-resource','외부 자원을 참조합니다.');
      if (!visible(el)) continue;
      const r = box(el);
      const source = imageSource(el);
      if (el.matches('img') && el.complete && !el.naturalWidth || el.matches('image') && (!source || svgImages.get(source)?.loaded === false)) {
        emitIssue(el,'error','image-broken','그림을 읽을 수 없습니다.',r);
      }
      if (el.matches(drawable) && overlap(r,{ x:0,y:0,width:1920,height:1080 }) > 0) content = true;
      if (!area(r) || el.closest('[data-bleed]')) continue;
      if (r.x < -4 || r.y < -4 || r.x+r.width > 1924 || r.y+r.height > 1084) {
        const text = blocks.has(el) || textElements.has(el);
        emitIssue(el,text?'error':'warn','overflow-canvas',text?'글자가 캔버스를 벗어났습니다.':'요소가 캔버스를 벗어났습니다.',r);
      }
    }
    // 수식은 꺾이지 않는다. 담긴 칸보다 넓으면 옆으로 삐져나와 다른 내용과 겹친다.
    for (const math of all(slide,'math')) {
      if (!visible(math) || math.closest('[data-bleed]')) continue;
      let holder = math.parentElement;
      while (holder && holder !== slide && /^(inline|contents)/.test(style(holder).display)) holder = holder.parentElement;
      if (!holder) continue;
      const outer = box(holder), cs = style(holder), px = name => parseFloat(cs[name]) || 0;
      const left = outer.x + px('borderLeftWidth') + px('paddingLeft'), right = outer.x + outer.width - px('borderRightWidth') - px('paddingRight');
      // 블록 수식의 상자는 칸 폭과 같으므로 실제 내용(자식)의 범위를 잰다.
      const parts = [...math.children].map(box).filter(area);
      if (!parts.length) parts.push(box(math));
      const from = Math.min(...parts.map(r => r.x)), to = Math.max(...parts.map(r => r.x+r.width));
      if (from < left-4 || to > right+4) {
        emitIssue(math,'error','math-overflow','수식이 담긴 칸보다 넓습니다. 줄을 나누거나 칸을 넓히세요.');
      }
    }
    // 칸이 좁아진 표 모양 수식(mtable)은 줄끼리 겹친다.
    for (const cell of all(slide,'mtd')) {
      if (!cell.children.length || !visible(cell)) continue;
      const outer = box(cell);
      let left = Infinity, right = -Infinity;
      for (const child of cell.children) {
        const inner = box(child);
        if (area(inner)) { left = Math.min(left,inner.x); right = Math.max(right,inner.x+inner.width); }
      }
      if (left < outer.x-4 || right > outer.x+outer.width+4) {
        emitIssue(cell.closest('math') || cell,'error','math-overflow','수식이 담긴 칸보다 넓습니다. 줄을 나누거나 칸을 넓히세요.');
      }
    }
    // 줄을 감싼 인라인 요소에도 단계나 마스코트 반응 조건이 붙을 수 있다.
    const presence = new Map([[slide, { from:0, until:Infinity, buddy:slide.closest('[data-buddy]'), moods:null }]]);
    function textPresence(el) {
      if (presence.has(el)) return presence.get(el);
      const parent = textPresence(el.parentElement);
      const buddy = el.hasAttribute('data-buddy') ? el : parent.buddy;
      const inherited = buddy === parent.buddy ? parent.moods : null;
      let moods = inherited;
      if (buddy && buddy !== el && el.hasAttribute('data-when')) {
        const words = el.dataset.when.trim().split(/\s+/).filter(Boolean);
        moods = inherited ? words.filter(word => inherited.includes(word)) : words;
      }
      const conditions = {
        from: Math.max(parent.from, stepLabel(el)),
        until: Math.min(parent.until, positiveLabel(el.dataset.until) || Infinity),
        buddy, moods
      };
      presence.set(el, conditions);
      return conditions;
    }
    function canAppearTogether(first, second) {
      const a = textPresence(first), b = textPresence(second);
      // 나타나는 번호는 포함하고 사라지는 번호는 포함하지 않는다.
      if (Math.max(a.from, b.from) >= Math.min(a.until, b.until)) return false;
      return !a.buddy || a.buddy !== b.buddy || !a.moods || !b.moods || a.moods.some(mood => b.moods.includes(mood));
    }
    const boxes = [...blocks].map(([el, fragments]) => {
      const rect = fragments.map(fragment => fragment.rect).reduce((a,b) => {
        const x = Math.min(a.x,b.x), y = Math.min(a.y,b.y);
        return {x,y,width:Math.max(a.x+a.width,b.x+b.width)-x,height:Math.max(a.y+a.height,b.y+b.height)-y};
      });
      return {el, fragments, rect, area:fragments.reduce((sum, fragment) => sum+area(fragment.rect),0)};
    });
    for (let a = 0; a < boxes.length; a++) for (let b = a+1; b < boxes.length; b++) {
      const first = boxes[a], second = boxes[b];
      const threshold = .2 * Math.min(first.area,second.area);
      if (first.el.contains(second.el) || second.el.contains(first.el) || overlap(first.rect,second.rect) <= threshold) continue;
      let shared = 0;
      for (const fa of first.fragments) for (const fb of second.fragments) {
        const common = overlap(fa.rect, fb.rect);
        if (common && canAppearTogether(fa.el, fb.el)) shared += common;
      }
      if (shared > threshold) emitIssue(second.el,'warn','text-overlap','글 덩어리가 다른 글과 겹칩니다.',second.rect,cssPath(first.el));
    }
    // lint owns the opacity-mask test. Recheck affected warnings with only the
    // text fragments and pictures that can coexist, without moving their boxes.
    function checkTextOnImage(target, recheck) {
      const fragments = blocks.get(target)?.filter(({ el }) => !el.closest('[data-over]'));
      if (!fragments?.length) return true;
      const images = all(slide, 'img').filter(img => visible(img) && !img.contains(target) &&
        fragments.some(fragment => overlap(fragment.rect, box(img))));
      if (!images.length) return true;
      const texts = [...new Set(fragments.map(fragment => fragment.el))];
      const groups = new Map();
      let filtered = false;
      for (const img of images) {
        const mask = texts.map(el => Number(canAppearTogether(el, img))).join('');
        filtered ||= mask.includes('0');
        if (!mask.includes('1')) continue;
        if (!groups.has(mask)) groups.set(mask, new Set());
        groups.get(mask).add(img);
      }
      if (!filtered) return true;
      for (const [mask, shown] of groups) {
        const hide = [...images.filter(img => !shown.has(img)), ...texts.filter((_, i) => mask[i] === '0')];
        const styles = hide.map(el => el.getAttribute('style'));
        try {
          for (const el of hide) el.style.setProperty('visibility', 'hidden', 'important');
          if (recheck().some(issue => issue.code === 'text-on-image' && issue.target === target)) return true;
        } finally {
          hide.forEach((el, i) => writeAttrs(el, ['style'], [styles[i]]));
        }
      }
      return false;
    }
    if (!content) emitIssue(slide,'error','empty-slide','보이는 내용이 없습니다.');
    if (!slide.dataset.title?.trim() && !slide.querySelector('h1,h2')) emitIssue(slide,'warn','no-title','슬라이드 제목이 없습니다.');
    if (externalReference(slide)) emitIssue(slide,'warn','external-resource','외부 자원을 참조합니다.');
    for (const el of all(doc.head,'style,link,script')) if (externalReference(el)) emitIssue(el,'warn','external-resource','덱이 외부 자원을 참조합니다.',{x:0,y:0,width:0,height:0});
    return checkTextOnImage;
  }
  function mountOverlay() {
    const css = `
      #selection,#hover-outline{position:fixed;pointer-events:none;border:1px solid var(--blue);z-index:2}
      #hover-outline{border-style:dashed;opacity:.8}
      #selection.typing{border-width:2px}
      #selection .handle{position:absolute;width:12px;height:12px;padding:0;min-height:0;border:2px solid var(--blue);border-radius:3px;background:white;translate:-50% -50%;pointer-events:auto;touch-action:none}
      #selection .handle:hover{background:#dfe8ff}
      #selection .handle[data-x="-1"]{left:0}
      #selection .handle[data-x="1"]{left:100%}
      #selection .handle[data-y="-1"]{top:0}
      #selection .handle[data-y="0"]{top:50%;width:8px;height:18px;cursor:ew-resize}
      #selection .handle[data-y="1"]{top:100%}
      #selection .handle[data-handle="nw"],#selection .handle[data-handle="se"]{cursor:nwse-resize}
      #selection .handle[data-handle="ne"],#selection .handle[data-handle="sw"]{cursor:nesw-resize}
      #floating-text{position:fixed;z-index:4;width:164px;height:32px;border:1px solid var(--blue);border-radius:6px;background:white;color:#202b43;padding:4px 8px;pointer-events:auto}
    `;
    outline = make('div'); outline.id = 'selection'; outline.hidden = true;
    hoverOutline = make('div'); hoverOutline.id = 'hover-outline'; hoverOutline.hidden = true;
    floating = make('div');
    for (const [name, x, y] of handles) {
      const handle = make('div', null, 'handle');
      Object.assign(handle.dataset, { handle: name, x, y });
      outline.append(handle);
    }
    ui.append(make('style', css), hoverOutline, outline, floating);
  }
  function mountEditor() {
    mountOverlay();
    const css = `
      #edit-panel{position:absolute;inset:0 0 0 auto;width:340px;background:#fff;pointer-events:auto;border-left:1px solid #dce1e9;display:flex;flex-direction:column;box-shadow:-5px 0 28px #0002;z-index:5}
      #panel-actions{flex:none;padding:14px 16px 12px;border-bottom:1px solid var(--border);display:grid;gap:6px}
      #panel-scroll{overflow:auto;overscroll-behavior:contain;padding:0 16px;scrollbar-width:thin;scrollbar-color:#bcc6d6 transparent}
      #panel-scroll section{display:grid;gap:7px;padding:13px 0;border-bottom:1px solid var(--border)}
      #panel-scroll section:last-child{border:0;padding-bottom:20px}
      #panel-scroll h2{margin:0 0 1px;color:#5b6475;font:750 12px/1.25 system-ui,"Malgun Gothic",sans-serif}
      .row,.stack{display:grid;gap:6px;min-width:0}
      .row{grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);align-items:center}
      #slide-nav{display:grid;grid-template-columns:36px minmax(50px,1fr) 36px;gap:6px;align-items:center}
      .slide-controls{display:grid;grid-template-columns:1fr minmax(86px,.8fr);gap:6px}
      .tight button{padding-inline:3px;font-size:12px}
      .range-label{display:grid;grid-template-columns:1fr auto;gap:1px 8px;align-items:center;font-weight:650;min-width:0}
      .range-label output{color:#2549bd;font-weight:750;font-size:12px}
      input[type=range]{grid-column:1/-1;margin:0;width:100%;height:18px;accent-color:var(--blue)}
      input[type=checkbox]{margin:0;accent-color:var(--blue);width:15px;height:15px}
      input[type=color]{width:38px;height:36px;padding:4px;border:1px solid var(--border);border-radius:8px;background:white;cursor:pointer}
      select{display:block;width:100%;min-width:0;height:36px;padding:0 5px;border:1px solid #d9dfe8;border-radius:8px;background:#fff}
      .field{display:grid;gap:4px;min-width:0;font-weight:650;font-size:13px}
      .swatch{display:flex;align-items:center;justify-content:space-between;gap:8px;font-weight:650;font-size:13px}
      .check{display:flex;align-items:center;gap:7px;min-height:27px}
      .notice{margin:0;color:#596276;font-size:12px;line-height:1.45}
      .font-row{display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:center;gap:8px;padding:0 0 3px;font-size:12px}
      .font-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .font-row output{color:#5b6475}
      #selection-path{display:flex;align-items:center;gap:4px;flex-wrap:wrap;color:#354878}
      #selection-path button{max-width:180px;overflow:hidden;text-overflow:ellipsis;background:#edf2ff;font-size:12px}
      #selection-path button:last-child{background:#e0e9ff}
      #selection-fields,.text-controls{display:grid;gap:7px}
      #reset-element{justify-self:start}
      #deck-status{position:absolute;z-index:8;bottom:54px;left:16px;max-width:min(460px,calc(100vw - 32px));padding:9px 13px;background:#182642;color:#fff;border-radius:8px;box-shadow:0 4px 16px #0003}
      @media(max-width:760px){#edit-panel{inset:auto 0 0;width:auto;height:var(--panel-height);border-left:0;border-top:1px solid var(--border)}#panel-actions{grid-template-columns:1fr 1fr;gap:8px;padding:9px 12px}#panel-scroll{padding-inline:12px}#deck-status{bottom:calc(var(--panel-height) + 8px)}}
      @media(max-width:365px){#panel-actions{grid-template-columns:1fr}.tight button{font-size:11px}}
    `;
    panel = make('aside'); panel.id = 'edit-panel'; panel.hidden = true;
    panel.setAttribute('aria-label','편집');
    const range = (id, label, min, max) => `<label class="range-label">${label}<output id="${id}-value">100%</output><input id="${id}" aria-label="${label}" type="range" min="${min}" max="${max}" value="100"></label>`;
    panel.innerHTML = `
      <div id="panel-actions">
        <div class="row"><button class="primary" id="edit-present" type="button">▶ 발표</button><button id="save" type="button">저장</button></div>
        <div class="row"><button id="undo" type="button">되돌리기</button><button id="redo" type="button">다시 하기</button></div>
      </div>
      <div id="panel-scroll">
        <section aria-labelledby="slide-heading">
          <h2 id="slide-heading">슬라이드</h2>
          <div class="slide-controls" id="slide-controls"><button id="edit-overview" type="button">모아보기</button></div>
          <div class="row tight">
            <button id="slide-new" type="button">새 장</button><button id="slide-copy" type="button" aria-label="슬라이드 복제">복제</button><button id="slide-delete" type="button" aria-label="슬라이드 삭제">삭제</button><button id="slide-earlier" type="button">앞으로</button><button id="slide-later" type="button">뒤로</button>
          </div>
          <label class="check"><input type="checkbox" id="skip-slide">발표에서 건너뛰기</label>
        </section>
        <section aria-labelledby="all-heading">
          <h2 id="all-heading">전체</h2>
          <div class="row"><button id="theme-light" type="button">밝게</button><button id="theme-dark" type="button">어둡게</button></div>
          ${range('title-scale','제목 크기',50,200)}
          ${range('body-scale','본문 크기',50,200)}
          <div class="row"><label class="field">제목 글꼴<select id="title-font"></select></label><label class="field">본문 글꼴<select id="body-font"></select></label></div>
          <p class="notice" id="font-fallback" hidden>이 글꼴이 없는 컴퓨터에서는 기본 글꼴로 보입니다.</p>
          <button id="add-font" type="button">글꼴 파일 추가</button>
          <button id="local-fonts" type="button" hidden>설치된 글꼴 더 보기</button>
          <div id="embedded-fonts" class="stack" aria-label="파일에 포함" hidden></div>
          <label class="swatch">강조색 <input id="accent" type="color"></label>
        </section>
        <section aria-labelledby="selection-heading">
          <h2 id="selection-heading">선택한 요소</h2>
          <p class="notice" id="selection-empty">슬라이드에서 고칠 곳을 누르세요. 두 번 누르면 글자를 고칩니다.</p>
          <div id="selection-fields" hidden>
            <div id="selection-path" aria-label="선택 경로"></div>
            ${range('size','크기',10,400)}
            <div id="text-controls" class="text-controls">
              ${range('font-size','글자 크기',20,400)}
              <div class="row"><label class="field">글꼴<select id="element-font"></select></label><button id="bold" type="button">굵게</button></div>
              <p class="notice" id="element-font-fallback" hidden>이 글꼴이 없는 컴퓨터에서는 기본 글꼴로 보입니다.</p>
              <div class="row tight" role="group" aria-label="정렬"><button id="align-left" type="button">왼쪽</button><button id="align-center" type="button">가운데</button><button id="align-right" type="button">오른쪽</button></div>
              <div class="field">글자색<div class="row tight" role="group" aria-label="글자색">
                <button id="color-default" type="button">기본</button><button id="color-accent" type="button">강조</button><button id="color-muted" type="button">흐리게</button><input id="color-custom" type="color" aria-label="색">
              </div></div>
            </div>
            <label class="field">나타나는 순서<select id="element-step"></select></label>
            <button id="picture-replace" type="button" hidden>그림 바꾸기</button>
            <div class="row tight"><button id="copy-element" type="button" aria-label="요소 복제">복제</button><button id="delete-element" type="button" aria-label="요소 삭제">삭제</button><button id="front" type="button">맨 앞으로</button><button id="back" type="button">맨 뒤로</button></div>
            <button id="reset-element" type="button">원래대로</button>
          </div>
        </section>
        <section aria-labelledby="add-heading"><h2 id="add-heading">넣기</h2><div class="row"><button id="add-text" type="button">글상자</button><button id="add-picture" type="button">그림</button></div></section>
        <section aria-labelledby="file-heading"><h2 id="file-heading">파일</h2><button id="print" type="button">PDF로 인쇄</button><button id="save-student" type="button">학생용으로 저장</button></section>
      </div>
      <input id="font-files" type="file" accept=".woff2,.woff,.ttf,.otf" multiple hidden>
      <input id="picture-file" type="file" accept="image/*" hidden>`;
    panel.querySelector('#slide-controls').prepend(addNavigation('slide-nav', false));
    if (credit()) panel.querySelector('#panel-scroll').append(make('p', credit(), 'notice deck-credit'));
    toast = make('div'); toast.id = 'deck-status'; toast.setAttribute('role','status'); toast.hidden = true;
    ui.append(make('style', css), panel, toast);
    fontsStyle = doc.querySelector('style[data-deck="fonts"]');
    if (!fontsStyle) { fontsStyle = make('style'); fontsStyle.dataset.deck = 'fonts'; doc.head.append(fontsStyle); }
    fontObserver = new MutationObserver(() => { fontInventory = null; });
    fontObserver.observe(fontsStyle, { childList: true, characterData: true, subtree: true });
    installed = detectFonts();
    control('local-fonts').hidden = typeof window.queryLocalFonts !== 'function';
    const pickPicture = target => {
      pictureTarget = target; pictureSlide = activeSlide(); control('picture-file').click();
    };
    const actions = {
      'edit-present': () => setMode('present'), save, 'save-student': saveStudent, undo: api.undo, redo: api.redo,
      'edit-overview': () => overview(true), 'theme-light': () => theme('light'), 'theme-dark': () => theme('dark'),
      'reset-element': restoreElement, 'copy-element': duplicateElement, 'delete-element': deleteElement,
      front: () => stackElement(true), back: () => stackElement(false),
      'add-text': addText, 'add-picture': () => pickPicture(null), 'picture-replace': () => pickPicture(selected),
      'add-font': () => control('font-files').click(), 'local-fonts': localFonts,
      print: () => { finishEdit(); window.print(); },
      bold: () => styleSelection(el => el.style.setProperty('font-weight', getComputedStyle(el).fontWeight >= 600 ? '400' : '700', 'important'))
    };
    for (const action of ['new','copy','delete','earlier','later']) actions[`slide-${action}`] = () => slideAction(action);
    for (const align of ['left','center','right']) actions[`align-${align}`] = () => styleSelection(el => {
      if (svgPart(el) && el.matches(svgText)) el.style.setProperty('text-anchor', { left:'start',center:'middle',right:'end' }[align], 'important');
      else el.style.setProperty('text-align', align, 'important');
    });
    function setColor(el, value) {
      el.style.setProperty('color', value, 'important');
      if (svgPart(el) && el.matches(svgText)) el.style.setProperty('fill', value ? 'currentColor' : '', 'important');
    }
    for (const [name,value] of [['default',''],['accent','var(--accent-ink,var(--accent))'],['muted','var(--ink-2,var(--muted,#667085))']]) {
      actions[`color-${name}`] = () => styleSelection(el => setColor(el, value));
    }
    for (const [id, action] of Object.entries(actions)) control(id).onclick = action;
    const change = (id, action) => control(id).addEventListener('change', event => action(event.target));
    change('skip-slide', input => { const slide = activeSlide(); if (slide) changeAttrs(slide, ['data-skip'], () => slide.toggleAttribute('data-skip', input.checked)); });
    change('element-step', input => {
      if (!selected) return;
      changeAttrs(selected, ['data-step'], () => writeAttrs(selected, ['data-step'], [Number(input.value) ? input.value : null]));
    });
    change('accent', input => changeAttrs(html, ['style'], () => html.style.setProperty('--accent', input.value)));
    change('color-custom', input => styleSelection(el => setColor(el, input.value)));
    for (const [id, prop] of [['title-font','--font-title'],['body-font','--font-body']]) {
      change(id, input => changeAttrs(html, ['style'], () => html.style.setProperty(prop, input.value ? cssText(input.value) : '')));
    }
    change('element-font', input => styleSelection(el => el.style.setProperty('font-family', input.value ? cssText(input.value) : '', 'important')));
    change('font-files', input => { const files = Array.from(input.files); input.value = ''; if (files.length) addFonts(files); });
    change('picture-file', input => { const file = input.files[0]; input.value = ''; if (file) usePicture(file, pictureTarget, pictureSlide); });
    boundSlider('title-scale', () => html, ['style'], (el,value) => el.style.setProperty('--title-scale', value));
    boundSlider('body-scale', () => html, ['style'], (el,value) => el.style.setProperty('--body-scale', value));
    boundSlider('font-size', () => selected, geometryAttrs, fontSize);
    boundSlider('size', () => selected, geometryAttrs, (el, value, { data, basis, rect }) => scaleAt(el, data, basis, value, rect, 0, 0), el => {
      const data = geometry(el); return { data, basis: translationBasis(el, data), rect: el.getBoundingClientRect() };
    });
  }

  // The native hidden attribute can still hide slides in print. Lift it only
  // during printing, without overriding the author's flex/grid display.
  let printVisibility;
  function beforePrint() {
    if (printVisibility) return;
    printVisibility = getSlides().map(slide => [slide, slide.getAttribute('hidden')]);
    for (const [slide] of printVisibility) slide.removeAttribute('hidden');
  }
  function afterPrint() {
    if (!printVisibility) return;
    for (const [slide, hidden] of printVisibility) writeAttrs(slide, ['hidden'], [hidden]);
    printVisibility = null;
  }

  async function init() {
    stage = doc.querySelector('.viewer > #deck');
    if (!stage) { resolveReady(api); return; }
    narrow = matchMedia('(max-width:760px)');
    narrow.addEventListener('change', fitMode);
    mountUI(); bindEditor();
    html.classList.add('deck-on');
    html.dataset.deckMode = mode;
    fitMode();
    for (const { hooks, api } of extensions.values()) hooks.init?.(api);
    extensionsReady = true;
    if (new URLSearchParams(location.search).has('edit')) setMode('edit');
    fromHash();
    window.addEventListener('hashchange', fromHash);
    window.addEventListener('pointermove', wakeCursor);
    window.addEventListener('beforeprint', beforePrint);
    window.addEventListener('afterprint', afterPrint);
    doc.addEventListener('fullscreenchange', () => {
      if (!doc.fullscreenElement && mode === 'present') setMode(priorMode);
    });
    stage.addEventListener('touchstart', touchStart, { passive: true });
    stage.addEventListener('touchend', touchEnd, { passive: false });
    stage.addEventListener('touchcancel', () => { touch = null; });
    const images = all(stage, 'img,image').map(async img => {
      if (img.localName === 'image') return preloadSVGImage(img);
      const loading = img.getAttribute('loading');
      if (img.loading === 'lazy') img.loading = 'eager';
      try { await img.decode(); } catch { /* audit reports broken images */ }
      finally { if (loading?.toLowerCase() === 'lazy') img.setAttribute('loading', loading); }
    });
    await Promise.all([...images, ...Array.from(doc.fonts, face => face.load().catch(() => {}))]);
    if (doc.readyState !== 'complete') await new Promise(resolve => window.addEventListener('load', resolve, { once: true }));
    await doc.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    resolveReady(api);
  }
  window.addEventListener('click', stageClick, true);
  window.addEventListener('keydown', keydown);
  const start = () => { init().catch(rejectReady); };
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
