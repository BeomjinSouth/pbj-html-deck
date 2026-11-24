"""콘티·틀·상태 명령의 표준 라이브러리 회귀 시험."""

import copy
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

SKILL = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures-plan"
SCRIPT = SKILL / "scripts" / "deck_plan.py"
SCRATCH = Path(os.environ.get("DECK_TEST_TMP", str(Path(tempfile.gettempdir()) / "pbj-html-deck-tools")))
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))
import deck_plan as deck  # noqa: E402


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class MathAndMarkupTest(unittest.TestCase):
    def test_middle_school_math_is_xml(self):
        examples = [r"y=ax^2", r"\frac{1}{2}x+3", r"\sqrt{2}", r"a\ne 0",
                    r"\overline{AB}=3", r"\angle A=60\circ", r"\triangle ABC",
                    r"x^2-5x+6=0", r"-3<x\le 2", r"\sqrt[3]{x^2+\frac{1}{\sqrt{2}}}",
                    r"x_{i}^{2}+\text{기울기}", r"\overarc{AB}", r"|(x+3)|",
                    r"\square ABCD\parallel AB", r"0.5+12.34", r"0.\overline{3}",
                    r"\frac{\frac{1}{2}}{\sqrt{x}}"]
        for source in examples:
            with self.subTest(source=source):
                fragment = deck.math_to_mathml(source)
                self.assertEqual(ET.fromstring(fragment).tag, "math")
        self.assertIn('<mi mathvariant="normal">A</mi>', deck.math_to_mathml("AB"))
        self.assertIn("<mi>x</mi>", deck.math_to_mathml("x"))
        self.assertIn("<mo>−</mo>", deck.math_to_mathml("x-3"))
        self.assertIn("<mn>12.34</mn>", deck.math_to_mathml("12.34"))
        self.assertIn("<mn>0.</mn>", deck.math_to_mathml(r"0.\overline{3}"))
        self.assertIn("</msup><mn>2</mn>", deck.math_to_mathml("x^12"))
        self.assertIn("<msup><mi>x</mi><mn>12</mn></msup>", deck.math_to_mathml("x^{12}"))
        self.assertIn('<mrow class="seg">', deck.math_to_mathml(r"\overline{AB}"))
        self.assertIn("<msubsup>", deck.math_to_mathml("x_1^2"))
        self.assertIn("<mroot>", deck.math_to_mathml(r"\sqrt[3]{2}"))
        self.assertIn("<mtext>한글</mtext>", deck.math_to_mathml(r"\text{한글}"))
        self.assertIn("<mn>60°</mn>", deck.math_to_mathml(r"60\circ"))

    def test_prefix_signs_are_distinct_from_binary_operators(self):
        prefix = '<mo form="prefix" lspace="0em" rspace="0em">'
        examples = {
            '-3x': ['−'], '+3': ['+'], 'y=-3x': ['−'], '(-2,-4)': ['−', '−'],
            'a+-b': ['−'], 'a--b': ['−'], r'2\times-3': ['−'],
            r'\frac{-1}{+2}': ['−', '+'], r'\sqrt{-x}': ['−'],
            r'\sqrt[+3]{-x}': ['−', '+'], 'x^{-1}': ['−'], 'x_{+1}': ['+'],
            'x^-1': ['−'], 'x_+1': ['+'], r'\quad-x': ['−'],
        }
        for source, signs in examples.items():
            with self.subTest(source=source):
                root = ET.fromstring(deck.math_to_mathml(source))
                actual = [node.text for node in root.iter('mo') if node.get('form') == 'prefix']
                self.assertCountEqual(actual, signs)
                for node in root.iter('mo'):
                    if node.get('form') == 'prefix':
                        self.assertEqual((node.get('lspace'), node.get('rspace')), ('0em', '0em'))
                self.assertEqual(deck.math_to_mathml(source).count(prefix), len(signs))
        for source in ('a-b', 'a+b', '(a)-b', '|a|-b', 'a!-b', r'a\circ-b', r'a\quad-b'):
            with self.subTest(source=source):
                self.assertNotIn(prefix, deck.math_to_mathml(source))

    def test_prefix_sign_width_is_used_inside_every_structure(self):
        width = lambda expression: deck.text_widths('$' + expression + '$')[0]
        self.assertAlmostEqual(width('-x'), .96 + .69)
        self.assertAlmostEqual(width('x^{-1}'), .69 + .76 * (.96 + .72) + .05)
        self.assertAlmostEqual(width('x^-1'), .69 + .76 * .96 + .05 + .72)
        self.assertAlmostEqual(width(r'\frac{-1}{+2}'), .76 * (.96 + .72) + .05)
        self.assertAlmostEqual(width(r'\sqrt{-x}'), 1.0 + .96 + .69)
        self.assertAlmostEqual(width('a-b'), .72 + 1.54 + .70)
        self.assertAlmostEqual(width('a+-b'), .72 + 1.54 + .96 + .70)

    def test_all_documented_commands(self):
        symbols = "times div pm le ge ne approx cdot angle triangle square perp parallel equiv sim pi circ therefore because cdots quad".split()
        self.assertEqual(set(symbols) - {"quad"}, set(deck.SYMBOLS))
        for name in symbols:
            with self.subTest(name=name):
                ET.fromstring(deck.math_to_mathml("\\" + name))

    def test_math_errors_name_formula_and_position(self):
        for source in (r"\unknown{x}", r"\frac{1}", r"\sqrt[3{2}", "(x+2", "x)",
                       "x^{2", "x^", "x^{}", r"\frac{}{2}", "a^^2", "x_1_2", "x]", "가", "|x", ""):
            with self.subTest(source=source):
                with self.assertRaises(deck.PlanError) as raised:
                    deck.math_to_mathml(source)
                self.assertIn("번째 글자", str(raised.exception))
                self.assertIn("수식 $", str(raised.exception))

    def test_markup_escaping_and_plain(self):
        source = '값 **중요**와 *x*\n$y=ax^2$ & <문장> \\* \\$'
        rendered = deck.display_text(source)
        self.assertIn('<b class="hl">중요</b>', rendered)
        self.assertIn('<var>x</var>', rendered)
        self.assertIn('<br>\n<math>', rendered)
        self.assertIn('&amp; &lt;문장&gt; * $', rendered)
        self.assertEqual(deck.plain_text(source), '값 중요와 x\ny=ax² & <문장> * $')
        for source in ("$x", "**끝없음", "*끝없음"):
            with self.assertRaises(deck.PlanError):
                deck.display_text(source)

    def test_weighted_widths_and_math_in_emphasis(self):
        self.assertAlmostEqual(deck.text_widths("가AB 1")[0], 3.54)
        self.assertEqual(deck.text_widths("**가**\n*x*"), [1.0, .69])
        self.assertEqual(deck.text_widths("$x$"), [.69])
        self.assertEqual(deck.text_widths("**$x$**"), [.69])
        self.assertAlmostEqual(deck.text_widths("$x^2$")[0], 1.2872)
        self.assertEqual(deck.text_widths(r"\*\$"), [2.0])

    def test_math_width_matches_browser_measurements(self):
        # 46px Maplestory 본문, Cambria Math 1.08em, 한글 '가' 39.34px 기준.
        samples = [
            (r"$x$", .672), (r"$y=ax^2$", 4.294), (r"$6<3+4$", 5.258),
            (r"$x^2-5x+6=0$", 8.671), (r"$8\le x<13$", 6.073),
            (r"$a+b=c$", 5.123), (r"$3(x+2)=15$", 7.676),
            (r"$\frac{1}{2}x+3$", 3.441), (r"$y=\frac{x+1}{2}$", 4.503),
            (r"$\sqrt{2}+\sqrt{3}$", 4.845), (r"$\sqrt{x^2+1}$", 4.411),
            (r"$a_1+a_2=a_3$", 6.956), (r"$\triangle ABC$", 3.324),
            (r"$\angle A=60\circ$", 5.139), (r"$AB\parallel CD$", 4.521),
            (r"$x\times 3=12$", 5.745), (r"$a\ne b$", 3.033),
            (r"$\frac{3}{4}+\frac{1}{2}$", 2.631), (r"$x^2+y^2=25$", 7.060),
            (r"$(-3,4)$", 3.862), (r"$x\quad y$", 2.642),
            (r"$\sqrt[3]{x^2}$", 2.311), (r"길이 $x$를 구해요", 7.169),
            (r"$a$가 커지면 그래프는?", 9.846), (r"값은 $2x+3$입니다", 8.860),
            (r"넓이 $\frac{1}{2}ab$를 구해요", 8.448), (r"$1\le x\le 10$ 입니다", 9.417),
            (r"$\frac{1}{\sqrt{2}}$", 1.268), (r"$\overline{AB}=3$", 3.907),
            (r"$\text{기울기}=2$", 5.283),
            # 부호 수정 후 Chrome에서 같은 글꼴·크기로 측정한 값(한글 한 글자 폭 기준).
            (r"$y=-3x$", 4.645), (r"$(-2,-4)$", 4.779), (r"$a-b$", 2.877),
            (r"$x^{-1}$", 1.913), (r"$-x+3$", 3.803), (r"$2\times(-3)$", 4.690),
            (r"$a+-b$", 3.815), (r"$\frac{-1}{-2}$", 1.243), (r"$\sqrt{-x}$", 2.571),
            (r"$x_{-1}$", 1.913), (r"$a\times-3$", 3.652),
        ]
        for text, actual in samples:
            with self.subTest(text=text):
                ratio = deck.text_widths(text)[0] / actual
                self.assertGreaterEqual(ratio, .9)
                self.assertLessEqual(ratio, 1.2)

    def test_math_width_respects_stacked_structure_and_live_values(self):
        width = lambda s: deck.text_widths(s)[0]
        self.assertAlmostEqual(width(r"$x_1^2$"), width(r"$x_1$"))
        self.assertGreater(width(r"$\frac{123}{2}$"), width(r"$\frac{1}{2}$"))
        self.assertGreater(width(r"$\sqrt[12]{x}$"), width(r"$\sqrt{x}$"))
        self.assertGreater(deck.text_widths(r"$x=\val{a}$", {"a": -12})[0], width("$x=2$"))

    def test_blank_with_emphasis_and_math_and_invalid_blank(self):
        result = deck.display_text(r"앞 [[**답** $x^2$]] 뒤")
        self.assertIn('<span class="blank" data-peek role="button" tabindex="0"><span><b class="hl">답</b> <math>', result)
        self.assertIn('</math></span></span> 뒤', result)
        self.assertEqual(deck.plain_text(r"앞 [[**답** $x^2$]] 뒤"), "앞 답 x² 뒤")
        for text in ("[[닫지 않음", "[[]]", "[[   ]]"):
            with self.subTest(text=text), self.assertRaises(deck.PlanError):
                deck.display_text(text)

    def test_live_output_initial_values_and_literal_escapes(self):
        state = {"a": -2.5, "b": 3}
        result = deck.display_text(r"{a} {b:1} {unknown+1} \{a} \[[답]]", state)
        self.assertIn('<output data-show="a">−2.5</output>', result)
        self.assertIn('<output data-show="b" data-fix="1">3.0</output>', result)
        self.assertIn('<output data-show="unknown+1">…</output>', result)
        self.assertIn('{a} [[답]]', result)
        self.assertEqual(deck.display_text("{a} {unclosed", None), "{a} {unclosed")
        for text in ("{}", "{missing"):
            with self.subTest(text=text), self.assertRaises(deck.PlanError):
                deck.display_text(text, state)

    def test_live_values_match_javascript_logical_operands_and_booleans(self):
        cases = {"0 || 4": 4, "2 && 5": 5, "false && unknown": False,
                 "true || unknown": True, "true === 1": False, "true !== 1": True}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(deck.live_eval(expression, {}), expected)
        rendered = deck.display_text("{true} {false} {2**10000}", {})
        self.assertIn('<output data-show="true">true</output>', rendered)
        self.assertIn('<output data-show="false">false</output>', rendered)
        self.assertIn('<output data-show="2**10000">…</output>', rendered)

    def test_live_fixed_decimals_round_like_javascript(self):
        result = deck.display_text("{a:1} {b:1} {c:2}", {"a": 2.25, "b": -2.25, "c": 1.005})
        self.assertIn('<output data-show="a" data-fix="1">2.3</output>', result)
        self.assertIn('<output data-show="b" data-fix="1">−2.3</output>', result)
        self.assertIn('<output data-show="c" data-fix="2">1.00</output>', result)

    def test_math_val_requires_state_and_renders_initial_value(self):
        result = deck.display_text(r"$x=\val{a:1}$", {"a": -2.5})
        self.assertIn('<mn data-show="a" data-fix="1">−2.5</mn>', result)
        ET.fromstring(result)
        with self.assertRaisesRegex(deck.PlanError, "state"):
            deck.display_text(r"$x=\val{a}$")

    def test_live_eval_operations_functions_and_rejected_expressions(self):
        state = {"a": 2, "b": -3}
        cases = {"a+b*2": -4, "2**3": 8, "a/4": .5, "-5%3": -2,
                 "Math.sqrt(9)": 3, "atan2(0,1)": 0, "max(a,4,b)": 4,
                 "a >= 2 && b < 0": True, "a === 2": True,
                 "!(a === 3) || false": True, "Math.PI > 3": True}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(deck.live_eval(expression, state), expected)
        for expression in ("unknown", "a ? 1 : 0", "open('x')"):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                deck.live_eval(expression, state)


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.plan = {"meta": {"title": "전체"}, "assets": [{"id": "pose_one", "alt": '그림 "$x^2$"'}]}
        self.slide = {"id": "slide-one", "title": '**제목** $x^2$', "image": "pose_one", "items": ["가", "나", "다"]}

    def test_values_contexts_comments_and_attribute_text(self):
        template = ('<section title="{{title}}"><p>{{title}} {{meta.title}} {{{raw}}}</p>'
                    '{{! 감춤 }}{{#obj}}{{label}} {{title}}{{/obj}}'
                    '{{^missing}}없음{{/missing}}{{asset:image|big}}'
                    '{{asset:missing}}</section>')
        slide = dict(self.slide, raw="<em>raw</em>", obj={"label": "안쪽", "title": "안쪽제목"})
        result = deck.Template(template).render(self.plan, slide)
        self.assertIn('title="제목 x²"', result)
        self.assertIn('<b class="hl">제목</b> <math>', result)
        self.assertIn('전체 <em>raw</em>', result)
        self.assertIn('안쪽 안쪽제목', result)
        self.assertIn('없음<img class="asset big" data-asset="pose_one"', result)
        self.assertIn('alt="그림 &quot;x²&quot;"', result)
        self.assertNotIn('감춤', result)
        attribute = deck.Template('<p title="{{title}}"></p>').render(self.plan, dict(slide, title='첫 줄\n$x^2$'))
        self.assertEqual(attribute, '<p title="첫 줄 x²"></p>')

    def test_repeat_index_and_step(self):
        source = ('{{#items}}({{@i}}:{{@n}}/{{@count}}:{{.}}'
                  '{{#@first}}F{{/@first}}{{#@last}}L{{/@last}}{{step}}){{/items}}{{step:same}}')
        result = deck.Template(source).render(self.plan, self.slide)
        self.assertEqual(result, '(0:1/3:가F data-step="1")(1:2/3:나 data-step="2")(2:3/3:다L data-step="3") data-step="3"')
        result = deck.Template(source).render(self.plan, dict(self.slide, reveal="all"))
        self.assertNotIn("data-step", result)

    def test_shuffle_deterministic_and_nonidentity(self):
        source = '{{#items|shuffle}}{{@rank}}:{{.}};{{/items}}'
        for size in range(2, 12):
            slide = dict(self.slide, items=[str(i) for i in range(size)])
            results = [deck.Template(source).render(self.plan, slide) for _ in range(3)]
            self.assertEqual(results[0], results[1])
            ranks = [int(x) for x in re.findall(r'(\d+):', results[0])]
            self.assertEqual(sorted(ranks), list(range(1, size + 1)))
            self.assertNotEqual(ranks, list(range(1, size + 1)))

    def test_template_errors_include_filename_and_line(self):
        for source in ('첫 줄\n{{#x}}값', '첫 줄\n{{#x}}{{/y}}', '첫 줄\n{{step:unknown}}',
                       '첫 줄\n{{#x|other}}x{{/x}}', '첫 줄\n{{broken', '첫 줄\n{{@bad}}'):
            with self.subTest(source=source):
                with self.assertRaises(deck.PlanError) as raised:
                    deck.Template(source, "틀.html")
                self.assertIn("틀.html:2", str(raised.exception))
        with self.assertRaisesRegex(deck.PlanError, r"틀.html:2"):
            deck.Template('줄\n{{step:same}}', '틀.html').render(self.plan, self.slide)

    def test_root_names_do_not_leak_and_json_is_safe_in_single_quotes(self):
        plan = dict(self.plan, mascot={"yes": "좋아요"})
        slide = dict(self.slide, state={"quote": "it's <safe> & nice", "n": [1, 2]})
        source = "{{#mascot}}누출{{/mascot}}{{^mascot}}없음{{/mascot}} {{meta.title}} {{deck.mascot.yes}} <p data-state='{{{state}}}'></p>"
        result = deck.Template(source).render(plan, slide)
        self.assertIn("없음 전체 좋아요", result)
        self.assertNotIn("누출", result)
        parser = deck.SourceParser(result)
        state = next(n["attrs"]["data-state"] for n in parser.nodes if n["tag"] == "p")
        self.assertEqual(json.loads(state), slide["state"])
        list_markup = deck.Template("<p data-value='{{{items}}}'></p>").render(plan, slide)
        parsed = deck.SourceParser(list_markup)
        self.assertEqual(json.loads(parsed.nodes[0]["attrs"]["data-value"]), slide["items"])

    def test_derived_numbers_tones_lengths_and_clock(self):
        slides = [
            {"id": "cover", "layout": "cover", "tag": "표지", "title": "표지"},
            {"id": "before", "layout": "section", "title": "구분"},
            {"id": "one", "layout": "cards", "tag": "탐구", "title": "a<b", "cards": [
                {"title": "가나다", "draw": "<svg></svg>"}, {"title": "가나다라"}],
             "items": ["가", "가나다"], "timer": 125},
            {"id": "same", "layout": "cards", "tag": "탐구"},
            {"id": "between", "layout": "section"},
            {"id": "next", "layout": "cards", "tag": "정리"},
            {"id": "after", "layout": "closing"},
        ]
        plan = {"slides": slides}
        derived = [deck.derived_slide(plan, s) for s in slides]
        self.assertEqual([s["_no"] for s in derived], list(range(1, 8)))
        self.assertEqual([s["_tone"] for s in derived], [1, 1, 1, 1, 2, 2, 2])
        self.assertEqual(derived[2]["_len"]["title"], 2.5)
        title_with_two_inequalities = dict(slides[2], title="a<b>c")
        self.assertEqual(deck.derived_slide(plan, title_with_two_inequalities)["_len"]["title"], 3.5)
        with_math = dict(slides[2], formula="$a<b>c$")
        self.assertEqual(deck.derived_slide(plan, with_math)["_len"]["formula"], 5.5)
        with_rounding = dict(slides[2], title="$bbbbbbbbbb$")
        self.assertEqual(deck.derived_slide(plan, with_rounding)["_len"]["title"], 7)
        self.assertEqual(derived[2]["_len"]["cards"]["title"], 4)
        self.assertNotIn("draw", derived[2]["_len"]["cards"])
        self.assertEqual(derived[2]["_len"]["items"], 3)
        self.assertEqual(derived[2]["_mmss"]["timer"], "2:05")


class PlanValidationTest(unittest.TestCase):
    def setUp(self):
        self.plan = load(FIXTURES / "콘티.json")
        self.layouts = load(FIXTURES / "layouts.json")

    def codes(self, plan=None, layouts=None):
        _, problems = deck.validate_plan(plan or self.plan, layouts or self.layouts)
        return {p["code"] for p in problems}

    def test_fixture_has_no_errors_and_varied_design(self):
        _, problems = deck.validate_plan(self.plan, self.layouts)
        self.assertEqual([p for p in problems if p["level"] == "error"], [])
        self.assertNotIn("slide-count", self.codes())
        self.assertNotIn("mascot-variety", self.codes())
        self.assertNotIn("interaction-variety", self.codes())

    def test_error_codes_trigger_and_clear(self):
        def missing(p): del p["slides"][1]["purpose"]
        def unknown_layout(p): p["slides"][1]["layout"] = "missing"
        def unknown_field(p): p["slides"][1]["surprise"] = 3
        def nested_unknown(p): p["slides"][1]["cards"][0]["surprise"] = 3
        def invalid_id(p): p["slides"][1]["id"] = "한글"
        def duplicate(p): p["slides"][1]["id"] = p["slides"][0]["id"]
        def asset(p): p["slides"][0]["image"] = "missing"
        def length(p): p["slides"][1]["title"] = "가" * 23
        def lines(p): p["slides"][1]["title"] = "가\n나"
        def count(p): p["slides"][1]["cards"] = [{"title": "하나"}]
        def math(p): p["slides"][1]["title"] = r"$x\unknown$"
        def example(p): p["slides"][1]["id"] = "example-card"
        def first(p): p["slides"][0], p["slides"][1] = p["slides"][1], p["slides"][0]
        def last(p): p["slides"][-1], p["slides"][-2] = p["slides"][-2], p["slides"][-1]
        def wrong_type(p): p["slides"][1]["cards"] = "두 개"
        def choice(p): p["slides"][1]["config"]["mode"] = "z"
        def visual(p):
            p["slides"][0].pop("image")
            p["slides"][0].pop("mascot")
        def mockup(p): p["slides"][1]["mockup"] = "same-as:없는장"
        cases = [("required", missing), ("unknown-layout", unknown_layout), ("unknown-field", unknown_field),
                 ("unknown-field", nested_unknown), ("id-format", invalid_id), ("duplicate-id", duplicate),
                 ("missing-asset", asset), ("text-limit", length), ("text-limit", lines), ("list-limit", count),
                 ("math", math), ("example", example), ("first-not-cover", first), ("last-not-wrap", last),
                 ("type", wrong_type), ("choice", choice), ("visual-required", visual),
                 ("mockup-reference", mockup)]
        baseline = self.codes()
        for code, mutate in cases:
            with self.subTest(code=code, mutate=mutate.__name__):
                self.assertNotIn(code, baseline)
                plan = copy.deepcopy(self.plan)
                mutate(plan)
                self.assertIn(code, self.codes(plan))

    def test_image_minimums_and_short_deck_exemptions(self):
        layouts = copy.deepcopy(self.layouts)
        layouts['detail']['fields']['draw'] = {'type': 'raw', 'visual': True}

        def errors(plan):
            return [p for p in deck.validate_plan(plan, layouts)[1] if p['level'] == 'error']

        def found(plan, code):
            return [p for p in errors(plan) if p['code'] == code]

        def short(plan):
            plan['slides'] = plan['slides'][:5]
            return plan

        # 2개는 오류, 3개는 경고만, 4개는 경고도 없다. 쓰지 않는 포즈는 세지 않는다.
        plan = copy.deepcopy(self.plan)
        for slide in plan['slides']:
            if slide.get('mascot'):
                slide['mascot'] = 'pose-one'
        plan['slides'][1]['mascot'] = 'pose-two'
        self.assertTrue(found(plan, 'mascot-count'))
        self.assertFalse(found(short(copy.deepcopy(plan)), 'mascot-count'))
        plan['slides'][2]['mascot'] = 'pose-three'
        self.assertFalse(found(plan, 'mascot-count'))
        self.assertIn('mascot-variety', self.codes(plan, layouts))
        plan['slides'][3]['mascot'] = 'pose-four'
        self.assertNotIn('mascot-variety', self.codes(plan, layouts))

        # 10장 중 2장은 실패, 정확히 3장은 통과한다.
        plan = copy.deepcopy(self.plan)
        plan['slides'][4:4] = [dict(plan['slides'][4], id='extra-one'), dict(plan['slides'][4], id='extra-two')]
        for slide in plan['slides']:
            slide.pop('mascot', None)
        plan['slides'][0]['mascot'] = 'pose-one'
        plan['slides'][1]['mascot'] = 'pose-two'
        self.assertTrue(found(plan, 'mascot-coverage'))
        self.assertFalse(found(short(copy.deepcopy(plan)), 'mascot-coverage'))
        plan['slides'][2]['mascot'] = 'pose-three'
        self.assertFalse(found(plan, 'mascot-coverage'))

        # 선언만 한 스티커·배경·직접 그린 SVG는 마스코트 외 그림으로 세지 않는다.
        plan = copy.deepcopy(self.plan)
        plan['slides'][0].pop('image')
        plan['slides'][4]['draw'] = '<svg></svg>'
        self.assertTrue(found(plan, 'non-mascot-visual'))
        self.assertFalse(found(short(copy.deepcopy(plan)), 'non-mascot-visual'))
        plan['slides'][0]['image'] = 'star'
        self.assertFalse(found(plan, 'non-mascot-visual'))
        plan['assets'][-1]['kind'] = 'scene'
        self.assertFalse(found(plan, 'non-mascot-visual'))
        plan['assets'][-1]['kind'] = 'background'
        self.assertTrue(found(plan, 'non-mascot-visual'))

        # 10장 중 3장은 실패, 4장은 통과. SVG 도형은 비율에서 제외한다.
        plan = copy.deepcopy(self.plan)
        plan['slides'][4:4] = [dict(plan['slides'][4], id='extra-one'), dict(plan['slides'][4], id='extra-two')]
        for slide in plan['slides']:
            slide.pop('mascot', None)
        for slide in plan['slides'][4:7]:
            slide['draw'] = '<svg></svg>'
        for i, ident in enumerate(('pose-one', 'pose-two', 'pose-three')):
            plan['slides'][i]['mascot'] = ident
        self.assertTrue(found(plan, 'image-coverage'))
        self.assertFalse(found(short(copy.deepcopy(plan)), 'image-coverage'))
        plan['slides'][3]['mascot'] = 'pose-four'
        self.assertFalse(found(plan, 'image-coverage'))

        # 2장 연속 경고는 유지하며 4장 연속부터 오류다. 5장 이하는 오류 면제다.
        plan = copy.deepcopy(self.plan)
        for slide in plan['slides'][1:5]:
            slide.pop('mascot', None)
        self.assertTrue(found(plan, 'no-visual-repeat'))
        self.assertFalse(found(short(copy.deepcopy(plan)), 'no-visual-repeat'))
        plan['slides'][4]['mascot'] = 'pose-one'
        self.assertFalse(found(plan, 'no-visual-repeat'))
        self.assertTrue(any(p['code'] == 'no-visual-repeat' and p['level'] == 'warn'
                            for p in deck.validate_plan(plan, layouts)[1]))

    def test_every_short_deck_is_exempt_from_image_errors(self):
        new_errors = {'mascot-count', 'mascot-coverage', 'non-mascot-visual', 'image-coverage', 'no-visual-repeat'}
        for length in range(1, 6):
            with self.subTest(length=length):
                plan = copy.deepcopy(self.plan)
                plan['slides'] = plan['slides'][:length]
                for slide in plan['slides']:
                    slide.pop('mascot', None)
                    slide.pop('image', None)
                problems = deck.validate_plan(plan, self.layouts)[1]
                self.assertFalse([p for p in problems if p['level'] == 'error' and p['code'] in new_errors])

    def test_master_aliases_and_unreferenced_assets_do_not_inflate_counts(self):
        plan = copy.deepcopy(self.plan)
        plan['assets'].extend([{'id': 'master-alias', 'kind': 'mascot_master', 'prompt': 'same master'},
                               {'id': 'unused-pose', 'kind': 'mascot', 'prompt': 'not shown'}])
        for slide in plan['slides']:
            if slide.get('mascot'):
                slide['mascot'] = 'mascot'
        plan['slides'][1]['mascot'] = 'master-alias'
        plan['slides'][2]['mascot'] = 'pose-one'
        problems = deck.validate_plan(plan, self.layouts)[1]
        self.assertTrue(any(p['code'] == 'mascot-count' and p['level'] == 'error' for p in problems))
        plan['slides'][3]['mascot'] = 'pose-two'
        problems = deck.validate_plan(plan, self.layouts)[1]
        self.assertFalse(any(p['code'] == 'mascot-count' for p in problems))

    def test_common_fields_and_asset_underscore(self):
        plan = copy.deepcopy(self.plan)
        plan["slides"][0].update({"tag": "탐구", "say": ["안녕"], "reveal": "all", "talk": "설명",
                                   "mockup_note": "지시", "mockup": "same-as:cards"})
        plan["assets"][0]["id"] = "pose_one"
        plan["slides"][0]["mascot"] = "pose_one"
        plan["slides"][5]["mascot"] = "pose_one"
        plan["slides"][1]["id"] = "cards--one"
        self.assertNotIn("id-format", self.codes(plan))
        self.assertNotIn("unknown-field", self.codes(plan))
        self.assertNotIn("missing-asset", self.codes(plan))

    def test_warning_codes_trigger_and_clear(self):
        baseline = self.codes()
        def layout_repeat(p):
            p["slides"].insert(6, dict(p["slides"][5], id="third-detail"))
        def no_visual(p):
            p["slides"][1].pop("mascot")
            p["slides"][2].pop("mascot")
        def gap(p):
            sample = p["slides"][4]
            p["slides"][3:3] = [dict(sample, id=f"gap-{n}") for n in range(6)]
        def variety(p):
            p["slides"][3].update(layout="quiz", options=["하나", "둘"])
            p["slides"][3].pop("items")
        def mascot(p):
            for slide in p["slides"]: slide.pop("mascot", None)
        def unused(p): p["assets"].append({"id": "extra", "kind": "sticker", "prompt": "extra"})
        def asset_count(p):
            p["assets"].extend({"id": f"extra-{n}", "kind": "sticker", "prompt": "extra"} for n in range(36))
        def purpose(p): p["slides"][0]["focus"] = p["slides"][0]["purpose"]
        def say(p): p["slides"][0]["say"] = ["가" * 19]
        def slide_count(p): p["slides"] = [p["slides"][0], p["slides"][-1]]
        for code, mutate in [("layout-repeat", layout_repeat), ("no-visual-repeat", no_visual),
                             ("interaction-gap", gap), ("interaction-variety", variety),
                             ("mascot-variety", mascot), ("unused-asset", unused),
                             ("asset-count", asset_count), ("purpose-focus", purpose),
                             ("say-long", say), ("slide-count", slide_count)]:
            with self.subTest(code=code):
                self.assertNotIn(code, baseline)
                plan = copy.deepcopy(self.plan)
                mutate(plan)
                self.assertIn(code, self.codes(plan))

    def test_unknown_asset_refs_and_missing_root(self):
        plan = copy.deepcopy(self.plan)
        plan["assets"][0]["ref"] = ["missing"]
        self.assertIn("missing-asset", self.codes(plan))
        self.assertIn("required", self.codes({"slides": []}))

    def test_unused_asset_references_follow_used_graph(self):
        plan = copy.deepcopy(self.plan)
        plan["assets"].extend([
            {"id": "used-ref", "kind": "sticker", "prompt": "used"},
            {"id": "dead-ref", "kind": "sticker", "prompt": "unused"},
            {"id": "dead-parent", "kind": "sticker", "prompt": "unused", "ref": ["dead-ref"]},
        ])
        plan["assets"][0]["ref"] = ["used-ref"]
        _, problems = deck.validate_plan(plan, self.layouts)
        unused = {p["field"] for p in problems if p["code"] == "unused-asset"}
        self.assertNotIn("assets.used-ref", unused)
        self.assertTrue({"assets.dead-parent", "assets.dead-ref"}.issubset(unused))

    def test_malformed_assets_and_mockup_cycles_are_reported(self):
        plan = copy.deepcopy(self.plan)
        plan["assets"][0]["kind"] = []
        plan["assets"][0]["alt"] = "$x"
        plan["style"]["asset_style"] = 1
        plan["slides"][0]["mockup"] = "same-as:cards"
        plan["slides"][1]["mockup"] = "same-as:cover"
        codes = self.codes(plan)
        self.assertTrue({"required", "math", "type", "mockup-reference"}.issubset(codes))
        plan["slides"][0]["id"] = ["wrong"]
        self.assertIn("required", self.codes(plan))

    def test_registry_error_is_reported(self):
        layouts = copy.deepcopy(self.layouts)
        layouts["cards"]["fields"]["title"] = "not a definition"
        self.assertIn("registry", self.codes(layouts=layouts))
        self.assertNotIn("registry", self.codes())

    def test_json_types_and_raw_visual_counts(self):
        layouts = copy.deepcopy(self.layouts)
        layouts["cards"]["visual"] = True
        layouts["cards"]["fields"].update({"state": {"type": "json"}, "draw": {"type": "raw", "visual": True}})
        plan = copy.deepcopy(self.plan)
        slide = plan["slides"][1]
        slide.pop("mascot")
        plan["slides"][2].pop("mascot")
        slide.update(state={"a": 1}, draw="<svg></svg>")
        rows, problems = deck.validate_plan(plan, layouts)
        self.assertEqual(rows[1]["images"], 1)
        self.assertFalse(any(p["code"] == "visual-required" and p["slide"] == 2 for p in problems))
        self.assertFalse(any(p["code"] == "no-visual-repeat" and p["slide"] == 3 for p in problems))
        slide["state"] = [1, 2]
        self.assertNotIn("type", self.codes(plan, layouts))
        slide["state"] = "{}"
        self.assertIn("type", self.codes(plan, layouts))
        slide.pop("draw")
        slide["state"] = {}
        self.assertIn("visual-required", self.codes(plan, layouts))
        rows, problems = deck.validate_plan(plan, layouts)
        self.assertEqual(rows[1]["images"], 0)
        self.assertTrue(any(p["code"] == "no-visual-repeat" and p["slide"] == 3 for p in problems))

    def test_text_list_limits_and_wrapped_lines(self):
        layouts = copy.deepcopy(self.layouts)
        layouts["quiz"]["fields"]["options"]["of"] = {"type": "text", "max": [4, 2]}
        plan = copy.deepcopy(self.plan)
        options = plan["slides"][2]["options"]
        options[0] = "가나다라마바사아"  # 자동 접힘 두 줄
        self.assertNotIn("text-limit", self.codes(plan, layouts))
        options[0] = "가나다라마바사아자"
        self.assertIn("text-limit", self.codes(plan, layouts))
        options[0] = "가나다라\n마바사아"
        self.assertNotIn("text-limit", self.codes(plan, layouts))
        options[0] = "가나다라마\n바"
        self.assertIn("text-limit", self.codes(plan, layouts))
        options[0] = "$x+x$\n$x+x$"
        self.assertNotIn("text-limit", self.codes(plan, layouts))
        options[0] = "$x+x+x$"  # 두 줄 분량이어도 한 수식은 자동으로 접히지 않는다
        self.assertIn("text-limit", self.codes(plan, layouts))
        layouts["quiz"]["fields"]["options"]["of"]["max"] = [4, 1]
        options[0] = "가나다라"
        options[1] = "마바사아"
        self.assertNotIn("text-limit", self.codes(plan, layouts))
        options[0] += "마"
        self.assertIn("text-limit", self.codes(plan, layouts))

    def test_numeric_limits_and_buddy_slots(self):
        layouts = copy.deepcopy(self.layouts)
        layouts["cards"]["fields"]["config"]["of"]["count"].update(min=1, max=3)
        plan = copy.deepcopy(self.plan)
        for value in (1, 3):
            plan["slides"][1]["config"]["count"] = value
            self.assertNotIn("number-limit", self.codes(plan, layouts))
        for value in (0, 4):
            plan["slides"][1]["config"]["count"] = value
            self.assertIn("number-limit", self.codes(plan, layouts))
        plan["slides"][1]["config"]["count"] = 2
        plan["slides"][1]["say"] = ["안녕"]
        for buddy, expected in [(None, {"no-mascot-slot", "no-say-slot"}),
                                ("pose", {"no-say-slot"}), ("say", set())]:
            layouts["cards"]["buddy"] = buddy
            found = self.codes(plan, layouts) & {"no-mascot-slot", "no-say-slot"}
            self.assertEqual(found, expected)
        plan["slides"][1].pop("mascot")
        layouts["cards"]["buddy"] = "say"
        self.assertIn("no-say-slot", self.codes(plan, layouts))
        del layouts["cards"]["buddy"]
        self.assertNotIn("no-say-slot", self.codes(plan, layouts))

    def test_mascot_reactions_pair_asset_references_and_unused(self):
        plan = copy.deepcopy(self.plan)
        plan["mascot"]["yes"] = "pose-one"
        self.assertIn("required", self.codes(plan))
        plan["mascot"]["no"] = "missing"
        self.assertIn("missing-asset", self.codes(plan))
        plan["assets"].extend([{"id": "yes-pose", "kind": "mascot", "prompt": "yes"},
                               {"id": "no-pose", "kind": "mascot", "prompt": "no"}])
        plan["mascot"].update(yes="yes-pose", no="no-pose")
        _, problems = deck.validate_plan(plan, self.layouts)
        self.assertFalse(any(p["code"] == "unused-asset" and p["field"] in {"assets.yes-pose", "assets.no-pose"} for p in problems))
        self.assertFalse(any(p["code"] == "missing-asset" for p in problems))

    def test_palette_is_checked_only_with_a_palette_registry(self):
        _, problems = deck.validate_plan(self.plan, self.layouts, ["sunny", "sky"])
        self.assertTrue(any(p["code"] == "choice" and p["field"] == "meta.palette" for p in problems))
        _, problems = deck.validate_plan(self.plan, self.layouts, [])
        self.assertFalse(any(p["field"] == "meta.palette" for p in problems))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="한글 공백 ", dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8", "DECK_KIT": str(FIXTURES),
                         "TEMP": str(self.root), "TMP": str(self.root), "TMPDIR": str(self.root)})

    def test_default_scratch_uses_system_temp_and_unicode_path(self):
        env = self.env.copy()
        env.pop("DECK_TEST_TMP", None)
        code = ("import json,sys; sys.path.insert(0,sys.argv[1]); import test_plan as t; "
                "case=t.WorkflowTest(); case.setUp(); "
                "print(json.dumps({'scratch':str(t.SCRATCH),'path':str(case.root)},ensure_ascii=False)); "
                "case.doCleanups()")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(Path(__file__).parent)],
                                env=env, text=True, encoding="utf-8", capture_output=True, timeout=30, check=True)
        paths = json.loads(result.stdout)
        self.assertEqual(Path(paths["scratch"]), self.root / "pbj-html-deck-tools")
        self.assertTrue(Path(paths["scratch"]).is_dir())
        self.assertEqual(Path(paths["path"]).parent, Path(paths["scratch"]))
        self.assertIn("한글 공백 ", Path(paths["path"]).name)

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)], env=env or self.env,
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, f"{args}\n{result.stdout}\n{result.stderr}")
        return result

    def put_plan(self, plan=None):
        (self.root / "콘티.json").write_text(json.dumps(plan or load(FIXTURES / "콘티.json"), ensure_ascii=False, indent=2), encoding="utf-8")

    def slide_raws(self, text):
        _, _, nodes = deck.document_slides(text)
        return {n["attrs"].get("data-id"): text[n["start"]:n["end"]] for n in nodes}

    def test_new_example_plan_and_complete_workflow(self):
        folder = self.root / "새 덱"
        self.cli("new", folder, "--title", "한글 수업 & 식", "--kind", "lesson", "--palette", "test")
        self.assertFalse((folder / "원고.html").exists())
        self.cli("new", folder, "--title", "겹침", expected=2)
        result = self.cli("plan", folder, expected=1)
        self.assertIn("example", result.stdout)
        self.assertIn("예시", result.stdout)
        plan = load(FIXTURES / "콘티.json")
        (folder / "콘티.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        self.cli("plan", folder, "--kit", FIXTURES)
        self.cli("scaffold", folder)
        content = (folder / "원고.html").read_text(encoding="utf-8")
        doc, body, slides = deck.document_slides(content)
        self.assertEqual(len(slides), 8)
        self.assertTrue(all(n["parent"] is body for n in slides))
        self.assertEqual([n["attrs"]["data-id"] for n in slides], [s["id"] for s in plan["slides"]])
        self.assertTrue(all(n["attrs"].get("data-layout") for n in slides))
        self.assertIn('data-title="삼각형의 조건"', content)
        self.assertIn('<b class="hl">조건</b>', content)
        self.assertIn('그림/star.png', content)
        self.assertIn("질문으로 시작한다", (folder / "진행안.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(doc)

    def test_new_uses_optional_product_and_design_templates(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from unittest.mock import patch
        fake_skill = self.root / "가짜 스킬"
        templates = fake_skill / "assets" / "impeccable"
        templates.mkdir(parents=True)
        (templates / "PRODUCT.md").write_text("제목 {{title}}", encoding="utf-8")
        (templates / "DESIGN.md").write_text("종류 {{kind}} · 색 {{palette}}", encoding="utf-8")
        folder = self.root / "덱"
        args = SimpleNamespace(folder=folder, title="수업", kind="lesson", palette="blue")
        with patch.object(deck, "SKILL", fake_skill), redirect_stdout(StringIO()):
            self.assertEqual(deck.new_command(args), 0)
            with self.assertRaises(deck.PlanError):
                deck.new_command(args)
        self.assertEqual((folder / "PRODUCT.md").read_text(encoding="utf-8"), "제목 수업")
        self.assertEqual((folder / "DESIGN.md").read_text(encoding="utf-8"), "종류 lesson · 색 blue")
        self.assertFalse((folder / "원고.html").exists())

    def test_incremental_scaffold_preserves_human_edit_and_inserts_in_order(self):
        self.put_plan()
        self.cli("scaffold", self.root, "--only", "cover,quiz,wrap")
        path = self.root / "원고.html"
        source = path.read_text(encoding="utf-8").replace('<h2>삼각형이 될까요?</h2>', "<h2 data-person='유지'>사람이 직접 &amp; 수정</h2>")
        source = source.replace("</body>", "<section class='slide' data-id='old'><p>원고 전용</p></section>\n</body>")
        path.write_text(source, encoding="utf-8", newline="")
        before = self.slide_raws(source)
        response = self.cli("scaffold", self.root)
        result = path.read_text(encoding="utf-8")
        after = self.slide_raws(result)
        for ident, value in before.items():
            self.assertEqual(after[ident], value)
        self.assertIn("원고에만 있는 장", response.stdout)
        self.assertEqual(list(after), ["cover", "cards", "quiz", "order", "detail-one", "detail-two", "quiz-two", "wrap", "old"])
        self.cli("scaffold", self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), result)

    def test_unclosed_existing_slide_is_not_overwritten(self):
        self.put_plan()
        path = self.root / "원고.html"
        original = '<html><body><section class="slide" data-id="cover">내용</body></html>'
        path.write_text(original, encoding="utf-8")
        self.cli("scaffold", self.root, expected=2)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_existing_crlf_slide_bytes_are_preserved(self):
        self.put_plan()
        self.cli("scaffold", self.root, "--only", "cover,wrap")
        path = self.root / "원고.html"
        original = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(original)
        text = original.decode("utf-8")
        before = self.slide_raws(text)
        self.cli("scaffold", self.root)
        updated = path.read_bytes().decode("utf-8")
        for ident, fragment in before.items():
            self.assertIn(fragment, updated)

    def test_force_only_and_force_all(self):
        self.put_plan()
        self.cli("scaffold", self.root)
        path = self.root / "원고.html"
        source = path.read_text(encoding="utf-8").replace("오늘의 정리", "수정 정리").replace("삼각형이 될까요?", "수정 문제")
        path.write_text(source, encoding="utf-8")
        before = self.slide_raws(source)
        self.cli("scaffold", self.root, "--force", "--only", "quiz")
        after = self.slide_raws(path.read_text(encoding="utf-8"))
        self.assertNotEqual(before["quiz"], after["quiz"])
        for ident in before.keys() - {"quiz"}:
            self.assertEqual(before[ident], after[ident])
        self.cli("scaffold", self.root, "--force")
        final = self.slide_raws(path.read_text(encoding="utf-8"))
        self.assertIn("오늘의 정리", final["wrap"])
        self.assertNotIn("수정 정리", final["wrap"])
        self.cli("scaffold", self.root, "--only", "없는-id", expected=2)

    def test_cli_plan_json_and_tool_failures(self):
        self.put_plan()
        report = self.cli("plan", self.root)
        self.assertIn("번호 | id", report.stdout)
        self.assertEqual(load(self.root / "검사" / "plan.json")["errors"], 0)
        plan = load(self.root / "콘티.json")
        plan["slides"][1]["cards"][0]["title"] = "너" * 20
        self.put_plan(plan)
        result = self.cli("plan", self.root, expected=1)
        self.assertIn("2번 장(cards)", result.stdout)
        self.assertIn("cards[1].title", result.stdout)
        self.cli("scaffold", self.root, expected=1)
        self.assertFalse((self.root / "원고.html").exists())
        (self.root / "콘티.json").write_text("{", encoding="utf-8")
        self.cli("plan", self.root, expected=2)

    def test_plan_reads_utf8_bom(self):
        plan = load(FIXTURES / '콘티.json')
        (self.root / '콘티.json').write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8-sig')
        result = self.cli('plan', self.root)
        self.assertIn('오류 0', result.stdout)

    def test_status_all_stages_and_freshness(self):
        self.put_plan()
        def state(expected=1):
            return json.loads(self.cli("status", self.root, "--json", expected=expected).stdout)
        def flags(result): return [entry["ok"] for entry in result["stages"]]
        initial = state()
        self.assertEqual(flags(initial), [False] * 8)
        script = (SKILL / "scripts" / "deck.py").as_posix()
        self.assertEqual(initial["next"], f'python -X utf8 "{script}" plan "{self.root.as_posix()}"')
        self.cli("plan", self.root)
        self.assertTrue(flags(state())[0])
        self.cli("scaffold", self.root)
        self.assertTrue(flags(state())[1])
        plan = load(self.root / "콘티.json")
        (self.root / "그림").mkdir()
        (self.root / "시안").mkdir()
        (self.root / "그림" / "mascot.png").write_bytes(b"master")
        for index, slide in enumerate(plan["slides"], 1):
            (self.root / "시안" / f'{index:02d}-{slide["id"]}.png').write_bytes(b"mockup")
        self.assertTrue(flags(state())[2])
        entries = {a["id"]: {"status": "ok"} for a in plan["assets"]}
        manifest = self.root / "그림" / "manifest.json"
        entries["leftover"] = {"status": "fail"}
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        self.assertFalse(flags(state())[3])
        entries["leftover"]["status"] = "ok"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        self.assertTrue(flags(state())[3])
        target = self.root / "삼각형 수업.html"
        target.write_text("<html><body>완성<script>const name = 'data-placeholder';</script></body></html>", encoding="utf-8")
        self.assertTrue(flags(state())[4])
        check = self.root / "검사" / "report.json"
        check.write_text(json.dumps({"slide_count": 8, "checked": list(range(1, 9)), "problems": [{"level": "error"}]}), encoding="utf-8")
        self.assertFalse(flags(state())[5])
        check.write_text(json.dumps({"slide_count": 8, "checked": [1], "problems": []}), encoding="utf-8")
        self.assertFalse(flags(state())[5])
        check.write_text(json.dumps({"slide_count": 8, "checked": list(range(1, 9)), "problems": [{"level": "warn"}]}), encoding="utf-8")
        self.assertTrue(flags(state())[5])
        compare = self.root / "검사" / "compare.json"
        compare.write_text(json.dumps({"slides": [{"id": "cover"}]}), encoding="utf-8")
        self.assertFalse(flags(state())[6])
        compare.write_text(json.dumps({"slides": [{"id": s["id"]} for s in plan["slides"]]}), encoding="utf-8")
        review = self.root / "검사" / "대조.md"
        review.write_text("최종: 통과\n", encoding="utf-8")
        self.assertFalse(flags(state())[6])
        review.write_text("\n".join(f'| {i} | {s["id"]} | 통과 |' for i, s in enumerate(plan["slides"], 1)) + "\n최종: 통과\n", encoding="utf-8")
        self.assertTrue(flags(state())[6])
        final = self.root / "검사" / "마감.md"
        final.write_text("판정: 통과\n완료", encoding="utf-8")
        self.assertEqual(flags(state(0)), [True] * 8)
        # 같은 내용이어도 새 검사/대조 뒤에는 사람의 옛 판정이 유효하지 않다. 동률도 통과하지 않는다.
        base = check.stat().st_mtime_ns
        def stamp(path, offset):
            value = base + offset * 1_000_000_000
            os.utime(path, ns=(value, value))
        stamp(compare, 2); stamp(review, 2); stamp(final, 3)
        result = state()
        self.assertEqual(flags(result)[-2:], [False, False])
        self.assertIn("대조.md가 새 대조보다 오래됨", result["stages"][-2]["reason"])
        self.assertIn(f'"{review.as_posix()}"', result["next"])
        self.assertIn("대조와-마감.md", result["next"])
        self.assertNotIn("notepad", result["next"])
        stamp(review, 4)
        result = state()
        self.assertEqual(flags(result)[-2:], [True, False])
        self.assertIn("최신 대조.md보다 오래됨", result["stages"][-1]["reason"])
        self.assertIn(f'"{final.as_posix()}"', result["next"])
        stamp(final, 5); stamp(check, 6)
        self.assertIn("새 검사보다 오래됨", state()["stages"][-1]["reason"])
        stamp(final, 7)
        self.assertEqual(flags(state(0)), [True] * 8)
        target.write_text('<img data-placeholder="">', encoding="utf-8")
        stamp(target, 8)
        self.assertIn("compare.json이 최신 빌드보다 오래됨", state()["stages"][-2]["reason"])
        self.assertFalse(flags(state())[4])
        self.assertFalse(flags(state())[5])
        self.assertFalse(flags(state())[6])
        self.assertFalse(flags(state())[7])
        result = self.cli("status", self.root, expected=1)
        self.assertIn("다음에 할 일", result.stdout)

    def test_status_shared_mockup_and_staleness(self):
        plan = load(FIXTURES / "콘티.json")
        plan["slides"][1]["mockup"] = "same-as:cover"
        self.put_plan(plan)
        self.cli("plan", self.root)
        self.cli("scaffold", self.root)
        (self.root / "그림").mkdir()
        (self.root / "시안").mkdir()
        (self.root / "그림" / "mascot.png").write_bytes(b"master")
        for i, slide in enumerate(plan["slides"], 1):
            if slide["id"] != "cards":
                (self.root / "시안" / f'{i:02d}-{slide["id"]}.png').write_bytes(b"image")
        result = json.loads(self.cli("status", self.root, "--json", expected=1).stdout)
        self.assertTrue(result["stages"][2]["ok"])
        report = self.root / "검사" / "plan.json"
        stale = (self.root / "콘티.json").stat().st_mtime_ns
        os.utime(report, ns=(stale, stale))
        result = json.loads(self.cli("status", self.root, "--json", expected=1).stdout)
        self.assertFalse(result["stages"][0]["ok"])

    def test_real_29_layouts_plan_then_scaffold(self):
        fixture = Path(__file__).resolve().parent / "fixtures-kit" / "콘티.json"
        plan = load(fixture)
        for slide in plan['slides']:
            if slide['id'] == 'ask':
                slide['mascot'] = 'mascot_think'
            elif slide['id'] == 'key':
                slide['mascot'] = 'mascot_cheer'
        plan['assets'].extend([{'id': 'mascot_think', 'kind': 'mascot', 'prompt': 'thinking'},
                               {'id': 'mascot_cheer', 'kind': 'mascot', 'prompt': 'cheering'}])
        (self.root / "콘티.json").write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
        real_kit = SKILL / "assets" / "kit"
        self.cli("plan", self.root, "--kit", real_kit)
        self.assertEqual(load(self.root / "검사" / "plan.json")["errors"], 0)
        self.cli("scaffold", self.root, "--kit", real_kit)
        content = (self.root / "원고.html").read_text(encoding="utf-8")
        doc, _, slides = deck.document_slides(content)
        self.assertEqual(len(slides), 29)
        self.assertEqual([(n["attrs"].get("data-id"), n["attrs"].get("data-layout")) for n in slides],
                         [(s["id"], s["layout"]) for s in plan["slides"]])
        self.assertNotIn("{{", content)
        self.assertNotIn("}}", content)
        self.assertNotRegex(content, r"--n\s*:\s*(?:;|[\"'])")
        html_node = next(n for n in doc.nodes if n["tag"] == "html")
        self.assertNotIn("data-look", html_node["attrs"])

    def test_new_uses_first_palette_and_first_wrap_layout(self):
        real_kit = SKILL / "assets" / "kit"
        env = dict(self.env, DECK_KIT=str(real_kit))
        folder = self.root / "기본 덱"
        self.cli("new", folder, "--title", "시험", env=env)
        plan = load(folder / "콘티.json")
        palettes = load(real_kit / "palettes.json")
        layouts = load(real_kit / "layouts.json")
        self.assertEqual(plan["meta"]["palette"], next(k for k in palettes if not k.startswith("_")))
        self.assertEqual(plan["slides"][-1]["layout"], next(k for k, v in layouts.items() if v.get("group") == "wrap"))

    def test_layouts_catalog_and_details_are_available_as_text_and_json(self):
        catalog = self.cli("layouts").stdout
        self.assertIn("이름(키)", catalog)
        self.assertIn("표지(cover)", catalog)
        data = json.loads(self.cli("layouts", "--json").stdout)
        self.assertEqual([entry["key"] for entry in data["layouts"]], ["cover", "cards", "detail", "order", "quiz", "wrap"])
        details = self.cli("layouts", "cards,quiz").stdout
        self.assertIn("cards[].title", details)
        self.assertIn("cards[].desc", details)
        self.assertIn("2~4", details)
        data = json.loads(self.cli("layouts", "cards,quiz", "--json").stdout)
        cards = data["layouts"][0]
        self.assertEqual(cards["key"], "cards")
        fields = {item["path"]: item for item in cards["fields"]}
        self.assertTrue(fields["cards[].title"]["required"])
        self.assertEqual(fields["cards"]["min"], 2)
        example = cards["example"]
        self.assertEqual(example["layout"], "cards")
        self.assertGreaterEqual(len(example["cards"]), 2)
        self.assertIsInstance(example["cards"][0]["title"], str)
        self.assertIsInstance(example["config"]["show"], bool)
        self.assertIsInstance(example["config"]["count"], (int, float))
        self.assertEqual(example["config"]["mode"], "a")
        self.assertEqual(example["config"]["raw"], "…")
        command = [sys.executable, "-B", str(SKILL / "scripts" / "deck.py"), "layouts", "cards", "--kit", str(FIXTURES), "--json"]
        self.assertEqual(json.loads(subprocess.run(command, env=self.env, capture_output=True, text=True, encoding="utf-8", check=True).stdout)["layouts"][0]["key"], "cards")
        error = self.cli("layouts", "card", expected=2)
        self.assertIn("cards", error.stderr)
        self.assertIn("오류", error.stderr)

    def test_output_encoding_remains_utf8_in_cp949_environment(self):
        self.put_plan()
        env = self.env.copy()
        env["PYTHONIOENCODING"] = "cp949"
        result = self.cli("plan", self.root, env=env)
        self.assertIn("삼각형", result.stdout)
        self.assertFalse((self.root / "검사" / "plan.json").read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
