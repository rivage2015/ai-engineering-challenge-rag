"""Fail-closed spatial rules for floor maps embedded in PPTX files.

The source observer is deliberately question independent: it receives complete
visible slide rasters, never a question, person name, or requested attribute.
This module then resolves person-relative geometry.  In image coordinates
(``x`` right, ``y`` down), a person's right vector is ``(-facing_y, facing_x)``;
viewer-right is never substituted for person-right.

Only a narrow, auditable PPTX subset is rasterized here: ordered visible slides,
stretched raster pictures, and opaque rectangular masks.  Unsupported drawing
operations, unsafe relationships, malformed raster/EMF data, ambiguous aliases,
unknown orientation, or non-unique joins all return a hold decision.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import math
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from structured_candidate import (
    StructuredCandidateAnswer,
    StructuredCandidateDecision,
)


PPTX_SPATIAL_RULE_VERSION = "0.1"

PPTX_PERSON_RIGHT = re.compile(
    r"^(?P<location>[^\r\n、。]+?)にある"
    r"(?P<container>[^\r\n、。]+?)において、"
    r"(?P<person>[^\r\n、。]+?)さんから見て"
    r"右側に座っている人の名前をすべて挙げてください。?$"
)
PPTX_PERSON_OPPOSITE_ATTRIBUTE = re.compile(
    r"^(?P<location>[^\r\n、。]+?)にある"
    r"(?P<container>[^\r\n、。]+?)において、"
    r"(?P<person>[^\r\n、。]+?)さんの向かいに座っている方の"
    r"(?P<attribute>[^\r\n、。]+?)を教えてください。?$"
)

_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_P = "{" + _PML_NS + "}"
_A = "{" + _DML_NS + "}"
_R = "{" + _REL_NS + "}"
_PR = "{" + _PKG_REL_NS + "}"

_SLIDE_REL = _REL_NS + "/slide"
_IMAGE_REL = _REL_NS + "/image"
_MAX_PPTX_BYTES = 256 * 1024 * 1024
_MAX_ZIP_ENTRIES = 10_000
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_RATIO = 200.0
_MAX_XML_BYTES = 32 * 1024 * 1024
_MAX_PIXELS = 50_000_000
_MAX_EMF_RECORDS = 100_000
_XML_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTRIBUTE_SUFFIXES = ("番号",)


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class SlideRaster:
    """Question-independent visible state of one active slide."""

    ordinal: int
    slide_member: str
    png_bytes: bytes
    width: int
    height: int
    composite_sha256: str
    source_sha256: str
    media_members: tuple[str, ...]
    media_sha256s: tuple[tuple[str, str], ...]
    opaque_masks: tuple[tuple[float, float, float, float], ...]


@dataclass(frozen=True)
class SeatEvidence:
    person: str
    pod: str
    center: Point
    facing: Point | None
    slide_ordinal: int
    composite_sha256: str
    evidence_id: str


@dataclass(frozen=True)
class DirectoryEvidence:
    person: str
    attributes: tuple[tuple[str, str], ...]
    evidence_id: str


@dataclass(frozen=True)
class SpatialObservation:
    source_sha256: str
    question_independent: bool
    status: str
    seats: tuple[SeatEvidence, ...]
    directory: tuple[DirectoryEvidence, ...]
    observer: str


SpatialObserver = Callable[[tuple[SlideRaster, ...]], SpatialObservation]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", _normalized(value))


def _graph_contract(
    question: str,
    match: re.Match[str],
    rule_id: str,
    operators: Sequence[str],
    *,
    cardinality: str,
) -> dict[str, Any]:
    bindings = {key: value for key, value in match.groupdict().items() if value}
    nodes: list[dict[str, Any]] = []
    previous = "input_question"
    for index, operator in enumerate(operators, 1):
        output = f"value_{index:03d}"
        nodes.append(
            {
                "operation_id": f"op_{index:03d}_{operator}",
                "operator": operator,
                "input_refs": [previous],
                "output_ref": output,
            }
        )
        previous = output
    core = {
        "pptx_spatial_rule_version": PPTX_SPATIAL_RULE_VERSION,
        "rule_id": rule_id,
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        "bindings": bindings,
        "scope": {
            "location": bindings["location"],
            "container": bindings["container"],
            "source_channel": "pptx_visible_composite_spatial_observation",
            "coordinate_system": "image_top_left_x_right_y_down",
            "reference_frame": "subject_facing",
            "orientation_required": True,
        },
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
                {"from": nodes[i - 1]["output_ref"], "to": nodes[i]["operation_id"]}
                for i in range(1, len(nodes))
            ],
        },
        "requested_output": {
            "source_operation_ref": nodes[-1]["operation_id"],
            "cardinality": cardinality,
            "answer_shape": {
                "container": "list" if cardinality == "all" else "scalar",
                "value_type": "string",
                "unit": None,
            },
        },
    }
    return {
        "graph_contract_id": "pptx_spatial_"
        + hashlib.sha256(_canonical_json(core).encode("utf-8")).hexdigest()[:32],
        **core,
    }


_RIGHT_OPERATORS = (
    "retrieve",
    "resolve_glossary_aliases",
    "parse_visible_slide_stack",
    "observe_all_seats_question_independently",
    "bind_unique_person",
    "require_subject_orientation",
    "rotate_facing_to_person_right",
    "select_same_pod_right_half_plane",
    "verify_nonempty_unique",
    "project_names",
)
_OPPOSITE_OPERATORS = (
    "retrieve",
    "resolve_glossary_aliases",
    "parse_visible_slide_stack",
    "observe_all_seats_question_independently",
    "bind_unique_person",
    "require_subject_orientation",
    "select_collinear_opposite_seat",
    "join_unique_directory_attribute",
    "verify_unique",
    "project_attribute",
)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str):
        return None
    match = PPTX_PERSON_RIGHT.fullmatch(question)
    if match:
        return _graph_contract(
            question,
            match,
            "pptx_person_facing_right_names",
            _RIGHT_OPERATORS,
            cardinality="all",
        )
    match = PPTX_PERSON_OPPOSITE_ATTRIBUTE.fullmatch(question)
    if match:
        return _graph_contract(
            question,
            match,
            "pptx_opposite_seat_attribute",
            _OPPOSITE_OPERATORS,
            cardinality="single",
        )
    return None


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    if not isinstance(contract, Mapping):
        return False
    expected = graph_contract_for_question(question)
    if expected is None:
        return False
    try:
        return _canonical_json(expected) == _canonical_json(contract)
    except (TypeError, ValueError):
        return False


def _hold(reason: str) -> StructuredCandidateDecision:
    return StructuredCandidateDecision("hold", reason)


def _safe_root(engine: Any) -> Path | None:
    try:
        root = Path(engine.source_root)
        if not root.is_dir() or root.is_symlink():
            return None
        return root.resolve()
    except (AttributeError, OSError, RuntimeError, TypeError):
        return None


def _has_symlink_component(path: Path, root: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == root:
            return False
        if root not in current.parents:
            return True
        current = current.parent


def _alias_values(
    value: str,
    glossary: Any,
    *,
    allow_multiple: bool = False,
) -> tuple[str, ...] | None:
    entries = getattr(glossary, "entries", {})
    if not isinstance(entries, Mapping):
        return (value,)
    canonical: set[str] = set()
    for alias, raw_values in entries.items():
        if _normalized(alias) != _normalized(value):
            continue
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes, bytearray)
        ):
            return None
        canonical.update(str(item).strip() for item in raw_values if str(item).strip())
    if len(canonical) > 1 and not allow_multiple:
        return None
    return (value, *tuple(sorted(canonical, key=lambda item: (_normalized(item), item))))


def _location_forms(value: str) -> tuple[str, ...]:
    forms = {_compact(value)}
    for suffix in ("フォルダ", "folder"):
        normalized_suffix = _compact(suffix)
        if next(iter(forms)).endswith(normalized_suffix):
            forms.add(next(iter(forms))[: -len(normalized_suffix)])
    return tuple(sorted(form for form in forms if form))


def _matching_pptx(engine: Any, location: str, container: str) -> tuple[Path, ...] | None:
    root = _safe_root(engine)
    if root is None:
        return None
    glossary = getattr(engine, "glossary", None)
    locations = _alias_values(location, glossary, allow_multiple=True)
    containers = _alias_values(container, glossary, allow_multiple=True)
    if locations is None or containers is None:
        return None
    location_forms = {form for value in locations for form in _location_forms(value)}
    container_forms = {_compact(value.removesuffix(".pptx")) for value in containers}
    matches: list[Path] = []
    try:
        for path in root.rglob("*.pptx"):
            if (
                not path.is_file()
                or path.name.startswith("~$")
                or path.stat().st_size > _MAX_PPTX_BYTES
                or _has_symlink_component(path, root)
            ):
                continue
            relative = path.relative_to(root)
            part_forms = {
                form
                for part in relative.parts[:-1]
                for form in _location_forms(part)
            }
            if not location_forms.intersection(part_forms):
                continue
            if _compact(path.stem) not in container_forms:
                continue
            matches.append(path.resolve())
    except (OSError, RuntimeError):
        return None
    return tuple(sorted(set(matches), key=lambda item: item.as_posix()))


def _safe_xml(data: bytes) -> ET.Element | None:
    if len(data) > _MAX_XML_BYTES:
        return None
    upper = data.upper()
    if any(token in upper for token in _XML_FORBIDDEN):
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _open_archive(path: Path) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]] | None:
    try:
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    if not infos or len(infos) > _MAX_ZIP_ENTRIES:
        archive.close()
        return None
    records: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        pure = PurePosixPath(info.filename)
        if (
            info.filename in records
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in info.filename
            or info.flag_bits & 1
            or info.file_size > _MAX_MEMBER_BYTES
        ):
            archive.close()
            return None
        if info.compress_size == 0:
            if info.file_size:
                archive.close()
                return None
        elif info.file_size / info.compress_size > _MAX_RATIO:
            archive.close()
            return None
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            archive.close()
            return None
        records[info.filename] = info
    return archive, records


def _safe_target(base_member: str, target: str) -> str | None:
    if not target or "\\" in target or target.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base_member), target))
    pure = PurePosixPath(joined)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return joined


def _relationship_map(
    archive: zipfile.ZipFile,
    records: Mapping[str, zipfile.ZipInfo],
    member: str,
) -> dict[str, tuple[str, str]] | None:
    pure = PurePosixPath(member)
    rels_member = str(pure.parent / "_rels" / (pure.name + ".rels"))
    if rels_member not in records:
        return None
    try:
        root = _safe_xml(archive.read(rels_member))
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    if root is None:
        return None
    result: dict[str, tuple[str, str]] = {}
    for rel in root.findall(_PR + "Relationship"):
        relationship_id = rel.get("Id")
        target = rel.get("Target")
        rel_type = rel.get("Type")
        if (
            not relationship_id
            or relationship_id in result
            or not target
            or not rel_type
            or rel.get("TargetMode") == "External"
        ):
            return None
        resolved = _safe_target(member, target)
        if resolved is None:
            return None
        result[relationship_id] = (rel_type, resolved)
    return result


def _validate_emf(data: bytes) -> bool:
    if len(data) < 108 or len(data) > _MAX_MEMBER_BYTES:
        return False
    position = 0
    records = 0
    saw_header = False
    saw_eof = False
    while position < len(data):
        if records >= _MAX_EMF_RECORDS or position + 8 > len(data):
            return False
        record_type = int.from_bytes(data[position : position + 4], "little")
        size = int.from_bytes(data[position + 4 : position + 8], "little")
        if size < 8 or size % 4 or position + size > len(data):
            return False
        if records == 0:
            if record_type != 1 or size < 88:
                return False
            signature = int.from_bytes(data[position + 40 : position + 44], "little")
            declared_size = int.from_bytes(data[position + 48 : position + 52], "little")
            if signature != 0x464D4520 or declared_size != len(data):
                return False
            saw_header = True
        elif record_type == 1:
            return False
        if record_type == 14:
            if size != 20 or position + size != len(data):
                return False
            saw_eof = True
        position += size
        records += 1
    return saw_header and saw_eof and position == len(data)


def _raster_image(data: bytes, suffix: str) -> Any | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > _MAX_PIXELS:
                return None
            actual = (source.format or "").casefold()
            expected = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(suffix)
            if expected is None or actual != expected:
                return None
            return source.convert("RGBA")
    except Exception:
        return None


def _transform(element: ET.Element) -> tuple[int, int, int, int] | None:
    xfrm = element.find("./" + _P + "spPr/" + _A + "xfrm")
    if xfrm is None:
        return None
    if xfrm.get("rot") not in (None, "0") or xfrm.get("flipH") == "1" or xfrm.get("flipV") == "1":
        return None
    off = xfrm.find(_A + "off")
    ext = xfrm.find(_A + "ext")
    if off is None or ext is None:
        return None
    try:
        values = tuple(int(value) for value in (off.get("x"), off.get("y"), ext.get("cx"), ext.get("cy")))
    except (TypeError, ValueError):
        return None
    if values[0] < 0 or values[1] < 0 or values[2] <= 0 or values[3] <= 0:
        return None
    return values  # type: ignore[return-value]


def _slide_rasters(path: Path) -> tuple[SlideRaster, ...] | None:
    opened = _open_archive(path)
    if opened is None:
        return None
    archive, records = opened
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        if "ppt/presentation.xml" not in records:
            return None
        presentation = _safe_xml(archive.read("ppt/presentation.xml"))
        relationships = _relationship_map(archive, records, "ppt/presentation.xml")
        if presentation is None or relationships is None:
            return None
        size = presentation.find(_P + "sldSz")
        if size is None:
            return None
        try:
            slide_width, slide_height = int(size.get("cx")), int(size.get("cy"))
        except (TypeError, ValueError):
            return None
        if slide_width <= 0 or slide_height <= 0:
            return None
        width_px = 1600
        height_px = round(width_px * slide_height / slide_width)
        if width_px * height_px > _MAX_PIXELS:
            return None
        slide_ids = presentation.find(_P + "sldIdLst")
        if slide_ids is None:
            return None
        output: list[SlideRaster] = []
        for ordinal, slide_id in enumerate(slide_ids.findall(_P + "sldId"), 1):
            rel_id = slide_id.get(_R + "id")
            relation = relationships.get(rel_id or "")
            if relation is None or relation[0] != _SLIDE_REL or relation[1] not in records:
                return None
            slide_member = relation[1]
            slide = _safe_xml(archive.read(slide_member))
            slide_relationships = _relationship_map(archive, records, slide_member)
            if slide is None or slide_relationships is None:
                return None
            if slide.get("show") == "0":
                continue
            sp_tree = slide.find("./" + _P + "cSld/" + _P + "spTree")
            if sp_tree is None:
                return None
            from PIL import Image, ImageDraw

            canvas = Image.new("RGBA", (width_px, height_px), (255, 255, 255, 255))
            media_members: list[str] = []
            media_sha256s: list[tuple[str, str]] = []
            opaque_masks: list[tuple[float, float, float, float]] = []
            rendered = False
            for element in list(sp_tree):
                if element.tag in {_P + "nvGrpSpPr", _P + "grpSpPr"}:
                    continue
                if element.tag == _P + "pic":
                    bounds = _transform(element)
                    blip = element.find("./" + _P + "blipFill/" + _A + "blip")
                    stretch = element.find("./" + _P + "blipFill/" + _A + "stretch/" + _A + "fillRect")
                    if bounds is None or blip is None or stretch is None:
                        return None
                    rel = slide_relationships.get(blip.get(_R + "embed", ""))
                    if rel is None or rel[0] != _IMAGE_REL or rel[1] not in records:
                        return None
                    member = rel[1]
                    data = archive.read(member)
                    suffix = PurePosixPath(member).suffix.casefold()
                    if suffix == ".emf":
                        if not _validate_emf(data):
                            return None
                        return None  # valid but not rasterized without a trusted renderer
                    image = _raster_image(data, suffix)
                    if image is None:
                        return None
                    x, y, cx, cy = bounds
                    box = (
                        round(x * width_px / slide_width),
                        round(y * height_px / slide_height),
                        round((x + cx) * width_px / slide_width),
                        round((y + cy) * height_px / slide_height),
                    )
                    if box[0] < 0 or box[1] < 0 or box[2] > width_px or box[3] > height_px:
                        return None
                    resized = image.resize((box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS)
                    canvas.alpha_composite(resized, (box[0], box[1]))
                    media_members.append(member)
                    media_sha256s.append((member, hashlib.sha256(data).hexdigest()))
                    rendered = True
                    continue
                if element.tag == _P + "sp":
                    hidden = element.find("./" + _P + "nvSpPr/" + _P + "cNvPr")
                    if hidden is not None and hidden.get("hidden") == "1":
                        continue
                    text = "".join(item.text or "" for item in element.findall(".//" + _A + "t")).strip()
                    bounds = _transform(element)
                    geometry = element.find("./" + _P + "spPr/" + _A + "prstGeom")
                    color = element.find("./" + _P + "spPr/" + _A + "solidFill/" + _A + "srgbClr")
                    if text or bounds is None or geometry is None or geometry.get("prst") != "rect" or color is None:
                        return None
                    alpha = color.find(_A + "alpha")
                    if alpha is not None and alpha.get("val") != "100000":
                        return None
                    raw_color = color.get("val", "")
                    if not re.fullmatch(r"[0-9A-Fa-f]{6}", raw_color):
                        return None
                    x, y, cx, cy = bounds
                    box = (
                        round(x * width_px / slide_width),
                        round(y * height_px / slide_height),
                        round((x + cx) * width_px / slide_width),
                        round((y + cy) * height_px / slide_height),
                    )
                    if box[0] < 0 or box[1] < 0 or box[2] > width_px or box[3] > height_px:
                        return None
                    rgb = tuple(int(raw_color[i : i + 2], 16) for i in (0, 2, 4))
                    ImageDraw.Draw(canvas).rectangle(box, fill=(*rgb, 255))
                    opaque_masks.append(
                        (
                            x / slide_width,
                            y / slide_height,
                            (x + cx) / slide_width,
                            (y + cy) / slide_height,
                        )
                    )
                    rendered = True
                    continue
                return None
            if not rendered or not media_members:
                return None
            payload = io.BytesIO()
            canvas.convert("RGB").save(payload, format="PNG", optimize=False)
            png = payload.getvalue()
            output.append(
                SlideRaster(
                    ordinal=ordinal,
                    slide_member=slide_member,
                    png_bytes=png,
                    width=width_px,
                    height=height_px,
                    composite_sha256=hashlib.sha256(png).hexdigest(),
                    source_sha256=source_sha,
                    media_members=tuple(media_members),
                    media_sha256s=tuple(media_sha256s),
                    opaque_masks=tuple(opaque_masks),
                )
            )
        return tuple(output) if output else None
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, ValueError):
        return None
    finally:
        archive.close()


def _valid_point(point: Point) -> bool:
    return (
        isinstance(point, Point)
        and math.isfinite(point.x)
        and math.isfinite(point.y)
        and 0.0 <= point.x <= 1.0
        and 0.0 <= point.y <= 1.0
    )


@dataclass(frozen=True)
class _Label:
    person: str
    role: str
    extension: str
    center_x: float
    center_y: float
    evidence_id: str


def _bbox_center(raw: object) -> tuple[float, float] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 4:
        return None
    try:
        x, y, width, height = (float(value) / 1000.0 for value in raw)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        return None
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.02 or y + height > 1.02:
        return None
    return x + width / 2.0, y + height / 2.0


def _masked(center: tuple[float, float], masks: Sequence[tuple[float, float, float, float]]) -> bool:
    return any(left <= center[0] <= right and top <= center[1] <= bottom for left, top, right, bottom in masks)


_NAME_ROLE = re.compile(
    r"(?P<name>(?:[\u3400-\u9fff々〆ヶ]{1,8}|[A-Za-z][A-Za-z .'-]{0,31}?))"
    r"\s*[\(（](?P<role>[A-Za-z]{2,8})"
)
_SUPPORTED_SEAT_ROLES = frozenset({"EXEC", "PM", "DS", "BA", "QA", "DE"})


def _extension_text(text: str) -> str | None:
    digits = "".join(re.findall(r"[0-9]", unicodedata.normalize("NFKC", text)))
    if len(digits) == 4:
        return digits
    # OCR commonly reads the telephone glyph immediately before a four-digit EXT.
    if len(digits) == 5:
        return digits[-4:]
    return None


def _artifact_record(
    engine: Any,
    path: Path,
    slides: Sequence[SlideRaster],
) -> Mapping[str, Any] | None:
    configured = getattr(engine, "pptx_spatial_observation_path", None)
    artifact = Path(configured) if configured is not None else Path(__file__).resolve().parents[1] / "artifacts" / "ocr-observation-v1" / "ocr-observations-full.jsonl"
    try:
        if not artifact.is_file() or artifact.is_symlink() or artifact.stat().st_size > 64 * 1024 * 1024:
            return None
        if len(slides) != 1 or len(slides[0].media_sha256s) != 1:
            return None
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        media = {member: digest for slide in slides for member, digest in slide.media_sha256s}
        if tuple(media.items()) != slides[0].media_sha256s:
            return None
        matches: list[Mapping[str, Any]] = []
        with artifact.open(encoding="utf-8") as handle:
            for raw_line in handle:
                if len(raw_line) > 8 * 1024 * 1024:
                    return None
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    return None
                if not isinstance(record, Mapping):
                    return None
                source = record.get("source")
                origin = record.get("origin")
                provenance = record.get("provenance")
                if not isinstance(source, Mapping) or not isinstance(origin, Mapping) or not isinstance(provenance, Mapping):
                    continue
                if source.get("sha256") != source_hash or provenance.get("question_independent") is not True:
                    continue
                member = origin.get("member_path")
                member_hash = origin.get("member_sha256")
                if not isinstance(member, str) or media.get(member) != member_hash:
                    continue
                matches.append(record)
        return matches[0] if len(matches) == 1 else None
    except (OSError, RuntimeError, UnicodeError):
        return None


def _labels_from_artifact(
    record: Mapping[str, Any],
    slide: SlideRaster,
) -> tuple[_Label, ...] | None:
    runs = record.get("engine_runs")
    if not isinstance(runs, Sequence) or isinstance(runs, (str, bytes)):
        return None
    names: list[tuple[float, float, str, str, float, str]] = []
    extensions: list[tuple[float, float, str, str]] = []
    engine_digests: set[str] = set()
    independence_groups: set[str] = set()
    for run in runs:
        if not isinstance(run, Mapping) or run.get("status") != "completed":
            continue
        run_id = str(run.get("run_id", "")).strip()
        engine = run.get("engine")
        lines = run.get("lines")
        if (
            not run_id
            or not isinstance(engine, Mapping)
            or not isinstance(lines, Sequence)
            or isinstance(lines, (str, bytes))
        ):
            return None
        digest = engine.get("digest")
        group = engine.get("independence_group")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or not isinstance(group, str) or not group.strip():
            return None
        if digest in engine_digests or group in independence_groups:
            return None
        engine_digests.add(digest)
        independence_groups.add(group)
        for line in lines:
            if not isinstance(line, Mapping):
                return None
            center = _bbox_center(line.get("bbox"))
            raw_text = line.get("raw_text")
            if center is None or not isinstance(raw_text, str) or _masked(center, slide.opaque_masks):
                continue
            try:
                confidence = float(line.get("confidence", 0.0))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
                return None
            name_match = _NAME_ROLE.search(unicodedata.normalize("NFKC", raw_text))
            if name_match is not None:
                role = name_match["role"].upper()
                if role not in _SUPPORTED_SEAT_ROLES:
                    continue
                names.append((center[0], center[1], name_match["name"], role, confidence, f"{run_id}:{line.get('line_id', '')}"))
                continue
            extension = _extension_text(raw_text)
            if extension is not None:
                extensions.append((center[0], center[1], extension, f"{run_id}:{line.get('line_id', '')}"))
    if len(engine_digests) < 2 or not names or not extensions:
        return None

    # Merge spatially coincident OCR readings without looking at any question token.
    clusters: list[list[tuple[float, float, str, str, float, str]]] = []
    for item in sorted(names, key=lambda value: (value[1], value[0], -value[4])):
        candidates = [cluster for cluster in clusters if abs(cluster[0][0] - item[0]) <= 0.04 and abs(cluster[0][1] - item[1]) <= 0.04]
        if len(candidates) > 1:
            return None
        if candidates:
            candidates[0].append(item)
        else:
            clusters.append([item])
    labels: list[_Label] = []
    for cluster in clusters:
        ranked = sorted(cluster, key=lambda value: (-value[4], _normalized(value[2]), value[5]))
        best = ranked[0]
        if len(ranked) > 1 and ranked[1][4] == best[4] and (_compact(ranked[1][2]), ranked[1][3]) != (_compact(best[2]), best[3]):
            return None
        ext_candidates = [
            item for item in extensions
            if 0 < best[1] - item[1] <= 0.09 and abs(best[0] - item[0]) <= 0.08
        ]
        if not ext_candidates:
            return None
        ext_candidates.sort(key=lambda item: (abs(best[0] - item[0]) + abs(best[1] - item[1]), item[2], item[3]))
        chosen_distance = abs(best[0] - ext_candidates[0][0]) + abs(best[1] - ext_candidates[0][1])
        tied = [item for item in ext_candidates if abs((abs(best[0] - item[0]) + abs(best[1] - item[1])) - chosen_distance) <= 1e-9]
        if len({item[2] for item in tied}) != 1:
            return None
        labels.append(_Label(best[2], best[3], ext_candidates[0][2], best[0], best[1], best[5] + "+" + ext_candidates[0][3]))
    identities = {(_compact(label.person), label.role, label.extension) for label in labels}
    evidence_ids = {label.evidence_id for label in labels}
    if len(identities) != len(labels) or len(evidence_ids) != len(labels):
        return None
    return tuple(labels)


def _topology_fingerprint(
    slide: SlideRaster,
    groups: Sequence[Sequence[_Label]],
) -> tuple[str, tuple[tuple[Point, ...], ...]] | None:
    """Prove a four-direction desk island in the actual visible raster.

    Colour alone is deliberately insufficient: each selected surface must occupy
    one unique sector around an inferred pod centre, with balanced radii,
    adjacent gaps, and two opposing pairs.  This rejects four otherwise-valid
    OCR labels painted over unrelated or collinear coloured rectangles.
    """

    try:
        from PIL import Image

        with Image.open(io.BytesIO(slide.png_bytes)) as source:
            image = source.convert("HSV").resize((400, 225))
    except Exception:
        return None
    width, height = image.size
    pixels = image.load()
    # Hue bins keep touching but differently coloured desk surfaces separate.
    # A small set of luminance bins admits an achromatic desk without treating
    # the whole pale floor as one surface.
    components: list[tuple[int, float, float, float, float, str]] = []

    def collect_component_bin(kind: str, predicate: Callable[[int, int, int], bool]) -> None:
        active = {
            (x, y)
            for y in range(round(height * 0.32), round(height * 0.72))
            for x in range(width)
            if predicate(*pixels[x, y])
        }
        while active:
            seed = active.pop()
            stack = [seed]
            points = [seed]
            while stack:
                x, y = stack.pop()
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if (nx, ny) in active:
                            active.remove((nx, ny))
                            stack.append((nx, ny))
                            points.append((nx, ny))
            if len(points) < 70:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            box_width = (max(xs) - min(xs) + 1) / width
            box_height = (max(ys) - min(ys) + 1) / height
            if not (0.035 <= box_width <= 0.16 and 0.035 <= box_height <= 0.145):
                continue
            components.append(
                (
                    len(points),
                    sum(xs) / len(xs) / width,
                    sum(ys) / len(ys) / height,
                    box_width,
                    box_height,
                    kind,
                )
            )

    for hue_bin in range(12):
        lower = hue_bin * 256 // 12
        upper = (hue_bin + 1) * 256 // 12
        collect_component_bin(
            f"h{hue_bin}",
            lambda hue, saturation, value, lower=lower, upper=upper: (
                saturation >= 65 and 45 <= value <= 245 and lower <= hue < upper
            ),
        )
    for value_bin, (lower, upper) in enumerate(
        ((55, 85), (85, 115), (115, 145), (145, 175), (175, 205), (205, 235))
    ):
        collect_component_bin(
            f"g{value_bin}",
            lambda _hue, saturation, value, lower=lower, upper=upper: (
                saturation < 65 and lower <= value < upper
            ),
        )
    deduplicated: list[tuple[int, float, float, float, float, str]] = []
    for component in sorted(components, key=lambda item: (-item[0], item[1], item[2], item[5])):
        if any(
            abs(component[1] - kept[1]) <= 0.005
            and abs(component[2] - kept[2]) <= 0.005
            and abs(component[3] - kept[3]) <= 0.01
            and abs(component[4] - kept[4]) <= 0.01
            for kept in deduplicated
        ):
            continue
        deduplicated.append(component)
    components = deduplicated
    if not components:
        return None

    def island_geometry(
        candidates: Sequence[tuple[int, float, float, float, float, str]],
        expected_center: Point,
    ) -> tuple[float, dict[str, Any], tuple[Point, ...]] | None:
        center_x = sum(item[1] for item in candidates) / 4.0
        center_y = sum(item[2] for item in candidates) / 4.0
        radii = [math.hypot(item[1] - center_x, item[2] - center_y) for item in candidates]
        if (
            min(radii) < 0.035
            or max(radii) > 0.105
            or max(radii) / min(radii) > 1.8
        ):
            return None
        angles = sorted(
            math.atan2(item[2] - center_y, item[1] - center_x)
            for item in candidates
        )
        gaps = [
            (angles[(index + 1) % 4] - angles[index]) % (2.0 * math.pi)
            for index in range(4)
        ]
        if min(gaps) < math.radians(40) or max(gaps) > math.radians(140):
            return None
        opposite_errors = [
            abs(
                ((angles[(index + 2) % 4] - angles[index]) % (2.0 * math.pi))
                - math.pi
            )
            for index in range(2)
        ]
        if max(opposite_errors) > math.radians(30):
            return None

        # Infer the island rotation modulo 90 degrees, then require exactly one
        # component in each north/west/south/east topological sector.
        phase = math.atan2(
            sum(math.sin(4.0 * angle) for angle in angles),
            sum(math.cos(4.0 * angle) for angle in angles),
        ) / 4.0
        sectors = [round((angle - phase) / (math.pi / 2.0)) % 4 for angle in angles]
        if len(set(sectors)) != 4:
            return None
        sector_deviations = [
            abs(
                (
                    angle - (phase + sector * math.pi / 2.0) + math.pi
                )
                % (2.0 * math.pi)
                - math.pi
            )
            for angle, sector in zip(angles, sectors)
        ]
        if max(sector_deviations) > math.radians(30):
            return None

        ordered = sorted(
            candidates,
            key=lambda item: round(
                (math.atan2(item[2] - center_y, item[1] - center_x) - phase)
                / (math.pi / 2.0)
            )
            % 4,
        )
        adjacent_distances = [
            math.hypot(
                ordered[(index + 1) % 4][1] - ordered[index][1],
                ordered[(index + 1) % 4][2] - ordered[index][2],
            )
            for index in range(4)
        ]
        opposite_distances = [
            math.hypot(ordered[index + 2][1] - ordered[index][1], ordered[index + 2][2] - ordered[index][2])
            for index in range(2)
        ]
        if (
            min(adjacent_distances) < 0.05
            or max(adjacent_distances) / min(adjacent_distances) > 2.0
            or max(opposite_distances) / min(opposite_distances) > 1.45
        ):
            return None
        mean_radius = sum(radii) / 4.0
        gray_count = sum(item[5].startswith("g") for item in candidates)
        score = (
            gray_count * 2.0
            + abs(center_x - expected_center.x)
            + abs(center_y - expected_center.y)
            + 10.0 * sum((radius - mean_radius) ** 2 for radius in radii)
            + sum((gap - math.pi / 2.0) ** 2 for gap in gaps)
        )
        return score, {
            "center": [round(center_x, 4), round(center_y, 4)],
            "rotation_degrees": round(math.degrees(phase), 2),
            "sector_occupancy": sorted(sectors),
            "radii": sorted(round(radius, 4) for radius in radii),
            "angle_gaps_degrees": [round(math.degrees(gap), 2) for gap in gaps],
            "opposite_errors_degrees": [
                round(math.degrees(error), 2) for error in opposite_errors
            ],
            "adjacent_distances": sorted(round(value, 4) for value in adjacent_distances),
            "opposite_distances": sorted(round(value, 4) for value in opposite_distances),
            "components": [
                [item[0], *(round(value, 4) for value in item[1:5]), item[5]]
                for item in sorted(candidates, key=lambda value: (value[1], value[2], value[5]))
            ],
        }, tuple(Point(item[1], item[2]) for item in candidates)

    centers = [sum(label.center_x for label in group) / len(group) for group in groups]
    boundaries = [0.0, *((centers[i] + centers[i + 1]) / 2.0 for i in range(len(centers) - 1)), 1.0]
    proof: list[dict[str, Any]] = []
    desk_groups: list[tuple[Point, ...]] = []
    for index, center in enumerate(centers):
        local = [
            component
            for component in components
            if boundaries[index] <= component[1] < boundaries[index + 1]
            and abs(component[1] - center) <= 0.18
        ]
        expected = Point(
            center,
            sum(label.center_y for label in groups[index]) / len(groups[index]),
        )
        matches: list[tuple[float, dict[str, Any], tuple[Point, ...]]] = []
        for candidate_group in itertools.combinations(local, 4):
            if min(
                math.hypot(first[1] - second[1], first[2] - second[2])
                for first, second in itertools.combinations(candidate_group, 2)
            ) < 0.025:
                continue
            match = island_geometry(candidate_group, expected)
            if match is not None:
                matches.append(match)
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], _canonical_json(item[1])))
        # Two nearly equal explanations mean the visual topology is ambiguous.
        if len(matches) > 1 and matches[1][0] - matches[0][0] < 0.01:
            return None
        proof.append({"island": index + 1, **matches[0][1]})
        desk_groups.append(matches[0][2])
    payload = {
        "composite_sha256": slide.composite_sha256,
        "ordered_media_sha256s": list(slide.media_sha256s),
        "islands": proof,
    }
    return (
        hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest(),
        tuple(desk_groups),
    )


def _default_spatial_observer(
    engine: Any,
    path: Path,
    slides: tuple[SlideRaster, ...],
) -> SpatialObservation | None:
    if len(slides) != 1:
        return None
    record = _artifact_record(engine, path, slides)
    if record is None:
        return None
    labels = _labels_from_artifact(record, slides[0])
    if labels is None:
        return None
    main = [label for label in labels if label.role != "EXEC"]
    if len(main) < 4 or len(main) % 4:
        return None
    ordered = sorted(main, key=lambda label: (label.center_x, label.center_y, _normalized(label.person)))
    gap_count = len(main) // 4 - 1
    gaps = sorted(
        ((ordered[index + 1].center_x - ordered[index].center_x, index) for index in range(len(ordered) - 1)),
        reverse=True,
    )
    if gap_count and (len(gaps) < gap_count or gaps[gap_count - 1][0] <= 0.04):
        return None
    cuts = sorted(index for _, index in gaps[:gap_count])
    groups: list[list[_Label]] = []
    start = 0
    for cut in cuts:
        groups.append(ordered[start : cut + 1])
        start = cut + 1
    groups.append(ordered[start:])
    if any(len(group) != 4 for group in groups):
        return None
    topology = _topology_fingerprint(slides[0], groups)
    if topology is None:
        return None
    topology_hash, desk_groups = topology

    seats: list[SeatEvidence] = []
    directory: list[DirectoryEvidence] = []
    for group_index, (group, desk_points) in enumerate(zip(groups, desk_groups), 1):
        # Infer callout directions from authored geometry, never from job roles.
        top = min(group, key=lambda label: (label.center_y, label.center_x))
        remaining = [label for label in group if label is not top]
        west = min(remaining, key=lambda label: (label.center_x, label.center_y))
        lower = [label for label in remaining if label is not west]
        if len(lower) != 2 or lower[0].center_x == lower[1].center_x:
            return None
        south, east = sorted(lower, key=lambda label: label.center_x)
        if not (
            top.center_y < west.center_y < min(south.center_y, east.center_y)
            and west.center_x < top.center_x < south.center_x < east.center_x
            and south.center_y > east.center_y
        ):
            return None

        desk_top = min(desk_points, key=lambda point: (point.y, point.x))
        desk_bottom = max(desk_points, key=lambda point: (point.y, -point.x))
        desk_west = min(desk_points, key=lambda point: (point.x, point.y))
        desk_east = max(desk_points, key=lambda point: (point.x, -point.y))
        if len({desk_top, desk_bottom, desk_west, desk_east}) != 4:
            return None
        sorted_x = sorted(point.x for point in desk_points)
        sorted_y = sorted(point.y for point in desk_points)
        if (
            sorted_x[1] - sorted_x[0] < 0.02
            or sorted_x[3] - sorted_x[2] < 0.02
            or sorted_y[1] - sorted_y[0] < 0.02
            or sorted_y[3] - sorted_y[2] < 0.02
        ):
            return None
        pod_center = Point(
            sum(point.x for point in desk_points) / 4.0,
            sum(point.y for point in desk_points) / 4.0,
        )
        assignments = (
            (top, desk_top),
            (west, desk_west),
            (south, desk_bottom),
            (east, desk_east),
        )
        pod = f"pod-{group_index}"
        for label, center in assignments:
            facing = _unit(Point(pod_center.x - center.x, pod_center.y - center.y))
            if facing is None:
                return None
            seats.append(SeatEvidence(label.person, pod, center, facing, slides[0].ordinal, slides[0].composite_sha256, label.evidence_id))
            directory.append(DirectoryEvidence(label.person, (("内線番号", label.extension),), label.evidence_id))
    return SpatialObservation(
        source_sha256=slides[0].source_sha256,
        question_independent=True,
        status="certified",
        seats=tuple(seats),
        directory=tuple(directory),
        observer="dual-ocr-four-seat-floor-map-topology-v1:" + topology_hash,
    )


def _validate_observation(
    observation: SpatialObservation,
    slides: Sequence[SlideRaster],
) -> bool:
    if not isinstance(observation, SpatialObservation):
        return False
    if not observation.question_independent or observation.status != "certified" or not observation.observer.strip():
        return False
    source_hashes = {slide.source_sha256 for slide in slides}
    slide_hashes = {slide.ordinal: slide.composite_sha256 for slide in slides}
    if source_hashes != {observation.source_sha256} or not _SHA256.fullmatch(observation.source_sha256):
        return False
    if not observation.seats or not observation.directory:
        return False
    people: set[str] = set()
    for seat in observation.seats:
        person = _compact(seat.person)
        if (
            not person
            or person in people
            or not seat.pod.strip()
            or not seat.evidence_id.strip()
            or not _valid_point(seat.center)
            or seat.slide_ordinal not in slide_hashes
            or slide_hashes[seat.slide_ordinal] != seat.composite_sha256
        ):
            return False
        if seat.facing is not None:
            if not isinstance(seat.facing, Point) or not math.isfinite(seat.facing.x) or not math.isfinite(seat.facing.y):
                return False
        people.add(person)
    directory_people: set[str] = set()
    for entry in observation.directory:
        person = _compact(entry.person)
        if not person or person in directory_people or not entry.evidence_id.strip():
            return False
        keys: set[str] = set()
        for key, value in entry.attributes:
            normalized_key = _compact(key)
            if not normalized_key or normalized_key in keys or not str(value).strip():
                return False
            keys.add(normalized_key)
        directory_people.add(person)
    return people == directory_people


def _unit(vector: Point | None) -> Point | None:
    if vector is None:
        return None
    norm = math.hypot(vector.x, vector.y)
    if not math.isfinite(norm) or norm <= 1e-9:
        return None
    return Point(vector.x / norm, vector.y / norm)


def _target_seat(observation: SpatialObservation, person: str) -> SeatEvidence | None:
    matches = [seat for seat in observation.seats if _compact(seat.person) == _compact(person)]
    return matches[0] if len(matches) == 1 else None


def _same_pod(observation: SpatialObservation, target: SeatEvidence) -> tuple[SeatEvidence, ...]:
    return tuple(
        seat
        for seat in observation.seats
        if seat.slide_ordinal == target.slide_ordinal
        and _compact(seat.pod) == _compact(target.pod)
        and _compact(seat.person) != _compact(target.person)
    )


def _right_people(observation: SpatialObservation, person: str) -> tuple[str, ...] | None:
    target = _target_seat(observation, person)
    if target is None:
        return None
    facing = _unit(target.facing)
    if facing is None:
        return None
    right = Point(-facing.y, facing.x)
    selected: list[tuple[float, float, str]] = []
    for seat in _same_pod(observation, target):
        dx = seat.center.x - target.center.x
        dy = seat.center.y - target.center.y
        distance = math.hypot(dx, dy)
        if distance <= 1e-9:
            return None
        lateral = dx * right.x + dy * right.y
        forward = dx * facing.x + dy * facing.y
        # Require a stable side classification, excluding straight-ahead/opposite seats.
        if lateral / distance > 0.35 and lateral > abs(forward) * 0.35:
            selected.append((distance, -lateral, seat.person.strip()))
    if not selected:
        return None
    selected.sort(key=lambda item: (item[0], item[1], _normalized(item[2])))
    names = tuple(item[2] for item in selected)
    return names if len({_compact(name) for name in names}) == len(names) else None


def _opposite_person(observation: SpatialObservation, person: str) -> str | None:
    target = _target_seat(observation, person)
    if target is None:
        return None
    facing = _unit(target.facing)
    if facing is None:
        return None
    aligned: list[tuple[float, str]] = []
    for seat in _same_pod(observation, target):
        dx = seat.center.x - target.center.x
        dy = seat.center.y - target.center.y
        distance = math.hypot(dx, dy)
        if distance <= 1e-9:
            return None
        forward = (dx * facing.x + dy * facing.y) / distance
        lateral = abs(dx * (-facing.y) + dy * facing.x) / distance
        if forward >= 0.85 and lateral <= 0.35:
            aligned.append((distance, seat.person.strip()))
    if len(aligned) != 1:
        return None
    return aligned[0][1]


def _canonical_attribute(attribute: str, glossary: Any) -> str | None:
    values = _alias_values(attribute, glossary)
    if values is None:
        return None
    canonical = [value for value in values if _compact(value) != _compact(attribute)]
    if len(canonical) > 1:
        return None
    return canonical[0] if canonical else attribute


def _directory_value(
    observation: SpatialObservation,
    person: str,
    attribute: str,
) -> str | None:
    entries = [entry for entry in observation.directory if _compact(entry.person) == _compact(person)]
    if len(entries) != 1:
        return None
    candidates = [
        str(value).strip()
        for key, value in entries[0].attributes
        if _compact(key) == _compact(attribute)
    ]
    return candidates[0] if len(candidates) == 1 and candidates[0] else None


def _decision(
    answer: str,
    path: Path,
    root: Path,
    operations: int,
    output_count: int,
) -> StructuredCandidateDecision:
    relative = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
    return StructuredCandidateDecision(
        "resolved",
        "certified_pptx_spatial",
        StructuredCandidateAnswer(
            answer=answer,
            source_paths=(relative,),
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            operation_count=operations,
            output_count=output_count,
        ),
    )


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    if not isinstance(question, str):
        return None
    right_match = PPTX_PERSON_RIGHT.fullmatch(question)
    opposite_match = PPTX_PERSON_OPPOSITE_ATTRIBUTE.fullmatch(question)
    match = right_match or opposite_match
    if match is None:
        return None
    paths = _matching_pptx(engine, match["location"], match["container"])
    if paths is None:
        return _hold("pptx_spatial_alias_or_root_ambiguous")
    if len(paths) != 1:
        return _hold("pptx_spatial_source_not_unique")
    path = paths[0]
    slides = _slide_rasters(path)
    if slides is None:
        return _hold("pptx_spatial_visible_slide_invalid")
    observer = getattr(engine, "pptx_spatial_observer", None)
    if callable(observer):
        try:
            observation = observer(slides)
        except Exception:
            return _hold("pptx_spatial_observer_failed")
    else:
        observation = _default_spatial_observer(engine, path, slides)
        if observation is None:
            return _hold("pptx_spatial_observer_unavailable")
    if not _validate_observation(observation, slides):
        return _hold("pptx_spatial_observation_invalid")
    root = _safe_root(engine)
    if root is None:
        return _hold("source_root_invalid")
    if right_match is not None:
        names = _right_people(observation, right_match["person"])
        if names is None:
            return _hold("pptx_spatial_right_relation_unproved")
        return _decision("、".join(names), path, root, len(_RIGHT_OPERATORS), len(names))
    assert opposite_match is not None
    opposite = _opposite_person(observation, opposite_match["person"])
    if opposite is None:
        return _hold("pptx_spatial_opposite_relation_unproved")
    attribute = _canonical_attribute(opposite_match["attribute"], getattr(engine, "glossary", None))
    if attribute is None:
        return _hold("pptx_spatial_attribute_alias_ambiguous")
    value = _directory_value(observation, opposite, attribute)
    if value is None:
        return _hold("pptx_spatial_attribute_join_unproved")
    return _decision(value, path, root, len(_OPPOSITE_OPERATORS), 1)


def decide_from_graph(
    engine: Any,
    question: str,
    graph_plan: Any,
) -> StructuredCandidateDecision | None:
    contract = graph_contract_for_question(question)
    if contract is None:
        return None
    if (
        graph_plan is None
        or getattr(graph_plan, "original_question", None) != question
        or getattr(graph_plan, "strict_status", None) != "pass"
    ):
        return _hold("pptx_spatial_graph_plan_not_certified")
    branches = getattr(graph_plan, "branch_intents", ())
    if not isinstance(branches, tuple) or len(branches) != 1:
        return _hold("pptx_spatial_graph_plan_not_certified")
    branch = branches[0]
    if not isinstance(branch, Mapping) or branch.get("status") != "resolved":
        return _hold("pptx_spatial_graph_plan_not_certified")
    intent = branch.get("intent")
    supplied = intent.get("extended_graph_contract") if isinstance(intent, Mapping) else None
    if not isinstance(supplied, Mapping) or not validate_graph_contract(question, supplied):
        return _hold("pptx_spatial_graph_plan_contract_mismatch")
    if _canonical_json(supplied) != _canonical_json(contract):
        return _hold("pptx_spatial_graph_plan_contract_mismatch")
    return decide_question(engine, question)


__all__ = [
    "PPTX_SPATIAL_RULE_VERSION",
    "PPTX_PERSON_RIGHT",
    "PPTX_PERSON_OPPOSITE_ATTRIBUTE",
    "Point",
    "SlideRaster",
    "SeatEvidence",
    "DirectoryEvidence",
    "SpatialObservation",
    "decide_from_graph",
    "decide_question",
    "graph_contract_for_question",
    "validate_graph_contract",
]
