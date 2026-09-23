"""Manual Computer Use fixture: real UI/HTTP, synthetic picker/build only.

Run with Python -B, then inspect the printed loopback URL using Computer Use.
Never points at trial/production config, real documents, or an Ollama model.
Ctrl-C closes the server and removes only its own TemporaryDirectory.
"""
from __future__ import annotations

import json
import signal
import threading
from unittest import mock

from test_source_selection_http import SourceSelectionHttpTests


def main():
    SourceSelectionHttpTests.setUpClass()
    fixture = SourceSelectionHttpTests()
    fixture.setUp()
    module = fixture.module
    current = {"phase": "not_started", "message": "合成テスト：取り込み未開始"}
    counts = {"setup": 0, "selected_build": 0}
    diagnosis = {"index_ready": False, "models": ["合成テスト・実モデル0回"],
                 "warnings": [], "memory_gb": 24, "free_gb": 40,
                 "architecture": "arm64", "ollama_online": False,
                 "source_root": str(fixture.source)}

    def setup():
        counts["setup"] += 1
        current["phase"] = "ready"
        diagnosis["index_ready"] = True

    def selected_build(path, config, identity):
        assert path == fixture.source and config == fixture.config
        assert identity["path"] == str(fixture.source)
        counts["selected_build"] += 1
        current["phase"] = "ready"
        diagnosis["index_ready"] = True

    original_home = module.home

    def test_home(*args, **kwargs):
        body = original_home(*args, **kwargs)
        notice = ("<section class='card'><h2>送信確認用・合成データのみ</h2>"
                  "<p>フォルダ選択と構築は代替処理です。実資料も実モデルも使いません。</p>"
                  f"<p>セットアップ受付: {counts['setup']} 回 / "
                  f"選択後の構築受付: {counts['selected_build']} 回</p></section>")
        return body.replace(b'<main class="wrap">',
                            b'<main class="wrap">' + notice.encode(), 1)

    try:
        replacements = [
            (module.bootstrap, "diagnose", {"return_value": diagnosis}),
            (module.bootstrap, "apply_source_selection", {"side_effect": selected_build}),
            (module, "state", {"return_value": current}),
            (module, "build_worker", {"side_effect": setup}),
            (module, "home", {"side_effect": test_home}),
            (module, "review_ticket_issuer", {"return_value": None}),
            (module, "document_version_review_notice", {"return_value": ""}),
            (module, "security_exclusion_notice", {"return_value": ""}),
            (module, "semantic_graph_answer_path_status", {"return_value": {
                "state": "ready", "show_rebuild": False, "css_class": "ok", "label": "合成テスト"}}),
        ]
        for target, name, options in replacements:
            fixture.stack.enter_context(mock.patch.object(target, name, **options))
        fixture.httpd.source_updates = mock.Mock(snapshot=lambda: {"phase": "complete", "candidates": []})
        # Keep the update scanner unavailable: this fixture tests transport, not scanning.
        fixture.stack.enter_context(mock.patch.object(module, "start_source_update",
                                                       side_effect=RuntimeError("synthetic update unavailable")))
        print(json.dumps({"url": fixture.base_url, "synthetic_only": True}), flush=True)
        stopped = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        stopped.wait()
    finally:
        print(json.dumps({**counts, "picker_calls": fixture.picker.call_count,
                          "config_unchanged": fixture.config_bytes == module.bootstrap.CONFIG.read_bytes(),
                          "state_unchanged": fixture.state_bytes == module.bootstrap.STATE.read_bytes()}), flush=True)
        fixture.tearDown()


if __name__ == "__main__":
    main()
