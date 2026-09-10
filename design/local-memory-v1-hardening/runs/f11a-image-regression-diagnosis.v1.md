# F11a image regression diagnosis v1

2026-09-09。担当 `/root/f03a_independent_audit`。Adapter / Agentic Audit を使用した bounded read-only diagnosis。新規作成は本診断文書のみ（追加指示の独立 gold は別途準備）。製品/test の変更・import・再実行はしていない。正式監査・製品 PASS ではない。

## 結論

**IMAGE の error は古い Notebook fixture の問題ではなく、F11a の新しい適用分類が既存の画像由来 provisional `text_block` まで保存出力と扱う製品回帰。** `notebook_state` を画像由来 VLM record に後付けする fixture 修正では直さない。画像/visual state は今回の metadata slice 外で、既存の暫定表現と系統を保つ必要がある。

親の実 run `f11a-root-images-001` は 3 methods、1 error、2 ok、0 skip。監査者の新規実行ではない。失敗ログ/結果は保存したままにする。app3 ERROR は本診断の対象外。

## 因果経路の根拠

1. `tests/test_local_embedded_visual_pipeline.py:313–340` は code cell の `execution_count:null` と source text、image-only display_data を含む小さな `.ipynb` を実際に書き、**現行** `Probe.extract` を呼ぶ。旧版 Evidence の手書き snapshot ではない。OCR/VLM 応答だけが mocked。test 全体の hash は preimplementation に読んだ値から不変。
2. 現 Probe `:7066–7074` はこの source `notebook_cell` に `notebook_state` を付ける。出力 data に `text/plain`/text はなく、通常 saved-output text Evidence は生成されない。既存 `_assert_pipeline` は Search 前に image=1、ocr_line=1、VLM provisional=1 と、その parent/origin/source hash の一致まで確認している（`:349–420`）。ログがそこを通過していることも分かる。
3. 既存 `_add_local_visual_observation`（現 Probe `:6012–6050`、before `:5698–5738`）は image parent を持つ `text_block`、method `local_vlm_visual_observation_provisional`、quality/marker/visual_origin を生成する。Notebook container の `notebook_cell_index` と visual-specific locator は既存 `_merge_visual_location` により維持される。この record に保存実行 state はないのが正しい。
4. 新 Search builder `consume(:818)` は全 Evidence に `notebook_evidence_state` を先行実行する。新 helper（Probe `:2718–2751`）は Document がない場合 `notebook_cell_index` で Notebook と判断し、kind が `text_block` なら textual とする。image parent/provisional visual method を分類せず、state 欠落を `notebook_rebuild_required` とする。ログの throw site はここに一致する。
5. before Search builder の consume にこの呼出しはなく、before/current の visual emitter は同じ `text_block` 契約を保つ。差分により新しく既存 visual record が拒否されるようになった。fixture を古いとして更新する根拠はない。

実行ログには offending Evidence ID 自体はないが、fixture・source state 付与・saved text 不在・到達済み assertions・唯一の provisional text 生成経路から当該 record を静的に特定できる。監査者が実際の tmp record を捕捉したと偽らない。

残る attachment/code-data-URI 2 methods は Probe/image/source assertions で止まり、`_assert_pipeline → Search` を呼ばない。したがって 2 ok はこの下流経路が健全である対照にはならない。

## 最小 remedy（提案、編集許可ではない）

- shared Notebook applicability を canonical source/saved-output text と既存 image-derived provisional text に分ける。正当な image-derived record に notebook_state を要求・伝播しない。Search consume だけの迂回では不足し、add_direct_text / native・stream Search checks / intermediate binding も同じ分類にする。
- `notebook_document_binding` は現在 kind `text_block` だけで None state を unchecked/unparsed に計数する（`:2797–2810`）。正当な image-derived record を除外するときは、この計数も対象外とし、偽の notebook_state_unparsed を足さない。
- **method 名や provisional という自己申告だけで skip しない。** 既存 visual parent image、same-document lineage、visual_origin/location、quality/marker/provenance の条件を維持して分類する。既存 Search `provisional_visual_text_contract_errors(:599–680)` は parent/origin/locator/context を確認する再利用可能な根拠。stream intermediate の既存 `visual_source_binding_contract_errors` は OCR-only なので、それだけで VLM も検査済みとはしない。
- source/saved-output state 欠落、native text を visual とラベル付けしただけの逃避、visual record への state 注入、無効 parent/origin は拒否を保つ。全画像内容の原本証明や本文/membership 完全性を追加で主張しない。
- producer の VLM 保存は Document を partial にする。修正後の intermediate report は、この fixture について **UNVERIFIED / notebook_extraction_incomplete** のままが契約通り。画像 discovery の build/Search/adapter が動くことと app gate の PASS は別であり、partial を success に直して通してはいけない。

## 固定すべき確認

既存 failing method と残る 2 methods は期待を変更せず再実行する。追加 gold は (a) genuine producer VLM の build＋両 Search validator、暫定 marker/origin/parent と state 非付与、(b) intermediate 3 paths の partial report・正しい metadata 件数、(c) native saved-output の visual relabel と image parent/provenance 偽装、(d) genuine visual への state/parent/origin 改変を含める。positive baseline を先に確認し、早期 fixture error を negative 防御成功にしない。小型 synthetic/mock image、既存 guard、明示 method allowlist だけを用い、未実行 gold を PASS と数えない。

## SHA-256 evidence

```text
c438ee4a714abc862d6a022df1d21bade07cf48098ad6d37d305006eab10be1e  runs/f11a-root-images-001/unittest.log
984c182b0e3eaa8ea70c54e118c28050ee1d9a12f44a6d471f27d4dfd411ca13  runs/f11a-root-images-001/result.json
892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508  tests/test_local_embedded_visual_pipeline.py
5a3c443a76f02b198367c036017967a33a75f22ec0826091ab5c8b54a988baa4  scripts/probe_intermediate_records.py
08d768bcc772c8d16c82d973d7cb8b212a1ee4bb1d8469bec5063e3707e0167b  scripts/build_search_units.py
34b949755780e5699e549320730127af486f232c09dae724275ae36a855121f7  scripts/validate_search_units.py
cfdbfaedaf7557cb5f09c16f6be4020a32cd4fd6ea8d918858dab93d1a57173a  scripts/validate_search_units_streaming.py
73b9ad531d4e184b47636f0a7558e07cc44688d1499ddd666b8dc58610f830f7  runs/f11a-executor-before-probe.v1.py
a14ffa6f5c4b04fd63ed3b95ba8b6892ca5c11f1cee050b323861c009c6eaaa1  runs/f11a-executor-before-search-builder.v1.py
bb5a7cd8b1e820e946e8f81b0763cea4ae93801241536d55813ddf81be8f8ce7  runs/f11a-root-image-run.v1.py
```

`runs/` は `design/local-memory-v1-hardening/runs/`。current Probe/Search hashes は executor manifest v1 の値とも一致した。診断の範囲はこの IMAGE regression の原因と remedy 提案だけである。
