import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / "scripts/probe_local_workflow_reading.py"
spec = importlib.util.spec_from_file_location("probe", PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ReadingProbeTest(unittest.TestCase):
    def setUp(self):
        self.packet = {"rows":[{"cells":[{"id":"試験!C2","text":"予約ありなら左へ。お待ちください。"}]}]}
        self.good = {"steps":[{"label":"案内","condition":"予約あり","actor":"担当者","action":"左へ案内","quotes":[{"text":"お待ちください。","cell":"試験!C2"}],"source_cells":["試験!C2"]}],"unknowns":[]}

    def test_exact_quote(self):
        self.assertTrue(probe.validate_extraction(self.good,self.packet)["mechanical_valid"])

    def test_fabricated_quote(self):
        self.good["steps"][0]["quotes"][0]["text"] = "右へどうぞ。"
        self.assertFalse(probe.validate_extraction(self.good,self.packet)["mechanical_valid"])

    def test_fabricated_cell(self):
        self.good["steps"][0]["source_cells"] = ["試験!Z999"]
        self.assertFalse(probe.validate_extraction(self.good,self.packet)["mechanical_valid"])

    def test_missing_steps(self):
        self.assertFalse(probe.validate_extraction({"steps":[]},self.packet)["mechanical_valid"])

    def test_no_semantic_pass(self):
        self.assertEqual(probe.validate_extraction(self.good,self.packet)["semantic_review"],"not_performed")

    def test_credentials(self):
        for text in ["PASS:dummy", "ID：dummy", "パスワード dummy", "API_KEY=dummy"]:
            self.assertTrue(probe.SENSITIVE.search(text))

    def test_private_output_only(self):
        with self.assertRaises(ValueError):
            probe.safe_output(probe.ROOT / "design")

    def test_no_redirect(self):
        with self.assertRaises(RuntimeError):
            probe.NoRedirect().redirect_request(None,None,302,"",{},"https://example.com")

    def test_prompt_no_answer_fixture(self):
        text = str(probe.messages_for(self.packet,"受付の流れは？","direct"))
        self.assertNotIn("DAWN",text)
        self.assertNotIn("1,500",text)


if __name__ == "__main__":
    unittest.main()
