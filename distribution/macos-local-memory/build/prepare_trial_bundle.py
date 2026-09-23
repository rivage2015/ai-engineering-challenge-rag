#!/usr/bin/env python3
"""Apply an explicit, fail-closed isolation overlay to a NEW trial bundle only.

Source defaults and installed apps are not edited. No config/index/model is
copied. Keep replacement anchors exact so future runtime changes need review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def prepare(resources: Path, profile: str, port: int, version: str) -> dict:
    if not re.fullmatch(r"LocalMemorySearch-Trial-[0-9]{8}-[0-9]{6}-[0-9]+", profile):
        raise ValueError("invalid_trial_profile")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535 or port in {8765, 11434}:
        raise ValueError("invalid_trial_port")
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}", version):
        raise ValueError("invalid_trial_version")
    resources = resources.absolute()
    if (resources.resolve() != resources or resources.name != "Resources"
            or resources.parent.name != "Contents"
            or not resources.parent.parent.name.endswith(".app")
            or "試用版" not in resources.parent.parent.name):
        raise ValueError("new_trial_bundle_required")
    stage = resources.parent.parent.parent
    manifest = stage / "trial-build.json"
    guide = stage / "試用版の使い方.txt"
    if manifest.exists() or manifest.is_symlink() or guide.exists() or guide.is_symlink():
        raise ValueError("trial_bundle_already_prepared")
    keychain = "jp.rivage.local-memory-search.trial." + profile.lower() + ".semantic-graph-root.v1"
    banner = (f'<section class="card"><b>試用版 {version}</b>'
              f'<p>既存版とは設定・索引を分けています。接続先: 127.0.0.1:{port}。'
              'Ollamaは共用するため、既存版との同時の取り込み・質問は避けてください。'
              'V1.00の全体受入はまだ完了していません。</p></section>')
    replacements = {
        "bootstrap.py": [
            ('APP_NAME = "LocalMemorySearch"', f'APP_NAME = "{profile}"'),
            ('"port": 8765,', f'"port": {port},'),
        ],
        "launch.sh": [
            ('Application Support/LocalMemorySearch', f'Application Support/{profile}'),
            ('Library/Caches/LocalMemorySearch', f'Library/Caches/{profile}'),
            ('mkdir -p "$LOG_DIR" "$CACHE_DIR"',
             'export TMPDIR="$CACHE_DIR/tmp"\nmkdir -p "$LOG_DIR" "$CACHE_DIR" "$TMPDIR"'),
            ('.get("port",8765)', f'.get("port",{port})'),
        ],
        "local_memory_server.py": [
            ('Application Support/LocalMemorySearch', f'Application Support/{profile}'),
            ('<title>Local Memory Search</title>', f'<title>Local Memory Search 試用版 {version}</title>'),
            ('<main class="wrap">{body}', '<main class="wrap">' + banner + '{body}'),
            ('parser.add_argument("--port", type=int, default=8765)',
             f'parser.add_argument("--port", type=int, default={port})'),
        ],
        "semantic_graph_trust.py": [
            ('jp.rivage.local-memory-search.semantic-graph-root.v1', keychain),
            ('KEYCHAIN_LABEL = "Local Memory Search Semantic Graph Root"',
             f'KEYCHAIN_LABEL = "Local Memory Search Trial {version} Semantic Graph Root"'),
        ],
        "semantic_graph_answer_promotion.py": [
            ('jp.rivage.local-memory-search.semantic-graph-root.v1', keychain),
        ],
        "engine/layer1/scripts/local_image_ocr.py": [
            ('/ "LocalMemorySearch"', f'/ "{profile}"'),
        ],
    }
    changes = {}
    for name, pairs in replacements.items():
        path = resources / name
        if path.is_symlink() or path.resolve() != path:
            raise ValueError("symlink_in_bundle")
        original = path.read_text(encoding="utf-8")
        content = original
        for old, new in pairs:
            expected = 2 if name == "launch.sh" and old == "Application Support/LocalMemorySearch" else 1
            if content.count(old) != expected:
                raise ValueError(f"trial_overlay_anchor_changed:{name}:{old}")
            content = content.replace(old, new)
        if name.endswith(".py"):
            compile(content, str(path), "exec")
        changes[name] = (path, original, content)
    # Every anchor and syntax check has passed before any copy is changed.
    for path, _, content in changes.values():
        path.write_text(content, encoding="utf-8")
    record = {
        "status": "trial_not_v1_acceptance", "version": version,
        "profile": profile, "port": port, "keychain_service": keychain,
        "shared": ["installed Ollama and model files"],
        "source_config_index_copied": False,
        "overlay": {name: {
            "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "trial_sha256": hashlib.sha256(content.encode()).hexdigest(),
        } for name, (_, original, content) in changes.items()},
    }
    with manifest.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    guide_text = (
        f"Local Memory Search 試用版 {version}\n\n"
        "1. 同じフォルダの「Local Memory Search 試用版」アプリをダブルクリックします。\n"
        "2. 初回は読み込みたい資料フォルダを選びます。元の資料は書き換えません。\n"
        "3. ブラウザーで「初回セットアップを開始」を押して準備完了を待ちます。\n"
        "   既存モデルを利用します。不足モデルの取得はボタンの説明を確認してください。\n"
        "4. 質問欄が表示されたら、知りたいことを入力してください。\n"
        "   対象を変える時は「読み込むフォルダを選ぶ」から変更します。\n\n"
        f"接続先: http://127.0.0.1:{port}/\n"
        f"保存先: ~/Library/Application Support/{profile}\n"
        "既存アプリ・設定・索引は引き継がず、別に保存します。\n"
        "Ollamaとダウンロード済みモデルだけ共用します。同時に両アプリで質問や取り込みをしないでください。\n"
        "これは試用版です。V1.00全体の受入検証はまだ完了していません。\n"
        "一部の対応形式には追加の読取環境が必要で、読めない場合は制限として表示します。\n"
        "従来のアプリに戻す場合は、従来のアプリを開いてください。試用版の結果は自動移行されません。\n"
    )
    with guide.open("x", encoding="utf-8") as stream:
        stream.write(guide_text)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("resources", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    prepare(args.resources, args.profile, args.port, args.version)
