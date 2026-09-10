# F11a independent static contract review v1

2026-09-09。担当 `/root/f03a_independent_audit`。**契約準備の静的レビュー完了。製品・正式監査の PASS ではない。実装許可も与えない。**

Adapter / Agentic Audit を使用。別コンテキストによる手続き上の分離であり、独立した実証の追加ではない。対象コード・指定 preflight・観測 receipt を読み、hash を照合した。製品/test の import・実行、新 agent、原本・モデル・network・GUI・install・commit は行っていない。作成したのはこの新規文書だけ。将来の訂正は旧版を保存して追記型にする。

## 結論と scope の選択

Root の最新案 **`notebook_metadata_binding` に限定する案は、以下の条件付きで採用可能**。これは本文結合より弱い契約であり、未実装の現製品を受理する判断ではない。

- PASS が意味するのは既存の構造/整合検査と、**実際に検査した textual Notebook Evidence の metadata が、同じ hash-bound 原本 bytes の宣言位置の facts と一致したこと**まで。
- `Evidence.raw_text` が宣言位置の source/output 本文であること、全 Evidence / SearchUnit の membership・抽出完全性、保存 output の freshness、実行履歴、回答での伝播は保証しない。これらの非保証と検査件数を機械可読にし、単なる `source_attested=true` や「原本と一致した保存出力」へ言い換えない。
- SearchUnit は参照 Evidence との state 一致だけを検証する。Search の単独成功を原本 attestation と呼ばない。Evidence と context の両方を同時に偽造した場合、Search-only 一致検査が成功し得ることを明示する。
- `f11a-fresh-preflight.v1.md` §§1,5 は raw 本文/data URI 変換まで再構成する強い案を推奨している。Root 案はそれを採らない **明示的な scope 選択**。本文結合および F11b の質問 shard / semantic projection / index / retrieval / 回答表示は未完了条件として残す。

metadata だけ正しいまま raw 本文を差し替え、ID/hash を再計算するケースや、whole-record 削除を今回の「拒否必須」とするのは、この限定案を越える。ただし既知の非保証 witness として残し、受入成功件数と混ぜない。pointer/role/location と metadata を正しく別の source 位置へ移した本文が通る余地も、本文結合の未完了そのものである。

## 実装前に固定すべき exact API / 必須境界

| ID | 必須の確定事項 / 最小の要求 |
|---|---|
| C1 | `validate_report(directory, source_root=None)` と streaming の `*, published_schema=True` を維持する追加 API 案でよい。共通 report の exact keys・enum・reason codes・件数の分母を固定する。旧 `validate` は PASS のときだけ従来の `document/evidence/relation` counts を返し、それ以外は明示例外。UNVERIFIED を返す前にも構造/既知の矛盾を検査し、壊れた record を root 不在で隠さない。 |
| C2 | `not_applicable + PASS` は対象 Document に `.ipynb` がない場合。producer の parser/version/state/locator 欠落を「Notebook なし」としない。Notebook Document があるが textual 検査件数 0 の場合を固定する。**提案:** `UNVERIFIED/no_textual_records_checked` とし、原本から genuine zero を証明する別規則を採るなら明記する。whole-record 完全性を保証しない以上、0 件で Notebook 全体が検証済みという表示は不可。 |
| C3 | `.ipynb` applicability は正規 source relative path と caller の root に結合し、extension 矛盾を検査する。canonical source/output role・locator・ordinal・pointer と state を一緒に検査する。parser 名を `bounded-text-stream` 等へ変更、state/indices 除去、Evidence type を別の textual type へ変更して検査を省略できないこと。対象不明の Notebook textual record は拒否または UNVERIFIED とし、PASS のまま黙って除外しない。画像/OCR 等の既存の scope 外を一律に Notebook text と誤認しない。 |
| C4 | source digest/length と metadata parse は同じ bounded read snapshot から計算する。native validator の既読 bytes を再利用し、streaming の hash 後 reopen、Probe の parse 後別 read hash、path/mtime を鍵にした持続 cache を許さない。原本 hash 不一致・原本欠損は既知の矛盾として例外、意図的 root 不在は UNVERIFIED と区別する。mtime やあらゆる競合/ancestor race の完全保証は追加しない。 |
| C5 | Root 案の真正 partial/failed・caller root 不在・大サイズは UNVERIFIED/非 0 を固定する。metadata shape/値不一致は失敗。真正 partial と偽造 state の両方がある場合の優先順位も固定する。invalid count の failed/partial 選択、missing code / execute-result count の診断と結果はまだ未決。`false != 0`, missing != null は既提案の project 規則であり、公式 nbformat 適合認証とは呼ばない。 |
| C6 | CLIs は PASS のみ 0、UNVERIFIED は非 0。具体 exit 値、FAIL の JSON/例外形式、stream の schema label を固定する。`build_adaptive_semantic_graph.py:420–433` は CLI 成功後に固定 `status: pass` を保存するので、この gate が非 0 で停止することを確認する。これを F11b metadata 伝播完了と数えない。 |
| C7 | Search validator 2 系統は新 field の exact/type-aware 一致、欠落、wrong pointer/index、同 ID context 変更、無関係な Evidence への注入を検査する。`True == 1` となる素朴な Python 比較だけにしない。Search の既存 return shape は維持可能。全 unit 削除/omission 拒否は metadata-only とは別の membership 契約であり、今回は保証しない。 |
| C8 | **resource limit は未決のまま。** 64 MiB bytes と depth 64 は parsed JSON の heap/token/node amplification 上限ではない。実装前に byte/read・parse・retention の実際の bounded policy と上限超の扱いを固定する。上限前の無制限 read/parse を後段 count check で安全化したと称しない。64 MiB 超 raw fallback と未定義の画像 data URI 変換に parsed-state/body attestation を与えない。 |

共通 report には `notebook_metadata_binding` の status、対象 Document 数、**実際に照合した** textual Evidence 数、未照合件数/reasons を区別するのが最小の提案。本文結合・membership・freshness が `not_verified` であることを固定の scope 宣言として返す案を推奨する。exact key 名はまだ規範ではなく Root が凍結する事項。一般の PASS を「全原本内容を独立に照合済み」と解釈する consumer があるなら、その consumer 契約を解決するまでこの名称変更だけでは不十分。

Strict/duplicate/nonfinite/overflow/decode の曖昧な Notebook JSON は、metadata 原本照合の前提を満たさない。制御された例外か UNVERIFIED のどちらかを固定し、PASS にはしない。非 object root・cells/output の型等の追加 shape 制限は fresh preflight の提案であり、ここで公式仕様由来の新必須条件へ昇格しない。

## 最小必須 gold（fixture / method / expected result は実装前に固定）

| 系統 | 判別力のある最小ケースと結果 |
|---|---|
| G1: literal 正例と false positive | 既存 4-cell/6-text fixture の全 state を手書き object で固定。7→2 の文書順、output 6、equal/zero/null/missing の対照、markdown/raw/source-only、空 source＋output、複数 output。source `raw_text`/hash/ordinal/location と原本 bytes が変わらないこと。長文は現在の one-Evidence/one-direct-unit と no truncation、whitespace-only は unit がなくてもよい既存挙動を保持する（新 chunking の要求にしない）。非 Notebook text は旧 counts、Notebook 画像/OCR scope 外の誤検出対照も必要。 |
| G2: metadata と分類の偽装 | 正常 fixture を先に成功させ、state 一 field の変更/除去、pointer/role/location/ordinal の不一致、parser/extension/locator/type による対象外化、無関係な record の state 注入を拒否。必要な ID・record/shard/state hash を整合させ、外側 checksum 失敗だけを本検査成功にしない。shape 上正しい誤 count の literal も含める。 |
| G3: Evidence→Search 境界 | 同じ unit ID の context count/pointer 変更・field 除去を両 Search validator で拒否。Evidence/state 一致だけの検査と原本照合を区別する。raw 本文の整合的差替え、Evidence＋context の coherent forgery、whole-record 削除は宣言した限界の witness として別計数し、「本文/完全性の攻撃を防いだ」としない。 |
| G4: 同じ原本 bytes / parser | bounded read seam で A を hash 後に path を B へ差替え、A の digest に B の facts を結合した候補が通らないこと。各呼出しの別 snapshot を path cache が再利用しない対照。duplicate/nonfinite/overflow/decode/depth と、invalid count/missing count の採用済み結果。小さく注入した cap の内外対照で過大 fixture を作らず上限分岐を検査する。 |
| G5: 3 経路と public gate | native / stream schema on / stream schema off で同一結果。root 不在・真正 partial/failed・上限超・0 checked の採用済み結果を report、旧 counts wrapper、CLI で確認する。非 0 が実際の managed caller の後続 Search/adapter 開始前に止まる小型 stub control。壊れた state と root 不在の組合せを UNVERIFIED だけで隠さない。 |
| G6: 移行・配布・保存 | 新版直/managed identity と Search pins の正例、真正旧 Notebook state 無しの no-silent-upgrade、旧非 Notebook 互換、旧 generation byte-map 不変、新 unpublished generation だけの再生成。既存配布 copy list と fingerprint が採用 helper を含むことを静的/限定 packaging control で確認。既存 Observer の 2 green は gap 観測であり新契約 semantic RED ではない。 |

G1/G2/G4/G5 は 3 intermediate validator 呼出し、G3 は 2 Search validator の対象経路を明記する。gold は producer helper の出力から生成しない。失敗が予定した検査まで到達したことを確かめ、missing API/import/TypeError/fixture 不良を semantic RED や防御成功にしない。正式実行は別承認の frozen runner 下のみ。今回のレビューに新 test 結果はない。

## 配布・移行・守る既存資産

Probe `0.7.1` と managed extractor `0.11.0` は別 identity。Search builder `0.6.0` と 2 validator/adaptive pin、managed structural producer tuple の整合が必要。版の数値は fresh preflight の推奨であり本レビューで承認していない。

Fresh preflight の「既存 Probe 内の pure helper」案は既存同梱と fingerprint に収まり、保護された package script を編集せずに済む。新 module に変えるなら同梱/identity/ownership を再確認する。現 copy list に streaming Search validator はないため、checkout の stream parity を配布済み CLI の存在証明にしない。旧 structural tuples の保持は歴史的互換であり、旧 Notebook の metadata 保証への昇格ではない。原本・公開 generation・F05b 成果物・保護 dirty files は不変とする。

## 読取根拠と照合

必読 4 文書を全文読了、以下 SHA-256 を独立に照合した。

```text
a98164a5ccb16eb2ea5ec9bf3779ffe3b0ac962ec1c55d141574d4264f921d46  runs/f11-next-contract.v1.md
4574cd369a27578619691cdeb71025e5dce130db109ef2a9ea25fa4c09da33ea  runs/f11a-heartbeat-preflight.v1.md
b2071444f339ef3f1d1459f18e10ad132c514baececddaf1f06e872bf7f8bbe4  runs/f11a-fresh-observation-result.v1.json
c269fb2f91584b78130969fe0fbddc8d18e05e9bbe5c00a2bcde0ec5b420d056  runs/f11a-fresh-preflight.v1.md
```

`runs/` は `design/local-memory-v1-hardening/runs/`。過去 binding preflight も読了。fresh observation の 2 observations / 6 validator calls は既存 receipt の読取であり、監査者による新規再実行ではない。

直接読んだ主要箇所: Probe `:454, :2584, :2659, :3219, :6586–6780`、managed builder `:64, :733, :1076–1125`、intermediate native `:164–280, :428–580`、stream `:385–505, :600–650`、Search builder `:749–851`、Search validators `:820–980 / :375–635`、adaptive builder `:398–450`、adaptive validator identity pins、bootstrap processing-code list、package copy list `:41–76`。これらの source hash は fresh preflight §9 と一致（adaptive builder は追加で `5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a` を照合）。全 210 source の再監査は今回行っていない。

残る判断は C1–C8 の exact freeze、特に **metadata-only の機械可読な scope / 0 checked と部分失敗の結果 / resource policy**。Root が契約を固定し、別承認で semantic RED を保存してから実装へ進む。F11a/F11 全体の製品受理は未実施。
