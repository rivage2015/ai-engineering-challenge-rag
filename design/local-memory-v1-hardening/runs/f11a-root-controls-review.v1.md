# F11a root application controls — static review v1

2026-09-09。Task `lms-v1-f11a-notebook-metadata-2026-09-09`、担当 `/root/f03a_independent_audit`。

**訂正版 test `b2548605…` と runner `f5a9e437…` に追加の実行前 blocker は見つからなかった。** 初版の誤った cleanup oracle は修正済み。これは固定 4 control の静的レビューであり、製品 PASS・test 成功・正式製品監査ではない。実行は coherent source freeze と Root の実行許可後に限る。

Codex Graph Engineering Adapter / Graph Engineering Agentic Audit を使用。製品/test の編集・import・実行、新 agent、原本/network/model/GUI/commit は行っていない。新規作成はこの文書のみ。製品は Executor の作業中なので、実装結果の判定はしていない。

## 誤った oracle と保存された訂正

初版 `e8887c8e…` は UNVERIFIED 停止後に未公開 generation が 1 個残ると期待した。しかし bootstrap `build_index` の finally は、未公開かつ snapshot-target 例外でない generation を削除する（`:4311–4315` 周辺）。SystemExit でも finally は実行されるため、正常な exit 2 gate でもこの assertion は失敗する。

Root は初版を `f11a-root-app-gold.v1.py` に完全保存し、訂正履歴も保存した。訂正版は actual `run_tool` の RuntimeError 直前で receipt/Search の不在を記録し、unwind 後は新 generation なし・旧 byte-map/CONFIG 不変を要求する。差分はこの観測位置と誤った残存期待の訂正に限定され、製品 cleanup は変更していない。初版・訂正版とも今回の reviewer は実行していない。これは test oracle の事前訂正であり、製品 formal repair ではない。

## 固定 4 method の経路と判定範囲

| Method | 接続 / oracle | 限界 |
|---|---|---|
| `test_unverified_notebook_stops_actual_bridge_and_preserves_public_generation` | 初期 CSV/text build と current registration を確認後、missing count の synthetic Notebook を追加。`bootstrap.build_index → run_semantic_pipeline → cached bridge.main/build → checked_stage → fresh.run_tool → dispatch → intermediate/validator.main` が接続する。`fresh` は同じ実コードを未置換で再 load した module。OS `subprocess.run` だけを CompletedProcess dispatcher に置換し、real run_tool が exit 2 を解釈・log 書込・RuntimeError 化する。stage 列が intermediate/validation のみ、validator code 2、report UNVERIFIED、cleanup 前 receipt/Search 不在、cleanup 後新 generation 不在、旧全 file byte-map/CONFIG と Notebook text 不変を要求。 | 内部 run_tool の actual exit 2 gate であり、外側 bootstrap の real Popen・OS process 分離・cwd/env/stderr 忠実再現・error STATE 表示は未検証。report の exact reason/count はこの method 単独では検査しない。|
| `test_changed_reader_contract_holds_old_generation_without_mutating_it` | current resource object を deepcopy し Probe の期待 hash だけを合成値へ変更。real `reader_generation_contract_status` の migration 判定と旧 generation/CONFIG 不変を確認する。 | synthetic current-resource mismatch control。真正な旧版 Notebook metadata 欠落や実際の旧 reader 再生ではない。method 単独では mutation 前 current / exact reason を assert しないが、同じ固定 suite の第 1・第 3 control には current 正例がある。|
| `test_explicit_rebuild_creates_new_generation_and_keeps_previous_bytes` | real bootstrap build を 2 回通し、新 index path、旧 generation byte-map 不変、新 current registration を要求する。 | seed は CSV/text。F11a Notebook 正例の app/回答伝播、モデル品質、旧版からの実移行を証明しない。|
| `test_probe_and_schemas_are_in_existing_package_copy_contract` | 注入された保護 build script の文字列に 6 scripts / 2 schemas の明示 source path があり、Probe が bootstrap / managed builder の processing lists にあることを確認。今回その実 cp block も直接読んだ。 | literal copy-list membership control。コメント化等まで解析する shell-semantic test や package build/import smoke test ではない。streaming Search validator は元から未同梱で、存在を主張しない。|

第 1 control は broad assertRaises だけではなく exact stage error、stage 列、CLI code/report と観測時点を併用しており、別箇所の早期例外を gate 成功に数えにくい。`fresh.subprocess` は共有 subprocess module なので、その `run` patch は一時的に process-wide となる。対象の Reader metadata/model subprocess は既存 harness が先に stub し、他の OS launch は guard/Popen 禁止で止まる。これを一般的な OS 実行再現とは呼ばない。

## Harness / runner / 副作用

- 既存 harness 全 469 行を読了。module top level は loader/定義のみ。`setUp` は owned TemporaryDirectory 配下へ source/workspace/SUPPORT/CONFIG/STATE/decision paths を向ける。bootstrap.run は in-process CLI、ensure_models/local_model_available は stub、Popen/HTTP は禁止。Reader OCR/VLM/Paddle/PDF runtime identities と password discovery を置換し、index embedding は fixed finite vector。継承された回答/model test は選ばない。
- runner は METHODS の明示 4 名から `NotebookApplicationTests(name)` を組む。`loadTestsFromModule` の redirect は target module に限定されるため、継承 17 methods の discovery は発動しない。F04 resolver wrapper の補助 harness setUp は走るが、その inherited tests は走らない。
- package script は guard 開始前に一度だけ read-only snapshot し、class の `package_copy_text` に注入する。保護 script を execute せず、worker の一般 read root を広げない。正式証跡にはこの script hash も保持する。現在 source hash は下記。
- F04 guard と F05b budget 全文、supervisor 全文も確認した。guard は reviewed code/runtime/owned tmp 以外の open、network/process を拒否し、tmp 外 write を拒否する。budget AUTHORS は新 test と既存 harness の 2 paths に限定され、両方の explicit Path writes を計数する。source fixture per-case 16 KiB / explicit cumulative 1 MiB。製品生成 artifact の全 disk/RSS 上限ではない。
- supervisor は fresh run ID `f11a-root-app-NNN` ごとに exclusive run directory、single `-I -B -u` child、30 s / captured log 1 MiB を使う。footer/exit/skip が一致しなければ passed へ昇格しない。全 attempts を保存する。OS sandbox やあらゆる外部 I/O 不在の証明ではない。
- build は synthetic generation、共有 synthetic review/state/log、lock files を作り、失敗 generation を削除する。CONFIG と旧 generation の不変は検査するが、共有 STATE/review/log の不変までは要求していない。この区別は正当。

## 実行・受入時に残す境界

Source freeze 前にはこの runner を実行しない。実行後は 4 methods / 0 required skips、budget line、result/log/hash を確認し、method 単独の未検証事項を suite 全体の証明へ広げない。optional な oracle 強化は migration 前 current＋exact reason、第 1 control の missing-count reason 明示だが、上記限定の内部 gate / synthetic mismatch control を成立させるための追加 blocker とはしない。本文 binding、membership、F11b、full package/model/OS runtime は別契約・別証拠のまま。

## 読取 SHA-256

```text
e8887c8ea0a401832bec05cee7462e6879cfb0d5dcc06ec193b9e214ee1237f8  runs/f11a-root-app-gold.v1.py (initial test exact bytes)
b254860584650f376400a50d3890b9cc6074633427a565787b91aae11965496a  tests/test_notebook_metadata_application.py (corrected)
d7334b4ac673ec37b6226ce33f2dc2a3d3f12c37dd5c2d847e9950dd06d561e2  runs/f11a-root-app-gold-correction.v1.md
f5a9e437fc0f28474eb24ceca8abb43c70634302710159aa056a2938c2e18539  runs/f11a-root-app-run.v1.py
bd967cb2e4d22aa3985d59514a72e22e42e9e9d03841dbc6b80164a69c9f97ff  distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py
e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b  distribution/macos-local-memory/app/bootstrap.py
5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a  distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py
10989f5f941c1e567a7a8c7fe82f2d5d5d88e66345d4503a1600c7c9827bd02f  distribution/macos-local-memory/build/build_package.sh
7c6953d3e92c46ede699b81690a138fb50e74e51b79fc37d14f9fc44d6c4391b  runs/f04a-executor-run.v1.py
f4c4a647997334385fb02edc2b120624cda9b0da95e438164eacdca0c80b24af  runs/f05b-fixture-budget.v1.py
6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9  scripts/run_local_memory_hardening_tests.py
```

`runs/` は `design/local-memory-v1-hardening/runs/`。implementation gate は読了したが、Executor の変更中 10-file source を正式 freeze と見なしたり、未実行の制御を PASS と記録したりしていない。
