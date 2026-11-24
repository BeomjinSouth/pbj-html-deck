#!/usr/bin/env python3
"""견본을 네 색 묶음과 두 테마로 묶어 부품·엔진 자체를 점검한다.

기존 견본은 읽기만 한다. 실제 덱과 같은 DESIGN.md·PRODUCT.md를 넣고 detect 원문을
모두 기록한다. 이 목록은 유지보수용 비교 자료이며 check 경고를 숨기는 데 쓰지 않는다.
"""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / "scripts"))
import deck  # noqa: E402


def component_hashes():
    """다른 작업자의 수정이 검사 도중 섞이지 않도록 구성 파일을 확인한다."""
    paths = [SKILL / "assets/kit/kit.css"]
    paths.extend((SKILL / "assets/engine").glob("*.css"))
    paths.extend((SKILL / "assets/engine").glob("*.js"))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path, help="읽기 전용 견본 덱")
    parser.add_argument("--work", required=True, type=Path, help="스크래치 폴더")
    parser.add_argument("--out", type=Path, required=True, help="유지보수용 점검 JSON 경로")
    parser.add_argument("--impeccable", type=Path, default=Path.home() / ".impeccable/bin/0.1.18/impeccable.exe")
    args = parser.parse_args()
    sample, work, output = args.sample.resolve(), args.work.resolve(), args.out.resolve()
    if not args.impeccable.is_file():
        parser.error("임페커블 실행 파일이 없습니다. 설치하거나 업데이트하지 마세요.")
    if sample == work or sample in work.parents or sample == output or sample in output.parents:
        parser.error("스크래치와 출력은 견본 덱 밖이어야 합니다.")
    work.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="imp-baseline-", dir=work))
    source = (sample / "원고.html").read_text(encoding="utf-8-sig")
    shutil.copytree(sample / "그림", run / "그림")
    doc = deck.Document(source)
    root = next((node for node in doc.nodes if node.tag == "html"), None)
    if root is None:
        parser.error("견본 원고에 html 태그가 없습니다.")
    palettes = json.loads((SKILL / "assets/kit/palettes.json").read_text(encoding="utf-8"))
    palette_names = [name for name in palettes if not name.startswith("_")]
    if len(palette_names) != 4:
        parser.error("색 묶음이 네 개가 아닙니다. 등록부를 확인하세요.")
    hashes = component_hashes()
    union = collections.defaultdict(set)
    occurrences = collections.Counter()
    variants = {}
    meta = json.loads(deck.read_text(sample / "콘티.json")).get("meta", {}) if (sample / "콘티.json").is_file() else {}
    title = deck.title_of(doc)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               TEMP=str(run), TMP=str(run), TMPDIR=str(run))
    for palette in palette_names:
        for theme in ("light", "dark"):
            stem = f"{palette}-{theme}"
            draft = run / f"원고-{stem}.html"
            context = run / stem
            context.mkdir()
            for name in ("DESIGN.md", "PRODUCT.md"):
                existing = sample / name
                template = existing if existing.is_file() else SKILL / "assets" / "impeccable" / name
                contents = deck.read_text(template).replace("{{title}}", title).replace("{{kind}}", meta.get("kind", "lesson"))
                contents = contents.replace("{{palette}}", palette)
                deck.write_text(context / name, contents)
            target = context / f"결과-{stem}.html"
            tag = deck.edit_tag(doc.start_tag(root), {"data-palette": palette, "data-theme": theme})
            draft.write_text(source[:root.start] + tag + source[root.start_end:], encoding="utf-8")
            command = [sys.executable, "-B", str(SKILL / "scripts/deck.py"), "build", str(draft), "-o", str(target)]
            built = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env, timeout=180)
            if built.returncode:
                raise RuntimeError(f"{stem} 묶기 실패: {built.stderr or built.stdout}")
            found = subprocess.run([str(args.impeccable), "detect", "--json", str(target)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                                   timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if found.returncode not in (0, 2):
                raise RuntimeError(f"{stem} detect 실패({found.returncode}): {found.stderr}")
            findings = json.loads(found.stdout)
            if not isinstance(findings, list):
                raise ValueError(f"{stem} detect 결과가 배열이 아닙니다")
            variants[stem] = findings
            for item in findings:
                if not isinstance(item, dict):
                    continue
                rule = item.get("antipattern") or item.get("rule")
                snippet = item.get("snippet")
                if isinstance(rule, str):
                    union[rule].add(snippet if isinstance(snippet, str) else "")
                    occurrences[rule] += 1
            print(f"{stem}: {len(findings)}건")
    if component_hashes() != hashes:
        raise RuntimeError("검사 도중 부품 또는 엔진이 바뀌었습니다. 다시 실행하세요.")
    rules = {rule: sorted(snippets) for rule, snippets in sorted(union.items())}
    for rule, snippets in rules.items():
        print(f"\n{rule}: 서로 다른 원문 {len(snippets)}개, 전체 {occurrences[rule]}건")
        for snippet in snippets:
            print("  " + snippet)
    note = "부품·엔진 유지보수용 점검 기록. check의 경고 필터로 쓰지 않음."
    deck.write_text(output, json.dumps({"_note": note, "rules": rules, "variants": variants}, ensure_ascii=False, indent=2) + "\n")
    print(f"\n부품 점검 기록: {output}\n검사 작업: {run}")


if __name__ == "__main__":
    main()
