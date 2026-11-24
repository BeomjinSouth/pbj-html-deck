(() => {
  'use strict';
  const deck = window.deck, ns = 'http://www.w3.org/2000/svg';
  const surfaces = new WeakMap();
  const tools = [['ink', '검정'], ['blue', '파랑'], ['red', '빨강'], ['mark', '형광펜'], ['erase', '획 지우개']];
  let api, toolbar, opener, current, gesture, enabled = false, tool = 'ink';
  const svg = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
    return el;
  };
  const pair = p => `${p.x} ${p.y}`;
  const mid = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const precise = ({ x, y }) => ({ x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 });

  function surface(page) {
    let entry = surfaces.get(page);
    if (entry?.node.parentNode) return entry;
    const node = svg('svg', { class: 'pen-surface', viewBox: '0 0 1920 1080', 'aria-hidden': 'true', 'data-runtime': '' });
    entry = { node, strokes: [] }; surfaces.set(page, entry);
    // Some slides use flex or grid; the core layer remains in canvas coordinates.
    api.layer(page).append(node);
    node.addEventListener('pointerdown', event => {
      if (event.button !== 0 || !event.isPrimary || gesture || !enabled || !api.active()) return;
      event.preventDefault(); event.stopPropagation();
      document.activeElement?.blur?.();
      node.setPointerCapture(event.pointerId);
      const point = precise(api.point(event));
      gesture = { entry, id: event.pointerId, last: point, tool };
      if (tool === 'erase') erase(point);
      else {
        const path = svg('path', { class: `pen-stroke pen-stroke-${tool}`, d: `M${pair(point)}h.01` });
        entry.node.append(path); entry.strokes.push(path);
        Object.assign(gesture, { path, curve: `M${pair(point)}` });
      }
    });
    node.addEventListener('pointermove', event => {
      if (gesture?.entry !== entry || event.pointerId !== gesture.id) return;
      event.stopPropagation();
      const samples = event.getCoalescedEvents?.();
      for (const sample of samples?.length ? samples : [event]) draw(precise(api.point(sample)));
    });
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) node.addEventListener(type, event => {
      if (gesture?.entry !== entry || event.pointerId !== gesture.id) return;
      if (type === 'pointerup') draw(precise(api.point(event)));
      finish();
    });
    node.addEventListener('click', event => event.stopPropagation());
    node.addEventListener('contextmenu', event => { if (enabled) event.preventDefault(); });
    return entry;
  }

  function remove(entry, path) {
    path?.remove();
    const index = entry.strokes.indexOf(path);
    if (index !== -1) entry.strokes.splice(index, 1);
  }
  function erase({ x, y }) {
    const at = new DOMPoint(x, y), radius = 10;
    for (const path of [...gesture.entry.strokes]) {
      const { x: l, y: t, width, height } = path.getBBox();
      const pad = radius + (parseFloat(getComputedStyle(path).strokeWidth) || 0) / 2;
      if (x < l - pad || x > l + width + pad || y < t - pad || y > t + height + pad) continue;
      let hit = path.isPointInStroke(at);
      for (let i = 0; !hit && i < 8; i++) {
        const angle = i * Math.PI / 4;
        hit = path.isPointInStroke(new DOMPoint(x + radius * Math.cos(angle), y + radius * Math.sin(angle)));
      }
      if (hit) remove(gesture.entry, path);
    }
  }
  function draw(point) {
    const g = gesture, gap = distance(g.last, point);
    if (gap < .1) return;
    if (g.tool === 'erase') {
      const count = Math.ceil(gap / 4);
      for (let i = 1; i <= count && g.entry.strokes.length; i++) {
        erase({ x: g.last.x + (point.x - g.last.x) * i / count, y: g.last.y + (point.y - g.last.y) * i / count });
      }
    } else {
      g.curve += `Q${pair(g.last)} ${pair(precise(mid(g.last, point)))}`;
      g.path.setAttribute('d', `${g.curve}L${pair(point)}`);
    }
    g.last = point;
  }
  function finish() {
    if (!gesture) return;
    const { entry, id } = gesture; gesture = null;
    if (entry.node.hasPointerCapture(id)) entry.node.releasePointerCapture(id);
  }
  function refresh() {
    current?.node.classList.toggle('is-drawing', enabled);
    toolbar.hidden = !enabled;
    // A standalone, touch-sized entry point in presentation (unlike the mouse edge nav).
    opener.hidden = enabled || deck.mode !== 'present';
    for (const button of toolbar.querySelectorAll('[aria-pressed]')) button.setAttribute('aria-pressed', String(button.dataset.penTool === tool));
  }
  function setEnabled(on) {
    if (on && (!api.active() || !deck.slides[deck.page])) return false;
    finish(); enabled = on;
    if (on) current = surface(deck.slides[deck.page]);
    refresh(); return true;
  }
  function button(className, label, action, toolName) {
    const el = document.createElement('button'); el.type = 'button'; el.className = className; el.textContent = label;
    el.addEventListener('click', event => { action(); if (event.detail) el.blur(); });
    if (toolName) { el.dataset.penTool = toolName; el.setAttribute('aria-pressed', 'false'); }
    return el;
  }
  deck.ext('pen', {
    init(shared) {
      api = shared; api.interactive('.pen-surface');
      const chrome = document.createElement('div'); chrome.className = 'pen-chrome'; chrome.dataset.deckRt = '';
      toolbar = document.createElement('div'); toolbar.className = 'pen-tools'; toolbar.setAttribute('role', 'group'); toolbar.setAttribute('aria-label', '판서 도구');
      opener = button('pen-button pen-open', '펜', () => setEnabled(true)); opener.title = '펜 (P)';
      for (const [id, label] of tools) toolbar.append(button(`pen-button pen-tool pen-${id}`, label, () => { finish(); tool = id; refresh(); }, id));
      toolbar.append(button('pen-button pen-clear', '모두 지우기', () => {
        finish(); if (current) { current.strokes.length = 0; current.node.replaceChildren(); }
      }), button('pen-button pen-close', '닫기', () => setEnabled(false)));
      chrome.append(opener, toolbar); document.body.append(chrome);
      window.addEventListener('blur', finish);
      refresh();
    },
    enter(page) { current = enabled ? surface(page) : surfaces.get(page); refresh(); },
    leave(page) { finish(); surfaces.get(page)?.node.classList.remove('is-drawing'); current = null; },
    mode(mode) { if (mode === 'edit') setEnabled(false); else refresh(); },
    key(event) {
      if (!api?.active() || event.altKey || event.shiftKey) return false;
      const key = event.code || event.key?.toLowerCase(), command = event.ctrlKey || event.metaKey;
      const target = event.composedPath?.()[0];
      if (!command && (key === 'KeyP' || key === 'p')) {
        if (target?.isContentEditable || target?.matches?.('input,textarea,select')) return false;
        return event.repeat || setEnabled(!enabled);
      }
      if (enabled && command && (key === 'KeyZ' || key === 'z')) {
        if (!event.repeat) { finish(); if (current) remove(current, current.strokes.at(-1)); }
        return true;
      }
      return false;
    }
  });
})();
