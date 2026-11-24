(() => {
  'use strict';
  const deck = window.deck, ns = 'http://www.w3.org/2000/svg';
  const math = Object.assign(Object.create(null), { Math, Infinity, NaN });
  for (const name of Object.getOwnPropertyNames(Math)) math[name] = Math[name];
  const labSelector = '[data-lab]', graphSelector = '[data-graph]';
  const inputSelector = 'input[type="range"][data-var],input[type="number"][data-var],input[type="checkbox"][data-var],select[data-var]';
  const actionSelector = '[data-set],[data-add]', dragSelector = '[data-drag]';
  const outputSelector = '[data-show],[data-if],[data-bind]';
  const roots = new WeakMap(), graphs = new WeakMap(), hooks = new WeakMap(), waiting = new Map();
  const compiled = new Map(), issues = new WeakSet(), editAllowed = new WeakSet(), remembered = new WeakMap();
  const all = (root, selector) => [...root.querySelectorAll(selector)];
  const owns = (root, selector) => all(root, selector).filter(el => el.closest(labSelector) === root);
  const activeSlide = () => deck.slides[deck.page];
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const cleanNumber = n => Number(n.toPrecision(12));
  const svgNum = n => Number(n.toFixed(5));
  const minus = str => str.replace(/^-/, '−').replace(/e-/i, 'e−');
  const format = (value, fix) => typeof value === 'number'
    ? minus(fix == null || !Number.isFinite(value) ? String(cleanNumber(value)) : value.toFixed(fix)) : String(value ?? '');
  let api, drag, serial = 0, errorCount = 0, pendingRefresh = false;

  // Expressions belong to trusted, authored decks, just like deck-specific scripts.
  function compile(source, tuple = false) {
    const key = `${Number(tuple)}:${source}`;
    if (!compiled.has(key)) {
      try {
        if (!source.trim()) throw Error('식이 비었습니다.');
        compiled.set(key, { run: new Function('scope', `with (scope) { return (${tuple ? `[${source}\n]` : `${source}\n`}); }`) });
      } catch (error) { compiled.set(key, { error }); }
    }
    return compiled.get(key);
  }
  function signal(el, attr, source, error) {
    errorCount++;
    if (issues.has(el)) return;
    issues.add(el);
    console.error(`조작 탐구: ${el.id ? '#' + el.id : '<' + el.localName + '>'}의 ${attr}="${source}" 처리 중 오류: ${error.message || error}`);
  }
  function evaluate(el, attr, source, scope, tuple = false) {
    const expression = compile(source, tuple);
    try {
      if (expression.error) throw expression.error;
      return expression.run(scope);
    } catch (error) { signal(el, attr, source, error); return failed; }
  }
  const failed = Symbol();
  function makeScope(state, graph) {
    return Object.assign(Object.create(math), graph ? { X: graph.X, Y: graph.Y } : null, state);
  }
  function initial(root) {
    const state = JSON.parse(root.getAttribute('data-lab'));
    if (!state || typeof state !== 'object' || Array.isArray(state)) throw Error('처음 상태를 JSON 객체로 적으세요.');
    return Object.assign(Object.create(null), state);
  }
  function data(root) {
    if (!root) return null;
    const raw = root.getAttribute('data-lab');
    let lab = roots.get(root);
    if (!lab || lab.raw !== raw) {
      if (lab) resetLab(root);
      try { lab = { raw, state: initial(root), inputs: new Map(), hookUndo: [] }; roots.set(root, lab); }
      catch (error) { signal(root, 'data-lab', raw, error); return null; }
    }
    return lab;
  }
  function remember(el, name = null) {
    let names = remembered.get(el);
    if (!names) remembered.set(el, names = new Set());
    names.add(name); api.keep(el, name);
  }
  function attr(el, name, value) {
    const next = value == null ? null : String(value);
    if (el.getAttribute(name) === next) return;
    remember(el, name);
    if (next === null) el.removeAttribute(name); else el.setAttribute(name, next);
  }
  function text(el, value) {
    if (el.textContent === value) return;
    remember(el); el.textContent = value;
  }
  function klass(el, name, on) {
    if (el.classList.contains(name) === on) return;
    remember(el, 'class'); el.classList.toggle(name, on);
  }
  function fixOf(el) {
    const raw = el.getAttribute('data-fix');
    if (raw === null) return null;
    if (!/^\d+$/.test(raw) || Number(raw) > 100) throw Error('소수 자릿수는 0~100의 정수여야 합니다.');
    return Number(raw);
  }
  // Separators inside calls, strings, and function bodies are not declarations.
  function bindings(source) {
    const pieces = [];
    let depth = 0, quote = '', start = 0;
    for (let i = 0; i < source.length; i++) {
      const c = source[i];
      if (quote) { if (c === '\\') i++; else if (c === quote) quote = ''; }
      else if (c === '"' || c === "'" || c === '`') quote = c;
      else if ('([{'.includes(c)) depth++;
      else if (')]}'.includes(c)) depth--;
      else if (c === ';' && !depth) { pieces.push(source.slice(start, i)); start = i + 1; }
    }
    pieces.push(source.slice(start));
    return pieces.filter(part => part.trim()).map(part => {
      const m = /^\s*([_a-zA-Z][-\w.]*|--[\w-]+)\s*:\s*([\s\S]*)$/.exec(part);
      if (!m) throw Error('바인딩은 이름: 식 형식으로 적으세요.');
      return [m[1], m[2].trim()];
    });
  }
  function outputs(el, scope) {
    const show = el.getAttribute('data-show'), when = el.getAttribute('data-if'), bind = el.getAttribute('data-bind');
    if (show !== null) {
      try {
        const value = evaluate(el, 'data-show', show, scope);
        if (value !== failed) text(el, format(value, fixOf(el)));
      } catch (error) { signal(el, 'data-show', show, error); }
    }
    if (when !== null) {
      const value = evaluate(el, 'data-if', when, scope);
      if (value !== failed) klass(el, 'is-off', !value);
    }
    if (bind !== null) {
      try {
        for (const [name, source] of bindings(bind)) {
          const value = evaluate(el, `data-bind (${name})`, source, scope);
          if (value === failed) continue;
          if (name === 'text') text(el, format(value, null));
          else if (name.startsWith('--')) {
            const css = value == null ? '' : String(value);
            if (el.style.getPropertyValue(name) !== css) { remember(el, 'style'); el.style.setProperty(name, css); }
          } else attr(el, name, value);
        }
      } catch (error) { signal(el, 'data-bind', bind, error); }
    }
  }

  const interval = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite) && value[0] < value[1] && Number.isFinite(value[1] - value[0]);
  const svg = (tag, attrs, content = null) => {
    const el = document.createElementNS(ns, tag);
    for (const [name, value] of Object.entries(attrs)) el.setAttribute(name, value);
    if (content != null) el.textContent = content;
    return el;
  };
  function canvasScale(el) {
    const slide = el.closest('.slide');
    const zoom = slide?.getBoundingClientRect().width / 1920;
    if (!zoom || !el.getClientRects().length) return null;
    const m = el.getScreenCTM?.();
    if (!m) return null;
    const sx = Math.hypot(m.a, m.b) / zoom, sy = Math.hypot(m.c, m.d) / zoom;
    return sx > 0 && sy > 0 ? [sx, sy] : null;
  }
  function config(el, scale = null) {
    if (el.namespaceURI !== ns || el.localName !== 'svg') throw Error('data-graph는 SVG에 적으세요.');
    const settings = JSON.parse(el.getAttribute('data-graph'));
    if (!settings || !interval(settings.x) || !interval(settings.y)) throw Error('x, y에 증가하는 두 수의 범위를 적으세요.');
    const { x, y, grid = 1, ticks = 1, labels = ['x', 'y'], origin = true, equal = true } = settings;
    if (typeof equal !== 'boolean') throw Error('equal은 true 또는 false여야 합니다.');
    if (![grid, ticks].every(n => Number.isFinite(n) && n >= 0)) throw Error('grid, ticks는 0 이상의 수여야 합니다.');
    if (![grid, ticks].every(n => !n || [x, y].every(([lo, hi]) => (hi - lo) / n <= 800 && Number.isSafeInteger(Math.ceil(lo / n)) && Number.isSafeInteger(Math.floor(hi / n))))) throw Error('모눈 또는 눈금이 너무 촘촘합니다. 간격을 늘리세요.');
    if (!Array.isArray(labels) || labels.length !== 2 || !labels.every(label => typeof label === 'string')) throw Error('labels는 축 이름 두 개의 배열이어야 합니다.');
    if (typeof origin !== 'boolean') throw Error('origin은 true 또는 false여야 합니다.');
    const base = el.viewBox.baseVal;
    const width = Number(el.getAttribute('width')), height = Number(el.getAttribute('height'));
    const view = el.hasAttribute('viewBox') ? [base.x, base.y, base.width, base.height] : [0, 0, width || 300, height || 150];
    const boxValid = box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && box[2] > 0 && box[3] > 0;
    if (!boxValid(view)) throw Error('viewBox의 폭과 높이는 양수여야 합니다.');
    const [sx, sy] = scale || [1, 1];
    // The deck/thumbnail's own zoom is not part of the graph's canvas font size.
    // A 0.01px guard keeps the measured size above 32 despite CSS serialization rounding.
    const autoFont = scale ? 32.01 / sy : view[3] * .044;
    const font = Object.hasOwn(settings, 'font') ? settings.font : autoFont;
    if (!Number.isFinite(font) || font <= 0) throw Error('font는 양수여야 합니다.');
    const padding = Math.min(font, view[2] / 8, view[3] / 6);
    const fixedBox = Object.hasOwn(settings, 'box');
    let box = fixedBox ? settings.box : [view[0] + 2 * padding, view[1] + 1.5 * padding, view[2] - 3.5 * padding, view[3] - 3 * padding];
    if (!boxValid(box)) throw Error('box에 왼쪽, 위, 양수 폭과 높이를 적으세요.');
    const [xl, xr] = x, [yb, yt] = y;
    if (!fixedBox && equal) {
      const [l, t, w, h] = box, dx = xr - xl, dy = yt - yb;
      const unit = Math.min(w * sx / dx, h * sy / dy);
      const width = unit * dx / sx, height = unit * dy / sy;
      box = [l + (w - width) / 2, t + (h - height) / 2, width, height];
    }
    const [l, t, w, h] = box;
    return { x, y, grid, ticks, autoTicks: !Object.hasOwn(settings, 'ticks'), labels, origin, box, view, font,
      X: n => l + w * ((n - xl) / (xr - xl)), Y: n => t + h * ((yt - n) / (yt - yb)) };
  }
  function multiples([lo, hi], spacing) {
    if (!spacing) return [];
    const a = Math.ceil(lo / spacing - 1e-10), b = Math.floor(hi / spacing + 1e-10);
    return Array.from({ length: Math.max(0, b - a + 1) }, (_, i) => cleanNumber((a + i) * spacing));
  }
  function addGraphic(parent, tag, css, attrs, label) {
    const node = svg(tag, { class: css, 'data-runtime': '', ...attrs }, label);
    parent.append(node); return node;
  }
  function plane(el, g) {
    const [l, t, w, h] = g.box, r = l + w, b = t + h, f = g.font;
    const [vl, vt, vw] = g.view, vr = vl + vw;
    const vertical = g.x[0] <= 0 && g.x[1] >= 0, horizontal = g.y[0] <= 0 && g.y[1] >= 0;
    const ox = g.X(0), oy = g.Y(0), arm = f * .14, extra = f * .45;
    const folder = svg('g', { class: 'g-plane', 'data-runtime': '', 'data-lab-plane': '', 'aria-hidden': 'true' });
    const id = g.clip = `deck-graph-clip-${++serial}`;
    const defs = addGraphic(folder, 'defs', 'g-defs');
    addGraphic(addGraphic(defs, 'clipPath', 'g-clip', { id, clipPathUnits: 'userSpaceOnUse' }), 'rect', 'g-clip-box', { x: l, y: t, width: w, height: h });
    const line = (css, x1, y1, x2, y2) => addGraphic(folder, 'line', css, { x1, y1, x2, y2 });
    function label(css, x, y, anchor, str) {
      const node = addGraphic(folder, 'text', css, { x, y, 'text-anchor': anchor, 'font-size': f }, str);
      return { node, anchor };
    }
    g.names = [];
    for (const n of multiples(g.x, g.grid)) line('g-grid', g.X(n), t, g.X(n), b);
    for (const n of multiples(g.y, g.grid)) line('g-grid', l, g.Y(n), r, g.Y(n));
    if (horizontal) {
      const end = Math.min(r + extra, vr - 2);
      line('g-axis g-axis-x', Math.max(l - extra, vl + 2), oy, end - extra / 2, oy);
      addGraphic(folder, 'path', 'g-arrow g-arrow-x', { d: `M${end},${oy}l${-extra},${-arm}v${2 * arm}z` });
      if (g.labels[0]) g.names.push(label('g-name g-name-x', end - f * .2, oy + (oy - vt > f * 1.3 ? -1 : 1) * f * .94, 'middle', g.labels[0]));
    }
    if (vertical) {
      const end = Math.max(t - extra, vt + 2);
      line('g-axis g-axis-y', ox, Math.min(b + extra, g.view[1] + g.view[3] - 2), ox, end + extra / 2);
      addGraphic(folder, 'path', 'g-arrow g-arrow-y', { d: `M${ox},${end}l${-arm},${extra}h${2 * arm}z` });
      if (g.labels[1]) g.names.push(label('g-name g-name-y', ox + (vr - ox > f * 1.3 ? 1 : -1) * f * .9, end + f * .35, 'middle', g.labels[1]));
    }
    g.numbers = [[], []];
    const gap = f * .88;
    for (const n of multiples(g.x, g.ticks)) if (n) {
      const x = g.X(n), y = horizontal ? oy : b;
      if (horizontal) line('g-tick g-tick-x', x, y - arm, x, y + arm);
      const pos = label('g-number g-number-x', x, y + gap, 'middle', format(n, null));
      pos.index = Math.round(n / g.ticks);
      g.numbers[0].push([pos, [x, y + gap, 'middle'], horizontal ? [x, y - gap, 'middle'] : null]);
    }
    for (const n of multiples(g.y, g.ticks)) if (n) {
      const x = vertical ? ox : l, y = g.Y(n);
      if (vertical) line('g-tick g-tick-y', x - arm, y, x + arm, y);
      const pos = label('g-number g-number-y', x - f * .35, y, 'end', format(n, null));
      pos.index = Math.round(n / g.ticks);
      g.numbers[1].push([pos, [x - f * .35, y, 'end'], vertical ? [x + f * .35, y, 'start'] : null]);
    }
    if (horizontal && vertical && g.origin) {
      const left = ox - f * .38, right = ox + f * .38;
      g.originLabel = [label('g-origin', left, oy + gap, 'end', 'O'),
        [left, oy + gap, 'end'], [left, oy - gap, 'end'], [right, oy + gap, 'start'], [right, oy - gap, 'start']];
    }
    el.prepend(folder); g.folder = folder;
  }
  function graph(el) {
    if (!el) return null;
    const scale = canvasScale(el);
    // Ignore floating-point noise when the whole slide is scaled.
    const key = [el.getAttribute('data-graph'), ...['viewBox','width','height'].map(name => el.getAttribute(name)),
      ...(scale || []).map(n => cleanNumber(n).toPrecision(6))].join('\n');
    let g = graphs.get(el);
    if (!g || g.key !== key || g.folder.parentElement !== el) {
      g?.folder.remove();
      try { g = config(el, scale); plane(el, g); g.key = key; graphs.set(el, g); }
      catch (error) { signal(el, 'data-graph', el.getAttribute('data-graph'), error); return null; }
    }
    return g;
  }
  function readSpan(source) {
    const [lo, hi, extra] = source.split('..');
    const span = [Number(lo), Number(hi)];
    if (!lo?.trim() || !hi?.trim() || extra !== undefined || !interval(span)) throw Error('범위를 작은 수..큰 수로 적으세요.');
    return span;
  }
  function fnPath(el, g, scope) {
    const source = el.getAttribute('data-fn'), expr = compile(source);
    try {
      if (expr.error) throw expr.error;
      const raw = el.getAttribute('data-domain'), domain = raw === null ? g.x : readSpan(raw);
      const lo = Math.max(domain[0], g.x[0]), hi = Math.min(domain[1], g.x[1]), [bottom, top] = g.y;
      const evaluateAt = x => { scope.x = x; return expr.run(scope); };
      const visible = y => Number.isFinite(y) && y >= bottom && y <= top;
      const runs = [];
      let run, previous;
      function emit(x, y, start = false) {
        if (start || !run) { run = []; runs.push(run); }
        run.push([g.X(x), g.Y(y)]);
      }
      function continuous(a, b) {
        const span = top - bottom, jump = Math.abs(b.y - a.y);
        let m = { x: (a.x + b.x) / 2 };
        m.y = evaluateAt(m.x);
        if (!visible(m.y)) return false;
        if (Math.abs(m.y - (a.y + b.y) / 2) <= Math.max(span * .002, jump * .18)) return true;
        // A genuine jump retains most of its height even after shrinking the interval.
        // A steep but continuous curve's height falls as the interval shrinks.
        for (let i = 0; i < 8; i++) {
          if (Math.abs(m.y - a.y) > Math.abs(b.y - m.y)) b = m; else a = m;
          m = { x: (a.x + b.x) / 2 }; m.y = evaluateAt(m.x);
          if (!visible(m.y)) return false;
        }
        return Math.abs(b.y - a.y) <= Math.max(span * .004, jump * .25);
      }
      function boundary(xIn, yIn, xOut, yOut) {
        if (!Number.isFinite(yOut)) return null;
        const edge = yOut > top ? top : bottom;
        // Refine where a continuous branch crosses the visible rectangle. The
        // sampled chords, not an out-of-range ordinate, form the visible path.
        for (let i = 0; i < 12; i++) {
          const x = (xIn + xOut) / 2, y = evaluateAt(x);
          if (!Number.isFinite(y)) return null;
          if (visible(y)) { xIn = x; yIn = y; }
          else { xOut = x; yOut = y; }
        }
        if (Math.abs(yIn - edge) > (top - bottom) * .02) return null;
        return [(xIn + xOut) / 2, edge];
      }
      if (lo <= hi) for (let i = 0; i < 200; i++) {
        const x = i === 199 ? hi : lo + (hi - lo) * i / 199, y = evaluateAt(x);
        const ok = visible(y), last = previous, lastOk = last && visible(last.y);
        if (ok && lastOk) {
          if (!continuous(last, { x, y })) run = null;
          emit(x, y);
        } else if (ok) {
          run = null;
          const edge = last && boundary(x, y, last.x, last.y);
          if (edge) emit(...edge);
          emit(x, y);
        } else if (lastOk) {
          const edge = boundary(last.x, last.y, x, y);
          if (edge) emit(...edge);
          run = null;
        }
        previous = { x, y };
        if (lo === hi) break;
      }
      const d = runs.filter(run => run.length > 1).map(run => run.map(([x, y], i) => `${i ? 'L' : 'M'}${svgNum(x)} ${svgNum(y)}`).join('')).join('');
      attr(el, 'd', d); attr(el, 'clip-path', `url(#${g.clip})`);
      return runs;
    } catch (error) { signal(el, 'data-fn', source, error); attr(el, 'd', ''); return []; }
  }
  function spot(el, g, scope) {
    const source = el.getAttribute('data-pt'), value = evaluate(el, 'data-pt', source, scope, true);
    if (value === failed) return;
    if (value.length !== 2 || !value.every(Number.isFinite)) {
      signal(el, 'data-pt', source, Error('유한한 좌표 두 개가 필요합니다.')); return;
    }
    let x = g.X(value[0]), y = g.Y(value[1]);
    const svgRoot = el.closest(graphSelector), parent = el.parentElement;
    if (parent !== svgRoot && parent.getScreenCTM) {
      const outer = svgRoot.getScreenCTM(), inner = parent.getScreenCTM();
      if (outer && inner) ({ x, y } = new DOMPoint(x, y).matrixTransform(inner.inverse().multiply(outer)));
    }
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    if (el.localName === 'circle') { attr(el, 'cx', svgNum(x)); attr(el, 'cy', svgNum(y)); }
    else attr(el, 'transform', `translate(${svgNum(x)} ${svgNum(y)})`);
  }
  function labelBounds(label, f) {
    if (!label.bounds) {
      const { node, anchor } = label, b = node.getBBox();
      const width = b.width || f * .57 * node.textContent.length;
      const height = b.height || f;
      const dy = b.height ? b.y - Number(node.getAttribute('y')) : -height / 2;
      label.bounds = (x, y, align = anchor) => {
        const left = x - (align === 'end' ? width : align === 'middle' ? width / 2 : 0);
        const pad = f * .09;
        return [left - pad, y + dy - pad, left + width + pad, y + dy + height + pad];
      };
    }
    return label.bounds;
  }
  function placeLabels(g, runs) {
    const [vl, vt, vw, vh] = g.view, f = g.font;
    const reserved = [];
    const origin = g.originLabel ? [g.originLabel[0]] : [];
    for (const label of [...g.names, ...origin, ...g.numbers.flat().map(([label]) => label)]) labelBounds(label, f);
    for (const label of g.names) reserved.push(label.bounds(Number(label.node.getAttribute('x')), Number(label.node.getAttribute('y'))));
    const choose = ([label, ...choices], omitBlocked = false) => {
      const { node } = label;
      const usable = choices.filter(Boolean).map(([x, y, anchor]) => {
        const rect = label.bounds(x, y, anchor);
        const [l, t, r, b] = rect;
        const overlap = ([a, c, d, e]) => l < d && a < r && t < e && c < b;
        const blocked = reserved.some(overlap) || l < vl || t < vt || r > vl + vw || b > vt + vh;
        const curve = runs.some(run => run.some(([ax, ay], i) => {
          const [bx, by] = run[i + 1] || [ax, ay];
          // Chords are left-to-right. Test their y span over only the label's x span.
          if (bx < l || ax > r) return false;
          const dy = by - ay, dx = bx - ax || 1;
          const a = ay + dy * clamp((l - ax) / dx, 0, 1), z = ay + dy * clamp((r - ax) / dx, 0, 1);
          return Math.min(a, z) <= b && Math.max(a, z) >= t;
        }));
        return { x, y, anchor, rect, score: Number(blocked) * 2 + Number(curve) };
      });
      const best = usable.reduce((a, b) => a.score <= b.score ? a : b);
      if (omitBlocked && best.score >= 2) { node.setAttribute('visibility', 'hidden'); return; }
      node.removeAttribute('visibility');
      node.setAttribute('x', best.x); node.setAttribute('y', best.y); node.setAttribute('text-anchor', best.anchor);
      reserved.push(best.rect);
    };
    if (g.originLabel) choose(g.originLabel);
    for (const [axis, entries] of g.numbers.entries()) {
      if (!entries.length) continue;
      const [lo, hi] = axis ? g.y : g.x;
      const cell = g.box[axis + 2] * g.ticks / (hi - lo);
      const span = Math.max(...entries.map(([label]) => {
        const rect = label.bounds(0, 0); return rect[axis + 2] - rect[axis];
      }));
      const stride = g.autoTicks ? Math.max(1, Math.ceil(span / cell)) : 1;
      // Keep the same regular spacing on either side of zero.
      for (const entry of entries) {
        const { node, index } = entry[0];
        if (index % stride) node.setAttribute('visibility', 'hidden');
        else choose(entry, g.autoTicks);
      }
    }
  }

  function syncControls(root, lab) {
    for (const el of owns(root, inputSelector)) {
      const key = el.getAttribute('data-var');
      if (!Object.hasOwn(lab.state, key)) continue;
      const prop = el.type === 'checkbox' ? 'checked' : 'value';
      const value = prop === 'checked' ? Boolean(lab.state[key]) : String(lab.state[key] ?? '');
      if (el[prop] !== value) {
        if (!lab.inputs.has(el)) lab.inputs.set(el, { prop, value: el[prop] });
        el[prop] = value;
      }
    }
  }
  function renderGraph(svgRoot, root, scope, layoutOnly = false) {
    const previous = graphs.get(svgRoot), g = graph(svgRoot);
    if (!g || (layoutOnly && g === previous)) return;
    const ownsGraph = el => el.closest(graphSelector) === svgRoot && el.closest(labSelector) === root;
    const graphScope = makeScope(scope, g), runs = [];
    // Bindings such as x1:X(0) must follow the same mapping as the curve and points.
    if (layoutOnly && root) {
      for (const el of all(svgRoot, outputSelector).filter(ownsGraph)) outputs(el, graphScope);
    }
    for (const el of all(svgRoot, '[data-fn],[data-pt]').filter(ownsGraph)) {
      if (el.hasAttribute('data-fn') && el.localName === 'path') runs.push(...fnPath(el, g, Object.create(graphScope)));
      if (el.hasAttribute('data-pt')) spot(el, g, graphScope);
    }
    placeLabels(g, runs);
  }
  function renderGraphs(slide, root, scope, element = slide) {
    for (const svgRoot of all(element, graphSelector).filter(el => el.closest(labSelector) === root)) renderGraph(svgRoot, root, scope);
  }
  function refreshGraph(svgRoot) {
    const root = svgRoot.closest(labSelector), lab = data(root);
    if (!root || lab) renderGraph(svgRoot, root, lab?.state, true);
  }
  function refreshGraphs(slide) {
    for (const el of all(slide, graphSelector)) refreshGraph(el);
  }
  function runCustom(root, lab) {
    const hook = hooks.get(root);
    if (!hook) return;
    // Keep reversible mutations made by the synchronous custom renderer. Data-runtime
    // remains available for code that creates its own disposable drawing layer.
    const observer = new MutationObserver(() => {});
    observer.observe(root, { subtree: true, attributes: true, attributeOldValue: true,
      childList: true, characterData: true, characterDataOldValue: true });
    try { hook(copyState(lab.state), root); }
    catch (error) { signal(root, 'deck.lab', hook.name || 'render', error); }
    finally { lab.hookUndo.push(...observer.takeRecords()); observer.disconnect(); }
  }
  function undoCustom(lab, restoredRoot) {
    // The core restores kept values before reset(slide). Do not put a custom
    // renderer's intermediate declarative values back after that restoration.
    const restored = (el, name) => restoredRoot?.contains(el) && remembered.get(el)?.has(name);
    for (let i = lab.hookUndo.length - 1; i >= 0; i--) {
      const { type, target, attributeName, attributeNamespace, oldValue, addedNodes, removedNodes, nextSibling } = lab.hookUndo[i];
      if (target.nodeType === Node.ELEMENT_NODE && target.closest('[data-runtime]')) continue;
      if (type === 'attributes') {
        if (restored(target, attributeName)) continue;
        if (oldValue == null) target.removeAttributeNS(attributeNamespace, attributeName);
        else target.setAttributeNS(attributeNamespace, attributeName, oldValue);
      } else if (type === 'characterData') {
        if (!target.parentElement?.closest('[data-runtime]')) target.data = oldValue;
      } else if (!restored(target, null)) {
        for (const child of addedNodes) if (child.parentNode === target) child.remove();
        const before = nextSibling?.parentNode === target ? nextSibling : null;
        for (const child of removedNodes) if (!child.parentNode) target.insertBefore(child, before);
      }
    }
    lab.hookUndo.length = 0;
  }
  function render(root, preview = false) {
    const lab = data(root);
    if (!lab || !api) return;
    if (!preview) {
      undoCustom(lab);
      syncControls(root, lab);
      for (const el of owns(root, outputSelector)) outputs(el, makeScope(lab.state, graph(el.closest(graphSelector))));
    }
    renderGraphs(root.closest('.slide'), root, lab.state, root);
    if (!preview) runCustom(root, lab);
  }
  function renderSlide(slide, preview = false) {
    if (!slide) return;
    for (const [selector, hook] of waiting) {
      const root = findRoot(selector);
      if (root) { hooks.set(root, hook); waiting.delete(selector); }
    }
    for (const root of all(slide, labSelector)) render(root, preview);
    renderGraphs(slide, null, null);
  }
  function resetLab(root, coreReset = false) {
    const lab = roots.get(root);
    if (lab) {
      undoCustom(lab, coreReset ? root : null);
      for (const [el, { prop, value }] of lab.inputs) el[prop] = value;
    }
    api?.restore(root); roots.delete(root);
    for (const el of [root, ...owns(root, '*')]) remembered.delete(el);
    for (const el of owns(root, '[data-lab-plane]')) el.remove();
  }
  const copyState = state => structuredClone({ ...state });
  function findRoot(root) {
    const el = typeof root === 'string' ? document.querySelector(root) : root;
    return el?.matches?.(labSelector) ? el : null;
  }
  function register(rootOrSelector, callback) {
    if (typeof callback !== 'function') throw TypeError('그리기 함수를 지정하세요.');
    const root = findRoot(rootOrSelector);
    if (root) {
      hooks.set(root, callback);
      if (api?.active() && activeSlide()?.contains(root)) render(root);
    } else if (typeof rootOrSelector === 'string') waiting.set(rootOrSelector, callback);
  }
  register.state = root => {
    const el = findRoot(root), lab = data(el);
    return lab ? copyState(lab.state) : undefined;
  };
  register.set = (root, patch) => {
    const el = findRoot(root), lab = data(el);
    if (!lab || !patch || typeof patch !== 'object') return undefined;
    for (const [key, value] of Object.entries(patch)) if (Object.hasOwn(lab.state, key)) lab.state[key] = structuredClone(value);
    if (api && activeSlide()?.contains(el)) render(el);
    return copyState(lab.state);
  };
  deck.lab = register;

  function allowed(el, event) {
    return activeSlide()?.contains(el) && (api.active() || (deck.mode === 'edit' && !el.closest('[contenteditable]') && (event.ctrlKey || editAllowed.has(el))));
  }
  function control(event) {
    const el = event.target;
    if (!el.matches?.(inputSelector) || !allowed(el, event) || el.disabled) return;
    const root = el.closest(labSelector), lab = data(root), key = el.getAttribute('data-var');
    if (!lab || !Object.hasOwn(lab.state, key)) return;
    let value = el.value;
    if (el.type === 'checkbox') value = el.checked;
    else if (el.type === 'number' || el.type === 'range') {
      value = el.valueAsNumber;
      if (!Number.isFinite(value)) { if (event.type === 'change') syncControls(root, lab); return; }
    } else if (typeof lab.state[key] === 'number') {
      value = Number(value); if (!Number.isFinite(value)) return;
    } else if (typeof lab.state[key] === 'boolean') value = value === 'true';
    if (!lab.inputs.has(el)) lab.inputs.set(el, { prop: el.type === 'checkbox' ? 'checked' : 'value', value: el.type === 'checkbox' ? el.defaultChecked : el.localName === 'select' ? initial(root)[key] : el.defaultValue });
    if (!Object.is(lab.state[key], value)) { lab.state[key] = value; render(root); }
    if (event.type === 'change' && el.type !== 'number' && pointerControl === el) el.blur();
  }
  function actionParts(el) {
    const attr = el.hasAttribute('data-set') ? 'data-set' : 'data-add';
    const raw = el.getAttribute(attr), at = raw.indexOf(attr === 'data-set' ? '=' : ':');
    if (at <= 0) throw Error('변수 이름과 식을 적으세요.');
    return [attr, raw.slice(0, at).trim(), raw.slice(at + 1).trim()];
  }
  function act(event) {
    const el = event.target.closest?.(actionSelector);
    if (!el || !allowed(el, event) || el.disabled || (deck.mode === 'edit' && !event.ctrlKey)) return;
    const root = el.closest(labSelector), lab = data(root);
    if (!lab) return;
    try {
      const [name, key, source] = actionParts(el);
      if (!Object.hasOwn(lab.state, key)) return;
      const value = evaluate(el, name, source, makeScope(lab.state));
      if (value === failed) return;
      if (name === 'data-add' && (!Number.isFinite(value) || !Number.isFinite(lab.state[key] + value))) throw Error('더할 값과 상태 값은 수여야 합니다.');
      lab.state[key] = name === 'data-set' ? value : cleanNumber(lab.state[key] + value);
      render(root); event.preventDefault();
      if (event.detail) el.blur?.();
    } catch (error) { signal(el, '조절 단추', el.getAttribute('data-set') ?? el.getAttribute('data-add'), error); }
  }
  function dragConfig(el, state, g) {
    const keys = el.getAttribute('data-drag').split(',').map(s => s.trim());
    if (keys.length !== 2 || keys[0] === keys[1] || !keys.every(k => Object.hasOwn(state, k) && Number.isFinite(state[k]))) throw Error('서로 다른 수 상태 두 개를 적으세요.');
    const axes = { x: g?.x || [0, 1920], y: g?.y || [0, 1080] };
    for (const piece of (el.getAttribute('data-range') || '').split(';').filter(s => s.trim())) {
      const m = /^\s*([xy])\s*:\s*(.*)$/.exec(piece);
      if (!m) throw Error('data-range는 x:작은 수..큰 수; y:작은 수..큰 수 형식입니다.');
      axes[m[1]] = readSpan(m[2]);
    }
    const snap = Number(el.getAttribute('data-snap')), axis = el.getAttribute('data-axis');
    if (!Number.isFinite(snap) || snap < 0 || (axis != null && !['x', 'y'].includes(axis))) throw Error('스냅은 0 이상의 수, 이동 축은 x 또는 y여야 합니다.');
    return { keys, snap, axes, axis };
  }
  function xy(event, svgRoot, g) {
    if (!g) return api.point(event);
    const matrix = svgRoot.getScreenCTM();
    if (!matrix) return null;
    const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return null;
    const [l, t, w, h] = g.box;
    return { x: g.x[0] + (g.x[1] - g.x[0]) * (p.x - l) / w,
      y: g.y[1] - (g.y[1] - g.y[0]) * (p.y - t) / h };
  }
  let pointerControl;
  function pointerDown(event) {
    const target = event.target, control = target.closest?.(inputSelector) || target.closest?.('label')?.control;
    if (pointerControl) editAllowed.delete(pointerControl);
    pointerControl = control?.matches(inputSelector) ? control : null;
    if (pointerControl && event.ctrlKey) editAllowed.add(pointerControl);
    const el = target.closest?.(dragSelector);
    if (!el || drag || event.button !== 0 || !event.isPrimary || !allowed(el, event)) return;
    const root = el.closest(labSelector), lab = data(root);
    if (!lab) return;
    const svgRoot = el.closest(graphSelector), g = graph(svgRoot);
    if (svgRoot && !g) return;
    try {
      const options = dragConfig(el, lab.state, g), start = xy(event, svgRoot, g);
      if (!start) return;
      const [kx, ky] = options.keys;
      const last = [event.clientX, event.clientY];
      drag = { el, root, lab, svgRoot, g, ...options, x0: lab.state[kx], y0: lab.state[ky], start, last, id: event.pointerId };
      if (event.isTrusted) el.setPointerCapture(event.pointerId);
      el.classList.add('is-drag'); document.activeElement?.blur?.();
      event.preventDefault();
    } catch (error) { signal(el, 'data-drag', el.getAttribute('data-drag'), error); }
  }
  function pointerMove(event) {
    if (drag?.id !== event.pointerId) return;
    const { root, lab, svgRoot, g, keys, axis, axes, snap, x0, y0, start, last } = drag;
    if (event.clientX === last[0] && event.clientY === last[1]) return;
    const p = xy(event, svgRoot, g);
    if (!p) return;
    drag.last = [event.clientX, event.clientY];
    const snapTo = (n, [lo, hi]) => cleanNumber(clamp(snap ? Math.round(n / snap) * snap : n, lo, hi));
    const x = snapTo(x0 + p.x - start.x, axes.x), y = snapTo(y0 + p.y - start.y, axes.y);
    if (axis !== 'y') lab.state[keys[0]] = x;
    if (axis !== 'x') lab.state[keys[1]] = y;
    render(root); event.preventDefault();
  }
  function endDrag(event) {
    if (!drag || (event?.pointerId != null && event.pointerId !== drag.id)) return;
    const { el, id } = drag; drag = null;
    el.classList.remove('is-drag'); if (!el.getAttribute('class')) el.removeAttribute('class');
    if (el.hasPointerCapture(id)) el.releasePointerCapture(id);
  }

  const problem = (target, message, code = 'widget-invalid') => ({ code, level: 'error', target, message });
  function validate(slide) {
    const results = [], seeds = new Map();
    const check = (el, fn) => { try { fn(); } catch (error) { results.push(problem(el, error.message)); } };
    const expression = (source, attr, tuple = false) => {
      if (compile(source, tuple).error) throw Error(`${attr} 식을 읽을 수 없습니다: ${source}`);
    };
    for (const el of all(slide, labSelector)) check(el, () => seeds.set(el, initial(el)));
    for (const el of all(slide, graphSelector)) check(el, () => config(el));
    for (const el of all(slide, '[data-var],[data-drag],[data-set],[data-add],[data-show],[data-if],[data-bind],[data-fn],[data-pt]')) {
      const root = el.closest(labSelector), seed = seeds.get(root);
      if (el.hasAttribute('data-var')) check(el, () => {
        if (!el.matches(inputSelector) || !seed || !Object.hasOwn(seed, el.getAttribute('data-var'))) throw Error(`data-var의 입력 형식 또는 변수 이름을 확인하세요: ${el.getAttribute('data-var')}`);
      });
      if (el.hasAttribute('data-drag')) check(el, () => dragConfig(el, seed || {}, null));
      if (el.matches(actionSelector)) check(el, () => {
        const [name, key, source] = actionParts(el);
        if (!seed || !Object.hasOwn(seed, key)) throw Error(`${name} 상태에 ${key} 변수가 없습니다.`);
        expression(source, name);
      });
      for (const name of ['data-show', 'data-if', 'data-fn', 'data-pt']) if (el.hasAttribute(name)) check(el, () => {
        if (name === 'data-fn' && (el.localName !== 'path' || !el.closest(graphSelector))) throw Error('data-fn은 data-graph SVG 안의 path에 적으세요.');
        if (name === 'data-pt' && !el.closest(graphSelector)) throw Error('data-pt는 data-graph SVG 안에 적으세요.');
        expression(el.getAttribute(name), name, name === 'data-pt');
        if (name === 'data-show') fixOf(el);
        if (name === 'data-fn' && el.hasAttribute('data-domain')) readSpan(el.getAttribute('data-domain'));
      });
      if (el.hasAttribute('data-bind')) check(el, () => {
        const list = bindings(el.getAttribute('data-bind'));
        if (!list.length) throw Error('data-bind에 바인딩을 적으세요.');
        for (const [name, source] of list) expression(source, `data-bind (${name})`);
      });
    }
    return results;
  }
  function selftest(slide) {
    const found = [], fail = (el, message) => found.push(problem(el, message, 'widget-broken'));
    if (validate(slide).length) return found; // 구조 오류는 audit에서 알린다.
    const atStart = errorCount;
    renderSlide(slide);
    for (const root of all(slide, labSelector)) {
      const lab = data(root), original = copyState(lab.state);
      const snapshot = () => root.outerHTML; // Includes the custom renderer; live input properties are not markup.
      const controls = owns(root, inputSelector).filter(el => !el.matches(':disabled') && !el.readOnly);
      for (const el of controls) {
        const initialValue = lab.state[el.getAttribute('data-var')];
        const prop = el.type === 'checkbox' ? 'checked' : 'value';
        const limits = el.type === 'checkbox' ? [false, true] : el.localName === 'select'
          ? Array.from(el.options).filter(option => !option.disabled && !option.closest('optgroup[disabled]')).map(option => option.value)
          : [el.min || (el.type === 'range' ? '0' : Number(initialValue) - 1), el.max || (el.type === 'range' ? '100' : Number(initialValue) + 1)];
        const before = errorCount;
        const screens = [limits[0], limits.at(-1)].map(value => {
          el[prop] = value; el.dispatchEvent(new InputEvent('input', { bubbles: true })); return snapshot();
        });
        if (errorCount > before || screens[0] === screens[1]) fail(el, '조절기의 양 끝 값에서 표시가 달라지지 않거나 식 오류가 있습니다.');
        // Isolate the next control; hold keeps the last control or drag result.
        if (el !== controls.at(-1)) { Object.assign(lab.state, original); render(root); }
      }
      for (const el of owns(root, dragSelector)) {
        if (el.closest('[data-waiting],[data-gone],[hidden],.is-off')) continue;
        const box = el.getBoundingClientRect(), x = box.left + box.width / 2, y = box.top + box.height / 2;
        const before = JSON.stringify(lab.state), errors = errorCount;
        const send = (type, dx = 0, dy = 0) => el.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
          pointerId: 431, isPrimary: true, button: 0, clientX: x + dx, clientY: y + dy, pointerType: 'mouse' }));
        if (box.width && box.height) {
          send('pointerdown');
          let end;
          for (const offset of [[130, 95], [-130, -95], [300, -300], [-300, 300]]) {
            end = offset; send('pointermove', ...end);
            if (JSON.stringify(lab.state) !== before) break;
          }
          send('pointerup', ...end);
        }
        if (errorCount > errors || JSON.stringify(lab.state) === before) fail(el, '점을 끌어도 상태가 달라지지 않거나 식 오류가 있습니다.');
      }
    }
    if (!found.length && errorCount > atStart) fail(slide, '조작 탐구를 그리는 도중 식 오류가 있습니다.');
    return found;
  }
  deck.ext('lab', {
    init(shared) {
      api = shared;
      api.interactive('[data-lab] [data-var],[data-lab] [data-set],[data-lab] [data-add],[data-lab] [data-drag],[data-lab] label:has([data-var])');
      window.addEventListener('pointerdown', pointerDown);
      window.addEventListener('pointermove', pointerMove);
      window.addEventListener('pointerup', event => { pointerMove(event); endDrag(event); });
      for (const type of ['pointercancel', 'lostpointercapture', 'blur']) window.addEventListener(type, endDrag);
      window.addEventListener('input', control, true); window.addEventListener('change', control, true);
      window.addEventListener('click', act);
      const resize = new ResizeObserver(entries => {
        for (const { target } of entries) if (activeSlide()?.contains(target)) refreshGraph(target);
      });
      deck.on('slidechange', ({ slide }) => {
        if (deck.mode === 'edit') renderSlide(slide, true);
        else refreshGraphs(slide);
        resize.disconnect();
        for (const el of all(slide, graphSelector)) resize.observe(el);
      });
      deck.on('change', () => refreshGraphs(activeSlide()));
      // Overview clones have no runtime graph marks. Measure in the thumbnail's
      // own canvas, not in the hidden source slide or in the main deck.
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node.id === 'deck-overview' && node.isConnected) {
            for (const slide of all(node, '.deck-thumb-canvas > .slide')) {
              for (const plane of all(slide, '[data-lab-plane]')) plane.remove();
              refreshGraphs(slide);
            }
          }
        }
      }).observe(document.body, { childList: true });
      window.addEventListener('beforeprint', () => {
        for (const slide of deck.slides) {
          const hidden = slide.getAttribute('hidden');
          const gone = all(slide, '[data-gone]').map(el => [el, el.getAttribute('data-gone')]);
          // beforeprint can run before print CSS and the core's visibility handler.
          slide.removeAttribute('hidden');
          for (const [el] of gone) el.removeAttribute('data-gone');
          try { refreshGraphs(slide); }
          finally {
            if (hidden !== null) slide.setAttribute('hidden', hidden);
            for (const [el, value] of gone) el.setAttribute('data-gone', value);
          }
        }
      });
    },
    enter(slide) { renderSlide(slide); },
    leave(slide) {
      endDrag();
      for (const root of all(slide, labSelector)) if (!root.closest('[data-keep]')) resetLab(root);
    },
    reset(slide) {
      if (drag?.el.closest('.slide') === slide) endDrag();
      for (const root of all(slide, labSelector)) resetLab(root, true);
      api.restore(slide);
      for (const el of all(slide, '[data-lab-plane]')) el.remove();
      // The core calls reset synchronously during serialize. Redraw only once
      // serialization and any mode/page change in the same task are complete.
      if (!pendingRefresh && slide === activeSlide()) {
        pendingRefresh = true;
        queueMicrotask(() => { pendingRefresh = false; renderSlide(activeSlide(), deck.mode === 'edit'); });
      }
    },
    mode(next) { endDrag(); if (next === 'edit') renderSlide(activeSlide(), true); },
    prepareAudit(slide) {
      if (slide.querySelector(graphSelector) && !validate(slide).length) refreshGraphs(slide);
    },
    validate, selftest
  });
})();
