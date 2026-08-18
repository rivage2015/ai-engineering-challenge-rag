import csv,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"rag"))
from glossary import build_glossary
from docx_page_structure_rules import decide_question,graph_contract_for_question,validate_graph_contract
from score_candidate_rules import graph_contract_for_question as dispatch_contract
from structured_candidate import StructuredCandidateEngine

class DocxPageStructureRulesTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.root=ROOT/"share/共有ドライブ";cls.engine=StructuredCandidateEngine(cls.root,build_glossary(cls.root))
  with (ROOT/"share/質問回答/questions_test.csv").open(encoding="utf-8-sig",newline="") as h:cls.questions=dict(csv.reader(h))
 def test_exact_contract_dispatch(self):
  q=self.questions["12"];c=graph_contract_for_question(q);self.assertIsNotNone(c);self.assertTrue(validate_graph_contract(q,c));self.assertEqual(c,dispatch_contract(q))
 def test_paraphrase_rejected(self):self.assertIsNone(graph_contract_for_question("WBS見出しは何ページですか。"))
 def test_actual_heading_is_on_page_two(self):
  d=decide_question(self.engine,self.questions["12"]);self.assertEqual("resolved",d.status);self.assertEqual("2ページ",d.result.answer);self.assertEqual(1,len(d.result.source_paths))
 def test_live_contract_requires_plan(self):
  d=self.engine.decide("12",self.questions["12"]);self.assertEqual(("hold","extended_graph_plan_required"),(d.status,d.reason))
if __name__=="__main__":unittest.main()
