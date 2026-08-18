from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

import pptx_spatial_rules as rules  # noqa: E402


P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

RIGHT_QUESTION = (
    "IMにあるFMにおいて、Atlasさんから見て"
    "右側に座っている人の名前をすべて挙げてください。"
)
OPPOSITE_QUESTION = (
    "Internal FolderにあるFMにおいて、"
    "Atlasさんの向かいに座っている方のEXTを教えてください。"
)


def _png() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (40, 40), "red")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _topology_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 225), "white")
    draw = ImageDraw.Draw(image)
    for box, color in (
        ((100, 87, 140, 103), "#3478a8"),
        ((74, 107, 114, 123), "#39a878"),
        ((100, 127, 140, 143), "#df7837"),
        ((126, 107, 166, 123), "#e0b83e"),
    ):
        draw.rectangle(box, fill=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _diagonal_topology_png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 225), "white")
    draw = ImageDraw.Draw(image)
    for index, color in enumerate(("#3478a8", "#39a878", "#df7837", "#e0b83e")):
        x = 55 + index * 45
        y = 78 + index * 20
        draw.rectangle((x, y, x + 36, y + 14), fill=color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _presentation_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <p:presentation xmlns:p="{P_NS}" xmlns:r="{R_NS}">
      <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
      <p:sldSz cx="1000" cy="1000"/>
    </p:presentation>"""


def _presentation_rels(*, external: bool = False) -> str:
    target = "https://invalid.example/slide.xml" if external else "slides/slide1.xml"
    mode = ' TargetMode="External"' if external else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">
      <Relationship Id="rId1" Type="{R_NS}/slide" Target="{target}"{mode}/>
    </Relationships>"""


def _slide_xml(*, mask: bool = True) -> str:
    overlay = ""
    if mask:
        overlay = f"""
        <p:sp><p:nvSpPr><p:cNvPr id="3" name="mask"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
          <p:spPr><a:xfrm><a:off x="500" y="0"/><a:ext cx="500" cy="1000"/></a:xfrm>
            <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
            <a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></p:spPr>
          <p:txBody><a:bodyPr/><a:lstStyle/><a:p/></p:txBody>
        </p:sp>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <p:sld xmlns:p="{P_NS}" xmlns:a="{A_NS}" xmlns:r="{R_NS}">
      <p:cSld><p:spTree>
        <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
        <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
        <p:pic><p:nvPicPr><p:cNvPr id="2" name="map"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
          <p:blipFill><a:blip r:embed="rIdImage"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
          <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000" cy="1000"/></a:xfrm>
            <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        </p:pic>{overlay}
      </p:spTree></p:cSld>
    </p:sld>"""


def _slide_rels() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="{PR_NS}">
      <Relationship Id="rIdImage" Type="{R_NS}/image" Target="../media/map.png"/>
    </Relationships>"""


def _write_pptx(
    root: Path,
    *,
    external: bool = False,
    traversal: bool = False,
    invalid_image: bool = False,
    topology_image: bool = False,
    diagonal_topology: bool = False,
    mask: bool = True,
) -> Path:
    path = root / "Internal" / "Opaque Map.pptx"
    path.parent.mkdir(parents=True, exist_ok=True)
    members: dict[str, bytes | str] = {
        "ppt/presentation.xml": _presentation_xml(),
        "ppt/_rels/presentation.xml.rels": _presentation_rels(external=external),
        "ppt/slides/slide1.xml": _slide_xml(mask=mask),
        "ppt/slides/_rels/slide1.xml.rels": _slide_rels(),
        "ppt/media/map.png": (
            b"not-png"
            if invalid_image
            else (
                _diagonal_topology_png()
                if diagonal_topology
                else (_topology_png() if topology_image else _png())
            )
        ),
    }
    if traversal:
        members["../escape.xml"] = b"opaque"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, value in members.items():
            archive.writestr(name, value.encode() if isinstance(value, str) else value)
    return path


def _observation(
    slides: tuple[rules.SlideRaster, ...],
    *,
    facing: rules.Point | None = rules.Point(0.0, 1.0),
) -> rules.SpatialObservation:
    slide = slides[0]
    seats = (
        rules.SeatEvidence("Atlas", "pod-a", rules.Point(0.50, 0.20), facing, 1, slide.composite_sha256, "seat-atlas"),
        rules.SeatEvidence("Boreal", "pod-a", rules.Point(0.20, 0.50), rules.Point(1.0, 0.0), 1, slide.composite_sha256, "seat-boreal"),
        rules.SeatEvidence("Cygnus", "pod-a", rules.Point(0.80, 0.50), rules.Point(-1.0, 0.0), 1, slide.composite_sha256, "seat-cygnus"),
        rules.SeatEvidence("Draco", "pod-a", rules.Point(0.50, 0.80), rules.Point(0.0, -1.0), 1, slide.composite_sha256, "seat-draco"),
    )
    directory = tuple(
        rules.DirectoryEvidence(name, (("内線番号", ext),), f"directory-{name.lower()}")
        for name, ext in (
            ("Atlas", "4101"),
            ("Boreal", "4102"),
            ("Cygnus", "4103"),
            ("Draco", "4104"),
        )
    )
    return rules.SpatialObservation(
        source_sha256=slide.source_sha256,
        question_independent=True,
        status="certified",
        seats=seats,
        directory=directory,
        observer="opaque-test-observer-v1",
    )


def _engine(root: Path, observer=None) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(
            entries={
                "IM": ["Internal Folder"],
                "FM": ["Opaque Map"],
                "EXT": ["内線番号"],
            }
        ),
        pptx_spatial_observer=observer or _observation,
    )


def _write_ocr_sidecar(
    path: Path,
    pptx: Path,
    *,
    bad_source_hash: bool = False,
    duplicate_record: bool = False,
    identity_conflict: bool = False,
    permute_roles: bool = False,
) -> Path:
    with zipfile.ZipFile(pptx) as archive:
        media = archive.read("ppt/media/map.png")
    lines = []
    roles = ("QA", "BA", "PM", "DS") if permute_roles else ("PM", "DS", "BA", "QA")
    for sequence, (x, y, name, role, ext) in enumerate(
        (
            (150, 130, "Atlas", roles[0], "4101"),
            (100, 300, "Boreal", roles[1], "4102"),
            (250, 700, "Draco", roles[2], "4104"),
            (350, 600, "Cygnus", roles[3], "4103"),
        ),
        1,
    ):
        lines.extend(
            (
                {"bbox": [x, y - 30, 60, 20], "confidence": 0.99, "line_id": f"ext-{sequence}", "raw_text": ext},
                {"bbox": [x, y, 100, 25], "confidence": 0.99, "line_id": f"name-{sequence}", "raw_text": f"{name}({role})"},
            )
        )
    source_hash = hashlib.sha256(pptx.read_bytes()).hexdigest()
    if bad_source_hash:
        source_hash = "0" * 64
    engine_runs = [
        {
            "run_id": "opaque-ocr-a",
            "status": "completed",
            "engine": {
                "digest": "1" * 64,
                "independence_group": "opaque-a",
            },
            "lines": lines,
        },
        {
            "run_id": "opaque-ocr-b",
            "status": "completed",
            "engine": {
                "digest": "2" * 64,
                "independence_group": "opaque-b",
            },
            "lines": [dict(line) for line in lines],
        },
    ]
    if identity_conflict:
        engine_runs.append(
            {
                "run_id": "conflicting-ocr",
                "status": "completed",
                "engine": {
                    "digest": "3" * 64,
                    "independence_group": "opaque-conflict",
                },
                "lines": [
                    {
                        "bbox": [150, 100, 60, 20],
                        "confidence": 0.99,
                        "line_id": "ext-conflict",
                        "raw_text": "4101",
                    },
                    {
                        "bbox": [150, 130, 100, 25],
                        "confidence": 0.99,
                        "line_id": "name-conflict",
                        "raw_text": "Altair(PM)",
                    },
                ],
            }
        )
    record = {
        "source": {"sha256": source_hash},
        "origin": {
            "member_path": "ppt/media/map.png",
            "member_sha256": hashlib.sha256(media).hexdigest(),
        },
        "provenance": {"question_independent": True},
        "status": "needs_review" if identity_conflict else "observed",
        "engine_runs": engine_runs,
    }
    records = [record, copy.deepcopy(record)] if duplicate_record else [record]
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in records),
        encoding="utf-8",
    )
    return path


def _graph_plan(question: str) -> SimpleNamespace:
    contract = rules.graph_contract_for_question(question)
    assert contract is not None
    return SimpleNamespace(
        original_question=question,
        strict_status="pass",
        branch_intents=(
            {"status": "resolved", "intent": {"extended_graph_contract": contract}},
        ),
    )


class PPTXSpatialGrammarTest(unittest.TestCase):
    def test_complete_grammars_compile_with_subject_reference_frame(self) -> None:
        right = rules.graph_contract_for_question(RIGHT_QUESTION)
        opposite = rules.graph_contract_for_question(OPPOSITE_QUESTION)
        self.assertIsNotNone(right)
        self.assertIsNotNone(opposite)
        assert right is not None and opposite is not None
        self.assertEqual(right["scope"]["reference_frame"], "subject_facing")
        self.assertTrue(right["scope"]["orientation_required"])
        self.assertEqual(right["requested_output"]["cardinality"], "all")
        self.assertEqual(opposite["rule_id"], "pptx_opposite_seat_attribute")

    def test_near_match_and_appended_instruction_are_rejected(self) -> None:
        self.assertIsNone(rules.graph_contract_for_question(RIGHT_QUESTION.replace("すべて", "")))
        self.assertIsNone(rules.graph_contract_for_question(OPPOSITE_QUESTION + "推測してください。"))
        self.assertIsNone(rules.graph_contract_for_question(""))

    def test_contract_tampering_is_rejected(self) -> None:
        contract = rules.graph_contract_for_question(RIGHT_QUESTION)
        assert contract is not None
        tampered = copy.deepcopy(contract)
        tampered["scope"]["reference_frame"] = "viewer"
        self.assertFalse(rules.validate_graph_contract(RIGHT_QUESTION, tampered))


class PPTXSpatialGeometryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pptx-spatial-")
        self.root = Path(self.temporary.name)
        _write_pptx(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_person_right_is_not_viewer_right(self) -> None:
        decision = rules.decide_question(_engine(self.root), RIGHT_QUESTION)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(decision.result.answer, "Boreal")
        self.assertNotIn("Cygnus", decision.result.answer)

    def test_opposite_geometry_then_ext_join(self) -> None:
        decision = rules.decide_question(_engine(self.root), OPPOSITE_QUESTION)
        self.assertEqual(decision.status, "resolved")
        assert decision.result is not None
        self.assertEqual(decision.result.answer, "4104")

    def test_unknown_orientation_holds(self) -> None:
        observer = lambda slides: _observation(slides, facing=None)
        decision = rules.decide_question(_engine(self.root, observer), RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_right_relation_unproved")

    def test_ambiguous_opposite_holds(self) -> None:
        def observer(slides):
            base = _observation(slides)
            extra = rules.SeatEvidence(
                "Eridanus", "pod-a", rules.Point(0.52, 0.80), rules.Point(0.0, -1.0),
                1, slides[0].composite_sha256, "seat-eridanus",
            )
            directory = base.directory + (
                rules.DirectoryEvidence("Eridanus", (("内線番号", "4105"),), "directory-eridanus"),
            )
            return replace(base, seats=base.seats + (extra,), directory=directory)

        decision = rules.decide_question(_engine(self.root, observer), OPPOSITE_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_opposite_relation_unproved")

    def test_missing_ext_join_holds(self) -> None:
        def observer(slides):
            base = _observation(slides)
            directory = tuple(
                replace(entry, attributes=()) if entry.person == "Draco" else entry
                for entry in base.directory
            )
            return replace(base, directory=directory)

        decision = rules.decide_question(_engine(self.root, observer), OPPOSITE_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_attribute_join_unproved")

    def test_question_dependent_observation_is_rejected(self) -> None:
        observer = lambda slides: replace(_observation(slides), question_independent=False)
        decision = rules.decide_question(_engine(self.root, observer), RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observation_invalid")

    def test_translation_scale_metamorphism_preserves_relation(self) -> None:
        slides = rules._slide_rasters(next(self.root.rglob("*.pptx")))
        assert slides is not None
        base = _observation(slides)

        def transform(point: rules.Point) -> rules.Point:
            return rules.Point(0.1 + point.x * 0.7, 0.1 + point.y * 0.7)

        seats = tuple(replace(seat, center=transform(seat.center)) for seat in base.seats)
        transformed = replace(base, seats=seats)
        self.assertEqual(rules._right_people(base, "Atlas"), ("Boreal",))
        self.assertEqual(rules._right_people(transformed, "Atlas"), ("Boreal",))
        self.assertEqual(rules._opposite_person(transformed, "Atlas"), "Draco")

    def test_rotation_metamorphism_preserves_relation(self) -> None:
        slides = rules._slide_rasters(next(self.root.rglob("*.pptx")))
        assert slides is not None
        base = _observation(slides)
        seats = tuple(
            replace(
                seat,
                center=rules.Point(1.0 - seat.center.y, seat.center.x),
                facing=None if seat.facing is None else rules.Point(-seat.facing.y, seat.facing.x),
            )
            for seat in base.seats
        )
        rotated = replace(base, seats=seats)
        self.assertEqual(rules._right_people(rotated, "Atlas"), ("Boreal",))
        self.assertEqual(rules._opposite_person(rotated, "Atlas"), "Draco")

    def test_live_graph_plan_api_executes_exact_contract(self) -> None:
        decision = rules.decide_from_graph(
            _engine(self.root), RIGHT_QUESTION, _graph_plan(RIGHT_QUESTION)
        )
        self.assertEqual(decision.status, "resolved")
        bad = _graph_plan(RIGHT_QUESTION)
        bad.branch_intents[0]["intent"]["extended_graph_contract"]["scope"]["reference_frame"] = "viewer"
        held = rules.decide_from_graph(_engine(self.root), RIGHT_QUESTION, bad)
        self.assertEqual(held.status, "hold")

    def test_actual_graphplan_runtime_accepts_extended_contract(self) -> None:
        import score_candidate_rules
        from question_graph_runtime import build_graph_plan

        with (
            mock.patch.object(
                score_candidate_rules,
                "graph_contract_for_question",
                rules.graph_contract_for_question,
            ),
            mock.patch.object(
                score_candidate_rules,
                "validate_graph_contract",
                rules.validate_graph_contract,
            ),
        ):
            plan = build_graph_plan("opaque", RIGHT_QUESTION, fast_advisory=True)
        self.assertEqual(plan.strict_status, "pass")
        self.assertEqual(plan.strict_reasons, ("extended_graph_certified",))
        decision = rules.decide_from_graph(_engine(self.root), RIGHT_QUESTION, plan)
        self.assertEqual(decision.status, "resolved")

    def test_missing_legacy_and_tampered_plans_hold(self) -> None:
        engine = _engine(self.root)
        missing = rules.decide_from_graph(engine, RIGHT_QUESTION, None)
        legacy = rules.decide_from_graph(
            engine,
            RIGHT_QUESTION,
            SimpleNamespace(
                original_question=RIGHT_QUESTION,
                strict_status="pass",
                branch_intents=(),
            ),
        )
        tampered = _graph_plan(RIGHT_QUESTION)
        tampered.branch_intents[0]["intent"]["extended_graph_contract"]["bindings"]["person"] = "Altair"
        changed = rules.decide_from_graph(engine, RIGHT_QUESTION, tampered)
        self.assertEqual(missing.reason, "pptx_spatial_graph_plan_not_certified")
        self.assertEqual(legacy.reason, "pptx_spatial_graph_plan_not_certified")
        self.assertEqual(changed.reason, "pptx_spatial_graph_plan_contract_mismatch")


class PPTXSpatialPackageSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiec-pptx-spatial-safe-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_opaque_mask_is_applied_before_observer(self) -> None:
        _write_pptx(self.root)

        def observer(slides):
            from PIL import Image

            with Image.open(io.BytesIO(slides[0].png_bytes)) as image:
                self.assertEqual(image.getpixel((200, 500)), (255, 0, 0))
                self.assertEqual(image.getpixel((1200, 500)), (255, 255, 255))
            return _observation(slides)

        decision = rules.decide_question(_engine(self.root, observer), RIGHT_QUESTION)
        self.assertEqual(decision.status, "resolved")

    def test_production_engine_auto_loads_hash_bound_question_independent_sidecar(self) -> None:
        pptx = _write_pptx(self.root, topology_image=True, mask=False)
        sidecar = _write_ocr_sidecar(self.root / "ocr.jsonl", pptx)
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        right = rules.decide_question(engine, RIGHT_QUESTION)
        opposite = rules.decide_question(engine, OPPOSITE_QUESTION)
        self.assertEqual(right.status, "resolved")
        self.assertEqual(opposite.status, "resolved")
        assert right.result is not None and opposite.result is not None
        self.assertEqual(right.result.answer, "Boreal")
        self.assertEqual(opposite.result.answer, "4104")

    def test_default_observer_geometry_is_independent_of_role_labels(self) -> None:
        pptx = _write_pptx(self.root, topology_image=True, mask=False)
        sidecar = _write_ocr_sidecar(
            self.root / "ocr.jsonl", pptx, permute_roles=True
        )
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        right = rules.decide_question(engine, RIGHT_QUESTION)
        opposite = rules.decide_question(engine, OPPOSITE_QUESTION)
        self.assertEqual(right.status, "resolved")
        self.assertEqual(opposite.status, "resolved")
        assert right.result is not None and opposite.result is not None
        self.assertEqual(right.result.answer, "Boreal")
        self.assertEqual(opposite.result.answer, "4104")

    def test_sidecar_source_hash_mismatch_holds(self) -> None:
        pptx = _write_pptx(self.root, topology_image=True, mask=False)
        sidecar = _write_ocr_sidecar(self.root / "ocr.jsonl", pptx, bad_source_hash=True)
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        decision = rules.decide_question(engine, RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observer_unavailable")

    def test_duplicate_artifact_record_holds(self) -> None:
        pptx = _write_pptx(self.root, topology_image=True, mask=False)
        sidecar = _write_ocr_sidecar(
            self.root / "ocr.jsonl", pptx, duplicate_record=True
        )
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        decision = rules.decide_question(engine, RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observer_unavailable")

    def test_equal_confidence_needs_review_identity_conflict_holds(self) -> None:
        pptx = _write_pptx(self.root, topology_image=True, mask=False)
        sidecar = _write_ocr_sidecar(
            self.root / "ocr.jsonl", pptx, identity_conflict=True
        )
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        decision = rules.decide_question(engine, RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observer_unavailable")

    def test_same_ocr_labels_with_changed_raster_topology_hold(self) -> None:
        pptx = _write_pptx(self.root, topology_image=False, mask=False)
        sidecar = _write_ocr_sidecar(self.root / "ocr.jsonl", pptx)
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        decision = rules.decide_question(engine, RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observer_unavailable")

    def test_same_ocr_boxes_with_diagonal_coloured_components_hold(self) -> None:
        pptx = _write_pptx(self.root, diagonal_topology=True, mask=False)
        sidecar = _write_ocr_sidecar(self.root / "ocr.jsonl", pptx)
        engine = _engine(self.root)
        del engine.pptx_spatial_observer
        engine.pptx_spatial_observation_path = sidecar
        decision = rules.decide_question(engine, RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_observer_unavailable")

    def test_external_relationship_fails_closed(self) -> None:
        _write_pptx(self.root, external=True)
        decision = rules.decide_question(_engine(self.root), RIGHT_QUESTION)
        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "pptx_spatial_visible_slide_invalid")

    def test_archive_traversal_and_invalid_raster_fail_closed(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        _write_pptx(first, traversal=True)
        _write_pptx(second, invalid_image=True)
        self.assertIsNone(rules._slide_rasters(next(first.rglob("*.pptx"))))
        self.assertIsNone(rules._slide_rasters(next(second.rglob("*.pptx"))))

    def test_xml_entities_are_rejected(self) -> None:
        self.assertIsNone(rules._safe_xml(b'<!DOCTYPE x [<!ENTITY e "x">]><x>&e;</x>'))

    def test_emf_record_walker_accepts_minimal_and_rejects_truncation(self) -> None:
        header = bytearray(88)
        struct.pack_into("<II", header, 0, 1, 88)
        struct.pack_into("<II", header, 40, 0x464D4520, 0x00010000)
        eof = struct.pack("<IIIII", 14, 20, 0, 16, 20)
        payload = header + eof
        struct.pack_into("<I", payload, 48, len(payload))
        self.assertTrue(rules._validate_emf(bytes(payload)))
        self.assertFalse(rules._validate_emf(bytes(payload[:-4])))


if __name__ == "__main__":
    unittest.main()
