# F11a lineage version review v1 — bounded static review

Task `lms-v1-f11a-notebook-metadata-2026-09-09`。2026-09-09。使用 skill: codex-graph-engineering-adapter / graph-engineering-agentic-audit。これは既存の診断を行った同じ reviewer context による手続的レビューであり、新たな独立 context の正式製品監査ではない。F11a correction 1 を変更・リセットしない。

## 結論

今回指定された範囲で追加 blocker は見つからない。親の version scope は、Search current 0.7.0 の exact gate と historical native structural tuples を分離する契約解釈を明示している。新しい正例は元 method の全 assertions を実行する構成で、保存された実 log/result は 2 methods 成功 / skip 0 と一致する。旧 ERROR は元の failed run として保持されている。

これは static source review と親の既存実行証拠の照合である。今回の新規 test 実行・import・compile・製品変更・既存 test/gold/log 編集は行っていない。新規本書だけを保存する。generator v3 の作成・実行、formal artifact の受理、全 lineage suite / 全 F11 / 全 V1 の受理は対象外。

## 指定事項の確認

| 確認対象 | 読み取った具体的根拠 | 判断・限界 |
| --- | --- | --- |
| current version 契約 | `f11a-task-contract.v1.md:80–82` は Search 0.7.0 と validator/adaptive pin 同期を指定。current adaptive line 101 は 0.7.0、1291–1296 は exact provenance check。`f11a-lineage-version-scope.v1.md` は old Search 0.6.0 の current 扱いを明示的に許可しない。 | 元診断の解釈を親が採用。製品 gate 緩和はない。native structural historical tuples の保持とは別。 |
| original test 全文の保持 | gold 読込み時に `tests/test_semantic_lineage_relations.py` の full source SHA-256 `26f1624c…` を照合。今回も実 hash が同じ。 | 元 test の黙示編集なし。静的 hash 照合であり、実行時全 source の同時 snapshot 証明ではない。 |
| current fixture の差分 | `current_fixture()` は保存した元 helper から新しい dict を生成し、provenance の version を literal `0.7.0` にし、同じ version を含む identity から Search unit ID を再計算する。builder、doc/source IDs、locator、text hash、timestamp、deterministic は元のまま。 | SUT expected-version 定数の読取りで期待版を追従させていない。元 helper 自体や原本 test bytes は変更しない。 |
| original positive 全 assertions | gold は `SemanticLineageRelationTests("test_unsharded_table_row_promotes_exact_stable_fan_in")` を作り、`mock.patch.object(original, "search_unit", current_fixture)` 内でその method を直接呼ぶ。元 class に専用 setUp/tearDown/run override はなく、method 内に早期 return / skip / 例外捕捉もない。 | line 162–217 の実本体を呼ぶので assertion を抜いたコピーではない。method の AssertionError / ValueError は外側の unittest へ伝播する。 |
| fan-in の具体的回収範囲 | 元 method line 172–217 の relations 2、verified derived 1、verified relation 2、held 0、to refs A/B、lineage/derived_from、derived from-ref、supporting refs、verified status、provenance、relation ID、fan-in hash、反転入力での relations/coverage 不変。 | すべて保持。`derived_projection(unit)` も current unit から projection ID / source_search_unit_id を作るため関連 ID は整合する。元 ERROR で未到達だった assertions を新正例で回収する。 |
| SUT 非差替え | gold の唯一の patch は test module の `search_unit`。runner の dummy `target.builder = SimpleNamespace()` が F04 の run_tool 代入を受けるため、original.validator / original.adaptive_builder を差し替えない。 | 対象 validator の定数・derive method・fail・assertions を patch していない。共有 harness の既存 network/model/process 拒否と tmp bootstrap 設定まで「patch なし」とは主張しない。 |
| legacy untouched exact failure | `historical_fixture` は patch 前の元 `search_unit` 関数への参照。legacy method はこの参照を直接呼び、literal 0.6.0 を確認。元と同じ A/B、semantic 順序、layer を構成し、`ValueError` の全文が `lineage_search_unit_provenance_invalid:` + coherent unit ID と一致することを要求。 | generic ValueError だけでは成功しない。stale ID / source IDs の先行エラーも受理しない。current positive 後には test helper の復元も assertIs で確認する。 |
| preserved failed history | original `f11a-collateral-lineage-001` の result/log は前診断と同じ hash。status failed、exit 1、3 methods、2 ok + 1 ERROR、skip / expected failures 0。 | 新しい legacy 拒否対照の成功で、元正例の ERROR を事後的に expected failure / PASS に変更していない。 |

## Runner と実行証拠

新 runner 全文、F04 guard 全文、F05b explicit budget 全文、共有 harness の module/setUp/module/run_cli を読取確認した。runner は固定 2 method を 1 suite に入れ、重複 load / discovery や未実行 selection を拒否する。F04 `worker("resolver")` が guard を開始してから target を読み込む。共有 harness の inherited E2E methods は選ばれない。supervisor は既存の 30 s / 1 MiB log の bounded worker、新しい run ID の形式を使用する。

`f11a-lineage-version-001/result.json` は次の実測記録。log SHA と実 file SHA は一致した。

- status `passed`、exit 0、tests_reported 2、skipped_reported 0、expected_failures_reported 0。
- log の二つの exact method 名がともに `ok`、footer `Ran 2 tests` / `OK`。
- runtime `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ/rag/.venv/bin/python -I -B -u`。elapsed 0.232372 s、log 479 bytes。
- explicit fixture counter は bytes / writes / source count / source max がすべて 0。二つの method は in-memory records のみ。共有 harness は owned tmp CONFIG を製品関数から作成するため、この 0 を worker 全体の「一切書込みなし」とは解釈しない。

Python guard と explicit fixture counter は OS sandbox / 全 RSS / 全生成 artifact の総量 cap ではない。今回の scoped controls に実資料・モデル・network・GUI を要求する経路は見られない。実行は親が行った一回の記録であり、reviewer が独立に再実行したとの主張はしない。

## 保持する限界

元 method は stable ID / projection ID / relation ID / canonical hash や provenance 名の期待値に SUT helper/constants を再利用する。新 current fixture も同じ stable ID helper を使う。したがって「既存 fan-in 回帰の全 assertions が current version で動く」証拠であって、ID/hash アルゴリズムの独立 literal gold ではない。今回この oracle を独立検証済みへ昇格させていない。

legacy 対照は旧 Search unit の version gate だけを対象とし、旧 app 全体、全 generation migration、未選択の他 lineage methods を実行していない。元 suite の共有 historical helper はそのまま残っている。必要な正式 artifact/source packet と別 context の formal audit は引き続き別段階である。

## SHA-256 参照

workspace root は `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。表の `runs/` は `design/local-memory-v1-hardening/runs/`。すべて今回 read-only で実 hash を確認した。

| path | SHA-256 |
| --- | --- |
| `runs/f11a-lineage-version-scope.v1.md` | `0cf546851da53863f01a848324dbcdbe4e4c8cf8b27cff0a8ff11787a3961647` |
| `runs/f11a-lineage-version-gold.v1.py` | `f06011075ae61d6d079e84d8506ca4582e8c8952058538b217e3d1bb8ac309de` |
| `runs/f11a-lineage-version-run.v1.py` | `2d38fdaeb4305debd86afdad95e9f1360a34b0dd09694e718266cd37e009281b` |
| `runs/f11a-lineage-version-001/result.json` | `2efea799367558707d1e7c3053ab4e0a84f07291e73589ee371fd5b2b6232e4a` |
| `runs/f11a-lineage-version-001/unittest.log` | `a992412ab9e1b465477deb919a694405e3eca039d0484d5732891d6d653cc3a8` |
| `runs/f11a-lineage-collateral-diagnosis.v1.md` | `a0462aa11cfb77a0dc0fc52f07d113ef9e6deeaae249664e176b8d9e29a1a490` |
| `runs/f11a-collateral-lineage-001/result.json` | `d2fd145407cde83f5ae43a43c6e57e7c3bb23774e86c3dbd08285204edeb0b42` |
| `runs/f11a-collateral-lineage-001/unittest.log` | `f76a721c2110cf1f388479f2edcc6c1337c9f8fcb14773fa56b9d54149f01cd2` |
| `tests/test_semantic_lineage_relations.py` | `26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0` |
| `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` | `c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3` |
| `runs/f11a-task-contract.v1.md` | `20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15` |
| `runs/f04a-executor-run.v1.py` | `7c6953d3e92c46ede699b81690a138fb50e74e51b79fc37d14f9fc44d6c4391b` |
| `runs/f05b-fixture-budget.v1.py` | `f4c4a647997334385fb02edc2b120624cda9b0da95e438164eacdca0c80b24af` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `bd967cb2e4d22aa3985d59514a72e22e42e9e9d03841dbc6b80164a69c9f97ff` |
| `scripts/run_local_memory_hardening_tests.py` | `6b3bde90b6ed649a82325f6b5ad9661756bef7b6e0303fa74c2d045fb26eb4a9` |
