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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = "1.1"
BUILDER_VERSION = "0.3.0"

# Capability identity is captured before tests instrument individual OS calls.
_HAS_FD_OPERATIONS = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.readlink in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SourceChangedError(OSError):
    """An observation no longer describes the object bound to its pathname."""


def _identity(metadata: os.stat_result) -> tuple:
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def _revision(metadata: os.stat_result) -> tuple:
    # Do not include atime: reading a source may legitimately update it.
    return (*_identity(metadata), metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_nlink)


def _require_same(expected: os.stat_result, actual: os.stat_result, *, revision: bool = True) -> None:
    key = _revision if revision else _identity
    if key(expected) != key(actual):
        raise SourceChangedError("source identity or metadata changed during observation")


def _absolute_root(root: Path) -> Path:
    # Do not resolve aliases or erase a symlink/.. component before checking it.
    if ".." in root.parts:
        raise OSError("root must not contain parent traversal components")
    return root.absolute()


def _open_verified_directory(parent_fd: int, name: str, expected: os.stat_result) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _require_same(expected, os.fstat(descriptor))
        _require_same(expected, os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _root_descriptors(root: Path):
    """Keep each supplied root component bound without following any symlink.

    A caller that intentionally uses an ancestor alias must supply the explicit
    canonical directory instead. There is no pathname-based fallback.
    """
    if not _HAS_FD_OPERATIONS or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise OSError("descriptor-bound no-follow filesystem operations are unavailable")
    descriptors = []
    bindings = []
    try:
        descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        bindings.append((descriptor, None, None, os.fstat(descriptor)))
        for name in root.parts[1:]:
            expected = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise OSError("root components must be real directories, not symlinks")
            parent_fd = descriptor
            descriptor = _open_verified_directory(parent_fd, name, expected)
            descriptors.append(descriptor)
            bindings.append((descriptor, parent_fd, name, expected))
        yield bindings
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def sha256_file(path: Path, *, dir_fd: int | None = None, expected: os.stat_result | None = None) -> str:
    """Hash one stable regular-file descriptor, never a symlink or special file."""
    if dir_fd is None:
        absolute = _absolute_root(path)
        with _root_descriptors(absolute.parent) as bindings:
            value = sha256_file(Path(absolute.name), dir_fd=bindings[-1][0])
            _verify_root_bindings(bindings)
            return value
    name = path.name
    expected = expected if expected is not None else os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    if not stat.S_ISREG(expected.st_mode):
        raise OSError("hash source must be a regular file")
    # O_NONBLOCK avoids hanging if a FIFO replaces the regular file before open.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    digest = hashlib.sha256()
    try:
        _require_same(expected, os.fstat(descriptor))
        remaining = expected.st_size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                raise SourceChangedError("source was truncated during hashing")
            digest.update(block)
            remaining -= len(block)
        _require_same(expected, os.fstat(descriptor))
        _require_same(expected, os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _verify_root_bindings(bindings: list) -> None:
    for index, (descriptor, parent_fd, name, expected) in enumerate(bindings):
        # Unrelated changes outside the selected root do not invalidate it.
        revision = index == len(bindings) - 1
        _require_same(expected, os.fstat(descriptor), revision=revision)
        if parent_fd is not None:
            _require_same(expected, os.stat(name, dir_fd=parent_fd, follow_symlinks=False), revision=revision)


def _directory_names(descriptor: int) -> list[str]:
    with os.scandir(descriptor) as iterator:
        return sorted((item.name for item in iterator), key=os.fsencode)


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


def birthtime_ns(metadata: os.stat_result) -> int | None:
    """Return the filesystem creation time when the platform exposes it."""
    value = getattr(metadata, "st_birthtime_ns", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    seconds = getattr(metadata, "st_birthtime", None)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return int(seconds * 1_000_000_000)
    return None


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
    root = _absolute_root(root)
    entries: list[dict] = []
    errors: list[dict] = []

    def error(relative: str, operation: str, exc: OSError) -> None:
        errors.append({"path": relative, "operation": operation, "error": f"{type(exc).__name__}: {exc}"})

    # Root validation raises instead of publishing an empty, apparently valid
    # inventory for a root that was never safely opened.
    with _root_descriptors(root) as bindings:
        root_fd, _, _, root_stat = bindings[-1]
        stack = [{"fd": root_fd, "parent_fd": None, "name": None, "relative": ".", "stat": root_stat, "names": None, "record": None}]

        def verify_active() -> None:
            _verify_root_bindings(bindings)
            for frame in stack[1:]:
                _require_same(frame["stat"], os.fstat(frame["fd"]))
                _require_same(frame["stat"], os.stat(frame["name"], dir_fd=frame["parent_fd"], follow_symlinks=False))

        def invalidate_tree(relative: str, exc: OSError) -> None:
            error(relative, "tree_identity", exc)
            # Invalidate immediately on the observation that failed. An
            # ancestor can be restored before the next traversal iteration.
            for record in entries:
                record["read_status"], record["sha256"] = "unresolved", None

        try:
            while stack:
                frame = stack[-1]
                try:
                    verify_active()
                except OSError as exc:
                    invalidate_tree(frame["relative"], exc)
                    break
                if frame["names"] is None:
                    try:
                        frame["names"] = iter(_directory_names(frame["fd"]))
                    except OSError as exc:
                        error(frame["relative"], "scandir", exc)
                        if frame["record"] is not None:
                            frame["record"]["read_status"] = "unresolved"
                        frame["names"] = iter(())
                    # Recheck directory metadata after obtaining its name list.
                    continue
                name = next(frame["names"], None)
                if name is None:
                    stack.pop()
                    if frame["parent_fd"] is not None:
                        os.close(frame["fd"])
                    continue
                path = Path(name) if frame["relative"] == "." else Path(frame["relative"]) / name
                relative = path.as_posix()
                parent_fd = frame["fd"]
                record = None
                try:
                    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    kind = kind_from_mode(metadata.st_mode)
                    record = {
                        "relative_path": relative,
                        "normalized_path": unicodedata.normalize("NFC", relative),
                        "parent_path": frame["relative"],
                        "basename": name,
                        "kind": kind,
                        "extension": extension(path, kind),
                        "size_bytes": metadata.st_size if kind == "file" else 0,
                        "mtime_ns": metadata.st_mtime_ns,
                        "birthtime_ns": birthtime_ns(metadata),
                        "mode": stat.S_IMODE(metadata.st_mode),
                        "symlink_target": None,
                        "sha256": None,
                        "read_status": "observed",
                    }
                    entries.append(record)
                    if kind == "file":
                        digest = sha256_file(Path(name), dir_fd=parent_fd, expected=metadata)
                        try:
                            verify_active()
                        except OSError as exc:
                            invalidate_tree(relative, exc)
                            break
                        record["sha256"] = digest
                    elif kind == "symlink":
                        target = os.readlink(name, dir_fd=parent_fd)
                        _require_same(metadata, os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                        record["symlink_target"] = target
                        record["sha256"] = sha256_bytes(("symlink\0" + target).encode("utf-8"))
                    elif kind == "directory":
                        descriptor = _open_verified_directory(parent_fd, name, metadata)
                        stack.append({"fd": descriptor, "parent_fd": parent_fd, "name": name, "relative": relative, "stat": metadata, "names": None, "record": record})
                except OSError as exc:
                    if record is not None:
                        record["read_status"], record["sha256"] = "unresolved", None
                    error(relative, "source_observation", exc)
        finally:
            for frame in reversed(stack[1:]):
                os.close(frame["fd"])
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
    # Root is depth0, not tied with its top-level children. Every child digest
    # must exist before the parent includes it in its canonical child list.
    for directory in sorted(directories, key=lambda value: (-(0 if value == "." else len(Path(value).parts)), os.fsencode(value))):
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
        for key in (
            "relative_path", "kind", "size_bytes", "mtime_ns",
            "birthtime_ns", "sha256", "read_status",
        )
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
        "status": "unresolved" if errors else "observed",
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
        source_hash = (dir_hash[item["relative_path"]] if item["kind"] == "directory" else item["sha256"]) if item["read_status"] == "observed" else None
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
                "birthtime_ns": item["birthtime_ns"],
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
            "enumeration_rule": "descriptor-bound os.scandir and no-follow child opens; stat identity and metadata checked during observation",
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
    root = _absolute_root(Path(args.root))
    entries, errors = enumerate_entries(root)
    output = Path(args.output_dir).resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
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
