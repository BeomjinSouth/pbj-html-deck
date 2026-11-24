"""시안·그림 생성과 구현 대조. deck.py에서 register로 명령을 붙인다.

지시문은 assets/prompts/*.txt에 있고 이 파일은 자리만 채운다:
{STYLE} 화풍 한 문단 · {PALETTE} 색 묶음(역할과 함께) · {SUBJECT} 에셋의 묘사(시안에서는 제목)
{PURPOSE} {FOCUS} 콘티의 목적·초점 · {LAYOUT} 등록부의 구도 설명 · {TEXTS} 화면에 보이는 글자("레이블: "문구"" 줄)
{ILLUSTRATIONS} 그 장에 보이는 그림과 자리 · {NOTE} 콘티의 mockup_note.
첨부한 참고 그림이 저마다 무엇인지는 render_message가 Codex에게 따로 알려 준다(ROLE_*).
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import base64
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib

import deck

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageChops, ImageStat
except ImportError:
    Image = ImageDraw = ImageFont = ImageOps = ImageFilter = ImageChops = ImageStat = None

KINDS = {"mascot_master", "mascot", "sticker", "scene", "background"}
ID = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
DEFAULT_PALETTE = "#FFF8EC, #33304D, #F5A9B8, #77B6A3, #F7CB73"


def safe_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise deck.DeckError(f"안전하지 않은 id입니다: {value!r}")
    return value


def read_json(path, default=None):
    if default is not None and not path.exists():
        return default
    try:
        return json.loads(deck.read_text(path))
    except (ValueError, TypeError) as exc:
        raise deck.DeckError(f"JSON을 읽을 수 없습니다: {path} ({exc})") from exc


def save_json(path, value):
    deck.write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def plan_data(folder):
    plan = read_json(folder / "콘티.json")
    if not isinstance(plan, dict) or not isinstance(plan.get("slides", []), list) or not isinstance(plan.get("assets", []), list):
        raise deck.DeckError("콘티는 slides와 assets 배열을 가진 JSON 객체여야 합니다")
    for section in ("slides", "assets"):
        seen = set()
        for item in plan.get(section, []):
            if not isinstance(item, dict):
                raise deck.DeckError(f"콘티의 {section} 항목은 객체여야 합니다")
            ident = safe_id(item.get("id"))
            if ident in seen:
                raise deck.DeckError(f"콘티에 중복 id가 있습니다: {ident}")
            seen.add(ident)
    for asset in plan.get("assets", []):
        if asset.get("kind") not in KINDS:
            raise deck.DeckError(f"알 수 없는 에셋 종류: {asset.get('id')}")
    return plan


def master_id(plan):
    return safe_id((plan.get("mascot") or {}).get("id") or "mascot")


def style_values(plan, assets, ground=True):
    default = deck.read_text(assets / "prompts" / "style-default.txt").strip()
    style = (plan.get("style") or {}).get("asset_style") or default
    palettes = read_json(assets / "kit" / "palettes.json", {})
    palette_name = (plan.get("meta") or {}).get("palette", "")
    selected = palettes.get(palette_name, {}) if isinstance(palettes, dict) else {}
    # 등록부가 palettes라는 바깥 키를 쓰는 경우도 받는다.
    if not selected and isinstance(palettes, dict) and isinstance(palettes.get("palettes"), dict):
        selected = palettes["palettes"].get(palette_name, {})
    colors = re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", json.dumps(selected, ensure_ascii=False))
    # 부품의 색 묶음(바탕·글자와 외곽선·강조·스티커 색)은 역할을 붙여 적는다. 모르는 꼴이면 색만 늘어놓는다.
    if isinstance(selected, dict) and all(isinstance(selected.get(k), str) for k in ("ground", "ink", "accent")):
        stickers = selected.get("stickers") if isinstance(selected.get("stickers"), list) else []
        stickers = [c for c in stickers if isinstance(c, str)]
        # 낱개 그림에는 바탕색을 알려 주지 않는다(투명 배경 대신 바탕을 칠해 버린다).
        named = ((f"background {selected['ground']}, text and outlines" if ground else "outlines")
                 + f" {selected['ink']}, accent {selected['accent']}"
                 + (", sticker colors " + " ".join(stickers) if stickers else ""))
        return str(style), named
    return str(style), ", ".join(dict.fromkeys(colors)) or DEFAULT_PALETTE


def render_prompt(assets, template, values):
    try:
        return deck.read_text(assets / "prompts" / template).format_map(values).strip() + "\n"
    except (KeyError, ValueError) as exc:
        raise deck.DeckError(f"프롬프트 틀의 자리를 채울 수 없습니다: {template} ({exc})") from exc


def asset_prompt(plan, assets, asset):
    style, palette = style_values(plan, assets, ground=asset["kind"] == "background")
    kind = asset["kind"]
    name = "asset-" + kind.replace("_", "-") + ".txt"
    text = asset.get("text")
    instruction = (f'The only text in the image is exactly: "{text}". No other text, letters, numbers, watermark or signature.'
                   if text else "No text, no letters, no numbers, no watermark, no signature.")
    return render_prompt(assets, name, {"STYLE": style, "PALETTE": palette,
                                       "SUBJECT": str(asset.get("prompt", "")), "TEXTS": instruction,
                                       "PURPOSE": "", "FOCUS": "", "LAYOUT": "", "ILLUSTRATIONS": "", "NOTE": ""})


def contains_value(value, ident):
    if isinstance(value, dict):
        return any(contains_value(v, ident) for v in value.values())
    if isinstance(value, list):
        return any(contains_value(v, ident) for v in value)
    return value == ident


def reference_mockup(folder, plan, ident):
    """에셋을 그릴 때 참고할 시안과, 그 시안에 이 그림이 실제로 그려져 있는지를 돌려준다.

    이 에셋을 쓰는 장 가운데 시안에 그림이 보이는 첫 장을 고른다. 어느 시안에도 없으면(단계가 지나 가려지는 그림 등)
    처음 쓰는 장의 시안을 화풍 참고로만 준다.
    """
    manifest = read_json(folder / "시안" / "manifest.json", {})
    holders = [s for s in plan.get("slides", []) if contains_value(s, ident)]
    for slide in holders:
        path = mockup_path(folder, plan, slide)
        entry = manifest.get(path.stem.split("-", 1)[-1]) if isinstance(manifest, dict) else None
        shown = entry.get("shown") if isinstance(entry, dict) else None
        if not isinstance(shown, list) or ident in shown:
            return path, True
    return (mockup_path(folder, plan, holders[0]), False) if holders else (None, False)


def mockup_path(folder, plan, slide):
    by_id = {s["id"]: (i, s) for i, s in enumerate(plan.get("slides", []), 1)}
    seen = set()
    while str(slide.get("mockup", "")).startswith("same-as:"):
        ident = str(slide["mockup"])[8:]
        if ident in seen or ident not in by_id:
            raise deck.DeckError(f"시안 same-as 참조를 찾을 수 없거나 순환합니다: {ident}")
        seen.add(ident)
        slide = by_id[ident][1]
    index, _ = by_id[slide["id"]]
    return folder / "시안" / f"{index:02d}-{slide['id']}.png"


# ---- 시안 지시문에 넣을 글자 목록 ---------------------------------------------------
# 화면에 처음부터 보이는 글자만 "레이블: "문구"" 줄로 적는다. 정답 표시·해설·식·SVG처럼 시안에 나오면 안 되는 것은 뺀다.
HIDDEN_FIELDS = {"explain", "cheer", "retry", "answer", "back", "correct", "to", "key", "truth", "mode", "tol", "now",
                 "hl", "yesno", "dashed", "tone", "snap", "drag", "at", "fn", "if", "var", "min", "max", "step",
                 "state", "graph", "stage", "draw", "image", "range", "curves"}
PLAN_ONLY = {"id", "layout", "purpose", "focus", "talk", "mockup_note", "mockup", "mascot", "reveal"}
FIELD_LABELS = {"tag": "Tag pill", "title": "Title", "sub": "Subtitle", "by": "Byline", "hint": "Hint chip",
                "note": "Yellow conclusion bar", "next": "Bottom bar", "say": "Mascot speech bubble", "items": "Row",
                "points": "Numbered row", "steps": "Step row", "options": "Answer button", "chips": "Chip",
                "bins": "Drop zone", "head": "Table header", "rows": "Table row", "cards": "Card",
                "left": "Left panel", "right": "Right panel", "controls": "Slider", "formula": "Formula readout",
                "readouts": "Readout row", "status": "Status badge", "question": "Question", "unit": "Unit label",
                "problem": "Problem", "lines": "Line", "frame": "Sentence frame", "lead": "Lead line",
                "no": "Big number", "label": "Label", "tabs": "Tab", "pairs": "Pair", "timer": "Timer button"}
PART_LABELS = {"title": "heading", "desc": "caption", "text": "", "label": "label", "lines": "line", "front": "front",
               "prompt": "prompt", "heading": "heading", "left": "left", "right": "right", "why": "reason chip",
               "cells": "", "value": "value box", "unit": "unit"}
MATH_WORDS = {"times": "×", "div": "÷", "pm": "±", "le": "≤", "ge": "≥", "ne": "≠", "approx": "≈", "cdot": "·",
              "angle": "∠", "triangle": "△", "square": "□", "perp": "⊥", "parallel": "∥", "equiv": "≡", "sim": "∼",
              "pi": "π", "circ": "°", "therefore": "∴", "because": "∵", "cdots": "⋯", "quad": " "}
SUPERSCRIPT = str.maketrans("0123456789+-n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ")


def live_value(expression, state):
    """조작 값의 처음 값. 계산할 수 없으면 식을 그대로 둔다."""
    try:
        import deck_plan
        body, _, fix = str(expression).partition(":")
        return deck_plan.live_show(deck_plan.live_eval(body.strip(), state or {}), fix.strip() or None)
    except Exception:
        return str(expression)


def live_true(expression, state):
    """조건식이 처음 상태에서 참인지. 계산할 수 없으면 None."""
    try:
        import deck_plan
        return bool(deck_plan.live_eval(str(expression).strip(), state or {}))
    except Exception:
        return None


def math_words(source, state):
    """$…$ 안의 수식을 화면에 보이는 꼴에 가까운 한 줄 글자로 바꾼다(이미지 모델이 읽을 용도)."""
    text = re.sub(r"\\val\{([^{}]*)\}", lambda m: live_value(m.group(1), state), source)
    for _ in range(4):
        text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2", text)
        text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√\1", text)
        text = re.sub(r"\\(?:text|overline|overarc)\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\^\{?([0-9n+-]+)\}?", lambda m: m.group(1).translate(SUPERSCRIPT), text)
    text = re.sub(r"\\([A-Za-z]+)", lambda m: MATH_WORDS.get(m.group(1), m.group(1)), text)
    text = text.replace("{", "").replace("}", "").replace("-", "−")
    return re.sub(r"\s+", " ", text).strip()


def prompt_text(value, state=None):
    """콘티의 표시 문법을 벗긴 글자와, 강조(**…**)된 낱말 목록을 돌려준다. 빈칸 [[답]]은 답을 가린 밑줄로 적는다."""
    text, out, marked, i = str(value), [], [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] in "*$\\[{":
            out.append(text[i + 1])
            i += 2
        elif c == "$" and text.find("$", i + 1) > 0:
            end = text.find("$", i + 1)
            out.append(math_words(text[i + 1:end], state))
            i = end + 1
        elif text.startswith("[[", i) and text.find("]]", i + 2) > 0:
            out.append("____")
            i = text.find("]]", i + 2) + 2
        elif c == "{" and state is not None and text.find("}", i + 1) > 0:
            end = text.find("}", i + 1)
            out.append(live_value(text[i + 1:end], state))
            i = end + 1
        elif text.startswith("**", i) and text.find("**", i + 2) > 0:
            end = text.find("**", i + 2)
            inner, _ = prompt_text(text[i + 2:end], state)
            out.append(inner)
            marked.append(inner)
            i = end + 2
        elif c == "*" and text.find("*", i + 1) > 0:
            end = text.find("*", i + 1)
            out.append(prompt_text(text[i + 1:end], state)[0])
            i = end + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), marked


def quoted(value, state=None):
    """글자 한 덩어리를 따옴표 줄로. 줄 나눔은 줄마다 따옴표를 따로 친다."""
    plain, marked = prompt_text(value, state)
    lines = [line.strip() for line in plain.split("\n") if line.strip()]
    if not lines:
        return ""
    result = " / ".join(f'"{line}"' for line in lines)
    if len(lines) > 1:
        result += f" ({len(lines)} lines, broken exactly here)"
    if marked:
        result += " — highlighted: " + ", ".join(f'"{word}"' for word in marked)
    return result


def visible_texts(slide, entry):
    """그 장에 처음부터 보이는 글자를 화면 순서에 가깝게 "레이블: "문구"" 줄로 모은다."""
    state = slide.get("state") if isinstance(slide.get("state"), dict) else None
    fields = entry.get("fields") if isinstance(entry, dict) and isinstance(entry.get("fields"), dict) else {}
    lines = []

    def add(label, value):
        text = quoted(value, state)
        if text:
            lines.append(f"- {label}: {text}")

    def walk(name, value, spec, label):
        kind = spec.get("type") if isinstance(spec, dict) and isinstance(spec.get("type"), str) else None
        if name in HIDDEN_FIELDS or kind in ("raw", "json", "bool", "asset", "choice"):
            return
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            if name == "timer":
                add(label, f"{int(value) // 60}:{int(value) % 60:02d}")
            elif name == "value":
                add(label, live_value(repr(value), {}))
            return
        if isinstance(value, str):
            if not re.search(r"<[A-Za-z/!]", value):      # 날것(SVG 등)은 글자가 아니다
                add(label, value)
            return
        of = spec.get("of") if isinstance(spec, dict) else None
        if isinstance(value, list):
            items = value
            if name == "tabs":          # 탭 이름은 모두 보이고, 내용은 처음 탭 것만 보인다.
                for n, item in enumerate(items, 1):
                    if isinstance(item, dict) and isinstance(item.get("label"), str):
                        add(f"Tab {n} label", item["label"])
                items = [{k: v for k, v in items[0].items() if k != "label"}] if items and isinstance(items[0], dict) else []
                label = "Open tab"
            if name == "status":        # 처음 상태에서 참인 것 하나만 보인다.
                shown = [item for item in items if isinstance(item, dict) and live_true(item.get("if", ""), state)]
                items = shown[:1] or items[:1]
            if name in ("cells", "head") and all(isinstance(item, str) for item in items):
                cells = [prompt_text(item, state)[0] for item in items]
                if cells:
                    lines.append(f"- {label}: " + " | ".join(f'"{cell}"' for cell in cells))
                return
            single = len(items) == 1
            for n, item in enumerate(items, 1):
                mark = chr(64 + n) if name == "options" and n <= 26 else str(n)
                item_label = label if single else f"{label} {mark}"
                if isinstance(item, dict):
                    sub = of if isinstance(of, dict) and "type" not in of else {}
                    for key, part in item.items():
                        walk(key, part, sub.get(key, {}), (item_label + " " + PART_LABELS.get(key, key)).strip())
                else:
                    walk(name + "[]", item, of if isinstance(of, dict) and "type" in of else {}, item_label)
            return
        if isinstance(value, dict):
            sub = of if isinstance(of, dict) and "type" not in of else {}
            for key, part in value.items():
                walk(key, part, sub.get(key, {}), (label + " " + PART_LABELS.get(key, key)).strip())

    ordered = [k for k in ("tag", "title") if k in slide]
    ordered += [k for k in fields if k in slide and k not in ordered]
    ordered += [k for k in slide if k not in ordered]
    for key in ordered:
        if key in PLAN_ONLY or key == "say":
            continue
        walk(key, slide[key], fields.get(key, {}), FIELD_LABELS.get(key, key))
    say = slide.get("say")
    if isinstance(say, list) and say and slide.get("mascot"):
        # 정리의 즉시 공개는 단계 속성을 만들지 않아 첫 대사만 보인다.
        # 다른 순차 대사 구도는 명시적인 단계 속성을 쓰므로 전부 공개한 캡처에는 마지막 대사가 보인다.
        first = "cheer" in fields or (slide.get("layout") == "summary" and slide.get("reveal") == "all")
        line = say[0] if first else say[-1]
        if isinstance(line, str):
            add(FIELD_LABELS["say"], line)
    return "\n".join(lines) or "- (no text on this slide)"


def illustration_lines(plan, slide, shown=None, placed=True):
    """그 장의 자리표시(점선 상자, 가운데에 alt 글자)를 무엇으로 바꿀지 한 줄씩 적는다.

    shown은 캡처에 실제로 보인 에셋들이다({id: "placeholder"|"drawn"}). 단계가 지나 가려진 그림, 맞음·틀림 때만
    나오는 그림은 적지 않는다. 에셋을 만든 뒤 시안을 다시 뽑으면 자리표시 대신 실제 그림이 보이므로 그에 맞게 적는다.
    placed가 거짓이면(캡처를 주지 않는 compose) 자리를 말하지 않는다 — 자리는 구도 설명이 정한다.
    """
    names = {"mascot_master": "The mascot", "mascot": "The mascot", "sticker": "A sticker", "scene": "A sticker scene",
             "background": "A background"}
    master = master_id(plan)
    candidates = list(plan.get("assets", []))
    if not any(a.get("id") == master for a in candidates):
        candidates.insert(0, {"id": master, "kind": "mascot_master", "alt": master})
    lines = []
    for asset in candidates:
        if not contains_value(slide, asset["id"]) or (shown is not None and asset["id"] not in shown):
            continue
        caption = asset.get("alt") or asset["id"]
        subject = str(asset.get("prompt", "")).strip() or caption
        if asset.get("kind") == "mascot_master":
            subject = "standing in its default friendly pose, exactly as on the character sheet"
        state = shown.get(asset["id"]) if isinstance(shown, dict) else "placeholder"
        where = (f'in place of the grey dashed placeholder box captioned "{caption}"' if state == "placeholder"
                 else "already drawn in the screenshot: keep it in the same place, pose and size")
        lines.append(f'- {names.get(asset.get("kind"), "An illustration")}' + (f" ({where})" if placed else "") + f": {subject}")
    return "\n".join(lines) or "- none: this slide has no illustration. Do not add the mascot or any other character."


def mockup_prompt(plan, assets, slide, layouts, mode, shown=None):
    style, palette = style_values(plan, assets)
    entry = layouts.get(slide.get("layout"), {}) if isinstance(layouts, dict) else {}
    layout = entry.get("mockup", "") if isinstance(entry, dict) else ""
    return render_prompt(assets, f"mockup-{mode}.txt", {
        "STYLE": style, "PALETTE": palette, "SUBJECT": prompt_text(slide.get("title", ""))[0].replace("\n", " "),
        "PURPOSE": prompt_text(slide.get("purpose", ""))[0], "FOCUS": prompt_text(slide.get("focus", ""))[0],
        "LAYOUT": layout or "(follow the reference)",
        "TEXTS": visible_texts(slide, entry if isinstance(entry, dict) else {}),
        "ILLUSTRATIONS": illustration_lines(plan, slide, shown, placed=mode == "polish"),
        "NOTE": str(slide.get("mockup_note") or "none")})


def png_solid(width, height, transparent=False):
    # Pillow가 없는 환경에서도 가짜 렌더러가 유효한 RGBA PNG를 만든다.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    if transparent:
        row = b"\0" + b"\0\0\0\0" * (width // 8) + b"\xf5\xa9\xb8\xff" * (width - 2 * (width // 8)) + b"\0\0\0\0" * (width // 8)
        blank = b"\0" + b"\0\0\0\0" * width
        raw = blank * (height // 8) + row * (height - 2 * (height // 8)) + blank * (height // 8)
    else:
        raw = (b"\0" + b"\xf8\xe4\xd2\xff" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


class Renderer:
    def __init__(self):
        self.exe = shutil.which("codex.cmd") or shutil.which("codex")
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.failures = 0

    def render(self, prompt, attachments, kind, job_root, roles=()):
        if self.stop.is_set():
            raise deck.DeckError("앞선 생성 실패로 호출을 중단했습니다")
        if os.environ.get("DECK_ART_FAKE") == "1":
            path = job_root / "fake.png"
            width, height = ((1920, 1080) if kind in ("mockup", "background") else (800, 800))
            path.write_bytes(png_solid(width, height, kind not in ("mockup", "background")))
            return path, {"thread_id": "fake"}
        if not self.exe:
            self.stop.set()
            raise deck.DeckError("codex 명령을 찾을 수 없습니다")
        if not str(job_root).isascii():
            raise deck.DeckError("Codex 임시 작업 폴더에는 ASCII 경로가 필요합니다")
        staged = []
        for index, source in enumerate(attachments, 1):
            target = job_root / f"img{index}.png"
            shutil.copyfile(source, target)
            staged.append(target)
        message = render_message(prompt, kind, list(roles)[:len(staged)], len(staged))
        command = [self.exe, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "-C", str(job_root), "-s", "read-only", "--json"]
        for source in staged:
            command.extend(["-i", str(source)])
        command.extend(["-o", str(job_root / "last.txt"), "-"])
        try:
            result = subprocess.run(command, input=message.encode("utf-8"), capture_output=True, timeout=600,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stdout = result.stdout.decode("utf-8", "replace")
            stderr = result.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", "replace")
            stderr = "600초 시간 초과"
        thread_id, messages = None, []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                item = event.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    messages.append(str(item.get("text", "")))
        images = []
        if thread_id and re.fullmatch(r"[A-Za-z0-9_-]+", str(thread_id)):
            home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
            images = list((home / "generated_images" / thread_id).glob("*.png"))
        if images:
            with self.lock:
                self.failures = 0
            return max(images, key=lambda p: p.stat().st_mtime_ns), {"thread_id": thread_id}
        last_file = job_root / "last.txt"
        last = last_file.read_text(encoding="utf-8", errors="replace").strip() if last_file.exists() else (messages[-1] if messages else "")
        diagnostic = (stderr + "\n" + stdout + "\n" + last).lower()
        fatal = any(term in diagnostic for term in ("401 unauthorized", "authentication", "not logged in", "please log in", "login required", "connection refused", "connection error", "network error", "error sending request", "failed to connect", "failed to lookup address", "image generation failed", "404"))
        with self.lock:
            self.failures += 1
            if fatal or self.failures >= 2:
                self.stop.set()
        raise deck.DeckError(f"이미지가 생성되지 않았습니다. {last[:300]} {stderr[-800:]}")


def render_message(prompt, kind, roles, count):
    """Codex에게 보내는 말. 지시문은 표식 사이에 그대로 넣고, 첨부 그림이 저마다 무엇인지 알려 준다."""
    if kind in ("background", "mockup"):
        canvas = ("Use an opaque background and a 16:9 landscape canvas (2048x1152 or 1536x864 if such sizes are offered; "
                  "otherwise the widest landscape size offered). ")
    else:
        canvas = "Use a transparent background (background=transparent) if the tool supports it, and a square canvas. "
    if count:
        named = [f"Attached image {n} is {roles[n - 1]}." if n <= len(roles) and roles[n - 1]
                 else f"Attached image {n} is a visual reference." for n in range(1, count + 1)]
        attached = " ".join(named) + " Use the attached images as reference images for the generation. "
    else:
        attached = ""
    return ("[DECK-RENDER]\nYou are a renderer. Do not read or write files and do not run shell commands. "
            "Call the built-in image generation tool exactly once, passing the text between the markers verbatim as the prompt "
            "(do not change a single character inside quotes). " + canvas + attached
            + "When finished reply with exactly one line: DONE or FAILED: <reason>.\n<<<PROMPT\n" + prompt + "PROMPT>>>")


def alpha_ratio(image):
    return sum(image.getchannel("A").histogram()[:10]) / (image.width * image.height)


def remove_background(image):
    width, height = image.size
    pixels = image.load()
    corners = [pixels[0, 0], pixels[width - 1, 0], pixels[0, height - 1], pixels[width - 1, height - 1]]
    color = tuple(sum(p[k] for p in corners) / 4 for k in range(3))
    seen = bytearray(width * height)
    queue = deque()
    def add(x, y):
        index = y * width + x
        if seen[index]:
            return
        seen[index] = 1
        if sum(abs(pixels[x, y][k] - color[k]) for k in range(3)) < 60:
            queue.append(index)
    for x in range(width):
        add(x, 0)
        add(x, height - 1)
    for y in range(height):
        add(0, y)
        add(width - 1, y)
    while queue:
        index = queue.popleft()
        x, y = index % width, index // width
        p = pixels[x, y]
        pixels[x, y] = (p[0], p[1], p[2], 0)
        if x: add(x - 1, y)
        if x + 1 < width: add(x + 1, y)
        if y: add(x, y - 1)
        if y + 1 < height: add(x, y + 1)


def chrome_canvas(images, width, height, script):
    # Pillow가 없을 때도 파일 URI나 외부 요청 없이 캔버스로 크기 변경·나란히 배치를 한다.
    urls = ["data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode("ascii") for p in images]
    expression = """(async()=>{const urls=URLS; const images=await Promise.all(urls.map(u=>new Promise((ok,no)=>{let i=new Image();i.onload=()=>ok(i);i.onerror=no;i.src=u}))); const c=document.createElement('canvas');c.width=WIDTH;c.height=HEIGHT;const ctx=c.getContext('2d'); SCRIPT;return c.toDataURL('image/png').split(',')[1]})()"""
    expression = expression.replace("URLS", json.dumps(urls)).replace("WIDTH", str(width)).replace("HEIGHT", str(height)).replace("SCRIPT", script)
    with deck.Browser() as browser:
        result = browser.cdp.evaluate(expression, True)
    return base64.b64decode(result)


def process_image(source, target, kind):
    if Image is None:
        if kind in ("mockup", "background"):
            script = "let i=images[0],s=Math.max(c.width/i.width,c.height/i.height),w=i.width*s,h=i.height*s;ctx.drawImage(i,(c.width-w)/2,(c.height-h)/2,w,h)"
            target.write_bytes(chrome_canvas([source], 1920, 1080, script))
            return {"size": [1920, 1080]}
        # 투명 배경을 유지하며 Chrome 캔버스로 같은 여백 정리·크기 조정을 수행한다.
        url = "data:image/png;base64," + base64.b64encode(Path(source).read_bytes()).decode("ascii")
        limit = 800
        script = r'''(async()=>{
          const image=await new Promise((ok,no)=>{let i=new Image();i.onload=()=>ok(i);i.onerror=no;i.src=__INPUT_URL__});
          const c=document.createElement('canvas');c.width=image.width;c.height=image.height;
          const ctx=c.getContext('2d',{willReadFrequently:true});ctx.drawImage(image,0,0);
          const w=c.width,h=c.height,n=w*h,img=ctx.getImageData(0,0,w,h),d=img.data;
          let transparent=0;for(let i=0;i<n;i++)if(d[i*4+3]<10)transparent++;
          let removed=false;
          if(transparent/n<0.03){
            const corners=[0,w-1,(h-1)*w,n-1],avg=[0,1,2].map(k=>corners.reduce((a,p)=>a+d[p*4+k],0)/4);
            const seen=new Uint8Array(n),q=new Uint32Array(n);let first=0,last=0;
            function add(x,y){let p=y*w+x;if(seen[p])return;seen[p]=1;let i=p*4;
              if(Math.abs(d[i]-avg[0])+Math.abs(d[i+1]-avg[1])+Math.abs(d[i+2]-avg[2])<60)q[last++]=p;}
            for(let x=0;x<w;x++){add(x,0);add(x,h-1)}for(let y=0;y<h;y++){add(0,y);add(w-1,y)}
            while(first<last){let p=q[first++],x=p%w,y=Math.floor(p/w);d[p*4+3]=0;
              if(x)add(x-1,y);if(x+1<w)add(x+1,y);if(y)add(x,y-1);if(y+1<h)add(x,y+1);}
            removed=true;ctx.putImageData(img,0,0);
          }
          let x0=w,y0=h,x1=-1,y1=-1;
          for(let y=0;y<h;y++)for(let x=0;x<w;x++)if(d[(y*w+x)*4+3]>8){x0=Math.min(x0,x);x1=Math.max(x1,x);y0=Math.min(y0,y);y1=Math.max(y1,y)}
          if(x1<0)return {error:'가공할 불투명 피사체가 없습니다'};
          const bw=x1-x0+1,bh=y1-y0+1,margin=Math.max(1,Math.ceil(Math.max(bw,bh)*0.03));
          const scale=Math.min(1,LIMIT/Math.max(bw+2*margin,bh+2*margin));
          const out=document.createElement('canvas');out.width=Math.max(1,Math.round((bw+2*margin)*scale));out.height=Math.max(1,Math.round((bh+2*margin)*scale));
          const draw=out.getContext('2d');draw.imageSmoothingQuality='high';
          draw.drawImage(c,x0,y0,bw,bh,margin*scale,margin*scale,bw*scale,bh*scale);
          const a=draw.getImageData(0,0,out.width,out.height).data;let clear=0;for(let i=3;i<a.length;i+=4)if(a[i]<10)clear++;
          return {png:out.toDataURL('image/png').split(',')[1],size:[out.width,out.height],transparent_ratio:clear/(out.width*out.height),bg_removed:removed};
        })()'''.replace("__INPUT_URL__", json.dumps(url)).replace("LIMIT", str(limit))
        with deck.Browser() as browser:
            result = browser.cdp.evaluate(script, True)
        if not isinstance(result, dict) or result.get("error"):
            raise deck.DeckError((result or {}).get("error", "브라우저가 그림을 가공하지 못했습니다"))
        target.write_bytes(base64.b64decode(result.pop("png")))
        result["transparent_ratio"] = round(result["transparent_ratio"], 6)
        removed = result.pop("bg_removed")
        if removed:
            result["bg_removed"] = "fallback"
            if result["transparent_ratio"] < 0.03:
                result["warning"] = "배경 제거 후에도 투명 비율이 3% 미만입니다"
        return result
    with Image.open(source) as original:
        if kind in ("mockup", "background"):
            raw_size = list(original.size)
            image = ImageOps.fit(original.convert("RGB"), (1920, 1080), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            image.save(target, "PNG")
            details = {"size": list(image.size), "raw_size": raw_size, "transparent_ratio": 0.0}
            if abs(raw_size[0] / raw_size[1] - 16 / 9) > 0.04:
                details["warning"] = f"원본이 16:9가 아니어서({raw_size[0]}×{raw_size[1]}) 가장자리가 잘렸습니다"
            return details
        image = original.convert("RGBA")
    details = {}
    if alpha_ratio(image) < 0.03:
        remove_background(image)
        details["bg_removed"] = "fallback"
        if alpha_ratio(image) < 0.03:
            details["warning"] = "배경 제거 후에도 투명 비율이 3% 미만입니다"
    bbox = image.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not bbox:
        raise deck.DeckError("가공할 불투명 피사체가 없습니다")
    image = image.crop(bbox)
    margin = max(1, math.ceil(max(image.size) * 0.03))
    image = ImageOps.expand(image, border=margin, fill=(0, 0, 0, 0))
    limit = 800
    image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    image.save(target, "PNG")
    details.update(size=list(image.size), transparent_ratio=round(alpha_ratio(image), 6))
    return details


def versioned_copy(source, directory, ident):
    raw = directory / "raw" / safe_id(ident)
    raw.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.stem[1:]) for p in raw.glob("v*.png") if re.fullmatch(r"v\d+", p.stem)]
    number = max(numbers, default=0) + 1
    while True:
        version = f"v{number:03d}"
        path = raw / (version + ".png")
        try:
            with path.open("xb") as dest, Path(source).open("rb") as src:
                shutil.copyfileobj(src, dest)
            return path, version
        except FileExistsError:
            number += 1


def sheet(directory, entries, wide=False):
    """모아보기 _sheet.png. 그림은 작은 칸 6열(남색 바탕 — 투명한 곳과 흰 테두리가 보인다), 시안(wide)은 16:9 큰 칸 3열."""
    existing = [(ident, path) for ident, path in entries if path.is_file()]
    if not existing:
        return
    cols, cell_w, cell_h, box_w, box_h, size = (3, 640, 396, 624, 351, 16) if wide else (6, 200, 200, 184, 166, 13)
    width, height = cols * cell_w, max(1, math.ceil(len(existing) / cols)) * cell_h
    if Image is None:
        script = (f"ctx.fillStyle='#2b2f5b';ctx.fillRect(0,0,c.width,c.height);images.forEach((im,n)=>{{let x=(n%{cols})*{cell_w},"
                  f"y=Math.floor(n/{cols})*{cell_h},s=Math.min({box_w}/im.width,{box_h}/im.height),w=im.width*s,h=im.height*s;"
                  f"ctx.drawImage(im,x+({cell_w}-w)/2,y+({cell_h - 28}-h)/2,w,h)}});")
        (directory / "_sheet.png").write_bytes(chrome_canvas([p for _, p in existing], width, height, script))
        return
    canvas = Image.new("RGB", (width, height), "#2b2f5b")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    for index, (ident, path) in enumerate(existing):
        x, y = index % cols * cell_w, index // cols * cell_h
        with Image.open(path) as source:
            image = source.convert("RGBA")
        image.thumbnail((box_w, box_h), Image.Resampling.LANCZOS)
        canvas.paste(image, (x + (cell_w - image.width) // 2, y + (cell_h - 28 - image.height) // 2), image)
        draw.text((x + 8, y + cell_h - 21 - (size - 13)), path.stem if wide else ident, font=font, fill="white")
    canvas.save(directory / "_sheet.png", "PNG")


SHOWN_ASSETS = """(() => {
  const slide = document.querySelectorAll('section.slide')[%d];
  if (!slide) return [];
  const seen = el => { const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && (!el.checkVisibility || el.checkVisibility({opacityProperty: true, visibilityProperty: true})); };
  const found = {};
  for (const el of [...slide.querySelectorAll('img[data-asset]')].filter(seen)) {
    const id = el.getAttribute('data-asset');
    if (found[id] !== 'placeholder') found[id] = el.hasAttribute('data-placeholder') ? 'placeholder' : 'drawn';
  }
  return found;
})()"""


def capture(source, indexes, directory, shown=None):
    with deck.Browser() as browser:
        cdp = browser.cdp
        for domain in ("Page.enable", "Runtime.enable", "Network.enable"):
            cdp.call(domain)
        # 외부 요청은 오프라인으로 막는다(주소 패턴으로 막으면 http:// 글자가 든 SVG 데이터 주소까지 막힌다).
        cdp.call("Network.emulateNetworkConditions", {"offline": True, "latency": 0,
                                                      "downloadThroughput": -1, "uploadThroughput": -1})
        cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 1, "mobile": False})
        cdp.call("Page.navigate", {"url": source.resolve().as_uri()})
        cdp.wait_event("Page.loadEventFired", 20)
        ready = cdp.evaluate("(async()=>{if(!window.deck)return false;await window.deck.ready;return true})()", True)
        if not ready:
            raise deck.DeckError(f"캡처할 덱 엔진이 없습니다: {source}")
        cdp.evaluate("Promise.resolve(window.deck.setMode('present'))", True)
        count = cdp.evaluate("window.deck.count")
        result = {}
        for index in indexes:
            if index >= count:
                raise deck.DeckError(f"캡처할 장이 없습니다: {index + 1}")
            cdp.evaluate(f"Promise.resolve(window.deck.go({index},'all'))", True)
            cdp.evaluate(deck.SETTLE)
            path = directory / f"capture-{index + 1:02d}.png"
            path.write_bytes(base64.b64decode(deck.screenshot(cdp, 1920, 1080)))
            result[index] = path
            if shown is not None:
                found = cdp.evaluate(SHOWN_ASSETS % index)
                # {에셋 id: "placeholder"(점선 자리표시) | "drawn"(이미 들어간 그림)}
                shown[index] = {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}
        return result


def build_temp(folder, assets, directory):
    from types import SimpleNamespace
    output = directory / "bundle.html"
    deck.build_command(SimpleNamespace(input=folder / "원고.html", output=output, assets=assets,
                                       force=True, no_font=False, student=False))
    return output


def prompt_hash(prompt, attachments):
    h = hashlib.sha256(prompt.encode("utf-8"))
    for path in attachments:
        h.update(b"\0")
        h.update(str(path.name).encode("utf-8"))
        h.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"missing")
    return h.hexdigest()


def generate_jobs(folder, category, jobs, options, job_root):
    directory = folder / ("시안" if category == "mockup" else "그림")
    directory.mkdir(parents=True, exist_ok=True)
    prompts_dir = directory / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path, {})
    if not isinstance(manifest, dict):
        raise deck.DeckError(f"manifest는 JSON 객체여야 합니다: {manifest_path}")
    for job in jobs:
        prompt_path = prompts_dir / (job["id"] + ".txt")
        attachments = json.dumps([str(p) for p in job["refs"]], ensure_ascii=False)
        deck.write_text(prompt_path, "첨부: " + attachments + "\n\n" + job["prompt"])
        if options.dry_run:
            print(f"[{job['id']}] 첨부: " + attachments)
            print(job["prompt"], end="")
    if options.dry_run:
        return 0
    renderer = Renderer()
    by_id = {job["id"]: job for job in jobs}
    pending = list(jobs)
    outcomes = {}
    lock = threading.Lock()
    def run(job):
        ident, target = job["id"], job["target"]
        refs = job["refs"]
        digest = prompt_hash(job["prompt"], refs)
        old = manifest.get(ident, {})
        if not options.force and target.is_file() and old.get("status") == "ok" and old.get("prompt_hash") == digest:
            print(f"[{ident}] 건너뜀 (프롬프트와 참고 그림이 같음)")
            return True
        missing = [str(p) for p in refs if not p.is_file()]
        if missing:
            print(f"경고: [{ident}] 없는 참고 그림은 빼고 진행합니다: " + ", ".join(missing))
        entry = {"kind": job["kind"], "status": "fail", "prompt_hash": digest, "version": old.get("version")}
        if job.get("shown") is not None:
            entry["shown"] = job["shown"]      # 이 시안에 그려진 에셋들(에셋을 뽑을 때 참고할 시안을 고르는 데 쓴다)
        started = time.monotonic()
        try:
            if renderer.stop.is_set():
                raise deck.DeckError("앞선 생성 실패로 호출을 중단했습니다")
            with tempfile.TemporaryDirectory(prefix="art-job-", dir=job_root) as temp:
                present = [n for n, p in enumerate(refs) if p.is_file()]
                roles = job.get("roles") or []
                image, info = renderer.render(job["prompt"], [refs[n] for n in present], job["kind"], Path(temp),
                                              [roles[n] if n < len(roles) else "" for n in present])
                raw, version = versioned_copy(image, directory, ident)
                entry.update(info, version=version)
                entry.update(process_image(raw, target, job["kind"]))
            entry["status"] = "ok"
        except Exception as exc:
            entry["error"] = str(exc)
        entry["seconds"] = round(time.monotonic() - started, 1)
        entry["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        with lock:
            manifest[ident] = entry
            save_json(manifest_path, manifest)
        print(f"[{ident}] " + (f"완료 ({entry['seconds']}초)" if entry["status"] == "ok" else "실패: " + entry.get("error", ""))
              + (" · 경고: " + entry["warning"] if entry.get("warning") else ""))
        return entry["status"] == "ok"
    with ThreadPoolExecutor(max_workers=options.jobs) as executor:
        running = {}
        while pending or running:
            for job in list(pending):
                dependencies = [r for r in job.get("dependencies", []) if r in by_id]
                if all(dep in outcomes for dep in dependencies) and len(running) < options.jobs:
                    running[executor.submit(run, job)] = job["id"]
                    pending.remove(job)
            if not running:
                raise deck.DeckError("에셋 참고 관계가 순환합니다")
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                outcomes[running.pop(future)] = future.result()
    # 부분 생성 뒤에도 기존에 성공한 그림을 모아보기에서 빼지 않는다.
    entries = []
    for ident, entry in manifest.items():
        if not isinstance(entry, dict) or entry.get("status") != "ok" or not ID.fullmatch(ident):
            continue
        if category == "mockup":
            matches = sorted(directory.glob(f"[0-9]*-{ident}.png"))
            target = matches[0] if matches else directory / "missing"
        else:
            target = directory / f"{ident}.png"
        entries.append((ident, target))
    if category == "mockup":
        entries.sort(key=lambda entry: entry[1].name)       # 장 번호 순
    sheet(directory, entries, wide=category == "mockup")
    return 0 if all(outcomes.values()) else 1


# 참고 그림이 무엇인지 Codex에게 알려 주는 말(첨부 순서대로 "Attached image N is …"에 들어간다).
ROLE_CAPTURE = "a browser screenshot of the real slide (the layout, text and typeface to keep; dashed boxes are illustration placeholders)"
ROLE_BOARD = "a kit board showing finished sample slides of this design system (a style reference only, not the layout)"
ROLE_MASTER = "the mascot character sheet (the character to keep identical)"
ROLE_SLIDE = "a finished slide design on which this illustration appears (shows its pose, look and drawing style)"
ROLE_STYLE = "a finished slide design from the same deck (a drawing-style reference only; this illustration is not on it)"
ROLE_RELATED = "a related illustration from the same deck (style and subject reference)"


def mascot_job(folder, plan, assets):
    ident = master_id(plan)
    master = {"id": ident, "kind": "mascot_master", "prompt": (plan.get("mascot") or {}).get("look", "")}
    return {"id": ident, "kind": "mascot_master", "target": folder / "그림" / f"{ident}.png",
            "prompt": asset_prompt(plan, assets, master), "refs": []}


def mockup_jobs(folder, plan, assets, mode, only, captures, shown=None):
    layouts = read_json(assets / "kit" / "layouts.json", {})
    if isinstance(layouts, dict) and isinstance(layouts.get("layouts"), dict):
        layouts = layouts["layouts"]
    master = folder / "그림" / f"{master_id(plan)}.png"
    palette = (plan.get("meta") or {}).get("palette", "")
    board = assets / "kit" / f"board-{palette}.png"
    result = []
    for index, slide in enumerate(plan.get("slides", [])):
        ident = slide["id"]
        if str(slide.get("mockup", "")).startswith("same-as:") or (only and ident not in only):
            continue
        refs = [captures[index], master] if mode == "polish" else [board, master]
        roles = [ROLE_CAPTURE if mode == "polish" else ROLE_BOARD, ROLE_MASTER]
        seen = shown.get(index) if isinstance(shown, dict) else None
        result.append({"id": ident, "kind": "mockup", "target": folder / "시안" / f"{index + 1:02d}-{ident}.png",
                       "prompt": mockup_prompt(plan, assets, slide, layouts, mode, seen), "refs": refs, "roles": roles,
                       "shown": sorted(seen) if seen is not None else None})
    return result


def asset_jobs(folder, plan, assets, only):
    lookup = {a["id"]: a for a in plan.get("assets", []) if a.get("kind") != "mascot_master"}
    masters = {master_id(plan)} | {a["id"] for a in plan.get("assets", []) if a.get("kind") == "mascot_master"}
    selected = set(only) & lookup.keys() if only else set(lookup)
    def expand(ident, visiting):
        if ident in visiting:
            raise deck.DeckError(f"에셋 참조가 순환합니다: {ident}")
        visiting.add(ident)
        asset = lookup[ident]
        refs = asset.get("ref", [])
        if not isinstance(refs, list):
            raise deck.DeckError(f"에셋 ref는 배열이어야 합니다: {ident}")
        for ref in refs:
            safe_id(ref)
            if ref in lookup and (ref in selected or not (folder / "그림" / f"{ref}.png").is_file()):
                selected.add(ref)
                expand(ref, visiting)
            elif ref not in lookup and ref not in masters:
                raise deck.DeckError(f"없는 참조 에셋입니다: {ref}")
        visiting.remove(ident)
    for ident in list(selected):
        expand(ident, set())
    result = []
    for asset in plan.get("assets", []):
        ident = asset["id"]
        if ident not in selected:
            continue
        dependencies = list(asset.get("ref", []))
        refs = {}
        if asset["kind"] == "mascot":
            refs[folder / "그림" / f"{master_id(plan)}.png"] = ROLE_MASTER
        mock, drawn = reference_mockup(folder, plan, ident)
        if mock:
            refs.setdefault(mock, ROLE_SLIDE if drawn else ROLE_STYLE)
        for ref in dependencies:
            refs.setdefault(folder / "그림" / f"{ref}.png", ROLE_MASTER if ref in masters else ROLE_RELATED)
        result.append({"id": ident, "kind": asset["kind"], "target": folder / "그림" / f"{ident}.png",
                       "prompt": asset_prompt(plan, assets, asset), "refs": list(refs), "roles": list(refs.values()),
                       "dependencies": dependencies})
    return result


def sync_master_aliases(folder, plan):
    # 콘티의 원화 에셋 id가 mascot.id와 달라도 원화는 한 번만 생성한다.
    master = master_id(plan)
    aliases = [a["id"] for a in plan.get("assets", []) if a.get("kind") == "mascot_master" and a["id"] != master]
    if not aliases:
        return
    directory = folder / "그림"
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path, {})
    entry = manifest.get(master, {})
    if entry.get("status") != "ok" or not entry.get("version"):
        raise deck.DeckError(f"마스코트 원화가 없습니다: {master}")
    source = directory / "raw" / master / (entry["version"] + ".png")
    for alias in aliases:
        old = manifest.get(alias, {})
        target = directory / f"{alias}.png"
        if old.get("alias_of") == master and old.get("source_version") == entry["version"] and target.is_file():
            continue
        raw, version = versioned_copy(source, directory, alias)
        details = process_image(raw, target, "mascot_master")
        manifest[alias] = {**entry, **details, "version": version, "alias_of": master,
                           "source_version": entry["version"]}
    save_json(manifest_path, manifest)
    sheet(directory, [(ident, directory / f"{ident}.png") for ident, item in manifest.items()
                      if isinstance(item, dict) and item.get("status") == "ok" and ID.fullmatch(ident)])


def import_image(folder, plan, ident, source):
    source = Path(source)
    if not source.is_file():
        raise deck.DeckError(f"가져올 그림이 없습니다: {source}")
    if ident.startswith("mockup:"):
        key = safe_id(ident[7:])
        slides = {s["id"]: (i, s) for i, s in enumerate(plan.get("slides", []), 1)}
        if key not in slides:
            raise deck.DeckError(f"없는 장 id입니다: {key}")
        index, slide = slides[key]
        if str(slide.get("mockup", "")).startswith("same-as:"):
            raise deck.DeckError(f"다른 시안을 쓰는 장에는 직접 가져올 수 없습니다: {key}")
        directory, kind = folder / "시안", "mockup"
        target = directory / f"{index:02d}-{key}.png"
        entries = [(s["id"], directory / f"{i:02d}-{s['id']}.png") for i, s in enumerate(plan.get("slides", []), 1)]
    else:
        key = safe_id(ident)
        asset = next((a for a in plan.get("assets", []) if a["id"] == key), None)
        if key == master_id(plan):
            kind = "mascot_master"
        elif asset:
            kind = asset["kind"]
        else:
            raise deck.DeckError(f"없는 에셋 id입니다: {key}")
        directory, target = folder / "그림", folder / "그림" / f"{key}.png"
        entries = [(master_id(plan), folder / "그림" / f"{master_id(plan)}.png")]
        entries += [(a["id"], folder / "그림" / f"{a['id']}.png") for a in plan.get("assets", [])]
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path, {})
    raw, version = versioned_copy(source, directory, key)
    entry = {"kind": kind, "status": "ok", "version": version, "imported": True,
             "prompt_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
             "updated": datetime.now().astimezone().isoformat(timespec="seconds")}
    entry.update(process_image(raw, target, kind))
    manifest[key] = entry
    save_json(manifest_path, manifest)
    sheet(directory, list(dict(entries).items()), wide=kind == "mockup")
    if kind == "mascot_master" and key == master_id(plan):
        sync_master_aliases(folder, plan)
    print(f"가져옴: {target} ({version})")
    return 0


def art_command(args):
    folder = Path(args.folder).resolve()
    assets = Path(args.assets).resolve()
    plan = plan_data(folder)
    if args.kind == "import":
        if not args.target or not args.file:
            raise deck.DeckError("art import에는 id와 그림 파일이 필요합니다")
        return import_image(folder, plan, args.target, args.file)
    if args.target or args.file:
        raise deck.DeckError("추가 위치 인자는 art import에서만 받습니다")
    if args.jobs < 1:
        raise deck.DeckError("--jobs는 1 이상의 정수여야 합니다")
    only = set(filter(None, (args.only or "").split(",")))
    for ident in only:
        safe_id(ident)
    allowed = ({master_id(plan)} | {s["id"] for s in plan.get("slides", [])} | {a["id"] for a in plan.get("assets", [])})
    unknown = only - allowed
    if unknown:
        raise deck.DeckError("없는 --only id: " + ", ".join(sorted(unknown)))
    failures = 0
    with tempfile.TemporaryDirectory(prefix="deck-art-") as temp:
        job_root = Path(temp)
        if args.kind in ("mascot", "all") and (not only or master_id(plan) in only or args.kind == "all"):
            failures += generate_jobs(folder, "asset", [mascot_job(folder, plan, assets)], args, job_root)
            if failures:
                return 1
            if not args.dry_run:
                sync_master_aliases(folder, plan)
        if args.kind in ("mockups", "all"):
            indexes = [i for i, slide in enumerate(plan.get("slides", []))
                       if not str(slide.get("mockup", "")).startswith("same-as:") and (not only or slide["id"] in only)]
            captures, shown = {}, {}
            if indexes:
                captures = capture(build_temp(folder, assets, job_root), indexes, job_root, shown)
            failures += generate_jobs(folder, "mockup", mockup_jobs(folder, plan, assets, args.mode, only, captures, shown), args, job_root)
            if failures:
                return 1
        if args.kind in ("assets", "all"):
            failures += generate_jobs(folder, "asset", asset_jobs(folder, plan, assets, only), args, job_root)
    return 1 if failures else 0


def local_score(left, right):
    if Image is None:
        return None
    with Image.open(left) as a, Image.open(right) as b:
        a = a.convert("L").resize((64, 36), Image.Resampling.LANCZOS)
        b = b.convert("L").resize((64, 36), Image.Resampling.LANCZOS)
    difference = ImageStat.Stat(ImageChops.difference(a, b)).mean[0] / 255
    edges_a = list(a.filter(ImageFilter.FIND_EDGES).getdata())
    edges_b = list(b.filter(ImageFilter.FIND_EDGES).getdata())
    mean_a, mean_b = sum(edges_a) / len(edges_a), sum(edges_b) / len(edges_b)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(edges_a, edges_b))
    denom = math.sqrt(sum((x - mean_a) ** 2 for x in edges_a) * sum((y - mean_b) ** 2 for y in edges_b))
    corr = covariance / denom if denom else (1.0 if edges_a == edges_b else 0.0)
    return {"overall": round(100 * (1 - difference + max(0.0, corr)) / 2, 1), "verdict": "자체 점수",
            "scores": {"grayscale_similarity": round(100 * (1 - difference), 1), "edge_correlation": round(100 * corr, 1)}}


def compare_picture(left, right, target, label):
    if Image is None:
        script = "ctx.fillStyle='#fff';ctx.fillRect(0,0,1920,540);ctx.fillStyle='#202735';ctx.font='20px sans-serif';ctx.fillText(" + json.dumps(label, ensure_ascii=False) + ",16,27);images.forEach((im,n)=>{let s=Math.min(960/im.width,500/im.height),w=im.width*s,h=im.height*s;ctx.drawImage(im,n*960+(960-w)/2,40+(500-h)/2,w,h)})"
        target.write_bytes(chrome_canvas([left, right], 1920, 540, script))
        return
    canvas = Image.new("RGB", (1920, 540), "white")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/malgun.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    draw.text((16, 8), label, font=font, fill="#202735")
    for index, path in enumerate((left, right)):
        with Image.open(path) as source:
            image = source.convert("RGB")
        image.thumbnail((960, 500), Image.Resampling.LANCZOS)
        canvas.paste(image, (index * 960 + (960 - image.width) // 2, 40 + (500 - image.height) // 2))
    canvas.save(target, "PNG")


def compare_command(args):
    folder = Path(args.folder).resolve()
    plan = plan_data(folder)
    slides = plan.get("slides", [])
    selected = deck.parse_slides(args.slides, len(slides))
    draft = folder / "원고.html"
    title = deck.title_of(deck.Document(deck.read_text(draft)))
    built = folder / (deck.valid_name(title) + ".html")
    if not built.is_file():
        raise deck.DeckError(f"대조할 결과 파일이 없습니다: {built} · {deck.cli_hint('build', folder)}")
    out = folder / "검사"
    out.mkdir(parents=True, exist_ok=True)
    executable = deck.find_impeccable()
    results = []
    with tempfile.TemporaryDirectory(prefix="deck-compare-") as temp:
        captures = capture(built, selected, Path(temp))
        for index in selected:
            slide = slides[index]
            mock = mockup_path(folder, plan, slide)
            if not mock.is_file():
                raise deck.DeckError(f"시안이 없습니다: {mock}")
            if executable:
                diff_dir = out / "diff" / f"{index + 1:02d}"
                diff_dir.mkdir(parents=True, exist_ok=True)
                score = deck.impeccable_json(executable, ["comp-diff", "--comp", mock, "--build", captures[index], "--out-dir", diff_dir, "--json"])
                if not isinstance(score, dict) or "overall" not in score:
                    raise deck.DeckError(f"{index + 1}번 장 comp-diff 결과가 올바르지 않습니다")
                score = {k: score.get(k) for k in ("overall", "verdict", "scores")}
                method = "impeccable"
            else:
                score = local_score(mock, captures[index])
                method = "자체" if score else "점수 없음"
            number = score.get("overall") if score else None
            display = round(number * 100, 1) if number is not None and method == "impeccable" else number
            label = f"{index + 1:02d} · {slide['id']} · " + (f"{display}점" if display is not None else "점수 없음")
            target = out / f"compare-{index + 1:02d}.png"
            compare_picture(mock, captures[index], target, label)
            results.append({"slide": index + 1, "id": slide["id"], "mockup": str(mock), "image": target.name,
                            "method": method, "overall": number, "verdict": score.get("verdict") if score else None,
                            "scores": score.get("scores") if score else None})
    results.sort(key=lambda row: (row["overall"] is None, row["overall"] if row["overall"] is not None else 0))
    save_json(out / "compare.json", {"slides": results})
    review = out / "대조.md"
    if not review.exists():
        lines = ["# 시안 대조", "", "| 장 | id | 구도 | 글자 | 색과 모양 | 그림 | 연출 | 판정 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {i:02d} | {s['id']} |  |  |  |  |  |  |" for i, s in enumerate(slides, 1)]
        lines += ["", "최종: 미판정", ""]
        try:
            with review.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines))
        except FileExistsError:
            pass
    for row in results:
        print(f"{row['slide']:02d} · {row['id']} · {row['overall'] if row['overall'] is not None else '점수 없음'} · {row['method']}")
    print(f"대조 결과: {out}")
    return 0


def register(subparsers):
    art = subparsers.add_parser("art", help="마스코트·시안·그림을 생성하거나 가져옵니다")
    art.add_argument("folder", metavar="덱폴더")
    art.add_argument("kind", choices=("mascot", "mockups", "assets", "all", "import"))
    art.add_argument("target", nargs="?", help="import할 에셋 id 또는 mockup:장id")
    art.add_argument("file", nargs="?", help="import할 그림 파일")
    art.add_argument("--only", metavar="id,...")
    art.add_argument("--force", action="store_true")
    art.add_argument("--jobs", type=int, default=4)
    art.add_argument("--dry-run", action="store_true")
    art.add_argument("--mode", choices=("polish", "compose"), default="polish")
    art.set_defaults(function=art_command)
    compare = subparsers.add_parser("compare", help="시안과 구현을 나란히 대조합니다")
    compare.add_argument("folder", metavar="덱폴더")
    compare.add_argument("--slides", metavar="2,5")
    compare.set_defaults(function=compare_command)
