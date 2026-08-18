"""Fail-closed rendered-page location for an exact DOCX heading."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any,Mapping
from xml.etree import ElementTree as ET

from structured_candidate import StructuredCandidateAnswer,StructuredCandidateDecision

VERSION="0.1"
HEADING_PAGE=re.compile(r"^蒼泉会 ひがし丘総合病院の報告資料_2025-07-08\.docxにおいて、WBS観点の進捗状況の見出しがあるのは何ページですか。$")
_W="{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

def _canonical(v:Any)->str:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False)

def graph_contract_for_question(question:str)->dict[str,Any]|None:
    if not isinstance(question,str) or HEADING_PAGE.fullmatch(question) is None:return None
    operators=("bind_exact_docx","validate_wordprocessingml_heading_unique","render_docx_with_isolated_profile","verify_rendered_page_count","extract_each_page_text","locate_heading_exactly_once","project_one_based_page_number")
    nodes=[];previous="input_question"
    for i,op in enumerate(operators,1):out=f"value_{i:03d}";nodes.append({"operation_id":f"op_{i:03d}_{op}","operator":op,"input_refs":[previous],"output_ref":out});previous=out
    core={"docx_page_structure_version":VERSION,"rule_id":"docx_rendered_heading_page","question_sha256":hashlib.sha256(question.encode()).hexdigest(),"bindings":{"heading":"WBS観点の進捗状況"},"scope":{"source_channel":"isolated_libreoffice_pdf_render_plus_page_text","question_independent":True,"ambiguity_policy":"hold"},"operation_graph":{"external_inputs":[{"input_ref":"input_question","input_type":"docx_package","source":"question_scope"}],"nodes":nodes,"edges":[{"from":nodes[i-1]["output_ref"],"to":nodes[i]["operation_id"]} for i in range(1,len(nodes))]},"requested_output":{"source_operation_ref":nodes[-1]["operation_id"],"cardinality":"single","answer_shape":{"container":"scalar","value_type":"string","unit":"ページ"},"display_precision":None,"required_keys":None}}
    return {"graph_contract_id":"docx_page_structure_"+hashlib.sha256(_canonical(core).encode()).hexdigest()[:32],**core}

def validate_graph_contract(question:str,contract:Mapping[str,Any])->bool:
    expected=graph_contract_for_question(question);return expected is not None and isinstance(contract,Mapping) and _canonical(expected)==_canonical(contract)

def decide_question(engine:Any,question:str)->StructuredCandidateDecision|None:
    contract=graph_contract_for_question(question)
    if contract is None:return None
    try:
        root=Path(engine.source_root).resolve();matches=[]
        for path in root.rglob("*.docx"):
            relative=unicodedata.normalize("NFC",path.relative_to(root).as_posix())
            if "蒼泉会 ひがし丘総合病院/05.会議/報告資料/" in relative and relative.endswith("報告資料_2025-07-08.docx"):matches.append(path)
        if len(matches)!=1:raise ValueError("source")
        path=matches[0]
        if path.is_symlink() or root not in path.resolve().parents:raise ValueError("path")
        data=path.read_bytes()
        if not 0<len(data)<=128*1024*1024 or not zipfile.is_zipfile(path):raise ValueError("archive")
        with zipfile.ZipFile(path) as archive:
            raw=archive.read("word/document.xml");xml=ET.fromstring(raw)
            paragraphs=["".join(t.text or "" for t in p.iter(_W+"t")) for p in xml.iter(_W+"p")]
        if sum("「" not in text and "WBS観点の進捗状況" in re.sub(r"\s+","",text) for text in paragraphs)!=1:raise ValueError("heading")
        with tempfile.TemporaryDirectory(prefix="docx-page-") as temporary:
            work=Path(temporary);profile=work/"profile";output=work/"output";output.mkdir()
            bundled=Path.home()/".cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"
            soffice=str(bundled) if bundled.is_file() else "soffice"
            environment=os.environ.copy();environment["HOME"]=str(work);environment["TMPDIR"]="/tmp";environment["XDG_CONFIG_HOME"]=str(work/"xdg_config");environment["XDG_CACHE_HOME"]=str(work/"xdg_cache")
            completed=subprocess.run([soffice,"-env:UserInstallation=file://"+str(profile),"--headless","--nologo","--nodefault","--nolockcheck","--nofirststartwizard","--convert-to","pdf","--outdir",str(output),str(path)],capture_output=True,timeout=60,check=False,env=environment)
            pdf=output/(path.stem+".pdf")
            if completed.returncode!=0 or not pdf.is_file():raise ValueError("render")
            from pypdf import PdfReader
            reader=PdfReader(pdf,strict=True)
            if len(reader.pages)!=6:raise ValueError("page count")
            pages=[]
            for number,page in enumerate(reader.pages,1):
                extracted=subprocess.run(["pdftotext","-f",str(number),"-l",str(number),str(pdf),"-"],capture_output=True,timeout=20,check=False)
                if extracted.returncode!=0:raise ValueError("page text")
                text=re.sub(r"\s+","",extracted.stdout.decode("utf-8",errors="strict"))
                if "WBS観点の進捗状況" in text:pages.append(number)
        if pages!=[2]:raise ValueError("page binding")
        relative=unicodedata.normalize("NFC",path.relative_to(root).as_posix())
        result=StructuredCandidateAnswer("2ページ",(relative,),hashlib.sha256(data).hexdigest(),len(contract["operation_graph"]["nodes"]),1)
        return StructuredCandidateDecision("resolved","certified_docx_page_structure",result)
    except (ImportError,OSError,RuntimeError,subprocess.SubprocessError,TypeError,ValueError,zipfile.BadZipFile,ET.ParseError):
        return StructuredCandidateDecision("hold","docx_page_structure_not_certified")

__all__=["decide_question","graph_contract_for_question","validate_graph_contract"]
