#!/usr/bin/env python3
"""자유로운 HTML 원고를 묶고, 검사하고, 다시 푸는 도구."""

import argparse
import base64
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse

try:
    from PIL import Image
except ImportError:
    Image = None

# 도구를 실행할 때 스킬 폴더에 Python 캐시를 남기지 않는다.
sys.dont_write_bytecode = True
from cdp import Browser, BrowserError

try:
    from fontTools import subset
    from fontTools.ttLib import TTFont
except ImportError:
    subset = None
    TTFont = None


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

ASSETS = Path(__file__).resolve().parent.parent / "assets"
MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
         ".avif": "image/avif", ".woff2": "font/woff2", ".woff": "font/woff",
         ".ttf": "font/ttf", ".otf": "font/otf", ".mp3": "audio/mpeg",
         ".mp4": "video/mp4"}
EXTENSIONS = {v: k for k, v in MIMES.items()}
EXTENSIONS["image/jpeg"] = ".jpg"
EXTENSIONS.update({"application/font-woff": ".woff", "application/x-font-ttf": ".ttf",
                   "application/octet-stream": ".bin"})
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
URL_RE = re.compile(r"url\(\s*(?:(['\"])((?:\\.|(?!\1).)*)\1|((?:\\.|[^)])*?))\s*\)", re.I | re.S)
ATTR_RE = re.compile(r"\s+([^\s=/>]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?", re.S)
REMOTE = re.compile(r"^https?://", re.I)


class DeckError(Exception):
    pass


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DeckError(f"파일을 읽을 수 없습니다: {path} ({exc}) · UTF-8 파일과 경로를 확인하세요") from exc


def write_text(path, content):
    try:
        with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content.replace("\r\n", "\n").replace("\r", "\n"))
    except OSError as exc:
        raise DeckError(f"파일을 쓸 수 없습니다: {path} ({exc}) · 폴더와 쓰기 권한을 확인하세요") from exc


def changes(text, edits):
    """서로 겹치지 않는 원문 구간만 교체한다."""
    end = len(text)
    for start, stop, value in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        if start < 0 or stop > end or start > stop:
            raise DeckError("HTML의 수정 구간이 겹칩니다 · 원고의 태그 중첩을 확인하세요")
        text = text[:start] + value + text[stop:]
        end = start
    return text


def edit_tag(tag, updates):
    """지정한 속성만 수정하고 나머지 시작 태그 원문을 보존한다."""
    first = re.match(r"<\s*[^\s/>]+", tag)
    if not first:
        return tag
    seen = set()
    edits = []
    for match in ATTR_RE.finditer(tag, first.end()):
        name = match.group(1).lower()
        if name not in updates:
            continue
        value = updates[name]
        if name in seen or value is None:
            replacement = ""
        elif value is True:
            replacement = match.group(0) if all(match.group(i) is None for i in (2, 3, 4)) else " " + match.group(1)
        else:
            raw = match.group(0)
            group = next((i for i in (2, 3, 4) if match.group(i) is not None), None)
            if group:
                quote = '"' if group == 2 else "'" if group == 3 else ""
                value_text = html.escape(str(value), quote=False)
                if quote == '"':
                    value_text = value_text.replace('"', "&quot;")
                elif quote == "'":
                    value_text = value_text.replace("'", "&#x27;")
                elif re.search(r"[\s\"'`=<>]", value_text):
                    value_text = '"' + value_text.replace('"', "&quot;") + '"'
                start = match.start(group) - match.start()
                end = match.end(group) - match.start()
                replacement = raw[:start] + value_text + raw[end:]
            else:
                replacement = raw + '="' + html.escape(str(value), quote=True) + '"'
        edits.append((match.start(), match.end(), replacement))
        seen.add(name)
    insert = []
    for name, value in updates.items():
        if name not in seen and value is not None:
            insert.append(" " + name + ("" if value is True else '="' + html.escape(str(value), quote=True) + '"'))
    if insert:
        pos = tag.rfind(">")
        if pos > 0 and tag[pos - 1] == "/":
            pos -= 1
        edits.append((pos, pos, "".join(insert)))
    return changes(tag, edits)


class Node:
    def __init__(self, tag, attrs, start, start_end, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.start = start
        self.start_end = start_end
        self.end_start = start_end
        self.end = start_end
        self.parent = parent

    def under(self, predicate):
        parent = self.parent
        while parent:
            if predicate(parent):
                return True
            parent = parent.parent
        return False


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0]
        self.lines.extend(m.end() for m in re.finditer("\n", text))
        self.nodes = []
        self.stack = []
        try:
            self.feed(text)
            self.close()
        except Exception as exc:
            raise DeckError(f"HTML을 해석할 수 없습니다: {exc} · 태그의 괄호와 따옴표를 확인하세요") from exc

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self.position()
        start_end = start + len(self.get_starttag_text())
        node = Node(tag, attrs, start, start_end, self.stack[-1] if self.stack else None)
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        start = self.position()
        close = self.text.find(">", start)
        close = len(self.text) if close < 0 else close + 1
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                for node in self.stack[index + 1:]:
                    node.end_start = start
                    node.end = start
                node = self.stack[index]
                node.end_start = start
                node.end = close
                del self.stack[index:]
                break

    def raw(self, node):
        return self.text[node.start:node.end]

    def start_tag(self, node):
        return self.text[node.start:node.start_end]

    def inner(self, node):
        return self.text[node.start_end:node.end_start]


def is_runtime(node):
    return "data-deck-rt" in node.attrs


def is_slide(node):
    return node.tag == "section" and "slide" in (node.attrs.get("class") or "").split()


def is_common(node):
    return (node.attrs.get("data-deck") or "").lower() in ("engine", "kit", "basefont")


def skipped(node):
    return is_runtime(node) or node.under(is_runtime) or is_common(node) or node.under(is_common)


def font_characters(document, source, assets=()):
    """기본 문자와 data URL을 제외한 문서의 문자 참조를 모두 모은다."""
    excluded = []
    external = list(assets)
    for node in document.nodes:
        if (is_common(node) or is_runtime(node)) and not node.under(lambda n: is_common(n) or is_runtime(n)):
            excluded.append((node.start, node.end, ""))
        if skipped(node):
            continue
        ref = None
        if node.tag == "script":
            ref = node.attrs.get("src")
        elif node.tag == "link" and "stylesheet" in (node.attrs.get("rel") or "").lower().split():
            ref = node.attrs.get("href")
        if ref:
            parts = urllib.parse.urlsplit(ref)
            if not parts.scheme and not parts.netloc and parts.path:
                try:
                    external.append((Path(source).parent / urllib.parse.unquote(parts.path)).read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeError):
                    # 실제 불러오기 단계에서 경로를 포함해 오류를 알린다.
                    pass
    text = html.unescape(changes(document.text, excluded) + "\n" + "\n".join(external))
    text = re.sub(r"data:[^\s\"'<>)]*", "", text, flags=re.I)
    chars = {ord(c) for c in text}
    chars.update(range(0x20, 0x7f))
    chars.update(range(0xa0, 0x100))
    chars.update(range(0x3131, 0x3164))
    chars.update(ord(c) for c in "…‘’“”·•–—※→←↑↓↔⇒①②③④⑤⑥⑦⑧⑨⑩○●□■△▲▽▼◇◆★☆℃％×÷≠≤≥±√∞∠⊥∥∽≡∴∵π°′″「」『』《》〈〉ㆍ‰∼∑∏∫∂∆∈∉∋⊂⊃⊆⊇∪∩∅∀∃∝∧∨¬≈≒≪≫")
    for first in range(0xb0, 0xc9):
        for second in range(0xa1, 0xff):
            chars.add(ord(bytes((first, second)).decode("euc-kr")))
    return chars


def subset_font(data, characters, require_variable=False):
    """WOFF로 줄이면서 원본의 굵기 축을 보존한다."""
    if subset is None:
        raise ImportError("fontTools가 없습니다")
    if data.startswith(b"wOF2"):
        from fontTools.ttLib import woff2
        if not woff2.haveBrotli:
            raise ValueError("WOFF2 글꼴을 읽으려면 brotli가 필요합니다")
    font = TTFont(io.BytesIO(data), recalcTimestamp=False)
    logger = logging.getLogger("fontTools.subset")
    old_level = logger.level
    try:
        axes = list(font["fvar"].axes) if "fvar" in font else []
        weight_axis = next((axis for axis in axes if axis.axisTag == "wght"), None)
        if require_variable and weight_axis is None:
            raise ValueError("가변 굵기 축이 없습니다")
        if weight_axis:
            weight = f"{weight_axis.minValue:g} {weight_axis.maxValue:g}"
        else:
            weight = str(getattr(font.get("OS/2"), "usWeightClass", 400))
        logger.setLevel(logging.ERROR)
        options = subset.Options()
        # 저작권(0)과 이용 허락(13·14) 표기를 줄인 글꼴 안에 남긴다(SIL OFL 등의 표시 조건)
        options.name_IDs = sorted(set(options.name_IDs) | {0, 13, 14})
        worker = subset.Subsetter(options=options)
        worker.populate(unicodes=characters)
        worker.subset(font)
        font.flavor = "woff"
        output = io.BytesIO()
        font.save(output)
        return output.getvalue(), weight
    finally:
        logger.setLevel(old_level)
        font.close()


def noto_candidates(assets):
    folder = Path(assets) / "fonts"
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            if re.sub(r"[\s_-]", "", path.name).lower().startswith("notosanskr"):
                yield path
    if os.name == "nt":
        yield Path("C:/Windows/Fonts/NotoSansKR-VF.ttf")
        local = os.environ.get("LOCALAPPDATA")
        roots = [Path(local) / "Microsoft/Windows/Fonts"] if local else []
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library/Fonts", Path("/Library/Fonts")]
    else:
        roots = [Path("/usr/share/fonts")]
    for root in roots:
        if root.is_dir():
            for directory, _, files in os.walk(root):
                for name in sorted(files, key=str.casefold):
                    if re.sub(r"[\s_-]", "", name).lower().startswith("notosanskr") and name.lower().endswith((".ttf", ".otf", ".woff")):
                        yield Path(directory) / name


def used_font_characters(document, assets=()):
    # 글꼴 보충은 전체 한글 집합이 아니라 화면에 쓸 문자만 대상으로 한다.
    parts = []
    class TextCollector(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.hidden = []
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.hidden.append(tag)
            for key, value in attrs:
                if key in ("alt", "title", "data-title", "placeholder", "aria-label", "value") and value:
                    parts.append(value)
        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            if self.hidden and self.hidden[-1] == tag:
                self.hidden.pop()
        def handle_endtag(self, tag):
            if self.hidden and self.hidden[-1] == tag:
                self.hidden.pop()
        def handle_data(self, value):
            if not self.hidden:
                parts.append(value)
    excluded = [(n.start, n.end, "") for n in document.nodes
                if (is_common(n) or is_runtime(n)) and not n.under(lambda p: is_common(p) or is_runtime(p))]
    source = changes(document.text, excluded)
    collector = TextCollector()
    collector.feed(source)
    # CSS content의 실제 문자열과 엔진이 표시하는 JS 문자열도 보충한다.
    css = "\n".join(list(assets[:2]) + [document.inner(n) for n in document.nodes
                                         if n.tag == "style" and not skipped(n)])
    parts.extend(m.group(2) for m in re.finditer(r"\bcontent\s*:\s*(['\"])(.*?)\1", css, re.I | re.S))
    if len(assets) > 2:
        parts.extend(m.group(2) for m in re.finditer(r"(['\"])((?:\\.|(?!\1).)*?)\1", assets[2], re.S))
    text = html.unescape("\n".join(parts))
    text = re.sub(r"data:[^\s\"'<>)]*", "", text, flags=re.I)
    return {ord(c) for c in text if c.isprintable()}


def make_basefont(args, characters, warnings):
    if args.no_font:
        return ""
    folder = Path(args.assets) / "fonts"
    notice = read_text(folder / "NOTICE.txt").splitlines()
    faces, cmaps = [], []
    for name, weight in (("Maplestory Bold.ttf", "700"), ("Maplestory Light.ttf", "300")):
        path = folder / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DeckError(f"메이플스토리 글꼴을 읽을 수 없습니다: {path} ({exc})") from exc
        faces.append('@font-face{font-family:"Maplestory";src:url(data:font/ttf;base64,'
                     + base64.b64encode(data).decode("ascii") + ') format("truetype");font-weight:'
                     + weight + ';font-style:normal;font-display:block}')
        if TTFont is not None:
            font = TTFont(io.BytesIO(data), recalcTimestamp=False)
            try:
                cmaps.append(set(font.getBestCmap() or {}))
            finally:
                font.close()
    if subset is None or TTFont is None:
        print("알림: fontTools가 없어 누락 글자 보충을 건너뜁니다")
    else:
        missing = characters - set.intersection(*cmaps)
        if missing:
            for path in noto_candidates(args.assets):
                if not path.is_file():
                    continue
                try:
                    compressed, weight = subset_font(path.read_bytes(), missing)
                    faces.append('@font-face{font-family:"Deck Sans";src:url(data:font/woff;base64,'
                                 + base64.b64encode(compressed).decode("ascii")
                                 + ') format("woff");font-weight:' + weight
                                 + ';font-style:normal;font-display:block}')
                    break
                except Exception as exc:
                    warnings.append(f"보충 글꼴을 읽거나 줄일 수 없습니다: {path} ({exc})")
            else:
                print("알림: Noto Sans KR이 없어 누락 글자 보충을 건너뜁니다")
    comment = "\n".join(notice[2:]).replace("*/", "* /")
    return '<style data-deck="basefont">/*\n' + comment + '\n*/\n' + '\n'.join(faces) + '</style>\n'


def root_tag(document, tag):
    node = next((n for n in document.nodes if n.tag == tag), None)
    return document.start_tag(node) if node else f"<{tag}>"


def clean_html_tag(tag):
    node = next((n for n in Document(tag).nodes if n.tag == "html"), None)
    if not node:
        return tag
    updates = {"data-deck-mode": None}
    classes = (node.attrs.get("class") or "").split()
    if "deck-on" in classes:
        classes.remove("deck-on")
        updates["class"] = " ".join(classes) if classes else None
    style = node.attrs.get("style")
    if style is not None:
        parts = style.split(";")
        kept = [s for s in parts if not re.match(r"\s*--(?:bar|panel)\s*:", s, re.I)]
        if len(kept) != len(parts):
            cleaned = ";".join(kept).strip("; ")
            updates["style"] = cleaned or None
    return edit_tag(tag, updates)


def title_of(doc):
    title = next((n for n in doc.nodes if n.tag == "title" and not skipped(n)), None)
    if not title:
        return "제목 없음"
    return html.unescape(re.sub(r"<[^>]*>", "", doc.inner(title))).strip() or "제목 없음"


def valid_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "제목 없음"
    if re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name[:180]


def slides_of(doc):
    result = []
    for node in doc.nodes:
        if not is_slide(node) or skipped(node):
            continue
        if node.under(is_slide):
            number = next((i for i, n in enumerate(result, 1) if node.start < n.end), len(result))
            raise DeckError(f"{number or 1}번 장에 다른 장이 중첩되어 있습니다 · section.slide를 장 밖으로 옮기세요")
        parent = node.parent
        if parent and (parent.tag == "body" or parent.attrs.get("id") == "deck"):
            if node.end <= node.start_end:
                raise DeckError(f"{len(result) + 1}번 장의 닫는 태그가 없습니다 · </section>을 추가하세요")
            result.append(node)
    if not result:
        raise DeckError("장이 없습니다 · body 또는 #deck 바로 아래에 section.slide를 추가하세요")
    return result


def css_warnings(css, where, warnings):
    # 문자열과 주석 안의 글자는 검사 대상인 CSS 구문이 아니다.
    clean = re.sub(r"/\*.*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", css, flags=re.S)
    if re.search(r"@import\b", clean, re.I):
        raise DeckError(f"{where}에 CSS @import가 있습니다 · 내용을 직접 넣거나 로컬 스타일시트를 연결하세요")
    if re.search(r"@scope\b", clean, re.I):
        warnings.append(f"{where}: CSS @scope가 있습니다 · 평평한 선택자로 바꾸세요")
    if re.search(r"(?<![\w-])[-+]?(?:\d*\.)?\d+(?:vw|vh|cqw)\b", clean, re.I):
        warnings.append(f"{where}: vw/vh/cqw 단위가 있습니다 · 캔버스 px 단위를 사용하세요")
    if re.search(r"\bposition\s*:\s*fixed\b", clean, re.I):
        warnings.append(f"{where}: position:fixed가 있습니다 · 장 안의 절대 위치를 사용하세요")
    # 괄호의 부모가 일반 선택자인지 확인한다.
    stack = []
    last = 0
    for match in re.finditer(r"[{}]", clean):
        if match.group() == "{":
            prelude = clean[last:match.start()].strip()
            if stack and stack[-1] == "rule":
                warnings.append(f"{where}: CSS 중첩 선택자가 있습니다 · 평평한 선택자로 바꾸세요")
                break
            stack.append("at" if prelude.startswith("@") else "rule")
        elif stack:
            stack.pop()
        last = match.end()


def safe_inline(content, tag):
    return re.sub(r"</" + tag, lambda m: "<\\/" + m.group()[2:], content, flags=re.I)


def css_font_face_ranges(css):
    """주석과 문자열을 건너뛰며 @font-face 블록의 위치를 찾는다."""
    ranges = []
    index = 0
    depth = 0
    start = None
    while index < len(css):
        if css.startswith("/*", index):
            end = css.find("*/", index + 2)
            index = len(css) if end < 0 else end + 2
        elif css[index] in "'\"":
            quote = css[index]
            index += 1
            while index < len(css):
                if css[index] == "\\":
                    index += 2
                elif css[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
        elif start is None and css[index:index + 10].lower() == "@font-face":
            match = re.match(r"@font-face(?:\s|/\*.*?\*/)*\{", css[index:], re.I | re.S)
            if match:
                start = index + match.end()
                depth = 1
                index = start
            else:
                index += 1
        elif start is not None and css[index] == "{":
            depth += 1
            index += 1
        elif start is not None and css[index] == "}":
            depth -= 1
            if depth == 0:
                ranges.append((start, index))
                start = None
            index += 1
        else:
            index += 1
    return ranges


def css_urls(css, replace):
    """주석과 일반 문자열 속의 url(...)은 건드리지 않는다."""
    edits = []
    index = 0
    while index < len(css):
        if css.startswith("/*", index):
            end = css.find("*/", index + 2)
            index = len(css) if end < 0 else end + 2
        elif css[index] in "'\"":
            quote = css[index]
            index += 1
            while index < len(css):
                if css[index] == "\\":
                    index += 2
                elif css[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
        elif (css[index:index + 3].lower() == "url" and
              (index == 0 or not re.match(r"[\w-]", css[index - 1]))):
            match = URL_RE.match(css, index)
            if match:
                replacement = replace(match)
                if isinstance(replacement, tuple):
                    replacement, end = replacement
                else:
                    end = match.end()
                edits.append((index, end, replacement))
                index = end
            else:
                index += 1
        else:
            index += 1
    return changes(css, edits)


def css_unescape(value):
    def decode(match):
        token = match.group(1)
        if re.fullmatch(r"[0-9a-fA-F]{1,6}\s?", token):
            number = int(token.strip(), 16)
            return chr(number) if 0 < number <= 0x10ffff else "\ufffd"
        return token
    return re.sub(r"\\([0-9a-fA-F]{1,6}\s?|[^\r\n\f])", decode, value)


class Bundler:
    def __init__(self, source, warnings, characters, assets=ASSETS):
        self.source = Path(source)
        self.warnings = warnings
        self.characters = characters
        self.protected_fonts = set()
        for name in ("Maplestory Bold.ttf", "Maplestory Light.ttf"):
            path = Path(assets) / "fonts" / name
            if path.is_file():
                self.protected_fonts.add(hashlib.sha256(path.read_bytes()).digest())

    def resource(self, value, where, base=None, font_face=False):
        value = value.strip()
        if not value or value.lower().startswith("data:") or value.startswith("#"):
            return value, False, False
        if REMOTE.match(value) or value.startswith("//"):
            self.warnings.append(f"{where}: 외부 자원 {value} · 오프라인 사용을 위해 로컬 파일로 바꾸세요")
            return value, False, False
        parts = urllib.parse.urlsplit(value)
        if parts.scheme or parts.netloc:
            raise DeckError(f"{where}: 지원하지 않는 자원 주소 {value} · 상대 파일 경로를 사용하세요")
        path = (base or self.source.parent) / urllib.parse.unquote(parts.path)
        suffix = path.suffix.lower()
        if suffix not in MIMES:
            raise DeckError(f"{where}: 지원하지 않는 자원 {value} · 지원 형식의 파일을 사용하세요")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DeckError(f"{where}: 로컬 파일이 없습니다: {path} · 경로와 파일 이름을 확인하세요") from exc
        mime = MIMES[suffix]
        reduced = False
        if suffix == ".png" and "그림" in path.parts and Image is not None:
            try:
                with Image.open(io.BytesIO(data)) as source_image:
                    image = source_image.convert("RGBA" if "A" in source_image.getbands() or "transparency" in source_image.info else "RGB")
                    image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                    buffer = io.BytesIO()
                    image.save(buffer, "WEBP", quality=88)
                    if len(buffer.getvalue()) < len(data):
                        data, mime = buffer.getvalue(), "image/webp"
            except (OSError, ValueError) as exc:
                self.warnings.append(f"{where}: WebP 변환을 건너뜁니다: {path} ({exc})")
        if mime.startswith("image/") and len(data) > 1.5 * 1024 * 1024:
            self.warnings.append(f"{where}: 그림 {path}의 크기가 1.5MB를 넘습니다 · 그림을 줄여 사용하세요")
        protected = (path.name.casefold() in ("maplestory bold.ttf", "maplestory light.ttf") or
                     hashlib.sha256(data).digest() in self.protected_fonts)
        if font_face and suffix in (".ttf", ".otf", ".woff", ".woff2") and len(data) > 300 * 1024 and not protected:
            if subset is None:
                self.warnings.append(f"{where}: 글꼴 {path}이 300KB를 넘지만 fontTools가 없어 그대로 넣습니다 · fontTools를 설치하세요")
            else:
                try:
                    data, _ = subset_font(data, self.characters)
                    mime = "font/woff"
                    reduced = True
                except Exception as exc:
                    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    self.warnings.append(f"{where}: 글꼴 {path}을 줄이지 못해 그대로 넣습니다 ({reason}) · 글꼴 형식을 확인하세요")
        url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
        if parts.fragment:
            url += "#" + parts.fragment
        return url, True, reduced

    def css(self, css, where, base=None):
        css_warnings(css, where, self.warnings)
        font_ranges = css_font_face_ranges(css)
        def replace(match):
            original = match.group(2) if match.group(1) else match.group(3).strip()
            in_font_face = any(start <= match.start() < end for start, end in font_ranges)
            new, embedded, reduced = self.resource(css_unescape(original), where, base, in_font_face)
            if not embedded:
                return match.group(0)
            group = 2 if match.group(1) else 3
            start, end = match.start(group) - match.start(), match.end(group) - match.start()
            result = match.group(0)[:start] + new + match.group(0)[end:]
            if reduced:
                form = re.match(r"((?:\s|/\*.*?\*/)*)format\s*\(\s*(?:\"[^\"]*\"|'[^']*'|[^)]*)\s*\)",
                                css[match.end():], re.I | re.S)
                if form:
                    return result + form.group(1) + 'format("woff")', match.end() + form.end()
            return result
        return css_urls(css, replace)

    def local_text(self, value, where, kind):
        value = value or ""
        if REMOTE.match(value) or value.startswith("//"):
            raise DeckError(f"{where}: 원격 {kind} {value} · 로컬 파일로 바꾸세요")
        parts = urllib.parse.urlsplit(value)
        if not value or parts.scheme or parts.netloc or value.startswith("#"):
            raise DeckError(f"{where}: {kind} 경로 {value}가 올바르지 않습니다 · 로컬 파일을 지정하세요")
        path = self.source.parent / urllib.parse.unquote(parts.path)
        try:
            # 연결한 CSS의 상대 URL은 그 CSS 파일을 기준으로 해석한다.
            return read_text(path), path.parent
        except DeckError as exc:
            raise DeckError(f"{where}: {exc}") from exc

    def transform(self, fragment, where, slide=False, page=None):
        doc = Document(fragment)
        edits = []
        for n in doc.nodes:
            if n.under(lambda parent: is_runtime(parent) or is_common(parent)):
                continue
            if is_runtime(n) or is_common(n):
                edits.append((n.start, n.end, ""))
                continue
            attrs = n.attrs
            updates = {}
            originals = {}
            if slide and n.tag == "br" and n.under(lambda parent: parent.tag in ("h1", "h2")):
                # 제목의 줄바꿈 뒤에 공백이 없으면 글자만 읽는 도구(목차·교실 가져오기)가 두 줄을 붙여 읽는다
                if n.end < len(fragment) and not fragment[n.end].isspace():
                    edits.append((n.end, n.end, "\n"))
            if slide and n.start == 0 and is_slide(n):
                if "data-slide" not in attrs:
                    updates["data-slide"] = True
                if page is not None:
                    # 쪽 번호: 스크립트 없는 정적 화면·낱장에서도 부품이 이 값으로 번호를 그린다
                    updates["data-deck-page"] = str(page)
            refs = []
            if n.tag == "img" and "asset" in (attrs.get("class") or "").split():
                src = attrs.get("src") or ("그림/" + (attrs.get("data-asset") or "asset") + ".png")
                parts = urllib.parse.urlsplit(src)
                if not parts.scheme and not parts.netloc and not (self.source.parent / urllib.parse.unquote(parts.path)).is_file():
                    label = html.escape(attrs.get("alt") or attrs.get("data-asset") or "그림", quote=True)
                    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500" viewBox="0 0 800 500">'
                           '<rect x="8" y="8" width="784" height="484" rx="24" fill="#f7f7f7" stroke="#888" stroke-width="8" stroke-dasharray="18 12"/>'
                           '<text x="400" y="250" text-anchor="middle" dominant-baseline="middle" font-size="32" fill="#555">'
                           + label + '</text></svg>')
                    updates.update({"src": "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii"),
                                    "data-deck-src": src, "data-placeholder": True})
                    self.warnings.append(f"{where}: 그림이 없어 자리표시를 넣었습니다: {src}")
                elif "src" in attrs:
                    refs.append("src")
            elif n.tag in ("img", "source", "video", "audio") and "src" in attrs:
                refs.append("src")
            if n.tag == "video" and "poster" in attrs:
                refs.append("poster")
            if n.tag == "image":
                refs += [key for key in ("href", "xlink:href") if key in attrs]
            for key in refs:
                val, embedded, _ = self.resource(attrs[key] or "", where)
                if embedded:
                    updates[key] = val
                    originals[key] = attrs[key]
            if originals and "data-deck-src" not in attrs and "data-deck-unpack-nometa" not in attrs:
                updates["data-deck-src"] = (next(iter(originals.values())) if len(originals) == 1
                                            else json.dumps(originals, ensure_ascii=False, separators=(",", ":")))
            if "data-deck-unpack-nometa" in attrs:
                updates["data-deck-unpack-nometa"] = None
            if "style" in attrs:
                style = self.css(attrs["style"] or "", where)
                if style != attrs["style"]:
                    updates["style"] = style
            if n.tag == "style":
                content = self.css(doc.inner(n), where)
                edits.append((n.start_end, n.end_start, safe_inline(content, "style")))
            if n.tag == "script" and "src" in attrs:
                content, _ = self.local_text(attrs["src"], where, "스크립트")
                updates["src"] = None
                edits.append((n.start_end, n.end_start, safe_inline(content, "script")))
            if n.tag == "link" and "stylesheet" in (attrs.get("rel") or "").lower().split():
                css, base = self.local_text(attrs.get("href"), where, "스타일시트")
                content = safe_inline(self.css(css, where, base), "style")
                edits.append((n.start, n.end, "<style>" + content + "</style>"))
                continue
            if updates:
                edits.append((n.start, n.start_end, edit_tag(doc.start_tag(n), updates)))
        return changes(fragment, edits)


def gather(doc, slides, bundler):
    styles, scripts, fonts = [], [], []
    for n in doc.nodes:
        if skipped(n) or n.under(is_slide):
            continue
        where = "덱 CSS/스크립트"
        if n.tag == "style" and (n.attrs.get("data-deck") or "").lower() == "fonts":
            fonts.append(bundler.css(doc.inner(n), "포함된 글꼴 CSS"))
        elif n.tag == "style" and "data-deck" not in n.attrs:
            styles.append(bundler.transform(doc.raw(n), where))
        elif n.tag == "script" and "data-deck" not in n.attrs:
            scripts.append(bundler.transform(doc.raw(n), where))
        elif n.tag == "link" and "stylesheet" in (n.attrs.get("rel") or "").lower().split():
            css, base = bundler.local_text(n.attrs.get("href"), where, "스타일시트")
            styles.append("<style>" + safe_inline(bundler.css(css, where, base), "style") + "</style>")
    return styles, scripts, fonts


def duplicate_warnings(doc, warnings):
    seen = set()
    for n in doc.nodes:
        if skipped(n):
            continue
        ident = n.attrs.get("id")
        if ident:
            if ident in seen:
                warnings.append(f"문서의 id '{ident}'가 중복됩니다 · 각 id를 다르게 바꾸세요")
            seen.add(ident)


def is_bundled(doc):
    return any(n.attrs.get("id") == "deck" and not skipped(n) for n in doc.nodes)


def saved_meta(doc):
    node = next((n for n in doc.nodes if n.tag == "meta" and
                 (n.attrs.get("name") or "").lower() == "deck-saved" and not skipped(n)), None)
    return doc.raw(node) if node else ""


def build_command(args):
    source = Path(args.input)
    if source.is_dir():
        source = source / "원고.html"
    doc = Document(read_text(source))
    slides = slides_of(doc)
    asset_dir = Path(args.assets)
    kit = read_text(asset_dir / "kit" / "kit.css")
    # 2차 부품을 그대로 쓰는 덱도 3차 기본 글꼴로 열린다. 3차 부품이 자체 스택을 제공하면 건드리지 않는다.
    if not args.no_font and not re.search(r"\bMaplestory\b", kit, re.I):
        kit += ('\n:root{--font-title:"Maplestory","Deck Sans","Malgun Gothic",sans-serif;'
                '--font-body:"Maplestory","Deck Sans","Malgun Gothic",sans-serif}\n')
    engine_dir = asset_dir / "engine"
    # 필수 핵심 파일은 누락되면 조용히 넘어가지 않는다.
    read_text(engine_dir / "deck.css")
    read_text(engine_dir / "deck.js")
    css_files = sorted(engine_dir.glob("*.css"), key=lambda p: p.name.casefold())
    js_files = list(engine_dir.glob("*.js"))
    priority = {name: index for index, name in enumerate(("deck.js", "widgets.js", "lab.js", "pen.js"))}
    js_files.sort(key=lambda p: (priority.get(p.name, 4), p.name.casefold()))
    engine_css = "\n".join(read_text(path) for path in css_files)
    engine_js = "\n;\n".join(read_text(path) for path in js_files)
    warnings = []
    duplicate_warnings(doc, warnings)
    characters = font_characters(doc, source, (kit, engine_css, engine_js))
    bundler = Bundler(source, warnings, characters, asset_dir)
    styles, scripts, fonts = gather(doc, slides, bundler)
    built_slides = []
    for index, slide in enumerate(slides, 1):
        raw = doc.raw(slide)
        if any(n.tag == "form" and n.start >= slide.start and n.end <= slide.end and not skipped(n)
               for n in doc.nodes):
            warnings.append(f"{index}번 장에 form이 있습니다 · form 없이 입력 요소를 구성하세요")
        built_slides.append(bundler.transform(raw, f"{index}번 장", slide=True, page=index))
    title = title_of(doc)
    output = Path(args.output) if args.output else source.parent / (valid_name(title) + ".html")
    same = output.resolve() == source.resolve()
    if same and not is_bundled(doc):
        raise DeckError(f"원고를 덮어쓸 수 없습니다: {source} · -o에 다른 결과 경로를 지정하세요")
    if output.exists() and not same and not args.force and saved_meta(Document(read_text(output))):
        raise DeckError(f"편집기로 저장한 결과를 덮어쓸 수 없습니다: {output} · unpack으로 풀어서 고치거나 --force를 사용하세요")
    html_tag = clean_html_tag(root_tag(doc, "html"))
    html_node = next((n for n in doc.nodes if n.tag == "html"), None)
    if html_node is None or "data-theme" not in html_node.attrs:
        html_tag = edit_tag(html_tag, {"data-theme": "light"})
    body_tag = root_tag(doc, "body")
    saved = saved_meta(doc)
    # 연결한 CSS·스크립트에 쓰이는 문자도, 묶은 뒤의 내용에서 함께 찾는다.
    basefont = make_basefont(args, used_font_characters(doc, (kit + "\n" + "\n".join(styles), engine_css,
                                                               engine_js + "\n" + "\n".join(scripts))), warnings)
    credit = ""
    if basefont:
        line = read_text(asset_dir / "fonts" / "NOTICE.txt").splitlines()[0]
        credit = '<meta name="deck-credit" content="' + html.escape(line, quote=True) + '">\n'
    student_output = output.with_name(output.stem + " (학생용)" + output.suffix) if getattr(args, "student", False) else None
    if student_output and student_output.resolve() == source.resolve():
        raise DeckError(f"원고를 덮어쓸 수 없습니다: {source}")
    if student_output and student_output.exists() and not args.force and saved_meta(Document(read_text(student_output))):
        raise DeckError(f"편집기로 저장한 학생용 결과를 덮어쓸 수 없습니다: {student_output} · --force를 사용하세요")
    result = ("<!doctype html>\n" + html_tag + "\n<head>\n<meta charset=\"utf-8\">\n"
              '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
              '<meta name="generator" content="pbj-html-deck 3">\n'
              + credit + (saved + "\n" if saved else "") + "<title>" + html.escape(title) + "</title>\n"
              + basefont + '<style data-deck="kit">' + kit + "</style>\n"
              + '<style data-deck="engine">' + engine_css + "</style>\n"
              + '<style data-deck="fonts">' + safe_inline("\n".join(fonts), "style") + "</style>\n"
              + "\n".join(styles) + "\n</head>\n" + body_tag + '\n<div class="viewer"><main id="deck">\n'
              + "\n".join(built_slides) + "\n</main></div>\n"
              + '<script data-deck="engine">' + engine_js + "</script>\n"
              + "\n".join(scripts) + "\n</body>\n</html>\n")
    write_text(output, result)
    if student_output:
        student_tag = edit_tag(html_tag, {"data-lock": "view"})
        write_text(student_output, result.replace(html_tag, student_tag, 1))
        print(f"학생용: {student_output}")
    for warning in warnings:
        print("경고: " + warning)
    print(f"{len(slides)}장 · {output} · {output.stat().st_size:,}바이트 · 경고 {len(warnings)}건")
    return 0


class Extractor:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.contents = {}
        self.counters = {}

    def extract(self, url, preferred=None):
        match = re.match(r"^data:([^,]*?),(.*)$", url, re.I | re.S)
        if not match:
            return url
        metadata, payload = match.groups()
        mime = metadata.split(";", 1)[0].lower() or "application/octet-stream"
        fragment = ""
        if "#" in payload:
            payload, fragment = payload.split("#", 1)
            fragment = "#" + fragment
        try:
            raw = urllib.parse.unquote_to_bytes(payload)
            data = (base64.b64decode(re.sub(rb"\s+", b"", raw), validate=True)
                    if re.search(r";base64(?:;|$)", metadata, re.I) else raw)
        except (ValueError, base64.binascii.Error) as exc:
            raise DeckError("data URL의 내용이 올바르지 않습니다 · 원본 파일을 확인하세요") from exc
        digest = hashlib.sha256(data).digest()
        if digest in self.contents:
            return self.contents[digest] + fragment
        suffix = EXTENSIONS.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        category = "그림" if mime.startswith("image/") else ("글꼴" if mime.startswith("font/") or "font" in mime else "자료")
        label = {"그림": "그림", "글꼴": "글꼴", "자료": "자료"}[category]
        preferred_path = urllib.parse.unquote(urllib.parse.urlsplit((preferred or "").replace("\\", "/")).path)
        name = valid_name(Path(preferred_path).name) if preferred_path else ""
        if not name or name == "제목 없음":
            self.counters[category] = self.counters.get(category, 0) + 1
            name = f"{label}-{self.counters[category]:02d}{suffix}"
        elif MIMES.get(Path(name).suffix.lower()) != mime:
            # 편집기에서 다른 형식의 그림으로 바꾼 경우에는 실제 MIME을 따른다.
            name = Path(name).stem + suffix
        directory = self.folder / category
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        stem, ext = path.stem, path.suffix
        index = 2
        while path.exists():
            if path.is_file() and path.read_bytes() == data:
                break
            path = directory / f"{stem}-{index}{ext}"
            index += 1
        if not path.exists():
            path.write_bytes(data)
        relative = category + "/" + path.name
        self.contents[digest] = relative
        return relative + fragment

    def css(self, css):
        def replace(match):
            value = match.group(2) if match.group(1) else match.group(3).strip()
            extracted = self.extract(value)
            if extracted == value:
                return match.group(0)
            group = 2 if match.group(1) else 3
            start, end = match.start(group) - match.start(), match.end(group) - match.start()
            return match.group(0)[:start] + extracted + match.group(0)[end:]
        return css_urls(css, replace)

    def transform(self, fragment):
        doc = Document(fragment)
        edits = []
        for n in doc.nodes:
            if n.under(lambda parent: is_runtime(parent) or is_common(parent)):
                continue
            if is_runtime(n) or is_common(n):
                edits.append((n.start, n.end, ""))
                continue
            attrs = n.attrs
            updates = {}
            metadata = attrs.get("data-deck-src")
            try:
                mapping = json.loads(metadata) if metadata and metadata.startswith("{") else {}
                if not isinstance(mapping, dict):
                    mapping = {}
            except ValueError:
                mapping = {}
            refs = []
            if n.tag in ("img", "source", "video", "audio") and "src" in attrs:
                refs.append("src")
            if n.tag == "video" and "poster" in attrs:
                refs.append("poster")
            if n.tag == "image":
                refs += [k for k in ("href", "xlink:href") if k in attrs]
            converted = False
            if "data-placeholder" in attrs and metadata:
                updates.update({"src": metadata, "data-placeholder": None, "data-deck-src": None})
                refs = [key for key in refs if key != "src"]
            for key in refs:
                value = attrs[key] or ""
                if value.lower().startswith("data:"):
                    preferred = mapping.get(key) if mapping else metadata
                    updates[key] = self.extract(value, preferred)
                    converted = True
            if converted and not metadata:
                updates["data-deck-unpack-nometa"] = True
            if "style" in attrs:
                style = self.css(attrs["style"] or "")
                if style != attrs["style"]:
                    updates["style"] = style
            if n.tag == "style":
                edits.append((n.start_end, n.end_start, self.css(doc.inner(n))))
            if updates:
                edits.append((n.start, n.start_end, edit_tag(doc.start_tag(n), updates)))
        return changes(fragment, edits)


def unpack_command(args):
    source = Path(args.input)
    doc = Document(read_text(source))
    if not is_bundled(doc):
        raise DeckError(f"묶은 파일이 아닙니다: {source} · build 결과 파일을 선택하세요")
    slides = slides_of(doc)
    folder = Path(args.output)
    output = folder / "원고.html"
    if output.exists():
        raise DeckError(f"이미 원고가 있습니다: {output} · 다른 폴더를 선택하세요")
    folder.mkdir(parents=True, exist_ok=True)
    extractor = Extractor(folder)
    # 같은 자산이 CSS와 요소에 함께 쓰이면, 요소에 기록된 원래 이름을 우선한다.
    for node in doc.nodes:
        if skipped(node) or not node.attrs.get("data-deck-src") or "data-placeholder" in node.attrs:
            continue
        metadata = node.attrs["data-deck-src"]
        try:
            mapping = json.loads(metadata) if metadata.startswith("{") else {}
        except ValueError:
            mapping = {}
        if not isinstance(mapping, dict):
            mapping = {}
        for key in ("src", "poster", "href", "xlink:href"):
            value = node.attrs.get(key) or ""
            if value.lower().startswith("data:"):
                extractor.extract(value, mapping.get(key) if mapping else metadata)
    styles, scripts = [], []
    for node in doc.nodes:
        if skipped(node) or node.under(is_slide):
            continue
        if node.tag == "style" and ("data-deck" not in node.attrs or node.attrs.get("data-deck") == "fonts"):
            if node.attrs.get("data-deck") == "fonts":
                content = extractor.css(doc.inner(node))
                if content.strip():
                    styles.append("<style>" + content + "</style>")
            else:
                styles.append(extractor.transform(doc.raw(node)))
        elif node.tag == "script" and "data-deck" not in node.attrs:
            scripts.append(extractor.transform(doc.raw(node)))
    extracted_slides = [extractor.transform(doc.raw(n)) for n in slides]
    title = html.escape(title_of(doc))
    result = ("<!doctype html>\n" + clean_html_tag(root_tag(doc, "html")) + "\n<head>\n"
              + '<meta charset="utf-8">\n<title>' + title + "</title>\n"
              + "\n".join(styles) + "\n</head>\n" + root_tag(doc, "body") + "\n"
              + "\n".join(extracted_slides) + "\n" + "\n".join(scripts)
              + "\n</body>\n</html>\n")
    try:
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(result.replace("\r\n", "\n").replace("\r", "\n"))
    except FileExistsError as exc:
        raise DeckError(f"이미 원고가 있습니다: {output} · 다른 폴더를 선택하세요") from exc
    print(f"{len(slides)}장 · {output} · 자산 {len(extractor.contents)}개")
    return 0


def screenshot(cdp, width, height):
    cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                                                    "deviceScaleFactor": 1, "mobile": False})
    # 배경으로 쓴 SVG 그림은 한 박자 늦게 그려진다. 두 프레임 뒤에 조금 더 기다렸다가 찍는다.
    cdp.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(() => requestAnimationFrame(r), 160))))", True)
    return cdp.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})["data"]


def console_problems(events, start, slide):
    result = []
    for event in events[start:]:
        method = event.get("method")
        params = event.get("params", {})
        text = None
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text")
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            text = " ".join(str(v.get("value", v.get("description", ""))) for v in params.get("args", []))
        elif method == "Log.entryAdded" and params.get("entry", {}).get("level") == "error":
            text = params["entry"].get("text")
        if text:
            result.append({"slide": slide, "level": "error", "code": "console-error",
                           "message": "콘솔 오류: " + text.splitlines()[0], "target": "", "rect": None})
    return result


def sheet_html(images):
    cells = []
    for number, title, data in images:
        caption = str(number) + (" · " + html.escape(title) if title else "")
        cells.append('<div class="cell"><img src="data:image/png;base64,' + data
                     + '"><div class="caption">' + caption + "</div></div>")
    return ("<!doctype html><meta charset=utf-8><style>*{box-sizing:border-box}"
            "html,body{margin:0;background:#e6e8ec;overflow:hidden}"
            "body{display:grid;grid-template-columns:repeat(3,640px);grid-auto-rows:388px;gap:12px}"
            ".cell{width:640px;height:388px}.cell img{display:block;width:640px;height:360px}"
            ".caption{height:28px;padding:0 8px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;"
            "color:#202735;font:16px/28px sans-serif}</style>" + "".join(cells))


def parse_slides(value, count):
    if not value:
        return list(range(count))
    try:
        nums = [int(s.strip()) for s in value.split(",")]
    except ValueError as exc:
        raise DeckError("--slides 값이 올바르지 않습니다 · 2,5처럼 쉼표로 장 번호를 지정하세요") from exc
    if not nums or any(n < 1 or n > count for n in nums):
        raise DeckError(f"--slides 장 번호가 범위를 벗어났습니다 · 1부터 {count}까지 지정하세요")
    return list(dict.fromkeys(n - 1 for n in nums))


# 캡처 직전: 등장 효과를 끝까지 보내고, 발표용 단추(펜 등)를 숨긴다.
SETTLE = ("document.documentElement.setAttribute('data-deck-shot', '');"
          "document.getAnimations().forEach(a => { try { a.finish(); } catch (e) {} })")
CHECK_IMAGE = re.compile(r"(sheet|part|slide|state)-\d{2,}\.png")
INTERACTIVE = "[data-quiz],[data-flip],[data-peek],[data-toggle],[data-tabs],[data-sort],[data-answer],[data-timer],[data-pick],[data-count],[data-lab]"
# 0.1.18의 정적 검사에서 원문 대신 계산값/구조 요약을 내는 형태. 이 경우에만 공용 부분을 다시 검사한다.
IMP_SUMMARIES = {
    "low-contrast": re.compile(r"\S+:1 \(need \S+:1\) — text #[\da-f]+ on #[\da-f]+", re.I),
    "nested-cards": re.compile(r"Card inside card \([\w-]+\)"),
    "cramped-padding": re.compile(r'<[\w-]+> ".*": children flush against .+ \(no inset\)'),
    "hard-offset-shadow": re.compile(r"box-shadow: .+: hard offset with no blur; .+"),
    "design-system-radius": re.compile(r'border-radius .+ on [\w-]+ ".*" is outside the DESIGN\.md rounded scale'),
    "design-system-color": re.compile(r'.+ on [\w-]+ ".*" is outside DESIGN\.md colors'),
}
IMP_COLOR = re.compile(r"#[\da-f]{3,8}(?![\w-])|rgba?\(\s*\d+[\s,]+\d+[\s,]+\d+\s*\)|\b(?:black|white)\b", re.I)
IMP_STYLE_PART = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\([^()]*\)|;''')
IMP_DATA = re.compile(r"data:[^\s'\"<>()]+", re.I)


def cli_hint(command, path, *options):
    return f'python -X utf8 "{Path(__file__).resolve().as_posix()}" {command} "{Path(path).resolve().as_posix()}"' + (
        " " + " ".join(options) if options else "")


def imp_normalize(text):
    # detect는 공백과 쉼표 등을 다시 쓰므로 간격/따옴표만 정리한다. 식별자 사이 공백은 보존한다.
    text = IMP_DATA.sub("data:…", text)
    text = text.replace("'", '"').strip()
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s*([:;,()=])\s*", r"\1", text)


def imp_style_parts(style):
    start = 0
    for match in IMP_STYLE_PART.finditer(style):
        if match.group() == ";":
            yield style[start:match.start()].strip()
            start = match.end()
    yield style[start:].strip()


def imp_tag_key(node):
    # build가 바꾸는 자산 주소와 쪽 표식은 틀 대조에 쓰지 않는다.
    exclude = {"style", "data-slide", "data-deck-page"}
    if node.tag == "img" and "data-asset" in node.attrs:
        exclude.update(("src", "data-deck-src", "data-placeholder"))
    return node.tag, tuple(sorted((key, value) for key, value in node.attrs.items() if key not in exclude))


def imp_template_nodes(doc, source, assets):
    """콘티에서 다시 렌더한 틀과 같은 태그만 공용으로 본다. 추가 노드나 바뀐 부모는 작성자 쪽이다."""
    try:
        from deck_plan import ID_RE, PlanError, rendered_slide
    except ImportError:
        return {}
    plan_path = source.parent / "콘티.json"
    if not plan_path.is_file():
        return {}
    try:
        plan = json.loads(read_text(plan_path))
        slides = {row["id"]: row for row in plan["slides"]}
    except (DeckError, ValueError, TypeError, KeyError):
        return {}
    result = {}
    for slide in slides_of(doc):
        spec = slides.get(slide.attrs.get("data-id"), {})
        layout = spec.get("layout")
        if not isinstance(layout, str) or not ID_RE.fullmatch(layout) or slide.attrs.get("data-layout") != layout:
            continue
        try:
            reference = Document(rendered_slide(plan, spec, Path(assets) / "kit", omit_raw_markup=True)).nodes
        except (DeckError, PlanError, ValueError, TypeError, KeyError):
            continue
        nodes = [node for node in doc.nodes if slide.start <= node.start < slide.end]
        aligned = SequenceMatcher(None, list(map(imp_tag_key, nodes)), list(map(imp_tag_key, reference)), autojunk=False)
        for actual, expected, count in aligned.get_matching_blocks():
            for node, model in zip(nodes[actual:actual+count], reference[expected:expected+count]):
                if node is slide or (node.parent in result and result[node.parent] is model.parent):
                    result[node] = model
    return result


def imp_origin_parts(doc, template_nodes):
    """공용/작성자 조각과, 계산형 지적을 검증할 공용 부분만의 HTML을 분리한다."""
    shared, authored, edits, removed = [], [], [], set()
    for node in doc.nodes:
        if is_common(node) or node.under(is_common):
            shared.append(doc.raw(node))
            continue
        if is_runtime(node) or node.under(is_runtime):
            continue
        matched = template_nodes.get(node)
        attrs = node.attrs
        if node.tag in ("style", "script"):
            authored.append(doc.inner(node))
        tag = doc.start_tag(node)
        # 태그에 적힌 style 전체를 다시 사용하지 않는다. 틀에 없는 선언만 작성자 쪽에 남긴다.
        bare = edit_tag(tag, {"style": None}) if "style" in attrs else tag
        if not matched:
            authored.append(tag)
        original = filter(None, imp_style_parts(matched.attrs.get("style") or "")) if matched else ()
        available = Counter(map(imp_normalize, original))
        kept, custom = [], []
        for declaration in filter(None, imp_style_parts(attrs.get("style") or "")):
            normalized = imp_normalize(declaration)
            if available[normalized]:
                available[normalized] -= 1
                kept.append(declaration)
            else:
                custom.append(declaration)
        if kept:
            shared.append(";".join(kept))
        if custom:
            authored.append(";".join(custom))
        if matched:
            shared.append(bare)
        if node.parent in removed:
            removed.add(node)
        elif node.tag in ("style", "script", "link") or (not matched and (is_slide(node) or node.under(is_slide))):
            removed.add(node)
            edits.append((node.start, node.end, ""))
        elif "style" in attrs and len(kept) != len(list(filter(None, imp_style_parts(attrs.get("style") or "")))):
            edits.append((node.start, node.start_end, edit_tag(tag, {"style": ";".join(kept) or None})))
    return [imp_normalize(s) for s in shared], [imp_normalize(s) for s in authored], changes(doc.text, edits)


def imp_colors(text):
    for match in IMP_COLOR.finditer(text):
        color = match.group().lower()
        if color[0] == "#":
            yield "#" + "".join(c * 2 for c in color[1:]) if len(color) in (4, 5) else color
        elif color in ("black", "white"):
            yield "#000000" if color == "black" else "#ffffff"
        else:
            channels = list(map(int, re.findall(r"\d+", color)))
            if all(n <= 255 for n in channels):
                yield "#" + "".join(f"{n:02x}" for n in channels)


def imp_has(parts, snippet):
    text = imp_normalize(snippet)
    if not text:
        return False
    start = r"(?<![\w-])" if re.match(r"[\w-]", text[0]) else ""
    end = r"(?![\w-])" if re.match(r"[\w-]", text[-1]) else ""
    pattern = re.compile(start + re.escape(text) + end)
    return any(pattern.search(part) for part in parts)


def imp_matches(rule, snippet, parts, colors, *, author=False):
    if imp_has(parts, snippet):
        return True
    if (rule == "design-system-color" and (author or snippet.startswith("Undocumented color "))) or (author and rule == "low-contrast"):
        # 계산된 색은 공용 CSS에 같은 값이 있다는 이유만으로 숨기지 않는다.
        value = snippet.split(' on ', 1)[0] if rule == "design-system-color" else snippet
        return bool(set(imp_colors(value)) & colors)
    radius = re.match(r"border-radius:\s*(.*?) is outside the DESIGN\.md rounded scale$", snippet)
    if author and not radius:
        radius = re.match(r'border-radius (\S+) on [\w-]+ "', snippet)
    if radius and rule == "design-system-radius":
        return imp_has(parts, "border-radius:" + radius[1])
    if rule in ("side-tab", "border-accent-on-rounded"):
        pair = re.fullmatch(r"(border-(?:bottom|left|right|top):\s*[\d.]+px) \+ (border-radius:\s*[\d.]+px)", snippet)
        if pair:
            return any(imp_has((block,), pair[1]) and imp_has((block,), pair[2])
                       for part in parts for block in re.split(r"[{}]", part))
    if author and rule == "hard-offset-shadow":
        return imp_has(parts, snippet.partition(": hard offset")[0])
    return False


def imp_signature(item):
    rule = str(item.get("antipattern") or item.get("rule") or "unknown")
    snippet = item.get("snippet")
    return rule, snippet if isinstance(snippet, str) else ""


def imp_shared_summaries(executable, source, projection):
    # HTML 파일의 정적 분석을 그대로 쓰되, 새 덱에서 쓰는 디자인 문맥도 동일하게 둔다.
    with tempfile.TemporaryDirectory(prefix="deck-imp-") as temp:
        folder = Path(temp)
        for name in ("DESIGN.md", "PRODUCT.md"):
            context = next((parent / name for parent in source.parents if (parent / name).is_file()), None)
            if context:
                write_text(folder / name, read_text(context))
        target = folder / source.name
        write_text(target, projection)
        found = impeccable_json(executable, ["detect", "--json", target])
    return Counter(imp_signature(item) for item in found if isinstance(item, dict)) if isinstance(found, list) else Counter()


def impeccable_problems(executable, source, assets):
    findings = impeccable_json(executable, ["detect", "--json", source])
    if not isinstance(findings, list):
        raise DeckError("임페커블 detect 결과가 배열이 아닙니다")
    rows = [(item, imp_signature(item)) for item in findings if isinstance(item, dict)]
    if not rows:
        return [], Counter()
    doc = Document(read_text(source))
    shared, authored, projection = imp_origin_parts(doc, imp_template_nodes(doc, source, assets))
    author_colors = set(color for part in authored for color in imp_colors(part))
    shared_colors = set(color for part in shared for color in imp_colors(part))
    totals = Counter(key for _, key in rows)
    proven = None
    problems, hidden = [], Counter()
    for item, (rule, snippet) in rows:
        file = item.get("file")
        local = not file or (source.parent / str(file)).resolve() == source
        if local and snippet.strip() and not imp_matches(rule, snippet, authored, author_colors, author=True):
            if imp_matches(rule, snippet, shared, shared_colors):
                hidden[rule] += 1
                continue
            if rule in IMP_SUMMARIES and IMP_SUMMARIES[rule].fullmatch(snippet):
                if proven is None:
                    try:
                        proven = imp_shared_summaries(executable, source, projection)
                    except (DeckError, OSError, subprocess.SubprocessError):
                        proven = Counter()  # 출처 확인에 실패해도 본 검사의 지적은 그대로 보여 준다.
                key = (rule, snippet)
                if proven[key] == totals[key]:
                    hidden[rule] += 1
                    continue
        problems.append({"slide": None, "level": "warn", "code": "imp-" + rule,
                         "message": str(item.get("name") or rule) + " · " + snippet[:140],
                         "target": "", "rect": None})
    return problems, hidden


def find_impeccable():
    configured = os.environ.get("IMPECCABLE_BIN")
    if configured and Path(configured).is_file():
        return str(Path(configured))
    folder = Path.home() / ".impeccable" / "bin"
    def version(path):
        return tuple(int(n) for n in re.findall(r"\d+", path.parent.name)) or (0,)
    candidates = list(folder.glob("*/impeccable.exe")) if folder.is_dir() else []
    return str(max(candidates, key=version)) if candidates else None


def impeccable_json(executable, arguments):
    result = subprocess.run([executable, *map(str, arguments)], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=120,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        detail = (result.stderr or result.stdout).strip()[:400]
        raise DeckError(f"임페커블 결과를 읽을 수 없습니다 (종료 {result.returncode}): {detail}") from exc


def plan_problems(source, assets):
    plan_path = source.parent / "콘티.json"
    if not plan_path.is_file():
        return []
    plan = json.loads(read_text(plan_path))
    layouts_path = Path(assets) / "kit" / "layouts.json"
    layouts = json.loads(read_text(layouts_path)) if layouts_path.is_file() else {}
    if isinstance(layouts, dict) and isinstance(layouts.get("layouts"), dict):
        layouts = layouts["layouts"]
    document = Document(read_text(source))
    actual = {node.attrs.get("data-id"): node for node in slides_of(document) if node.attrs.get("data-id")}
    attr_for = {"quiz": "data-quiz", "sort": "data-sort", "flip": "data-flip", "tabs": "data-tabs",
                "lab": "data-lab", "reveal": "data-step", "input": "data-answer", "pick": "data-pick",
                "toggle": "data-toggle", "peek": "data-peek", "timer": "data-timer", "count": "data-count"}
    result = []
    for index, slide in enumerate(plan.get("slides", []), 1):
        ident, layout = slide.get("id"), slide.get("layout")
        node = actual.get(ident)
        issue = None
        if node is None:
            issue = f"콘티의 장 {ident}이 원고에 없습니다"
        elif node.attrs.get("data-layout") != layout:
            issue = f"콘티의 구도 {layout}와 원고의 구도가 다릅니다: {ident}"
        else:
            entry = layouts.get(layout, {}) if isinstance(layouts, dict) else {}
            interaction = entry.get("interaction") if isinstance(entry, dict) else None
            required = attr_for.get(interaction) if isinstance(interaction, str) else None
            if required and not any(required in child.attrs and (child is node or child.under(lambda p: p is node))
                                    for child in document.nodes):
                issue = f"콘티의 상호작용 {interaction}이 원고에 없습니다: {ident}"
        if issue:
            result.append({"slide": index, "level": "error", "code": "plan-mismatch", "message": issue, "target": str(ident or ""), "rect": None})
    return result


def check_command(args):
    source = Path(args.input).resolve()
    deck_folder = source if source.is_dir() else None
    if deck_folder:
        draft = deck_folder / "원고.html"
        if not draft.is_file():
            raise DeckError(f"원고가 없습니다: {draft} · {cli_hint('scaffold', deck_folder)}")
        source = deck_folder / (valid_name(title_of(Document(read_text(draft)))) + ".html")
    if not source.is_file():
        hint = cli_hint('build', deck_folder) if deck_folder else "결과 파일 경로를 확인하세요"
        raise DeckError(f"검사할 파일이 없습니다: {source} · {hint}")
    out = Path(args.out) if args.out else source.parent / "검사"
    problems = plan_problems(source, getattr(args, "assets", ASSETS))
    files = []
    images = []
    with Browser() as browser:
        cdp = browser.cdp
        for domain in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
            cdp.call(domain)
        # 외부 요청은 오프라인으로 막는다(주소 패턴으로 막으면 http:// 글자가 든 SVG 데이터 주소까지 막힌다).
        cdp.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                                                      "downloadThroughput": -1, "uploadThroughput": -1})
        version = cdp.call("Browser.getVersion").get("product", "알 수 없음")
        cdp.call("Page.navigate", {"url": source.as_uri()})
        cdp.wait_event("Page.loadEventFired", 20)
        ready = cdp.evaluate("""(async()=>{if(!window.deck)return 'missing';
          try {return await Promise.race([Promise.resolve(window.deck.ready).then(()=> 'ready'),
          new Promise(r=>setTimeout(()=>r('timeout'),20000))]);} catch(e){return 'rejected:'+String(e)}})()""", True)
        if ready == "missing":
            raise DeckError(f"pbj-html-deck으로 묶은 파일이 아닙니다: {source} · build 결과를 선택하세요")
        if ready != "ready":
            raise DeckError(f"덱 준비에 실패했습니다: {source} ({ready}) · 엔진과 자산을 확인하세요")
        cursor = 0
        problems.extend(console_problems(cdp.events, cursor, None))
        cursor = len(cdp.events)
        count = cdp.evaluate("window.deck.count")
        if not isinstance(count, int) or count < 1:
            raise DeckError(f"장의 수가 올바르지 않습니다: {source} · 엔진과 장을 확인하세요")
        selected = parse_slides(args.slides, count)
        # 일부 장만 본 모아보기는 part-NN.png로 따로 쓴다. 전체 모아보기(sheet-NN.png)와 섞이지 않는다.
        partial = selected != list(range(count))
        cdp.evaluate("Promise.resolve(window.deck.setMode('present'))", True)
        has_selftest = cdp.evaluate("typeof window.deck.selftest === 'function'")
        if not has_selftest:
            print("알림: 엔진에 deck.selftest가 없어 상호작용 자체 시험을 건너뜁니다")
        out.mkdir(parents=True, exist_ok=True)
        for index in selected:
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 640, "height": 360,
                                                            "deviceScaleFactor": 1, "mobile": False})
            cdp.evaluate(f"Promise.resolve(window.deck.go({index},'all'))", True)
            # 단계 등장 효과가 덜 끝난 채 재거나 찍히지 않게 끝까지 보낸다.
            cdp.evaluate(SETTLE)
            cdp.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))", True)
            audit = cdp.evaluate(f"Promise.resolve(window.deck.audit({index}))", True)
            if not isinstance(audit, list):
                raise DeckError(f"{index + 1}번 장의 검사 결과가 배열이 아닙니다 · 엔진의 deck.audit를 확인하세요")
            problems.extend(audit)
            # 장 이름은 엔진과 같은 규칙으로 읽는다(data-title 먼저, 없으면 제목 글자).
            # 예전 엔진으로 묶은 파일에는 deck.title이 없으므로 같은 순서로 직접 읽는다.
            title = cdp.evaluate(f"""(()=>{{
              if (typeof window.deck.title === 'function') return window.deck.title({index});
              const slide = document.querySelectorAll('#deck > section.slide[data-slide]')[{index}];
              const named = (slide && slide.getAttribute('data-title') || '').replace(/\\s+/g, ' ').trim();
              const heading = slide && slide.querySelector('h1,h2');
              return named || (heading ? heading.innerText : '').replace(/\\s+/g, ' ').trim();
            }})()""")
            cdp.evaluate(SETTLE)
            img = screenshot(cdp, 640, 360)
            images.append((index + 1, title, img))
            if args.shots:
                png = screenshot(cdp, 1920, 1080)
                name = f"slide-{index + 1:02d}.png"
                (out / name).write_bytes(base64.b64decode(png))
                files.append(name)
            if has_selftest:
                try:
                    test = cdp.evaluate(f"Promise.resolve(window.deck.selftest({index},{{hold:true}}))", True)
                    if not isinstance(test, list):
                        raise DeckError(f"{index + 1}번 장의 자체 시험 결과가 배열이 아닙니다")
                    problems.extend(test)
                    interactive = cdp.evaluate(f"!!document.querySelectorAll('#deck > section.slide[data-slide]')[{index}].querySelector({json.dumps(INTERACTIVE)})")
                    if interactive:
                        cdp.evaluate(SETTLE)
                        name = f"state-{index + 1:02d}.png"
                        (out / name).write_bytes(base64.b64decode(screenshot(cdp, 1920, 1080)))
                        files.append(name)
                finally:
                    cdp.evaluate("Promise.resolve(window.deck.reset())", True)
            problems.extend(console_problems(cdp.events, cursor, index + 1))
            cursor = len(cdp.events)
        for start in range(0, len(images), 12):
            group = images[start:start + 12]
            rows = (len(group) + 2) // 3
            width, height = 3 * 640 + 2 * 12, rows * (360 + 28) + (rows - 1) * 12
            page = base64.b64encode(sheet_html(group).encode("utf-8")).decode("ascii")
            cdp.events = [e for e in cdp.events if e.get("method") != "Page.loadEventFired"]
            cdp.call("Page.navigate", {"url": "data:text/html;base64," + page})
            cdp.wait_event("Page.loadEventFired", 20)
            cdp.evaluate("Promise.all([...document.images].map(i=>i.decode()))", True)
            image = screenshot(cdp, width, height)
            name = f"{'part' if partial else 'sheet'}-{start // 12 + 1:02d}.png"
            (out / name).write_bytes(base64.b64decode(image))
            files.append(name)
    impeccable = find_impeccable()
    hidden = {}
    if impeccable:
        imp_problems, hidden = impeccable_problems(impeccable, source, getattr(args, "assets", ASSETS))
        problems.extend(imp_problems)
    else:
        print("알림: 임페커블이 없어 detect 검사를 건너뜁니다")
    problems.sort(key=lambda p: (0 if p.get("level") == "error" else 1, p.get("slide") or 0))
    files.append("report.json")
    report = {"slide_count": count, "checked": [index + 1 for index in selected],
              "browser_version": version, "problems": problems, "files": files,
              "imp_hidden": {"total": sum(hidden.values()), "by_rule": dict(sorted(hidden.items()))}}
    write_text(out / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for p in problems:
        place = f"{p.get('slide')}번 장" if p.get("slide") else "문서"
        level = "오류" if p.get("level") == "error" else "경고"
        print(f"{level} · {place} · {p.get('code', '')} · {p.get('message', '')} · {p.get('target', '')}")
    errors = sum(p.get("level") == "error" for p in problems)
    warns = sum(p.get("level") == "warn" for p in problems)
    # 이번 검사에서 쓰지 않은 그림이 폴더에 있으면 지난 검사 것이다. 건드리지 않고 알리기만 한다.
    old = sorted(item.name for item in out.iterdir()
                 if item.is_file() and CHECK_IMAGE.fullmatch(item.name) and item.name not in files)
    if old:
        shown = ", ".join(old[:4]) + (" …" if len(old) > 4 else "")
        print(f"알림 · 지난 검사의 그림 {len(old)}개가 폴더에 남아 있습니다 ({shown}) · 이번 검사 화면이 아닙니다")
    scope = f" · {count}장 중 {len(selected)}장만 검사" if partial else ""
    print(f"오류 {errors} · 경고 {warns}{scope} · 모아보기 경로 {out}")
    sheets = [name for name in files if name.startswith(("sheet-", "part-")) and name.endswith(".png")]
    for index, name in enumerate(sheets):
        if hidden and index == len(sheets) - 1:
            print(f"알림 · 부품이 의도한 임페커블 지적 {sum(hidden.values())}건은 숨겼습니다")
        print((out / name).resolve())
    return 1 if errors else 0


class KoreanParser(argparse.ArgumentParser):
    def format_help(self):
        text = super().format_help()
        return (text.replace("usage:", "사용법:").replace("positional arguments:", "위치 인자:")
                .replace("options:", "옵션:").replace("show this help message and exit", "도움말을 표시합니다"))

    def error(self, message):
        replacements = {"the following arguments are required:": "필수 인자가 없습니다:",
                        "unrecognized arguments:": "알 수 없는 인자:", "invalid choice:": "올바르지 않은 선택:",
                        "expected one argument": "값이 필요합니다", "argument": "인자"}
        for old, new in replacements.items():
            message = message.replace(old, new)
        self.exit(2, f"오류: {message} · --help로 사용법을 확인하세요\n")


def parser():
    root = KoreanParser(description="HTML 수업 슬라이드 원고를 만들고 묶고 검사합니다.")
    root.add_argument("--assets", default=str(ASSETS), help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True, title="명령")
    b = commands.add_parser("build", help="원고를 하나의 HTML로 묶습니다", description="원고 또는 묶은 HTML을 자산과 함께 묶습니다.")
    b.add_argument("input", metavar="덱폴더|원고.html", help="덱 폴더 또는 원고 파일")
    b.add_argument("-o", "--output", metavar="결과.html", help="결과 HTML 경로")
    b.add_argument("--force", action="store_true", help="편집기로 저장된 다른 결과도 덮어씁니다")
    b.add_argument("--student", action="store_true", help="편집을 잠근 학생용 파일도 만듭니다")
    b.add_argument("--no-font", action="store_true", help="기본 글꼴 내장을 건너뜁니다")
    b.set_defaults(function=build_command)
    c = commands.add_parser("check", help="브라우저에서 장을 검사합니다", description="실제 브라우저로 장을 검사하고 모아보기를 만듭니다.")
    c.add_argument("input", metavar="덱폴더|결과.html", help="덱 폴더 또는 묶은 HTML 파일")
    c.add_argument("--out", metavar="폴더", help="검사 결과 폴더 (기본 검사/)")
    c.add_argument("--shots", action="store_true", help="각 장의 전체 크기 PNG도 저장합니다")
    c.add_argument("--slides", metavar="2,5", help="검사할 장 번호를 쉼표로 구분합니다")
    c.set_defaults(function=check_command)
    u = commands.add_parser("unpack", help="묶은 파일을 원고와 자산으로 풉니다", description="묶은 HTML을 원고와 자산으로 풉니다.")
    u.add_argument("input", metavar="결과.html", help="묶은 HTML 파일")
    u.add_argument("-o", "--output", required=True, metavar="폴더", help="원고와 자산을 만들 폴더")
    u.set_defaults(function=unpack_command)
    # 선택 모듈이 없으면 그 모듈의 명령만 생략한다. 모듈 내부의 오류는 숨기지 않는다.
    import importlib
    sys.modules.setdefault("deck", sys.modules[__name__])
    for name in ("deck_plan", "deck_art"):
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
        else:
            module.register(commands)
    for command in set(commands.choices.values()):
        if "--assets" not in command._option_string_actions:
            command.add_argument("--assets", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return root


def main():
    args = parser().parse_args()
    try:
        return args.function(args)
    except (DeckError, BrowserError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"오류: 파일 또는 브라우저 처리에 실패했습니다 ({exc}) · 경로와 실행 환경을 확인하세요", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
