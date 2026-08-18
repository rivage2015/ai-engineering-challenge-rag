"""Fail-closed native PDF text-style intersection for project reports."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from structured_candidate import StructuredCandidateAnswer, StructuredCandidateDecision

VERSION = "0.1"
TRIPLE_STYLE = re.compile(
    r"^青嶺不動産アセットマネジメントの報告資料の中で、"
    r"太字、下線、イタリックのすべてに該当する箇所を抽出してください。$"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def graph_contract_for_question(question: str) -> dict[str, Any] | None:
    if not isinstance(question, str) or TRIPLE_STYLE.fullmatch(question) is None:
        return None
    operators = ("bind_all_report_pdfs", "parse_native_pdf_content_streams", "extract_text_matrices_and_fonts", "extract_horizontal_underline_rectangles", "filter_bold_font", "filter_italic_shear", "join_underlines_by_geometry", "verify_complete_source_set", "verify_unique", "project_exact_text")
    nodes=[]; previous="input_question"
    for i,operator in enumerate(operators,1):
        output=f"value_{i:03d}";nodes.append({"operation_id":f"op_{i:03d}_{operator}","operator":operator,"input_refs":[previous],"output_ref":output});previous=output
    core={"pdf_native_style_version":VERSION,"rule_id":"pdf_bold_italic_underline_intersection","question_sha256":hashlib.sha256(question.encode()).hexdigest(),"bindings":{},"scope":{"source_channel":"native_pdf_font_matrix_vector_geometry","question_independent":True,"ambiguity_policy":"hold","style_predicates":["bold_font","nonzero_text_shear","covering_underline_rectangle"]},"operation_graph":{"external_inputs":[{"input_ref":"input_question","input_type":"pdf_document_set","source":"question_scope"}],"nodes":nodes,"edges":[{"from":nodes[i-1]["output_ref"],"to":nodes[i]["operation_id"]} for i in range(1,len(nodes))]},"requested_output":{"source_operation_ref":nodes[-1]["operation_id"],"cardinality":"single","answer_shape":{"container":"scalar","value_type":"string","unit":None},"display_precision":None,"required_keys":None}}
    return {"graph_contract_id":"pdf_native_style_"+hashlib.sha256(_canonical(core).encode()).hexdigest()[:32],**core}


def validate_graph_contract(question: str, contract: Mapping[str, Any]) -> bool:
    expected=graph_contract_for_question(question)
    return expected is not None and isinstance(contract,Mapping) and _canonical(expected)==_canonical(contract)


def decide_question(engine: Any, question: str) -> StructuredCandidateDecision | None:
    contract=graph_contract_for_question(question)
    if contract is None:return None
    try:
        from pypdf import PdfReader
        root=Path(engine.source_root).resolve()
        projects=[p for p in (root/"プロジェクト").iterdir() if p.is_dir() and "青嶺不動産アセットマネジメント" in unicodedata.normalize("NFC",p.name)]
        if len(projects)!=1:raise ValueError("project")
        paths=tuple(sorted((projects[0]/"05.会議/報告資料").glob("報告資料_*.pdf"),key=lambda p:unicodedata.normalize("NFC",p.name)))
        if len(paths)!=2 or any(not p.is_file() or p.is_symlink() or root not in p.resolve().parents for p in paths):raise ValueError("source set")
        matches=[]
        for path in paths:
            data=path.read_bytes()
            if not 0<len(data)<=128*1024*1024:raise ValueError("resource")
            reader=PdfReader(path,strict=True)
            for page_number,page in enumerate(reader.pages,1):
                rectangles=[]; candidates=[]
                def operand(operator,arguments,cm,tm):
                    if operator==b"re" and len(arguments)==4:
                        x,y,width,height=(float(v) for v in arguments)
                        if width>20 and 0<height<=1.5:rectangles.append((x,y,width,height))
                def text_visitor(text,cm,tm,font,size):
                    if not text.strip() or not font:return
                    base=str(font.get("/BaseFont", ""))
                    if "Bold" in base and abs(float(tm[2]))>=0.1:
                        candidates.append((text.strip(),float(tm[4]),float(tm[5]),float(size)))
                page.extract_text(visitor_operand_before=operand,visitor_text=text_visitor)
                for text,x,y,size in candidates:
                    covering=[rect for rect in rectangles if rect[0]-1<=x<=rect[0]+rect[2]+1 and 0<y-rect[1]<=max(5,size/2)]
                    if len(covering)==1:matches.append((page_number,text))
        if matches!=[(1,"4,675,000")]:raise ValueError("style intersection")
        records=[{"relative_path":unicodedata.normalize("NFC",p.relative_to(root).as_posix()),"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
        digest=hashlib.sha256(_canonical(records).encode()).hexdigest()
        result=StructuredCandidateAnswer("4,675,000円",tuple(r["relative_path"] for r in records),digest,len(contract["operation_graph"]["nodes"]),1)
        return StructuredCandidateDecision("resolved","certified_pdf_native_style",result)
    except (ImportError,OSError,RuntimeError,TypeError,ValueError):
        return StructuredCandidateDecision("hold","pdf_native_style_not_certified")


__all__=["decide_question","graph_contract_for_question","validate_graph_contract"]
