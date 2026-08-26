#!/usr/bin/env python3
"""Build deterministic, local-only semantic bookmarks for the AI-related folder."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
import subprocess
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET


SCHEMA_VERSION = "0.1"
CHUNK_CHARS = 3000
MAX_DOCUMENT_CHARS = 600_000

PLAIN_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".py", ".js",
    ".mjs", ".cjs", ".ts", ".mts", ".bat", ".ps1", ".gs", ".ini",
    ".toml", ".log", ".lock", ".env",
}
HTML_EXTENSIONS = {".html", ".htm"}
OFFICE_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico"}
MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".mov", ".m4v"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz"}

GENERATED_COMPONENTS = {".next", "node_modules", "__pycache__", ".obsidian", ".cursor"}
GENERATED_SUFFIXES = {".pyc", ".map", ".css", ".ダウンロード"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "settings.local.json"}
GENERATED_NAMES = {".DS_Store", "desktop.ini"}
SENSITIVE_PARTS = ("credential", "secret", "api_key", "apikey", "token")

THEMES: dict[str, tuple[str, ...]] = {
    "AIエージェント": ("AIエージェント", "AI agent", "agentic", "エージェント"),
    "Claude": ("Claude", "Anthropic", "CLAUDE.md"),
    "ChatGPT": ("ChatGPT", "GPTs", "OpenAI"),
    "Codex": ("Codex",),
    "Gemini・Gemma": ("Gemini", "Gemma", "Ollama"),
    "RAG・意味検索": ("RAG", "意味検索", "semantic search", "ベクトル検索", "検索インデックス"),
    "グラフエンジニアリング": ("グラフエンジニアリング", "Evidence Graph", "node", "edge", "ノード", "エッジ"),
    "Instagram": ("Instagram", "インスタ", "フィード投稿", "カルーセル"),
    "Canva・デザイン": ("Canva", "デザイン", "スライド", "プレゼン"),
    "画像生成": ("画像生成", "ImageGen", "DALL", "Midjourney"),
    "音声・文字起こし": ("Whisper", "文字起こし", "音声入力", "transcription"),
    "動画": ("動画", "YouTube", "video"),
    "自動化・GAS": ("自動化", "Google Apps Script", "GAS", "workflow"),
    "MCP・ツール連携": ("MCP", "Model Context Protocol", "ツール連携"),
    "Obsidian・知識管理": ("Obsidian", "知識管理", "第二の脳", "記憶"),
    "リサーチ": ("リサーチ", "research", "調査"),
    "ビジネス・売上": ("ビジネス", "売上", "顧客", "クライアント", "サービス"),
    "プロンプト設計": ("プロンプト", "prompt", "指示書"),
    "ソフトウェア開発": ("Python", "JavaScript", "TypeScript", "ソフトウェア", "アプリ", "開発"),
}


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def stable_id(prefix: str, value: object, length: int = 32) -> str:
    return f"{prefix}_{hashlib.sha256(canonical(value)).hexdigest()[:length]}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\x00", "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    value = "\n".join(lines)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp932"):
        try:
            return clean_text(data.decode(encoding))
        except UnicodeDecodeError:
            pass
    return clean_text(data.decode("utf-8", errors="replace"))


def chunks(value: str) -> tuple[list[str], bool]:
    truncated = len(value) > MAX_DOCUMENT_CHARS
    value = value[:MAX_DOCUMENT_CHARS]
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", value) if part.strip()]
    output: list[str] = []
    current = ""
    for paragraph in paragraphs:
        parts = [paragraph[i:i + CHUNK_CHARS] for i in range(0, len(paragraph), CHUNK_CHARS)] or [""]
        for part in parts:
            candidate = part if not current else current + "\n\n" + part
            if current and len(candidate) > CHUNK_CHARS:
                output.append(current)
                current = part
            else:
                current = candidate
    if current:
        output.append(current)
    return output, truncated


class VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag.lower() in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        elif tag.lower() in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def extract_html(path: Path) -> tuple[str, dict]:
    parser = VisibleHTML()
    parser.feed(read_text(path))
    text = clean_text(html.unescape("".join(parser.parts)))
    return text, {"visible_character_count": len(text)}


def xml_paragraphs(data: bytes) -> list[str]:
    root = ET.fromstring(data)
    result: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}p"):
            text = "".join(node.text or "" for node in element.iter() if node.tag.endswith("}t"))
            if text.strip():
                result.append(text.strip())
    return result


def extract_docx(path: Path) -> tuple[list[tuple[dict, str]], dict]:
    with zipfile.ZipFile(path) as archive:
        text = clean_text("\n\n".join(xml_paragraphs(archive.read("word/document.xml"))))
    parts, truncated = chunks(text)
    return [({"chunk": i}, part) for i, part in enumerate(parts, 1)], {
        "character_count": len(text), "truncated": truncated,
    }


def extract_pptx(path: Path) -> tuple[list[tuple[dict, str]], dict]:
    units: list[tuple[dict, str]] = []
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        names.sort(key=lambda n: int(re.search(r"\d+", Path(n).stem).group()))
        for name in names:
            number = int(re.search(r"\d+", Path(name).stem).group())
            text = clean_text("\n".join(xml_paragraphs(archive.read(name))))
            if text:
                units.append(({"slide": number}, text))
    return units, {"slide_count": len(names), "text_slide_count": len(units), "truncated": False}


def extract_xlsx(path: Path) -> tuple[list[tuple[dict, str]], dict]:
    """Extract cell text from modern Excel files without external packages."""
    units: list[tuple[dict, str]] = []
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter():
                if item.tag.endswith("}si"):
                    shared.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
        sheets = [name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)]
        sheets.sort(key=lambda name: int(re.search(r"\d+", Path(name).stem).group()))
        for sheet_name in sheets:
            number = int(re.search(r"\d+", Path(sheet_name).stem).group())
            root = ET.fromstring(archive.read(sheet_name))
            cells: list[str] = []
            for cell in (node for node in root.iter() if node.tag.endswith("}c")):
                address = cell.attrib.get("r", "?")
                kind = cell.attrib.get("t", "")
                value_node = next((node for node in cell if node.tag.endswith("}v")), None)
                inline = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
                value = inline
                if value_node is not None and value_node.text is not None:
                    value = value_node.text
                    if kind == "s":
                        try:
                            value = shared[int(value)]
                        except (ValueError, IndexError):
                            pass
                if value.strip():
                    cells.append(f"{address}: {value.strip()}")
            if cells:
                text = clean_text("\n".join(cells))
                parts, _ = chunks(text)
                units.extend((({"sheet": number, "chunk": index}, part)) for index, part in enumerate(parts, 1))
    return units, {"sheet_count": len(sheets), "text_unit_count": len(units), "truncated": False}


def extract_pdf(path: Path) -> tuple[list[tuple[dict, str]], dict]:
    binary = shutil.which("pdftotext")
    if binary:
        command = [binary, "-layout", "-enc", "UTF-8", str(path), "-"]
        method = "pdftotext-layout"
    else:
        command = ["/usr/bin/mdls", "-raw", "-name", "kMDItemTextContent", str(path)]
        method = "spotlight-text-fallback"
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace").strip())
    extracted = process.stdout.decode("utf-8", errors="replace")
    if method == "spotlight-text-fallback" and extracted.strip() in {"(null)", "null"}:
        raise RuntimeError("PDF本文抽出にpdftotextが必要です")
    pages = extracted.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    units = [({"page": i}, clean_text(page)) for i, page in enumerate(pages, 1) if clean_text(page)]
    return units, {"page_count": len(pages), "text_page_count": len(units), "truncated": False, "method": method}


def classify(relative_path: str) -> tuple[str, str, str]:
    path = Path(relative_path)
    suffix = path.suffix.lower()
    components = set(path.parts)
    lowered_name = path.name.lower()
    if path.name in SENSITIVE_NAMES or any(part in lowered_name for part in SENSITIVE_PARTS):
        return "sensitive_excluded", "sensitive_name_pattern", "none"
    if components & GENERATED_COMPONENTS or any(part.endswith("_files") for part in path.parts):
        return "generated_excluded", "generated_or_application_support_path", "none"
    if suffix in GENERATED_SUFFIXES or path.name in GENERATED_NAMES:
        return "generated_excluded", "generated_or_binary_support_file", "none"
    if suffix in IMAGE_EXTENSIONS:
        return "metadata_only", "image_title_only_by_policy", "filename"
    if suffix in MEDIA_EXTENSIONS:
        return "metadata_only", "media_transcription_disabled_by_policy", "filename"
    if suffix in ARCHIVE_EXTENSIONS:
        return "metadata_only", "archive_not_expanded_by_policy", "filename"
    if suffix in OFFICE_EXTENSIONS:
        return "extractable", "supported_office_format", suffix.lstrip(".")
    if suffix in HTML_EXTENSIONS:
        return "extractable", "visible_html_text", "html-visible-text"
    if suffix in PLAIN_EXTENSIONS:
        return "extractable", "supported_text_format", "direct-text"
    if not suffix:
        return "extractable", "extensionless_text_candidate", "direct-text"
    return "metadata_only", "unsupported_or_binary_format", "filename"


def extract(path: Path, method: str) -> tuple[list[tuple[dict, str]], dict]:
    if method == "pdf":
        return extract_pdf(path)
    if method == "docx":
        return extract_docx(path)
    if method == "pptx":
        return extract_pptx(path)
    if method == "xlsx":
        return extract_xlsx(path)
    if method == "html-visible-text":
        text, metadata = extract_html(path)
    else:
        text, metadata = read_text(path), {}
    parts, truncated = chunks(text)
    metadata.update({"character_count": len(text), "truncated": truncated})
    return [({"chunk": i}, part) for i, part in enumerate(parts, 1)], metadata


def theme_hits(text: str) -> list[tuple[str, list[str]]]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    output = []
    for theme, terms in THEMES.items():
        matched = sorted({term for term in terms if unicodedata.normalize("NFKC", term).casefold() in folded})
        if matched:
            output.append((theme, matched))
    return output


DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})[-/.年](0?[1-9]|1[0-2])(?:[-/.月](0?[1-9]|[12]\d|3[01])日?)?"),
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)"),
)


def dates_in(text: str) -> list[str]:
    values: set[str] = set()
    for index, pattern in enumerate(DATE_PATTERNS):
        for match in pattern.finditer(text):
            year, month, day = match.groups()
            if day:
                try:
                    values.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
                except ValueError:
                    pass
            elif index == 0:
                values.add(f"{int(year):04d}-{int(month):02d}")
    return sorted(values)[:50]


def write_jsonl(path: Path, records: list[dict]) -> None:
    data = b"".join(canonical(record) + b"\n" for record in records)
    path.write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    inventory_path = Path(args.inventory).resolve(strict=True)
    source_root = Path(args.source_root).resolve(strict=True)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    inventory = [json.loads(line) for line in inventory_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    files = sorted((item for item in inventory if item["kind"] == "file"), key=lambda x: unicodedata.normalize("NFC", x["relative_path"]))
    documents: list[dict] = []
    evidence: list[dict] = []
    relations: list[dict] = []
    counts: Counter[str] = Counter()
    errors: list[str] = []
    theme_nodes: dict[str, str] = {theme: stable_id("theme", theme) for theme in THEMES}
    date_nodes: dict[str, str] = {}
    project_nodes: dict[str, str] = {}

    for item in files:
        relative = item["relative_path"]
        path = source_root / relative
        doc_id = stable_id("doc", {"relative_path": relative, "sha256": item["sha256"]})
        classification, reason, method = classify(relative)
        top = relative.split("/", 1)[0]
        project_id = project_nodes.setdefault(top, stable_id("project", top))
        doc_evidence: list[str] = []
        metadata: dict = {}
        status = classification
        current_error = None
        if not path.is_file() or path.is_symlink() or path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
            status, current_error = "source_changed", "source_inventory_binding_failed"
            errors.append(relative)
        elif classification == "extractable":
            try:
                units, metadata = extract(path, method)
                for ordinal, (locator, observed_text) in enumerate(units, 1):
                    if not observed_text.strip():
                        continue
                    ev_id = stable_id("ev", {"document_id": doc_id, "locator": locator, "observed_text": observed_text})
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "evidence_id": ev_id,
                        "document_id": doc_id,
                        "ordinal": ordinal,
                        "locator": locator,
                        "observed_text": observed_text,
                        "source": {"relative_path": relative, "sha256": item["sha256"]},
                        "extraction_method": method,
                        "status": "observed",
                    }
                    evidence.append(record)
                    doc_evidence.append(ev_id)
                    relations.append({
                        "relation_id": stable_id("rel", {"type": "has_evidence", "from": doc_id, "to": ev_id}),
                        "relation_type": "has_evidence", "from_id": doc_id, "to_id": ev_id,
                        "status": "verified", "basis": "deterministic_extraction",
                    })
                    bookmark_text = relative + "\n" + observed_text
                    for theme, matched in theme_hits(bookmark_text):
                        relations.append({
                            "relation_id": stable_id("rel", {"type": "mentions_theme", "from": ev_id, "to": theme_nodes[theme]}),
                            "relation_type": "mentions_theme", "from_id": ev_id, "to_id": theme_nodes[theme],
                            "status": "verified_lexical_match", "basis": {"matched_terms": matched},
                        })
                    for date in dates_in(bookmark_text):
                        date_id = date_nodes.setdefault(date, stable_id("date", date))
                        relations.append({
                            "relation_id": stable_id("rel", {"type": "mentions_date", "from": ev_id, "to": date_id}),
                            "relation_type": "mentions_date", "from_id": ev_id, "to_id": date_id,
                            "status": "verified_lexical_match", "basis": {"normalized_date": date},
                        })
                status = "extracted" if doc_evidence else "empty_after_extraction"
            except Exception as exc:  # fail closed and preserve the file in coverage
                status, current_error = "extraction_failed", f"{type(exc).__name__}:{exc}"
                errors.append(relative)
        documents.append({
            "schema_version": SCHEMA_VERSION,
            "document_id": doc_id,
            "source": {
                "relative_path": relative, "absolute_path": str(path), "sha256": item["sha256"],
                "size_bytes": item["size_bytes"], "file_type": Path(relative).suffix.lower().lstrip(".") or "no_extension",
            },
            "classification": classification, "classification_reason": reason,
            "project_id": project_id, "extraction_method": method, "status": status,
            "evidence_ids": doc_evidence, "extraction_metadata": metadata, "error": current_error,
        })
        relations.append({
            "relation_id": stable_id("rel", {"type": "member_of_project", "from": doc_id, "to": project_id}),
            "relation_type": "member_of_project", "from_id": doc_id, "to_id": project_id,
            "status": "verified", "basis": "top_level_path_component",
        })
        counts[status] += 1

    nodes = (
        [{"node_id": value, "node_type": "project", "label": label} for label, value in sorted(project_nodes.items())]
        + [{"node_id": value, "node_type": "theme", "label": label} for label, value in sorted(theme_nodes.items())]
        + [{"node_id": value, "node_type": "date", "label": label} for label, value in sorted(date_nodes.items())]
    )
    documents_path = output_dir / "semantic-documents.jsonl"
    evidence_path = output_dir / "semantic-evidence.jsonl"
    relations_path = output_dir / "semantic-relations.jsonl"
    nodes_path = output_dir / "semantic-nodes.jsonl"
    write_jsonl(documents_path, documents)
    write_jsonl(evidence_path, evidence)
    write_jsonl(relations_path, relations)
    write_jsonl(nodes_path, nodes)
    coverage = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_root": str(source_root),
        "source_inventory": str(inventory_path),
        "source_inventory_sha256": sha256_file(inventory_path),
        "file_count": len(files), "document_count": len(documents), "evidence_count": len(evidence),
        "relation_count": len(relations), "semantic_node_count": len(nodes),
        "status_counts": dict(sorted(counts.items())), "error_paths": errors,
        "policy": {
            "external_network_used": False, "llm_used_for_extraction": False,
            "images": "filename_and_metadata_only", "media": "filename_and_metadata_only",
            "archives": "not_expanded", "generated_assets": "excluded_with_reason",
            "sensitive_name_patterns": "excluded_with_reason",
        },
        "outputs": {
            "documents_sha256": sha256_file(documents_path), "evidence_sha256": sha256_file(evidence_path),
            "relations_sha256": sha256_file(relations_path), "nodes_sha256": sha256_file(nodes_path),
        },
    }
    (output_dir / "semantic-coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    theme_counts = Counter()
    for relation in relations:
        if relation["relation_type"] == "mentions_theme":
            theme_counts[next(label for label, node_id in theme_nodes.items() if node_id == relation["to_id"])] += 1
    summary = [
        "# AI関連フォルダ 意味しおり v0.1", "",
        f"- 対象ファイル: {len(files)}", f"- 本文Evidence: {len(evidence)}",
        f"- 関係: {len(relations)}", f"- 意味ノード: {len(nodes)}", "",
        "## 処理状態", "",
    ]
    summary += [f"- {key}: {value}" for key, value in sorted(counts.items())]
    summary += ["", "## 主なテーマ（Evidence件数）", ""]
    summary += [f"- {key}: {value}" for key, value in theme_counts.most_common()]
    summary += ["", "## 安全方針", "", "- 外部通信なし、LLM抽出なし", "- 画像・音声・動画は名前とメタデータだけ", "- ZIPは展開しない", "- Web保存部品・生成物・機密名パターンは理由付き除外", "- テーマ関係は語句一致のみ。意味推測や因果推定はしていない", ""]
    (output_dir / "semantic-summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps(coverage, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
