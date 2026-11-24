;(() => {
  'use strict';
  const deck = window.deck;
  if (!deck?.ext) return;

  const ROOTS = '[data-quiz],[data-flip],[data-peek],[data-toggle],[data-tabs],[data-sort],[data-inputs],[data-answer],[data-note],[data-timer],[data-pick],[data-count]';
  const ACTIONS = '[data-opt],[data-check],[data-flip],[data-peek],[data-toggle],[data-tab],[data-chip],[data-bin],[data-pool],[data-lane],[data-timer],[data-pick],[data-count]';
  const ZONES = '[data-bin],[data-pool],[data-lane]';
  const SLIDE = 'section.slide[data-slide]';
  let api, stage, drag, suppressedClick, editField;
  const states = new WeakMap();
  const all = (root, selector) => [...root.querySelectorAll(selector)];
  const owned = (root, selector, boundary) => all(root, selector).filter(el => el.closest(boundary) === root);
  const currentSlide = () => deck.slides[deck.page];
  const state = root => {
    let s = states.get(root);
    if (!s) { s = { marks: new Map() }; states.set(root, s); }
    return s;
  };
  function mark(root, el, name, on = true) {
    const s = state(root);
    let names = s.marks.get(el);
    if (!names) { names = new Set(); s.marks.set(el, names); }
    if (on) names.add(name); else names.delete(name);
    el.classList.toggle(name, on);
    if (!el.getAttribute('class')) el.removeAttribute('class');
  }
  function clearRoot(root) {
    const s = states.get(root);
    if (s) {
      clearInterval(s.timer);
      clearInterval(s.rolling);
      if (s.timerLeft) {
        const [value, priority] = s.timerLeft;
        if (value) root.style.setProperty('--timer-left', value, priority);
        else root.style.removeProperty('--timer-left');
        if (!s.hadStyle && !root.getAttribute('style')) root.removeAttribute('style');
      }
      // Restore from the last recorded location to the first. Original siblings
      // that were moved later have already been put back when we need them.
      for (const [chip, { parent, next }] of [...(s.origins || [])].reverse()) {
        if (root.contains(chip) && root.contains(parent)) parent.insertBefore(chip, next?.parentNode === parent ? next : null);
      }
      for (const [el, names] of s.marks) for (const name of names) {
        el.classList.remove(name);
        if (!el.getAttribute('class')) el.removeAttribute('class');
      }
      if (s.output) api.restore(s.output);
      if (s.timer != null) api.restore(root);
      states.delete(root);
    }
    if (root.matches('input[data-answer]')) root.value = root.defaultValue;
    if (root.matches('textarea[data-note]')) root.value = '';
    if (root.hasAttribute('data-tabs')) initTabs(root);
  }
  function resetWidgets(slide, leaving = false) {
    if (drag?.chip.closest(SLIDE) === slide) finishDrag();
    if (editField?.closest(SLIDE) === slide) { editField.blur(); editField = null; }
    const kept = el => leaving && el.closest('[data-keep]') && slide.contains(el.closest('[data-keep]'));
    for (const root of all(slide, ROOTS)) if (!kept(root)) clearRoot(root);
    for (const el of all(slide, '[data-buddy][data-mood]')) if (!kept(el)) el.removeAttribute('data-mood');
  }
  function quiz(root, option) {
    if (root.classList.contains('is-solved')) return;
    const options = owned(root, '[data-opt]', '[data-quiz]');
    const isCorrect = el => el.hasAttribute('data-correct');
    const multi = root.dataset.quiz === 'multi';
    if (multi && option) {
      mark(root, option, 'is-picked', !option.classList.contains('is-picked'));
      mark(root, option, 'is-wrong', false); mark(root, option, 'is-correct', false);
      return;
    }
    if (!multi && !option) return;
    const picks = multi ? options.filter(el => el.classList.contains('is-picked')) : [option];
    const correct = multi ? picks.length > 0 && options.every(el => picks.includes(el) === isCorrect(el)) : isCorrect(option);
    const solved = correct || root.dataset.quiz === 'once';
    for (const el of picks) {
      mark(root, el, isCorrect(el) ? 'is-correct' : 'is-wrong');
      if (multi) mark(root, el, 'is-wrong', !isCorrect(el));
    }
    if (!correct && multi) {
      // A subset of correct answers is still incomplete.
      for (const el of picks.filter(isCorrect)) mark(root, el, 'is-wrong', !picks.some(el => !isCorrect(el)));
    }
    if (solved) {
      mark(root, root, 'is-solved');
      mark(root, root, 'is-missed', !correct);
      for (const el of options) {
        mark(root, el, 'is-correct', isCorrect(el));
        mark(root, el, 'is-dim', !isCorrect(el));
      }
      for (const el of owned(root, '[data-explain]', '[data-quiz]')) mark(root, el, 'is-open');
    }
    api.result(option || root, { type:'quiz', correct, solved });
  }

  function toggle(root) {
    const on = !root.classList.contains('is-on'), group = root.dataset.group;
    if (on && group) for (const el of all(root.closest(SLIDE), '[data-toggle][data-group]')) {
      if (el.dataset.group === group && el !== root) mark(el, el, 'is-on', false);
    }
    mark(root, root, 'is-on', on);
  }
  const tabsIn = (root, selector) => owned(root, selector, '[data-tabs]');
  function chooseTab(root, key) {
    const panes = tabsIn(root, '[data-pane]');
    if (!panes.some(el => el.dataset.pane === key)) return;
    for (const el of tabsIn(root, '[data-tab]')) mark(root, el, 'is-on', el.dataset.tab === key);
    for (const el of panes) mark(root, el, 'is-off', el.dataset.pane !== key);
    state(root).tab = key;
  }
  function initTabs(root) {
    if (states.get(root)?.tab != null) return;
    const buttons = tabsIn(root, '[data-tab]'), panes = tabsIn(root, '[data-pane]');
    const key = [root.dataset.start, ...buttons.map(el => el.dataset.tab)].find(k => panes.some(el => el.dataset.pane === k));
    if (key != null) chooseTab(root, key);
  }

  const sortIn = (root, selector) => owned(root, selector, '[data-sort]');
  const sortZone = (root, chip) => {
    const zone = chip.parentElement?.closest(ZONES);
    return zone?.closest('[data-sort]') === root ? zone : null;
  };
  function sortSelection(root, chip) {
    const s = state(root);
    if (s.chosen) mark(root, s.chosen, 'is-picked', false);
    s.chosen = chip === s.chosen ? null : chip;
    if (s.chosen) mark(root, s.chosen, 'is-picked');
  }
  function relocate(root, chip, parent, before = null) {
    const s = state(root);
    s.origins ||= new Map();
    if (!s.origins.has(chip)) s.origins.set(chip, { parent:chip.parentNode, next:chip.nextSibling });
    parent.insertBefore(chip, before);
    mark(root, chip, 'is-placed', !parent.hasAttribute('data-pool'));
  }
  function checkSort(root, only) {
    const chips = sortIn(root, '[data-chip]'), order = root.dataset.sort === 'order';
    const lane = sortIn(root, '[data-lane]')[0], pool = sortIn(root, '[data-pool]')[0];
    const arranged = order ? chips.filter(chip => sortZone(root, chip) === lane) : [];
    const tests = chips.map(chip => {
      const zone = sortZone(root, chip);
      const placed = !!zone && !zone.hasAttribute('data-pool');
      const right = placed && (order ? zone === lane && Number(chip.dataset.rank) === arranged.indexOf(chip) + 1 : zone.dataset.bin === chip.dataset.to);
      return { chip, placed, right };
    });
    const solved = tests.length > 0 && tests.every(test => test.right);
    let wrong = false, tested = false;
    for (const { chip, placed, right } of tests) {
      if (only && !order && chip !== only) continue;
      if (!placed) continue;
      tested = true; wrong ||= !right;
      mark(root, chip, 'is-correct', right); mark(root, chip, 'is-wrong', !right);
      if (!right && pool) relocate(root, chip, pool);
    }
    mark(root, root, 'is-solved', solved);
    if (tested) api.result(only || root, { type:'sort', correct:!wrong && (solved || !!only), solved });
  }
  function drop(root, chip, zone, event) {
    if (!zone || zone.closest('[data-sort]') !== root || !root.contains(chip)) return;
    const order = root.dataset.sort === 'order', pool = sortIn(root, '[data-pool]')[0];
    if (!zone.matches(order ? '[data-lane],[data-pool]' : '[data-bin],[data-pool]')) return;
    const peers = sortIn(root, '[data-chip]').filter(el => el !== chip && sortZone(root, el) === zone);
    const max = Number(zone.dataset.max);
    if (zone.hasAttribute('data-bin') && max > 0 && peers.length >= max) {
      if (!pool) return;
      for (const previous of peers.slice(max - 1)) {
        relocate(root, previous, pool);
        mark(root, previous, 'is-correct', false); mark(root, previous, 'is-wrong', false);
      }
    }
    let before = null;
    if (order && zone.hasAttribute('data-lane') && event) {
      const exact = event.target.closest?.('[data-chip]');
      const hit = event.type === 'click' && peers.includes(exact) ? exact : null;
      if (hit && hit.parentNode === zone) before = hit;
      else {
        const p = api.point(event);
        const vertical = getComputedStyle(zone).flexDirection.startsWith('column');
        before = peers.find(el => {
          if (el.parentNode !== zone) return false;
          const r = el.getBoundingClientRect(), center = api.point({ clientX:r.x+r.width/2, clientY:r.y+r.height/2 });
          return vertical ? p.y < center.y : p.x < center.x;
        }) || null;
      }
    }
    relocate(root, chip, zone, before);
    if (state(root).chosen) sortSelection(root, null);
    mark(root, chip, 'is-correct', false); mark(root, chip, 'is-wrong', false); mark(root, root, 'is-solved', false);
    if (!zone.hasAttribute('data-pool') && !sortIn(root, '[data-check]').length) checkSort(root, chip);
  }
  function allowed(event) {
    return !!event.target && event.target.closest?.(SLIDE) === currentSlide() && (api.active() || (deck.mode === 'edit' && event.ctrlKey && !event.target.isContentEditable));
  }
  function dragStart(event) {
    suppressedClick = null;
    if (drag || event.button !== 0 || !event.isPrimary || !allowed(event)) return;
    if (deck.mode === 'edit' && event.ctrlKey) {
      const control = event.target.closest('label')?.control || event.target;
      if (control.matches('input[data-answer],textarea[data-note]')) editField = control;
    }
    const chip = event.target.closest('[data-chip]'), root = chip?.closest('[data-sort]');
    if (!root || chip.matches(':disabled')) return;
    const r = chip.getBoundingClientRect(), scale = api.scale();
    drag = { chip, root, id:event.pointerId, x:event.clientX, y:event.clientY, width:r.width/scale, height:r.height/scale,
      offsetX:(event.clientX-r.x)/scale, offsetY:(event.clientY-r.y)/scale };
    if (event.isTrusted) chip.setPointerCapture(event.pointerId);
  }
  function hitZone(event, root) {
    // 칸 위에 얹힌 꾸밈(마스코트 등)에 가려도 그 아래의 칸을 찾는다.
    for (const hit of document.elementsFromPoint(event.clientX, event.clientY)) {
      const zone = hit.closest?.(ZONES);
      if (zone && zone.closest('[data-sort]') === root) return zone;
    }
    return null;
  }
  function dragMove(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const { root, chip } = drag;
    if (!drag.proxy) {
      if (Math.hypot(event.clientX-drag.x,event.clientY-drag.y) < 4) return;
      const proxy = chip.cloneNode(true);
      proxy.removeAttribute('id');
      for (const el of all(proxy, '[id]')) el.removeAttribute('id');
      proxy.classList.add('deck-sort-proxy','is-drag'); proxy.inert = true;
      proxy.style.width = `${drag.width}px`; proxy.style.height = `${drag.height}px`;
      proxy.style.margin = '0'; proxy.style.translate = 'none'; proxy.style.transform = 'none'; proxy.style.scale = 'none';
      api.layer(chip.closest(SLIDE)).append(proxy); drag.proxy = proxy;
      mark(root, chip, 'is-drag');
    }
    const p = api.point(event);
    drag.proxy.style.left = `${p.x-drag.offsetX}px`; drag.proxy.style.top = `${p.y-drag.offsetY}px`;
    const zone = hitZone(event, root);
    if (drag.zone && drag.zone !== zone) mark(root, drag.zone, 'is-over', false);
    if (zone) mark(root, zone, 'is-over');
    drag.zone = zone;
    if (event.cancelable) event.preventDefault();
  }
  function finishDrag(event) {
    if (!drag || (event && event.pointerId !== drag.id)) return;
    const { chip, root, proxy, zone, id } = drag;
    drag = null;
    if (chip.hasPointerCapture(id)) chip.releasePointerCapture(id);
    if (!proxy) return;
    proxy.remove(); mark(root, chip, 'is-drag', false);
    if (zone) mark(root, zone, 'is-over', false);
    if (event?.type === 'pointerup') {
      suppressedClick = chip;
      drop(root, chip, hitZone(event, root), event);
    }
  }

  const normalized = text => text.normalize('NFKC').trim().toLowerCase().replace(/\s+/g, '').replaceAll('−','-');
  const alternatives = el => el.dataset.answer.split('|').map(s => s.trim()).filter(Boolean);
  const numeric = text => /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(text) && Number.isFinite(Number(text));
  const answers = root => root.matches('input[data-answer]') ? [root] : owned(root, 'input[data-answer]', '[data-inputs]');
  function checkInputs(root) {
    const fields = answers(root);
    if (!fields.length) return;
    const solved = fields.map(el => {
      const value = normalized(el.value), tolerance = Number(el.dataset.tol);
      const correct = alternatives(el).map(normalized).some(answer => value === answer ||
        Number.isFinite(tolerance) && tolerance >= 0 && numeric(value) && numeric(answer) && Math.abs(Number(value)-Number(answer)) <= tolerance);
      mark(el, el, 'is-correct', correct); mark(el, el, 'is-wrong', !correct);
      return correct;
    }).every(Boolean);
    if (root.hasAttribute('data-inputs')) mark(root, root, 'is-solved', solved);
    api.result(root, { type:'input', correct:solved, solved });
  }
  function runTimer(root) {
    const seconds = Number(root.dataset.timer);
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    if (states.get(root)?.timer != null) clearRoot(root);
    const s = state(root), duration = seconds * 1000, stop = performance.now() + duration;
    api.keep(root);
    s.hadStyle = root.hasAttribute('style');
    s.timerLeft = [root.style.getPropertyValue('--timer-left'), root.style.getPropertyPriority('--timer-left')];
    mark(root, root, 'is-running');
    const tick = () => {
      const left = Math.max(0, stop - performance.now()), remain = Math.ceil(left / 1000);
      const text = `${Math.floor(remain / 60)}:${String(remain % 60).padStart(2,'0')}`;
      if (root.textContent !== text) root.textContent = text;
      root.style.setProperty('--timer-left', String(left / duration));
      if (!left) {
        clearInterval(s.timer); mark(root, root, 'is-running', false); mark(root, root, 'is-done');
        api.result(root, { type:'timer', solved:true });
      }
    };
    root.style.setProperty('--timer-left','1');
    s.timer = setInterval(tick, 80);
    root.textContent = `${Math.floor(Math.ceil(seconds) / 60)}:${String(Math.ceil(seconds) % 60).padStart(2,'0')}`;
  }
  function outputFor(root, out, control) {
    const inside = root.querySelector(out);
    if (inside) return inside;
    for (let el = root.nextElementSibling; el && !el.matches(control); el = el.nextElementSibling) {
      if (el.matches(out)) return el;
    }
    const siblings = [...(root.parentElement?.children || [])];
    if (siblings.filter(el => el.matches(control)).length === 1) return siblings.find(el => el.matches(out));
  }
  function pickValues(root) {
    const value = root.dataset.pick.trim(), range = /^(-?\d+)\s*-\s*(-?\d+)$/.exec(value);
    if (!range) return /^-?\d+\s*-\s*$/.test(value) ? [] : [...new Set(value.split(',').map(s => s.trim()).filter(Boolean))];
    const start = Number(range[1]), end = Number(range[2]);
    return Number.isSafeInteger(start) && Number.isSafeInteger(end) && end >= start && end - start < 10000 ? Array.from({length:end-start+1},(_, i) => String(i + start)) : [];
  }
  function rememberOutput(root, el) {
    // Result elements may be siblings, so remember their owner for a leave reset.
    state(root).output = el; api.keep(el);
  }
  function pick(root) {
    const s = state(root), values = pickValues(root);
    if (s.rolling || !values.length) return;
    const out = outputFor(root, '[data-pick-out]', '[data-pick]') || root;
    rememberOutput(root, out);
    if (!s.remaining?.length) s.remaining = values.slice();
    const randomIndex = list => Math.floor(Math.random() * list.length);
    const selected = s.remaining.splice(randomIndex(s.remaining), 1)[0];
    let ticks = 0;
    mark(root, root, 'is-rolling');
    s.rolling = setInterval(() => {
      if (++ticks < 5) out.textContent = values[randomIndex(values)];
      else {
        clearInterval(s.rolling); s.rolling = null; out.textContent = selected;
        mark(root, root, 'is-rolling', false);
        api.result(root, { type:'pick', solved:true });
      }
    }, 55);
  }
  function count(root, change) {
    const out = outputFor(root, '[data-count-out]', '[data-count]');
    if (!out) return;
    rememberOutput(root, out);
    const previous = Number(out.textContent.trim());
    out.textContent = String((Number.isSafeInteger(previous) ? previous : 0) + change);
  }
  function clicked(event) {
    const el = event.target.closest(ACTIONS);
    if (!el) return;
    if (suppressedClick && (el === suppressedClick || el === stage) && (event.detail || event.pointerType)) {
      suppressedClick = null; event.preventDefault(); event.stopPropagation(); return;
    }
    if (!allowed(event) || el.matches(':disabled')) return;
    const action = el.closest('button,a,[role="button"],input[type="button"]');
    if ((event.detail || event.pointerType) && action?.contains(document.activeElement)) document.activeElement.blur();
    event.preventDefault(); event.stopPropagation();
    if (el.hasAttribute('data-opt')) { const root = el.closest('[data-quiz]'); if (root) quiz(root, el); }
    else if (el.hasAttribute('data-flip')) mark(el, el, 'is-flipped', !el.classList.contains('is-flipped'));
    else if (el.hasAttribute('data-peek')) mark(el, el, 'is-open', !el.classList.contains('is-open'));
    else if (el.hasAttribute('data-toggle')) toggle(el);
    else if (el.hasAttribute('data-tab')) { const root = el.closest('[data-tabs]'); if (root) chooseTab(root, el.dataset.tab); }
    else if (el.matches('[data-chip],' + ZONES)) {
      const root = el.closest('[data-sort]');
      if (!root) return;
      const chosen = states.get(root)?.chosen;
      if (el.hasAttribute('data-chip')) {
        if (chosen && chosen !== el && root.dataset.sort === 'order' && el.closest('[data-lane]')) drop(root, chosen, el.closest('[data-lane]'), event);
        else sortSelection(root, el);
      } else if (chosen) drop(root, chosen, el);
    } else if (el.hasAttribute('data-check')) {
      const root = el.closest('[data-quiz],[data-sort],[data-inputs]');
      if (root?.hasAttribute('data-quiz')) quiz(root);
      else if (root?.hasAttribute('data-sort')) checkSort(root);
      else {
        const input = el.previousElementSibling;
        if (root) checkInputs(root);
        else if (input?.matches('input[data-answer]')) checkInputs(input);
      }
    } else if (el.hasAttribute('data-timer')) runTimer(el);
    else if (el.hasAttribute('data-pick')) pick(el);
    else if (el.hasAttribute('data-count')) count(el, event.shiftKey ? -1 : 1);
  }
  function key(event) {
    const el = event.target;
    if (event.key === 'Enter' && el?.matches?.('input[data-answer]') && (allowed(event) || (editField === el && document.activeElement === el && !el.isContentEditable))) {
      checkInputs(el.closest('[data-inputs]') || el);
      return true;
    }
    if (!allowed(event) || !['Enter',' '].includes(event.key) || !el.matches?.(ACTIONS) || el.matches('button,input,a[href]') || el.isContentEditable || el.tabIndex < 0) return false;
    if (!event.repeat) el.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, ctrlKey:event.ctrlKey, shiftKey:event.shiftKey }));
    return true;
  }

  function validate(slide) {
    const problems = [], bad = (target, message) => problems.push({ target, message, code:'widget-invalid', level:'error' });
    for (const root of all(slide, '[data-quiz]')) {
      const opts = owned(root, '[data-opt]', '[data-quiz]');
      if (!opts.some(el => el.hasAttribute('data-correct'))) bad(root, '고르기 정답 없음');
      if (!['','once','multi'].includes(root.dataset.quiz)) bad(root, '고르기 방식 오류');
      if (root.dataset.quiz === 'multi' && !owned(root, '[data-check]', '[data-quiz]').length) bad(root, '고르기 확인 버튼 없음');
    }
    for (const root of all(slide, '[data-flip]')) if (root.children.length !== 2) bad(root, '뒤집기 앞·뒤 면 필요');
    for (const root of all(slide, '[data-tabs]')) {
      const tabs = tabsIn(root, '[data-tab]'), panes = tabsIn(root, '[data-pane]');
      const pair = (list, prop, value) => list.filter(el => el.dataset[prop] === value).length === 1;
      if (!tabs.length || !panes.length) bad(root, '탭 단추·칸 필요');
      for (const tab of tabs) if (!tab.dataset.tab || !pair(panes,'pane',tab.dataset.tab) || !pair(tabs,'tab',tab.dataset.tab)) bad(tab, '탭과 짝인 칸 없음 또는 중복');
      for (const pane of panes) if (!pair(tabs,'tab',pane.dataset.pane)) bad(pane, '칸과 짝인 탭 없음 또는 중복');
      if (root.hasAttribute('data-start') && !pair(panes,'pane',root.dataset.start)) bad(root, '시작 탭 없음');
    }
    for (const root of all(slide, '[data-sort]')) {
      const chips = sortIn(root, '[data-chip]'), pool = sortIn(root, '[data-pool]'), bins = sortIn(root, '[data-bin]');
      if (!chips.length || pool.length !== 1) bad(root, '칩·대기 칸 필요');
      if (root.dataset.sort === 'order') {
        if (sortIn(root, '[data-lane]').length !== 1) bad(root, '순서 칸 필요');
        const ranks = chips.map(chip => Number(chip.dataset.rank)).sort((a,b) => a-b);
        if (!ranks.every((rank,i) => rank === i + 1)) bad(root, '칩 순서 번호 오류');
      } else {
        if (root.dataset.sort !== '') bad(root, '끌어 놓기 방식 오류');
        for (const chip of chips) if (!chip.dataset.to || bins.filter(bin => bin.dataset.bin === chip.dataset.to).length !== 1) bad(chip, '칩의 칸 없음 또는 중복');
        for (const bin of bins) if (bin.hasAttribute('data-max') && (!Number.isSafeInteger(Number(bin.dataset.max)) || Number(bin.dataset.max) < 1 || chips.filter(chip => chip.dataset.to === bin.dataset.bin).length > Number(bin.dataset.max))) bad(bin, '칸의 최대 칩 수 오류');
      }
    }
    for (const root of all(slide, '[data-inputs]')) if (!answers(root).length) bad(root, '답 입력칸 없음');
    for (const el of all(slide, '[data-answer]')) if (!el.matches('input') || !alternatives(el).length ||
      (el.hasAttribute('data-tol') && (!el.dataset.tol.trim() || !Number.isFinite(Number(el.dataset.tol)) || Number(el.dataset.tol) < 0))) bad(el, '입력 정답·허용 오차 오류');
    for (const el of all(slide, '[data-note]:not(textarea)')) bad(el, '메모 입력칸 필요');
    for (const el of all(slide, '[data-timer]')) if (!Number.isFinite(Number(el.dataset.timer)) || Number(el.dataset.timer) <= 0) bad(el, '타이머 시간 오류');
    for (const el of all(slide, '[data-pick]')) if (!pickValues(el).length) bad(el, '뽑기 값 없음');
    for (const el of all(slide, '[data-count]')) if (!outputFor(el, '[data-count-out]', '[data-count]')) bad(el, '세기 결과 칸 없음');
    for (const [selector, root] of [['[data-opt]','[data-quiz]'],['[data-chip]','[data-sort]'],['[data-tab],[data-pane]','[data-tabs]']]) {
      for (const el of all(slide, selector)) if (!el.closest(root)) bad(el, '위젯 뿌리 없음');
    }
    return problems;
  }

  // In-page checks send the same DOM events as the delegated controls. The
  // end-to-end suite separately supplies trusted mouse/touch/key events via Chrome.
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const center = el => { const r = el.getBoundingClientRect(); return {clientX:r.x+r.width/2,clientY:r.y+r.height/2}; };
  const has = (el, name) => !!el?.classList.contains(name);
  function press(el, options) {
    if (!el || el.matches(':disabled') || !el.checkVisibility({visibilityProperty:true})) return false;
    const p = { bubbles:true, cancelable:true, composed:true, pointerType:'mouse', pointerId:1, isPrimary:true, ...center(el), ...options };
    for (const type of ['pointerdown','pointerup','click']) el.dispatchEvent(new PointerEvent(type, p));
    return true;
  }
  function carry(chip, zone) {
    if (!chip || !zone || chip.matches(':disabled') || !chip.checkVisibility({visibilityProperty:true}) || !zone.checkVisibility({visibilityProperty:true})) return false;
    // Aim at the empty end of the zone so order mode appends in turn.
    const box = zone.getBoundingClientRect(), p = { bubbles:true, cancelable:true, pointerId:1, pointerType:'mouse', isPrimary:true, ...center(chip) };
    // 둥근 모서리의 바깥을 누르지 않게 반지름만큼 안쪽을 겨눈다.
    const radius = parseFloat(getComputedStyle(zone).borderBottomRightRadius) || 0;
    const inset = Math.min(radius * api.scale() * .3 + 4, box.width / 2, box.height / 2);
    const end = { ...p, clientX:box.right - inset, clientY:box.bottom - inset };
    chip.dispatchEvent(new PointerEvent('pointerdown', p));
    chip.dispatchEvent(new PointerEvent('pointermove', end));
    chip.dispatchEvent(new PointerEvent('pointerup', end));
    return zone.contains(chip);
  }
  async function selftest(slide) {
    const found = [], bad = (target, message) => found.push({target, message, code:'widget-broken', level:'error'});
    for (const root of all(slide, ROOTS)) {
      if (root.closest('[hidden],[data-gone],[data-waiting]')) continue;
      // An inner widget can live in an initially inactive pane or on a flip back.
      const parents = [];
      for (let el = root.parentElement; el && el !== slide; el = el.parentElement) parents.unshift(el);
      for (const pane of parents.filter(el => el.hasAttribute('data-pane') && el.classList.contains('is-off'))) {
        const tabs = pane.closest('[data-tabs]');
        press(tabs && tabsIn(tabs, '[data-tab]').find(el => el.dataset.tab === pane.dataset.pane));
      }
      const broken = label => bad(root, `${label} 동작 오류`);
      if (root.hasAttribute('data-quiz')) {
        const opts = owned(root, '[data-opt]', '[data-quiz]'), good = opts.filter(el => el.hasAttribute('data-correct')), wrong = opts.find(el => !el.hasAttribute('data-correct'));
        const multi = root.dataset.quiz === 'multi', check = owned(root, '[data-check]', '[data-quiz]')[0];
        if (wrong) {
          press(wrong); if (multi) press(check);
          if (!has(wrong,'is-wrong')) broken('고르기 오답');
          clearRoot(root);
        }
        for (const el of multi ? good : good.slice(0,1)) press(el);
        if (multi) press(check);
        if (!has(root,'is-solved')) broken('고르기 정답');
      } else if (root.matches('[data-flip],[data-peek],[data-toggle]')) {
        const klass = root.hasAttribute('data-flip') ? 'is-flipped' : root.hasAttribute('data-peek') ? 'is-open' : 'is-on';
        const before = has(root, klass);
        if (!press(root) || has(root,klass) === before) broken('전환');
      } else if (root.hasAttribute('data-tabs')) {
        for (const tab of tabsIn(root, '[data-tab]')) {
          const pane = tabsIn(root, '[data-pane]').find(el => el.dataset.pane === tab.dataset.tab);
          const usable = press(tab), box = pane?.getBoundingClientRect();
          if (!usable || !has(tab,'is-on') || has(pane,'is-off') || !box?.width || !box.height) bad(tab, '탭 전환 오류');
        }
      } else if (root.hasAttribute('data-sort')) {
        const order = root.dataset.sort === 'order', lane = sortIn(root,'[data-lane]')[0], pool = sortIn(root,'[data-pool]')[0];
        const check = sortIn(root,'[data-check]')[0], bins = sortIn(root,'[data-bin]'), chips = sortIn(root,'[data-chip]');
        const chip = order ? chips.find(el => Number(el.dataset.rank) !== 1) : chips.find(el => bins.some(bin => bin.dataset.bin !== el.dataset.to));
        if (chip) {
          carry(chip, order ? lane : bins.find(bin => bin.dataset.bin !== chip.dataset.to));
          if (check) press(check);
          if (!pool?.contains(chip) || !has(chip,'is-wrong')) broken('끌어 놓기 오답');
          clearRoot(root);
        }
        if (order) chips.sort((a,b) => Number(a.dataset.rank)-Number(b.dataset.rank));
        for (const el of chips) carry(el, order ? lane : bins.find(bin => bin.dataset.bin === el.dataset.to));
        if (check) press(check);
        if (!has(root,'is-solved')) broken('끌어 놓기 정답');
      } else if (root.matches('[data-inputs],input[data-answer]') && !(root.hasAttribute('data-answer') && root.closest('[data-inputs]'))) {
        const fields = answers(root), first = fields[0];
        if (!first || fields.some(el => el.disabled || el.readOnly || !el.checkVisibility({ visibilityProperty:true }))) { broken('답 쓰기'); continue; }
        const check = root.hasAttribute('data-inputs') ? owned(root, '[data-check]', '[data-inputs]')[0]
          : root.nextElementSibling?.matches('[data-check]') && root.nextElementSibling;
        const submit = () => check ? press(check) : first.dispatchEvent(new KeyboardEvent('keydown', { key:'Enter', bubbles:true, cancelable:true }));
        for (const el of fields) {
          el.value = alternatives(el)[0] || '';
          el.dispatchEvent(new InputEvent('input', { bubbles:true }));
        }
        submit();
        if (fields.some(el => !has(el,'is-correct'))) broken('답 쓰기');
      } else if (root.hasAttribute('data-pick')) {
        const out = outputFor(root,'[data-pick-out]','[data-pick]') || root;
        const started = press(root.querySelector('button') || root) && has(root,'is-rolling');
        if (started) await wait(330);
        if (!started || has(root,'is-rolling') || !pickValues(root).includes(out.textContent)) broken('뽑기');
      } else if (root.hasAttribute('data-timer')) {
        const began = press(root) && has(root,'is-running'), initial = root.textContent;
        for (let i = 0; began && root.textContent === initial && i < 25; i++) await wait(50);
        if (!began || root.textContent === initial) broken('타이머');
      } else if (root.hasAttribute('data-count')) {
        const out = outputFor(root,'[data-count-out]','[data-count]'), before = Number(out?.textContent) || 0;
        if (!press(root) || Number(out?.textContent) !== before + 1) broken('세기');
        if (!press(root,{shiftKey:true}) || Number(out?.textContent) !== before) broken('세기 빼기');
        press(root);
      }
    }
    return found;
  }
  deck.ext('widgets', {
    init(context) {
      api = context; stage = document.getElementById('deck');
      api.interactive(`${ROOTS},${ACTIONS}`);
      for (const el of all(stage, '[data-tabs]')) initTabs(el);
      // Only initialize newly inserted tab markup; all interaction is delegated.
      new MutationObserver(records => {
        for (const record of records) for (const el of record.addedNodes) if (el.nodeType === Node.ELEMENT_NODE && stage.contains(el)) {
          if (el.matches('[data-tabs]')) initTabs(el);
          for (const tabs of all(el, '[data-tabs]')) initTabs(tabs);
        }
      }).observe(stage, { childList:true, subtree:true });
      stage.addEventListener('click', clicked);
      stage.addEventListener('pointerdown', dragStart);
      // 포인터 캡처가 적용되지 않으면 장 바깥의 꾸밈이 이벤트를 받는다.
      // 시작은 장 안에서만 받고, 같은 포인터의 이동·종료는 장 밖에서도 받는다.
      window.addEventListener('pointermove', dragMove, true);
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
        window.addEventListener(type, finishDrag, true);
      }
      stage.addEventListener('dragstart', event => { if (event.target.closest('[data-chip]')) event.preventDefault(); });
      window.addEventListener('blur', () => finishDrag());
    },
    enter(slide) { for (const root of all(slide, '[data-tabs]')) initTabs(root); },
    leave(slide) { resetWidgets(slide, true); },
    reset(slide) { resetWidgets(slide); },
    key, validate, selftest
  });
})();
