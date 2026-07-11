"""Translate textual image captions in an already translated EPUB artifact."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv

from app.domain.translation_qa_service import audit_translated_epub_output
from app.engine.chunk_extractor import BLOCK_TAGS, is_image_caption_block
from app.engine.cleaners.semantics_translator import SemanticsTranslator


@dataclass
class _CaptionTarget:
    member_name: str
    block: Tag
    source_html: str

    @property
    def source_text(self) -> str:
        return self.block.get_text(" ", strip=True)


def _leaf_caption_blocks(soup: BeautifulSoup) -> list[Tag]:
    return [
        block
        for block in soup.find_all(BLOCK_TAGS)
        if isinstance(block, Tag)
        and block.find(BLOCK_TAGS) is None
        and is_image_caption_block(block)
    ]


def _replace_inner_html(block: Tag, translated_html: str) -> None:
    fragment = BeautifulSoup(translated_html or "", "html.parser")
    new_tag = fragment.find(BLOCK_TAGS) or fragment.find()
    contents = (
        list(new_tag.contents)
        if new_tag is not None and new_tag.name == block.name
        else list(fragment.contents)
    )
    block.clear()
    block.extend(contents)


def _replace_text_preserving_markup(block: Tag, translated_text: str) -> None:
    """Replace caption prose while retaining spans, page anchors, and other markup."""
    text_nodes = [
        node
        for node in block.descendants
        if isinstance(node, NavigableString) and str(node).strip()
    ]
    if not text_nodes:
        block.insert(0, translated_text)
        return
    text_nodes[0].replace_with(translated_text)
    for node in text_nodes[1:]:
        node.replace_with("")


def _load_translations(path: str | Path, targets: list[_CaptionTarget]) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = (
        payload.get("translations", payload.get("captions"))
        if isinstance(payload, dict)
        else payload
    )
    if not isinstance(rows, list):
        raise ValueError(
            "caption translation JSON must be a list or contain translations/captions"
        )
    if rows and all(isinstance(row, str) for row in rows):
        translations = [row.strip() for row in rows]
        if len(translations) != len(targets):
            raise ValueError(
                f"caption translation list has {len(translations)} rows; "
                f"expected {len(targets)}"
            )
        if any(not row for row in translations):
            raise ValueError("caption translation list contains an empty row")
        return translations

    translations: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"caption translation row {index} must be an object")
        source_text = str(row.get("source_text") or "").strip()
        translated_text = str(row.get("translated_text") or "").strip()
        if not source_text or not translated_text:
            raise ValueError(f"caption translation row {index} is incomplete")
        if source_text in translations and translations[source_text] != translated_text:
            raise ValueError(f"conflicting translations for caption: {source_text[:80]}")
        translations[source_text] = translated_text
    missing = [target for target in targets if target.source_text not in translations]
    if missing:
        raise RuntimeError(
            f"caption translation map is missing {len(missing)} blocks: "
            f"{json.dumps([target.source_text[:180] for target in missing[:5]], ensure_ascii=False)}"
        )
    return [translations[target.source_text] for target in targets]


def _write_epub(
    source_path: Path,
    output_path: Path,
    modified_members: dict[str, bytes],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=".epub",
        dir=str(output_path.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(source_path) as source_zip:
            infos = source_zip.infolist()
            mimetype = next((info for info in infos if info.filename == "mimetype"), None)
            with zipfile.ZipFile(tmp_path, "w") as output_zip:
                output_zip.comment = source_zip.comment
                if mimetype is not None:
                    mime_data = modified_members.get("mimetype", source_zip.read("mimetype"))
                    mime_info = zipfile.ZipInfo("mimetype", date_time=mimetype.date_time)
                    mime_info.compress_type = zipfile.ZIP_STORED
                    mime_info.external_attr = mimetype.external_attr
                    output_zip.writestr(mime_info, mime_data)
                for info in infos:
                    if info.filename == "mimetype":
                        continue
                    output_zip.writestr(
                        info,
                        modified_members.get(info.filename, source_zip.read(info.filename)),
                    )
        os.replace(tmp_path, output_path)
    finally:
        tmp_path.unlink(missing_ok=True)


async def repair_image_captions(
    source_path: str | Path,
    output_path: str | Path,
    *,
    target_lang: str = "zh-CN",
    model: str | None = None,
    translations_json: str | Path | None = None,
) -> dict:
    """Translate caption text only, then fail closed if the resulting EPUB does not pass QA."""
    source = Path(source_path)
    output = Path(output_path)
    if not source.is_file():
        raise FileNotFoundError(source)

    soups: dict[str, BeautifulSoup] = {}
    targets: list[_CaptionTarget] = []
    with zipfile.ZipFile(source) as source_zip:
        for member_name in source_zip.namelist():
            if not member_name.lower().endswith((".html", ".xhtml")):
                continue
            soup = BeautifulSoup(
                source_zip.read(member_name).decode("utf-8", errors="replace"),
                "html.parser",
            )
            blocks = _leaf_caption_blocks(soup)
            if not blocks:
                continue
            soups[member_name] = soup
            targets.extend(
                _CaptionTarget(member_name, block, str(block))
                for block in blocks
            )

    if not targets:
        raise RuntimeError("no textual image captions found")

    if translations_json is not None:
        translations = _load_translations(translations_json, targets)
        for target, translated_text in zip(targets, translations):
            _replace_text_preserving_markup(target.block, translated_text)
        translation_stats = {
            "mode": "local_translation_map",
            "translated_chunks": len(targets),
            "translation_rows": len(translations),
        }
    else:
        translator = SemanticsTranslator(
            target_lang=target_lang,
            bilingual=False,
            model=model,
        )
        results = await translator.translate_many_chunks_async(
            [target.source_html for target in targets],
            progress_label="图片说明补译",
        )
        failures = [
            {
                "member": target.member_name,
                "error": result.error,
                "source": target.source_text[:180],
            }
            for target, result in zip(targets, results)
            if result.error
        ]
        if failures:
            raise RuntimeError(
                f"image caption translation failed for {len(failures)} blocks: "
                f"{json.dumps(failures[:5], ensure_ascii=False)}"
            )

        for target, result in zip(targets, results):
            _replace_inner_html(target.block, result.translated_html)
        translation_stats = translator.stats.to_dict()

    modified_members = {
        member_name: str(soup).encode("utf-8")
        for member_name, soup in soups.items()
    }
    _write_epub(source, output, modified_members)

    audit = audit_translated_epub_output(output, target_lang=target_lang)
    if audit.get("status") != "passed":
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "caption-repaired EPUB failed artifact QA: "
            f"{json.dumps(audit, ensure_ascii=False)}"
        )

    return {
        "status": "passed",
        "source": str(source),
        "output": str(output),
        "caption_blocks": len(targets),
        "modified_html_files": len(modified_members),
        "translation_stats": translation_stats,
        "artifact_audit": audit,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--target-lang", default="zh-CN")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument(
        "--translations-json",
        help="apply a local source_text/translated_text mapping instead of calling a model API",
    )
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    report = asyncio.run(
        repair_image_captions(
            args.source,
            args.output,
            target_lang=args.target_lang,
            model=args.model,
            translations_json=args.translations_json,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
