"""임페커블 지적의 출처 판정과 실제 덱의 detect 회귀 시험."""
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))
import deck
from cdp import BrowserError, find_browser


def finding(rule, snippet, **extra):
    return {"antipattern": rule, "snippet": snippet, "line": 0, **extra}


class OriginTest(unittest.TestCase):
    def test_shared_author_both_and_unknown_without_detector(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("DECK_TEST_TMP")) as temp:
            source = Path(temp).resolve() / "출처 시험.html"
            source.write_text('''<!doctype html><html><head>
<style data-deck="kit">.card { border-bottom:6px solid var(--line); border-radius:14px } .card{animation:pop 1s cubic-bezier(.3,1.4,.5,1) backwards;}</style>
<script data-deck="engine">const styles = `.ui{background:#222631}`;</script>
<style>.mine { border-bottom: 6px solid var(--line); border-radius : 14px; color: #C120AB; }</style>
<script>const custom = 'box-shadow: 10px 10px 0 #232B5C';</script>
</head><body><main id="deck">
<section class="slide"><p style='border-left:7px solid #8814BE; border-radius:37px'>직접 쓴 글</p></section>
</main></body></html>''', encoding="utf-8")
            items = [
                finding("bounce-easing", "cubic-bezier(.3, 1.4, .5, 1)"),
                finding("design-system-color", "Undocumented color #222631 is outside DESIGN.md colors"),
                finding("design-system-color", "Undocumented color #C120AB is outside DESIGN.md colors"),
                finding("design-system-color", 'border-left rgb(136, 20, 190) on p "직접 쓴 글" is outside DESIGN.md colors'),
                finding("border-accent-on-rounded", "border-bottom: 6px + border-radius: 14px"),
                finding("side-tab", "border-left: 7px + border-radius: 37px"),
                finding("hard-offset-shadow", "box-shadow: 10px 10px 0 #232B5C: hard offset with no blur; extra"),
                finding("oversized-h1", "이전에는 규칙 이름으로 숨긴 지적"),
                finding("bounce-easing", ""),
                finding("different-rule", "어느 쪽에도 없는 원문"),
                finding("other-file", "cubic-bezier(.3, 1.4, .5, 1)", file="other.css"),
            ]
            with patch.object(deck, "impeccable_json", return_value=items):
                issues, hidden = deck.impeccable_problems("fake", source, deck.ASSETS)
            self.assertEqual(hidden, {"bounce-easing": 1, "design-system-color": 1})
            self.assertEqual([p["code"] for p in issues], ["imp-" + item["antipattern"] for item in items[2:]])

    def test_template_inline_and_hand_added_markup_are_separate(self):
        sample = SKILL / "examples" / "부품-견본"
        with tempfile.TemporaryDirectory(dir=os.environ.get("DECK_TEST_TMP")) as temp:
            folder = Path(temp).resolve()
            shutil.copyfile(sample / "콘티.json", folder / "콘티.json")
            plan = json.loads(deck.read_text(folder / "콘티.json"))
            from deck_plan import rendered_slide
            slide = plan["slides"][0]
            markup = rendered_slide(plan, slide, deck.ASSETS / "kit")
            import re
            inline = re.search(r'style="(--n:[\d.]+)"', markup)[1]
            source = folder / "시험.html"
            source.write_text("<body><main id='deck'>" + markup.replace("</section>",
                '<span style="color:#876abc">손으로 추가</span></section>') + "</main></body>", encoding="utf-8")
            items = [finding("generated-inline", inline), finding("hand-inline", "color: #876abc")]
            with patch.object(deck, "impeccable_json", return_value=items):
                issues, hidden = deck.impeccable_problems("fake", source, deck.ASSETS)
            self.assertEqual(hidden, {"generated-inline": 1})
            self.assertEqual([p["code"] for p in issues], ["imp-hand-inline"])
            source.write_text(deck.read_text(source).replace('style="color:#876abc"', f'style="{inline};color:#876abc"'), encoding="utf-8")
            with patch.object(deck, "impeccable_json", return_value=items):
                issues, hidden = deck.impeccable_problems("fake", source, deck.ASSETS)
            self.assertFalse(hidden)
            self.assertEqual([p["code"] for p in issues], ["imp-generated-inline", "imp-hand-inline"])


def has_browser():
    try:
        return bool(find_browser())
    except BrowserError:
        return False


@unittest.skipUnless(deck.find_impeccable() and has_browser(), "임페커블이나 Chrome/Edge가 없습니다")
class RealDetectTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="imp-real-", dir=os.environ.get("DECK_TEST_TMP"))
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.env = dict(os.environ, IMPECCABLE_BIN=deck.find_impeccable(), TEMP=temp.name, TMP=temp.name, TMPDIR=temp.name)

    def cli(self, command, *args, expected=(0,)):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(SKILL / "scripts/deck.py"), command, str(self.folder), *args],
                                capture_output=True, encoding="utf-8", env=self.env, timeout=120)
        self.assertIn(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout

    def sample(self, palette="sunny", dark=False, layouts=None):
        self.cli("new", "--title", "출처 점검", "--palette", palette)
        sample = SKILL / "examples" / "부품-견본"
        plan = json.loads(deck.read_text(sample / "콘티.json"))
        plan["meta"].update(title="출처 점검", palette=palette)
        if layouts:
            by_layout = {slide["layout"]: slide for slide in plan["slides"]}
            plan["slides"] = [by_layout[layout] for layout in layouts]
        deck.write_text(self.folder / "콘티.json", json.dumps(plan, ensure_ascii=False))
        shutil.copytree(sample / "그림", self.folder / "그림")
        self.cli("scaffold")
        draft = self.folder / "원고.html"
        if dark:
            deck.write_text(draft, deck.read_text(draft).replace('<html lang="ko"', '<html lang="ko" data-theme="dark"', 1))
        self.assertTrue((self.folder / "DESIGN.md").is_file())
        self.assertTrue((self.folder / "PRODUCT.md").is_file())
        return draft

    def findings(self, expected=(0,)):
        self.cli("build", "--no-font")
        # detect는 완성 파일 전체를 검사한다. 브라우저 경로도 지나되 캡처는 1장만 한다.
        self.cli("check", "--slides", "1", expected=expected)
        result = json.loads(deck.read_text(self.folder / "검사/report.json"))
        self.assertGreater(result["imp_hidden"]["total"], 0)
        self.assertEqual(result["imp_hidden"]["total"], sum(result["imp_hidden"]["by_rule"].values()))
        return [item for item in result["problems"] if item["code"].startswith("imp-")]

    def test_unedited_29_layouts_with_context(self):
        self.sample()
        self.assertEqual(self.findings(), [])

    def test_unedited_12_slide_lesson_with_context(self):
        # 실제 12장 수업 덱과 같은 구도 구성. 임시 증거 덱 없이도 재실행할 수 있다.
        self.sample(layouts=("cover", "goals", "show", "table", "ask", "steps", "lab", "compare",
                             "quiz", "sort", "input", "summary"))
        self.assertEqual(self.findings(), [])

    def test_sky_palette_dark_with_context(self):
        self.sample("sky", dark=True)
        self.assertEqual(self.findings(), [])

    def test_author_colors_duplicate_border_and_easing_are_reported(self):
        draft = self.sample()
        text = deck.read_text(draft).replace("</head>", '''<style>
.probe { color:#A719C1; border-bottom:6px solid var(--line); border-radius:14px; }
.probe { background:linear-gradient(90deg,#7b2cbf,#9d4edd); animation:probe 1s cubic-bezier(.2,1.6,.4,1); }
</style></head>''', 1).replace("</section>", '''<div class="probe">
<div class="card"><div class="card">직접 더한 카드</div></div>
<p style="border-left:7px solid #8814BE; border-radius:37px; animation:mine 1s cubic-bezier(.1,1.7,.2,1)">직접 쓴 인라인</p>
</div></section>''', 1)
        deck.write_text(draft, text)
        warnings = self.findings(expected=(0, 1))
        count = Counter(item["code"] for item in warnings)
        self.assertGreater(count["imp-border-accent-on-rounded"], 0, warnings)
        messages = "\n".join(item["message"] for item in warnings)
        self.assertRegex(messages, r"(?i)#a719c1|rgb\(167, 25, 193\)")
        for excerpt in ('<div> "probe": children flush', "#7b2cbf", "cubic-bezier(.2, 1.6, .4, 1)",
                        "cubic-bezier(.1, 1.7, .2, 1)", "border-left: 7px + border-radius: 37px"):
            self.assertIn(excerpt, messages)
