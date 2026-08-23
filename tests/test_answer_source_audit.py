import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("audit", ROOT / "scripts" / "build_answer_source_audit.py")
audit = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(audit)


class AnswerSourceAuditTests(unittest.TestCase):
    def test_retrieval_only_never_claims_correctness(self):
        status, proof = audit.classify({"strict_status": "hold", "decision_status": "hold", "candidate_answer": None}, "仮回答")
        self.assertEqual("unverified", status); self.assertFalse(proof["proof_complete"])

    def test_strict_source_answer_verifies_matching_answer(self):
        status, proof = audit.classify({"strict_status": "pass", "decision_status": "resolved", "candidate_answer": "80〜130時間", "source_paths": ["report.pdf"]}, "80 〜 130時間")
        self.assertEqual("verified", status); self.assertTrue(proof["proof_complete"])

    def test_strict_source_answer_can_contradict_tentative_answer(self):
        status, proof = audit.classify({"strict_status": "pass", "decision_status": "resolved", "candidate_answer": "4,250,000円", "source_paths": ["contract.docx"]}, "該当なし")
        self.assertEqual("contradicted", status); self.assertTrue(proof["proof_complete"])

    def test_strict_candidate_without_source_binding_stays_unverified(self):
        status, proof = audit.classify({"strict_status": "pass", "decision_status": "resolved", "candidate_answer": "答え"}, "答え")
        self.assertEqual("unverified", status)
        self.assertFalse(proof["proof_complete"])

    def test_supported_new_question_grammars_are_exact(self):
        self.assertIsNotNone(audit.TM_RATE_CHANGE_QUESTION.fullmatch("TM案件において、RATEが変更されたのは何年何月1日からと想定されますか。"))
        self.assertIsNone(audit.TM_RATE_CHANGE_QUESTION.fullmatch("RATEが変わったのはいつ？"))
        match = audit.CONTRACT_OVERLAP_QUESTION.fullmatch("2025-08-15 から 2025-09-07 の間に契約期間が重なっている案件の中で、契約期間が 40日 を超えている案件を、主略称ですべて挙げてください。")
        self.assertEqual("40", match.group("days"))
        self.assertIsNotNone(audit.MAX_TM_HOURS_GAP_QUESTION.fullmatch("事後精算案件のうち、提案時の見積工数と最終報告で報告されている実績工数の乖離が最も大きい案件を主略称で挙げてください。"))
        variance = audit.RATE_AND_HOURS_VARIANCE_QUESTION.fullmatch("AOMINEの契約条件において、契約単価が現状よりも2,000円高く、実績工数が11.2時間少なかった場合、税込請求金額は、実際の税込請求金額と比べていくら変動しますか。")
        self.assertEqual(("AOMINE", "2,000", "11.2"), (variance.group("alias"), variance.group("rate_delta"), variance.group("hours_delta")))


if __name__ == "__main__": unittest.main()
