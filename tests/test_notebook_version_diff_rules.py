from __future__ import annotations

import base64
import binascii
import copy
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RAG = ROOT / "rag"
if str(RAG) not in sys.path:
    sys.path.insert(0, str(RAG))

import notebook_version_diff_rules as rules  # noqa: E402
from notebook_version_diff_rules import (  # noqa: E402
    decide_from_graph,
    decide_question,
    graph_contract_for_question,
    validate_graph_contract,
)


LOCATION = "架空審査部門"
BEFORE_NAME = "review_previous.ipynb"
AFTER_NAME = "review_current.ipynb"
TARGET = "outcome_flag"
OLD_COUNT = 13
NEW_COUNT = 14
SLOTS = 4


def question(
    *,
    location: str = LOCATION,
    before: str = BEFORE_NAME,
    after: str = AFTER_NAME,
) -> str:
    return (
        f"{location}の{before}から{after}への変更内容のうち、"
        "内容として変わっている点は何ですか。"
    )


def engine_for(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        source_root=root.resolve(),
        glossary=SimpleNamespace(entries={}),
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = binascii.crc32(kind)
    crc = binascii.crc32(payload, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def table_png(
    count: int,
    *,
    mutate_header: int | None = None,
) -> bytes:
    """Create a small transparent, wrapped table fixture without Pillow."""

    blocks = 4
    row_stride = 10
    horizontal_count = blocks * 4
    width = SLOTS * 20 + 1
    height = (horizontal_count - 1) * row_stride + 1
    pixels = bytearray(width * height * 4)

    def pixel(x: int, y: int, rgba: tuple[int, int, int, int]) -> None:
        offset = (y * width + x) * 4
        pixels[offset : offset + 4] = bytes(rgba)

    grid = (180, 180, 180, 255)
    for line in range(horizontal_count):
        y = line * row_stride
        for x in range(width):
            pixel(x, y, grid)
    for x in range(0, width, 10):
        for y in range(height):
            pixel(x, y, grid)

    for index in range(count):
        block, slot = divmod(index, SLOTS)
        x0 = slot * 20 + 3
        y0 = block * 4 * row_stride + 2
        if index == mutate_header:
            points = {(x, y) for x in range(8) for y in range(6)}
        else:
            points = {
                (0, 0),
                (1, 0),
                (2, 0),
                (3, 0),
                (4, 0),
                (0, 1),
                (4, 1),
                (0, 2),
                (4, 2),
                (0, 3),
                (4, 3),
                (0, 4),
                (1, 4),
                (2, 4),
                (3, 4),
                (4, 4),
                (1 + index % 3, 2),
            }
        for dx, dy in points:
            pixel(x0 + dx, y0 + dy, (0, 0, 0, 255))

    filtered = bytearray()
    stride = width * 4
    for y in range(height):
        filtered.append(0)
        filtered.extend(pixels[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", ihdr),
            _png_chunk(b"IDAT", zlib.compress(bytes(filtered), level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def image_source(png: bytes) -> list[str]:
    payload = base64.b64encode(png).decode("ascii")
    return [f"![fixture](data:image/png;base64,{payload})\n"]


def notebook(
    png: bytes,
    image_id: str,
    *,
    dataset_rel: str = "data/train.csv",
    target: str = TARGET,
) -> dict[str, object]:
    code = f'''from pathlib import Path
ENCODINGS = ("utf-8-sig", "utf-8", "cp932")
csv_rel = Path("{dataset_rel}")
csv_path = analysis_root / csv_rel
df, used_encoding = load_csv_auto(csv_path)
target_col = "{target}"
if target_col not in df.columns:
    target_col = df.columns[-1]
'''
    return {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "stable-code",
                "metadata": {},
                "outputs": [],
                "source": [code],
            },
            {
                "cell_type": "markdown",
                "id": "stable-heading",
                "metadata": {},
                "source": ["### 基本統計量"],
            },
            {
                "cell_type": "markdown",
                "id": image_id,
                "metadata": {},
                "source": image_source(png),
            },
            {
                "cell_type": "markdown",
                "id": "stable-tail",
                "metadata": {},
                "source": ["## 次の分析\n"],
            },
        ],
        "metadata": {"kernelspec": {"name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write_notebook(path: Path, value: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def write_csv(path: Path, *, target_last: bool = True, target: str = TARGET) -> Path:
    measures = [f"measure_{index:02d}" for index in range(1, NEW_COUNT)]
    numeric = [*measures, target] if target_last else [*measures[:-1], target, measures[-1]]
    rows = [
        ["record_key", *numeric],
        ["row-a", *[str(index + 1) for index in range(len(numeric))]],
        ["row-b", *[str((index + 1) * 2) for index in range(len(numeric))]],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(",".join(row) for row in rows) + "\n", encoding="utf-8")
    return path


def make_project(
    root: Path,
    *,
    before_png: bytes | None = None,
    after_png: bytes | None = None,
    target_last: bool = True,
    dataset_rel: str = "data/train.csv",
) -> tuple[Path, Path, Path]:
    project = (
        root
        / f"{LOCATION}株式会社"
        / "04.分析"
        / "analysis_project"
    )
    notebooks = project / "notebooks"
    before = write_notebook(
        notebooks / BEFORE_NAME,
        notebook(before_png or table_png(OLD_COUNT), "before-image", dataset_rel=dataset_rel),
    )
    after = write_notebook(
        notebooks / AFTER_NAME,
        notebook(after_png or table_png(NEW_COUNT), "after-image", dataset_rel=dataset_rel),
    )
    dataset = write_csv(project / "data" / "train.csv", target_last=target_last)
    return before, after, dataset


def load_notebook(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class NotebookVersionDiffRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.real_ocr_adapter = rules._tesseract_header_reading
        self.ocr_patcher = mock.patch.object(
            rules,
            "_tesseract_header_reading",
            return_value=TARGET,
        )
        self.ocr_mock = self.ocr_patcher.start()

    def tearDown(self) -> None:
        self.ocr_patcher.stop()
        self.temp.cleanup()

    def test_resolves_source_derived_appended_column(self) -> None:
        before, after, dataset = make_project(self.root)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "resolved")
        self.assertEqual(decision.reason, "certified_notebook_embedded_statistics_diff")
        self.assertIsNotNone(decision.result)
        assert decision.result is not None
        self.assertEqual(
            decision.result.answer,
            f"基本統計量の表に「{TARGET}」列が追加されています。",
        )
        self.assertEqual(decision.result.operation_count, 11)
        self.assertEqual(decision.result.output_count, 1)
        self.assertEqual(
            decision.result.source_paths,
            tuple(path.relative_to(self.root).as_posix() for path in (before, after, dataset)),
        )
        self.assertRegex(decision.result.source_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(
            [call.args[1] for call in self.ocr_mock.call_args_list],
            [3, 6, 7, 10],
        )

    def test_source_target_change_without_png_change_holds(self) -> None:
        make_project(self.root)
        new_target = "approval_result"
        project = next(self.root.rglob("analysis_project"))
        before = project / "notebooks" / BEFORE_NAME
        after = project / "notebooks" / AFTER_NAME
        for path, image_id, count in (
            (before, "before-image", OLD_COUNT),
            (after, "after-image", NEW_COUNT),
        ):
            write_notebook(path, notebook(table_png(count), image_id, target=new_target))
        write_csv(project / "data" / "train.csv", target=new_target)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")
        self.assertIsNone(decision.result)

    def test_one_mismatched_or_missing_ocr_reading_holds(self) -> None:
        make_project(self.root)

        for readings in (
            (TARGET, TARGET, TARGET.upper(), TARGET),
            (TARGET, None, TARGET, TARGET),
        ):
            with self.subTest(readings=readings):
                self.ocr_mock.reset_mock(side_effect=True)
                self.ocr_mock.side_effect = readings

                decision = decide_question(engine_for(self.root), question())

                self.assertEqual(decision.status, "hold")
                self.assertIsNone(decision.result)
                self.assertEqual(self.ocr_mock.call_count, 4)

    def test_header_normalization_preserves_case(self) -> None:
        self.assertEqual(
            rules._header_token("\u3000\uff4f\uff55\uff54\uff43\uff4f\uff4d\uff45\uff3f\uff46\uff4c\uff41\uff47\n"),
            TARGET,
        )
        self.assertNotEqual(rules._header_token(TARGET.upper()), TARGET)

    def test_last_occupied_header_bbox_and_two_x_pgm(self) -> None:
        image = rules._decode_png(table_png(NEW_COUNT))

        evidence = rules._header_evidence(image, NEW_COUNT)

        expected = rules._PixelBox(left=21, top=121, right=40, bottom=130)
        self.assertEqual(evidence.occupied_boxes[-1], expected)
        self.assertEqual(len(evidence.occupied_boxes), NEW_COUNT)
        pgm = rules._header_crop_pgm(image, evidence.occupied_boxes[-1])
        header = b"P5\n38 18\n255\n"
        self.assertTrue(pgm.startswith(header))
        self.assertEqual(len(pgm), len(header) + 38 * 18)

    def test_production_ocr_adapter_invocation_and_fail_closed_paths(self) -> None:
        pgm = b"P5\n1 1\n255\n\xff"
        completed = SimpleNamespace(returncode=0, stdout=b"class\n")
        with (
            mock.patch.object(rules.shutil, "which", return_value="/opt/bin/tesseract"),
            mock.patch.object(rules.subprocess, "run", return_value=completed) as run,
        ):
            reading = self.real_ocr_adapter(pgm, 7)

        self.assertEqual(reading, "class\n")
        run.assert_called_once_with(
            [
                "/opt/bin/tesseract",
                "stdin",
                "stdout",
                "-l",
                "jpn+eng",
                "--oem",
                "1",
                "--psm",
                "7",
            ],
            input=pgm,
            capture_output=True,
            check=False,
            timeout=20,
        )

        with mock.patch.object(rules.shutil, "which", return_value=None):
            self.assertIsNone(self.real_ocr_adapter(pgm, 7))
        with (
            mock.patch.object(rules.shutil, "which", return_value="tesseract"),
            mock.patch.object(
                rules.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=1, stdout=b"class\n"),
            ),
        ):
            self.assertIsNone(self.real_ocr_adapter(pgm, 7))
        with (
            mock.patch.object(rules.shutil, "which", return_value="tesseract"),
            mock.patch.object(
                rules.subprocess,
                "run",
                side_effect=rules.subprocess.TimeoutExpired("tesseract", 20),
            ),
        ):
            self.assertIsNone(self.real_ocr_adapter(pgm, 7))

    def test_graph_contract_is_recompiled_and_graph_decision_resolves(self) -> None:
        make_project(self.root)
        contract = graph_contract_for_question(question())
        self.assertIsNotNone(contract)
        assert contract is not None
        self.assertTrue(validate_graph_contract(question(), contract))
        tampered = copy.deepcopy(contract)
        tampered["scope"]["column_name_evidence"] = "caller_claim"
        self.assertFalse(validate_graph_contract(question(), tampered))

        graph_plan = SimpleNamespace(
            original_question=question(),
            strict_status="pass",
            branch_intents=(
                {
                    "status": "resolved",
                    "intent": {"extended_graph_contract": contract},
                },
            ),
        )
        decision = decide_from_graph(engine_for(self.root), question(), graph_plan)
        self.assertEqual(decision.status, "resolved")

    def test_unmatched_or_same_file_question_is_not_claimed(self) -> None:
        self.assertIsNone(graph_contract_for_question("何が変わりましたか。"))
        self.assertIsNone(
            graph_contract_for_question(
                question(before=BEFORE_NAME, after=BEFORE_NAME)
            )
        )
        self.assertIsNone(decide_question(engine_for(self.root), "何が変わりましたか。"))

    def test_changed_normal_cell_source_holds(self) -> None:
        _, after, _ = make_project(self.root)
        value = load_notebook(after)
        value["cells"][3]["source"] = ["## 別の分析\n"]
        write_notebook(after, value)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "notebook_version_diff_source_not_certified")

    def test_changed_code_output_holds(self) -> None:
        _, after, _ = make_project(self.root)
        value = load_notebook(after)
        value["cells"][0]["outputs"] = [
            {"name": "stdout", "output_type": "stream", "text": ["changed\n"]}
        ]
        write_notebook(after, value)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_image_cell_id_must_be_the_only_non_image_difference(self) -> None:
        before, after, _ = make_project(self.root)
        before_value = load_notebook(before)
        after_value = load_notebook(after)
        after_value["cells"][2]["id"] = before_value["cells"][2]["id"]
        write_notebook(after, after_value)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_corrupt_png_crc_holds(self) -> None:
        corrupted = bytearray(table_png(NEW_COUNT))
        corrupted[-5] ^= 0x01
        make_project(self.root, after_png=bytes(corrupted))

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_existing_header_shape_change_holds(self) -> None:
        make_project(
            self.root,
            after_png=table_png(NEW_COUNT, mutate_header=5),
        )

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_more_than_one_appended_header_holds(self) -> None:
        make_project(self.root, after_png=table_png(NEW_COUNT + 1))

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_target_must_be_last_numeric_column(self) -> None:
        make_project(self.root, target_last=False)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_unsafe_dataset_relative_path_holds(self) -> None:
        make_project(self.root, dataset_rel="../data/train.csv")

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_duplicate_notebook_binding_holds(self) -> None:
        before, _, _ = make_project(self.root)
        duplicate = (
            self.root
            / "duplicate"
            / f"{LOCATION}株式会社"
            / "analysis_project"
            / "notebooks"
            / BEFORE_NAME
        )
        duplicate.parent.mkdir(parents=True)
        shutil.copy2(before, duplicate)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")
        self.assertEqual(decision.reason, "notebook_version_diff_pair_not_unique")

    def test_duplicate_json_key_holds(self) -> None:
        _, after, _ = make_project(self.root)
        text = after.read_text(encoding="utf-8")
        after.write_text(text.replace('{"cells":', '{"cells": [], "cells":', 1), encoding="utf-8")

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")

    def test_non_unique_embedded_image_holds(self) -> None:
        _, after, _ = make_project(self.root)
        value = load_notebook(after)
        extra = copy.deepcopy(value["cells"][2])
        extra["id"] = "second-image"
        value["cells"].append(extra)
        write_notebook(after, value)

        decision = decide_question(engine_for(self.root), question())

        self.assertEqual(decision.status, "hold")


if __name__ == "__main__":
    unittest.main()
