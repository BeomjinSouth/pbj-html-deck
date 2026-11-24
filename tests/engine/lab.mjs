import assert from 'node:assert/strict';
import { suite } from './pen.mjs';

const near = (a, b, epsilon = .015) => assert(Math.abs(a - b) <= epsilon, `${a} ≠ ${b} (±${epsilon})`);
const pieces = d => d.split('M').slice(1).map(line => line.split('L').map(pair => pair.split(' ').map(Number)));
const checkPoint = (runs, x, y) => {
  for (const run of runs) for (let i = 1; i < run.length; i++) {
    const [a, b] = [run[i - 1], run[i]];
    if (a[0] <= x && x <= b[0] && b[0] !== a[0]) { near(a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]), y, .06); return; }
  }
  assert.fail(`경로에 x=${x}가 없습니다.`);
};

await suite('lab', async ({ js, test, click, point, key, drag, view, shot, send }) => {
  const state = () => js('deck.lab.state("#lab")');
  const get = (selector, name) => js(`document.querySelector(${JSON.stringify(selector)}).getAttribute(${JSON.stringify(name)})`);
  const move = (page, mode = 'view') => js(`deck.setMode('${mode}'); deck.go(${page});`);
  const input = (id, value) => js(`{ const el=document.querySelector('#${id}'); el[el.type==='checkbox'?'checked':'value']=${JSON.stringify(value)}; el.dispatchEvent(new InputEvent('input',{bubbles:true})); }`);
  const graphAt = (x, y) => js(`{const p=new DOMPoint(${x},${y}).matrixTransform(document.querySelector('#parabola').getScreenCTM()); [p.x,p.y];}`);
  const canvasAt = (x, y) => js(`{const r=document.querySelector('#deck').getBoundingClientRect(); [r.x+r.width*${x}/1920,r.y+r.width*${y}/1920];}`);
  const path = async id => pieces(await get(id,'d'));
  await test('입력 네 종류, 조건, 서식, 세 가지 바인딩과 그리기 훅', async () => {
    await input('slider', -1.5); await input('number', 3); await click('#check'); await input('choice', 'q');
    assert.deepEqual(await state(), { a:-1.5, b:3, on:true, kind:'q', px:2, py:1, cx:200, cy:850 });
    assert.equal(await js('document.querySelector("#show").textContent'), '−1.5');
    assert.equal(await js('document.querySelector("#hook").textContent'), '−1.5 / -1.5');
    assert.deepEqual(await js(`{const el=document.querySelector('#bound'); [el.title,el.style.getPropertyValue('--amount'),el.textContent,document.querySelector('#tag').classList.contains('is-off')];}`), ['3','-1.5','Q',false]);
    assert.equal(await js('document.querySelector("#tag text").textContent'), 'Q');
    assert.equal(await get('#tag','transform'), 'translate(584 328)');
    await click('#set'); assert.equal((await state()).a, 2);
    await click('#add'); assert.equal((await state()).a, 2.5);
    await js('const copy=deck.lab.state("#lab"); copy.a=200;'); assert.equal((await state()).a, 2.5);
    await js('deck.lab.set("#lab",{a:1,on:false});');
    assert(await js('document.querySelector("#tag").classList.contains("is-off")'));
  });
  await test('이차함수 경로의 실제 좌표·잘림, 축·화살촉·눈금과 멱등', async () => {
    await js('deck.reset()');
    const one = await path('#quadratic');
    checkPoint(one, 584, 112); // (2,4): 80+7×72, 40+1×72
    near(one[0][0][0], 440 - 72 * Math.sqrt(5)); near(one[0][0][1], 40);
    assert(one.flat().every(([x,y]) => x >= 80 && x <= 800 && y >= 40 && y <= 760));
    assert.match(await get('#quadratic','clip-path'), /^url\(#deck-graph-clip-/);
    assert.equal((await js(`{const p=document.querySelector('#parabola path[data-fn]'); getComputedStyle(p).clipPath;}`)).replaceAll('"',''), await get('#quadratic','clip-path'));
    const axis = () => js(`{const p=document.querySelector('#parabola'); [p.firstElementChild.matches('.g-plane[data-runtime]'),...['.g-plane','.g-grid','.g-axis','.g-arrow','.g-tick','.g-number'].map(s=>p.querySelectorAll(s).length), ...['.g-axis-x','.g-axis-y'].map(s=>{const el=p.querySelector(s);return [Number(el.getAttribute('x1')),Number(el.getAttribute('y1'))]}),p.querySelector('.g-origin').textContent,[...p.querySelectorAll('.g-name')].map(e=>[e.textContent,getComputedStyle(e).fontStyle]), [...p.querySelectorAll('.g-number')].some(e=>e.textContent==='−3'), [...p.querySelectorAll('.g-number')].some(e=>e.textContent==='0')];}`);
    assert.deepEqual(await axis(), [true,1,22,2,2,20,20,[65.6,400],[440,774.4],'O',[['x','italic'],['y','italic']],true,false]);
    await js('deck.lab.set("#lab",{a:.5});');
    checkPoint(await path('#quadratic'), 584, 256); // (2,2)
    await js('deck.lab.set("#lab",{a:1}); deck.go(1); deck.go(0);');
    assert.equal(await js('document.querySelectorAll("#parabola .g-plane").length'), 1);
  });
  await test('반비례 양쪽 가지·비대칭 축척·정적 정의역·범위 밖 축', async () => {
    // Preserve the old asymmetric coordinates by opting out of equal units.
    await js(`{const s=document.querySelector('#reciprocal').closest('svg');
      s.dataset.graph=JSON.stringify({...JSON.parse(s.dataset.graph),equal:false});}`);
    await move(1); const reciprocal = await path('#reciprocal');
    assert.equal(reciprocal.length, 2);
    // 자동 여백(32px 글꼴): left 64, top 48, width 738, height 754.
    checkPoint(reciprocal, 580.6, 274.2); // (2,2)
    checkPoint(reciprocal, 285.4, 575.8); // (−2,−2)
    near(reciprocal[0].at(-1)[0], 373.96); near(reciprocal[0].at(-1)[1], 802); // (−0.8,−5)
    near(reciprocal[1][0][0], 492.04); near(reciprocal[1][0][1], 48); // (0.8,5)
    await move(2);
    assert.deepEqual((await path('#line1'))[0][0], [150,595]); // (−3,−2), 정의역 왼끝
    assert.deepEqual((await path('#line1'))[0].at(-1), [640,140]); // (4,5)
    checkPoint(await path('#line1'), 500, 270); checkPoint(await path('#line2'), 500, 270);
    assert.deepEqual([await get('#crossing','cx'),await get('#crossing','cy')], ['500','270']);
    await move(3);
    assert.deepEqual(await js(`{const s=document.querySelector('#off-axis'); ['.g-axis-x','.g-axis-y','.g-origin','.g-number-x','.g-number-y','.g-grid'].map(x=>s.querySelectorAll(x).length);}`), [1,0,0,10,6,17]);
    assert.equal(await js('document.querySelector("#off-axis .g-name").textContent'), 'u');
  });
  const fontProbe = selector => `(() => {const s=document.querySelector(${JSON.stringify(selector)});
    const zoom=s.closest('.slide').getBoundingClientRect().width/1920;
    return [...s.querySelectorAll('.g-number,.g-name,.g-origin')].filter(e=>e.checkVisibility({visibilityProperty:true}))
      .map(e=>{const m=e.getScreenCTM();return [e.textContent,parseFloat(getComputedStyle(e).fontSize)*Math.hypot(m.c,m.d)/zoom];});})()`;
  const graphFonts = selector => js(fontProbe(selector));
  const readable = fonts => {
    for (const label of ['x', 'y', 'O']) assert(fonts.some(([s])=>s===label), label);
    assert(fonts.filter(([s])=>/\d/.test(s)).length >= 2, JSON.stringify(fonts));
    for (const [label, size] of fonts) assert(size >= 32 && size <= 32.1, `${label}: ${size}px`);
  };
  const plot = (id, style, fn, points) => `<svg id="${id}" style="${style}" viewBox="0 0 940 760" data-graph='{"x":[-5,5],"y":[-2,8]}'><path data-fn="${fn}"/>${points.map(pt=>`<circle r="5" data-pt="${pt}"/>`).join('')}</svg>`;
  const smallPlot = plot('small-plot','left:90px;top:380px;width:300px;height:240px','x',['0,0','1,0','0,1']);
  const largePlot = plot('large-plot','left:850px;top:120px;width:940px;height:760px','2*x',['1,2']);
  await js(`deck.go(0); deck.slides[3].insertAdjacentHTML('beforeend', ${JSON.stringify(smallPlot + largePlot)});`);
  await test('box 없는 그래프에서 두 축의 한 칸이 화면에서 같은 길이', async () => {
    await move(3);
    const [dx, dy] = await js(`{const s=document.querySelector('#small-plot');
      const [o,x,y]=[...s.querySelectorAll('circle')].map(e=>new DOMPoint(e.cx.baseVal.value,e.cy.baseVal.value).matrixTransform(e.getScreenCTM()));
      [x.x-o.x,o.y-y.y];}`);
    assert(dx > 0); near(dx, dy);
  });
  await test('작은 칸과 큰 칸에서 배율과 관계없이 눈금 글자가 읽힘', async () => {
    await move(3);
    for (const [width, height] of [[1920, 1080], [800, 600]]) {
      await view(width, height);
      await js('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))', true);
      for (const selector of ['#small-plot', '#large-plot']) {
        const fonts = await graphFonts(selector);
        readable(fonts);
        console.log(`글자 실측 ${width}×${height} ${selector}: 최소 ${Math.min(...fonts.map(([, size])=>size)).toFixed(4)}px`);
      }
    }
    const changeHeight = height => js(`{document.querySelector('#small-plot').style.height='${height}px';
      new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));}`, true);
    try {
      await changeHeight(180);
      readable(await graphFonts('#small-plot'));
    } finally { await changeHeight(240); }
  });
  await test('좁은 칸에서는 모눈을 유지하며 눈금 숫자만 솎고 겹치지 않음', async () => {
    const { small, large, grids, overlap } = await js(`{const counts = id => {
      const s=document.querySelector(id);
      return [[...s.querySelectorAll('.g-number')].filter(e=>e.checkVisibility({visibilityProperty:true})),s.querySelectorAll('.g-grid').length]; };
      const [a,grids]=counts('#small-plot'),[b]=counts('#large-plot');
      const rects=a.map(e=>e.getBoundingClientRect());
      const overlap=rects.some((r,i)=>rects.slice(i+1).some(s=>r.left<s.right && s.left<r.right && r.top<s.bottom && s.top<r.bottom));
      ({small:a.length,large:b.length,grids,overlap});}`);
    assert.equal(grids,22); assert(small >= 2 && small < large); assert.equal(overlap,false);
  });
  await test('box·font·ticks 지정은 그대로 적용', async () => {
    await move(2);
    const actual = await js(`{const s=document.querySelector('#lines'),box=s.querySelector('.g-clip-box');
      [['x','y','width','height'].map(k=>Number(box.getAttribute(k))),
      [...new Set([...s.querySelectorAll('.g-number,.g-name,.g-origin')].map(e=>parseFloat(getComputedStyle(e).fontSize)))],
      [...s.querySelectorAll('.g-number-x')].filter(e=>e.checkVisibility({visibilityProperty:true})).map(e=>e.textContent)];}`);
    assert.deepEqual(actual, [[80,75,700,650], [34], ['−4','−2','2','4','6']]);
  });
  await test('열기 전의 그래프도 모아보기와 인쇄에 표시', async () => {
    await move(0);
    const id = await js(`{const s=deck.slides[3].querySelector('#small-plot').cloneNode(true);
      s.id='unvisited-plot';s.querySelector('.g-plane')?.remove();deck.slides[1].append(s);s.id;}`);
    try {
      await js('deck.overview(true)');
      const overviewFonts = await graphFonts(`#deck-overview #${id}`);
      readable(overviewFonts);
      console.log(`글자 실측 모아보기: 최소 ${Math.min(...overviewFonts.map(([, size])=>size)).toFixed(4)}px`);
      await js(`deck.overview(false); window.printGraphFonts=null;
        window.addEventListener('beforeprint',()=>{window.printGraphFonts=${fontProbe(`#${id}`)};},{once:true});`);
      const pdf = await send('Page.printToPDF', { preferCSSPageSize:true });
      assert(pdf.data.startsWith('JVBER'));
      readable(await js('window.printGraphFonts'));
      assert(await js(`document.querySelector('#${id}').closest('.slide').hidden`));
      await send('Emulation.setEmulatedMedia', {media:'print'});
      await js('window.dispatchEvent(new Event("beforeprint"))');
      readable(await graphFonts(`#${id}`));
    } finally {
      await js(`deck.overview(false); window.dispatchEvent(new Event('afterprint'));document.querySelector('#${id}').remove();`);
      await send('Emulation.setEmulatedMedia', {media:'screen'});
      await js(`for (const id of ['small-plot','large-plot']) document.getElementById(id)?.remove();`);
    }
  });
  await test('800px에서 마우스·터치의 스냅, 범위, 축과 캔버스 좌표', async () => {
    await view(800, 620); await move(0);
    await drag([await graphAt(584,328), await graphAt(621,215)]); // +0.51, y 변화는 무시
    assert.deepEqual([(await state()).px,(await state()).py],[2.5,1]);
    assert.deepEqual([await get('#dot','cx'),await get('#dot','cy')], ['620','328']);
    assert.equal(await get('#segment','x2'), '620');
    await drag([await graphAt(620,328), await graphAt(440,328), await graphAt(161,328)], { touch:true });
    assert.equal((await state()).px, -3); assert.equal(await js('deck.page'), 0);
    assert.equal(await js('document.querySelector("#dot").classList.contains("is-drag")'), false);
    await drag([await point('#free'), await canvasAt(387,917)]);
    assert.deepEqual([(await state()).cx,(await state()).cy], [340,890]);
  });
  await test('장 이동·data-keep·R·편집과 깨끗한 저장', async () => {
    await move(3); await click('#keep-add'); await move(0); await move(3);
    assert.equal(await js('deck.lab.state("#keep").n'),2);
    await key('r'); assert.equal(await js('deck.lab.state("#keep").n'),1);
    await click('#keep-add'); await move(0);
    assert.equal((await state()).px,2);
    await js('deck.lab.set("#lab",{a:-2,on:true,kind:"q"})');
    const saved = await js(`{const d=new DOMParser().parseFromString(deck.serialize(),'text/html');
      [d.querySelector('#show').textContent,d.querySelector('#bound').textContent,d.querySelector('#bound').getAttribute('style'),d.querySelector('#quadratic').getAttribute('d'),!!d.querySelector('.g-plane,[data-runtime],.is-off,#hook')];}`);
    assert.deepEqual(saved, ['1.0','P','--amount:1',null,false]);
    assert.equal(await js('deck.lab.state("#keep").n'),1);
    await js('deck.lab.set("#lab",{a:-2,px:3})'); await key('e'); assert.equal((await state()).a,1);
    await click('#set'); assert.equal(await js('deck.selection?.id'), 'set'); assert.equal((await state()).a,1);
    await view(1600, 950); // Keep the editor's selection handles clear of the button centre.
    await click('#set', 2); assert.equal((await state()).a,2);
    await drag([await graphAt(584,328), await graphAt(620,328)], { modifiers:2 });
    assert.equal((await state()).px,2.5);
    await key('e'); assert.equal((await state()).px,2);
  });
  await test('validate의 네 오류와 selftest 성공·실패·hold', async () => {
    await view(1600, 950); await move(0);
    const markup = `<h2>검사 대상</h2><div data-lab='{"a":1}'><input id="bad-var" data-var="missing" type="number"><output id="bad-exp" data-show="a+*2"></output></div><svg id="bad-graph" data-graph='{"x":[2,1],"y":[0,2]}'></svg><svg><path id="bad-fn" data-fn="x"/></svg>`;
    const targets = await js(`{const source=deck.slides[0], el=document.createElement('section'); el.className='slide'; el.dataset.slide='';
      el.innerHTML=${JSON.stringify(markup)}; source.parentElement.append(el);
      const out=deck.audit(deck.count-1).filter(x=>x.code==='widget-invalid'); el.remove(); out.map(x=>[x.target,x.level]).sort();}`);
    assert.deepEqual(targets,[['#bad-exp','error'],['#bad-fn','error'],['#bad-graph','error'],['#bad-var','error']]);
    assert.deepEqual(await js('deck.selftest(0,{hold:true})',true),[]);
    assert.equal((await state()).kind,'q'); assert.notEqual((await state()).px,2);
    assert.deepEqual(await js('deck.selftest(0)',true),[]); assert.equal((await state()).px,2);
    const broken = await js(`(async()=>{ const el=document.querySelector('#number'); el.dataset.var='cx';
      try { const off=document.querySelector('#free'); off.setAttribute('data-bind','--left:200; --top:850');
        return (await deck.selftest(0)).filter(x=>x.code==='widget-broken').map(x=>x.target);
      } finally { el.dataset.var='b';document.querySelector('#free').setAttribute('data-bind',"--left: cx + 'px'; --top: cy + 'px'");deck.reset(); }})()`,true);
    assert(broken.includes('#number'));
  });
  await test('발표 화면 세 장을 1920×1080으로 캡처', async () => {
    await view(1920, 1080); await move(0, 'present');
    await input('slider', 0.5);
    await js(`{deck.lab.set('#lab',{px:2,py:2}); for (const el of document.querySelectorAll('#lab .samples')) el.hidden=true;}`);
    await shot('01-quadratic.png');
    await move(1, 'present'); await shot('02-reciprocal.png');
    await move(2, 'present'); await shot('03-intersection.png');
  });
});
