"""Use the audited Layer-1 lexical or hybrid indexes in the answer pipeline."""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from retrieval_trace_common import enrich_retrieval, load_document_sources  # noqa: E402
from search_hybrid import search as search_hybrid  # noqa: E402
from search_lexical_index import search as search_lexical  # noqa: E402

from index import Chunk  # noqa: E402


def project_from_path(source_path: str) -> str:
    parts = Path(source_path).parts
    if len(parts) >= 2 and parts[0] == "プロジェクト":
        return parts[1]
    return ""


def location_text(result: dict[str, object]) -> str:
    values: list[str] = []
    for label, key in (("page", "page"), ("sheet", "sheet"), ("slide", "slide")):
        value = result.get(key)
        if value is not None:
            values.append(f"{label}={value}")
    if result.get("section"):
        values.append(str(result["section"]))
    return " / ".join(values)


class Layer1Index:
    """Adapter exposing the existing ``Index.search`` interface."""

    def __init__(
        self,
        mode: str,
        lexical_index: Path,
        intermediates: list[Path],
        semantic_index: Path | None = None,
    ) -> None:
        if mode not in {"layer1-lexical", "layer1-hybrid"}:
            raise ValueError(f"unsupported Layer-1 retrieval mode: {mode}")
        if mode == "layer1-hybrid" and semantic_index is None:
            raise ValueError("layer1-hybrid requires a semantic index")
        self.mode = mode
        self.lexical_index = lexical_index.resolve()
        self.semantic_index = semantic_index.resolve() if semantic_index else None
        self.document_sources, self.intermediate_states = load_document_sources(intermediates)
        self.projects = sorted({
            project_from_path(source_path)
            for source_path in self.document_sources.values()
            if project_from_path(source_path)
        })
        lexical_state = json.loads(
            (self.lexical_index / "lexical-index-state.json").read_text(encoding="utf-8")
        )
        if lexical_state.get("build_status") != "complete":
            raise ValueError("Layer-1 lexical index is incomplete")
        self.record_count = int(lexical_state.get("output", {}).get("record_count", 0))
        if self.semantic_index is not None:
            semantic_state = json.loads(
                (self.semantic_index / "semantic-index-state.json").read_text(encoding="utf-8")
            )
            if semantic_state.get("build_status") != "complete":
                raise ValueError("Layer-1 semantic index is incomplete")
            if int(semantic_state.get("matrix", {}).get("record_count", 0)) != self.record_count:
                raise ValueError("Layer-1 lexical and semantic index counts differ")
            if (
                semantic_state.get("source", {}).get("search_units_sha256")
                != lexical_state.get("source", {}).get("search_units_sha256")
            ):
                raise ValueError("Layer-1 lexical and semantic indexes use different SearchUnits")

    def target_projects(self, query: str) -> set[str]:
        normalized_query = unicodedata.normalize("NFKC", query)
        targets: set[str] = set()
        for project in self.projects:
            normalized_project = unicodedata.normalize("NFKC", project)
            core = re.sub(r"(株式会社|医療法人社団|有限会社)", "", normalized_project).strip()
            tokens = [normalized_project, core, *core.split()]
            if any(len(token) >= 3 and token in normalized_query for token in tokens):
                targets.add(project)
        return targets

    def strip_project_names(self, query: str, targets: set[str]) -> str:
        result = unicodedata.normalize("NFKC", query)
        for project in targets:
            normalized_project = unicodedata.normalize("NFKC", project)
            core = re.sub(r"(株式会社|医療法人社団|有限会社)", " ", normalized_project).strip()
            for token in (normalized_project, *core.split()):
                if len(token) >= 3:
                    result = result.replace(token, " ")
        return result

    def project_rerank(self, retrieval: dict[str, object], targets: set[str]) -> None:
        if not targets:
            return

        def priority(item: dict[str, object]) -> tuple[int, int]:
            project = project_from_path(str(item.get("file") or ""))
            if project in targets:
                group = 0
            elif not project:
                group = 1
            else:
                group = 2
            return group, int(item["rank"])

        results = sorted(retrieval["results"], key=priority)
        for rank, item in enumerate(results, 1):
            item["rank"] = rank
        retrieval["results"] = results

    def search(self, query: str, extra_terms=(), top_k: int = 12) -> list[Chunk]:
        expanded_query = query
        if extra_terms:
            expanded_query += "\n" + "\n".join(str(term) for term in extra_terms)
        targets = self.target_projects(expanded_query)
        retrieval_query = self.strip_project_names(expanded_query, targets)
        allowed_document_ids = None
        if targets:
            allowed_document_ids = {
                document_id
                for document_id, source_path in self.document_sources.items()
                if project_from_path(source_path) in targets or not project_from_path(source_path)
            }
        candidate_k = max(100, top_k * 8)
        if self.mode == "layer1-hybrid":
            retrieval = search_hybrid(
                self.lexical_index,
                self.semantic_index,
                retrieval_query,
                candidate_k,
                candidate_k=candidate_k,
                snippet_chars=6000,
                adaptive_semantic=False,
                document_ids=allowed_document_ids,
            )
        else:
            retrieval = search_lexical(
                self.lexical_index,
                retrieval_query,
                candidate_k,
                snippet_chars=6000,
                document_ids=allowed_document_ids,
            )
        enrich_retrieval(retrieval, self.document_sources)
        self.project_rerank(retrieval, targets)
        chunks: list[Chunk] = []
        for item in retrieval["results"][:top_k]:
            source_path = item.get("file") or item["document_id"]
            chunks.append(Chunk(
                cid=int(item["rank"]),
                path=str(source_path),
                project=project_from_path(str(source_path)),
                filename=Path(str(source_path)).name,
                kind=str(item["unit_type"]),
                location=location_text(item),
                text=str(item["evidence_text"]),
            ))
        return chunks
