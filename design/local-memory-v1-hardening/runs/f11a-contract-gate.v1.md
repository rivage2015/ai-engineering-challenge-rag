# F11a contract gate v1 — static design review

Task: `lms-v1-f11a-notebook-metadata-2026-09-09`。2026-09-09、担当 `/root/f03a_independent_audit`。

**判断: 契約＋addendum の範囲なら、次の gold / before / runner / semantic RED 準備へ進められる。C1–C8 は契約上閉じた。追加の設計 blocker は見つからなかった。** 製品 PASS、正式 Agentic Audit の pass、test/runner の実行承認、製品編集許可ではない。製品編集には契約どおり別の Root RED gate が必要。

Codex Graph Engineering Adapter / Graph Engineering Agentic Audit を使用。`same_model_separate_context` は手続き上の分離のみ。契約 92 行と addendum 全文、前 review を照合し、Git の既存 dirty 状態を読取確認した。製品/test の import・実行・編集、新 agent、原本・network・model・GUI・install・commit は行っていない。新規作成はこの gate 文書のみ。製品実装の正しさや Root が準備中の test/runner 自体は今回の確認対象ではない。

## C1–C8 の closure

行番号は `f11a-task-contract.v1.md`。closed は規範の未決事項が解消された意味であり、実装済みという意味ではない。

| 項目 | 判定 | 固定された条件 / 根拠 |
|---|---|---|
| C1: exact report / 旧 API | closed | L41–64: native/stream の署名、共通 report の exact keys、scope 非保証、件数、sorted unique reasons、PASS のみ旧 counts、UNVERIFIED は明示例外。既知の矛盾は report に隠さない（L27,35）。 |
| C2: not_applicable / 0 checked | closed | L33,37,62: `.ipynb` Document がない場合のみ not_applicable。Notebook Document の 0 checked は genuine empty でも UNVERIFIED。非 0 件は membership 完全性を証明しない。検査済み/未検査件数の定義も固定。 |
| C3: applicability / role 偽装 | closed | L15–19,33–35: path suffix と extension、canonical role/locator/ordinal/pointer、state の閉じた型を合わせて検査。parser/version/state 欠落で opt-out 不可。unsupported textual substitution を黙って除外せず、画像/OCR 等の別 scope と区別する。 |
| C4: same-read source authority | closed | L23–29,33: cap+1 の単一 bounded snapshot、同じ bytes の hash/length/parse、hash 後 reopen 禁止、path/mtime cache 禁止、一 Notebook の parsed object だけを保持。原本欠損/hash/size 矛盾と意図的 root 不在を区別。全競合/OS atomicity を保証したとはしない。 |
| C5: incomplete / invalid / missing | closed after addendum | L17,27,35,62 と addendum §1: missing code/execute-result count は UNVERIFIED、invalid type/negative は failure。partial/failed は `notebook_extraction_incomplete`、必要なら unparsed も併記。照合済み metadata を偽って unparsed としない。budget 内で分かる malformed state は優先して failure。 |
| C6: CLI / caller gate | closed | L64,66: PASS=0、UNVERIFIED=2、ValueError は bounded error 付き FAIL=1、stream schema label は CLI-only。既存 adaptive caller が非 0 で downstream 前に止まる control を要求。Python programming exception を成功へ変換しない。 |
| C7: Search 境界 | closed | L9,66: Evidence との exact state 一致のみ。same-ID context 変更/欠落/無関係 injection の拒否、既存返却形保持。原本検証には同じ immutable intermediate generation の別検証が必要。本文/whole-unit membership を保証しないことが明示された。 |
| C8: resource policy | closed as a design limit | L23–29: 8 MiB/read+1、preparse token 100000、depth 64、number token 256、escape-aware scanner、token list を作らず conversion 前に制限。限界超は resource reason/UNVERIFIED と partial raw fallback。malformed JSON と区別し、RSS/全 job 性能保証にはしない。 |

## この gate 中に解消した 2 点

1. 元契約は partial/failed を UNVERIFIED としたが、その専用 reason がなく、metadata は全て照合できる部分失敗を正確に表現できなかった。Root は元契約を保存したまま addendum §1 に `notebook_extraction_incomplete` を追加した。視覚抽出だけの不完了を text state 未解析と誤記しない条件も明示された。
2. 元 runner 文言の `actual import` は Python 製品 module import まで禁じると semantic RED と矛盾する。addendum §2 は実資料の index 取り込み禁止と明確化し、review 済み製品 module を frozen synthetic guard 内で使えることを明示した。今回の監査者には引き続き test 実行権限はない。

これは実装前の契約 clarification であり、製品 formal repair 回数には数えない。旧契約/前 review は変更していない。

## 次の gate で検査する点（新規 scope ではない）

- L88 の G1–G6 literal gold、before bytes/hash、permanent test AST、runner side effects を Root が確認する。API/import/TypeError の失敗を semantic RED としない。今回の静的 closure を、その確認の代わりにしない。
- C2 の 0 checked は各 Notebook Document の条件として確認し、混合入力の合計 checked が非 0 でも未検査 Document を隠さない。body/membership omission witness は成功した防御とは別計数する。
- resource gold は各 cap の境界、escaped quote/backslash、文字列内の構造文字、number conversion 前の拒否を確認する。小さな注入 limit を使い、8 MiB fixture を実際に作らない。schema on/off で意味検査を落とさない。
- partial/failed 専用 reason、rootless の既知 mismatch 優先、same-read 差替え、同 ID Search context、CLI の actual caller 停止、旧 generation byte-map 不変、既存 image/long-text 挙動の維持を test/正式監査で確認する。checkout parity を、未同梱の streaming Search CLI の配布証明にしない。

## 残存・非保証

metadata-only なので、宣言位置の本文との一致、完全 membership、freshness/実行履歴、画像/OCR/error/display state、F11b の app/shard/index/retrieval/回答伝播、全 V1 / 全 F11、production RSS/performance は未完了または未保証のまま。8 MiB 等は保守的な設計上限であり実測性能の保証ではない。形式仕様の Web 調査や公式 nbformat 適合確認は行っていない。

## 固定入力 SHA-256

```text
20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15  f11a-task-contract.v1.md
2542f29b2776127a5d919639c69505d31b4efdf46968d9ff80a0af7d930f6f8f  f11a-task-contract-addendum.v1.md
7640c4ca4ad2aa770d061701e2dc26924761a422eaecf7d50bd86b1a98f1fa3d  f11a-contract-review.v1.md
```

いずれも `design/local-memory-v1-hardening/runs/` 内。実行結果・製品 source freeze・formal artifact の受領は別タスク。この設計 gate は限定契約の準備を進める判断だけを記録する。
