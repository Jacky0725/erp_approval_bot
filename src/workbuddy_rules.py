"""Versioned, lossless text sections for WorkBuddy's rule-reading tools."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from openpyxl import load_workbook

from excel_exports import atomic_excel_path


PART_LIMIT = 12000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_sections(workbook: bytes) -> list[str]:
    book = load_workbook(io.BytesIO(workbook), read_only=True, data_only=True)
    sections = []
    try:
        for sheet in book:
            heading = f"工作表 {sheet.title}\n行号与单元格值（按原列序，null 为空）\n"
            section = heading
            for number, cells in enumerate(sheet.values, 1):
                cells = list(cells)
                while cells and cells[-1] is None:
                    cells.pop()
                if not cells:
                    continue
                row = f"{number} " + json.dumps(cells, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
                if len((heading + row).encode("utf-8")) > PART_LIMIT:
                    raise ValueError(f"规则 {sheet.title} 第 {number} 行超出单段长度。")
                if len((section + row).encode("utf-8")) > PART_LIMIT:
                    sections.append(section)
                    section = heading
                section += row
            sections.append(section)
    finally:
        book.close()
    return sections


def publish_compact(cache: Path, workbook: bytes) -> Path:
    source = sha256(workbook)
    sections = build_sections(workbook)
    path = cache / f"{source}.json"
    cache.mkdir(parents=True, exist_ok=True)
    with atomic_excel_path(path) as staged:
        staged.write_bytes(encode({"source_sha256": source, "sections": sections, "sections_sha256": sha256(encode(sections))}))
    return path


def copy_compact(cache: Path, batch: Path, source: str) -> str:
    bundle = json.loads((cache / f"{source}.json").read_bytes())
    if not isinstance(bundle, dict):
        raise ValueError("紧凑规则缓存格式无效。")
    sections = bundle.get("sections")
    if (bundle.get("source_sha256") != source or not isinstance(sections, list) or not sections
            or any(not isinstance(part, str) or not part or len(part.encode("utf-8")) > PART_LIMIT for part in sections)
            or sha256(encode(sections)) != bundle.get("sections_sha256")):
        raise ValueError("紧凑规则缓存摘要不匹配。")
    target = batch / "rules_compact"
    target.mkdir()
    entries = {}
    for number, part in enumerate(sections, 1):
        name = f"part-{number:03d}.txt"
        data = part.encode("utf-8")
        (target / name).write_bytes(data)
        entries[name] = sha256(data)
    manifest = encode({"source_sha256": source, "parts": entries})
    (target / "manifest.json").write_bytes(manifest)
    return sha256(manifest)


def verify_compact(batch: Path, source: str, manifest_sha: str) -> None:
    target = batch / "rules_compact"
    manifest = (target / "manifest.json").read_bytes()
    if sha256(manifest) != manifest_sha:
        raise ValueError("紧凑规则清单摘要不匹配。")
    data = json.loads(manifest)
    if data["source_sha256"] != source:
        raise ValueError("紧凑规则与批次源版本不匹配。")
    for name, expected in data["parts"].items():
        if sha256((target / name).read_bytes()) != expected:
            raise ValueError(f"紧凑规则分段摘要不匹配：{name}")
