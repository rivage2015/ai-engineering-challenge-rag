#!/usr/bin/env python3
"""Build a deterministic, source-bound filesystem path Evidence Graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = "1.0"
BUILDER_VERSION = "0.1.0"


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    return prefix + "_" + sha256_bytes("\0".join(parts).encode("utf-8"))[:32]


def kind_from_mode(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def extension(path: Path, kind: str) -> str:
    return path.suffix.casefold() if kind == "file" and path.suffix else ""


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(value)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    # ``Path.write_text(newline=...)`` is unavailable on the Python version
    # bundled with the local evaluation environment.  ``open`` preserves the
    # same explicit UTF-8/LF contract across supported Python versions.
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def enumerate_entries(root: Path) -> tuple[list[dict], list[dict]]:
    entries: list[dict] = []
    errors: list[dict] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            errors.append({"path": str(directory), "operation": "scandir", "error": f"{type(exc).__name__}: {exc}"})
            continue
        child_directories = []
        for item in children:
            path = Path(item.path)
            relative = path.relative_to(root).as_posix()
            try:
                metadata = item.stat(follow_symlinks=False)
                kind = kind_from_mode(metadata.st_mode)
                target = os.readlink(path) if kind == "symlink" else None
                record = {
                    "relative_path": relative,
                    "normalized_path": unicodedata.normalize("NFC", relative),
                    "parent_path": path.parent.relative_to(root).as_posix() if path.parent != root else ".",
                    "basename": item.name,
                    "kind": kind,
                    "extension": extension(path, kind),
                    "size_bytes": metadata.st_size if kind == "file" else 0,
                    "mtime_ns": metadata.st_mtime_ns,
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "symlink_target": target,
                    "sha256": None,
                    "read_status": "observed",
                }
                if kind == "file":
                    try:
                        record["sha256"] = sha256_file(path)
                    except OSError as exc:
                        record["read_status"] = "unresolved"
                        errors.append({"path": relative, "operation": "sha256", "error": f"{type(exc).__name__}: {exc}"})
                elif kind == "symlink":
                    record["sha256"] = sha256_bytes(("symlink\0" + (target or "")).encode("utf-8"))
                entries.append(record)
                if kind == "directory":
                    child_directories.append(path)
            except OSError as exc:
                errors.append({"path": relative, "operation": "lstat", "error": f"{type(exc).__name__}: {exc}"})
        stack.extend(reversed(child_directories))
    entries.sort(key=lambda item: os.fsencode(item["relative_path"]))
    return entries, errors


def directory_hashes(entries: list[dict]) -> dict[str, str]:
    by_parent: dict[str, list[dict]] = defaultdict(list)
    directories = {"."}
    for item in entries:
        by_parent[item["parent_path"]].append(item)
        if item["kind"] == "directory":
            directories.add(item["relative_path"])
    result: dict[str, str] = {}
    for directory in sorted(directories, key=lambda value: (-value.count("/"), os.fsencode(value))):
        children = []
        for item in sorted(by_parent.get(directory, []), key=lambda value: os.fsencode(value["basename"])):
            child_hash = result.get(item["relative_path"]) if item["kind"] == "directory" else item["sha256"]
            children.append({
                "basename": item["basename"],
                "kind": item["kind"],
                "size_bytes": item["size_bytes"],
                "sha256": child_hash,
            })
        result[directory] = sha256_bytes(canonical(children))
    return result


def inventory_records(entries: list[dict]) -> list[dict]:
    return [{
        key: item[key]
        for key in ("relative_path", "kind", "size_bytes", "mtime_ns", "sha256", "read_status")
    } for item in entries]


def inventory_jsonl(records: list[dict]) -> bytes:
    return b"".join(canonical(record) + b"\n" for record in records)


def make_graph(root: Path, entries: list[dict], errors: list[dict], inventory_sha256: str) -> dict:
    dir_hash = directory_hashes(entries)
    root_node_id = stable_id("node", "root", ".")
    node_by_path = {".": root_node_id}
    nodes = [{
        "node_id": root_node_id,
        "node_type": "filesystem_root",
        "raw_value": {"absolute_path": str(root)},
        "normalized_value": {"relative_path": ".", "normalized_path": "."},
        "status": "observed",
        "source": {
            "source_id": "filesystem-root",
            "path": str(root),
            "sha256": dir_hash["."],
            "locator": {"relative_path": "."},
            "quote": "",
            "extraction_method": "filesystem_lstat_and_sha256",
        },
    }]
    for item in entries:
        node_id = stable_id("node", item["kind"], item["relative_path"])
        node_by_path[item["relative_path"]] = node_id
        source_hash = dir_hash[item["relative_path"]] if item["kind"] == "directory" else item["sha256"]
        nodes.append({
            "node_id": node_id,
            "node_type": f"filesystem_{item['kind']}",
            "raw_value": {
                "relative_path": item["relative_path"],
                "basename": item["basename"],
                "kind": item["kind"],
                "extension": item["extension"],
                "size_bytes": item["size_bytes"],
                "mtime_ns": item["mtime_ns"],
                "mode": item["mode"],
                "symlink_target": item["symlink_target"],
            },
            "normalized_value": {
                "relative_path": item["normalized_path"],
                "extension": item["extension"],
            },
            "status": item["read_status"],
            "source": {
                "source_id": stable_id("source", item["kind"], item["relative_path"]),
                "path": str(root / item["relative_path"]),
                "sha256": source_hash,
                "locator": {"relative_path": item["relative_path"]},
                "quote": "",
                "extraction_method": "filesystem_lstat_and_sha256",
            },
        })

    edges = []
    for item in entries:
        parent_id = node_by_path[item["parent_path"]]
        child_id = node_by_path[item["relative_path"]]
        edges.append({
            "edge_id": stable_id("edge", "contains", item["parent_path"], item["relative_path"]),
            "edge_type": "contains",
            "from_node_id": parent_id,
            "to_node_id": child_id,
            "scope": {"root": str(root)},
            "basis": {
                "claim": "The child path was enumerated directly from the parent directory.",
                "comparison_fields": ["parent_path", "relative_path"],
                "evidence_node_ids": [parent_id, child_id],
            },
            "policy": {
                "required": ["direct_os_scandir_membership", "lstat_success"],
                "one_of": [],
                "forbidden": ["symlink_traversal", "semantic_name_inference"],
            },
            "audit": {
                "machine": "pass",
                "blind": "not_required_deterministic_relation",
                "falsifier": "path_parent_recomputed",
                "hallucination_risk_flags": [],
            },
            "status": "verified",
        })

    duplicates: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for item in entries:
        if item["kind"] == "file" and item["sha256"]:
            duplicates[(item["size_bytes"], item["sha256"])].append(item)
    duplicate_groups = []
    for (size, digest), group in sorted(duplicates.items(), key=lambda value: (value[0][1], value[0][0])):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: os.fsencode(item["relative_path"]))
        representative = ordered[0]
        group_id = stable_id("duplicate", digest, str(size))
        duplicate_groups.append({
            "group_id": group_id,
            "sha256": digest,
            "size_bytes": size,
            "paths": [item["relative_path"] for item in ordered],
        })
        for item in ordered[1:]:
            left = node_by_path[representative["relative_path"]]
            right = node_by_path[item["relative_path"]]
            edges.append({
                "edge_id": stable_id("edge", "exact_duplicate", representative["relative_path"], item["relative_path"]),
                "edge_type": "exact_duplicate",
                "from_node_id": left,
                "to_node_id": right,
                "scope": {"root": str(root), "duplicate_group_id": group_id},
                "basis": {
                    "claim": "Both regular files have identical byte length and SHA-256.",
                    "comparison_fields": ["size_bytes", "sha256"],
                    "evidence_node_ids": [left, right],
                },
                "policy": {
                    "required": ["regular_file", "same_size", "same_sha256"],
                    "one_of": [],
                    "forbidden": ["basename_only", "extension_only", "semantic_similarity"],
                },
                "audit": {
                    "machine": "pass",
                    "blind": "not_required_deterministic_relation",
                    "falsifier": "sha256_and_size_recomputed",
                    "hallucination_risk_flags": [],
                },
                "status": "verified",
            })

    policies = {
        "contains": {
            "required": ["direct_os_scandir_membership", "lstat_success"],
            "forbidden": ["symlink_traversal", "semantic_name_inference"],
        },
        "exact_duplicate": {
            "required": ["regular_file", "same_size", "same_sha256"],
            "forbidden": ["basename_only", "extension_only", "semantic_similarity"],
        },
    }
    coverage_complete = not errors and len(entries) == len(nodes) - 1 and len(entries) == sum(1 for edge in edges if edge["edge_type"] == "contains")
    graph = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "evidence_graph",
        "graph_id": stable_id("graph", str(root), inventory_sha256),
        "builder": {"name": "build_path_graph.py", "version": BUILDER_VERSION},
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "question_intent": {
            "requested": [
                "Enumerate every filesystem path under the supplied root without following symlinks.",
                "Represent roots, directories, files, and containment as source-bound nodes and edges.",
                "Connect exact duplicate regular files only when size and SHA-256 match.",
                "Return a coverage and branch-size summary.",
            ],
            "not_requested": [
                "Read or summarize document contents.",
                "Transcribe media.",
                "Expand archives.",
                "Infer semantic relationships from names.",
            ],
            "forbidden": [
                "Modify, move, rename, or delete source paths.",
                "Follow symlinks.",
                "Use external network services.",
                "Treat similar names as verified identity or causality.",
            ],
            "ambiguity": [
                "The phrase path graph could mean structural or semantic relations; this build fixes scope to structural filesystem paths.",
            ],
            "answer_shape": {"graph": "JSON", "summary": "Markdown"},
            "proof_obligations": [
                "Every enumerated non-root entry has exactly one verified contains edge.",
                "No symlink is traversed.",
                "Every exact_duplicate edge is backed by equal size and SHA-256.",
                "Errors and unreadable paths remain explicit.",
            ],
        },
        "source_universe": {
            "scope": str(root),
            "enumeration_rule": "recursive os.scandir; lstat semantics; do not follow symlinks",
            "inventory_file": "path-source-inventory.jsonl",
            "expected_sources": "all descendants observable at build time",
            "observed_source_count": len(entries),
            "excluded_sources": [{"kind": "archive_members", "reason": "archive expansion not requested"}],
            "coverage_status": "complete" if coverage_complete else "incomplete",
        },
        "graph_plan": {
            "required_node_types": ["filesystem_root", "filesystem_directory", "filesystem_file", "filesystem_symlink", "filesystem_other"],
            "required_edge_types": ["contains", "exact_duplicate"],
            "operations": ["enumerate", "lstat", "sha256_regular_files", "hash_directories", "verify_parent_child", "verify_exact_duplicates", "audit_coverage"],
            "stopping_conditions": ["all_descendants_consumed", "all_read_errors_recorded", "all_edge_policies_machine_checked"],
        },
        "edge_policies": policies,
        "nodes": nodes,
        "edges": edges,
        "duplicate_groups": duplicate_groups,
        "unresolved": [{
            "kind": "filesystem_read_error",
            "description": error["error"],
            "path": error["path"],
            "required_checks": [error["operation"]],
            "status": "open",
            "blocks": ["complete_path_coverage"],
        } for error in errors],
        "coverage_audit": {
            "status": "complete" if coverage_complete else "incomplete",
            "checks": [
                {"check": "enumerated_entry_count_equals_nonroot_node_count", "status": "pass" if len(entries) == len(nodes) - 1 else "fail"},
                {"check": "every_nonroot_node_has_one_contains_edge", "status": "pass" if len(entries) == sum(1 for edge in edges if edge["edge_type"] == "contains") else "fail"},
                {"check": "read_errors_empty", "status": "pass" if not errors else "fail"},
                {"check": "symlink_traversal_disabled", "status": "pass"},
            ],
            "missing_items": errors,
        },
        "human_review": {
            "status": "not_required" if coverage_complete else "required",
            "trigger_edge_ids": [],
            "risk_flags": [] if coverage_complete else ["coverage_unknown"],
            "confirmed_facts": ["Filesystem structure only; no content semantics were inferred."],
            "unresolved_items": errors,
            "options": [],
            "question": None,
            "user_decision": None,
        },
        "answer_projection": {
            "operation": "project_verified_path_structure_and_duplicate_groups",
            "input_node_ids": [node["node_id"] for node in nodes],
            "input_edge_ids": [edge["edge_id"] for edge in edges],
            "result": {
                "entry_count": len(entries),
                "directory_count": sum(item["kind"] == "directory" for item in entries),
                "file_count": sum(item["kind"] == "file" for item in entries),
                "symlink_count": sum(item["kind"] == "symlink" for item in entries),
                "other_count": sum(item["kind"] == "other" for item in entries),
                "duplicate_group_count": len(duplicate_groups),
            },
            "status": "ready" if coverage_complete else "blocked",
        },
        "integrity": {
            "algorithm": "sha256",
            "source_inventory_sha256": inventory_sha256,
            "graph_content_sha256": None,
            "audit_policy_sha256": sha256_bytes(canonical(policies)),
        },
    }
    graph["integrity"]["graph_content_sha256"] = sha256_bytes(canonical({**graph, "integrity": {**graph["integrity"], "graph_content_sha256": None}}))
    return graph


def branch_counts(entries: list[dict]) -> list[tuple[str, int, int, int]]:
    totals: dict[str, Counter] = defaultdict(Counter)
    for item in entries:
        top = item["relative_path"].split("/", 1)[0]
        totals[top]["entries"] += 1
        totals[top][item["kind"]] += 1
    return sorted(
        ((name, values["entries"], values["directory"], values["file"]) for name, values in totals.items()),
        key=lambda item: (-item[1], os.fsencode(item[0])),
    )


def summary_markdown(graph: dict, entries: list[dict]) -> str:
    result = graph["answer_projection"]["result"]
    lines = [
        "# AI関連・パスEvidence Graph",
        "",
        f"- 対象：`{graph['source_universe']['scope']}`",
        f"- 被覆：**{graph['source_universe']['coverage_status']}**",
        f"- 総エントリ：**{result['entry_count']:,}**",
        f"- フォルダ：{result['directory_count']:,}",
        f"- ファイル：{result['file_count']:,}",
        f"- シンボリックリンク：{result['symlink_count']:,}",
        f"- 完全重複グループ：{result['duplicate_group_count']:,}",
        f"- 読み取りエラー：{len(graph['coverage_audit']['missing_items']):,}",
        "- 外部通信：なし",
        "- 原本変更：なし",
        "",
        "## 質問意図契約",
        "",
        "- requested：全パスの列挙、親子関係、完全一致重複、被覆証明",
        "- not_requested：本文理解、動画文字起こし、アーカイブ展開、意味関係の推定",
        "- forbidden：原本変更、symlink追跡、外部通信、名前だけの同一視",
        "- ambiguity：今回は『パス』をファイルシステム構造として固定",
        "",
        "## 検証済みエッジ",
        "",
        "```text",
        "filesystem_root",
        "  └─ contains ─> directory / file",
        "                       └─ contains ─> child directory / file",
        "file ── exact_duplicate ──> file   （同一size + SHA-256のみ）",
        "```",
        "",
        "## 最上位ブランチ",
        "",
        "| ブランチ | 全項目 | フォルダ | ファイル |",
        "|---|---:|---:|---:|",
    ]
    for name, total, directories, files in branch_counts(entries):
        lines.append(f"| `{name}` | {total:,} | {directories:,} | {files:,} |")
    lines.extend([
        "",
        "## 監査結果",
        "",
        f"- Answerability：**{graph['answer_projection']['status']}**",
        "- すべての非rootノードに、親からの`contains`エッジが1本あります。",
        "- 重複はファイル名ではなく、ファイルサイズとSHA-256の完全一致でのみ接続しました。",
        "- 内容上の関係、因果、同一プロジェクトなどはまだ確定していません。",
        "- 次段階で意味グラフを作る場合も、この構造グラフをSource Universeとして使えます。",
        "",
        "## Integrity",
        "",
        f"- source_inventory_sha256：`{graph['integrity']['source_inventory_sha256']}`",
        f"- graph_content_sha256：`{graph['integrity']['graph_content_sha256']}`",
        f"- audit_policy_sha256：`{graph['integrity']['audit_policy_sha256']}`",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise SystemExit("root must be a real directory, not a symlink")
    output = Path(args.output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    entries, errors = enumerate_entries(root)
    records = inventory_records(entries)
    inventory_data = inventory_jsonl(records)
    graph = make_graph(root, entries, errors, sha256_bytes(inventory_data))
    atomic_bytes(output / "path-source-inventory.jsonl", inventory_data)
    atomic_json(output / "path-evidence-graph.json", graph)
    atomic_text(output / "path-evidence-graph-summary.md", summary_markdown(graph, entries))
    print(json.dumps({
        "graph": str(output / "path-evidence-graph.json"),
        "inventory": str(output / "path-source-inventory.jsonl"),
        "summary": str(output / "path-evidence-graph-summary.md"),
        "coverage": graph["source_universe"]["coverage_status"],
        **graph["answer_projection"]["result"],
        "errors": len(errors),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
