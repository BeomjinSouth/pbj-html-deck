(() => {
  'use strict';

  // 이 확장은 검사 결과만 돌려주며 장의 DOM이나 보기 상태를 바꾸지 않는다.
  const W = 1920, H = 1080, CELL = 48, MASK = 64;
  const widget = '[data-quiz],[data-flip],[data-tabs],[data-sort],[data-lab],[data-peek],[data-timer],[data-pick],[data-answer],[data-toggle]';
  const ignored = 'script,style,link,meta,template,defs,title,desc,[data-deck-rt]';
  const visual = `img,svg,canvas,math,table,${widget}`;
  const imageMasks = new Map(), backgrounds = new Map(), glyphs = new Map();
  const all = (root, selector) => [...root.querySelectorAll(selector)];
  const area = r => Math.max(0, r.width) * Math.max(0, r.height);
  const intersection = (a, b) => {
    const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
    return { x, y, width: Math.max(0, Math.min(a.x + a.width, b.x + b.width) - x), height: Math.max(0, Math.min(a.y + a.height, b.y + b.height) - y) };
  };
  const canvasRect = { x: 0, y: 0, width: W, height: H };
  const inline = display => /^(?:inline(?:$|[- ])|contents$)/.test(display);
  const fontFaceExists = name => [...document.fonts].some(face => face.status === 'loaded' && face.family.replace(/^['"]|['"]$/g, '').toLowerCase() === name.toLowerCase());

  function fontHas(name, char) {
    const key = `${name}\0${char}`;
    if (glyphs.has(key)) return glyphs.get(key);
    const ctx = fontHas.context ||= document.createElement('canvas').getContext('2d');
    if (!ctx) return true;
    const width = family => { ctx.font = `80px ${family}`; return ctx.measureText(char).width; };
    // fonts.check()는 글꼴이나 해당 글자가 없어도 true일 수 있다.
    const result = ['monospace', 'serif', 'sans-serif'].some(base => Math.abs(width(`"${name}",${base}`) - width(base)) > .01);
    glyphs.set(key, result); return result;
  }

  function mask(image, key) {
    if (!image?.complete || !image.naturalWidth || !image.naturalHeight) return null;
    if (imageMasks.has(key)) return imageMasks.get(key);
    try {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = MASK;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(image, 0, 0, MASK, MASK);
      const rgba = ctx.getImageData(0, 0, MASK, MASK).data;
      const alpha = new Uint8Array(MASK * MASK);
      let opaque = 0;
      for (let i = 0; i < alpha.length; i++) { alpha[i] = rgba[i * 4 + 3]; if (alpha[i] >= 245) opaque++; }
      const value = { alpha, opaque: opaque / alpha.length };
      imageMasks.set(key, value); return value;
    } catch { return null; } // 교차 출처·읽을 수 없는 그림은 불투명하다고 추정하지 않는다.
  }

  function backgroundImage(source) {
    if (!/^data:image\//i.test(source)) return null;
    if (!backgrounds.has(source)) { const image = new Image(); image.src = source; backgrounds.set(source, image); }
    return backgrounds.get(source);
  }
  function preloadBackgrounds() {
    document.fonts.addEventListener('loadingdone', () => glyphs.clear());
    for (const el of document.querySelectorAll('section.slide[data-slide],section.slide[data-slide] *')) {
      const match = /^url\(\s*(["']?)(data:image\/.*)\1\s*\)$/i.exec(getComputedStyle(el).backgroundImage);
      if (match) backgroundImage(match[2]);
    }
  }

  function validate(slide) {
    // 엔진이 단계를 펼친 뒤 호출한다. 현재 장에서 이미 실행 중인 등장 효과도 끝 위치에서 잰다.
    for (const animation of slide.getAnimations({ subtree: true })) try { animation.finish(); } catch { /* 무한 효과 */ }
    const origin = slide.getBoundingClientRect(), zoom = origin.width / W || 1;
    const styles = new Map(), visibility = new Map(), boxes = new Map(), results = [];
    const style = el => { if (!styles.has(el)) styles.set(el, getComputedStyle(el)); return styles.get(el); };
    const rect = r => ({ x: (r.left - origin.left) / zoom, y: (r.top - origin.top) / zoom, width: r.width / zoom, height: r.height / zoom });
    const box = el => { if (!boxes.has(el)) boxes.set(el, rect(el.getBoundingClientRect())); return boxes.get(el); };
    function rendered(el) {
      if (!el || el === slide.parentElement) return true;
      if (visibility.has(el)) return visibility.get(el);
      const cs = style(el), value = !el.matches(ignored) && cs.display !== 'none' && Number(cs.opacity) > 0 && rendered(el.parentElement);
      visibility.set(el, value); return value;
    }
    const visible = el => !!el && style(el).visibility === 'visible' && rendered(el);
    const opacity = el => { let value = 1; for (let node = el; node && node !== slide.parentElement; node = node.parentElement) value *= Number(style(node).opacity); return value; };
    const report = (code, level, target, message, r) => results.push({ code, level, target, message, ...(r ? { rect: r } : {}) });
    const elements = all(slide, '*'), blocks = new Map(), textRuns = [];
    const tree = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
    let weighted = 0;
    while (tree.nextNode()) {
      const node = tree.currentNode, el = node.parentElement;
      if (!node.data.trim() || !visible(el)) continue;
      const range = document.createRange(); range.selectNodeContents(node);
      const lines = [...range.getClientRects()].map(rect).filter(area);
      if (!lines.length) continue;
      let block = el;
      if (el.closest('svg')) block = el.closest('text') || el;
      else if (el.closest('math')) block = el.closest('math');
      else while (block !== slide && inline(style(block).display)) block = block.parentElement;
      if (block !== slide) {
        if (!blocks.has(block)) blocks.set(block, []);
        blocks.get(block).push(...lines);
      }
      textRuns.push({ node, el, block, lines });
      if (!el.closest('[data-opt],[data-chip],math,svg,[data-explain]') && !(el.closest('[data-peek]') && el !== el.closest('[data-peek]'))) {
        for (const ch of node.data) if (!/\s/u.test(ch)) weighted += /[A-Za-z0-9]/.test(ch) ? .5 : 1;
      }
    }
    if (weighted > 90) report('text-heavy', 'warn', slide, `보이는 글자가 ${weighted}자로 많습니다. 내용을 나누거나 줄이세요.`);

    const images = [];
    for (const img of elements.filter(el => el.localName === 'img' && visible(el))) {
      const r = box(img), cs = style(img), nw = img.naturalWidth, nh = img.naturalHeight;
      if (img.hasAttribute('data-placeholder')) report('placeholder-left', 'error', img, `자리표시 그림 ${img.dataset.asset || '(이름 없음)'}이 남았습니다. 실제 그림으로 바꾸세요.`);
      if (!nw || !nh || !area(r)) continue;
      // 클라이언트 사각형에는 화면 축소와 요소 자체의 scale이 적용된다.
      const sx = r.width / (img.offsetWidth || r.width);
      const sy = r.height / (img.offsetHeight || r.height);
      const px = name => parseFloat(cs[name]) || 0;
      const content = { x: r.x + (px('borderLeftWidth') + px('paddingLeft')) * sx, y: r.y + (px('borderTopWidth') + px('paddingTop')) * sy,
        width: Math.max(0, r.width - (px('borderLeftWidth') + px('borderRightWidth') + px('paddingLeft') + px('paddingRight')) * sx),
        height: Math.max(0, r.height - (px('borderTopWidth') + px('borderBottomWidth') + px('paddingTop') + px('paddingBottom')) * sy) };
      if (!area(content)) continue;
      const fit = cs.objectFit, natural = nw / nh;
      const unscaledW = content.width / (sx || 1), unscaledH = content.height / (sy || 1);
      if (fit === 'fill' && Math.abs((unscaledW / unscaledH) / natural - 1) > .030001) report('image-distorted', 'error', img, '그림의 가로세로 비가 원본과 다릅니다. 비율을 유지하거나 object-fit:contain을 쓰세요.');
      let dw = content.width, dh = content.height;
      if (fit !== 'fill') {
        let k = fit === 'cover' ? Math.max(unscaledW / nw, unscaledH / nh) : fit === 'none' ? 1 : Math.min(unscaledW / nw, unscaledH / nh);
        if (fit === 'scale-down') k = Math.min(1, k);
        dw = nw * k * sx; dh = nh * k * sy;
      }
      const position = cs.objectPosition.split(/\s+/);
      const offset = (part, available) => part?.endsWith('%') ? available * parseFloat(part) / 100 : (parseFloat(part) || 0);
      const painted = { x: content.x + offset(position[0], content.width - dw), y: content.y + offset(position[1] || '50%', content.height - dh), width: dw, height: dh };
      const source = img.currentSrc || img.src || '';
      if (!/^data:image\/svg\+xml/i.test(source) && !/\.svg(?:[?#]|$)/i.test(source) && (dw / (sx || 1) / nw > 1.4 || dh / (sy || 1) / nh > 1.4)) report('image-upscaled', 'warn', img, '그림을 원본보다 1.4배 넘게 키웠습니다. 더 큰 원본을 쓰거나 그림을 줄이세요.');
      images.push({ el: img, painted, clip: content, mask: mask(img, source), opacity: opacity(img) });
    }
    // 배경 그림도 시안 전체 이미지 판정에 포함한다.
    for (const el of [slide, ...elements]) {
      if (!visible(el)) continue;
      const match = /^url\(\s*(["']?)(data:image\/.*)\1\s*\)$/i.exec(style(el).backgroundImage);
      if (!match) continue;
      const image = backgroundImage(match[2]), r = box(el), cs = style(el);
      if (!image?.naturalWidth || !image.naturalHeight || !area(r)) continue;
      const sx = r.width / (el.offsetWidth || r.width), sy = r.height / (el.offsetHeight || r.height);
      const cw = r.width / sx, ch = r.height / sy, nw = image.naturalWidth, nh = image.naturalHeight;
      const size = cs.backgroundSize.split(/\s+/), dimension = (value, total) => value === 'auto' ? null : value.endsWith('%') ? parseFloat(value) * total / 100 : value.endsWith('px') ? parseFloat(value) : NaN;
      let w, h;
      if (size[0] === 'cover' || size[0] === 'contain') {
        const factor = size[0] === 'cover' ? Math.max(cw/nw, ch/nh) : Math.min(cw/nw, ch/nh);
        w = nw * factor; h = nh * factor;
      } else {
        w = dimension(size[0], cw); h = dimension(size[1] || 'auto', ch);
        if (w == null && h == null) { w = nw; h = nh; }
        else if (w == null) w = h * nw / nh;
        else if (h == null) h = w * nh / nw;
      }
      if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) continue;
      const position = cs.backgroundPosition.split(/\s+/), offset = (part, free, scale) => part?.endsWith('%') ? free * parseFloat(part) / 100 : (parseFloat(part) || 0) * scale;
      const painted = { x: r.x + offset(position[0], r.width-w*sx, sx), y: r.y + offset(position[1] || '50%', r.height-h*sy, sy), width: w*sx, height: h*sy };
      images.push({ el, painted, clip: r, mask: mask(image, match[2]), opacity: opacity(el), background: true });
    }
    const large = images.filter(image => area(intersection(intersection(image.clip, image.painted), canvasRect)) > W * H * .6 && image.opacity >= .95 && image.mask?.opaque >= .97);
    const headingAndBody = [...blocks.keys()].some(el => el.matches('h1,h2') || el.closest('h1,h2')) && [...blocks.keys()].some(el => !el.matches('h1,h2') && !el.closest('h1,h2'));
    if (large.length && blocks.size <= 2 && !headingAndBody) report('slide-is-image', 'error', large[0].el, '장 대부분이 불투명한 그림 한 장입니다. 글자와 요소를 편집할 수 있게 나누세요.');

    // 알파 지도의 각 칸을 실제 겹친 넓이만큼 가중한다.
    function opaqueOverlap(line, image) {
      if (!image.mask) return 0;
      const hit = intersection(intersection(line, image.clip), image.painted);
      if (!area(hit)) return 0;
      const p = image.painted, x0 = Math.max(0, Math.floor((hit.x - p.x) / p.width * MASK)), x1 = Math.min(MASK, Math.ceil((hit.x + hit.width - p.x) / p.width * MASK));
      const y0 = Math.max(0, Math.floor((hit.y - p.y) / p.height * MASK)), y1 = Math.min(MASK, Math.ceil((hit.y + hit.height - p.y) / p.height * MASK));
      let sum = 0;
      for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
        if (image.mask.alpha[y * MASK + x] * image.opacity < 128) continue;
        sum += area(intersection(hit, { x: p.x + x * p.width / MASK, y: p.y + y * p.height / MASK, width: p.width / MASK, height: p.height / MASK }));
      }
      return sum;
    }
    function mergeLines(lines) {
      const merged = [];
      for (const r of lines) {
        const line = merged.find(a => Math.min(a.y+a.height,r.y+r.height)-Math.max(a.y,r.y) >= Math.min(a.height,r.height)*.5);
        if (!line) { merged.push({ ...r }); continue; }
        const right = Math.max(line.x+line.width,r.x+r.width), bottom = Math.max(line.y+line.height,r.y+r.height);
        line.x = Math.min(line.x,r.x); line.y = Math.min(line.y,r.y); line.width = right-line.x; line.height = bottom-line.y;
      }
      return merged;
    }
    for (const [block] of blocks) {
      const runs = textRuns.filter(run => run.block === block);
      if (runs.filter(run => !run.el.closest('[data-bleed]')).flatMap(run => run.lines).some(r => r.x < 56 || r.y < 56 || r.x + r.width > W - 56 || r.y + r.height > H - 56)) report('edge-crowding', 'warn', block, '글자가 장의 가장자리에 너무 가깝습니다. 안쪽으로 옮기세요.');
      const unmarked = mergeLines(runs.filter(run => !run.el.closest('[data-over]')).flatMap(run => run.lines));
      const hit = images.some(image => !image.background && !large.includes(image) && !image.el.contains(block) && unmarked.some(line => opaqueOverlap(line, image) >= .15 * area(line)));
      if (hit) report('text-on-image', 'warn', block, '글자가 그림의 불투명한 부분과 겹칩니다. 위치를 옮기거나 의도한 겹침에 data-over를 쓰세요.');
    }

    if (!['cover', 'section', 'ask'].includes(slide.dataset.layout) && !slide.matches('.cover,.divider,.ask') && !slide.hasAttribute('data-roomy')) {
      const occupied = new Uint8Array(Math.ceil(W / CELL) * Math.ceil(H / CELL)), cols = Math.ceil(W / CELL), rows = Math.ceil(H / CELL);
      let bottom = false;
      function mark(r) {
        r = intersection(r, canvasRect); if (!area(r)) return;
        if (r.y + r.height > H * .7) bottom = true;
        for (let y = Math.max(0, Math.floor(r.y / CELL)); y < Math.min(rows, Math.ceil((r.y + r.height) / CELL)); y++) for (let x = Math.max(0, Math.floor(r.x / CELL)); x < Math.min(cols, Math.ceil((r.x + r.width) / CELL)); x++) occupied[y * cols + x] = 1;
      }
      for (const run of textRuns) if (!run.el.closest('[data-lock]')) for (const line of run.lines) mark(line);
      for (const el of elements) {
        if (!visible(el) || el.closest('[data-lock]') || el.closest('svg,math') && !el.matches('svg,math')) continue;
        let r = box(el); const cs = style(el);
        // SVG 캔버스가 크더라도 내부 도형은 배경이 아니라 내용이다.
        if (el.localName === 'svg') {
          try {
            const b = el.getBBox(), m = el.getScreenCTM();
            if (m && b.width && b.height) {
              const points = [[b.x,b.y],[b.x+b.width,b.y],[b.x,b.y+b.height],[b.x+b.width,b.y+b.height]].map(([x,y]) => new DOMPoint(x,y).matrixTransform(m));
              const left = Math.min(...points.map(p => p.x)), top = Math.min(...points.map(p => p.y));
              r = rect({ left, top, width: Math.max(...points.map(p => p.x))-left, height: Math.max(...points.map(p => p.y))-top });
            }
          } catch { /* 그릴 수 없는 SVG */ }
        }
        if (!area(r)) continue;
        const full = area(intersection(r, canvasRect)) >= W * H * .6;
        const simpleBackground = el.localName === 'svg' && el.children.length === 1 && el.firstElementChild.matches('rect');
        if (full && (el.localName !== 'svg' || simpleBackground)) continue;
        const painted = el.matches(visual) || cs.backgroundImage !== 'none' || !/^(?:rgba\(0, 0, 0, 0\)|transparent)$/.test(cs.backgroundColor) || ['Top','Right','Bottom','Left'].some(side => parseFloat(cs[`border${side}Width`]) > 0 && cs[`border${side}Style`] !== 'none');
        if (painted) mark(r);
      }
      const fraction = occupied.reduce((a, b) => a + b, 0) / occupied.length;
      if (fraction < .35 || !bottom) report('empty-space', 'warn', slide, `내용이 차지하는 영역이 작거나 아래가 비었습니다 (${Math.round(fraction * 100)}%). 내용 배치를 확인하세요.`);
    }

    for (const parent of [slide, ...elements]) {
      if (!visible(parent) || parent.closest('svg,math,[data-deck-rt]')) continue;
      const children = [...parent.children].filter(el => {
        if (!visible(el) || el.closest('[data-lock]') || /^(inline|contents)$/.test(style(el).display)) return false;
        const r = box(el); return r.width >= 24 && r.height >= 24;
      });
      let found = false;
      for (let a = 0; a < children.length && !found; a++) for (let b = a + 1; b < children.length && !found; b++) {
        const x = box(children[a]), y = box(children[b]);
        if (area(intersection(x, y))) continue;
        // 서로 다른 줄의 flex/grid 항목과 크기가 다른 의도적 배치는 건너뛴다.
        const row = Math.min(x.y + x.height, y.y + y.height) - Math.max(x.y, y.y) >= Math.min(x.height, y.height) * .5 && Math.abs(x.height - y.height) < 2;
        const col = Math.min(x.x + x.width, y.x + y.width) - Math.max(x.x, y.x) >= Math.min(x.width, y.width) * .5 && Math.abs(x.width - y.width) < 2;
        const gaps = row ? [Math.abs(x.y-y.y), Math.abs(x.y+x.height-y.y-y.height), Math.abs(x.y+x.height/2-y.y-y.height/2)] : col ? [Math.abs(x.x-y.x), Math.abs(x.x+x.width-y.x-y.width), Math.abs(x.x+x.width/2-y.x-y.width/2)] : [];
        if (gaps.length && Math.min(...gaps) >= 2 && Math.min(...gaps) <= 10) {
          report('near-miss', 'warn', children[b], '나란한 요소의 정렬이 조금 어긋났습니다. 같은 기준선에 맞추세요.'); found = true;
        }
      }
    }
    if (slide.dataset.layout !== 'section' && !slide.matches('.divider') && !elements.some(el => el.matches(visual) && visible(el))) report('no-visual', 'warn', slide, '그림·도형·위젯이 없습니다. 내용을 보여 줄 시각 요소를 더하세요.');

    if (fontFaceExists('Maplestory')) {
      const supplement = fontFaceExists('Deck Sans'), missing = new Set();
      for (const { node, el } of textRuns) {
        if (el.closest('math,svg,var') || /(?:cambria math|stix|latin modern math|\bmath\b)/i.test(style(el).fontFamily)) continue;
        for (const ch of node.data) if (!/[\s\p{M}\p{C}]/u.test(ch) && !fontHas('Maplestory', ch) && !(supplement && fontHas('Deck Sans', ch))) missing.add(ch);
      }
      if (missing.size) report('font-fallback', 'warn', slide, `덱 글꼴에 없는 글자 ${[...missing].join(' ')}가 다른 글꼴로 보입니다. 보충 글꼴을 넣거나 글자를 바꾸세요.`);
    }
    return results;
  }

  deck.ext('lint', { init: preloadBackgrounds, validate });
})();
