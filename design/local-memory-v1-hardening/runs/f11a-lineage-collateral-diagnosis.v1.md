# F11a lineage collateral diagnosis v1

Task `lms-v1-f11a-notebook-metadata-2026-09-09`。2026-09-09。独立 reviewer による bounded read-only diagnosis。使用 skill: codex-graph-engineering-adapter / graph-engineering-agentic-audit。same-model separate context の手続的分離のみ。正式製品監査／製品 PASS ではない。

今回の書込みは新規本書のみ。製品・既存 test・runner・gold・旧記録は変更せず、Python import、test 実行、fixture 生成もしていない。以下の実行結果は親が保存した log/result を直接読んだ結果である。

## 結論

今回の一件は、自己整合した **historical Search 0.6.0 fixture が current Search 0.7.0 の exact version gate に拒否されたもの**。この ERROR だけから current 0.7.0 の fan-in 処理の製品 regression は立証できない。元の正例は current 版用としては古い fixture である。

契約の Search 0.7.0 への同期と、既存 native structural producer tuples の保持は別の境界である。before にも Search の複数版 allowlist は存在せず、当時の一つの版との exact 比較だった。したがって製品 pin の緩和を最小修正とはしない。**親が互換範囲の解釈を明示してから、別名の current 正例と untouched historical 拒否対照を固定・実行する**のが最小の次段階。解消までは formal artifact を draft に留める。

## 観測と拒否箇所

`runs/f11a-collateral-lineage-001/result.json` は status `failed`、exit 1、tests_reported 3、skip 0、expected_failures 0。log は次を示す。

| selected method | 保存された結果 | 到達範囲 |
| --- | --- | --- |
| `test_unsharded_table_row_promotes_exact_stable_fan_in` | ERROR | test line 168 → adaptive line 1291 の Search provenance gate。後続 fan-in assertions は未到達。 |
| `test_native_section_contains_requires_real_heading_evidence` | ok | literal managed extractor 0.10.1 と実 heading evidence の条件。 |
| `test_full_validator_publishes_only_after_pass` | ok | current producer の CSV → Reader → lineage → index と改変拒否。 |

一件目の exact error は `ValueError: lineage_search_unit_provenance_invalid:su_d59a0eeffdd8f7a32a06d66ff2aeb9fe`。3 methods / 2 成功 + 1 ERROR を 3 PASS や expected failure に読み替えない。log の実 fixture budget は explicit_test_bytes 3560 / writes 3、source_fixture_count 1 / max_source_fixture_bytes 28。30 s / 1 MiB log の run は elapsed 0.529435 s、log 1652 bytes。

`tests/test_semantic_lineage_relations.py:54–89` の `search_unit()` は provenance に literal `builder_version: "0.6.0"`（line 75）を置く。line 80–88 の stable ID もその同じ値を使うため、version のみを変えて ID を放置した壊れた fixture ではない。builder 名、deterministic True、RFC3339 timestamp は後続 provenance 条件を満たす。

current `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py:1269–1296` は、stable ID、document/source IDs を先に検査してから、provenance の builder/version/deterministic/timestamp を検査する。line 101 の expected Search version は `0.7.0`。今回の literal とこの値の不一致が exact error と一致する。派生 Evidence の対応と fan-in の内容検査はその後なので、この失敗をそれらの正常性証拠に数えない。

## 契約・before/current の対照

| 境界 | 規範と source | 今回の判定 |
| --- | --- | --- |
| current Search identity | `f11a-task-contract.v1.md:80–82` は adaptive の変更を identity pins に限定し、Search builder 0.7.0 と validators/adaptive の同期を指定。before adaptive line 101 は 0.6.0、current は 0.7.0。 | current consumer が古い Search 0.6.0 を current と扱う義務は、この規定からは導けない。 |
| historical native structural tuples | 同契約 line 82 の new managed structural tuple と historical tuples 保持。`f11a-contract-review.v1.md:50–52` も旧 structural tuples と記載。current adaptive line 111–117 は旧 0.7.0 / 0.8.0 / 0.10.1 / 0.11.0 の四つを保持し 0.12.0 を追加。 | `derive_native_structural_relations` の tuple membership（line 1035）に関する互換性。Search の一つの exact pin とは異なる。0.10.1 control の実 ok と整合。 |
| current full validator | adaptive line 2718–2725 は Search build-state の version を exact pin と比較、2738–2747 は各 unit の provenance と run を照合。 | 低水準 helper だけ historical version を許可しても、whole validator の current provenance 契約と一致しない。 |
| historical generations | 既存世代を変更せず migration を要求することと、旧 Search unit を現版として受理することは別。今回の純粋 helper 失敗は旧 CONFIG / generation migration の実行ではない。 | この log だけで旧 app 全体の互換性、migration 全経路、全 non-Notebook 形式を判定しない。 |

before/current の read-only `diff -u` と保存 delta を確認した。adaptive の変更は **Search version 0.6.0 → 0.7.0 と native tuple 0.12.0 追加の二 hunk のみ**。before line 1288 以降にも同じ exact Search version 比較があり、fan-in アルゴリズムの変更はない。before の実行は今回していないので、その runtime PASS を新規に主張しない。

`f11a-task-contract-addendum.v1.md` は partial reason と module import / 実資料取り込みの区別を補足するもので、historical Search allowlist は追加していない。`f11a-implementation-gate.v1.md` の既存 test 無断編集禁止と履歴保持は継続する。広い「non-Notebook 互換」「関連回帰」の文言を、既存の exact version gate を解除する規定へ拡張しない。

## 前 preflight の訂正と残る必須確認

私の `f11a-collateral-preflight.v1.md:20` は「literal historical Search 0.6.0 / managed extractor 0.10.1 の保持」を同じ正例選択理由にまとめていた。**Search 0.6.0 の正例維持まで期待した部分は過剰解釈だった**。元 preflight、runner、test、failed run は不変で残し、本書で訂正する。製品欠陥の修理回数や製品 PASS に読み替えない。

次は以下の二 method を別名・別ファイルの gold として提案する。これは本書時点では未作成・未実行であり、既存 test の黙示書換え許可ではない。

1. **current literal 0.7.0 の正例**。元の table row、source IDs、endpoint、入力順序を維持し、expected version は SUT 定数の読取りでなく literal `0.7.0` に固定する。Search unit ID、projection ID、`source_search_unit_id` の関連値を同じ current fixture から整合させる。元 method の relations 2、verified derived 1、verified relations 2、held 0、正しい from/to/supporting refs、verified status、provenance、relation ID、fan-in hash、順序反転不変の全 assertions を保持する。単に例外が無くなるだけの正例にはしない。
2. **historical literal 0.6.0 の拒否対照**。元 helper の self-consistent old unit/projection と期待 hash/ID を保持し、`lineage_search_unit_provenance_invalid:<その unit ID>` を exact に要求する。generic ValueError、stale ID による先行失敗、任意の別エラーでは合格にしない。current 正例と入力差を限定して比較する。これは Search unit version gate の対照であり、旧 app / 全 migration の replay ではない。

既存の historical structural 0.10.1 と actual current full-pipeline の二つの成功結果は別の証拠として保持する。元 first method の未到達 assertions は上記 current 正例で初めて回収する。他にも `search_unit()` を共有する未選択 methods があるため、今回の 3-method selection から全 lineage suite の互換性を推定しない。

親の scope 明記 → 新 gold / runner の全文・hash review → owned tmp / 固定 selection / 30 s / 1 MiB log / source ≤16 KiB の新 run ID で実行 → 元 ERROR と新結果を併記、の順とする。製品 allowlist 拡張、pin を 0.6.0 に戻す操作、SUT 定数 monkeypatch、skip 追加、元失敗の削除・再分類はいずれも提案しない。

## 固定参照

workspace root は `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。表の `runs/` は `design/local-memory-v1-hardening/runs/`。下記 SHA-256 を今回 read-only で再確認した。

| path | SHA-256 |
| --- | --- |
| `runs/f11a-task-contract.v1.md` | `20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15` |
| `runs/f11a-task-contract-addendum.v1.md` | `2542f29b2776127a5d919639c69505d31b4efdf46968d9ff80a0af7d930f6f8f` |
| `runs/f11a-contract-review.v1.md` | `7640c4ca4ad2aa770d061701e2dc26924761a422eaecf7d50bd86b1a98f1fa3d` |
| `runs/f11a-implementation-gate.v1.md` | `fc3f2c41e0eae4bed65588565871e665c62e368b7a6aff92fe601287f4c866b6` |
| `tests/test_semantic_lineage_relations.py` | `26f1624c0dcdddacd86f8cf21253247db9f3f330d764c9b9e4cad80f8e41f4d0` |
| `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` | `c2587b685d06a1e8d1ec006bb2577be47168a0c14b072a22b3a90878c30563e3` |
| `runs/f11a-executor-before-adaptive-validator.v1.py` | `17c5de11a6f8f958ea5d1db447840f5653128ef24314fad193a610f824c5a106` |
| `runs/f11a-executor-adaptive-validator-delta.v1.diff` | `6ccfa7ebad96d34d16751a5dba8dafc408242f7da04bce42001db85e7744070a` |
| `runs/f11a-collateral-preflight.v1.md` | `9296f68a5bb939f855e042566993cb95a37fcf1ed8b8dda2b38938ade704c9d4` |
| `runs/f11a-collateral-run.v1.py` | `5a5c78e853aa3b611c67d482d24be82330ec9c3154a88135bb8b6538dd2716d1` |
| `runs/f11a-collateral-lineage-001/result.json` | `d2fd145407cde83f5ae43a43c6e57e7c3bb23774e86c3dbd08285204edeb0b42` |
| `runs/f11a-collateral-lineage-001/unittest.log` | `f76a721c2110cf1f388479f2edcc6c1337c9f8fcb14773fa56b9d54149f01cd2` |

本書は原因と最小の再確認案を固定したもの。current fixture の新しい実行結果、正式 artifact の受理、全 F11 / 全 V1 の完成は含まない。
