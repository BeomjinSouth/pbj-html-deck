"""그림 생성·시안 대조의 외부 호출을 가짜 렌더러로 검증한다."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[2]
SCRIPT = SKILL / "scripts" / "deck.py"
FAKE = Path(__file__).resolve().parent / "fake-assets"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))
from cdp import find_browser, BrowserError


def has_browser():
    try:
        return bool(find_browser())
    except BrowserError:
        return False


@unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 없습니다")
class ArtTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="그림 시험 ", dir=os.environ.get("DECK_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "공용 자산"
        shutil.copytree(FAKE, self.assets)
        shutil.copytree(SKILL / "assets" / "fonts", self.assets / "fonts")
        shutil.copytree(SKILL / "assets" / "prompts", self.assets / "prompts")
        (self.assets / "kit" / "layouts.json").write_text(json.dumps({"cover": {"mockup": "Large headline and one sticker"}}, ensure_ascii=False), encoding="utf-8")
        (self.assets / "kit" / "palettes.json").write_text(json.dumps({"warm": {"accent": "#F5A9B8", "background": "#FFF8EC"}}), encoding="utf-8")
        self.plan = {"meta": {"title": "그림 시험", "palette": "warm"},
                     "mascot": {"id": "mascot", "look": "a friendly triangle"},
                     "slides": [{"id": "cover", "layout": "cover", "purpose": "삼각형을 살펴본다", "focus": "큰 제목", "title": "삼각형", "image": "pencil"},
                                {"id": "again", "layout": "cover", "purpose": "같은 시안을 사용한다", "focus": "그림", "mockup": "same-as:cover", "mascot": "wave"}],
                     "assets": [{"id": "wave", "kind": "mascot", "prompt": "waving"},
                                {"id": "pencil", "kind": "sticker", "prompt": "a yellow pencil"}]}
        (self.root / "콘티.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        (self.root / "원고.html").write_text('<!doctype html><html lang="ko"><head><title>그림 시험</title></head><body>'
            '<section class="slide" data-id="cover" data-layout="cover"><h1>삼각형</h1><img class="asset" data-asset="pencil" src="그림/pencil.png" alt="연필"></section>'
            '<section class="slide" data-id="again" data-layout="cover"><h1>다시</h1></section></body></html>', encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(DECK_ART_FAKE="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        self.env.pop("IMPECCABLE_BIN", None)
        for key in ("TEMP", "TMP", "TMPDIR"):
            self.env[key] = str(self.root)

    def run_deck(self, *args, expected=0):
        wrapper = "import sys;sys.path.insert(0,sys.argv.pop(1));import deck;deck.find_impeccable=lambda:None;sys.exit(deck.main())"
        result = subprocess.run([sys.executable, "-B", "-c", wrapper, str(SKILL / "scripts"), *map(str, args), "--assets", str(self.assets)],
                                capture_output=True, encoding="utf-8", text=True, env=self.env, timeout=180)
        self.assertEqual(result.returncode, expected, f"{args}\n{result.stdout}\n{result.stderr}")
        return result

    def manifest(self, folder):
        return json.loads((self.root / folder / "manifest.json").read_text(encoding="utf-8"))

    def test_dry_run_and_fake_mascot_assets_import(self):
        result = self.run_deck("art", self.root, "mascot", "--dry-run")
        self.assertIn("friendly triangle", result.stdout)
        self.assertIn("#F5A9B8", result.stdout)
        self.assertIn("첨부", result.stdout)
        prompt = (self.root / "그림/prompts/mascot.txt").read_text(encoding="utf-8")
        self.assertIn("첨부: []", prompt)
        self.assertIn("friendly triangle", prompt)
        self.assertFalse((self.root / "그림/mascot.png").exists())
        self.run_deck("art", self.root, "mascot")
        manifest = self.manifest("그림")
        self.assertEqual(manifest["mascot"]["version"], "v001")
        self.assertEqual(manifest["mascot"]["status"], "ok")
        result = self.run_deck("art", self.root, "mascot")
        self.assertIn("건너뜀", result.stdout)
        self.assertEqual(self.manifest("그림")["mascot"]["version"], "v001")
        self.run_deck("art", self.root, "mascot", "--force")
        self.assertEqual(self.manifest("그림")["mascot"]["version"], "v002")
        self.run_deck("art", self.root, "assets", "--only", "pencil")
        self.assertTrue((self.root / "그림/pencil.png").is_file())
        self.assertFalse((self.root / "그림/wave.png").exists())
        self.assertTrue((self.root / "그림/_sheet.png").is_file())
        self.run_deck("art", self.root, "assets", "--only", "wave")
        self.assertEqual(self.manifest("그림")["wave"]["status"], "ok")
        self.run_deck("art", self.root, "import", "pencil", self.root / "그림/mascot.png")
        self.assertTrue(self.manifest("그림")["pencil"]["imported"])
        self.assertEqual(self.manifest("그림")["pencil"]["version"], "v002")
        self.assertTrue((self.root / "그림/raw/pencil/v002.png").is_file())

    def test_mockups_compare_and_review_preservation(self):
        self.run_deck("art", self.root, "mascot")
        result = self.run_deck("art", self.root, "mockups", "--dry-run")
        self.assertIn("Large headline", result.stdout)
        self.assertIn("capture-01.png", result.stdout)
        self.assertIn('- Title: "삼각형"', result.stdout)
        self.assertNotIn('{"title"', result.stdout)
        self.assertIn('placeholder box captioned "pencil"', result.stdout)
        self.assertFalse((self.root / "시안/01-cover.png").exists())
        composed = self.run_deck("art", self.root, "mockups", "--mode", "compose", "--dry-run")
        self.assertIn("board-warm.png", composed.stdout)
        self.assertIn("- A sticker: a yellow pencil", composed.stdout)
        self.assertNotIn("placeholder box", composed.stdout)
        self.assertNotIn("capture-01.png", composed.stdout.split("[cover] 첨부:", 1)[1])
        self.run_deck("art", self.root, "mockups")
        mock = self.root / "시안/01-cover.png"
        self.assertTrue(mock.is_file())
        again = self.run_deck("art", self.root, "mockups")
        self.assertIn("건너뜀", again.stdout)
        self.assertEqual(self.manifest("시안")["cover"]["version"], "v001")
        self.assertEqual(self.manifest("시안")["cover"]["shown"], ["pencil"])
        self.assertFalse((self.root / "시안/02-again.png").exists())
        self.assertEqual(struct.unpack(">II", mock.read_bytes()[16:24]), (1920, 1080))
        self.assertTrue((self.root / "시안/_sheet.png").exists())
        # 에셋의 참고 그림: 그 그림이 그려진 시안은 그대로, 그려지지 않은 시안은 화풍 참고로만 준다
        import deck_art
        jobs = {job["id"]: job for job in deck_art.asset_jobs(self.root, deck_art.plan_data(self.root), self.assets, set())}
        self.assertEqual(jobs["pencil"]["roles"], [deck_art.ROLE_SLIDE])
        self.assertEqual(jobs["wave"]["roles"], [deck_art.ROLE_MASTER, deck_art.ROLE_STYLE])
        self.assertEqual([path.name for path in jobs["wave"]["refs"]], ["mascot.png", "01-cover.png"])
        # 에셋을 만든 뒤 다시 뽑는 시안은 자리표시가 아니라 이미 들어간 그림을 보게 된다
        self.run_deck("art", self.root, "assets", "--only", "pencil")
        redo = self.run_deck("art", self.root, "mockups", "--dry-run")
        self.assertIn("already drawn in the screenshot", redo.stdout)
        self.assertNotIn("placeholder box captioned", redo.stdout)
        self.run_deck("art", self.root, "import", "mockup:cover", mock)
        self.assertEqual(self.manifest("시안")["cover"]["version"], "v002")
        self.run_deck("build", self.root)
        self.run_deck("compare", self.root)
        out = self.root / "검사"
        rows = json.loads((out / "compare.json").read_text(encoding="utf-8"))["slides"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(isinstance(row["overall"], (int, float)) for row in rows))
        self.assertEqual(struct.unpack(">II", (out / "compare-01.png").read_bytes()[16:24]), (1920, 540))
        review = out / "대조.md"
        self.assertIn("| 02 | again |", review.read_text(encoding="utf-8"))
        review.write_text("사람이 쓴 판정", encoding="utf-8")
        self.run_deck("compare", self.root, "--slides", "1")
        self.assertEqual(review.read_text(encoding="utf-8"), "사람이 쓴 판정")
        wrapper = "import sys;sys.path.insert(0,sys.argv.pop(1));import deck;deck.find_impeccable=lambda:None;sys.exit(deck.main())"
        no_pillow = subprocess.run([sys.executable, "-S", "-B", "-c", wrapper, str(SKILL / "scripts"),
                                    "compare", str(self.root), "--slides", "1", "--assets", str(self.assets)],
                                   capture_output=True, encoding="utf-8", text=True, env=self.env, timeout=120)
        self.assertEqual(no_pillow.returncode, 0, no_pillow.stderr)
        self.assertIn("점수 없음", no_pillow.stdout)
        self.assertEqual(struct.unpack(">II", (out / "compare-01.png").read_bytes()[16:24]), (1920, 540))
        self.assertEqual(review.read_text(encoding="utf-8"), "사람이 쓴 판정")

    def test_check_folder_selftest_and_plan_mismatch(self):
        engine = self.assets / "engine" / "test-selftest.js"
        engine.write_text("window.deck.selftest=async(i,o)=>{document.querySelectorAll('.slide')[i].setAttribute('data-tested','yes');return []};window.deck.reset=()=>{};", encoding="utf-8")
        source = self.root / "원고.html"
        source.write_text(source.read_text(encoding="utf-8").replace('<h1>삼각형</h1>', '<h1>삼각형</h1><button data-timer="1">시작</button>'), encoding="utf-8")
        self.run_deck("build", self.root)
        self.run_deck("check", self.root, "--slides", "1")
        report = json.loads((self.root / "검사/report.json").read_text(encoding="utf-8"))
        self.assertIn("state-01.png", report["files"])
        self.assertTrue((self.root / "검사/state-01.png").exists())
        self.plan["slides"][1]["layout"] = "changed"
        (self.root / "콘티.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        result = self.run_deck("check", self.root, "--slides", "1", expected=1)
        self.assertIn("plan-mismatch", result.stdout)

    def test_explicit_master_alias_is_one_render(self):
        self.plan["assets"].append({"id": "master-alias", "kind": "mascot_master", "prompt": "ignored; use mascot.look"})
        self.plan["assets"][1]["ref"] = ["master-alias"]
        (self.root / "콘티.json").write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")
        self.run_deck("art", self.root, "mascot")
        manifest = self.manifest("그림")
        self.assertEqual(manifest["master-alias"]["alias_of"], "mascot")
        self.assertEqual(manifest["master-alias"]["thread_id"], manifest["mascot"]["thread_id"])
        self.assertTrue((self.root / "그림/master-alias.png").is_file())
        self.run_deck("art", self.root, "assets", "--only", "pencil")
        self.assertEqual(self.manifest("그림")["pencil"]["status"], "ok")

    def test_fake_sticker_without_pillow(self):
        wrapper = "import sys;sys.path.insert(0,sys.argv.pop(1));import deck;deck.find_impeccable=lambda:None;sys.exit(deck.main())"
        result = subprocess.run([sys.executable, "-S", "-B", "-c", wrapper, str(SKILL / "scripts"),
                                 "art", str(self.root), "assets", "--only", "pencil", "--assets", str(self.assets)],
                                capture_output=True, encoding="utf-8", text=True, env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        image = self.root / "그림/pencil.png"
        # 원본 800px에서 투명 여백을 다듬으면 636px이다. 작은 그림을 강제로 확대하지 않는다.
        self.assertEqual(max(struct.unpack(">II", image.read_bytes()[16:24])), 636)
        self.assertGreater(self.manifest("그림")["pencil"]["transparent_ratio"], 0)

    def test_sticker_uses_800px_limit_with_and_without_pillow(self):
        import deck_art
        source = self.root / "large.png"
        source.write_bytes(deck_art.png_solid(1600, 1200, transparent=True))
        self.run_deck("art", self.root, "import", "pencil", source)
        image = self.root / "그림/pencil.png"
        self.assertEqual(max(struct.unpack(">II", image.read_bytes()[16:24])), 800)
        self.assertEqual(max(self.manifest("그림")["pencil"]["size"]), 800)
        wrapper = "import sys;sys.path.insert(0,sys.argv.pop(1));import deck;deck.find_impeccable=lambda:None;sys.exit(deck.main())"
        result = subprocess.run([sys.executable, "-S", "-B", "-c", wrapper, str(SKILL / "scripts"),
                                 "art", str(self.root), "import", "pencil", str(source), "--assets", str(self.assets)],
                                capture_output=True, encoding="utf-8", text=True, env=self.env, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(max(struct.unpack(">II", image.read_bytes()[16:24])), 800)
        self.assertEqual(max(self.manifest("그림")["pencil"]["size"]), 800)

    def test_reject_unknown_and_wrong_import(self):
        self.run_deck("art", self.root, "assets", "--only", "missing", expected=2)
        self.run_deck("art", self.root, "import", "missing", self.root / "nothing.png", expected=2)
        self.run_deck("art", self.root, "mascot", "--jobs", "0", expected=2)


class PromptTextTest(unittest.TestCase):
    """시안 지시문의 글자·그림 목록. 화면에 처음부터 보이는 것만, 읽기 좋은 줄로 적는지 본다."""

    @classmethod
    def setUpClass(cls):
        import deck_art
        cls.art = deck_art

    def test_quiz_lists_visible_text_only(self):
        entry = {"fields": {"title": {"type": "text"}, "hint": {"type": "text"}, "mode": {"type": "choice"},
                            "options": {"type": "list", "of": {"text": {"type": "text"}, "correct": {"type": "bool"}}},
                            "explain": {"type": "text"}, "cheer": {"type": "text"}, "retry": {"type": "text"}}}
        slide = {"id": "quiz", "layout": "quiz", "purpose": "점을 고른다", "focus": "보기", "tag": "확인 문제",
                 "title": "$y = 3x$의 그래프가\n지나는 **점**은?", "hint": "*x*에 수를 넣어 봐요", "mode": "once",
                 "options": [{"text": "$(1, 3)$", "correct": True}, {"text": "$(-1, 3)$"}],
                 "explain": "해설은 숨김", "cheer": "맞았어", "retry": "다시", "talk": "교사 메모",
                 "mascot": "host", "say": ["골라 봐!", "둘째 대사"]}
        text = self.art.visible_texts(slide, entry)
        self.assertEqual(text.splitlines(), [
            '- Tag pill: "확인 문제"',
            '- Title: "y = 3x의 그래프가" / "지나는 점은?" (2 lines, broken exactly here) — highlighted: "점"',
            '- Hint chip: "x에 수를 넣어 봐요"',
            '- Answer button A: "(1, 3)"',
            '- Answer button B: "(−1, 3)"',
            '- Mascot speech bubble: "골라 봐!"'])
        for hidden in ("해설은 숨김", "맞았어", "다시", "교사 메모", "True", "once", "둘째 대사"):
            self.assertNotIn(hidden, text)

    def test_live_values_blanks_tables_and_tabs(self):
        entry = {"fields": {"title": {"type": "text"}, "state": {"type": "json"}, "stage": {"type": "raw"},
                            "controls": {"type": "list", "of": {"label": {"type": "text"}, "var": {"type": "raw"},
                                                                 "min": {"type": "number"}, "max": {"type": "number"},
                                                                 "value": {"type": "number"}, "unit": {"type": "text"}}},
                            "formula": {"type": "text"}, "readouts": {"type": "list", "of": {"type": "text"}},
                            "status": {"type": "list", "of": {"if": {"type": "raw"}, "text": {"type": "text"}}},
                            "head": {"type": "list", "of": {"type": "text"}},
                            "rows": {"type": "list", "of": {"cells": {"type": "list", "of": {"type": "text"}}, "hl": {"type": "bool"}}},
                            "tabs": {"type": "list", "of": {"label": {"type": "text"}, "heading": {"type": "text"},
                                                             "lines": {"type": "list", "of": {"type": "text"}}}},
                            "frame": {"type": "text"}, "timer": {"type": "number"}}}
        slide = {"id": "lab", "layout": "lab", "title": "값을 바꿔요", "state": {"a": 2},
                 "stage": "<svg><text>그림 속 글자</text></svg>",
                 "controls": [{"label": "*a*", "var": "a", "min": -3, "max": 3, "value": 2, "unit": "cm"}],
                 "formula": r"$y = \val{a}x^2$", "readouts": ["{a} 와 7"],
                 "status": [{"if": "a > 5", "text": "크다"}, {"if": "a <= 5", "text": "작다"}],
                 "head": ["*x*", "*y*"], "rows": [{"cells": ["$-2$", "$-4$"], "hl": True}],
                 "tabs": [{"label": "하나", "heading": "첫 탭", "lines": ["보이는 줄"]},
                          {"label": "둘", "heading": "가린 탭", "lines": ["가린 줄"]}],
                 "frame": "두 변의 [[합]]보다 짧다", "timer": 90, "mascot": "m", "say": ["처음", "마지막"]}
        lines = self.art.visible_texts(slide, entry).splitlines()
        for expected in ('- Slider label: "a"', '- Slider value box: "2"', '- Slider unit: "cm"',
                         '- Formula readout: "y = 2x²"', '- Readout row: "2 와 7"', '- Status badge: "작다"',
                         '- Table header: "x" | "y"', '- Table row: "−2" | "−4"', '- Tab 1 label: "하나"',
                         '- Tab 2 label: "둘"', '- Open tab heading: "첫 탭"', '- Open tab line: "보이는 줄"',
                         '- Sentence frame: "두 변의 ____보다 짧다"', '- Timer button: "1:30"',
                         '- Mascot speech bubble: "마지막"'):
            self.assertIn(expected, lines)
        text = "\n".join(lines)
        for hidden in ("그림 속 글자", "크다", "가린 탭", "가린 줄", "합", "처음", "-3"):
            self.assertNotIn(hidden, text)

    def test_summary_all_uses_first_speech_but_explicit_step_bubbles_keep_the_last(self):
        import deck_plan
        layouts = deck_plan.read_json(SKILL / "assets/kit/layouts.json")
        slide = {"mascot": "m", "say": ["첫 대사", "마지막 대사"]}
        for layout, mode, expected in [("summary", "all", "첫 대사"), ("summary", "step", "마지막 대사"),
                                       ("ask", "all", "마지막 대사"), ("quiz", "all", "첫 대사")]:
            with self.subTest(layout=layout, mode=mode):
                text = self.art.visible_texts(dict(slide, layout=layout, reveal=mode), layouts[layout])
                self.assertEqual([line for line in text.splitlines() if "speech bubble" in line],
                                 [f'- Mascot speech bubble: "{expected}"'])

    def test_unknown_layout_falls_back_to_plain_fields(self):
        slide = {"id": "x", "layout": "?", "title": "제목", "body": "본문", "draw": "<svg/>", "answer": "3"}
        self.assertEqual(self.art.visible_texts(slide, {}).splitlines(), ['- Title: "제목"', '- body: "본문"'])

    def test_illustrations_follow_what_the_capture_shows(self):
        plan = {"mascot": {"id": "mascot"},
                "assets": [{"id": "pose", "kind": "mascot", "prompt": "waving", "alt": "손 흔드는 모습"},
                           {"id": "late", "kind": "sticker", "prompt": "a star"}]}
        slide = {"id": "s", "mascot": "pose", "steps": [{"image": "late"}]}
        both = self.art.illustration_lines(plan, slide)
        self.assertIn('placeholder box captioned "손 흔드는 모습"): waving', both)
        self.assertIn("a star", both)
        shown = self.art.illustration_lines(plan, slide, {"pose": "drawn"})
        self.assertIn("already drawn in the screenshot", shown)
        self.assertNotIn("a star", shown)
        self.assertIn("none", self.art.illustration_lines(plan, slide, {}))

    def test_render_message_names_each_attachment(self):
        message = self.art.render_message("PROMPT BODY\n", "mockup", [self.art.ROLE_CAPTURE, self.art.ROLE_MASTER], 2)
        self.assertIn("16:9 landscape canvas", message)
        self.assertIn("Attached image 1 is a browser screenshot", message)
        self.assertIn("Attached image 2 is the mascot character sheet", message)
        self.assertTrue(message.endswith("<<<PROMPT\nPROMPT BODY\nPROMPT>>>"))
        sticker = self.art.render_message("X\n", "sticker", [], 0)
        self.assertIn("transparent background", sticker)
        self.assertNotIn("Attached image", sticker)

    def test_prompt_templates_fill_every_slot(self):
        keys = ("STYLE", "PALETTE", "SUBJECT", "PURPOSE", "FOCUS", "LAYOUT", "TEXTS", "ILLUSTRATIONS", "NOTE")
        for path in sorted((SKILL / "assets" / "prompts").glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Draft template", text, path.name)
            filled = text.format_map({key: "<" + key + ">" for key in keys})      # 모르는 자리 이름이면 KeyError
            if path.name.startswith("mockup-"):
                for key in ("PALETTE", "STYLE", "LAYOUT", "TEXTS", "ILLUSTRATIONS", "PURPOSE", "FOCUS", "NOTE"):
                    self.assertIn("<" + key + ">", filled, path.name)
            elif path.name.startswith("asset-"):
                for key in ("SUBJECT", "STYLE", "TEXTS"):
                    self.assertIn("<" + key + ">", filled, path.name)


if __name__ == "__main__":
    unittest.main()
