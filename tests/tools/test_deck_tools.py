"""CLI 회귀 시험. 브라우저가 있으면 CDP를 실제로 실행한다."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

SKILL = Path(__file__).resolve().parents[2]
SCRIPT = SKILL / "scripts" / "deck.py"
ASSETS = Path(__file__).resolve().parent / "fake-assets"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SKILL / "scripts"))
from cdp import find_browser, BrowserError, Browser  # noqa: E402
import deck  # noqa: E402
from deck import Document, slides_of, font_characters, subset_font, TTFont  # noqa: E402

TEST_FONT = Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
FONT_TESTS = TTFont is not None and TEST_FONT.is_file()

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/6b0AAAAASUVORK5CYII=")


def has_browser():
    try:
        return bool(find_browser())
    except BrowserError:
        return False


def slide_text(text):
    doc = Document(text)
    return [doc.raw(node) for node in slides_of(doc)]


def slide_structure(text):
    # PNG를 더 작은 WebP로 바꾸는 것은 3차의 의도된 차이다.
    return [re.sub(r'data:image/(?:png|webp);base64,[A-Za-z0-9+/=]+', 'data:image/IMAGE', slide)
            for slide in slide_text(text)]


def basefont_data(text):
    doc = Document(text)
    blocks = [n for n in doc.nodes if n.tag == "style" and n.attrs.get("data-deck") == "basefont"]
    if len(blocks) != 1:
        raise AssertionError(f"기본 글꼴 블록 수: {len(blocks)}")
    css = doc.inner(blocks[0])
    match = re.search(r"data:font/woff;base64,([A-Za-z0-9+/=]+)", css)
    if not match:
        raise AssertionError("WOFF data URL이 없습니다")
    return css, base64.b64decode(match.group(1))


class DeckToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="덱 시험 ", dir=os.environ.get("DECK_TEST_TMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["PYTHONIOENCODING"] = "utf-8"
        # 설치된 임페커블은 CLI 래퍼에서 모의 처리하고 실제 사용자 프로필은 유지한다.
        self.env.pop("IMPECCABLE_BIN", None)
        for key in ("TEMP", "TMP", "TMPDIR"):
            self.env[key] = str(self.root)

    def run_deck(self, *args, expected=0, assets=ASSETS, python_flags=()):
        args = list(args)
        if args and args[0] == "build" and Path(assets) != deck.ASSETS and "--no-font" not in args:
            args.append("--no-font")
        wrapper = "import sys;sys.path.insert(0,sys.argv.pop(1));import deck;deck.find_impeccable=lambda:None;sys.exit(deck.main())"
        command = [sys.executable, *python_flags, "-B", "-c", wrapper, str(SKILL / "scripts"), *map(str, args), "--assets", str(assets)]
        env = self.env.copy()
        if "-S" in python_flags:
            env.pop("PYTHONPATH", None)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        self.assertEqual(result.returncode, expected, f"명령: {args}\n표준 출력: {result.stdout}\n표준 오류: {result.stderr}")
        return result

    def source(self, body='<section class="slide"><h1>첫 장</h1></section>', head="", title="한글 제목", attrs='lang="ko"', body_attrs=""):
        path = self.root / "원고.html"
        path.write_text(f'<!doctype html>\n<html {attrs}><head><meta charset="utf-8"><title>{title}</title>{head}</head>\n<body {body_attrs}>{body}</body></html>', encoding="utf-8")
        return path

    def build(self, source=None, name="결과.html", *extra):
        source = source or self.source()
        output = self.root / name
        result = self.run_deck("build", source, "-o", output, *extra)
        return output, result

    def test_help_and_build(self):
        for args in ((), ("build",), ("check",), ("unpack",), ("art",), ("compare",)):
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), *args, "--help"],
                                    capture_output=True, text=True, encoding="utf-8", env=self.env)
            self.assertEqual(result.returncode, 0)
            self.assertIn("사용법:", result.stdout)
            self.assertNotIn("--assets", result.stdout)
            if args == ("build",):
                self.assertNotIn("--font ", result.stdout)
                self.assertIn("--no-font", result.stdout)
                self.assertIn("--student", result.stdout)
        source = self.source(title='삼각형 &amp; &quot;도형&quot;')
        result = self.run_deck("build", source)
        output = self.root / '삼각형 & _도형_.html'
        self.assertTrue(output.is_file(), result.stdout)
        text = output.read_text(encoding="utf-8")
        for token in ('class="viewer"', '<main id="deck">', 'data-deck="kit"',
                      'data-deck="engine"', 'data-deck="fonts"', 'data-slide', 'data-theme="light"'):
            self.assertIn(token, text)
        for asset in (ASSETS / "engine/deck.css", ASSETS / "engine/deck.js", ASSETS / "kit/kit.css"):
            self.assertIn(asset.read_text(encoding="utf-8"), text)
        self.assertNotIn(b'\r\n', output.read_bytes())
        self.assertFalse(output.read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_inlines_resources_styles_and_script(self):
        (self.root / "그림 파일.png").write_bytes(PNG)
        (self.root / "글꼴.woff2").write_bytes(b"fake-font")
        (self.root / "동작.js").write_text("const 말 = '</script>';", encoding="utf-8")
        (self.root / "외부.css").write_text('@font-face{src:url("글꼴.woff2")} .x{background:url("그림 파일.png")}', encoding="utf-8")
        body = '<section class="slide" style="background:url(\'그림 파일.png\')"><img src="그림 파일.png"><svg><image href="그림 파일.png"/></svg></section><script src="동작.js"></script>'
        source = self.source(body, '<link rel="stylesheet" href="외부.css">')
        output, _ = self.build(source)
        text = output.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("data:image/png;base64,"), 4)
        self.assertIn("data:font/woff2;base64,", text)
        self.assertIn('data-deck-src="그림 파일.png"', text)
        self.assertIn("<\\/script>", text)
        self.assertNotIn('src="동작.js"', text)

    def test_linked_css_uses_stylesheet_folder_and_large_image_warns(self):
        (self.root / "스타일").mkdir()
        (self.root / "스타일" / "테마.css").write_text('.slide{background:url(그림.png)}', encoding="utf-8")
        (self.root / "스타일" / "그림.png").write_bytes(b'x' * (1572864 + 1))
        source = self.source(head='<link rel="stylesheet" href="스타일/테마.css">')
        output, result = self.build(source)
        self.assertIn("1.5MB", result.stdout)
        self.assertIn('data:image/png;base64,', output.read_text(encoding="utf-8"))

    def test_missing_file_and_forbidden_styles(self):
        cases = [
            ('<section class="slide"><img src="없음.png"></section>', "", "1번 장", "없음.png"),
            ('<section class="slide">한 장</section>', '<link rel="stylesheet" href="https://example.invalid/a.css">', "원격", "스타일시트"),
            ('<section class="slide">한 장</section><script src="https://example.invalid/a.js"></script>', "", "원격", "스크립트"),
            ('<section class="slide">한 장</section>', '<style>@import "a.css";</style>', "@import", "CSS"),
            ('<div>장 없음</div>', "", "장이 없습니다", "section.slide"),
            ('<section class="slide"><section class="slide">중첩</section></section>', "", "1번 장", "중첩"),
        ]
        for body, head, *expected in cases:
            with self.subTest(body=body, head=head):
                source = self.source(body, head)
                result = self.run_deck("build", source, "-o", self.root / "out.html", expected=2)
                for term in expected:
                    self.assertIn(term, result.stderr)
                self.assertFalse((self.root / "out.html").exists())

    def test_original_markup_css_comments_and_escaped_path(self):
        (self.root / "a&amp;b.png").write_bytes(PNG)
        untouched = "<p style='color:red; background:url( #x )'>그대로 &amp; 유지</p>"
        style = '<style>/* @import "없는.css"; url(없는.png) */ .x::before{content:"url(없는.png)"}</style>'
        body = "<section class='slide' data-slide data-theme='dark'>" + untouched + style + '<img src="a&amp;amp;b.png"></section>'
        source = self.source(body, attrs="lang='ko' style='--accent:red;'")
        output, result = self.build(source)
        text = output.read_text(encoding="utf-8")
        self.assertIn(untouched, text)
        self.assertIn(style, text)
        self.assertIn("style='--accent:red;'", text.split('<head>')[0])
        self.assertIn('data-deck-src="a&amp;amp;b.png"', text)
        self.assertIn("경고 0건", result.stdout)

    def test_media_with_two_original_names_round_trips(self):
        (self.root / "동영상.mp4").write_bytes("동영상 데이터".encode("utf-8"))
        (self.root / "표지.png").write_bytes(PNG)
        source = self.source('<section class="slide"><video src="동영상.mp4" poster="표지.png"></video></section>')
        output, _ = self.build(source)
        first = output.read_text(encoding="utf-8")
        self.assertIn("data-deck-src", first)
        folder = self.root / "영상 풀기"
        self.run_deck("unpack", output, "-o", folder)
        self.assertTrue((folder / "자료" / "동영상.mp4").is_file())
        self.assertTrue((folder / "그림" / "표지.png").is_file())
        self.run_deck("build", folder / "원고.html", "-o", folder / "결과.html")
        second = (folder / "결과.html").read_text(encoding="utf-8")
        self.assertEqual(slide_structure(first), slide_structure(second))

    def test_missing_slide_script_reports_slide_and_path(self):
        source = self.source('<section class="slide"><script src="없는.js"></script></section>')
        result = self.run_deck("build", source, "-o", self.root / "결과.html", expected=2)
        self.assertIn("1번 장", result.stderr)
        self.assertIn("없는.js", result.stderr)

    def test_missing_engine_asset(self):
        assets = self.root / "빈 자산"
        assets.mkdir()
        source = self.source()
        command = [sys.executable, "-B", str(SCRIPT), "build", str(source), "--assets", str(assets)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("kit.css", result.stderr)

    def test_warning_types_and_original_slide_markup(self):
        head = '<style>.a{width:10vw;position:fixed;& .b{color:red}} @scope (.slide) { p{color:red} }</style>'
        body = '<section class="slide"><h1 id="same">원문 &amp; 글</h1><form></form><img src="https://example.invalid/a.png"></section><section class="slide"><p id="same">둘</p></section>'
        output, result = self.build(self.source(body, head))
        for term in ("vw/vh/cqw", "position:fixed", "중첩", "@scope", "form", "중복", "외부 자원"):
            self.assertIn(term, result.stdout)
        self.assertIn('<h1 id="same">원문 &amp; 글</h1>', output.read_text(encoding="utf-8"))

    def test_page_numbers_and_theme_default(self):
        body = ('<section class="slide" data-deck-page="7"><h1>하나</h1></section>'
                '<section class="slide"><h2>둘</h2></section>'
                "<section data-deck-page='1' class='slide'><h2>셋</h2></section>"
                '<section class="slide"><h2>첫 줄<br>둘째 줄<br/> 셋째 줄</h2><p>본문<br>그대로</p></section>')
        output, _ = self.build(self.source(body))
        text = output.read_text(encoding="utf-8")
        self.assertIn('<h2>첫 줄<br>\n둘째 줄<br/> 셋째 줄</h2><p>본문<br>그대로</p>', text)
        self.assertIn('<html lang="ko" data-theme="light">', text)
        self.assertIn('<section class="slide" data-deck-page="1" data-slide><h1>하나</h1>', text)
        self.assertIn('<section class="slide" data-slide data-deck-page="2"><h2>둘</h2>', text)
        self.assertIn("<section data-deck-page='3' class='slide' data-slide><h2>셋</h2>", text)
        self.assertNotRegex(text, r'<section[^>]*data-theme')
        dark = self.source('<section class="slide"><h1>밤</h1></section>', attrs='lang="ko" data-theme="dark"')
        output, _ = self.build(dark)
        text = output.read_text(encoding="utf-8")
        self.assertIn('<html lang="ko" data-theme="dark">', text)
        self.assertNotIn('data-theme="light"', text.split('<head>')[0])

    def test_rebuild_editor_serialization_and_saved_protection(self):
        source = self.source('<section class="slide"><h1>내용</h1></section>', '<style>.slide{color:red}</style>')
        output, _ = self.build(source)
        text = output.read_text(encoding="utf-8")
        marked = '<html lang="ko" data-theme="light" class="custom deck-on" data-deck-mode="edit" style="--bar:44px;--panel:340px;--accent:red">'
        self.assertIn('<html lang="ko" data-theme="light">', text)
        text = text.replace('<html lang="ko" data-theme="light">', marked)
        text = text.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="deck-saved" content="2026-01-01">', 1)
        text = text.replace('<div class="viewer">', '<div class="viewer"><div data-deck-rt=""><p>런타임</p></div>')
        output.write_text(text, encoding="utf-8")
        blocked = self.run_deck("build", source, "-o", output, expected=2)
        self.assertIn("unpack", blocked.stderr)
        self.run_deck("build", output, "-o", output)
        rebuilt = output.read_text(encoding="utf-8")
        self.assertEqual(rebuilt.count('data-deck="kit"'), 1)
        self.assertEqual(rebuilt.count('data-deck="engine"'), 2)
        self.assertIn('name="deck-saved"', rebuilt)
        self.assertIn('class="custom"', rebuilt)
        self.assertIn('--accent:red', rebuilt)
        self.assertNotIn('data-deck-mode', rebuilt.split('<head>')[0])
        self.assertNotIn('--bar', rebuilt.split('<head>')[0])
        self.assertNotIn('런타임', rebuilt)
        self.run_deck("build", source, "-o", output, "--force")
        self.run_deck("build", source, "-o", source, expected=2)

    def test_browser_serialized_quote_forms_and_bom(self):
        body = "<div class='viewer'><main id='deck'><section data-theme='dark' class='slide' data-slide=''><h2 title='a &amp; b'>보기</h2></section></main></div>"
        source = self.source(body, "<meta content='saved' name='deck-saved'><style data-deck='engine'>old</style><script data-deck='engine'>old</script>", attrs="data-look='notebook' lang='ko'")
        source.write_bytes(b"\xef\xbb\xbf" + source.read_bytes())
        output, _ = self.build(source)
        content = output.read_text(encoding="utf-8")
        self.assertIn("<h2 title='a &amp; b'>보기</h2>", content)
        self.assertIn("name='deck-saved'", content)
        self.assertNotIn(">old</", content)
        self.assertEqual(len(slide_text(content)), 1)

    def test_unpack_build_round_trip(self):
        (self.root / "풍경.png").write_bytes(PNG)
        (self.root / "체.woff2").write_bytes(b"font bytes")
        body = '<section class="slide" style="background:url(풍경.png)"><h1>원형 &amp; 삼각형</h1><img src="풍경.png"><img src="풍경.png"></section>'
        source = self.source(body, '<style>@font-face{font-family:x;src:url(체.woff2)}</style>')
        first, _ = self.build(source)
        folder = self.root / "다시 풀기"
        self.run_deck("unpack", first, "-o", folder)
        draft = folder / "원고.html"
        self.assertTrue((folder / "그림" / "풍경.png").exists())
        self.assertEqual(len(list((folder / "그림").iterdir())), 1)
        self.assertEqual(len(list((folder / "글꼴").iterdir())), 1)
        self.run_deck("unpack", first, "-o", folder, expected=2)
        second = folder / "다시.html"
        self.run_deck("build", draft, "-o", second)
        self.assertEqual(slide_structure(first.read_text(encoding="utf-8")), slide_structure(second.read_text(encoding="utf-8")))

    def test_unpack_round_trip_preserves_attribute_quotes(self):
        image = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        body = f'''<section class="slide"><img src = '{image}'><p style='background:url("{image}")'>글</p></section>'''
        first, _ = self.build(self.source(body))
        folder = self.root / "인용 부호 풀기"
        self.run_deck("unpack", first, "-o", folder)
        self.run_deck("build", folder / "원고.html", "-o", folder / "결과.html")
        self.assertEqual(slide_structure(first.read_text(encoding="utf-8")),
                         slide_structure((folder / "결과.html").read_text(encoding="utf-8")))

    def test_unpack_editor_font_and_embedded_image(self):
        output, _ = self.build()
        text = output.read_text(encoding="utf-8")
        font = 'data:font/woff2;base64,' + base64.b64encode(b'editor font').decode()
        image = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
        text = text.replace('<style data-deck="fonts"></style>', f'<style data-deck="fonts">@font-face{{font-family:x;src:url({font})}}</style>')
        text = text.replace('</h1></section>', f'</h1><img src="{image}"></section>')
        output.write_text(text, encoding="utf-8")
        folder = self.root / "저장 풀기"
        self.run_deck("unpack", output, "-o", folder)
        draft = (folder / "원고.html").read_text(encoding="utf-8")
        self.assertNotIn('data-deck="fonts"', draft)
        self.assertIn('글꼴/글꼴-01.woff2', draft)
        self.assertIn('그림/그림-01.png', draft)
        rebuilt = folder / "결과.html"
        self.run_deck("build", folder / "원고.html", "-o", rebuilt)
        self.assertEqual(slide_structure(text), slide_structure(rebuilt.read_text(encoding="utf-8")))

    def test_font_character_set_excludes_data_urls(self):
        doc = Document('<html><body><section class="slide">&#48577;<img src="data:image/png;base64,쀍"></section></body></html>')
        chars = font_characters(doc, self.root / "원고.html")
        ks = {ord(bytes((a, b)).decode("euc-kr")) for a in range(0xb0, 0xc9) for b in range(0xa1, 0xff)}
        self.assertEqual(len(ks), 2350)
        self.assertTrue(ks.issubset(chars))
        self.assertIn(48577, chars)
        self.assertNotIn(ord("쀍"), chars)

    def test_maplestory_embedded_unmodified_and_credit(self):
        source = self.source('<section class="slide"><h1>제목 ✓ −</h1></section>')
        output = self.root / "글꼴 결과.html"
        self.run_deck("build", source, "-o", output, assets=deck.ASSETS)
        text = output.read_text(encoding="utf-8")
        css = Document(text)
        block = next(n for n in css.nodes if n.tag == "style" and n.attrs.get("data-deck") == "basefont")
        content = css.inner(block)
        encoded = re.findall(r'data:font/ttf;base64,([A-Za-z0-9+/=]+)', content)
        self.assertEqual(len(encoded), 2)
        for blob, name in zip(encoded, ("Maplestory Bold.ttf", "Maplestory Light.ttf")):
            expected = (deck.ASSETS / "fonts" / name).read_bytes()
            self.assertEqual(hashlib.sha256(base64.b64decode(blob)).digest(), hashlib.sha256(expected).digest())
        for token in ('font-family:"Maplestory"', 'font-weight:700', 'font-weight:300', 'format("truetype")', 'font-display:block', 'name="deck-credit"', 'Copyright (c) 2017'):
            self.assertIn(token, text)
        folder = self.root / "글꼴 풀기"
        self.run_deck("unpack", output, "-o", folder)
        self.assertNotIn('data-deck="basefont"', (folder / "원고.html").read_text(encoding="utf-8"))
        self.run_deck("build", folder / "원고.html", "-o", folder / "다시.html", assets=deck.ASSETS)
        self.assertIn('font-family:"Maplestory"', (folder / "다시.html").read_text(encoding="utf-8"))
        self.run_deck("build", source, "-o", output, "--no-font", assets=deck.ASSETS)
        self.assertNotIn('data-deck="basefont"', output.read_text(encoding="utf-8"))

    def test_author_supplied_maplestory_is_never_subset(self):
        original = (deck.ASSETS / "fonts" / "Maplestory Bold.ttf").read_bytes()
        (self.root / "renamed.ttf").write_bytes(original)
        source = self.source(head='<style>@font-face{font-family:author;src:url(renamed.ttf) format("truetype")}</style>')
        output = self.root / "원본 글꼴.html"
        self.run_deck("build", source, "-o", output, "--no-font", assets=deck.ASSETS)
        text = output.read_text(encoding="utf-8")
        match = re.search(r'data:font/ttf;base64,([A-Za-z0-9+/=]+)', text)
        self.assertIsNotNone(match)
        self.assertEqual(hashlib.sha256(base64.b64decode(match.group(1))).digest(), hashlib.sha256(original).digest())
        self.assertNotIn('format("woff")', text)

    @unittest.skipUnless(FONT_TESTS, "fontTools 또는 Noto Sans KR이 없습니다")
    def test_supplement_contains_only_missing_characters(self):
        source = self.source('<section class="slide"><h1>제목 ✓ −</h1></section>')
        output = self.root / "보충.html"
        self.run_deck("build", source, "-o", output, assets=deck.ASSETS)
        css, data = basefont_data(output.read_text(encoding="utf-8"))
        self.assertIn('font-family:"Deck Sans"', css)
        font = TTFont(io.BytesIO(data))
        try:
            cmap = font.getBestCmap()
            self.assertIn(ord("−"), cmap)
            self.assertNotIn(ord("가"), cmap)
        finally:
            font.close()

    @unittest.skipUnless(has_browser(), "브라우저가 없습니다")
    def test_browser_loads_maplestory(self):
        source = self.source('<section class="slide"><h1>한글 제목</h1></section>')
        output = self.root / "글꼴 표시.html"
        self.run_deck("build", source, "-o", output, assets=deck.ASSETS)
        with Browser() as browser:
            cdp = browser.cdp
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Page.navigate", {"url": output.as_uri()})
            cdp.wait_event("Page.loadEventFired")
            faces = cdp.evaluate('document.fonts.load(\'700 40px "Maplestory"\', "한글").then(x=>x.map(f=>({family:f.family,status:f.status})))', True)
            self.assertEqual(faces, [{"family": "Maplestory", "status": "loaded"}])

    @unittest.skipUnless(FONT_TESTS, "fontTools 또는 시험용 Noto Sans KR이 없습니다")
    def test_linked_font_face_over_300kb_is_subset(self):
        folder = self.root / "스타일"
        folder.mkdir()
        shutil.copyfile(TEST_FONT, folder / "글꼴.ttf")
        (folder / "글꼴.css").write_text('@font-face{font-family:x;src:url("글꼴.ttf") format("truetype")} .x:after{content:"쀍"}', encoding="utf-8")
        source = self.source('<section class="slide"><h1>뷁</h1></section>', '<link rel="stylesheet" href="스타일/글꼴.css">')
        output = self.root / "별도 글꼴.html"
        self.run_deck("build", source, "-o", output, "--no-font")
        text = output.read_text(encoding="utf-8")
        self.assertNotIn('data-deck="basefont"', text)
        self.assertIn('format("woff")', text)
        match = re.search(r'data:font/woff;base64,([A-Za-z0-9+/=]+)', text)
        self.assertIsNotNone(match)
        data = base64.b64decode(match.group(1))
        self.assertLess(len(data), (folder / "글꼴.ttf").stat().st_size)
        font = TTFont(io.BytesIO(data))
        try:
            self.assertIn(ord("뷁"), font.getBestCmap())
            self.assertIn(ord("쀍"), font.getBestCmap())
            self.assertIn("fvar", font)
        finally:
            font.close()
        unpacked = self.root / "글꼴 왕복"
        self.run_deck("unpack", output, "-o", unpacked)
        rebuilt = unpacked / "다시.html"
        self.run_deck("build", unpacked / "원고.html", "-o", rebuilt, "--no-font")
        repeat = re.search(r'data:font/woff;base64,([A-Za-z0-9+/=]+)', rebuilt.read_text(encoding="utf-8"))
        self.assertIsNotNone(repeat)
        self.assertEqual(base64.b64decode(repeat.group(1)), data)

    def test_engine_order_student_and_folder_argument(self):
        assets = self.root / "추가 엔진"
        shutil.copytree(ASSETS, assets)
        for name in ("widgets.js", "lab.js", "pen.js", "z-extra.js", "a-extra.js"):
            (assets / "engine" / name).write_text("/* " + name + " */", encoding="utf-8")
        (assets / "engine" / "a.css").write_text("/* a.css */", encoding="utf-8")
        source = self.source(title="폴더 제목")
        self.run_deck("build", self.root, "--student", assets=assets)
        text = (self.root / "폴더 제목.html").read_text(encoding="utf-8")
        order = ['window.deck = deck', '/* widgets.js */', '/* lab.js */', '/* pen.js */', '/* a-extra.js */', '/* z-extra.js */']
        self.assertEqual([text.index(s) for s in order], sorted(text.index(s) for s in order))
        self.assertLess(text.index('/* a.css */'), text.index((assets / "engine" / "deck.css").read_text(encoding="utf-8")))
        student = (self.root / "폴더 제목 (학생용).html").read_text(encoding="utf-8")
        self.assertIn('data-lock="view"', student.split('<head>')[0])
        self.assertNotIn('data-lock="view"', text.split('<head>')[0])

    def test_placeholder_round_trip(self):
        source = self.source('<section class="slide"><img class="asset" src="그림/없음.png" alt="연필 &amp; 공책"></section>')
        output, result = self.build(source)
        text = output.read_text(encoding="utf-8")
        self.assertIn('data-placeholder', text)
        self.assertIn('data:image/svg+xml;base64,', text)
        self.assertIn('data-deck-src="그림/없음.png"', text)
        self.assertIn('자리표시', result.stdout)
        folder = self.root / "자리표시 풀기"
        self.run_deck("unpack", output, "-o", folder)
        raw = (folder / "원고.html").read_text(encoding="utf-8")
        self.assertIn('src="그림/없음.png"', raw)
        self.assertNotIn('data-placeholder', raw)
        self.run_deck("build", folder, "-o", folder / "다시.html")
        self.assertIn('data-placeholder', (folder / "다시.html").read_text(encoding="utf-8"))

    def test_missing_optional_modules(self):
        isolated = self.root / "모듈 없음"
        isolated.mkdir()
        shutil.copyfile(SCRIPT, isolated / "deck.py")
        shutil.copyfile(SKILL / "scripts" / "cdp.py", isolated / "cdp.py")
        result = subprocess.run([sys.executable, "-B", str(isolated / "deck.py"), "--help"],
                                capture_output=True, text=True, encoding="utf-8", env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("build", result.stdout)
        self.assertNotIn("compare", result.stdout)
        self.assertNotIn("new", result.stdout)

    def test_webp_unpack_rebuild(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow가 없습니다")
        images = self.root / "그림"
        images.mkdir()
        image = Image.new("RGBA", (1600, 1200), (90, 160, 210, 128))
        image.save(images / "사진.png")
        source = self.source('<section class="slide"><img class="asset" src="그림/사진.png" alt="사진"></section>')
        output, _ = self.build(source)
        text = output.read_text(encoding="utf-8")
        self.assertIn('data:image/webp;base64,', text)
        self.assertIn('data-deck-src="그림/사진.png"', text)
        data = base64.b64decode(re.search(r'data:image/webp;base64,([A-Za-z0-9+/=]+)', text).group(1))
        with Image.open(io.BytesIO(data)) as converted:
            self.assertEqual(max(converted.size), 1200)
            self.assertEqual(converted.getpixel((10, 10))[3], 128)
        folder = self.root / "WebP 풀기"
        self.run_deck("unpack", output, "-o", folder)
        self.run_deck("build", folder, "-o", folder / "다시.html")
        self.assertEqual(slide_text(text), slide_text((folder / "다시.html").read_text(encoding="utf-8")))

    @unittest.skipUnless(TTFont is not None, "fontTools가 없습니다")
    def test_woff2_without_brotli_is_kept_with_warning(self):
        from fontTools.ttLib import woff2
        if woff2.haveBrotli:
            self.skipTest("brotli가 설치되어 있습니다")
        data = b"wOF2" + b"x" * (300 * 1024)
        (self.root / "압축 글꼴.woff2").write_bytes(data)
        source = self.source(head='<style>@font-face{font-family:x;src:url("압축 글꼴.woff2") format("woff2")}</style>')
        output = self.root / "브로틀리 없음.html"
        result = self.run_deck("build", source, "--no-font", "-o", output)
        self.assertIn("brotli가 필요합니다", result.stdout)
        self.assertNotIn("The WOFF2 decoder", result.stderr)
        text = output.read_text(encoding="utf-8")
        match = re.search(r'data:font/woff2;base64,([A-Za-z0-9+/=]+)', text)
        self.assertIsNotNone(match)
        self.assertEqual(base64.b64decode(match.group(1)), data)
        self.assertIn('format("woff2")', text)

    def test_fonttools_missing_keeps_large_author_font(self):
        data = b"x" * (300 * 1024 + 1)
        (self.root / "대형.ttf").write_bytes(data)
        source = self.source(head='<style>@font-face{font-family:x;src:url(대형.ttf) format("truetype")}</style>')
        output = self.root / "도구 없음.html"
        result = self.run_deck("build", source, "-o", output, python_flags=("-S",), assets=deck.ASSETS)
        self.assertIn("fontTools가 없어 누락 글자 보충", result.stdout)
        self.assertIn("300KB", result.stdout)
        text = output.read_text(encoding="utf-8")
        self.assertIn('data-deck="basefont"', text)
        self.assertEqual(text.count('data:font/ttf;base64,'), 3)
        blobs = re.findall(r'data:font/ttf;base64,([A-Za-z0-9+/=]+)', text)
        self.assertIn(data, [base64.b64decode(blob) for blob in blobs])
        self.assertIn('format("truetype")', text)

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_sets_capture_mode_and_offline_keeps_svg_data_urls(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow가 없습니다")
        from urllib.parse import quote
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"></svg>'
        url = 'data:image/svg+xml,' + quote(svg, safe=':/')
        head = '<style>html[data-deck-shot] .capture-control{display:none}</style>'
        body = (f'<section class="slide" style="background:#00ff00"><h1>시험</h1><img id="svg-test" src="{url}">'
                '<div class="capture-control" style="position:absolute;inset:0;background:#ff0000"></div></section>'
                '<script>const baseAudit=deck.audit;deck.audit=i=>{const issues=baseAudit(i);'
                'if(navigator.onLine || !document.documentElement.hasAttribute("data-deck-shot") || '
                '!document.querySelector("#svg-test").naturalWidth) issues.push({slide:i+1,level:"error",'
                'code:"capture-probe",message:"캡처·오프라인·SVG 검사 실패",target:"",rect:null});return issues}</script>')
        output, _ = self.build(self.source(body, head))
        folder = self.root / "캡처 검사"
        self.run_deck("check", output, "--shots", "--out", folder)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["problems"], [])
        with Image.open(folder / "slide-01.png") as image:
            self.assertEqual(image.convert("RGB").getpixel((960, 540)), (0, 255, 0))

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_real_browser_and_exit_codes(self):
        body = '<section class="slide"><h1>첫 장</h1></section><section class="slide" data-test-error><h1>둘째 장</h1></section>'
        output, _ = self.build(self.source(body))
        folder = self.root / "검사 결과"
        result = self.run_deck("check", output, "--out", folder, "--shots", expected=1)
        self.assertIn("오류 1 · 경고 0", result.stdout)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["slide_count"], 2)
        self.assertEqual(report["checked"], [1, 2])
        self.assertTrue(report["browser_version"])
        self.assertEqual(report["problems"][0]["slide"], 2)
        sheet = (folder / "sheet-01.png").read_bytes()
        self.assertEqual(struct.unpack('>II', sheet[16:24]), (1944, 388))
        self.assertEqual(report["files"], ["slide-01.png", "slide-02.png", "sheet-01.png", "report.json"])
        self.assertEqual(result.stdout.splitlines()[-1], str((folder / "sheet-01.png").resolve()))
        shots = []
        for name in ("slide-01.png", "slide-02.png"):
            data = (folder / name).read_bytes()
            self.assertEqual(struct.unpack('>II', data[16:24]), (1920, 1080))
            shots.append(data)
        self.assertNotEqual(shots[0], shots[1])
        only = self.root / "첫 장 검사"
        result = self.run_deck("check", output, "--slides", "1", "--out", only)
        partial = json.loads((only / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(partial["problems"], [])
        self.assertEqual(partial["checked"], [1])
        self.assertEqual(partial["files"], ["part-01.png", "report.json"])
        self.assertFalse((only / "sheet-01.png").exists())
        self.assertIn("오류 0 · 경고 0 · 2장 중 1장만 검사", result.stdout)
        self.assertIn("deck.selftest가 없어", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], str((only / "part-01.png").resolve()))
        self.run_deck("check", output, "--slides", "3", "--out", only, expected=2)
        # 일부 검사는 전체 검사 그림을 건드리지 않고, 남아 있는 지난 그림을 알린다.
        result = self.run_deck("check", output, "--slides", "1", "--out", folder)
        self.assertEqual((folder / "sheet-01.png").read_bytes(), sheet)
        self.assertEqual((folder / "slide-02.png").read_bytes(), shots[1])
        self.assertIn("알림 · 지난 검사의 그림 3개가 폴더에 남아 있습니다 (sheet-01.png, slide-01.png, slide-02.png)",
                      result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], str((folder / "part-01.png").resolve()))
        whole = self.run_deck("check", output, "--slides", "1,2", "--out", only, expected=1)
        self.assertNotIn("만 검사", whole.stdout)
        self.assertEqual(json.loads((only / "report.json").read_text(encoding="utf-8"))["files"],
                         ["sheet-01.png", "report.json"])

    def test_unpack_replaced_image_uses_actual_mime(self):
        (self.root / "원본.png").write_bytes(PNG)
        output, _ = self.build(self.source('<section class="slide"><img src="원본.png"></section>'))
        text = output.read_text(encoding="utf-8")
        text = text.replace('data:image/png;base64,' + base64.b64encode(PNG).decode(),
                            'data:image/jpeg;base64,' + base64.b64encode(b'new jpeg').decode())
        output.write_text(text, encoding="utf-8")
        folder = self.root / "바뀐 그림 풀기"
        self.run_deck("unpack", output, "-o", folder)
        self.assertTrue((folder / "그림" / "원본.jpg").exists())
        self.run_deck("build", folder / "원고.html", "-o", folder / "결과.html")
        self.assertEqual(slide_text(text), slide_text((folder / "결과.html").read_text(encoding="utf-8")))

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_records_shared_finding_counts_and_prints_one_notice(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch

        output, _ = self.build()
        text = deck.read_text(output).replace('data-deck="kit">', 'data-deck="kit">.x{border-radius:14px}', 1)
        deck.write_text(output, text)
        findings = [
            {"antipattern": "design-system-radius", "snippet": "border-radius: 14px is outside the DESIGN.md rounded scale"},
            {"antipattern": "other", "snippet": "다른 지적"},
        ]
        args = SimpleNamespace(input=output, out=self.root / "출처 검사", slides=None, shots=False, assets=ASSETS)
        stdout = io.StringIO()
        with patch.object(deck, "find_impeccable", return_value="fake"), patch.object(deck, "impeccable_json", return_value=findings), redirect_stdout(stdout):
            self.assertEqual(deck.check_command(args), 0)
        report = json.loads((args.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["imp_hidden"], {"total": 1, "by_rule": {"design-system-radius": 1}})
        self.assertEqual([p["code"] for p in report["problems"] if p["code"].startswith("imp-")], ["imp-other"])
        self.assertEqual([line for line in stdout.getvalue().splitlines() if "숨겼습니다" in line],
                         ["알림 · 부품이 의도한 임페커블 지적 1건은 숨겼습니다"])

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_multiple_sheets(self):
        slides = ''.join(f'<section class="slide"><h1>{n}번</h1></section>' for n in range(1, 18))
        output, _ = self.build(self.source(slides))
        folder = self.root / "두 장 모아보기"
        result = self.run_deck("check", output, "--out", folder)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["slide_count"], 17)
        self.assertEqual(report["files"], ["sheet-01.png", "sheet-02.png", "report.json"])
        self.assertEqual(result.stdout.splitlines()[-2:],
                         [str((folder / name).resolve()) for name in report["files"][:-1]])
        for name, dimensions in (("sheet-01.png", (1944, 1588)), ("sheet-02.png", (1944, 788))):
            data = (folder / name).read_bytes()
            self.assertEqual(struct.unpack('>II', data[16:24]), dimensions)

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_sheet_caption_layout_and_escaping(self):
        title = '<제목> & ' + '긴 제목 ' * 150
        page = deck.sheet_html([(7, title, base64.b64encode(PNG).decode("ascii"))])
        url = "data:text/html;base64," + base64.b64encode(page.encode("utf-8")).decode("ascii")
        with Browser() as browser:
            cdp = browser.cdp
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1944, "height": 388,
                                                            "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Page.navigate", {"url": url})
            cdp.wait_event("Page.loadEventFired")
            layout = cdp.evaluate("""(()=>{
              const img = document.querySelector('.cell img'), caption = document.querySelector('.caption');
              const rect = el => { const r = el.getBoundingClientRect(); return [r.x,r.y,r.width,r.height]; };
              const style = getComputedStyle(caption);
              return {image:rect(img), caption:rect(caption), text:caption.textContent,
                childCount:caption.children.length, overflow:style.overflow, ellipsis:style.textOverflow,
                whitespace:style.whiteSpace, truncated:caption.scrollWidth > caption.clientWidth,
                htmlOverflow:getComputedStyle(document.documentElement).overflow,
                bodyOverflow:getComputedStyle(document.body).overflow};
            })()""")
        self.assertEqual(layout["image"], [0, 0, 640, 360])
        self.assertEqual(layout["caption"], [0, 360, 640, 28])
        self.assertEqual(layout["text"], '7 · ' + title)
        self.assertEqual(layout["childCount"], 0)
        self.assertEqual((layout["overflow"], layout["ellipsis"], layout["whitespace"]),
                         ("hidden", "ellipsis", "nowrap"))
        self.assertTrue(layout["truncated"])
        self.assertEqual((layout["htmlOverflow"], layout["bodyOverflow"]), ("hidden", "hidden"))

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_title_priority_fallback_and_selection(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import patch
        body = ('<section class="slide" data-title="우선 &amp; &lt;제목&gt;"><p>본문</p></section>'
                '<section class="slide" data-title="붙인  이름"><h1>가려진 제목</h1></section>'
                '<section class="slide"><h2>첫 제목</h2><h1>뒤 제목</h1></section>'
                '<section class="slide"><p>제목 없음</p></section>'
                '<section class="slide"><h1>둘째<br><em>장</em></h1></section>')
        output, _ = self.build(self.source(body))
        folder = self.root / "제목 검사"
        captured = []
        sheet_html = deck.sheet_html

        def collect(images):
            captured.extend(images)
            return sheet_html(images)

        args = SimpleNamespace(input=output, out=folder, slides="3,1,2,4,5", shots=False)
        with patch.object(deck, "sheet_html", side_effect=collect), patch.object(deck, "find_impeccable", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(deck.check_command(args), 0)
        self.assertEqual([(number, title) for number, title, _ in captured],
                         [(3, "첫 제목"), (1, "우선 & <제목>"), (2, "붙인 이름"), (4, ""), (5, "둘째 장")])

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_browser_start_rereads_locked_or_empty_port_file(self):
        from unittest.mock import patch
        real = Path.read_text
        calls = []

        def flaky(path, *args, **kwargs):
            if path.name == "DevToolsActivePort":
                calls.append(path)
                if len(calls) == 1:
                    raise PermissionError(13, "Permission denied")
                if len(calls) == 2:
                    return ""
            return real(path, *args, **kwargs)

        with patch.object(Path, "read_text", flaky):
            with Browser() as browser:
                self.assertTrue(browser.cdp.call("Browser.getVersion").get("product"))
        self.assertGreaterEqual(len(calls), 3)

    @unittest.skipUnless(has_browser(), "Chrome 또는 Edge가 설치되지 않았습니다")
    def test_check_console_and_not_deck(self):
        source = self.source('<section class="slide"><h1>장</h1></section><script>console.error("콘솔 시험")</script>')
        output, _ = self.build(source)
        result = self.run_deck("check", output, "--out", self.root / "콘솔 검사", expected=1)
        self.assertIn("console-error", result.stdout)
        other = self.root / "일반.html"
        other.write_text('<!doctype html><html><body>일반 문서</body></html>', encoding="utf-8")
        result = self.run_deck("check", other, expected=2)
        self.assertIn("pbj-html-deck으로 묶은 파일이 아닙니다", result.stderr)


if __name__ == "__main__":
    unittest.main()
