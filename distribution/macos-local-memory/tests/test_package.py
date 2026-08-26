#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"


class PackageTests(unittest.TestCase):
    def test_python_files_compile(self) -> None:
        paths = [*ROOT.glob("app/*.py"), *ENGINE.glob("*.py")]
        result = subprocess.run([os.sys.executable, "-m", "py_compile", *map(str, paths)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_path_to_semantic_graph_without_external_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "memo.txt").write_text("講演のテーマはAIエージェントとハルシネーション対策。", encoding="utf-8")
            with zipfile.ZipFile(source / "table.xlsx", "w") as archive:
                archive.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>請求金額</t></si></sst>')
                archive.writestr("xl/worksheets/sheet1.xml", '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>12000</v></c></row></sheetData></worksheet>')
            path_out = base / "path"
            semantic_out = base / "semantic"
            subprocess.run([os.sys.executable, str(ENGINE / "build_path_graph.py"), str(source), "--output-dir", str(path_out)], check=True, capture_output=True)
            subprocess.run([os.sys.executable, str(ENGINE / "validate_path_graph.py"), str(path_out / "path-evidence-graph.json"), str(path_out / "path-source-inventory.jsonl")], check=True, capture_output=True)
            subprocess.run([os.sys.executable, str(ENGINE / "build_semantic_graph.py"), "--inventory", str(path_out / "path-source-inventory.jsonl"), "--source-root", str(source), "--output-dir", str(semantic_out)], check=True, capture_output=True)
            subprocess.run([os.sys.executable, str(ENGINE / "validate_semantic_graph.py"), "--output-dir", str(semantic_out)], check=True, capture_output=True)
            evidence = [json.loads(line) for line in (semantic_out / "semantic-evidence.jsonl").read_text(encoding="utf-8").splitlines()]
            observed = "\n".join(item["observed_text"] for item in evidence)
            self.assertIn("AIエージェント", observed)
            self.assertIn("請求金額", observed)
            self.assertIn("12000", observed)
            security_out = base / "security"
            security_out.mkdir()
            subprocess.run([
                os.sys.executable, str(ENGINE / "content_security_gate.py"),
                "--evidence", str(semantic_out / "semantic-evidence.jsonl"),
                "--documents", str(semantic_out / "semantic-documents.jsonl"),
                "--output-dir", str(security_out),
            ], check=True, capture_output=True)
            subprocess.run([
                os.sys.executable, str(ENGINE / "validate_content_security_gate.py"),
                "--evidence", str(semantic_out / "semantic-evidence.jsonl"),
                "--documents", str(semantic_out / "semantic-documents.jsonl"),
                "--gate-dir", str(security_out),
            ], check=True, capture_output=True)
            state = json.loads((security_out / "content-security-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["execution_policy"], "never_execute")

    def test_server_binds_loopback_only(self) -> None:
        text = (ROOT / "app" / "local_memory_server.py").read_text(encoding="utf-8")
        self.assertIn('if args.host not in {"127.0.0.1", "localhost"}', text)

    def test_bootstrap_downloads_are_official_and_verified(self) -> None:
        text = (ROOT / "app" / "launch.sh").read_text(encoding="utf-8")
        self.assertIn("https://www.python.org/ftp/python/", text)
        self.assertIn("Python Software Foundation", text)
        self.assertIn("https://ollama.com/download/Ollama.dmg", text)
        self.assertIn("codesign --verify --deep --strict", text)
        self.assertIn("3MU9H2V9Y9", text)

    def test_model_roles_are_explicit_and_separate(self) -> None:
        bootstrap = (ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
        server = (ROOT / "app" / "local_memory_server.py").read_text(encoding="utf-8")
        answer_v2 = (ENGINE / "answer_local_memory_v2.py").read_text(encoding="utf-8")
        final_audit = (ROOT / "app" / "final_answer_audit.py").read_text(encoding="utf-8")

        self.assertIn('"answer_model": "qwen3.5:9b"', bootstrap)
        self.assertIn('"audit_model": "gemma4:12b"', bootstrap)
        self.assertIn('default="qwen3.5:9b"', answer_v2)
        self.assertIn('config["answer_model"]', server)
        self.assertIn('config["audit_model"]', server)
        self.assertIn('"independent_final_auditor"', final_audit)
        self.assertIn('"audited-answers.jsonl"', server)


if __name__ == "__main__":
    unittest.main()
