"""Fail-closed deterministic rules for image-based PDF questions.

The module deliberately has no validation labels or prior-output input.  A
question first has to match one complete grammar.  The selected source PDF is
then bound by project/report identity, every page is covered by either a
hash-verified question-independent materialization or a deterministic 200-DPI
render, and only source pixels/OCR are used to construct the answer.

Visual extraction is kept question-independent: page OCR words, neutral inline
marker regions, table separators, cells, and ink colours are observed before
question bindings select a page, table, or column.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence


if TYPE_CHECKING:
    from structured_candidate import StructuredCandidateDecision


PDF_VISUAL_RULE_VERSION = "0.2"

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_DEFAULT_ARTIFACT_ROOT = _ROOT / "artifacts"

_MAX_PDF_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_PAGES = 250
_MAX_PIXELS = 50_000_000
_OCR_TIMEOUT_SECONDS = 45

_MARKER_QUESTION = re.compile(
    r"^(?P<location>.+?)の(?P<report_kind>[^,、。]*?報告(?:書)?)"
    r"における、?(?P<page_title>[^,、。]+?)のページで、"
    r"マーカーされている(?P<target>単語)を"
    r"(?P<quantifier>すべて)抜き出してください。?$"
)

_TABLE_EXTREME_QUESTION = re.compile(
    r"^(?P<location>.+?)の(?P<report_kind>[^,、。]*?報告書)にて、"
    r"(?P<metric>[^,、。]+?)が"
    r"(?P<extremum>最も高い|最も低い)とされている"
    r"(?P<target>[^,、。]+?)を抜き出してください。?$"
)

_MEETING_SECTION_PAGE_QUESTION = re.compile(
    r"^(?P<location>.+?)の会議ID\s*[:：]\s*"
    r"(?P<meeting_id>[A-Za-zＡ-Ｚａ-ｚ0-9０-９_-]+)の"
    r"(?P<report_kind>会議録)にて、"
    r"(?P<section>[^,、。]+?)が記載されている"
    r"(?P<target>ページ番号)を答えてください。?$"
)

_PHASE_EFFORT_SUM_QUESTION = re.compile(
    r"^(?P<location>.+?)の(?P<report_kind>最終報告)PDFにおいて、将来の"
    r"(?P<phase_left>フェーズ[A-Za-zＡ-Ｚａ-ｚ0-9０-９]+)と"
    r"(?P<phase_right>フェーズ[A-Za-zＡ-Ｚａ-ｚ0-9０-９]+)"
    r"を実施した場合の(?P<metric>想定工数)は"
    r"(?P<aggregate>合計)で何(?P<unit>時間)ですか。?$"
)

_MARKER_OPERATORS = (
    "retrieve",
    "bind_unique_source",
    "enumerate_pages",
    "verify_or_render",
    "ocr",
    "match_page_title",
    "select_unique_page",
    "detect_inline_markers",
    "align_words",
    "verify_complete",
    "project",
    "list",
)

_TABLE_OPERATORS = (
    "retrieve",
    "bind_unique_source",
    "enumerate_pages",
    "verify_or_render",
    "ocr",
    "detect_table",
    "match_headers",
    "extract_cells",
    "parse_ordinal",
    "argextreme_all",
    "verify_unique",
    "project",
)

_MEETING_SECTION_PAGE_OPERATORS = (
    "retrieve",
    "enumerate_candidate_documents",
    "enumerate_pages",
    "verify_or_render",
    "ocr",
    "bind_meeting_id_header",
    "verify_ocr_run_agreement",
    "match_section_heading",
    "verify_unique_page",
    "bind_page_to_source",
    "project",
    "page_number",
)

_PHASE_EFFORT_SUM_OPERATORS = (
    "retrieve",
    "bind_unique_source",
    "enumerate_pages",
    "verify_or_render",
    "ocr",
    "match_phase_headers",
    "associate_effort_ranges_spatially",
    "verify_ocr_run_agreement",
    "sum_range_endpoints",
    "bind_page_to_source",
    "project",
)


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", _normalized(value))


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _graph_contract(
    question: str,
    match: re.Match[str],
    rule_id: str,
    operators: Sequence[str],
    output_shape: Mapping[str, Any],
) -> dict[str, Any]:
    bindings = {
        key: value
        for key, value in sorted(match.groupdict().items())
        if value is not None
    }
    nodes: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output_ref = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output_ref,
            }
        )
        previous = output_ref
    scope: dict[str, Any] = {
        "location": bindings["location"],
        "container": "*.pdf",
        "report_kind": bindings["report_kind"],
    }
    if rule_id == "pdf_page_inline_marker_word_projection":
        scope.update(
            {
                "page_title": bindings["page_title"],
                "style_channel": "raster_inline_background_marker",
                "target": bindings["target"],
            }
        )
    elif rule_id == "pdf_table_ordinal_argextreme_projection":
        scope.update(
            {
                "metric_header": bindings["metric"],
                "target_header": bindings["target"],
                "extremum": "max"
                if bindings["extremum"] == "最も高い"
                else "min",
                "style_channel": "raster_table_cell",
            }
        )
    elif rule_id == "pdf_meeting_section_page_number":
        scope.update(
            {
                "container": "05.会議/会議録/*.pdf",
                "meeting_id": bindings["meeting_id"],
                "section_heading": bindings["section"],
                "target": bindings["target"],
                "style_channel": "raster_heading_text",
            }
        )
    elif rule_id == "pdf_phase_effort_range_sum":
        scope.update(
            {
                "phases": [bindings["phase_left"], bindings["phase_right"]],
                "metric": bindings["metric"],
                "aggregate": bindings["aggregate"],
                "unit": bindings["unit"],
                "style_channel": "raster_spatial_text",
            }
        )
    else:
        raise ValueError(f"unsupported PDF visual rule: {rule_id}")
    core = {
        "pdf_visual_rule_version": PDF_VISUAL_RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": scope,
        "operation_graph": {
            "external_inputs": [
                {
                    "input_ref": "input_question",
                    "input_type": "source_records",
                    "source": "question_scope",
                }
            ],
            "nodes": nodes,
            "edges": [
                {
                    "from": nodes[index - 1]["output_ref"],
                    "to": nodes[index]["operation_id"],
                }
                for index in range(1, len(nodes))
            ],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            **dict(output_shape),
        },
    }
    return {
        "graph_contract_id": "pdfgraph_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


def graph_contract_for_pdf_question(question: str) -> dict[str, Any] | None:
    """Compile one complete PDF visual grammar into a typed graph contract."""

    if not isinstance(question, str):
        return None
    marker = _MARKER_QUESTION.fullmatch(question)
    if marker is not None:
        return _graph_contract(
            question,
            marker,
            "pdf_page_inline_marker_word_projection",
            _MARKER_OPERATORS,
            {
                "cardinality": "all",
                "answer_shape": {
                    "container": "list",
                    "value_type": "string",
                    "unit": None,
                },
            },
        )
    table = _TABLE_EXTREME_QUESTION.fullmatch(question)
    if table is not None:
        return _graph_contract(
            question,
            table,
            "pdf_table_ordinal_argextreme_projection",
            _TABLE_OPERATORS,
            {
                "cardinality": "single",
                "answer_shape": {
                    "container": "scalar",
                    "value_type": "string",
                    "unit": None,
                },
            },
        )
    meeting = _MEETING_SECTION_PAGE_QUESTION.fullmatch(question)
    if meeting is not None:
        return _graph_contract(
            question,
            meeting,
            "pdf_meeting_section_page_number",
            _MEETING_SECTION_PAGE_OPERATORS,
            {
                "cardinality": "single",
                "answer_shape": {
                    "container": "scalar",
                    "value_type": "integer",
                    "unit": None,
                },
            },
        )
    phase_effort = _PHASE_EFFORT_SUM_QUESTION.fullmatch(question)
    if phase_effort is not None:
        if _compact(phase_effort["phase_left"]) == _compact(
            phase_effort["phase_right"]
        ):
            return None
        return _graph_contract(
            question,
            phase_effort,
            "pdf_phase_effort_range_sum",
            _PHASE_EFFORT_SUM_OPERATORS,
            {
                "cardinality": "single",
                "answer_shape": {
                    "container": "scalar",
                    "value_type": "string",
                    "unit": phase_effort["unit"],
                },
            },
        )
    return None


@dataclass(frozen=True)
class _BBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2.0

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass(frozen=True)
class _OCRWord:
    text: str
    bbox: _BBox
    line_key: tuple[int, int, int, int]
    sequence: int
    confidence: float


@dataclass(frozen=True)
class _OCRLine:
    text: str
    bbox: _BBox
    sequence: int


@dataclass(frozen=True)
class _OCRResult:
    words: tuple[_OCRWord, ...]
    lines: tuple[_OCRLine, ...]


@dataclass
class _PageEvidence:
    page_number: int
    png_bytes: bytes
    image_sha256: str
    width: int
    height: int
    materialized_path: Path | None
    hint_runs: tuple[tuple[_OCRLine, ...], ...] = ()
    ocr: _OCRResult | None = None


@dataclass(frozen=True)
class _MarkerEvidence:
    bbox: _BBox
    fill_rgb: tuple[int, int, int]
    local_contrast: float
    word_sequence: int


@dataclass(frozen=True)
class _TableRowEvidence:
    target_bbox: _BBox
    metric_bbox: _BBox
    target_words: tuple[_OCRWord, ...]
    metric_words: tuple[_OCRWord, ...]
    metric_text: str
    metric_family: str
    metric_rank: Decimal


@dataclass(frozen=True)
class _EffortRangeEvidence:
    lower: int
    upper: int
    line_sequence: int
    bbox: _BBox


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_limited(path: Path, limit: int) -> bytes | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _source_sha256(path: Path) -> str | None:
    data = _read_limited(path, _MAX_PDF_BYTES)
    return _sha256_bytes(data) if data is not None else None


def _relative_source(path: Path, source_root: Path) -> str | None:
    try:
        return _nfc(path.relative_to(source_root).as_posix())
    except ValueError:
        return None


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _artifact_root(engine: Any) -> Path | None:
    raw = getattr(engine, "pdf_visual_artifact_root", _DEFAULT_ARTIFACT_ROOT)
    try:
        root = Path(raw).resolve()
    except (OSError, TypeError, ValueError):
        return None
    return root if root.is_dir() and not root.is_symlink() else None


def _report_key(value: str) -> str:
    result = _compact(value)
    return result[:-1] if result.endswith("書") else result


def _matching_report_paths(
    engine: Any,
    location: str,
    report_kind: str,
) -> tuple[Path, ...]:
    try:
        from structured_candidate import _candidate_values, _location_matches
    except Exception:
        return ()
    try:
        root = Path(getattr(engine, "source_root", "")).resolve()
    except (OSError, TypeError, ValueError):
        return ()
    if not root.is_dir() or root.is_symlink():
        return ()
    candidates = _candidate_values(location, getattr(engine, "glossary", None))
    report_key = _report_key(report_kind)
    if not report_key:
        return ()
    matches: list[Path] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith("~$")
            or path.suffix.casefold() != ".pdf"
        ):
            continue
        relative = path.relative_to(root)
        if not _location_matches(relative.parts[:-1], candidates):
            continue
        compact_stem = _compact(path.stem)
        if report_key not in compact_stem:
            continue
        report_suffix = compact_stem.rsplit(report_key, 1)[1]
        if any(
            marker in report_suffix
            for marker in (
                "旧版",
                "旧稿",
                "_old",
                "-old",
                "_backup",
                "-backup",
                "_bak",
                "-bak",
                "_archive",
                "-archive",
                "_archived",
                "-archived",
                "_previous",
                "-previous",
                "_prev",
                "-prev",
            )
        ):
            continue
        if not any("報告" in _normalized(part) for part in relative.parts):
            continue
        matches.append(path)
    return tuple(
        sorted(
            matches,
            key=lambda item: _nfc(item.relative_to(root).as_posix()),
        )
    )


def _matching_meeting_paths(engine: Any, location: str) -> tuple[Path, ...]:
    """Enumerate current meeting-minute PDFs without guessing an ID from names."""

    try:
        from structured_candidate import _candidate_values, _location_matches
    except Exception:
        return ()
    try:
        root = Path(getattr(engine, "source_root", "")).resolve()
    except (OSError, TypeError, ValueError):
        return ()
    if not root.is_dir() or root.is_symlink():
        return ()
    candidates = _candidate_values(location, getattr(engine, "glossary", None))
    matches: list[Path] = []
    for path in root.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.startswith("~$")
            or path.suffix.casefold() != ".pdf"
        ):
            continue
        relative = path.relative_to(root)
        if not _location_matches(relative.parts[:-1], candidates):
            continue
        if _compact(path.parent.name) != "会議録" or not _compact(path.stem).startswith(
            "会議録"
        ):
            continue
        if not any(_compact(part).startswith("05.会議") for part in relative.parts):
            continue
        compact_stem = _compact(path.stem)
        if any(
            marker in compact_stem
            for marker in (
                "旧版",
                "旧稿",
                "_old",
                "-old",
                "_backup",
                "-backup",
                "_bak",
                "-bak",
                "_archive",
                "-archive",
                "_archived",
                "-archived",
                "_previous",
                "-previous",
                "_prev",
                "-prev",
            )
        ):
            continue
        matches.append(path)
    return tuple(
        sorted(
            matches,
            key=lambda item: _nfc(item.relative_to(root).as_posix()),
        )
    )


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...] | None:
    raw = _read_limited(path, _MAX_MANIFEST_BYTES)
    if raw is None:
        return () if not path.exists() else None
    try:
        text = raw.decode("utf-8")
        records = tuple(
            json.loads(line)
            for line in text.splitlines()
            if line.strip()
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return records if all(isinstance(record, dict) for record in records) else None


def _pdf_page_count(path: Path) -> int | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return None
        count = len(reader.pages)
    except Exception:
        return None
    return count if 1 <= count <= _MAX_PAGES else None


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if image.format != "PNG":
                return None
    except Exception:
        return None
    if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
        return None
    return width, height


def _render_pdf_page(path: Path, page_number: int) -> bytes | None:
    executable = shutil.which("pdftoppm")
    if executable is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-visual-render-") as temporary:
            prefix = Path(temporary) / "page"
            completed = subprocess.run(
                [
                    executable,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-r",
                    "200",
                    "-singlefile",
                    "-png",
                    str(path),
                    str(prefix),
                ],
                capture_output=True,
                timeout=_OCR_TIMEOUT_SECONDS,
                check=False,
            )
            output = prefix.with_suffix(".png")
            if completed.returncode != 0:
                return None
            return _read_limited(output, _MAX_IMAGE_BYTES)
    except (OSError, subprocess.SubprocessError):
        return None


def _matching_materialized_records(
    artifact_root: Path,
    source_root: Path,
    source_path: Path,
    source_sha256: str,
) -> tuple[dict[str, Any], ...] | None:
    manifest = artifact_root / "visual-classification-v1" / "materialized-full-batch.jsonl"
    records = _read_jsonl(manifest)
    if records is None:
        return None
    relative = _relative_source(source_path, source_root)
    if relative is None:
        return None
    result = []
    for record in records:
        source = record.get("source")
        if not isinstance(source, Mapping):
            continue
        if source.get("sha256") != source_sha256:
            continue
        if _normalized(source.get("relative_path", "")) != _normalized(relative):
            continue
        result.append(record)
    return tuple(result)


def _materialized_page(
    record: Mapping[str, Any],
    artifact_root: Path,
    source_root: Path,
    source_path: Path,
    source_sha256: str,
    page_count: int,
) -> _PageEvidence | None:
    source = record.get("source")
    origin = record.get("origin")
    provenance = record.get("provenance")
    materialization = record.get("materialization")
    if not all(
        isinstance(value, Mapping)
        for value in (source, origin, provenance, materialization)
    ):
        return None
    try:
        source_size = source_path.stat().st_size
    except OSError:
        return None
    if (
        source.get("sha256") != source_sha256
        or source.get("size_bytes") != source_size
        or origin.get("kind") != "pdf_page"
        or provenance.get("question_independent") is not True
        or materialization.get("dpi") != 200
        or materialization.get("mime_type") != "image/png"
    ):
        return None
    page_number = origin.get("page_number")
    if not isinstance(page_number, int) or not 1 <= page_number <= page_count:
        return None
    declared = record.get("materialized_path")
    if not isinstance(declared, str) or not declared:
        return None
    candidate = Path(declared)
    if not candidate.is_absolute():
        candidate = artifact_root / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not _path_inside(resolved, artifact_root) or resolved.is_symlink():
        return None
    data = _read_limited(resolved, _MAX_IMAGE_BYTES)
    if data is None:
        return None
    digest = _sha256_bytes(data)
    if digest != materialization.get("sha256"):
        return None
    dimensions = _image_dimensions(data)
    if dimensions is None:
        return None
    width, height = dimensions
    if (
        width != materialization.get("width_px")
        or height != materialization.get("height_px")
    ):
        return None
    relative = _relative_source(source_path, source_root)
    if relative is None or _normalized(source.get("relative_path", "")) != _normalized(relative):
        return None
    return _PageEvidence(
        page_number=page_number,
        png_bytes=data,
        image_sha256=digest,
        width=width,
        height=height,
        materialized_path=resolved,
    )


def _hint_bbox(value: object, width: int, height: int) -> _BBox | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) for item in value)
    ):
        return None
    x, y, w, h = value
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1000 or y + h > 1000:
        return None
    left = round(x * width / 1000)
    top = round(y * height / 1000)
    right = round((x + w) * width / 1000)
    bottom = round((y + h) * height / 1000)
    return _BBox(left, top, right, bottom)


def _attach_ocr_hints(
    pages: Sequence[_PageEvidence],
    artifact_root: Path,
    source_root: Path,
    source_path: Path,
    source_sha256: str,
) -> bool:
    manifest = artifact_root / "ocr-observation-v1" / "ocr-observations-full.jsonl"
    records = _read_jsonl(manifest)
    if records is None:
        return False
    if not records:
        return True
    relative = _relative_source(source_path, source_root)
    if relative is None:
        return False
    try:
        source_size = source_path.stat().st_size
    except OSError:
        return False
    by_page = {page.page_number: page for page in pages}
    seen: set[int] = set()
    for record in records:
        source = record.get("source")
        origin = record.get("origin")
        if not isinstance(source, Mapping) or not isinstance(origin, Mapping):
            continue
        if source.get("sha256") != source_sha256:
            continue
        if _normalized(source.get("relative_path", "")) != _normalized(relative):
            continue
        page_number = origin.get("page_number")
        if not isinstance(page_number, int) or page_number not in by_page or page_number in seen:
            return False
        seen.add(page_number)
        page = by_page[page_number]
        asset = record.get("asset")
        provenance = record.get("provenance")
        if (
            source.get("size_bytes") != source_size
            or origin.get("kind") != "pdf_page"
            or not isinstance(asset, Mapping)
            or asset.get("sha256") != page.image_sha256
            or not isinstance(provenance, Mapping)
            or provenance.get("question_independent") is not True
        ):
            return False
        dimensions = asset.get("dimensions")
        if not isinstance(dimensions, Mapping) or (
            dimensions.get("width_px") != page.width
            or dimensions.get("height_px") != page.height
        ):
            return False
        runs: list[tuple[_OCRLine, ...]] = []
        independence_groups: set[str] = set()
        engine_runs = record.get("engine_runs")
        if not isinstance(engine_runs, list):
            return False
        for engine_run in engine_runs:
            if not isinstance(engine_run, Mapping) or engine_run.get("status") != "completed":
                continue
            engine_identity = engine_run.get("engine")
            if not isinstance(engine_identity, Mapping):
                continue
            independence_group = engine_identity.get("independence_group")
            engine_name = engine_identity.get("name")
            engine_digest = engine_identity.get("digest")
            if (
                not isinstance(independence_group, str)
                or not independence_group
                or independence_group in independence_groups
                or not isinstance(engine_name, str)
                or not engine_name
                or not isinstance(engine_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", engine_digest)
            ):
                continue
            raw_lines = engine_run.get("lines")
            if not isinstance(raw_lines, list):
                return False
            parsed: list[_OCRLine] = []
            for index, line in enumerate(raw_lines, 1):
                if not isinstance(line, Mapping) or not isinstance(line.get("raw_text"), str):
                    return False
                bbox = _hint_bbox(line.get("bbox"), page.width, page.height)
                if bbox is None:
                    return False
                parsed.append(_OCRLine(line["raw_text"], bbox, index))
            if parsed:
                runs.append(tuple(parsed))
                independence_groups.add(independence_group)
        if len(runs) >= 2 and len(independence_groups) == len(runs):
            page.hint_runs = tuple(runs)
    return True


def _all_pdf_pages(
    engine: Any,
    source_path: Path,
    source_sha256: str,
) -> tuple[_PageEvidence, ...] | None:
    source_root = Path(getattr(engine, "source_root", "")).resolve()
    artifact_root = _artifact_root(engine)
    if artifact_root is None:
        return None
    page_count = _pdf_page_count(source_path)
    if page_count is None:
        return None
    records = _matching_materialized_records(
        artifact_root, source_root, source_path, source_sha256
    )
    if records is None:
        return None
    materialized: dict[int, _PageEvidence] = {}
    for record in records:
        page = _materialized_page(
            record,
            artifact_root,
            source_root,
            source_path,
            source_sha256,
            page_count,
        )
        if page is None or page.page_number in materialized:
            return None
        materialized[page.page_number] = page
    pages: list[_PageEvidence] = []
    for page_number in range(1, page_count + 1):
        page = materialized.get(page_number)
        if page is None:
            data = _render_pdf_page(source_path, page_number)
            if data is None:
                return None
            dimensions = _image_dimensions(data)
            if dimensions is None:
                return None
            page = _PageEvidence(
                page_number=page_number,
                png_bytes=data,
                image_sha256=_sha256_bytes(data),
                width=dimensions[0],
                height=dimensions[1],
                materialized_path=None,
            )
        pages.append(page)
    if not _attach_ocr_hints(
        pages,
        artifact_root,
        source_root,
        source_path,
        source_sha256,
    ):
        return None
    return tuple(pages)


def _tesseract_executable() -> str | None:
    return shutil.which("tesseract")


def _parse_tesseract_tsv(tsv_text: str, width: int, height: int) -> _OCRResult | None:
    reader = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
    required = {
        "level",
        "page_num",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    }
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        return None
    words: list[_OCRWord] = []
    grouped: OrderedDict[tuple[int, int, int, int], list[_OCRWord]] = OrderedDict()
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            if int(row["level"]) != 5 or float(row["conf"]) < 0:
                continue
            left = int(row["left"])
            top = int(row["top"])
            word_width = int(row["width"])
            word_height = int(row["height"])
            key = tuple(
                int(row[name])
                for name in ("page_num", "block_num", "par_num", "line_num")
            )
            confidence = float(row["conf"]) / 100.0
        except (KeyError, TypeError, ValueError):
            return None
        bbox = _BBox(left, top, left + word_width, top + word_height)
        if (
            bbox.width <= 0
            or bbox.height <= 0
            or bbox.left < 0
            or bbox.top < 0
            or bbox.right > width
            or bbox.bottom > height
        ):
            return None
        word = _OCRWord(text, bbox, key, len(words) + 1, confidence)
        words.append(word)
        grouped.setdefault(key, []).append(word)
    if not words:
        return None
    lines: list[_OCRLine] = []
    for index, line_words in enumerate(grouped.values(), 1):
        ordered = sorted(line_words, key=lambda item: item.bbox.left)
        bbox = _bbox_union(item.bbox for item in ordered)
        if bbox is None:
            return None
        lines.append(
            _OCRLine(
                " ".join(item.text for item in ordered),
                bbox,
                index,
            )
        )
    return _OCRResult(tuple(words), tuple(lines))


def _run_page_ocr(page: _PageEvidence) -> _OCRResult | None:
    executable = _tesseract_executable()
    if executable is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="pdf-visual-ocr-") as temporary:
            output_base = Path(temporary) / "ocr"
            completed = subprocess.run(
                [
                    executable,
                    "stdin",
                    str(output_base),
                    "-l",
                    "jpn+eng",
                    "--oem",
                    "1",
                    "--psm",
                    "3",
                    "-c",
                    "preserve_interword_spaces=1",
                    "tsv",
                ],
                input=page.png_bytes,
                capture_output=True,
                timeout=_OCR_TIMEOUT_SECONDS,
                check=False,
            )
            output = output_base.with_suffix(".tsv")
            if completed.returncode != 0 or output.is_symlink():
                return None
            raw = _read_limited(output, 8 * 1024 * 1024)
            if raw is None:
                return None
            tsv_text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError, subprocess.SubprocessError):
        return None
    return _parse_tesseract_tsv(tsv_text, page.width, page.height)


def _ensure_page_ocr(page: _PageEvidence) -> _OCRResult | None:
    if page.ocr is None:
        page.ocr = _run_page_ocr(page)
    return page.ocr


def _page_runs(page: _PageEvidence) -> tuple[tuple[_OCRLine, ...], ...] | None:
    if page.hint_runs:
        return page.hint_runs
    ocr = _ensure_page_ocr(page)
    return (ocr.lines,) if ocr is not None else None


def _title_matches(source_title: str, requested_title: str) -> bool:
    source = _compact(source_title)
    requested = _compact(requested_title)
    if not source or not requested:
        return False
    return source == requested or source.startswith(requested + "(")


def _page_matches_title(page: _PageEvidence, title: str) -> bool | None:
    runs = _page_runs(page)
    if not runs:
        return None
    votes = 0
    for run in runs:
        if any(
            line.bbox.top < page.height * 0.25
            and line.bbox.height >= page.height * 0.018
            and _title_matches(line.text, title)
            for line in run
        ):
            votes += 1
    required = 2 if len(runs) >= 2 else 1
    return votes >= required


def _fresh_page_matches_title(page: _PageEvidence, title: str) -> bool | None:
    """Recheck a selected page title against fresh OCR, never manifest hints."""

    ocr = _ensure_page_ocr(page)
    if ocr is None:
        return None
    return any(
        line.bbox.top < page.height * 0.25
        and line.bbox.height >= page.height * 0.018
        and _title_matches(line.text, title)
        for line in ocr.lines
    )


def _selected_page_matches_source(page: _PageEvidence, source_path: Path) -> bool:
    """Bind a materialized selected page back to a fresh source-PDF render."""

    if page.materialized_path is None:
        return True
    rendered = _render_pdf_page(source_path, page.page_number)
    if rendered is None or _sha256_bytes(rendered) != page.image_sha256:
        return False
    dimensions = _image_dimensions(rendered)
    return dimensions == (page.width, page.height)


_MEETING_ID_HEADER = re.compile(
    r"(?:会)?議id[:：](?P<meeting_id>[a-z0-9_-]{2,32})"
)
_NUMBERED_HEADING = re.compile(
    r"^(?P<ordinal>[0-9]{1,3})[.:、・)）](?P<label>.+)$"
)
_EFFORT_RANGE = re.compile(
    r"(?<![0-9])(?P<lower>[0-9]{1,5})"
    r"[-‐‑‒–—―−〜~]"
    r"(?P<upper>[0-9]{1,5})(?:hours?|hrs?|h|時間)(?![a-z])"
)


def _meeting_id_binding_state(
    page: _PageEvidence,
    requested_meeting_id: str,
) -> str:
    """Classify a candidate while requiring stronger evidence for a positive bind."""

    if page.page_number != 1:
        return "ambiguous"
    runs = _page_runs(page)
    if not runs:
        return "ambiguous"
    observed: list[str] = []
    for run in runs:
        values = {
            match["meeting_id"]
            for line in run
            if line.bbox.top < page.height * 0.25
            for match in [_MEETING_ID_HEADER.search(_compact(line.text))]
            if match is not None
        }
        if len(values) > 1:
            return "ambiguous"
        if values:
            observed.append(next(iter(values)))
    if not observed or len(set(observed)) != 1:
        return "ambiguous"
    requested = _compact(requested_meeting_id)
    if observed[0] != requested:
        # One clear, source-bound non-target reading is sufficient to exclude a
        # document.  A positive bind is stricter: every available run must read
        # the requested ID, so a dropped/conflicting run can never select it.
        return "mismatch"
    return "match" if len(observed) == len(runs) else "ambiguous"


def _script_group(character: str) -> str:
    code = ord(character)
    if (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    ):
        return "han"
    if 0x3040 <= code <= 0x309F:
        return "hiragana"
    if 0x30A0 <= code <= 0x30FF:
        return "katakana"
    if character.isalnum():
        return "alnum"
    return "other"


def _section_anchors(section: str) -> tuple[str, ...]:
    compact = _compact(section)
    if not compact:
        return ()
    parts: list[str] = []
    active_group: str | None = None
    active: list[str] = []
    for character in compact:
        group = _script_group(character)
        if group == "other":
            if active:
                parts.append("".join(active))
                active = []
                active_group = None
            continue
        if active and group != active_group:
            parts.append("".join(active))
            active = []
        active.append(character)
        active_group = group
    if active:
        parts.append("".join(active))
    return tuple(part for part in parts if part)


def _section_heading_ordinal(line_text: str, section: str) -> int | None:
    match = _NUMBERED_HEADING.fullmatch(_compact(line_text))
    if match is None:
        return None
    label = match["label"]
    requested = _compact(section)
    if not requested:
        return None
    if label != requested:
        anchors = _section_anchors(section)
        # OCR runs remain separate.  The tolerant path only accepts one inserted
        # glyph between stable script-boundary anchors; it never rewrites either
        # run into a preferred reading.
        if len(anchors) < 2 or len(label) != len(requested) + 1:
            return None
        cursor = 0
        for index, anchor in enumerate(anchors):
            position = label.find(anchor, cursor)
            if position < 0 or (index == 0 and position != 0) or position - cursor > 1:
                return None
            cursor = position + len(anchor)
        if cursor != len(label):
            return None
    ordinal = int(match["ordinal"])
    return ordinal if 1 <= ordinal <= 999 else None


def _meeting_section_page(
    pages: Sequence[_PageEvidence],
    section: str,
) -> _PageEvidence | None:
    candidates: list[_PageEvidence] = []
    for page in pages:
        runs = _page_runs(page)
        if not runs:
            return None
        run_ordinals: list[int | None] = []
        for run in runs:
            ordinals = {
                ordinal
                for line in run
                for ordinal in [_section_heading_ordinal(line.text, section)]
                if ordinal is not None
            }
            if len(ordinals) > 1:
                return None
            run_ordinals.append(next(iter(ordinals)) if ordinals else None)
        present = [ordinal for ordinal in run_ordinals if ordinal is not None]
        if present and len(present) != len(run_ordinals):
            return None
        if present:
            if len(set(present)) != 1:
                return None
            candidates.append(page)
    return candidates[0] if len(candidates) == 1 else None


def _bound_meeting_source(
    engine: Any,
    location: str,
    requested_meeting_id: str,
) -> tuple[Path, tuple[_PageEvidence, ...]] | None:
    paths = _matching_meeting_paths(engine, location)
    if not paths:
        return None
    requested = _compact(requested_meeting_id)
    if not requested:
        return None
    matches: list[tuple[Path, tuple[_PageEvidence, ...]]] = []
    for path in paths:
        source_sha = _source_sha256(path)
        if source_sha is None:
            return None
        pages = _all_pdf_pages(engine, path, source_sha)
        if not pages:
            return None
        first_pages = [page for page in pages if page.page_number == 1]
        if len(first_pages) != 1 or not _selected_page_matches_source(first_pages[0], path):
            return None
        state = _meeting_id_binding_state(first_pages[0], requested)
        if state == "ambiguous":
            return None
        if state == "match":
            matches.append((path, pages))
    return matches[0] if len(matches) == 1 else None


def _effort_ranges(value: str) -> tuple[tuple[int, int], ...]:
    ranges: set[tuple[int, int]] = set()
    for match in _EFFORT_RANGE.finditer(_compact(value)):
        lower = int(match["lower"])
        upper = int(match["upper"])
        if 0 < lower <= upper <= 100_000:
            ranges.add((lower, upper))
    return tuple(sorted(ranges))


def _line_belongs_to_phase(
    page: _PageEvidence,
    header: _OCRLine,
    candidate: _OCRLine,
) -> bool:
    vertical_distance = candidate.bbox.center_y - header.bbox.center_y
    if not 0 <= vertical_distance <= page.height * 0.25:
        return False
    overlap = max(
        0,
        min(header.bbox.right, candidate.bbox.right)
        - max(header.bbox.left, candidate.bbox.left),
    )
    minimum_width = min(header.bbox.width, candidate.bbox.width)
    return minimum_width > 0 and overlap >= minimum_width * 0.25


def _phase_effort_for_run(
    page: _PageEvidence,
    run: Sequence[_OCRLine],
    phase: str,
) -> tuple[str, _EffortRangeEvidence | None]:
    requested = _compact(phase)
    phase_pattern = re.compile(re.escape(requested) + r"(?![a-z0-9])")
    headers = [
        line for line in run if phase_pattern.search(_compact(line.text)) is not None
    ]
    if not headers:
        return "absent", None
    if len(headers) != 1:
        return "ambiguous", None
    header = headers[0]
    candidates: list[_EffortRangeEvidence] = []
    for line in run:
        if not _line_belongs_to_phase(page, header, line):
            continue
        ranges = _effort_ranges(line.text)
        if len(ranges) > 1:
            return "ambiguous", None
        if len(ranges) == 1:
            lower, upper = ranges[0]
            candidates.append(
                _EffortRangeEvidence(lower, upper, line.sequence, line.bbox)
            )
    if len(candidates) != 1:
        return "ambiguous", None
    return "resolved", candidates[0]


def _phase_efforts_for_page(
    page: _PageEvidence,
    phase_left: str,
    phase_right: str,
) -> tuple[str, tuple[_EffortRangeEvidence, _EffortRangeEvidence] | None]:
    runs = _page_runs(page)
    if not runs:
        return "ambiguous", None
    resolved: list[tuple[_EffortRangeEvidence, _EffortRangeEvidence]] = []
    absent_runs = 0
    for run in runs:
        left_state, left = _phase_effort_for_run(page, run, phase_left)
        right_state, right = _phase_effort_for_run(page, run, phase_right)
        if left_state == right_state == "absent":
            absent_runs += 1
            continue
        if (
            left_state != "resolved"
            or right_state != "resolved"
            or left is None
            or right is None
            or left.line_sequence == right.line_sequence
        ):
            return "ambiguous", None
        resolved.append((left, right))
    if absent_runs == len(runs):
        return "absent", None
    if absent_runs or not resolved:
        return "ambiguous", None
    values = {
        (left.lower, left.upper, right.lower, right.upper)
        for left, right in resolved
    }
    if len(values) != 1:
        return "ambiguous", None
    return "resolved", resolved[0]


def _fresh_effort_range_agrees(
    page: _PageEvidence,
    evidence: _EffortRangeEvidence,
) -> bool:
    crop = _crop_image(
        page,
        evidence.bbox,
        padding=max(4, round(evidence.bbox.height * 0.16)),
    )
    if crop is None:
        return False
    readings = [_ocr_crop_once(crop, psm, "jpn+eng") for psm in (3, 6, 7)]
    if any(reading is None or len(reading) > 512 for reading in readings):
        return False
    parsed = [_effort_ranges(reading or "") for reading in readings]
    return all(
        values == ((evidence.lower, evidence.upper),)
        for values in parsed
    )


def _phase_effort_page(
    pages: Sequence[_PageEvidence],
    phase_left: str,
    phase_right: str,
) -> tuple[_PageEvidence, _EffortRangeEvidence, _EffortRangeEvidence] | None:
    candidates: list[
        tuple[_PageEvidence, _EffortRangeEvidence, _EffortRangeEvidence]
    ] = []
    for page in pages:
        state, evidence = _phase_efforts_for_page(page, phase_left, phase_right)
        if state == "ambiguous":
            return None
        if state == "resolved" and evidence is not None:
            candidates.append((page, evidence[0], evidence[1]))
    return candidates[0] if len(candidates) == 1 else None


def _runs_contain_headers(
    page: _PageEvidence,
    target_header: str,
    metric_header: str,
) -> bool | None:
    runs = _page_runs(page)
    if not runs:
        return None
    target = _compact(target_header)
    metric = _compact(metric_header)
    votes = 0
    for run in runs:
        text = _compact("\n".join(line.text for line in run))
        if target in text and metric in text:
            votes += 1
    required = 2 if len(runs) >= 2 else 1
    return votes >= required


def _open_rgb(page: _PageEvidence) -> Any | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(page.png_bytes)) as image:
            return image.convert("RGB")
    except Exception:
        return None


def _bbox_union(boxes: Iterable[_BBox]) -> _BBox | None:
    values = tuple(boxes)
    if not values:
        return None
    return _BBox(
        min(value.left for value in values),
        min(value.top for value in values),
        max(value.right for value in values),
        max(value.bottom for value in values),
    )


def _boolean_runs(values: Sequence[bool], minimum: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    start: int | None = None
    for index, active in enumerate(values):
        if active and start is None:
            start = index
        if start is not None and (not active or index == len(values) - 1):
            end = index if not active else index + 1
            if end - start >= minimum:
                result.append((start, end))
            start = None
    return result


def _close_short_gaps(values: Sequence[bool], maximum_gap: int) -> list[bool]:
    result = list(values)
    index = 0
    while index < len(result):
        if result[index]:
            index += 1
            continue
        start = index
        while index < len(result) and not result[index]:
            index += 1
        if (
            start > 0
            and index < len(result)
            and index - start <= maximum_gap
        ):
            result[start:index] = [True] * (index - start)
    return result


def _marker_for_word(rgb: Any, word: _OCRWord) -> _MarkerEvidence | None:
    import numpy as np

    height, width = rgb.shape[:2]
    box = word.bbox
    if (
        not any(character.isalnum() for character in word.text)
        or box.width < 8
        or box.height < 8
        or box.width > width * 0.35
    ):
        return None
    pad_y = max(2, round(box.height * 0.18))
    x0, x1 = max(0, box.left), min(width, box.right)
    y0, y1 = max(0, box.top - pad_y), min(height, box.bottom + pad_y)
    sample = rgb[y0:y1, x0:x1].astype(np.float32)
    if sample.size == 0:
        return None
    luminance = sample.mean(axis=2)
    chroma = sample.max(axis=2) - sample.min(axis=2)
    neutral = (
        (chroma <= 15.0)
        & (luminance >= 112.0)
        & (luminance <= 218.0)
    )
    columns = _close_short_gaps(
        list(neutral.mean(axis=0) >= 0.34),
        max(2, round(box.height * 0.25)),
    )
    runs = _boolean_runs(columns, max(5, round(box.height * 0.18)))
    if len(runs) != 1:
        return None
    column_start, column_end = runs[0]
    strip = neutral[:, column_start:column_end]
    rows = _close_short_gaps(
        list(strip.mean(axis=1) >= 0.27),
        max(2, round(box.height * 0.05)),
    )
    row_runs = _boolean_runs(rows, max(5, round(box.height * 0.45)))
    if len(row_runs) != 1:
        return None
    row_start, row_end = row_runs[0]
    region = _BBox(
        x0 + column_start,
        y0 + row_start,
        x0 + column_end,
        y0 + row_end,
    )
    if not (
        box.height * 1.15 <= region.height <= box.height * 1.55
        and region.width >= max(box.height * 0.50, box.width * 0.70)
    ):
        return None
    region_sample = rgb[region.top:region.bottom, region.left:region.right].astype(
        np.float32
    )
    region_luma = region_sample.mean(axis=2)
    region_chroma = region_sample.max(axis=2) - region_sample.min(axis=2)
    fill_mask = (
        (region_chroma <= 15.0)
        & (region_luma >= 112.0)
        & (region_luma <= 218.0)
    )
    if fill_mask.mean() < 0.32 or (region_luma < 100.0).mean() < 0.01:
        return None
    fill_pixels = region_sample[fill_mask]
    fill_rgb = tuple(int(round(value)) for value in np.median(fill_pixels, axis=0))
    fill_luma = float(sum(fill_rgb) / 3.0)
    ring_pad = max(8, round(box.height * 0.85))
    rx0 = max(0, region.left - ring_pad)
    ry0 = max(0, region.top - ring_pad)
    rx1 = min(width, region.right + ring_pad)
    ry1 = min(height, region.bottom + ring_pad)
    ring = rgb[ry0:ry1, rx0:rx1].astype(np.float32)
    ring_luma = ring.mean(axis=2)
    inside = np.zeros(ring_luma.shape, dtype=bool)
    halo = max(2, round(box.height * 0.35))
    inside[
        max(0, region.top - halo - ry0) : min(
            ring_luma.shape[0], region.bottom + halo - ry0
        ),
        max(0, region.left - halo - rx0) : min(
            ring_luma.shape[1], region.right + halo - rx0
        ),
    ] = True
    ring_values = ring_luma[(~inside) & (ring_luma >= 100.0)]
    if ring_values.size < 20:
        return None
    local_background = float(np.percentile(ring_values, 85))
    local_contrast = abs(local_background - fill_luma)
    if local_contrast < 24.0:
        return None
    return _MarkerEvidence(region, fill_rgb, local_contrast, word.sequence)


def _iou(left: _BBox, right: _BBox) -> float:
    width = max(0, min(left.right, right.right) - max(left.left, right.left))
    height = max(0, min(left.bottom, right.bottom) - max(left.top, right.top))
    intersection = width * height
    if intersection == 0:
        return 0.0
    union = left.width * left.height + right.width * right.height - intersection
    return intersection / union if union else 0.0


def _overlap_fraction(left: _BBox, right: _BBox) -> float:
    width = max(0, min(left.right, right.right) - max(left.left, right.left))
    height = max(0, min(left.bottom, right.bottom) - max(left.top, right.top))
    intersection = width * height
    smaller = min(left.width * left.height, right.width * right.height)
    return intersection / smaller if smaller else 0.0


def _same_marker_region(left: _BBox, right: _BBox) -> bool:
    if _overlap_fraction(left, right) >= 0.40:
        return True
    horizontal = max(
        0,
        min(left.right, right.right) - max(left.left, right.left),
    )
    smaller_width = min(left.width, right.width)
    vertical = min(left.bottom, right.bottom) - max(left.top, right.top)
    return (
        smaller_width > 0
        and horizontal / smaller_width >= 0.80
        and vertical >= 0
    )


def _image_marker_regions(
    rgb: Any,
    ocr_markers: Sequence[_MarkerEvidence],
) -> tuple[_BBox, ...] | None:
    """Enumerate raster marker rectangles independently of OCR word presence.

    OCR-linked markers establish only the page-local marker scale.  The raster
    scan then enumerates every dense neutral rectangle at that scale.  The
    caller separately proves a one-to-one correspondence between these image
    regions and OCR-linked words, so a marker whose word vanished from OCR
    cannot be silently omitted from an ``all`` answer.
    """

    try:
        import numpy as np
    except Exception:
        return None
    height, width = rgb.shape[:2]
    if not ocr_markers or height * width > 12_000_000:
        return None
    channel_high = rgb.max(axis=2)
    channel_low = rgb.min(axis=2)
    channel_sum = (
        rgb[:, :, 0].astype(np.uint16)
        + rgb[:, :, 1].astype(np.uint16)
        + rgb[:, :, 2].astype(np.uint16)
    )
    neutral = (
        (channel_high.astype(np.int16) - channel_low.astype(np.int16) <= 15)
        & (channel_sum >= 3 * 112)
        & (channel_sum <= 3 * 218)
    )
    del channel_high, channel_low, channel_sum
    vertical_sum = np.vstack(
        (
            np.zeros((1, width), dtype=np.int32),
            np.cumsum(neutral, dtype=np.int32, axis=0),
        )
    )
    marker_heights = sorted({marker.bbox.height for marker in ocr_markers})
    if len(marker_heights) > 3:
        marker_heights = [
            marker_heights[0],
            marker_heights[len(marker_heights) // 2],
            marker_heights[-1],
        ]
    scan_heights = sorted(
        {
            max(8, round(marker_height * scale))
            for marker_height in marker_heights
            for scale in (0.70, 0.85, 1.00, 1.15, 1.30)
        }
    )
    proposals: list[tuple[_BBox, float]] = []
    for scan_height in scan_heights:
        if scan_height >= height:
            continue
        step = max(2, scan_height // 10)
        tops = list(range(0, height - scan_height + 1, step))
        if not tops or tops[-1] != height - scan_height:
            tops.append(height - scan_height)
        for top in tops:
            column_fraction = (
                vertical_sum[top + scan_height] - vertical_sum[top]
            ) / scan_height
            columns = _close_short_gaps(
                list(column_fraction >= 0.34),
                max(2, round(scan_height * 0.28)),
            )
            runs = _boolean_runs(
                columns,
                max(8, round(scan_height * 0.45)),
            )
            for left, right in runs:
                if right - left > width * 0.35:
                    continue
                region = neutral[top : top + scan_height, left:right]
                fill_fraction = float(region.mean())
                if fill_fraction < 0.48:
                    continue
                edge_height = max(2, round(scan_height * 0.12))
                edge_fill = float(
                    max(region[:edge_height].mean(), region[-edge_height:].mean())
                )
                if edge_fill < 0.58:
                    continue
                region_rgb = rgb[top : top + scan_height, left:right]
                dark_fraction = float(
                    (
                        region_rgb[:, :, 0].astype(np.uint16)
                        + region_rgb[:, :, 1].astype(np.uint16)
                        + region_rgb[:, :, 2].astype(np.uint16)
                        < 3 * 100
                    ).mean()
                )
                if dark_fraction < 0.01:
                    continue
                proposals.append(
                    (
                        _BBox(left, top, right, top + scan_height),
                        fill_fraction + edge_fill * 0.20,
                    )
                )
                if len(proposals) > 2048:
                    return None
    selected: list[_BBox] = []
    for bbox, _ in sorted(proposals, key=lambda item: item[1], reverse=True):
        if any(
            _same_marker_region(bbox, previous)
            or any(
                _overlap_fraction(marker.bbox, bbox) >= 0.40
                and _overlap_fraction(marker.bbox, previous) >= 0.40
                for marker in ocr_markers
            )
            for previous in selected
        ):
            continue
        selected.append(bbox)
    return tuple(sorted(selected, key=lambda bbox: (bbox.top, bbox.left))) or None


def _detect_markers(page: _PageEvidence, ocr: _OCRResult) -> tuple[_MarkerEvidence, ...] | None:
    image = _open_rgb(page)
    if image is None:
        return None
    try:
        import numpy as np

        rgb = np.asarray(image, dtype=np.uint8)
    except Exception:
        return None
    candidates = [
        marker
        for word in ocr.words
        for marker in [_marker_for_word(rgb, word)]
        if marker is not None
    ]
    selected: list[_MarkerEvidence] = []
    for candidate in sorted(candidates, key=lambda value: value.word_sequence):
        if any(_iou(candidate.bbox, previous.bbox) >= 0.50 for previous in selected):
            continue
        selected.append(candidate)
    if not selected:
        return None
    image_regions = _image_marker_regions(rgb, selected)
    if image_regions is None or len(image_regions) != len(selected):
        return None
    word_to_regions = [
        [
            index
            for index, region in enumerate(image_regions)
            if _overlap_fraction(marker.bbox, region) >= 0.70
        ]
        for marker in selected
    ]
    region_to_words = [
        [
            index
            for index, marker in enumerate(selected)
            if _overlap_fraction(marker.bbox, region) >= 0.70
        ]
        for region in image_regions
    ]
    if any(len(matches) != 1 for matches in word_to_regions) or any(
        len(matches) != 1 for matches in region_to_words
    ):
        return None
    return tuple(selected)


def _crop_image(page: _PageEvidence, bbox: _BBox, padding: int = 0) -> Any | None:
    image = _open_rgb(page)
    if image is None:
        return None
    left = max(0, bbox.left - padding)
    top = max(0, bbox.top - padding)
    right = min(page.width, bbox.right + padding)
    bottom = min(page.height, bbox.bottom + padding)
    if right <= left or bottom <= top:
        return None
    return image.crop((left, top, right, bottom))


def _ocr_crop_once(image: Any, psm: int, languages: str) -> str | None:
    executable = _tesseract_executable()
    if executable is None:
        return None
    buffer = io.BytesIO()
    try:
        image.save(buffer, format="PNG")
        completed = subprocess.run(
            [
                executable,
                "stdin",
                "stdout",
                "-l",
                languages,
                "--oem",
                "1",
                "--psm",
                str(psm),
            ],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=_OCR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        text = completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    rendered = re.sub(r"\s+", " ", _nfc(text)).strip(" |_\n\r\t")
    return rendered or None


def _crop_consensus(
    image: Any,
    psms: Sequence[int],
    *,
    languages: str = "jpn+eng",
    minimum_votes: int = 2,
) -> str | None:
    readings = [
        reading
        for psm in psms
        for reading in [_ocr_crop_once(image, psm, languages)]
        if reading is not None and len(reading) <= 256
    ]
    if not readings:
        return None
    keys = Counter(_compact(reading) for reading in readings if _compact(reading))
    if not keys:
        return None
    ranked = keys.most_common()
    if ranked[0][1] < minimum_votes or (
        len(ranked) > 1 and ranked[0][1] == ranked[1][1]
    ):
        return None
    winner = ranked[0][0]
    return next(reading for reading in readings if _compact(reading) == winner)


def _marked_words(page: _PageEvidence) -> tuple[str, ...] | None:
    ocr = _ensure_page_ocr(page)
    if ocr is None:
        return None
    markers = _detect_markers(page, ocr)
    if not markers:
        return None
    answers: list[str] = []
    for marker in markers:
        crop = _crop_image(page, marker.bbox)
        if crop is None:
            return None
        answer = _crop_consensus(
            crop,
            (7, 8, 10, 13),
            languages="jpn+eng",
            minimum_votes=2,
        )
        if answer is None:
            return None
        cleaned = answer.strip(
            "()\uff08\uff09[]\uff3b\uff3d{}\uff5b\uff5d\u300c\u300d\u300e\u300f<>\uff1c\uff1e"
        ).strip()
        if not cleaned or len(cleaned.split()) > 1:
            return None
        answers.append(cleaned)
    normalized_answers = [_compact(answer) for answer in answers]
    if any(not value for value in normalized_answers) or len(normalized_answers) != len(
        set(normalized_answers)
    ):
        return None
    return tuple(answers)


def _line_groups(words: Sequence[_OCRWord]) -> tuple[tuple[_OCRWord, ...], ...]:
    groups: OrderedDict[tuple[int, int, int, int], list[_OCRWord]] = OrderedDict()
    for word in words:
        groups.setdefault(word.line_key, []).append(word)
    return tuple(
        tuple(sorted(group, key=lambda item: item.bbox.left))
        for group in groups.values()
    )


def _phrase_spans(words: Sequence[_OCRWord], phrase: str) -> tuple[_BBox, ...]:
    expected = _compact(phrase)
    if not expected:
        return ()
    spans: list[_BBox] = []
    for line in _line_groups(words):
        clean = [word for word in line if _compact(word.text) not in {"|", "｜"}]
        for start in range(len(clean)):
            text = ""
            for end in range(start, min(len(clean), start + 12)):
                text += clean[end].text
                compact = _compact(text)
                if compact == expected:
                    bbox = _bbox_union(word.bbox for word in clean[start : end + 1])
                    if bbox is not None:
                        spans.append(bbox)
                    break
                if len(compact) > len(expected) + 4:
                    break
    unique: list[_BBox] = []
    for span in spans:
        if span not in unique:
            unique.append(span)
    return tuple(unique)


def _mask_runs(scores: Any, threshold: float, minimum: int = 1) -> list[tuple[int, int]]:
    return _boolean_runs(list(scores >= threshold), minimum)


def _ordinal_value(text: str) -> tuple[str, Decimal] | None:
    value = _compact(text).strip("|:：.．")
    japanese = {"低": 1, "中": 2, "高": 3}
    english = {
        "low": 1,
        "mid": 2,
        "medium": 2,
        "mid-high": 3,
        "high": 3,
    }
    if value in japanese:
        return "ja_low_mid_high", Decimal(japanese[value])
    if value in english:
        return "en_low_mid_high", Decimal(english[value])
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return ("numeric", number) if number.is_finite() else None


def _colored_ink_bbox(page: _PageEvidence, cell: _BBox) -> _BBox | None:
    image = _open_rgb(page)
    if image is None:
        return None
    try:
        import numpy as np

        rgb = np.asarray(image, dtype=np.uint8)
    except Exception:
        return None
    pad = max(3, round(min(cell.width, cell.height) * 0.025))
    x0, y0 = cell.left + pad, cell.top + pad
    x1, y1 = cell.right - pad, cell.bottom - pad
    if x1 <= x0 or y1 <= y0:
        return None
    sample = rgb[y0:y1, x0:x1].astype(np.float32)
    maximum = sample.max(axis=2)
    minimum = sample.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1.0)
    luminance = sample.mean(axis=2)
    mask = (saturation >= 0.075) & (luminance < 218.0)
    ys, xs = np.where(mask)
    if len(xs) < 16:
        return None
    bbox = _BBox(
        x0 + int(xs.min()),
        y0 + int(ys.min()),
        x0 + int(xs.max()) + 1,
        y0 + int(ys.max()) + 1,
    )
    if bbox.width > cell.width * 0.70 or bbox.height > cell.height * 0.80:
        return None
    return bbox


def _hint_ordinal(
    page: _PageEvidence,
    cell: _BBox,
) -> tuple[str, Decimal, str] | None:
    values: dict[tuple[str, Decimal], str] = {}
    for run in page.hint_runs:
        for line in run:
            if not (
                cell.left <= line.bbox.center_x <= cell.right
                and cell.top <= line.bbox.center_y <= cell.bottom
            ):
                continue
            ordinal = _ordinal_value(line.text)
            if ordinal is not None:
                values.setdefault(ordinal, line.text)
    if len(values) != 1:
        return None
    (family, rank), text = next(iter(values.items()))
    return family, rank, text


def _cell_has_raster_content(rgb: Any, gray: Any, cell: _BBox) -> bool:
    """Detect source-pixel content inside a cell without consulting OCR."""

    import numpy as np

    inset_x = max(3, round(cell.width * 0.03))
    inset_y = max(3, round(cell.height * 0.08))
    left = cell.left + inset_x
    right = cell.right - inset_x
    top = cell.top + inset_y
    bottom = cell.bottom - inset_y
    if right <= left or bottom <= top:
        return False
    region_rgb = rgb[top:bottom, left:right]
    region_gray = gray[top:bottom, left:right]
    chroma = region_rgb.max(axis=2).astype(np.int16) - region_rgb.min(
        axis=2
    ).astype(np.int16)
    ink = (region_gray < 145.0) | ((chroma > 25) & (region_gray < 225.0))
    minimum = max(12, round(ink.size * 0.002))
    return int(ink.sum()) >= minimum


def _table_rows(
    page: _PageEvidence,
    target_header: str,
    metric_header: str,
) -> tuple[_TableRowEvidence, ...] | None:
    import numpy as np

    ocr = _ensure_page_ocr(page)
    image = _open_rgb(page)
    if ocr is None or image is None:
        return None
    target_spans = _phrase_spans(ocr.words, target_header)
    metric_spans = _phrase_spans(ocr.words, metric_header)
    if len(target_spans) != 1 or len(metric_spans) != 1:
        return None
    target_span, metric_span = target_spans[0], metric_spans[0]
    if (
        target_span.center_x >= metric_span.center_x
        or abs(target_span.center_y - metric_span.center_y)
        > max(target_span.height, metric_span.height)
    ):
        return None
    rgb = np.asarray(image, dtype=np.uint8)
    gray = rgb.astype(np.float32).mean(axis=2)
    scan_top = max(0, min(target_span.top, metric_span.top) - round(page.height * 0.04))
    scan_bottom = min(page.height, round(page.height * 0.92))
    vertical_scores = (gray[scan_top:scan_bottom] < 105.0).mean(axis=0)
    vertical_runs = _mask_runs(vertical_scores, 0.30)
    vertical_centers = [round((start + end - 1) / 2) for start, end in vertical_runs]
    between = [
        value
        for value in vertical_centers
        if target_span.right < value < metric_span.left
    ]
    to_right = [value for value in vertical_centers if value > metric_span.right]
    if len(between) != 1 or not to_right:
        return None
    separator = between[0]
    right_separator = min(to_right)
    if right_separator <= separator + max(10, metric_span.width // 2):
        return None
    x_floor = max(0, target_span.left - round(page.width * 0.16))
    horizontal_scores = (gray[:, x_floor:right_separator] < 105.0).mean(axis=1)
    horizontal_runs = _mask_runs(horizontal_scores, 0.55)
    horizontal_centers = [round((start + end - 1) / 2) for start, end in horizontal_runs]
    header_bottom = max(target_span.bottom, metric_span.bottom)
    row_lines = [
        value
        for value in horizontal_centers
        if value > header_bottom + max(2, round(page.height * 0.006))
    ]
    if len(row_lines) < 2:
        return None
    first_line = row_lines[0]
    band = gray[max(0, first_line - 2) : min(page.height, first_line + 3), :separator]
    horizontal_pixels = (band < 105.0).mean(axis=0) >= 0.40
    left_runs = _boolean_runs(list(horizontal_pixels), 3)
    if not left_runs:
        return None
    table_left = min(start for start, _ in left_runs)
    gaps = [right - left for left, right in zip(row_lines, row_lines[1:])]
    typical_gap = int(round(float(np.median(gaps)))) if gaps else round(page.height * 0.1)
    intervals = list(zip(row_lines, row_lines[1:]))
    if not intervals:
        return None
    tail_top = row_lines[-1]
    tail_bottom = min(page.height, tail_top + typical_gap)
    if tail_bottom - tail_top >= max(12, typical_gap * 0.45):
        tail_target = _BBox(table_left, tail_top, separator, tail_bottom)
        tail_metric = _BBox(separator, tail_top, right_separator, tail_bottom)
        if _cell_has_raster_content(
            rgb,
            gray,
            tail_target,
        ) or _cell_has_raster_content(rgb, gray, tail_metric):
            intervals.append((tail_top, tail_bottom))
    rows: list[_TableRowEvidence] = []
    for top, bottom in intervals:
        if bottom - top < max(12, typical_gap * 0.45):
            return None
        target_words = tuple(
            word
            for word in ocr.words
            if table_left < word.bbox.center_x < separator
            and top < word.bbox.center_y < bottom
            and _compact(word.text) not in {"|", "｜"}
        )
        metric_words = tuple(
            word
            for word in ocr.words
            if separator < word.bbox.center_x < right_separator
            and top < word.bbox.center_y < bottom
            and _compact(word.text) not in {"|", "｜"}
        )
        if not target_words or not metric_words:
            return None
        target_cell = _BBox(table_left, top, separator, bottom)
        metric_cell = _BBox(separator, top, right_separator, bottom)
        ink_bbox = _colored_ink_bbox(page, metric_cell)
        metric_bbox = ink_bbox or _bbox_union(word.bbox for word in metric_words)
        if metric_bbox is None:
            return None
        metric_crop = _crop_image(page, metric_bbox, padding=2)
        metric_text = (
            _crop_consensus(metric_crop, (8, 10, 13), minimum_votes=2)
            if metric_crop is not None
            else None
        )
        ordinal_evidence: list[tuple[str, Decimal, str]] = []
        cropped_ordinal = _ordinal_value(metric_text or "")
        if cropped_ordinal is not None:
            ordinal_evidence.append((*cropped_ordinal, metric_text or ""))
        word_text = "".join(
            word.text for word in sorted(metric_words, key=lambda word: word.sequence)
        )
        word_ordinal = _ordinal_value(word_text)
        if word_ordinal is not None:
            ordinal_evidence.append((*word_ordinal, word_text))
        hinted = _hint_ordinal(page, metric_cell)
        if hinted is not None:
            ordinal_evidence.append(hinted)
        values = {(family, rank) for family, rank, _ in ordinal_evidence}
        if len(values) != 1:
            return None
        family, rank = next(iter(values))
        metric_text = next(
            text
            for candidate_family, candidate_rank, text in ordinal_evidence
            if (candidate_family, candidate_rank) == (family, rank)
        )
        rows.append(
            _TableRowEvidence(
                target_bbox=target_cell,
                metric_bbox=metric_cell,
                target_words=target_words,
                metric_words=metric_words,
                metric_text=metric_text or "",
                metric_family=family,
                metric_rank=rank,
            )
        )
    if len(rows) != len(intervals):
        return None
    return tuple(rows) if rows else None


def _table_extreme_answer(
    page: _PageEvidence,
    target_header: str,
    metric_header: str,
    extremum: str,
) -> str | None:
    rows = _table_rows(page, target_header, metric_header)
    if not rows or len({row.metric_family for row in rows}) != 1:
        return None
    ranks = [row.metric_rank for row in rows]
    extreme = max(ranks) if extremum == "最も高い" else min(ranks)
    winners = [row for row in rows if row.metric_rank == extreme]
    if len(winners) != 1:
        return None
    winner = winners[0]
    crop = _crop_image(page, winner.target_bbox, padding=-3)
    if crop is None:
        return None
    answer = _crop_consensus(crop, (3, 4, 6), minimum_votes=2)
    if answer is None:
        return None
    answer = answer.strip()
    word_reading = "".join(
        word.text
        for word in sorted(
            winner.target_words,
            key=lambda word: word.sequence,
        )
    )
    if not word_reading or _compact(answer) != _compact(word_reading):
        return None
    return answer


def _decision(
    answer: str,
    source_path: Path,
    source_root: Path,
    operation_count: int,
) -> StructuredCandidateDecision | None:
    if not answer.strip():
        return None
    try:
        from structured_candidate import (
            StructuredCandidateAnswer,
            StructuredCandidateDecision,
        )
    except Exception:
        return None
    relative = _relative_source(source_path, source_root)
    source_sha = _source_sha256(source_path)
    if relative is None or source_sha is None:
        return None
    return StructuredCandidateDecision(
        "resolved",
        "certified_pdf_visual",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(relative,),
            source_sha256=source_sha,
            operation_count=operation_count,
            output_count=1,
        ),
    )


def decide_pdf_visual(engine: Any, question: str) -> StructuredCandidateDecision | None:
    """Resolve a supported PDF visual question, otherwise fail closed."""

    contract = graph_contract_for_pdf_question(question)
    if contract is None:
        return None
    bindings = contract["bindings"]
    try:
        source_root = Path(getattr(engine, "source_root", "")).resolve()
    except (OSError, TypeError, ValueError):
        return None
    if contract["rule_id"] == "pdf_meeting_section_page_number":
        bound = _bound_meeting_source(
            engine,
            bindings["location"],
            bindings["meeting_id"],
        )
        if bound is None:
            return None
        source, pages = bound
        selected_page = _meeting_section_page(pages, bindings["section"])
        if selected_page is None or not _selected_page_matches_source(
            selected_page, source
        ):
            return None
        return _decision(
            str(selected_page.page_number),
            source,
            source_root,
            len(_MEETING_SECTION_PAGE_OPERATORS),
        )
    paths = _matching_report_paths(
        engine,
        bindings["location"],
        bindings["report_kind"],
    )
    if len(paths) != 1:
        return None
    source = paths[0]
    source_sha = _source_sha256(source)
    if source_sha is None:
        return None
    pages = _all_pdf_pages(engine, source, source_sha)
    if not pages:
        return None
    if contract["rule_id"] == "pdf_page_inline_marker_word_projection":
        matches: list[_PageEvidence] = []
        for page in pages:
            state = _page_matches_title(page, bindings["page_title"])
            if state is None:
                return None
            if state:
                matches.append(page)
        if len(matches) != 1:
            return None
        selected_page = matches[0]
        if not _selected_page_matches_source(selected_page, source):
            return None
        if selected_page.materialized_path is not None:
            fresh_title = _fresh_page_matches_title(
                selected_page,
                bindings["page_title"],
            )
            if fresh_title is not True:
                return None
        words = _marked_words(selected_page)
        if not words:
            return None
        return _decision(
            "、".join(words),
            source,
            source_root,
            len(_MARKER_OPERATORS),
        )
    if contract["rule_id"] == "pdf_phase_effort_range_sum":
        selected = _phase_effort_page(
            pages,
            bindings["phase_left"],
            bindings["phase_right"],
        )
        if selected is None:
            return None
        selected_page, left, right = selected
        if not _selected_page_matches_source(selected_page, source):
            return None
        if not _fresh_effort_range_agrees(
            selected_page, left
        ) or not _fresh_effort_range_agrees(selected_page, right):
            return None
        lower = left.lower + right.lower
        upper = left.upper + right.upper
        if not 0 < lower <= upper <= 100_000:
            return None
        answer = f"{lower}時間" if lower == upper else f"{lower}〜{upper}時間"
        return _decision(
            answer,
            source,
            source_root,
            len(_PHASE_EFFORT_SUM_OPERATORS),
        )
    if contract["rule_id"] != "pdf_table_ordinal_argextreme_projection":
        return None
    candidate_pages: list[_PageEvidence] = []
    for page in pages:
        state = _runs_contain_headers(
            page,
            bindings["target"],
            bindings["metric"],
        )
        if state is None:
            return None
        if state:
            candidate_pages.append(page)
    if len(candidate_pages) != 1:
        return None
    if not _selected_page_matches_source(candidate_pages[0], source):
        return None
    answer = _table_extreme_answer(
        candidate_pages[0],
        bindings["target"],
        bindings["metric"],
        bindings["extremum"],
    )
    if answer is None:
        return None
    return _decision(answer, source, source_root, len(_TABLE_OPERATORS))


__all__ = [
    "PDF_VISUAL_RULE_VERSION",
    "decide_pdf_visual",
    "graph_contract_for_pdf_question",
]
