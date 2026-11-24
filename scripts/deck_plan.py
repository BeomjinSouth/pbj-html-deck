#!/usr/bin/env python3
"""콘티 검사, 구도 틀 렌더링, 원고 뼈대 및 작업 상태."""

import argparse
import ast
import difflib
from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass

SKILL = Path(__file__).resolve().parent.parent
DEFAULT_KIT = SKILL / "assets" / "kit"
ID_RE = re.compile(r"[a-z0-9-]+\Z")
ASSET_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
COMMON = {"id", "layout", "purpose", "focus", "title", "tag", "mascot", "say",
          "reveal", "talk", "mockup_note", "mockup"}
META_FIELDS = {"title", "kind", "audience", "duration", "goal", "message", "palette"}
ASSET_KINDS = {"mascot_master", "mascot", "sticker", "scene", "background"}
# 그림 기준은 실제 장에 쓰인 에셋만 센다. 백분율은 정수 비교로 경계값을 정확히 지킨다.
VISUAL_LIMITS = {"min_slides": 6, "mascot_min": 3, "mascot_recommended": 4, "other_min": 1,
                 "mascot_percent": 30, "image_percent": 40, "empty_warning": 2, "empty_run": 4}
REACTION_LAYOUTS = {"quiz", "quiz-long", "ox", "sort", "order", "input"}


class PlanError(Exception):
    pass


def read_text(path):
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise PlanError(f"파일을 읽을 수 없습니다: {path} ({exc}) · UTF-8 파일과 경로를 확인하세요") from exc


def write_text(path, text, exclusive=False):
    try:
        with Path(path).open("x" if exclusive else "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
    except OSError as exc:
        raise PlanError(f"파일을 쓸 수 없습니다: {path} ({exc}) · 폴더와 권한을 확인하세요") from exc


def read_json(path):
    try:
        return json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise PlanError(f"JSON을 읽을 수 없습니다: {path} · {exc.lineno}줄 {exc.colno}번째 글자를 확인하세요") from exc


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def kit_path(args):
    return Path(getattr(args, "kit", None) or os.environ.get("DECK_KIT") or DEFAULT_KIT)


def load_layouts(args):
    path = kit_path(args) / "layouts.json"
    result = read_json(path)
    if not isinstance(result, dict) or not all(isinstance(v, dict) for v in result.values()):
        raise PlanError(f"등록부 형식이 올바르지 않습니다: {path} · 구도 이름과 설정 객체를 확인하세요")
    return result


def load_palettes(args):
    """부품이 정한 색 묶음 이름들. palettes.json이 없는 부품이면 빈 목록(검사하지 않는다)."""
    path = kit_path(args) / "palettes.json"
    if not path.is_file():
        return []
    result = read_json(path)
    return [name for name in result if not name.startswith("_")] if isinstance(result, dict) else []


# 결과는 (MathML, 글자만) 쌍이다. 각 명령의 인자는 재귀적으로 해석한다.
SYMBOLS = {
    "times": "×", "div": "÷", "pm": "±", "le": "≤", "ge": "≥", "ne": "≠",
    "approx": "≈", "cdot": "⋅", "angle": "∠", "triangle": "△", "square": "□",
    "perp": "⊥", "parallel": "⫽", "equiv": "≡", "sim": "∽", "pi": "π",
    "circ": "°", "therefore": "∴", "because": "∵", "cdots": "⋯",
}
SUPERS = str.maketrans("0123456789+-=()nix", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱˣ")
SUBS = str.maketrans("0123456789+-=()aeiox", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒₓ")


def wrap_row(parts):
    markup = "".join(p[0] for p in parts)
    return (markup if len(parts) == 1 else "<mrow>" + markup + "</mrow>",
            "".join(p[1] for p in parts))


# 조작 값: 장의 state로 처음 값을 미리 계산해 적는다. 열린 뒤에는 엔진(data-show)이 같은 식으로 다시 채운다.
LIVE_FUNCS = {name: getattr(math, name) for name in
              ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "hypot", "floor", "ceil", "log", "exp", "trunc")}
LIVE_FUNCS.update({"abs": abs, "min": min, "max": max, "pow": pow, "round": lambda x: math.floor(x + 0.5),
                   "sign": lambda x: (x > 0) - (x < 0), "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x)})
LIVE_NAMES = {"PI": math.pi, "E": math.e, "SQRT2": math.sqrt(2), "true": True, "false": False}


def live_eval(expression, state):
    """JS 식 가운데 파이썬과 뜻이 같은 꼴만 계산한다. 그 밖(삼항 등)은 ValueError — 브라우저가 채운다."""
    source = re.sub(r"\bMath\.", "", str(expression)).replace("!==", " is not ").replace("===", " is ")
    source = re.sub(r"!(?!=)", " not ", source.replace("&&", " and ").replace("||", " or ")).strip()
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ValueError(str(exc)) from exc

    def truthy(value):
        # JS에서 빈 배열·객체는 참이다.
        return bool(value) if value is None or isinstance(value, (bool, int, float, str)) else True

    def strict_equal(a, b):
        if isinstance(a, bool) or isinstance(b, bool):
            return isinstance(a, bool) and isinstance(b, bool) and a == b
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return a == b
        if isinstance(a, str) and isinstance(b, str):
            return a == b
        if a is None or b is None:
            return a is None and b is None
        raise ValueError("객체 비교")

    def walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in state:
                return state[node.id]
            if node.id in LIVE_NAMES:
                return LIVE_NAMES[node.id]
            raise ValueError(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
            value = walk(node.operand)
            return (not truthy(value)) if isinstance(node.op, ast.Not) else -value if isinstance(node.op, ast.USub) else +value
        if isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod):
            a, b = walk(node.left), walk(node.right)
            if isinstance(a, str) or isinstance(b, str):
                raise ValueError("글자 계산")
            if isinstance(node.op, ast.Pow) and abs(b) > 1024:
                raise ValueError("너무 큰 거듭제곱")
            return {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Div: lambda: a / b,
                    ast.Pow: lambda: a ** b, ast.Mod: lambda: math.fmod(a, b)}[type(node.op)]()
        if isinstance(node, ast.BoolOp):
            value = walk(node.values[0])
            for child in node.values[1:]:
                if truthy(value) != isinstance(node.op, ast.And):
                    return value
                value = walk(child)
            return value
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op, right in zip(node.ops, node.comparators):
                right = walk(right)
                tests = {ast.Lt: lambda: left < right, ast.LtE: lambda: left <= right, ast.Gt: lambda: left > right,
                         ast.GtE: lambda: left >= right, ast.Eq: lambda: left == right, ast.NotEq: lambda: left != right,
                         ast.Is: lambda: strict_equal(left, right), ast.IsNot: lambda: not strict_equal(left, right)}
                if type(op) not in tests:
                    raise ValueError("비교")
                if not tests[type(op)]():
                    return False
                left = right
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in LIVE_FUNCS and not node.keywords:
            return LIVE_FUNCS[node.func.id](*[walk(arg) for arg in node.args])
        raise ValueError(type(node).__name__)
    try:
        return walk(tree.body)
    except (ArithmeticError, TypeError) as exc:
        raise ValueError(str(exc)) from exc


def live_show(value, fix=None):
    """엔진의 data-show와 같은 꼴: 자리 지정이 없으면 군더더기 없는 수, 음수의 빼기는 −."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if not isinstance(value, (int, float)):
        raise ValueError("지원하지 않는 조작 값")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("너무 큰 조작 값") from exc
    if not math.isfinite(value):
        raise ValueError("유한하지 않은 조작 값")
    if fix is not None:
        # JS toFixed는 정확한 이진 float에서 반올림하며 중간값은 0에서 멀어지게 한다.
        if abs(value) >= 1e21:
            raise ValueError("너무 큰 고정 소수")
        with localcontext() as context:
            context.prec = 350
            rounded = Decimal.from_float(value if value else 0.0).quantize(Decimal(1).scaleb(-int(fix)), rounding=ROUND_HALF_UP)
        text = f"{rounded:.{int(fix)}f}"
    else:
        value = round(value, 10)
        text = str(int(value)) if value.is_integer() else repr(value)
    return ("−" + text[1:]) if text.startswith("-") else text


def live_output(body, state, tag):
    """{식} · {식:자리} → 처음 값이 적힌 data-show 요소와 그 글자."""
    match = re.fullmatch(r"(.*):\s*(\d)\s*", body, re.S)
    expression, fix = (match.group(1), match.group(2)) if match else (body, None)
    expression = expression.strip()
    if not expression:
        raise PlanError("조작 값 {} 안에 식이 없습니다")
    try:
        value = live_show(live_eval(expression, state or {}), fix)
    except ValueError:
        value = "…"
    attrs = ' data-show="' + html.escape(expression, quote=True) + '"' + (f' data-fix="{fix}"' if fix else "")
    return f"<{tag}{attrs}>{html.escape(value)}</{tag}>", value


class MathParser:
    def __init__(self, source, live=None):
        self.source = source
        self.pos = 0
        self.live = live

    def fail(self, message, at=None):
        pos = self.pos if at is None else at
        raise PlanError(f"수식 ${self.source}$ · {pos + 1}번째 글자: {message}")

    def spaces(self):
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def group(self, opening="{", closing="}"):
        self.spaces()
        if self.pos >= len(self.source) or self.source[self.pos] != opening:
            self.fail(f"{opening}가 필요합니다")
        self.pos += 1
        result = self.sequence(closing)
        if self.pos >= len(self.source) or self.source[self.pos] != closing:
            self.fail(f"닫는 {closing}가 없습니다")
        if not result:
            self.fail(f"{opening}{closing} 안에 값이 없습니다")
        self.pos += 1
        return wrap_row(result)

    @staticmethod
    def prefix_sign(part):
        return ('<mo form="prefix" lspace="0em" rspace="0em">' + part[1] + '</mo>', part[1])

    def argument(self):
        self.spaces()
        if self.pos >= len(self.source):
            self.fail("첨자에 값이 필요합니다")
        if self.source[self.pos] == "{":
            return self.group()
        if self.source[self.pos].isascii() and self.source[self.pos].isdigit():
            digit = self.source[self.pos]
            self.pos += 1
            return f"<mn>{digit}</mn>", digit
        part = self.atom()
        return self.prefix_sign(part) if part[0] in ("<mo>+</mo>", "<mo>−</mo>") else part

    def atom(self):
        self.spaces()
        if self.pos >= len(self.source):
            self.fail("값이 필요합니다")
        start = self.pos
        c = self.source[self.pos]
        self.pos += 1
        if c == "\\":
            match = re.match(r"[A-Za-z]+", self.source[self.pos:])
            if not match:
                self.fail("명령 이름이 필요합니다", start)
            name = match.group()
            self.pos += len(name)
            if name in SYMBOLS:
                symbol = SYMBOLS[name]
                if name in ("triangle", "square"):
                    return (f'<mi mathvariant="normal">{symbol}</mi>', symbol)
                if name == "pi":
                    return (f"<mi>{symbol}</mi>", symbol)
                return (f"<mo>{symbol}</mo>", symbol)
            if name == "quad":
                return ('<mspace width="1em"></mspace>', " ")
            if name == "val":
                self.spaces()
                if self.pos >= len(self.source) or self.source[self.pos] != "{":
                    self.fail("\\val 뒤에 {식}이 필요합니다")
                begin = self.pos + 1
                end = self.source.find("}", begin)
                if end < 0:
                    self.fail("\\val의 중괄호를 확인하세요")
                if self.live is None:
                    self.fail("\\val은 state가 있는 장(조작 탐구)에서만 씁니다", start)
                self.pos = end + 1
                return live_output(self.source[begin:end], self.live, "mn")
            if name == "frac":
                a, b = self.group(), self.group()
                return (f"<mfrac>{a[0]}{b[0]}</mfrac>", f"({a[1]})/({b[1]})")
            if name == "sqrt":
                self.spaces()
                index = self.group("[", "]") if self.pos < len(self.source) and self.source[self.pos] == "[" else None
                value = self.group()
                if index:
                    return (f"<mroot>{value[0]}{index[0]}</mroot>", f"√[{index[1]}]({value[1]})")
                return (f"<msqrt>{value[0]}</msqrt>", "√" + (value[1] if len(value[1]) == 1 else f"({value[1]})"))
            if name in ("overline", "overarc"):
                value = self.group()
                if name == "overline":
                    return (f'<mrow class="seg">{value[0]}</mrow>', value[1] + "̅")
                return (f'<mover>{value[0]}<mo stretchy="true">⏜</mo></mover>', value[1] + "⌒")
            if name == "text":
                self.spaces()
                if self.pos >= len(self.source) or self.source[self.pos] != "{":
                    self.fail("\\text 뒤에 {글자}가 필요합니다")
                begin = self.pos + 1
                end = self.source.find("}", begin)
                if end < 0 or "{" in self.source[begin:end]:
                    self.fail("\\text의 중괄호를 확인하세요")
                value = self.source[begin:end]
                self.pos = end + 1
                return ("<mtext>" + html.escape(value) + "</mtext>", value)
            self.fail(f"모르는 명령 \\{name}입니다", start)
        if c == "{":
            self.pos -= 1
            return self.group()
        if c == "(":
            content = self.sequence(")")
            if self.pos >= len(self.source) or self.source[self.pos] != ")":
                self.fail("닫는 )가 없습니다")
            self.pos += 1
            inside = wrap_row(content)
            return ('<mrow><mo stretchy="false">(</mo>' + inside[0] + '<mo stretchy="false">)</mo></mrow>',
                    "(" + inside[1] + ")")
        if c == "|":
            content = self.sequence("|")
            if self.pos >= len(self.source) or self.source[self.pos] != "|":
                self.fail("닫는 |가 없습니다")
            self.pos += 1
            inside = wrap_row(content)
            return ("<mrow><mo>|</mo>" + inside[0] + "<mo>|</mo></mrow>", "|" + inside[1] + "|")
        if c.isascii() and c.isalpha():
            attr = ' mathvariant="normal"' if c.isupper() else ""
            return (f"<mi{attr}>{c}</mi>", c)
        if c.isascii() and (c.isdigit() or c == "."):
            match = re.match(r"(?:\d+(?:\.\d*)?|\.\d+)", self.source[start:])
            if not match:
                return ("<mo>.</mo>", ".")
            value = match.group()
            self.pos = start + len(value)
            return (f"<mn>{value}</mn>", value)
        if c in "+-=<>,!":
            value = "−" if c == "-" else c
            return ("<mo>" + html.escape(value) + "</mo>", value)
        self.fail(f"지원하지 않는 글자 {c!r}입니다", start)

    def sequence(self, closing=None):
        result = []
        while True:
            self.spaces()
            if self.pos >= len(self.source) or (closing and self.source[self.pos] == closing):
                return result
            if self.source[self.pos] in "})]":
                self.fail("짝이 맞지 않는 닫는 괄호입니다")
            if self.source[self.pos] in "^_":
                self.fail("첨자 앞에 값이 필요합니다")
            base = self.atom()
            if base[1] in ("+", "−") and base[0].startswith("<mo>"):
                previous = next((part for part in reversed(result) if not part[0].startswith("<mspace ")), None)
                if previous is None or (previous[0].startswith(("<mo>", "<mo ")) and previous[1] not in ("!", "°")):
                    base = self.prefix_sign(base)
            if base == ("<mo>°</mo>", "°") and result and re.fullmatch(r"<mn>[^<]+</mn>", result[-1][0]):
                previous = result[-1]
                result[-1] = (previous[0][:-5] + "°</mn>", previous[1] + "°")
                continue
            sup = sub = None
            while True:
                self.spaces()
                if self.pos >= len(self.source) or self.source[self.pos] not in "^_":
                    break
                op = self.source[self.pos]
                self.pos += 1
                if (op == "^" and sup is not None) or (op == "_" and sub is not None):
                    self.fail("같은 첨자가 중복되었습니다")
                arg = self.argument()
                if op == "^":
                    sup = arg
                else:
                    sub = arg
            if sup or sub:
                markup = (f"<msubsup>{base[0]}{sub[0]}{sup[0]}</msubsup>" if sup and sub else
                          f"<msup>{base[0]}{sup[0]}</msup>" if sup else f"<msub>{base[0]}{sub[0]}</msub>")
                plain = base[1]
                for arg, table, prefix in ((sub, SUBS, "_"), (sup, SUPERS, "^")):
                    if arg:
                        converted = arg[1].translate(table)
                        plain += converted if all(a != b for a, b in zip(arg[1], converted)) else prefix + "(" + arg[1] + ")"
                base = (markup, plain)
            result.append(base)

    def parse(self):
        parts = self.sequence()
        if not parts:
            self.fail("빈 수식입니다", 0)
        return "<math>" + "".join(p[0] for p in parts) + "</math>", "".join(p[1] for p in parts)


def math_to_mathml(source):
    return MathParser(source).parse()[0]


def markup_parts(text, live=None):
    """표시 문법을 HTML과 평문, 수식 여부가 있는 조각으로 나눈다.

    live는 그 장의 state다. state가 있는 장에서만 {식}이 조작 값이 된다(없는 장에서는 그냥 글자).
    """
    text = str(text)
    result = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "*$\\[{":
            result.append((html.escape(text[i + 1]), text[i + 1], False))
            i += 2
        elif c == "$":
            end = i + 1
            while end < len(text):
                if text[end] == "$" and (end == 0 or text[end - 1] != "\\"):
                    break
                end += 1
            if end == len(text):
                raise PlanError(f"수식 ${text[i + 1:]} · {i + 1}번째 글자: 닫는 $가 없습니다")
            markup, plain = MathParser(text[i + 1:end], live).parse()
            result.append((markup, plain, True))
            i = end + 1
        elif text.startswith("[[", i):
            end = text.find("]]", i + 2)
            if end < 0:
                raise PlanError(f"빈칸 [[ · {i + 1}번째 글자: 닫는 ]]가 없습니다")
            inner = markup_parts(text[i + 2:end], live)
            if not "".join(part[1] for part in inner).strip():
                raise PlanError(f"빈칸 [[]] · {i + 1}번째 글자: 안에 답이 없습니다")
            result.append(('<span class="blank" data-peek role="button" tabindex="0"><span>', "", False))
            result.extend(inner)
            result.append(("</span></span>", "", False))
            i = end + 2
        elif c == "{" and live is not None:
            end = text.find("}", i + 1)
            if end < 0:
                raise PlanError(f"조작 값 {{ · {i + 1}번째 글자: 닫는 }}가 없습니다 · 글자 그대로 쓰려면 \\{{로 쓰세요")
            markup, plain = live_output(text[i + 1:end], live, "output")
            result.append((markup, plain, False))
            i = end + 1
        elif c == "*":
            marker = "**" if text.startswith("**", i) else "*"
            end = i + len(marker)
            while end < len(text):
                if text.startswith(marker, end) and (end == 0 or text[end - 1] != "\\"):
                    break
                end += 1
            if end == len(text):
                raise PlanError(f"표시 기호 {marker} · {i + 1}번째 글자: 닫는 기호가 없습니다")
            inner = markup_parts(text[i + len(marker):end], live)
            tag = 'b class="hl"' if marker == "**" else "var"
            close = "b" if marker == "**" else "var"
            result.append(("<" + tag + ">", "", False))
            result.extend(inner)
            result.append(("</" + close + ">", "", False))
            i = end + len(marker)
        elif c == "\n":
            result.append(("<br>\n", "\n", False))
            i += 1
        else:
            result.append((html.escape(c, quote=True), c, False))
            i += 1
    return result


def display_text(text, live=None):
    return "".join(part[0] for part in markup_parts(text, live))


def plain_text(text, live=None):
    return "".join(part[1] for part in markup_parts(text, live))


def attribute_text(text, live=None):
    return " ".join(plain_text(text, live).splitlines())


# 46px 본문에서 한글 한 글자를 1로 놓고 Maplestory와 Cambria Math의 폭을 잰 값.
# 글꼴 대체와 픽셀 반올림을 고려해 조금 넉넉하게 센다.
TEXT_LATIN = dict(zip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", (
    .76, .75, .69, .75, .70, .61, .73, .71, .29, .29, .73, .30, .96, .68, .75, .72, .72, .58, .63, .62, .68, .71, 1.02, .69, .71, .62,
    .76, .76, .85, .80, .68, .68, .92, .81, .56, .62, .77, .66, 1.02, .81, .93, .79, .93, .77, .75, .75, .82, .76, 1.08, .75, .78, .73,
    .81, .52, .73, .72, .80, .68, .72, .65, .77, .73)))
MATH_LATIN = dict(zip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", (
    .72, .70, .60, .75, .65, .72, .79, .72, .42, .47, .72, .42, 1.08, .75, .69, .73, .70, .62, .60, .52, .75, .71, .96, .69, .73, .63,
    .81, .80, .73, .86, .75, .70, .80, .89, .43, .41, .82, .70, 1.06, .88, .85, .74, .85, .81, .65, .77, .84, .79, 1.19, .75, .74, .70)))
TEXT_SYMBOLS = {**dict.fromkeys(",.:;", .32), "!": .36, "?": .70,
                **dict.fromkeys("+-=", .65), **dict.fromkeys("<>", .52), "/": .38,
                **dict.fromkeys("()", .44), **dict.fromkeys("[]", .45)}
MATH_OPERATORS = {**dict.fromkeys("=<>≤≥≠≈≡∽⊥⫽", 1.68),
                  **dict.fromkeys("+−÷±", 1.54), "×": 1.36, "⋅": .80,
                  "(": .54, ")": .54, "|": .45, ",": .49, "!": .38,
                  "°": .37, "∠": .85, "∴": 1.1, "∵": 1.1, "⋯": 1.0}


def text_char_width(c):
    if c.isspace():
        return .5  # 실제 공백은 약 .3이지만 줄바꿈 여유를 둔다
    return TEXT_LATIN.get(c, TEXT_SYMBOLS.get(c, 1.0))


def math_width(markup):
    """MathML의 가로 폭. 첨자·분수는 겹쳐 놓인 두 부분 중 넓은 쪽을 센다."""
    def characters(value, kind):
        result = 0.0
        for c in value:
            if kind == "mtext":
                result += text_char_width(c)
            elif c.isascii() and c.isdigit():
                result += .72
            elif c in MATH_LATIN:
                result += MATH_LATIN[c]
            elif c == ".":
                result += .36
            elif c == "−":       # 조작 값의 음수는 mn 안에 있다
                result += .96
            elif c == "π":
                result += .75
            else:
                result += MATH_OPERATORS.get(c, 1.08)
        return result

    def measure(node):
        tag = node.tag
        children = list(node)
        if tag in ("mi", "mn", "mtext"):
            return characters(node.text or "", tag)
        if tag == "mo":
            if node.get("form") == "prefix" and node.text in ("+", "−"):
                return .96
            return MATH_OPERATORS.get(node.text or "", 1.1)
        if tag == "mspace":
            return 1.27
        if tag in ("msup", "msub", "msubsup"):
            return measure(children[0]) + .76 * max(map(measure, children[1:])) + .05
        if tag == "mfrac":
            return .76 * max(map(measure, children)) + .05
        if tag == "msqrt":
            return sum(map(measure, children)) + 1.0
        if tag == "mroot":
            return measure(children[0]) + 1.0 + max(0.0, .6 * measure(children[1]) - .33)
        if tag == "mover":
            return max(measure(children[0]), 1.1)
        return sum(map(measure, children))

    return measure(ET.fromstring(markup))


def text_widths(text, live=None):
    widths = [0.0]
    for markup, plain, is_math in markup_parts(text, live):
        if is_math:
            widths[-1] += math_width(markup)
            continue
        for c in plain:
            if c == "\n":
                widths.append(0.0)
            else:
                widths[-1] += text_char_width(c)
    return widths


def slide_state(slide):
    """조작 탐구 장의 처음 상태. 없으면 None({식}을 글자로 둔다)."""
    return slide.get("state") if isinstance(slide, dict) and isinstance(slide.get("state"), dict) else None


def tone_numbers(slides):
    """단계 표시(tag)마다 색 번호 1~5를 처음 나온 순서로 매긴다. 표지는 늘 1번."""
    tones = {}
    for slide in slides:
        tag = slide.get("tag") if isinstance(slide, dict) and slide.get("layout") != "cover" else None
        if isinstance(tag, str) and tag and tag not in tones:
            tones[tag] = len(tones) % 5 + 1
    return tones


def derived_slide(plan, slide):
    """틀에서 쓰는 계산 값을 붙인 장의 사본.

    _no 장 번호 · _tone 단계 표시의 색 번호 · _len.<필드> 가장 긴 줄의 글자 수(한글 1, 영문·숫자 0.5)
    · _mmss.<필드> 초를 분:초로.
    """
    slides = [s for s in plan.get("slides", []) if isinstance(s, dict)]
    index = next((i for i, s in enumerate(slides) if s is slide or s.get("id") == slide.get("id")), 0)
    tones = tone_numbers(slides)
    tag = slide.get("tag")
    if slide.get("layout") == "cover":
        tone = 1
    elif isinstance(tag, str) and tag in tones:
        tone = tones[tag]
    else:
        # 표시가 없는 장(구분장 등)은 뒤따르는 단계의 색을, 없으면 앞 단계의 색을 쓴다.
        following = next((s.get("tag") for s in slides[index + 1:] if s.get("tag") in tones), None)
        previous = next((s.get("tag") for s in reversed(slides[:index]) if s.get("tag") in tones), None)
        tone = tones.get(following) or tones.get(previous) or 1
    live = slide_state(slide)

    def longest(value, field=None):
        """글자는 가장 긴 줄의 길이, 목록은 항목 가운데 가장 긴 것, 객체는 필드별. 잴 수 없으면 None."""
        if isinstance(value, str):
            try:
                # 수식 안의 a<b>c는 HTML 태그가 아니다. raw SVG 등은 수식 밖에서만 가려낸다.
                outside_math = "".join("\0" if is_math else plain for _, plain, is_math in markup_parts(value, live))
                if field != "title" and re.search(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*?)?\s*/?>|<![^<>]*>", outside_math):
                    return None
                width = math.ceil(max(text_widths(value, live)) * 2 - 1e-9) / 2
            except PlanError:
                return None
            return int(width) if width == int(width) else width
        if isinstance(value, dict):
            found = {key: longest(item, key) for key, item in value.items()}
            return {key: item for key, item in found.items() if item is not None} or None
        if isinstance(value, list):
            found = [size for item in value if (size := longest(item, field)) is not None]
            if found and all(isinstance(item, dict) for item in found):
                merged = {}
                for item in found:
                    for key, size in item.items():
                        if isinstance(size, (int, float)) and isinstance(merged.get(key, 0), (int, float)):
                            merged[key] = max(merged.get(key, 0), size)
                        elif key not in merged:
                            merged[key] = size
                return merged
            numbers = [item for item in found if isinstance(item, (int, float))]
            return max(numbers) if numbers else None
        return None

    result = dict(slide)
    lengths = longest({key: value for key, value in slide.items() if not key.startswith("_")}) or {}
    clocks = {key: f"{int(value) // 60}:{int(value) % 60:02d}" for key, value in slide.items()
              if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0}
    result.update({"_no": index + 1, "_tone": tone, "_len": lengths, "_mmss": clocks})
    return result


class Template:
    def __init__(self, source, filename="<틀>"):
        self.source = source
        self.filename = str(filename)
        self.tokens = []
        self.pos = 0
        self.tree = self.parse_section(None)

    def fail(self, pos, text):
        line = self.source.count("\n", 0, pos) + 1
        raise PlanError(f"틀 {self.filename}:{line} · {text} · 틀 문법을 확인하세요")

    def parse_section(self, closing):
        nodes = []
        while self.pos < len(self.source):
            start = self.source.find("{{", self.pos)
            if start < 0:
                nodes.append(("text", self.source[self.pos:]))
                self.pos = len(self.source)
                break
            if start > self.pos:
                nodes.append(("text", self.source[self.pos:start]))
            triple = self.source.startswith("{{{", start)
            end_mark = "}}}" if triple else "}}"
            end = self.source.find(end_mark, start + (3 if triple else 2))
            if end < 0:
                self.fail(start, "닫히지 않은 태그가 있습니다")
            token = self.source[start + (3 if triple else 2):end].strip()
            self.pos = end + len(end_mark)
            if not token:
                self.fail(start, "빈 태그가 있습니다")
            if not triple and token.startswith("!"):
                continue
            if not triple and token.startswith("/"):
                name = token[1:].strip()
                if closing != name:
                    self.fail(start, f"닫는 구역 {name}이 맞지 않습니다")
                return nodes
            if not triple and token[0] in "#^":
                invert = token[0] == "^"
                expression = token[1:].strip()
                pieces = expression.split("|")
                name = pieces[0].strip()
                if len(pieces) > 2 or (len(pieces) == 2 and (pieces[1].strip() != "shuffle" or invert)):
                    self.fail(start, f"모르는 구역 도우미 {expression}입니다")
                self.valid_name(name, start)
                children = self.parse_section(name)
                nodes.append(("section", name, invert, len(pieces) == 2, children, start))
                continue
            if not triple and token.startswith("asset:"):
                bits = token[6:].split("|")
                if len(bits) > 2:
                    self.fail(start, "asset 도우미의 클래스가 올바르지 않습니다")
                self.valid_name(bits[0].strip(), start)
                nodes.append(("asset", bits[0].strip(), bits[1].strip() if len(bits) == 2 else "", start))
            elif not triple and token in ("step", "step:same"):
                nodes.append(("step", token == "step:same", start))
            else:
                self.valid_name(token, start)
                nodes.append(("raw" if triple else "value", token, start))
        if closing is not None:
            self.fail(len(self.source), f"구역 {closing}이 닫히지 않았습니다")
        return nodes

    def valid_name(self, name, pos):
        if not re.fullmatch(r"(?:\.|@[A-Za-z]+|[\w-]+(?:\.[\w-]+)*)", name):
            self.fail(pos, f"모르는 도우미 또는 잘못된 이름 {name}입니다")
        if name.startswith("@") and name not in ("@i", "@n", "@count", "@first", "@last", "@rank"):
            self.fail(pos, f"모르는 반복 변수 {name}입니다")

    def in_attribute(self, pos):
        before = self.source[:pos]
        tag = before[before.rfind("<"):] if before.rfind("<") > before.rfind(">") else ""
        return bool(tag and (tag.count('"') % 2 or tag.count("'") % 2))

    def render(self, plan, slide, *, omit_raw_markup=False):
        # 콘티 최상위는 meta와 deck 두 이름으로만 본다. 장에 없는 필드(mascot 등)가 최상위의 같은 이름으로 새지 않게 한다.
        contexts = [{"meta": plan.get("meta", {}), "deck": plan}, slide]
        assets = {a.get("id"): a for a in plan.get("assets", []) if isinstance(a, dict)}
        state = {"step": 0}
        live = slide_state(slide)

        def lookup(name):
            if name == ".":
                return contexts[-1]
            keys = name.split(".")
            for context in reversed(contexts):
                if isinstance(context, dict) and keys[0] in context:
                    value = context[keys[0]]
                    for key in keys[1:]:
                        value = value.get(key) if isinstance(value, dict) else None
                    return value
            return None

        def render_nodes(nodes):
            output = []
            for node in nodes:
                kind = node[0]
                if kind == "text":
                    output.append(node[1])
                elif kind in ("value", "raw"):
                    value = lookup(node[1])
                    if value is not None:
                        if isinstance(value, bool):
                            value = "true" if value else "false"
                        elif isinstance(value, (dict, list)):
                            # 객체·목록은 속성에 넣을 JSON으로 적는다(작은따옴표 속성 안에서 안전하게).
                            value = (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                                     .replace("&", "&amp;").replace("'", "&#39;").replace("<", "&lt;"))
                            kind = "raw"
                        value = str(value)
                        in_attribute = self.in_attribute(node[2])
                        # 날것 그림·마크업은 틀이 아니라 콘티 작성자가 쓴 것이다.
                        if omit_raw_markup and kind == "raw" and not in_attribute and "<" in value:
                            continue
                        output.append(value if kind == "raw" else
                                      html.escape(attribute_text(value, live), quote=True) if in_attribute
                                      else display_text(value, live))
                elif kind == "asset":
                    value = lookup(node[1])
                    if value and isinstance(value, str):
                        asset = assets.get(value, {})
                        alt = attribute_text(asset.get("alt") or value)
                        cls = "asset" + (" " + node[2] if node[2] else "")
                        output.append('<img class="' + html.escape(cls, quote=True) + '" data-asset="' + html.escape(str(value), quote=True)
                                      + '" src="그림/' + html.escape(str(value), quote=True) + '.png" alt="' + html.escape(alt, quote=True) + '">')
                elif kind == "step":
                    if slide.get("reveal", "step") != "all":
                        if not node[1]:
                            state["step"] += 1
                        elif not state["step"]:
                            self.fail(node[2], "step:same 앞에 step이 없습니다")
                        output.append(f' data-step="{state["step"]}"')
                else:
                    _, name, inverted, shuffle, children, pos = node
                    value = lookup(name)
                    if inverted:
                        if not value:
                            output.append(render_nodes(children))
                    elif isinstance(value, list):
                        indexed = list(enumerate(value))
                        if shuffle and len(indexed) > 1:
                            seed = hashlib.sha256(str(slide.get("id", "")).encode("utf-8")).digest()
                            random.Random(seed).shuffle(indexed)
                            if [i for i, _ in indexed] == list(range(len(indexed))):
                                indexed = indexed[1:] + indexed[:1]
                        for index, (original, item) in enumerate(indexed):
                            repeat = {"@i": index, "@n": index + 1, "@count": len(indexed),
                                      "@first": index == 0, "@last": index == len(indexed) - 1, "@rank": original + 1}
                            contexts.extend([repeat, item])
                            output.append(render_nodes(children))
                            del contexts[-2:]
                    elif shuffle and value is not None:
                        self.fail(pos, "shuffle은 목록에만 사용할 수 있습니다")
                    elif value:
                        if isinstance(value, dict):
                            contexts.append(value)
                            output.append(render_nodes(children))
                            contexts.pop()
                        else:
                            output.append(render_nodes(children))
            return "".join(output)
        return render_nodes(self.tree)


def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .") or "제목 없음"
    if re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    return name[:180]


LAYOUT_GROUPS = {"open": "열기", "explain": "설명", "explore": "탐구", "check": "확인", "wrap": "정리"}
BUDDY_LABELS = {"say": "말풍선까지", "pose": "그림만"}


def layout_fields(fields, prefix=""):
    """목록과 객체를 경로로 펼쳐 등록부의 한도를 보존한다."""
    rows = []
    for name, spec in fields.items():
        if not isinstance(spec, dict):
            raise PlanError(f"구도 등록부의 필드 정의가 올바르지 않습니다: {prefix}{name}")
        path = prefix + name
        kind = spec.get("type", "text")
        row = {"path": path, "type": kind, "required": bool(spec.get("required"))}
        for key in ("min", "max", "options"):
            if key in spec:
                row[key] = spec[key]
        rows.append(row)
        child = spec.get("of")
        if kind == "object" and isinstance(child, dict):
            rows.extend(layout_fields(child, path + "."))
        elif kind == "list":
            if child == "text":
                rows.append({"path": path + "[]", "type": "text", "required": True})
            elif isinstance(child, dict) and isinstance(child.get("type"), str):
                rows.extend(layout_fields({path + "[]": child}))
            elif isinstance(child, dict):
                rows.extend(layout_fields(child, path + "[]."))
    return rows


def layout_example_value(spec):
    kind = spec.get("type", "text")
    if kind == "text":
        limit = spec.get("max")
        return "예" if isinstance(limit, list) and limit and limit[0] < 2 else "예시"
    if kind == "asset":
        return "에셋_id"
    if kind == "bool":
        return False
    if kind == "number":
        return spec.get("min", min(1, spec.get("max", 1)))
    if kind == "choice":
        return next(iter(spec.get("options", [])), "")
    if kind == "raw":
        return "…"
    if kind == "json":
        return {}
    child = spec.get("of", {})
    if kind == "object":
        return {name: layout_example_value(field) for name, field in child.items()}
    if kind == "list":
        child = {"type": "text"} if child == "text" else child
        if isinstance(child, dict) and not isinstance(child.get("type"), str):
            value = {name: layout_example_value(field) for name, field in child.items()}
        else:
            value = layout_example_value(child)
        count = max(1, spec.get("min", 0))
        if isinstance(spec.get("max"), int):
            count = min(count, spec["max"])
        return [value for _ in range(count)]
    return "…"


def layout_limit(row):
    kind = row["type"]
    if kind == "text" and isinstance(row.get("max"), list) and len(row["max"]) == 2:
        return f'{row["max"][0]}자 × {row["max"][1]}줄'
    if kind in ("list", "number") and ("min" in row or "max" in row):
        return f'{row.get("min", "-")}~{row.get("max", "-")}' + ("개" if kind == "list" else "")
    if kind == "choice":
        return ", ".join(json.dumps(option, ensure_ascii=False) for option in row.get("options", []))
    return "-"


def layouts_command(args):
    layouts = load_layouts(args)
    if args.keys is None:
        keys = sorted(layouts, key=lambda key: list(LAYOUT_GROUPS).index(layouts[key].get("group"))
                      if layouts[key].get("group") in LAYOUT_GROUPS else len(LAYOUT_GROUPS))
    else:
        keys = [key.strip() for key in args.keys.split(",") if key.strip()]
        if not keys:
            raise PlanError("구도 키를 입력하세요")
        unknown = [key for key in keys if key not in layouts]
        if unknown:
            similar = difflib.get_close_matches(unknown[0], layouts.keys(), n=3, cutoff=0.35)
            suggestion = " · 비슷한 구도: " + ", ".join(similar) if similar else ""
            raise PlanError(f"모르는 구도 {unknown[0]}입니다{suggestion}")
    entries = []
    for key in keys:
        entry = layouts[key]
        row = {"key": key, "name": entry.get("name", key), "use": entry.get("use", ""),
               "group": entry.get("group"), "interaction": entry.get("interaction"),
               "buddy": entry.get("buddy"), "visual": bool(entry.get("visual"))}
        if args.keys is not None:
            fields = entry.get("fields", {})
            row["fields"] = layout_fields(fields)
            example = {"id": "slide-id", "layout": key, "purpose": "이 장에서 할 일을 적어요", "focus": "먼저 볼 것"}
            example.update({name: layout_example_value(spec) for name, spec in fields.items()})
            if row["visual"] and row["buddy"] in BUDDY_LABELS:
                example["mascot"] = "에셋_id"
            row["example"] = example
        entries.append(row)
    if args.json:
        print(json.dumps({"layouts": entries}, ensure_ascii=False, indent=2))
    elif args.keys is None:
        print("이름(키) | 쓰임 | 상호작용 | 마스코트 자리 | 그림 필수")
        current = None
        for row in entries:
            if row["group"] != current:
                current = row["group"]
                print("\n" + LAYOUT_GROUPS.get(current, str(current or "기타")))
            print(f'{row["name"]}({row["key"]}) | {row["use"] or "-"} | {row["interaction"] or "-"} | '
                  f'{BUDDY_LABELS.get(row["buddy"], "-")} | {"예" if row["visual"] else "아니요"}')
    else:
        for row in entries:
            print(f'{row["name"]}({row["key"]}) · {row["use"] or "-"}')
            print("필드 | 형식 | 필수 | 한도")
            for field in row["fields"]:
                print(f'{field["path"]} | {field["type"]} | {"예" if field["required"] else "-"} | {layout_limit(field)}')
            print("콘티 조각 (에셋 id와 예시 내용을 실제 값으로 바꾸세요)")
            print(json.dumps(row["example"], ensure_ascii=False, indent=2))
    return 0


def new_command(args):
    folder = Path(args.folder)
    plan_file = folder / "콘티.json"
    templates = SKILL / "assets" / "impeccable"
    destinations = [plan_file] + [folder / name for name in ("PRODUCT.md", "DESIGN.md") if (templates / name).is_file()]
    conflicts = [p for p in (plan_file, folder / "PRODUCT.md", folder / "DESIGN.md") if p.exists()]
    if conflicts:
        raise PlanError(f"이미 파일이 있습니다: {conflicts[0]} · 다른 폴더를 선택하세요")
    try:
        layouts, palettes = load_layouts(args), load_palettes(args)
    except PlanError:
        layouts, palettes = {}, []
    palette = args.palette or (palettes[0] if palettes else "sunny")
    # 마지막 장의 예시는 부품의 정리 구도 가운데 첫 번째를 쓴다.
    closing = next((name for name, entry in layouts.items() if entry.get("group") == "wrap"), "wrap")
    last = {"id": "example-wrap", "layout": closing, "purpose": "예시 정리를 고치세요", "focus": "예시 결론을 고치세요", "title": "정리"}
    if isinstance(layouts.get(closing, {}).get("fields", {}).get("items"), dict):
        last["items"] = ["예시 정리 하나", "예시 정리 둘"]
    plan = {"meta": {"title": args.title, "kind": args.kind, "audience": "예시 청중", "duration": "45분",
                     "goal": "예시 목표를 고치세요", "message": "예시 핵심 메시지를 고치세요", "palette": palette},
            "mascot": {"id": "mascot", "name": "예시", "look": "Example mascot"},
            "style": {},
            "slides": [{"id": "example-cover", "layout": "cover", "purpose": "예시 목적을 고치세요", "focus": "예시 초점을 고치세요", "title": args.title},
                       last],
            "assets": [{"id": "example-wave", "kind": "mascot", "prompt": "Example wave", "alt": "예시 포즈"},
                       {"id": "example-sticker", "kind": "sticker", "prompt": "Example sticker", "alt": "예시 그림"}]}
    folder.mkdir(parents=True, exist_ok=True)
    write_text(plan_file, json.dumps(plan, ensure_ascii=False, indent=2) + "\n", exclusive=True)
    for dest in destinations[1:]:
        template = read_text(templates / dest.name)
        write_text(dest, template.replace("{{title}}", args.title).replace("{{kind}}", args.kind)
                   .replace("{{palette}}", palette), exclusive=True)
    print(f"콘티 틀을 만들었습니다: {plan_file} · 예시를 실제 내용으로 바꾸세요")
    return 0


def validate_plan(plan, layouts, palettes=None):
    problems, rows = [], []
    def issue(level, code, index, ident, field, message, fix):
        problems.append({"level": level, "code": code, "slide": index, "id": ident,
                         "field": field, "message": message, "fix": fix})
    def error(code, i, ident, field, message, fix):
        issue("error", code, i, ident, field, message, fix)
    def warn(code, i, ident, field, message, fix):
        issue("warn", code, i, ident, field, message, fix)
    if not isinstance(plan, dict):
        error("type", 0, "", "콘티", "최상위 값이 객체가 아닙니다", "객체로 작성하세요")
        return rows, problems
    for key in set(plan) - {"meta", "mascot", "style", "slides", "assets"}:
        error("unknown-field", 0, "", key, "등록되지 않은 필드입니다", "필드 이름을 확인하세요")
    meta = plan.get("meta")
    if not isinstance(meta, dict):
        error("required", 0, "", "meta", "메타 정보가 없습니다", "meta 객체를 작성하세요")
        meta = {}
    for key in META_FIELDS:
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            error("required", 0, "", "meta." + key, "필수 글자가 없습니다", "값을 작성하세요")
    if isinstance(meta.get("title"), str):
        try:
            plain_text(meta["title"])
        except PlanError as exc:
            error("math", 0, "", "meta.title", str(exc), "표시 기호와 수식을 고치세요")
    if meta.get("kind") and meta.get("kind") not in ("lesson", "training", "talk"):
        error("choice", 0, "", "meta.kind", "지원하지 않는 종류입니다", "lesson, training, talk 중 하나를 쓰세요")
    for key in set(meta) - META_FIELDS:
        error("unknown-field", 0, "", "meta." + key, "등록되지 않은 필드입니다", "필드 이름을 확인하세요")
    mascot = plan.get("mascot")
    if not isinstance(mascot, dict):
        error("required", 0, "", "mascot", "마스코트 정보가 없습니다", "mascot 객체를 작성하세요")
        mascot = {}
    for key in ("id", "name", "look"):
        if not isinstance(mascot.get(key), str) or not mascot[key].strip():
            error("required", 0, "", "mascot." + key, "필수 글자가 없습니다", "값을 작성하세요")
    if mascot.get("id") and (not isinstance(mascot["id"], str) or not ASSET_ID_RE.fullmatch(mascot["id"])):
        error("id-format", 0, "", "mascot.id", "id 형식이 올바르지 않습니다", "소문자·숫자·하이픈·밑줄을 쓰세요")
    for key in set(mascot) - {"id", "name", "look", "yes", "no"}:
        error("unknown-field", 0, "", "mascot." + key, "등록되지 않은 필드입니다", "필드 이름을 확인하세요")
    # 확인 구도에서 맞았을 때·틀렸을 때 바꿔 보일 포즈(선택). 둘을 함께 쓴다.
    reactions = [key for key in ("yes", "no") if key in mascot]
    if len(reactions) == 1:
        error("required", 0, "", "mascot." + ("no" if reactions[0] == "yes" else "yes"),
              "반응 포즈는 yes와 no를 함께 씁니다", "두 포즈의 에셋 id를 모두 쓰거나 둘 다 빼세요")
    if palettes and isinstance(meta.get("palette"), str) and meta["palette"].strip() and meta["palette"] not in palettes:
        error("choice", 0, "", "meta.palette", f"색 묶음 {meta['palette']}이 없습니다", "다음 중에서 고르세요: " + ", ".join(palettes))
    style = plan.get("style", {})
    if not isinstance(style, dict):
        error("type", 0, "", "style", "객체가 아닙니다", "객체로 작성하세요")
    else:
        for key in set(style) - {"asset_style"}:
            error("unknown-field", 0, "", "style." + key, "등록되지 않은 필드입니다", "필드 이름을 확인하세요")
        if "asset_style" in style and not isinstance(style["asset_style"], str):
            error("type", 0, "", "style.asset_style", "글자가 아닙니다", "문자열로 작성하세요")
    assets = plan.get("assets")
    if not isinstance(assets, list):
        error("required", 0, "", "assets", "에셋 목록이 없습니다", "assets 배열을 작성하세요")
        assets = []
    asset_map = {}
    for number, asset in enumerate(assets, 1):
        where = f"assets[{number}]"
        if not isinstance(asset, dict):
            error("type", 0, "", where, "객체가 아닙니다", "에셋 객체로 작성하세요")
            continue
        ident = asset.get("id")
        for key in ("id", "kind", "prompt"):
            if not isinstance(asset.get(key), str) or not asset[key].strip():
                error("required", 0, "", where + "." + key, "필수 글자가 없습니다", "값을 작성하세요")
        for key in set(asset) - {"id", "kind", "prompt", "ref", "text", "alt"}:
            error("unknown-field", 0, "", where + "." + key, "등록되지 않은 필드입니다", "필드 이름을 확인하세요")
        if isinstance(ident, str) and ident:
            if not ASSET_ID_RE.fullmatch(ident):
                error("id-format", 0, "", where + ".id", "id 형식이 올바르지 않습니다", "소문자·숫자·하이픈·밑줄을 쓰세요")
            if ident in asset_map:
                error("duplicate-id", 0, "", where + ".id", "에셋 id가 중복되었습니다", "서로 다른 id를 쓰세요")
            asset_map[ident] = asset
            if ident.startswith("example-"):
                error("example", 0, "", where + ".id", "예시가 남아 있습니다", "예시를 지우거나 실제 에셋으로 바꾸세요")
        if isinstance(asset.get("kind"), str) and asset["kind"] and asset["kind"] not in ASSET_KINDS:
            error("choice", 0, "", where + ".kind", "모르는 에셋 종류입니다", "정해진 kind를 쓰세요")
        if "ref" in asset and (not isinstance(asset["ref"], list) or not all(isinstance(x, str) for x in asset["ref"])):
            error("type", 0, "", where + ".ref", "참고 id 배열이 아닙니다", "문자열 배열로 쓰세요")
        for key in ("alt", "text"):
            if key in asset and not isinstance(asset[key], str):
                error("type", 0, "", where + "." + key, "글자가 아닙니다", "문자열로 작성하세요")
        if isinstance(asset.get("alt"), str):
            try:
                plain_text(asset["alt"])
            except PlanError as exc:
                error("math", 0, "", where + ".alt", str(exc), "표시 기호와 수식을 고치세요")
    used = {mascot["id"]} if isinstance(mascot.get("id"), str) else set()
    for key in reactions:
        if not isinstance(mascot[key], str) or mascot[key] not in asset_map:
            error("missing-asset", 0, "", "mascot." + key, f"에셋 {mascot[key]}가 없습니다", "assets에 포즈를 추가하세요")
        else:
            used.add(mascot[key])
    for number, asset in enumerate(assets, 1):
        if not isinstance(asset, dict) or not isinstance(asset.get("ref", []), list):
            continue
        for ref in asset.get("ref", []):
            if not isinstance(ref, str):
                continue
            if ref not in asset_map and ref != mascot.get("id"):
                error("missing-asset", 0, "", f"assets[{number}].ref", f"에셋 {ref}가 없습니다", "assets에 추가하세요")
    slides = plan.get("slides")
    if not isinstance(slides, list) or not slides:
        error("required", 0, "", "slides", "장 목록이 없거나 비었습니다", "slides 배열에 장을 넣으세요")
        slides = []
    seen, slide_data = set(), []
    current = {"live": None}   # 검사 중인 장의 state({식}의 글자 수를 처음 값으로 센다)

    def validate_field(value, spec, path, index, ident, refs, optional=False):
        if not isinstance(spec, dict):
            error("registry", index, ident, path, "등록부 필드 정의가 올바르지 않습니다", "구도 등록부를 확인하세요")
            return 0.0
        if value is None or value == "" or (spec.get("required") and value == []):
            if spec.get("required"):
                error("required", index, ident, path, "필수 값이 없습니다", "값을 작성하세요")
            return 0.0
        kind = spec.get("type", "text")
        valid = ((kind in ("text", "asset", "raw", "choice") and isinstance(value, str)) or
                 (kind == "list" and isinstance(value, list)) or (kind == "object" and isinstance(value, dict)) or
                 (kind == "json" and isinstance(value, (dict, list))) or
                 (kind == "bool" and isinstance(value, bool)) or
                 (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)))
        if not valid:
            error("type", index, ident, path, f"{kind} 형식이 아닙니다", "등록부의 필드 형식에 맞추세요")
            return 0.0
        if kind == "json":
            return 0.0
        if kind == "raw" and spec.get("visual") and value.strip():
            refs.add("#" + path)      # 직접 그린 그림(SVG)도 그 장의 그림으로 센다
        if kind == "number":
            low, high = spec.get("min"), spec.get("max")
            if (isinstance(low, (int, float)) and value < low) or (isinstance(high, (int, float)) and value > high):
                error("number-limit", index, ident, path, f"값 {value:g}이 범위 밖입니다", "등록부의 min·max에 맞추세요")
            return 0.0
        if kind == "choice" and value not in spec.get("options", []):
            error("choice", index, ident, path, "선택할 수 없는 값입니다", "등록부의 options에서 고르세요")
        if kind == "asset":
            refs.add(value)
            used.add(value)
            if value not in asset_map and value != mascot.get("id"):
                error("missing-asset", index, ident, path, f"에셋 {value}가 없습니다", "assets에 추가하세요")
        if kind == "text":
            try:
                widths = text_widths(value, current["live"])
            except PlanError as exc:
                error("math", index, ident, path, str(exc), "표시 기호와 수식을 고치세요")
                return 0.0
            maximum = spec.get("max")
            if isinstance(maximum, list) and len(maximum) == 2:
                # 한 줄 칸은 그 줄에 다 들어가야 하고, 여러 줄 칸은 화면에서 접히는 줄까지 세어 줄 수를 본다.
                rows = sum(max(1, math.ceil(width / maximum[0] - 1e-9)) for width in widths)
                # math는 CSS에서 줄을 나누지 않는다. 여러 줄 칸이어도 수식 하나가 한 줄보다 넓으면 넘친다.
                wide_math = any(is_math and math_width(markup) > maximum[0] + 1e-9
                                for markup, _, is_math in markup_parts(value, current["live"]))
                if rows > maximum[1] or wide_math:
                    detail = "수식 하나가 한 줄보다 깁니다" if wide_math else f"지금 약 {rows}줄"
                    error("text-limit", index, ident, path, f"글자 한도 {maximum[0]}자 × {maximum[1]}줄을 넘었습니다({detail})", "글이나 줄 수를 줄이세요")
            return sum(widths)
        total = 0.0
        if kind == "list":
            if len(value) < spec.get("min", 0) or len(value) > spec.get("max", float("inf")):
                error("list-limit", index, ident, path, f"목록 개수 {len(value)}개가 범위 밖입니다", "등록부의 min·max에 맞추세요")
            of = spec.get("of", "text")
            for j, item in enumerate(value, 1):
                childpath = f"{path}[{j}]"
                if of == "text":
                    total += validate_field(item, {"type": "text", "required": True}, childpath, index, ident, refs)
                elif isinstance(of, dict) and isinstance(of.get("type"), str):
                    # 항목 하나의 형식(예: 한도가 있는 글자) — 필드 묶음이 아니다
                    total += validate_field(item, of, childpath, index, ident, refs)
                elif isinstance(of, dict):
                    total += validate_object(item, of, childpath, index, ident, refs)
                else:
                    error("registry", index, ident, path, "목록 항목 정의가 올바르지 않습니다", "구도 등록부를 확인하세요")
        elif kind == "object":
            total += validate_object(value, spec.get("of", {}), path, index, ident, refs)
        return total

    def validate_object(value, fields, path, index, ident, refs):
        if not isinstance(fields, dict):
            error("registry", index, ident, path, "객체 필드 정의가 올바르지 않습니다", "구도 등록부를 확인하세요")
            return 0.0
        if not isinstance(value, dict):
            error("type", index, ident, path, "객체가 아닙니다", "필드 객체로 작성하세요")
            return 0.0
        total = 0.0
        for key in set(value) - set(fields):
            error("unknown-field", index, ident, path + "." + key, "등록되지 않은 필드입니다", "등록부의 필드를 쓰세요")
        for key, spec in fields.items():
            total += validate_field(value.get(key), spec, path + "." + key, index, ident, refs)
        return total

    for i, slide in enumerate(slides, 1):
        if not isinstance(slide, dict):
            error("type", i, "", "장", "객체가 아닙니다", "장 객체로 작성하세요")
            slide_data.append(({}, {}, set()))
            continue
        ident = slide.get("id")
        for key in ("id", "layout", "purpose", "focus"):
            if not isinstance(slide.get(key), str) or not slide[key].strip():
                error("required", i, str(ident or ""), key, "필수 글자가 없습니다", "값을 작성하세요")
        if isinstance(ident, str) and ident:
            if not ID_RE.fullmatch(ident):
                error("id-format", i, ident, "id", "id 형식이 올바르지 않습니다", "소문자·숫자·하이픈을 쓰세요")
            if ident in seen:
                error("duplicate-id", i, ident, "id", "장 id가 중복되었습니다", "서로 다른 id를 쓰세요")
            seen.add(ident)
            if ident.startswith("example-"):
                error("example", i, ident, "id", "예시 장이 남아 있습니다", "예시를 지우거나 실제 장으로 바꾸세요")
        name = slide.get("layout")
        layout = layouts.get(name, {}) if isinstance(name, str) else {}
        if name and not layout:
            error("unknown-layout", i, str(ident or ""), "layout", f"구도 {name}이 없습니다", "등록부에서 구도를 고르세요")
        fields = layout.get("fields", {})
        if not isinstance(fields, dict):
            error("registry", i, str(ident or ""), "layout", "구도 필드 정의가 올바르지 않습니다", "등록부를 확인하세요")
            fields = {}
        for key in set(slide) - COMMON - set(fields):
            error("unknown-field", i, str(ident or ""), key, "등록되지 않은 필드입니다", "등록부의 필드를 쓰세요")
        refs = set()
        total = 0.0
        current["live"] = slide_state(slide)
        for key, spec in fields.items():
            total += validate_field(slide.get(key), spec, key, i, ident or "", refs)
        common_specs = {"title": {"type": "text"}, "tag": {"type": "text"}, "mascot": {"type": "asset"},
                        "say": {"type": "list", "of": "text"}, "reveal": {"type": "choice", "options": ["step", "all"]},
                        "talk": {"type": "text"}, "mockup_note": {"type": "text"}, "mockup": {"type": "raw"},
                        "purpose": {"type": "text"}, "focus": {"type": "text"}}
        for key, spec in common_specs.items():
            if key not in fields and key in slide:
                width = validate_field(slide[key], spec, key, i, ident or "", refs)
                if key not in ("purpose", "focus", "talk", "mockup_note", "mockup"):
                    total += width
        if layout.get("visual") is True and not refs:
            error("visual-required", i, ident or "", "asset", "그림이 필수인 구도에 에셋이 없습니다", "에셋을 추가하세요")
        # 구도가 마스코트·말풍선 자리를 밝혀 둔 경우(buddy: "say" 말풍선까지 · "pose" 그림만 · null 없음)
        if "buddy" in layout:
            if slide.get("mascot") and not layout["buddy"]:
                error("no-mascot-slot", i, ident or "", "mascot", "이 구도에는 마스코트 자리가 없습니다", "mascot을 빼거나 마스코트가 나오는 구도로 바꾸세요")
            if slide.get("say") and layout["buddy"] != "say":
                error("no-say-slot", i, ident or "", "say", "이 구도에는 말풍선 자리가 없습니다", "say를 빼거나 말풍선이 있는 구도로 바꾸세요")
            elif slide.get("say") and not slide.get("mascot"):
                error("no-say-slot", i, ident or "", "say", "말풍선은 마스코트와 함께 씁니다", "mascot에 포즈 에셋을 쓰세요")
        say = slide.get("say", [])
        if isinstance(say, list):
            for j, line in enumerate(say, 1):
                if isinstance(line, str):
                    try:
                        if any(width > 18 for width in text_widths(line, current["live"])):
                            warn("say-long", i, ident or "", f"say[{j}]", "대사가 18자를 넘습니다", "대사를 줄이세요")
                    except PlanError:
                        pass
        purpose, focus = slide.get("purpose"), slide.get("focus")
        if isinstance(purpose, str) and isinstance(focus, str):
            try:
                purpose, focus = plain_text(purpose).strip(), plain_text(focus).strip()
                if purpose == focus or len(purpose) < 8 or len(focus) < 2:
                    warn("purpose-focus", i, ident or "", "purpose/focus", "목적이 8자 미만이거나 초점과 같습니다", "목적은 청중이 할 일 한 문장으로, 초점은 먼저 보일 것 하나로 쓰세요")
            except PlanError:
                pass  # 같은 필드의 표시 오류는 위에서 보고했다.
        rows.append({"slide": i, "id": ident or "", "layout": name or "", "title": slide.get("title", ""),
                     "images": len(refs), "interaction": layout.get("interaction"), "text": total})
        slide_data.append((slide, layout, refs))
    by_slide_id = {s.get("id"): s for s, _, _ in slide_data if isinstance(s.get("id"), str)}
    for i, (slide, _, _) in enumerate(slide_data, 1):
        reference = slide.get("mockup", "")
        if isinstance(reference, str) and reference.startswith("same-as:"):
            visited = {slide.get("id")} if isinstance(slide.get("id"), str) else set()
            while reference.startswith("same-as:"):
                target = reference[len("same-as:"):]
                if target not in by_slide_id or target in visited:
                    error("mockup-reference", i, slide.get("id", ""), "mockup", "시안 참조가 없거나 순환합니다", "다른 장의 id를 확인하세요")
                    break
                visited.add(target)
                reference = by_slide_id[target].get("mockup", "")
                if not isinstance(reference, str):
                    break
    if slides:
        first = slide_data[0]
        last = slide_data[-1]
        if first[0].get("layout") != "cover" and "표지" not in str(first[1].get("name", "")):
            error("first-not-cover", 1, first[0].get("id", ""), "layout", "첫 장이 표지가 아닙니다", "표지 구도로 바꾸세요")
        if last[1].get("group") != "wrap" and last[0].get("layout") != "wrap":
            error("last-not-wrap", len(slides), last[0].get("id", ""), "layout", "마지막 장이 정리가 아닙니다", "정리 구도로 바꾸세요")
    for i in range(2, len(slide_data)):
        three = slide_data[i-2:i+1]
        if all(x[0].get("layout") and x[0].get("layout") == three[0][0].get("layout") for x in three):
            warn("layout-repeat", i+1, three[-1][0].get("id", ""), "layout", "같은 구도가 3장 이어집니다", "구도를 바꾸세요")
    empty_run = 0
    for i, (slide, _, refs) in enumerate(slide_data, 1):
        empty_run = 0 if refs else empty_run + 1
        if empty_run >= VISUAL_LIMITS["empty_warning"]:
            warn("no-visual-repeat", i, slide.get("id", ""), "asset",
                 f"그림 없는 장이 {VISUAL_LIMITS['empty_warning']}장 이어집니다", "그림을 추가하세요")
        if len(slide_data) >= VISUAL_LIMITS["min_slides"] and empty_run == VISUAL_LIMITS["empty_run"]:
            error("no-visual-repeat", i, slide.get("id", ""), "asset",
                  f"그림 없는 장이 {empty_run}장 이어집니다", "중간 장에 그림을 추가하세요")
    interactive = [(i, data[1].get("interaction")) for i, data in enumerate(slide_data) if data[1].get("interaction")]
    if meta.get("kind") in ("lesson", "training"):
        for (previous, _), (current, _) in zip(interactive, interactive[1:]):
            if current - previous - 1 > 5:
                warn("interaction-gap", current+1, slide_data[current][0].get("id", ""), "interaction", "상호작용 사이가 5장 넘게 벌어졌습니다", "중간에 상호작용을 넣으세요")
    if len({kind for _, kind in interactive}) == 1:
        warn("interaction-variety", 0, "", "interaction", "상호작용 종류가 한 가지입니다", "다른 종류도 사용하세요")
    # 원화·포즈는 일반 그림 칸에도 들어갈 수 있다. 원화의 mascot_master 별칭은 같은 그림이다.
    master = mascot.get("id") if isinstance(mascot.get("id"), str) else None
    master_ids = {ident for ident, asset in asset_map.items() if asset.get("kind") == "mascot_master"}
    if master:
        master_ids.add(master)
    mascot_ids = master_ids | {ident for ident, asset in asset_map.items() if asset.get("kind") == "mascot"}
    other_ids = {ident for ident, asset in asset_map.items() if asset.get("kind") in ("sticker", "scene")}
    mascot_used, other_used = set(), set()
    mascot_count = image_count = 0
    for slide, _, refs in slide_data:
        mascots, others = refs & mascot_ids, refs & other_ids
        if mascots:
            mascot_count += 1
            mascot_used.update(master if ident in master_ids else ident for ident in mascots)
        if mascots or others:
            image_count += 1
        other_used.update(others)
        # 확인 구도에 마스코트가 있어야 맞음·틀림 포즈가 실제로 렌더링된다.
        if (isinstance(slide.get("layout"), str) and slide["layout"] in REACTION_LAYOUTS and
                isinstance(slide.get("mascot"), str) and slide["mascot"] in mascot_ids):
            mascot_used.update(master if mascot[key] in master_ids else mascot[key]
                               for key in reactions if isinstance(mascot[key], str) and mascot[key] in mascot_ids)
    count, poses = len(slide_data), len(mascot_used)
    limits = VISUAL_LIMITS
    if count >= limits["min_slides"]:
        if poses < limits["mascot_min"]:
            error("mascot-count", 0, "", "mascot", f"사용한 마스코트 그림이 {poses}개로 {limits['mascot_min']}개 미만입니다",
                  "서로 다른 원화·포즈를 장에 더 쓰세요")
        if 100 * mascot_count < limits["mascot_percent"] * count:
            error("mascot-coverage", 0, "", "mascot", f"마스코트가 나온 장이 {count}장 중 {mascot_count}장으로 {limits['mascot_percent']}% 미만입니다",
                  "마스코트를 넣은 장을 늘리세요")
        if len(other_used) < limits["other_min"]:
            error("non-mascot-visual", 0, "", "assets", f"사용한 스티커·장면 그림이 {len(other_used)}개입니다",
                  "sticker 또는 scene을 장에 넣으세요")
        if 100 * image_count < limits["image_percent"] * count:
            error("image-coverage", 0, "", "assets", f"이미지 모델 그림이 든 장이 {count}장 중 {image_count}장으로 {limits['image_percent']}% 미만입니다",
                  "마스코트·스티커·장면 그림을 넣은 장을 늘리세요")
    if count and (poses < limits["mascot_recommended"] or
                  (count < limits["min_slides"] and 100 * mascot_count < limits["mascot_percent"] * count)):
        warn("mascot-variety", 0, "", "mascot", f"사용한 마스코트 그림이 {poses}개입니다(권장 {limits['mascot_recommended']}개 이상)",
             "등장과 포즈를 늘리세요")
    # 장에서 쓰는 에셋에 실제로 필요한 참고만 재귀적으로 사용 처리한다.
    pending = list(used)
    while pending:
        asset = asset_map.get(pending.pop(), {})
        refs = asset.get("ref", [])
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, str) and ref not in used:
                    used.add(ref)
                    pending.append(ref)
    for ident in sorted(asset_map.keys() - used):
        warn("unused-asset", 0, "", "assets." + ident, "사용하지 않는 에셋입니다", "사용하거나 목록에서 빼세요")
    if len(assets) > 40:
        warn("asset-count", 0, "", "assets", "에셋이 40개를 넘습니다", "에셋 수를 줄이세요")
    if len(slides) < 8 or len(slides) > 24:
        warn("slide-count", 0, "", "slides", f"장이 {len(slides)}개입니다", "8~24장으로 구성하세요")
    return rows, problems


def plan_command(args):
    folder = Path(args.folder)
    plan = read_json(folder / "콘티.json")
    rows, problems = validate_plan(plan, load_layouts(args), load_palettes(args))
    errors = sum(p["level"] == "error" for p in problems)
    warnings = sum(p["level"] == "warn" for p in problems)
    out = folder / "검사"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "plan.json", {"slide_count": len(rows), "errors": errors, "warnings": warnings,
                                   "slides": rows, "problems": problems})
    print("번호 | id | 구도 | 제목 | 그림 | 상호작용 | 글자 수")
    for row in rows:
        print(f'{row["slide"]} | {row["id"]} | {row["layout"]} | {str(row["title"]).replace(chr(10), " ")} | {row["images"]} | {row["interaction"] or "-"} | {row["text"]:g}')
    for p in problems:
        place = f'{p["slide"]}번 장({p["id"]})' if p["slide"] else "콘티"
        print(f'{"오류" if p["level"] == "error" else "경고"} · {place} · {p["field"]} · {p["code"]}: {p["message"]} · {p["fix"]}')
    print(f"오류 {errors} · 경고 {warnings} · {out / 'plan.json'}")
    return 1 if errors else 0


VOID = set("area base br col embed hr img input link meta param source track wbr".split())


class SourceParser(HTMLParser):
    """원문의 위치만 기록하고 직렬화하지 않는다."""
    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0] + [m.end() for m in re.finditer("\n", text)]
        self.stack = []
        self.nodes = []
        self.feed(text)
        self.close()

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        start = self.position()
        node = {"tag": tag, "attrs": dict(attrs), "start": start,
                "start_end": start + len(self.get_starttag_text()), "end": None,
                "end_start": None, "closed": tag in VOID,
                "parent": self.stack[-1] if self.stack else None}
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node)
        else:
            node["end"] = node["start_end"]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            node = self.stack.pop()
            node["end"] = node["start_end"]
            node["closed"] = True

    def handle_endtag(self, tag):
        start = self.position()
        end = self.text.find(">", start)
        end = len(self.text) if end < 0 else end + 1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                for unclosed in self.stack[i + 1:]:
                    unclosed["end"] = start
                    unclosed["end_start"] = start
                node = self.stack[i]
                node["end_start"] = start
                node["end"] = end
                node["closed"] = True
                del self.stack[i:]
                break


def document_slides(text):
    doc = SourceParser(text)
    body = next((n for n in doc.nodes if n["tag"] == "body"), None)
    if not body or body["end_start"] is None or not body["closed"]:
        raise PlanError("원고의 body가 닫히지 않았습니다 · body 태그를 확인하세요")
    slides = [n for n in doc.nodes if n["tag"] == "section" and n["parent"] is body
              and "slide" in (n["attrs"].get("class") or "").split()]
    if any(not n["closed"] or n["end"] is None or n["end"] <= n["start_end"] for n in slides):
        raise PlanError("원고의 장이 닫히지 않았습니다 · section 태그를 확인하세요")
    return doc, body, slides


def rendered_slide(plan, slide, kit, *, omit_raw_markup=False):
    path = kit / "layouts" / (slide["layout"] + ".html")
    source = read_text(path)
    result = Template(source, path).render(plan, derived_slide(plan, slide), omit_raw_markup=omit_raw_markup)
    # 틀은 장 하나만 담아야 하고, 시작 태그만 필요한 속성을 추가한다.
    doc = SourceParser(result)
    nodes = [n for n in doc.nodes if n["tag"] == "section" and n["parent"] is None
             and "slide" in (n["attrs"].get("class") or "").split()]
    if len(nodes) != 1 or not nodes[0]["closed"] or nodes[0]["end"] is None or result[:nodes[0]["start"]].strip() or result[nodes[0]["end"]:].strip():
        raise PlanError(f"틀 {path}:1 · 장 하나가 아닙니다 · section.slide 하나만 작성하세요")
    node = nodes[0]
    insertion = ""
    for key, value in (("data-id", slide["id"]), ("data-layout", slide["layout"])):
        if key not in node["attrs"]:
            insertion += f' {key}="{html.escape(value, quote=True)}"'
    where = node["start_end"] - 1
    return result[:where] + insertion + result[where:]


def apply_edits(text, edits):
    end = len(text)
    for start, stop, value in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        if start > stop or stop > end:
            raise PlanError("원고 수정 위치가 겹칩니다 · 장 순서와 태그를 확인하세요")
        text = text[:start] + value + text[stop:]
        end = start
    return text


def scaffold_command(args):
    folder = Path(args.folder)
    plan = read_json(folder / "콘티.json")
    layouts = load_layouts(args)
    _, problems = validate_plan(plan, layouts, load_palettes(args))
    errors = [p for p in problems if p["level"] == "error"]
    if errors:
        from deck import cli_hint
        print(f"오류 · 콘티에 {len(errors)}건의 문제가 있습니다 · {cli_hint('plan', folder)}")
        return 1
    slides = plan["slides"]
    ids = [s["id"] for s in slides]
    only = None
    if args.only is not None:
        only = [part.strip() for part in args.only.split(",") if part.strip()]
        unknown = set(only) - set(ids)
        if not only or unknown:
            raise PlanError(f"--only의 장 id가 올바르지 않습니다: {', '.join(sorted(unknown))} · 콘티의 id를 쓰세요")
    selected = set(only if only is not None else ids)
    output = folder / "원고.html"
    existing_text = read_text(output) if output.exists() else None
    if existing_text is None:
        rendered = [rendered_slide(plan, s, kit_path(args)) for s in slides if s["id"] in selected]
        title = html.escape(attribute_text(plan["meta"]["title"]), quote=True)
        palette = html.escape(plan["meta"]["palette"], quote=True)
        result = ('<!doctype html>\n<html lang="ko" data-palette="' + palette + '">\n'
                  '<head>\n<meta charset="utf-8">\n<title>' + title + '</title>\n</head>\n<body>\n'
                  + "\n".join(rendered) + '\n</body>\n</html>\n')
    else:
        _, body, old_slides = document_slides(existing_text)
        by_id = {}
        for node in old_slides:
            ident = node["attrs"].get("data-id")
            if ident and ident in by_id:
                raise PlanError(f"원고의 data-id가 중복됩니다: {ident} · 장마다 다른 id를 쓰세요")
            if ident:
                by_id[ident] = node
            if not ident or ident not in ids:
                print(f"알림 · 원고에만 있는 장({ident or 'id 없음'})은 그대로 둡니다")
        existing_order = [n["attrs"].get("data-id") for n in old_slides if n["attrs"].get("data-id") in ids]
        if existing_order != [ident for ident in ids if ident in by_id]:
            print("알림 · 기존 장 순서가 콘티와 다릅니다 · 원고를 보존하므로 직접 정리하세요")
        edits, insertions = [], {}
        for index, slide in enumerate(slides):
            ident = slide["id"]
            if ident not in selected:
                continue
            if ident in by_id:
                if args.force:
                    node = by_id[ident]
                    edits.append((node["start"], node["end"], rendered_slide(plan, slide, kit_path(args))))
                continue
            following = next((by_id[x] for x in ids[index+1:] if x in by_id), None)
            preceding = next((by_id[x] for x in reversed(ids[:index]) if x in by_id), None)
            if following:
                anchor, before = following["start"], True
            elif preceding:
                anchor, before = preceding["end"], False
            else:
                anchor, before = body["end_start"], True
            insertions.setdefault((anchor, before), []).append(rendered_slide(plan, slide, kit_path(args)))
        for (anchor, before), fragments in insertions.items():
            value = "\n".join(fragments)
            edits.append((anchor, anchor, value + "\n" if before else "\n" + value))
        result = apply_edits(existing_text, edits)
    write_text(output, result)
    guide = ["# 진행안", ""]
    for number, slide in enumerate(slides, 1):
        guide.extend([f"## {number}. {plain_text(slide.get('title', ''))}", "", str(slide.get("talk", "")), ""])
    write_text(folder / "진행안.md", "\n".join(guide))
    print(f"원고 뼈대를 만들었습니다: {output} · 진행안.md")
    return 0


def newer(path, dependencies):
    try:
        return path.is_file() and all(not dep.is_file() or path.stat().st_mtime_ns > dep.stat().st_mtime_ns for dep in dependencies)
    except OSError:
        return False


def load_optional_json(path):
    try:
        return read_json(path)
    except PlanError:
        return None


def manifest_ok(path, ids):
    manifest = load_optional_json(path)
    if not isinstance(manifest, dict):
        return False
    entries = manifest.get("assets", manifest.get("items", manifest))
    if isinstance(entries, list):
        entries = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
    if not isinstance(entries, dict):
        return False
    def ok(entry):
        return isinstance(entry, dict) and (entry.get("status") == "ok" or entry.get("ok") is True)
    return all(ok(entry) for entry in entries.values()) and all(ok(entries.get(ident)) for ident in ids)


def status_command(args):
    from deck import cli_hint
    folder = Path(args.folder).resolve()
    plan_path = folder / "콘티.json"
    plan = load_optional_json(plan_path)
    if not isinstance(plan, dict):
        plan = {}
    slides = plan.get("slides") if isinstance(plan.get("slides"), list) else []
    slides = [s for s in slides if isinstance(s, dict) and isinstance(s.get("id"), str)]
    ids = [s["id"] for s in slides]
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    assets = plan.get("assets") if isinstance(plan.get("assets"), list) else []
    assets = [a for a in assets if isinstance(a, dict) and isinstance(a.get("id"), str)]
    mascot = plan.get("mascot") if isinstance(plan.get("mascot"), dict) else {}
    report = load_optional_json(folder / "검사" / "plan.json")
    plan_ok = (isinstance(report, dict) and isinstance(report.get("problems"), list) and
               all(isinstance(p, dict) and p.get("level") != "error" for p in report["problems"]) and
               report.get("errors") == 0 and bool(ids) and newer(folder / "검사" / "plan.json", [plan_path]))
    source = folder / "원고.html"
    source_ids = []
    source_title = meta.get("title", "제목 없음")
    if source.is_file():
        try:
            source_text = read_text(source)
            source_doc, _, parsed = document_slides(source_text)
            source_ids = [n["attrs"].get("data-id") for n in parsed]
            title_node = next((n for n in source_doc.nodes if n["tag"] == "title" and n["closed"]), None)
            if title_node:
                source_title = html.unescape(re.sub(r"<[^>]*>", "", source_text[title_node["start_end"]:title_node["end_start"]])).strip() or "제목 없음"
        except (PlanError, ValueError):
            pass
    scaffold_ok = bool(ids) and all(ident in source_ids for ident in ids)
    master = mascot.get("id", "mascot")
    def mockup_exists(slide):
        by_id = {s["id"]: (i, s) for i, s in enumerate(slides, 1)}
        visited = set()
        while isinstance(slide.get("mockup"), str) and slide["mockup"].startswith("same-as:"):
            ident = slide["mockup"][len("same-as:"):]
            if ident in visited or ident not in by_id:
                return False
            visited.add(ident)
            slide = by_id[ident][1]
        index, slide = by_id[slide["id"]]
        return (folder / "시안" / f'{index:02d}-{slide["id"]}.png').is_file()
    art_ok = isinstance(master, str) and (folder / "그림" / (master + ".png")).is_file()
    art_ok = bool(art_ok and slides and all(mockup_exists(slide) for slide in slides))
    asset_ids = [a["id"] for a in assets]
    asset_ok = manifest_ok(folder / "그림" / "manifest.json", asset_ids)
    target = folder / (safe_name(source_title) + ".html")
    pictures = [p for p in (folder / "그림").glob("*.png") if p.name != "_sheet.png"]
    implementation_ok = source.is_file() and target.is_file() and newer(target, [source] + pictures)
    if implementation_ok:
        try:
            target_doc = SourceParser(read_text(target))
            implementation_ok = not any("data-placeholder" in n["attrs"] for n in target_doc.nodes)
        except (PlanError, ValueError):
            implementation_ok = False
    check_path = folder / "검사" / "report.json"
    check_report = load_optional_json(check_path)
    check_ok = (target.is_file() and newer(check_path, [target]) and isinstance(check_report, dict) and
                isinstance(check_report.get("problems"), list) and
                all(isinstance(p, dict) and p.get("level") != "error" for p in check_report["problems"]) and
                check_report.get("slide_count") == len(ids) and isinstance(check_report.get("checked"), list) and
                all(isinstance(n, int) for n in check_report["checked"]) and
                set(check_report["checked"]) == set(range(1, len(ids) + 1)) and bool(ids))
    compare = folder / "검사" / "compare.json"
    review = folder / "검사" / "대조.md"
    compare_data = load_optional_json(compare)
    compared = compare_data.get("slides", []) if isinstance(compare_data, dict) else []
    compared_ids = {item.get("id") for item in compared if isinstance(item, dict) and isinstance(item.get("id"), str)} if isinstance(compared, list) else set()
    compare_reason = None
    collect_compare = cli_hint("compare", folder)
    write_review = f'모든 장의 대조 그림을 보고 "{review.as_posix()}"에 판정을 쓰세요 (참고: 대조와-마감.md).'
    if not target.is_file() or not compare.is_file():
        compare_reason = "결과 HTML 또는 compare.json이 없습니다"
    elif not newer(compare, [target]):
        compare_reason = "compare.json이 최신 빌드보다 오래됨: 대조를 다시 실행하세요"
    elif not ids or not set(ids).issubset(compared_ids):
        compare_reason = "compare.json에 모든 장의 대조 결과가 없습니다"
    else:
        collect_compare = write_review
        if not review.is_file():
            compare_reason = "대조.md가 없습니다"
        elif not newer(review, [compare]):
            compare_reason = "대조.md가 새 대조보다 오래됨: 다시 보고 고쳐 쓰세요"
        else:
            try:
                review_text = read_text(review)
                if not (re.search(r"최종\s*:\s*통과", review_text) and all(
                        re.search(r"(?m)^.*(?:\b" + re.escape(ident) + r"\b|\|\s*" + str(i) + r"\s*\|).*$", review_text)
                        for i, ident in enumerate(ids, 1))):
                    compare_reason = "대조.md에 모든 장의 판정 또는 ‘최종: 통과’가 없습니다"
            except PlanError:
                compare_reason = "대조.md를 읽을 수 없습니다"
    final = folder / "검사" / "마감.md"
    final_reason = None
    if not final.is_file():
        final_reason = "마감.md가 없습니다"
    elif check_path.is_file() and not newer(final, [check_path]):
        final_reason = "마감.md가 새 검사보다 오래됨: 다시 검수하고 고쳐 쓰세요"
    elif review.is_file() and not newer(final, [review]):
        final_reason = "마감.md가 최신 대조.md보다 오래됨: 다시 검수하고 고쳐 쓰세요"
    elif compare_reason or not check_ok:
        final_reason = "최신 검사와 대조를 먼저 통과하세요"
    else:
        try:
            if not re.fullmatch(r"\s*판정\s*:\s*통과\s*", read_text(final).splitlines()[0]):
                final_reason = "마감.md 첫 줄에 ‘판정: 통과’가 없습니다"
        except (PlanError, IndexError):
            final_reason = "마감.md의 판정을 읽을 수 없습니다"
    write_final = f'최신 검사와 대조 결과를 검수하고 "{final.as_posix()}"에 판정을 쓰세요 (참고: 대조와-마감.md).'
    art_step = "mockups" if (folder / "그림" / (str(master) + ".png")).is_file() else "mascot"
    stages = [("콘티", plan_ok, cli_hint("plan", folder)),
              ("뼈대", scaffold_ok, cli_hint("scaffold", folder)),
              ("시안", art_ok, cli_hint("art", folder, art_step)),
              ("에셋", asset_ok, cli_hint("art", folder, "assets")),
              ("구현", implementation_ok, cli_hint("build", folder)),
              ("검사", check_ok, cli_hint("check", folder, "--shots")),
              ("대조", compare_reason is None, collect_compare),
              ("마감", final_reason is None, write_final)]
    reasons = {"대조": compare_reason, "마감": final_reason}
    next_command = next((command for _, ok, command in stages if not ok), None)
    if args.json:
        print(json.dumps({"stages": [{"name": name, "ok": bool(ok), **({"reason": reasons[name]} if reasons.get(name) else {})}
                                    for name, ok, _ in stages],
                          "next": next_command}, ensure_ascii=False, indent=2))
    else:
        print("단계 | 상태")
        for name, ok, _ in stages:
            reason = " · " + reasons[name] if reasons.get(name) else ""
            print(f'{name} | {"✓" if ok else "✗"}{reason}')
        print("다음에 할 일: " + (next_command or "없음"))
    return 1 if next_command else 0


def command_wrapper(function):
    def call(args):
        try:
            return function(args)
        except (PlanError, OSError, UnicodeError, ValueError) as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 2
    return call


def register(subparsers):
    catalog = subparsers.add_parser("layouts", help="구도 목록과 필드·예시를 봅니다")
    catalog.add_argument("keys", nargs="?", metavar="키,…", help="자세히 볼 구도 키")
    catalog.add_argument("--kit", metavar="폴더", help="구도 등록부가 든 kit 폴더")
    catalog.add_argument("--json", action="store_true", help="JSON으로 출력합니다")
    catalog.set_defaults(function=command_wrapper(layouts_command))
    new = subparsers.add_parser("new", help="새 콘티 틀을 만듭니다")
    new.add_argument("folder", metavar="덱폴더")
    new.add_argument("--title", required=True, metavar="제목")
    new.add_argument("--kind", choices=("lesson", "training", "talk"), default="lesson")
    new.add_argument("--palette", default=None, metavar="이름", help="색 묶음(생략하면 부품의 첫 번째)")
    new.set_defaults(function=command_wrapper(new_command))
    plan = subparsers.add_parser("plan", help="콘티를 검사합니다")
    plan.add_argument("folder", metavar="덱폴더")
    plan.add_argument("--kit", metavar="폴더", help="구도 등록부가 든 kit 폴더")
    plan.set_defaults(function=command_wrapper(plan_command))
    scaffold = subparsers.add_parser("scaffold", help="원고 뼈대를 만듭니다")
    scaffold.add_argument("folder", metavar="덱폴더")
    scaffold.add_argument("--only", metavar="id,…")
    scaffold.add_argument("--force", action="store_true")
    scaffold.add_argument("--kit", metavar="폴더", help="구도 등록부가 든 kit 폴더")
    scaffold.set_defaults(function=command_wrapper(scaffold_command))
    status = subparsers.add_parser("status", help="제작 상태를 확인합니다")
    status.add_argument("folder", metavar="덱폴더")
    status.add_argument("--json", action="store_true")
    status.set_defaults(function=command_wrapper(status_command))


class KoreanParser(argparse.ArgumentParser):
    def format_help(self):
        return (super().format_help().replace("usage:", "사용법:").replace("positional arguments:", "위치 인자:")
                .replace("options:", "옵션:").replace("show this help message and exit", "도움말을 표시합니다"))

    def error(self, message):
        for old, new in {"the following arguments are required:": "필수 인자가 없습니다:",
                         "unrecognized arguments:": "알 수 없는 인자:", "invalid choice:": "올바르지 않은 선택:",
                         "expected one argument": "값이 필요합니다", "argument": "인자"}.items():
            message = message.replace(old, new)
        self.exit(2, f"오류: {message} · --help로 사용법을 확인하세요\n")


def main(argv=None):
    parser = KoreanParser(description="콘티를 검사하고 슬라이드 뼈대를 만듭니다.")
    subparsers = parser.add_subparsers(dest="command", required=True, title="명령")
    register(subparsers)
    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    sys.exit(main())
