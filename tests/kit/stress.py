"""부품 한도 시험기 — 등록부(layouts.json)의 글자 한도까지 채워도 넘치지 않는지 본다.

구도마다 장을 만든다.
  full  모든 글자 필드를 한도까지(여러 줄 칸은 줄마다 가득), 목록은 최대 개수, 선택 필드 모두 채움
  wrap  (여러 줄 칸이 있는 구도만) 여러 줄 칸을 줄바꿈 없는 한 문장으로 한도까지 — 브라우저가 접는다
  min   목록은 최소 개수, 선택 필드 비움, 글자는 짧게
  math  글자 필드를 수식으로 한도까지 채움
  frac  분수와 첨자가 든 수식으로 한도까지 채움
그 콘티로 plan → scaffold → build → check --shots를 돌리고, 검사 결과에 더해
뒤집은 면·풀이·말풍선·다른 탭처럼 처음에는 숨은 상태도 직접 열어 같은 검사를 한다.

    python -X utf8 tests/kit/stress.py --out <임시폴더> [--palette sky] [--dark] [--only cards,quiz]

종료 코드: 문제 0이면 0, 문제가 있으면 1, 도구 실패는 2.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
import deck_plan  # noqa: E402  (글자 수를 도구와 똑같이 센다)
from cdp import Browser  # noqa: E402

PICTURES = Path(os.environ.get("KIT_STRESS_PICTURES", SKILL / "examples" / "부품-견본" / "그림"))
# 표로 보일 검사 코드(오류는 모두 보인다). spill·under-32는 이 시험기가 따로 재는 것.
CODES = {"text-clipped", "overflow-canvas", "small-text", "edge-crowding", "near-miss", "text-on-image",
         "widget-broken", "text-overlap", "math-overflow"}

# 실제 수업 문장을 이어 붙인 말뭉치. 칸마다 다른 곳에서 잘라 쓴다(같은 글자 반복은 줄바꿈이 비현실적이다).
CORPUS = ("삼각형의 세 변의 길이를 견주어 봅니다 가장 긴 변은 나머지 두 변의 합보다 짧아야 합니다 "
          "막대 세 개로 직접 만들어 보고 끝이 만나는지 확인해 봅시다 짝과 함께 까닭을 이야기해 보세요 "
          "기울기가 커지면 그래프는 더 가파르게 올라갑니다 넓이를 구하는 방법을 서로 비교해 봅시다 "
          "분모를 같게 만든 뒤 분자끼리 더합니다 모르는 수를 문자로 놓고 방정식을 세워 봅니다 "
          "각의 크기를 재어 표에 적고 규칙을 찾아봅시다 원의 둘레는 지름에 원주율을 곱한 값입니다 "
          "자료를 모아 막대그래프로 나타내고 가장 많은 것을 찾습니다 두 직선이 만나는 점의 좌표를 읽어 봅니다")
WORDS = CORPUS.split()

ROD = ('<svg viewBox="0 0 360 240" role="img"><line class="rod-edge" x1="30" y1="200" x2="150.8" y2="111.1"/>'
       '<line class="rod c-coral" x1="30" y1="200" x2="150.8" y2="111.1"/><line class="rod-edge" x1="330" y1="200" x2="150.8" y2="111.1"/>'
       '<line class="rod c-sky" x1="330" y1="200" x2="150.8" y2="111.1"/><line class="rod-edge" x1="30" y1="200" x2="330" y2="200"/>'
       '<line class="rod c-yellow" x1="30" y1="200" x2="330" y2="200"/></svg>')
STAGE = ('<svg viewBox="0 0 900 640" role="img" aria-label="길이를 바꿀 수 있는 막대 세 개">'
         '<line class="rod-edge" x1="90" y1="520" x2="570" y2="520" data-bind="x2:90+80*a"/>'
         '<line class="rod c-yellow" x1="90" y1="520" x2="570" y2="520" data-bind="x2:90+80*a"/>'
         '<g transform="rotate(-36.34 90 476)" data-bind="transform:\'rotate(\'+(a<7?-atan2(sqrt(max(0,9-pow((a*a-7)/(2*a),2))),(a*a-7)/(2*a))*180/PI:0)+\' 90 476)\'">'
         '<line class="rod-edge" x1="90" y1="476" x2="330" y2="476"/><line class="rod c-sky" x1="90" y1="476" x2="330" y2="476"/></g>'
         '<g transform="translate(480 0)" data-bind="transform:\'translate(\'+(80*a)+\' 0)\'">'
         '<g transform="rotate(26.38 90 476)" data-bind="transform:\'rotate(\'+(a<7?atan2(sqrt(max(0,9-pow((a*a-7)/(2*a),2))),a-(a*a-7)/(2*a))*180/PI:0)+\' 90 476)\'">'
         '<line class="rod-edge" x1="90" y1="476" x2="-230" y2="476"/><line class="rod c-coral" x1="90" y1="476" x2="-230" y2="476"/></g></g>'
         '<text class="lb ko" x="330" y="592" data-bind="x:90+40*a"><tspan data-show="a">6</tspan> cm</text></svg>')



def rods(w, h):
    """가로·세로 비가 다른 직접 그린 그림(draw): 칸이 viewBox 비율과 상관없이 그림을 담는지 본다."""
    m = 30
    pts = [(m, h - m), (w - m, h - m), (w * .45, m)]
    lines = "".join(f'<line class="rod-edge" x1="{a[0]:g}" y1="{a[1]:g}" x2="{b[0]:g}" y2="{b[1]:g}"/>'
                    f'<line class="rod {c}" x1="{a[0]:g}" y1="{a[1]:g}" x2="{b[0]:g}" y2="{b[1]:g}"/>'
                    for (a, b), c in zip([(pts[0], pts[2]), (pts[1], pts[2]), (pts[0], pts[1])], ("c-coral", "c-sky", "c-yellow")))
    return f'<svg viewBox="0 0 {w} {h}" role="img">{lines}</svg>'


# 그림 칸 시험 변형: 모든 그림 칸을 이 비율의 draw로 채운다
SHAPES = {"wide": rods(480, 200), "sq": rods(300, 300), "tall": rods(200, 420)}

# raw·json 필드와 뜻이 있는 값의 기본값: (구도, 필드) → {변형: 값}. "*"는 모든 변형.
DEFAULTS = {
    ("lab", "state"): {"full": {"a": 6, "b": 3, "c": 4}, "wrap": {"a": 6, "b": 3, "c": 4}, "min": {"a": 6}},
    ("lab", "stage"): {"*": STAGE},
    ("graph", "state"): {"full": {"a": 2, "b": 1, "c": 0, "p": 2, "q": 4}, "min": {"a": 1}},
    ("graph", "graph"): {"*": {"x": [-5, 5], "y": [-2, 8]}},
    ("input", "answer"): {"*": "12|12cm"},
    ("input", "tol"): {"full": 0.5},
    ("pick", "range"): {"full": "1-30", "min": "1-28"},
}
LAB_CONTROLS = [dict(var="a", min=5, max=9, step=0.5, value=6), dict(var="b", min=1, max=6, step=1, value=3),
                dict(var="c", min=1, max=6, step=1, value=4)]
GRAPH_CONTROLS = [dict(var="a", min=0.5, max=4, step=0.5, value=2), dict(var="b", min=-2, max=4, step=1, value=1),
                  dict(var="c", min=-3, max=3, step=1, value=0)]
LAB_STATUS = [("a < 7", "yes"), ("a == 7", ""), ("a > 7 && a < 8", "no"), ("a >= 8", "no")]
GRAPH_CURVES = [dict(fn="x*x", tone="ink", dashed=True), dict(fn="a*x*x", tone=""), dict(fn="a*x*x+b", tone="sky"),
                dict(fn="x+c", tone="mint")]
GRAPH_POINTS = [dict(at="1, a", tone="sky"), dict(at="-1, a", tone="mint"), dict(at="0, b", tone="coral"),
                dict(at="p, q", tone="", drag="p,q", snap=0.5)]

PROBE_STATES = """
(index, state) => {
  const slide = document.querySelectorAll('#deck > section.slide[data-slide]')[index];
  if (state === 'list') {
    const names = [];
    if (slide.querySelector('[data-flip],[data-explain],.blank,.flap')) names.push('열림');
    if (slide.querySelector('[data-buddy]')) names.push('맞음', '틀림');
    if (slide.querySelector('[data-chip]')) names.push('채움');
    const tabs = [...slide.querySelectorAll('[data-tab]')].map(t => t.getAttribute('data-tab'));
    for (const k of tabs.slice(1)) names.push('탭:' + k);
    return names;
  }
  if (state === '열림' || state === '맞음' || state === '틀림') {
    slide.querySelectorAll('[data-flip]').forEach(e => e.classList.add('is-flipped'));
    slide.querySelectorAll('[data-explain],.blank,.flap').forEach(e => e.classList.add('is-open'));
  }
  if (state === '맞음' || state === '틀림')
    slide.querySelectorAll('[data-buddy]').forEach(e => e.setAttribute('data-mood', state === '맞음' ? 'correct' : 'wrong'));
  if (state === '채움') {      // 조각을 모두 제자리에 놓은 화면(끝까지 푼 모습)
    const root = slide.querySelector('[data-sort]');
    const lane = root.querySelector('[data-lane]');
    const chips = [...root.querySelectorAll('[data-chip]')];
    chips.sort((a, b) => (+a.dataset.rank || 0) - (+b.dataset.rank || 0));
    for (const chip of chips) {
      const bin = chip.dataset.to && root.querySelector(`[data-bin="${chip.dataset.to}"]`);
      (bin || lane || chip.parentElement).append(chip);
    }
  }
  if (state.startsWith('탭:')) {
    const k = state.slice(2);
    slide.querySelectorAll('[data-tab]').forEach(t => t.classList.toggle('is-on', t.getAttribute('data-tab') === k));
    slide.querySelectorAll('[data-pane]').forEach(p => p.classList.toggle('is-off', p.getAttribute('data-pane') !== k));
  }
  return true;
}
"""
# 글자가 자기를 담은 면(테두리나 바탕색이 있는 가장 가까운 블록 상자) 밖으로 나가는지, 32px보다 작은지 잰다.
PROBE_SPILL = r"""
(index) => {
  const slide = document.querySelectorAll('#deck > section.slide[data-slide]')[index];
  const o = slide.getBoundingClientRect(), z = o.width / 1920, out = [];
  const path = el => { const p = []; for (let e = el; e && e !== slide; e = e.parentElement) {
      p.unshift(e.tagName.toLowerCase() + [...e.classList].filter(c => !c.startsWith('is-')).map(c => '.' + c).join(''));
      if (p.length > 3) break; } return p.join('>'); };
  const boxy = el => { const cs = getComputedStyle(el);
    if (/^inline$|^contents$/.test(cs.display)) return false;
    return (parseFloat(cs.borderTopWidth) >= 2 && cs.borderTopStyle !== 'none') ||
           !/rgba\(0, 0, 0, 0\)|transparent/.test(cs.backgroundColor); };
  const seen = new Set();
  const walker = document.createTreeWalker(slide, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.data.trim() || el.closest('svg,math,[data-deck-rt],[data-runtime]')) continue;
    if (!el.checkVisibility({visibilityProperty: true, opacityProperty: true})) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const lines = [...range.getClientRects()].filter(r => r.width > 1 && r.height > 1);
    if (!lines.length) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    if (fs < 31.5 && !seen.has('f' + path(el))) { seen.add('f' + path(el));
      out.push({code: 'under-32', target: path(el), message: `글자 ${fs.toFixed(1)}px`}); }
    let box = el; while (box && box !== slide && !boxy(box)) box = box.parentElement;
    if (!box || box === slide) continue;
    const b = box.getBoundingClientRect();
    const over = Math.max(...lines.map(r => Math.max(b.left - r.left, r.right - b.right, b.top - r.top, r.bottom - b.bottom))) / z;
    if (over > 2 && !seen.has('s' + path(box))) { seen.add('s' + path(box));
      out.push({code: 'spill', target: path(box), message: `글자가 면 밖으로 ${over.toFixed(0)}px: ${node.data.trim().slice(0, 14)}`}); }
  }
  // 수식은 개별 MathML 텍스트 노드가 아니라 수식 전체 상자로 잰다.
  for (const math of slide.querySelectorAll('math')) {
    if (!math.checkVisibility({visibilityProperty: true, opacityProperty: true})) continue;
    let box = math.parentElement;
    while (box && box !== slide && !boxy(box)) box = box.parentElement;
    const r = math.getBoundingClientRect();
    if (box && box !== slide) {
      const b = box.getBoundingClientRect();
      const over = Math.max(b.left - r.left, r.right - b.right, b.top - r.top, r.bottom - b.bottom) / z;
      if (over > 2 && !seen.has('m' + path(math))) { seen.add('m' + path(math));
        out.push({code: 'math-spill', target: path(math), message: `수식이 면 밖으로 ${over.toFixed(0)}px`}); }
    }
    for (const image of slide.querySelectorAll('img.asset')) {
      if (!image.checkVisibility({visibilityProperty: true, opacityProperty: true})) continue;
      const p = image.getBoundingClientRect();
      const w = (Math.min(r.right, p.right) - Math.max(r.left, p.left)) / z;
      const h = (Math.min(r.bottom, p.bottom) - Math.max(r.top, p.top)) / z;
      if (w > 2 && h > 2 && !seen.has('mi' + path(math))) { seen.add('mi' + path(math));
        out.push({code: 'math-on-image', target: path(math), message: `수식이 그림과 ${w.toFixed(0)}×${h.toFixed(0)}px 겹칩니다`}); }
    }
  }
  for (const pic of slide.querySelectorAll('svg, img.asset')) {
    if (pic.closest('[data-runtime],[data-deck-rt]') || !pic.parentElement.closest('.pic,.shot,.frame,.stage')) continue;
    if (!pic.checkVisibility({visibilityProperty: true, opacityProperty: true})) continue;
    const face = pic.closest('.card,.face,.pane'), r = pic.getBoundingClientRect();
    if (!face || r.width < 2) continue;
    const b = face.getBoundingClientRect();
    const over = Math.max(b.left - r.left, r.right - b.right, b.top - r.top, r.bottom - b.bottom) / z;
    if (over > 2 && !seen.has('p' + path(face))) { seen.add('p' + path(face));
      out.push({code: 'pic-spill', target: path(pic), message: `그림이 면 밖으로 ${over.toFixed(0)}px`}); }
  }
  return out;
}
"""


def width(text):
    return sum(deck_plan.text_widths(text))


def fill(target, seed):
    """띄어쓰기를 섞은 실제 낱말로 한도에 정확히 맞춘다."""
    for shift in range(len(WORDS)):
        text = cut(target, seed + shift)
        if abs(width(text) - target) < 1e-8 and not text.endswith(" "):
            return text
    return cut(target, seed, pad=True)


def cut(target, seed, pad=False):
    start = seed % len(WORDS)
    stream = " ".join((WORDS[start:] + WORDS[:start]) * 3)
    out, used = "", 0.0
    for c in stream:
        if not out and c == " ":
            continue
        w = deck_plan.text_char_width(c)
        if used + w > target + 1e-8:
            break
        out, used = out + c, used + w
    if not pad:
        return out
    out = out.rstrip()
    while width(out) + 1 <= target + 1e-8:
        out += "다"
    if abs(target - width(out) - .5) < 1e-8:
        out = out[:-1] + " " + out[-1:]
    assert abs(width(out) - target) < 1e-8, (out, target)
    return out


def fill_math(target, structured=False):
    """수식 자체를 한도 가까이 채운다. 첨자·분수도 별도 변형에서 시험한다."""
    expression = r"\frac{1}{2}x^2" if structured and target >= 3 else "x"
    for term in ("+2x", "x", "i"):
        while width("$" + expression + term + "$") <= target + 1e-8:
            expression += term
    return "$" + expression + "$"


def emphasize(text):
    """제목처럼 첫 낱말에 강조 표시를 둔다(표시 기호는 세지 않는다)."""
    head, sep, rest = text.partition(" ")
    return f"**{head}**{sep}{rest}" if len(head) > 1 else text


class Maker:
    def __init__(self, layout_name, layout, variant):
        self.name, self.layout, self.variant = layout_name, layout, variant
        self.seed = sum(map(ord, layout_name)) % 97

    def text(self, spec, key):
        self.seed += 7
        if self.variant == "min":
            return fill(3 if key == "title" else 2, self.seed)
        cols, rows = spec.get("max") or [8, 1]
        if self.variant in ("math", "frac"):
            return "\n".join(fill_math(cols, self.variant == "frac") for _ in range(rows))
        if rows > 1 and self.variant in ("full",) + tuple(SHAPES):
            value = "\n".join(fill(cols, self.seed + 3 * i) for i in range(rows))
        else:
            value = fill(cols * rows, self.seed)
        return emphasize(value) if key == "title" else value

    def field(self, spec, key, path):
        default = DEFAULTS.get((self.name, path))
        if default is not None:
            return default.get(self.variant, default.get("*", default.get("full")))
        kind = spec.get("type", "text")
        required = spec.get("required")
        if self.variant == "min" and not required and kind != "list":
            return None
        if kind == "text":
            return self.text(spec, key)
        if kind == "list":
            count = spec.get("max", 4) if self.variant != "min" else spec.get("min", 0)
            of = spec.get("of", "text")
            items = []
            for i in range(count):
                if of == "text":
                    items.append(self.text({"max": [8, 1]}, key))
                elif isinstance(of, dict) and isinstance(of.get("type"), str):
                    items.append(self.field(of, key, path + "[]"))
                else:
                    items.append(self.obj(of, path + "[]", i))
            return items if items or required else None
        if kind == "object":
            return self.obj(spec.get("of", {}), path, 0)
        if kind == "bool":
            return True
        if kind == "number":
            if key == "timer":
                return 3599 if self.variant != "min" else 60
            return spec.get("min", 1)
        if kind == "choice":
            options = [o for o in spec.get("options", []) if o]
            return options[0] if options and self.variant != "min" else ""
        if kind == "asset":
            return "sticks"
        if kind == "raw":
            return SHAPES.get(self.variant, ROD) if spec.get("visual") else "a"
        return None

    def obj(self, fields, path, index):
        out = {}
        for key, spec in fields.items():
            value = self.field(spec, key, f"{path}.{key}")
            if value is not None:
                out[key] = value
        if "image" in out and "draw" in out:      # 그림 칸은 그림과 직접 그린 도형 가운데 하나만(번갈아)
            out.pop("image" if self.variant in SHAPES else "draw" if index % 2 == 0 else "image")
        return out

    def slide(self):
        fields = self.layout["fields"]
        slide = {"id": f"{self.variant}-{self.name}", "layout": self.name,
                 "purpose": "한도 시험에서 글이 넘치지 않는지 본다", "focus": "가장 긴 글"}
        for key, spec in fields.items():
            value = self.field(spec, key, key)
            if value is not None:
                slide[key] = value
        if "image" in slide and "draw" in slide:
            slide.pop("image" if self.name == "example" or self.variant in SHAPES else "draw")
        buddy = self.layout.get("buddy")
        if self.variant != "min":
            if "tag" not in fields:
                slide["tag"] = self.text({"max": [8, 1]}, "tag")
            if buddy:
                slide["mascot"] = "mascot_sticks" if self.name in ("cover", "activity") else "mascot"
            if buddy == "say" and "say" not in fields:
                slide["say"] = [self.text({"max": [18, 1]}, "say") for _ in range(2)]
        else:
            slide.pop("tag", None)
            slide.pop("say", None)
        self.fix(slide)
        if self.variant == "min" and self.layout.get("visual") and not has_visual(slide, fields):
            give_visual(slide, fields, buddy)
        return slide

    def fix(self, s):
        """뜻이 맞아야 하는 값(정답 하나, 칸 이름, 조작 변수 등)을 맞춘다."""
        full = self.variant != "min"
        n = self.name
        if n == "agenda":
            for i, item in enumerate(s["items"]):
                item.pop("now", None)
                if full and i == 1:
                    item["now"] = True
        if n in ("quiz", "quiz-long"):
            for i, opt in enumerate(s["options"]):
                opt.pop("correct", None)
                if i == 1:
                    opt["correct"] = True
        if n == "ox":
            s["truth"] = full
        if n == "sort":
            keys = "abc"[:len(s["bins"])]
            for i, b in enumerate(s["bins"]):
                b["key"] = keys[i]
            for i, chip in enumerate(s["chips"]):     # 한 칸에 몰리는 경우까지: 앞의 다섯은 첫 칸
                chip["to"] = keys[0] if full and i < 5 else keys[i % len(keys)]
        if n == "table":
            cols = len(s["head"])
            for i, row in enumerate(s["rows"]):
                row["cells"] = row["cells"][:cols]
                while len(row["cells"]) < cols:
                    row["cells"].append(self.text({"max": [7, 1]}, "cell"))
                row.pop("hl", None)
                if full and i == 0:
                    row["hl"] = True
        if n == "lab":
            for ctl, base in zip(s["controls"], LAB_CONTROLS):
                ctl.update(base)
                if not full:
                    ctl.pop("step", None)
            for st, (test, tone) in zip(s.get("status", []), LAB_STATUS):
                st["if"], st["tone"] = test, tone
        if n == "graph" and full:
            for ctl, base in zip(s["controls"], GRAPH_CONTROLS):
                ctl.update(base)
            s["curves"] = [{k: v for k, v in c.items() if v is not False} for c in GRAPH_CURVES[:len(s["curves"])]]
            s["points"] = [dict(p) for p in GRAPH_POINTS[:len(s["points"])]]
            if "formula" in s:
                s["formula"] = fill_math(self.layout["fields"]["formula"]["max"][0], self.variant == "frac")
        if n == "write" and full:
            s["frame"] = blanks(s["frame"])     # 공책 글에는 눌러 여는 빈칸이 들어간다(빈칸은 글자보다 넓다)


def blanks(text):
    lines = text.split("\n")
    for j in sorted({0, len(lines) - 1}):
        words = lines[j].split(" ")
        k = len(words) // 2
        words[k] = f"[[{words[k]}]]"
        lines[j] = " ".join(words)
    return "\n".join(lines)


def has_visual(slide, fields):
    for key, spec in fields.items():
        kind = spec.get("type")
        if kind == "asset" and slide.get(key):
            return True
        if kind == "raw" and spec.get("visual") and slide.get(key):
            return True
        of = spec.get("of")
        if kind in ("list", "object") and isinstance(of, dict) and not isinstance(of.get("type"), str):
            items = slide.get(key) or []
            for item in (items if isinstance(items, list) else [items]):
                if isinstance(item, dict) and has_visual(item, of):
                    return True
    return bool(slide.get("mascot"))


def give_visual(slide, fields, buddy):
    """그림이 필수인 구도의 최소 장: 마스코트 자리가 있으면 마스코트, 없으면 첫 그림 칸 하나만 채운다."""
    if buddy:
        slide["mascot"] = "mascot"
        return
    for key, spec in fields.items():
        if spec.get("type") == "asset":
            slide[key] = "sticks"
            return
        of = spec.get("of")
        if isinstance(of, dict) and "image" in of and slide.get(key):
            first = slide[key][0] if isinstance(slide[key], list) else slide[key]
            first["image"] = "sticks"
            return


def has_draw(fields):
    for key, spec in fields.items():
        if spec.get("type") == "raw" and spec.get("visual") and key == "draw":
            return True
        of = spec.get("of")
        if isinstance(of, dict) and not isinstance(of.get("type"), str) and has_draw(of):
            return True
    return False


def multi_line(fields):
    for spec in fields.values():
        if spec.get("type") == "text" and (spec.get("max") or [0, 1])[1] > 1:
            return True
        of = spec.get("of")
        if isinstance(of, dict):
            if isinstance(of.get("type"), str):
                if of["type"] == "text" and (of.get("max") or [0, 1])[1] > 1:
                    return True
            elif multi_line(of):
                return True
    return False


def make_plan(layouts, palette, only):
    names = [n for n in layouts if not only or n in only]
    order = [n for n in names if n == "cover"] + [n for n in names if n not in ("cover", "closing")] + \
            [n for n in names if n == "closing"]
    slides = []
    for name in order:
        variants = ["min", "full", "math", "frac"] + (["wrap"] if multi_line(layouts[name]["fields"]) else [])
        if has_draw(layouts[name]["fields"]):
            variants += list(SHAPES)
        if name == "cover":
            variants = ["full"] + [v for v in variants if v != "full"]
        for variant in variants:
            slides.append(Maker(name, layouts[name], variant).slide())
    if slides[0]["layout"] != "cover":
        slides.insert(0, Maker("cover", layouts["cover"], "min").slide())
    if layouts.get(slides[-1]["layout"], {}).get("group") != "wrap":
        slides.append(Maker("closing", layouts["closing"], "min").slide())
    if only and len(slides) >= deck_plan.VISUAL_LIMITS['min_slides']:
        # 일부 구도만 시험해도 실제 덱의 그림 관문을 통과하도록 보조 장을 둔다.
        support = Maker('show', layouts['show'], 'min').slide()
        support['id'] = 'support-show'
        anchors = []
        for pose in ('mascot_think', 'mascot_cheer'):
            anchor = Maker('point', layouts['point'], 'min').slide()
            anchor.update(id='support-' + pose.replace('_', '-'), mascot=pose)
            anchors.append(anchor)
        slides[-1:-1] = [support, *anchors]
    def weave(items):
        # 최소 변형의 선택 필드를 채우지 않고 그림 있는 변형을 사이사이에 배치한다.
        middle = items[1:-1]
        pictured = [s for s in middle if has_visual(s, layouts[s["layout"]]["fields"])]
        plain = [s for s in middle if not has_visual(s, layouts[s["layout"]]["fields"])]
        mixed = []
        while pictured:
            mixed.append(pictured.pop(0))
            mixed.extend(plain[:3])
            del plain[:3]
        return items[:1] + mixed + plain + items[-1:]

    slides = weave(slides)
    # 한도 시험의 그림 수와 범위를 지키면서 실제로 다른 포즈도 사용한다.
    candidates = [s for s in slides[1:-1] if s.get("mascot") and not s['id'].startswith('support-')]
    for slide, pose in zip(candidates, ("mascot_think", "mascot_cheer")):
        slide["mascot"] = pose
    plan = {
        "meta": {"title": "부품 한도 시험", "kind": "lesson", "audience": "중학교 1학년 · 수학", "duration": "45분",
                 "goal": "한도까지 채운 글이 넘치지 않는다", "message": "한도 안의 글은 언제나 들어간다", "palette": palette},
        "mascot": {"id": "mascot", "name": "세모", "look": "a cute chubby orange fox cub with a teal scarf",
                   "yes": "mascot_sticks", "no": "mascot_sticks"},
        "slides": slides,
        "assets": [{"id": "mascot_sticks", "kind": "mascot", "prompt": "holding three colorful sticks", "alt": "막대를 든 세모"},
                   {"id": "mascot_think", "kind": "mascot", "prompt": "thinking", "alt": "생각하는 세모"},
                   {"id": "mascot_cheer", "kind": "mascot", "prompt": "cheering", "alt": "기뻐하는 세모"},
                   {"id": "sticks", "kind": "sticker", "prompt": "three colorful sticks", "alt": "막대 세 개"}],
    }
    if only and len(slides) >= deck_plan.VISUAL_LIMITS['min_slides']:
        visual_errors = {'mascot-count', 'mascot-coverage', 'non-mascot-visual', 'image-coverage', 'no-visual-repeat'}
        for index in range(len(slides) + 1):
            problems = deck_plan.validate_plan(plan, layouts)[1]
            if not any(p['level'] == 'error' and p['code'] in visual_errors for p in problems):
                break
            anchor = Maker('point', layouts['point'], 'min').slide()
            anchor.update(id=f'support-extra-{index}', mascot=('mascot', 'mascot_think', 'mascot_cheer')[index % 3])
            plan['slides'].insert(-1, anchor)
            plan['slides'] = weave(plan['slides'])
        else:
            raise ValueError('부분 한도 시험의 그림 기준을 맞추지 못했습니다')
    return plan


def run(step, folder, *extra, retries=3):
    """deck.py를 부른다. 다른 작업자가 도구를 고치는 중이라 실패하면 30초 뒤 다시."""
    command = [sys.executable, "-X", "utf8", str(SKILL / "scripts" / "deck.py"), step, str(folder), *extra]
    for attempt in range(retries):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode in (0, 1):
            return result
        print(f"알림 · {step} 실패(코드 {result.returncode}) · {result.stderr.strip()[-300:]}")
        if attempt + 1 < retries:
            time.sleep(30)
    raise SystemExit(f"오류 · {step}을 끝내지 못했습니다\n{result.stdout}\n{result.stderr}")


def probe(built, count):
    """숨은 상태(뒤집은 면·풀이·말풍선·다른 탭)를 열어 엔진 검사를 다시 하고, 면 밖 글자·32px 미만 글자를 잰다."""
    found = []
    with Browser() as browser:
        cdp = browser.cdp
        for domain in ("Page.enable", "Runtime.enable"):
            cdp.call(domain)
        cdp.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                                                      "downloadThroughput": -1, "uploadThroughput": -1})
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": built.as_uri()})
        cdp.wait_event("Page.loadEventFired", 20)
        cdp.evaluate("Promise.resolve(window.deck.ready)", True)
        cdp.evaluate("Promise.resolve(window.deck.setMode('present'))", True)
        settle = ("document.documentElement.setAttribute('data-deck-shot','');"
                  "document.getAnimations().forEach(a=>{try{a.finish()}catch(e){}});"
                  "new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        for index in range(count):
            cdp.evaluate(f"Promise.resolve(window.deck.go({index},'all'))", True)
            cdp.evaluate(settle, True)
            states = ["처음"] + (cdp.evaluate(f"({PROBE_STATES})({index}, 'list')") or [])
            for state in states:
                if state != "처음":
                    cdp.evaluate(f"({PROBE_STATES})({index}, {json.dumps(state)})")
                    cdp.evaluate(settle, True)
                    for p in cdp.evaluate(f"Promise.resolve(window.deck.audit({index}))", True) or []:
                        if p.get("level") == "error" or p.get("code") in CODES:
                            found.append({**p, "slide": index + 1, "state": state})
                for p in cdp.evaluate(f"({PROBE_SPILL})({index})") or []:
                    found.append({**p, "slide": index + 1, "state": state, "level": "warn"})
            cdp.evaluate("Promise.resolve(window.deck.reset())", True)
    return found


def main():
    ap = argparse.ArgumentParser(description="부품 한도 시험기")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "pbj-kit-stress"), help="임시 덱 폴더를 만들 곳")
    ap.add_argument("--palette", default="sunny", help="색 묶음(palettes.json의 이름)")
    ap.add_argument("--dark", action="store_true", help='<html data-theme="dark">로 본다')
    ap.add_argument("--only", metavar="구도,…", help="이 구도만 시험한다")
    ap.add_argument("--no-shots", action="store_true", help="check에 --shots를 주지 않는다")
    args = ap.parse_args()

    layouts = json.loads((SKILL / "assets" / "kit" / "layouts.json").read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    if only and only - set(layouts):
        print(f"오류 · 모르는 구도: {', '.join(sorted(only - set(layouts)))}")
        return 2
    name = args.palette + ("-dark" if args.dark else "") + ("-" + "-".join(sorted(only)) if only else "")
    folder = Path(args.out) / name
    folder.mkdir(parents=True, exist_ok=True)
    plan = make_plan(layouts, args.palette, only)
    (folder / "콘티.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pictures = folder / "그림"
    pictures.mkdir(exist_ok=True)
    for picture in PICTURES.glob("*.png"):
        shutil.copyfile(picture, pictures / picture.name)
    draft = folder / "원고.html"
    if draft.exists():   # scaffold는 있는 원고를 보존하므로, 지난 원고는 시각을 붙인 이름으로 옆에 둔다
        draft.rename(folder / time.strftime("원고-%Y%m%d-%H%M%S.html"))

    run("plan", folder)
    plan_report = json.loads((folder / "검사" / "plan.json").read_text(encoding="utf-8"))
    if plan_report["errors"]:
        for p in plan_report["problems"]:
            if p["level"] == "error":
                print(f"plan 오류 · {p['id']} · {p['field']} · {p['message']}")
        return 2
    run("scaffold", folder)
    if args.dark:
        text = draft.read_text(encoding="utf-8")
        draft.write_text(text.replace('<html lang="ko"', '<html lang="ko" data-theme="dark"', 1), encoding="utf-8")
    run("build", folder)
    started = time.time()
    check = run("check", folder, *([] if args.no_shots else ["--shots"]))
    report_path = folder / "검사" / "report.json"
    if report_path.stat().st_mtime < started - 1:
        print(check.stdout, check.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    ids = [s["id"] for s in plan["slides"]]
    problems = [dict(p, state="처음") for p in report["problems"]
                if p.get("slide") and (p.get("level") == "error" or p.get("code") in CODES)]
    built = folder / (deck_plan.safe_name(plan["meta"]["title"]) + ".html")
    if not built.exists():
        built = next(p for p in folder.glob("*.html") if not p.name.startswith("원고"))
    problems += probe(built, len(ids))

    by_slide = {}
    for p in problems:
        by_slide.setdefault(p["slide"], []).append(p)
    print(f"색 묶음 {args.palette}{' · 어둡게' if args.dark else ''} · 장 {len(ids)}개 · {folder}")
    print("장 | 구도 | 변형 | 상태 | 코드 | 대상 | 내용")
    for number in sorted(by_slide):
        variant, _, layout = ids[number - 1].partition("-")
        for p in by_slide[number]:
            target = str(p.get("target", ""))
            target = target if len(target) < 60 else "…" + target[-59:]
            print(f"{number} | {layout} | {variant} | {p['state']} | {p.get('code')} | {target} | {p.get('message', '')}")
    layouts_seen = {i.partition("-")[2] for i in ids if not i.startswith('support-')}
    bad = {ids[n - 1].partition("-")[2] for n in by_slide if not ids[n - 1].startswith('support-')}
    print(f"문제 없는 구도 {len(layouts_seen - bad)}/{len(layouts_seen)}: {', '.join(sorted(layouts_seen - bad))}")
    print(f"문제 {len(problems)}건 · 캡처 {folder / '검사'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
