from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import local_image_ocr as image_reader  # noqa: E402
import local_visual_observation as visual  # noqa: E402
import probe_intermediate_records as records  # noqa: E402


RUN_AT = "2031-04-01T00:00:00+00:00"
MODEL_DIGEST = "a" * 64
IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mP8/x8AAusB9Y9ZK7sAAAAASUVORK5CYII="
)
IMAGE_SHA256 = hashlib.sha256(IMAGE_BYTES).hexdigest()


def worker_result() -> dict[str, object]:
    observation = {
        "visible_objects": [
            {"object_id": "o1", "kind": "chart", "description": "青い棒グラフ"}
        ],
        "explicit_labels": [{"label_id": "l1", "text": "架空の比較表"}],
        "explicit_relations": [
            {"source_ref": "l1", "relation": "labels", "target_ref": "o1"}
        ],
        "labeled_values": [],
        "warnings": [],
    }
    return {
        "schema_version": "0.1",
        "record_type": "local_visual_observation",
        "observation_type": "whole_image_literal_visual_observation",
        "status": "provisional",
        "quality_tier": "provisional",
        "provisional_marker": visual.PROVISIONAL_MARKER,
        "text": visual._provisional_text(observation),
        "observation": observation,
        "question_independent": True,
        "model": visual.VISUAL_OBSERVATION_MODEL,
        "model_digest": MODEL_DIGEST,
        "prompt_sha256": visual.VISUAL_OBSERVATION_PROMPT_SHA256,
        "input_image_sha256": IMAGE_SHA256,
        "model_output_sha256": hashlib.sha256(
            json.dumps(observation, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "runner": visual.VISUAL_OBSERVATION_RUNNER,
        "runner_version": visual.VISUAL_OBSERVATION_VERSION,
        "host": visual.OLLAMA_HOST,
        "port": visual.OLLAMA_PORT,
        "temperature": 0,
        "strict_json": True,
        "external_network_used": False,
        "downloads_performed": False,
    }


class LocalVisualMemoIntegrationTests(unittest.TestCase):
    """Exercise the real Probe -> public reader boundary without any inference."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="aiec-visual-memo-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pdf_path = self.root / "fictional-document.pdf"
        self.pptx_path = self.root / "fictional-deck.pptx"
        # These are source-identity fixtures, not containers to parse. The test
        # enters the existing post-materialization Probe boundary directly.
        self.pdf_path.write_bytes(b"fictional PDF source identity")
        self.pptx_path.write_bytes(b"fictional PPTX source identity")
        image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear()
        self.addCleanup(image_reader._LOCAL_MODEL_TIMEOUT_LATCH.clear)
        self.http = self.enterContext(mock.patch.object(
            visual, "_ollama_json", side_effect=AssertionError("network forbidden")
        ))
        self.process = self.enterContext(mock.patch.object(
            visual.subprocess, "Popen", side_effect=AssertionError("model process forbidden")
        ))
        self.model = self.enterContext(mock.patch.object(
            visual,
            "_installed_model",
            return_value={
                "requested": visual.VISUAL_OBSERVATION_MODEL,
                "resolved": visual.VISUAL_OBSERVATION_MODEL,
                "digest": MODEL_DIGEST,
            },
        ))
        self.worker = self.enterContext(mock.patch.object(
            visual, "_run_isolated_task", side_effect=lambda *args, **kwargs: worker_result()
        ))

    def tearDown(self) -> None:
        self.http.assert_not_called()
        self.process.assert_not_called()

    def _probe(self) -> records.Probe:
        return records.Probe(self.root, RUN_AT, None, diagnostic=False)

    def _add_occurrence(
        self,
        probe: records.Probe,
        document: dict[str, object],
        *,
        number: int,
        powerpoint: bool = False,
    ) -> tuple[dict[str, object] | None, dict[str, object], dict[str, object]]:
        location = (
            {
                "slide_number": number,
                "object_index": 3,
                "source_member": "ppt/media/image1.png",
            }
            if powerpoint else {"page_number": number}
        )
        origin = probe._embedded_visual_origin(
            IMAGE_BYTES,
            document,
            location_prefix=location,
            source_name=(
                "ppt/media/image1.png" if powerpoint else f"rendered-page-{number}.png"
            ),
            visual_origin_kind=("office_embedded_image" if powerpoint else "pdf_page_image"),
        )
        if not powerpoint:
            origin["materialization"]["page_number"] = number
        parent = probe.add_evidence(
            document["document_id"],
            "image",
            copy.deepcopy(location),
            records.content(
                content_ref=f"{document['source']['relative_path']}#occurrence={number}",
                mime_type="image/png",
            ),
            ordinal=number,
            native_properties={"visual_origin": copy.deepcopy(origin)},
        )
        probe.contain_document(document["document_id"], parent["evidence_id"])
        retained = probe._add_local_visual_observation(
            self.root / "not-read-because-image-bytes-were-supplied.png",
            document,
            parent_id=parent["evidence_id"],
            location_prefix=copy.deepcopy(location),
            visual_origin=copy.deepcopy(origin),
            ordinal=number,
            image_bytes=IMAGE_BYTES,
            timeout=1,
            release_paddle=False,
        )
        return (probe.evidence[-1] if retained else None), parent, origin

    def _read_occurrences(self, *, enabled: bool):
        probe = self._probe()
        occurrences = []
        with visual.visual_observation_session(enabled=enabled) as session:
            pdf = probe.add_document(self.pdf_path, "synthetic-pdf-materialization")
            occurrences.append(self._add_occurrence(probe, pdf, number=2))
            occurrences.append(self._add_occurrence(probe, pdf, number=5))
            probe.finalize_document()
            deck = probe.add_document(self.pptx_path, "synthetic-office-materialization")
            occurrences.append(self._add_occurrence(probe, deck, number=7, powerpoint=True))
            probe.finalize_document()
            stats = session.stats()
        return probe, occurrences, stats

    def test_reused_image_keeps_each_document_page_and_slide_provenance(self) -> None:
        probe, occurrences, stats = self._read_occurrences(enabled=True)
        self.assertEqual(self.worker.call_count, 1)
        self.assertGreaterEqual(self.model.call_count, 3)
        self.assertEqual(stats["hits"], 2)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["stores"], 1)
        self.assertEqual(stats["entries"], 1)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(len({item[0]["evidence_id"] for item in occurrences}), 3)
        documents = {doc["document_id"]: doc for doc in probe.documents}
        for evidence, parent, origin in occurrences:
            self.assertIsNotNone(evidence)
            self.assertEqual(evidence["document_id"], parent["document_id"])
            self.assertEqual(evidence["parent_evidence_id"], parent["evidence_id"])
            self.assertEqual(
                evidence["location"],
                {**origin["source_location"], "locator_text": "visual_observation=whole_image"},
            )
            properties = evidence["native_properties"]
            self.assertEqual(properties["visual_origin"], origin)
            document = documents[evidence["document_id"]]
            self.assertEqual(origin["source_relative_path"], document["source"]["relative_path"])
            self.assertEqual(origin["source_sha256"], document["source"]["sha256"])
            self.assertEqual(properties["input_image_sha256"], IMAGE_SHA256)
            self.assertEqual(properties["quality_tier"], "provisional")
            self.assertEqual(properties["provisional_marker"], visual.PROVISIONAL_MARKER)
            self.assertEqual(evidence["provenance"]["confidence"], 0.0)
            self.assertFalse(evidence["provenance"]["deterministic"])
            self.assertIn(visual.PROVISIONAL_MARKER, evidence["content"]["raw_text"])
            self.assertEqual(document["extraction"]["status"], "partial")
            self.assertTrue(any(
                relation["relation_type"] == "contains"
                and relation["from_ref"]["record_id"] == parent["evidence_id"]
                and relation["to_ref"]["record_id"] == evidence["evidence_id"]
                for relation in probe.relations
            ))
        pdf_origin = occurrences[0][0]["native_properties"]["visual_origin"]
        office_origin = occurrences[2][0]["native_properties"]["visual_origin"]
        self.assertEqual(pdf_origin["source_location"]["page_number"], 2)
        self.assertEqual(office_origin["source_location"]["slide_number"], 7)
        # Reusing the underlying bytes must not pretend that Office cropping,
        # transparency or rotation was resolved from a PDF occurrence.
        self.assertTrue(pdf_origin["materialization"]["display_transform_resolved"])
        self.assertFalse(office_origin["materialization"]["display_transform_resolved"])
        self.assertEqual(office_origin["materialization"]["display_transform_status"], "unresolved")

    def test_mutating_one_evidence_does_not_change_reused_observation(self) -> None:
        probe = self._probe()
        document = probe.add_document(self.pdf_path, "synthetic-pdf-materialization")
        with visual.visual_observation_session() as session:
            first, _, _ = self._add_occurrence(probe, document, number=1)
            self.assertIsNotNone(first)
            expected_observation = copy.deepcopy(first["native_properties"]["structured_observation"])
            first["native_properties"]["structured_observation"]["visible_objects"][0]["description"] = "changed first"
            first["native_properties"]["visual_origin"]["source_location"]["page_number"] = 999
            first["content"]["raw_text"] = "changed first text"
            second, _, second_origin = self._add_occurrence(probe, document, number=2)
            self.assertIsNotNone(second)
            self.assertEqual(second["native_properties"]["structured_observation"], expected_observation)
            self.assertEqual(second["native_properties"]["visual_origin"], second_origin)
            second["native_properties"]["structured_observation"]["explicit_labels"][0]["text"] = "changed second"
            third, _, _ = self._add_occurrence(probe, document, number=3)
            self.assertIsNotNone(third)
            self.assertEqual(third["native_properties"]["structured_observation"], expected_observation)
            self.assertEqual(first["content"]["raw_text"], "changed first text")
            self.assertEqual(
                first["native_properties"]["structured_observation"]["explicit_labels"][0]["text"],
                expected_observation["explicit_labels"][0]["text"],
            )
            self.assertEqual(session.stats()["hits"], 2)
        probe.finalize_document()
        self.assertEqual(self.worker.call_count, 1)

    def test_disabled_and_enabled_reuse_emit_identical_evidence_and_relations(self) -> None:
        ordinary, _, _ = self._read_occurrences(enabled=False)
        self.assertEqual(self.worker.call_count, 3)
        self.worker.reset_mock()
        reused, _, _ = self._read_occurrences(enabled=True)
        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(reused.documents, ordinary.documents)
        self.assertEqual(reused.evidence, ordinary.evidence)
        self.assertEqual(reused.relations, ordinary.relations)

    def test_failed_asset_is_not_cached_or_materialized_as_success(self) -> None:
        self.worker.side_effect = [
            visual.CompletedResponseValidationError("completed response rejected"),
            worker_result(),
        ]
        probe = self._probe()
        document = probe.add_document(self.pdf_path, "synthetic-pdf-materialization")
        with visual.visual_observation_session() as session:
            first, first_parent, _ = self._add_occurrence(probe, document, number=1)
            self.assertIsNone(first)
            self.assertEqual(session.stats()["stores"], 0)
            second, second_parent, _ = self._add_occurrence(probe, document, number=2)
            self.assertIsNotNone(second)
            self.assertEqual(session.stats()["hits"], 0)
            self.assertEqual(session.stats()["stores"], 1)
        probe.finalize_document()
        observations = [item for item in probe.evidence if item["evidence_type"] == "text_block"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["parent_evidence_id"], second_parent["evidence_id"])
        self.assertNotEqual(observations[0]["parent_evidence_id"], first_parent["evidence_id"])
        self.assertEqual(self.worker.call_count, 2)
        self.assertEqual(document["extraction"]["status"], "partial")
        self.assertTrue(any("visual meaning unavailable" in item for item in document["extraction"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
