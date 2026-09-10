# Password no-guess boundary — static review v1

2026-09-09。根拠契約 `safety-release-scope.v1.md`。対象は root による Probe の二関数変更とその直接 call sites、既存 AST test と保存 run のみ。使用 skill: codex-graph-engineering-adapter / graph-engineering-agentic-audit。same-model の手続的 reviewer 分離であり、正式 full acceptance／安全策全 8 項目の完了ではない。

今回の書込みは新規本書のみ。製品・test・既存記録の編集、test／product import・実行、モデル・server・実資料・実 CONFIG の利用はしていない。commit/push／アプリ更新をこの reviewer は実施しない。旧 F11a packet は不変保存し、許可された Probe 変更後の source に旧結果をそのまま適用しない。既存 repair count は維持する。

## 結論

**今回の自動候補探索・Office 一括キー試行の禁止差分に、追加の静的 blocker は見つからない。** 非 ZIP をパスワード保護と断定せず、読取不能／要確認の固定メッセージに止めている。変更関数に秘密候補を出力・保存・外部送信する経路はない。

ただし「ユーザーがファイルごとに明示入力したパスワードを、そのファイルだけに使う」UI／秘密配送 API は未実装。現在は自動推測を止め、非 ZIP の Office 読取を保留する狭い安全化である。AST 3 PASS だけで native/fallback 全経路、managed failure diagnostics、配布 bundle、全安全要件を受理しない。下記の小型回帰と現 source packet 固定が package 前に必要。

## exact delta と直接呼出し

`runs/f11a-image-repair-after-probe.v1.py` と current `scripts/probe_intermediate_records.py` の read-only `diff -u` は **二 hunk のみ**。HEAD との差分には既存 F11a／他の履歴があるため、それを今回の password 変更と混同しない。

| 境界 | current source evidence | 判定 |
| --- | --- | --- |
| 候補探索 | Probe `2465–2472` の `discover_password_candidates(root)` は引数に触れず `()` を返す。旧 root.rglob、alias/date 抽出、候補直積生成を除去。 | この関数による全 root traversal と推測候補生成を止める。関数の呼出し前後にある通常の root.resolve／明示 input inventory／source hash 読取まで「なくなった」とは主張しない。 |
| Office source | `3896–3905` は `zipfile.is_zipfile(path)` が真なら従来通り `(str(path), False)`、それ以外は固定 ValueError。旧 msoffcrypto import、OfficeFile/load_key/decrypt、全候補 loop を除去。 | 普通の ZIP path/return shape は維持。非 ZIP を暗号化・旧形式・破損の「可能性」と記載し、どれかを断定しない。 |
| managed 呼出し | `scripts/build_intermediate_records.py:1249,1273–1279` は空候補を `process_file` に渡し、同 `1088–1098` が Probe を作る。 | 既存 signature の互換性を残しながら標準 producer の candidate tuple は空となる。 |
| diagnostic 呼出し | Probe `7391–7395` は既存 discovery を呼ぶが今は空。`7374–7384` に個別 password 引数はない。 | 診断 CLI に自動候補再導入はない。明示入力機能を備えたとの主張は不可。 |
| 外部から候補を渡された場合 | Probe `3090,3106` は `password_candidates` 引数／instance attribute を保持するが、現 office_source は読まない。 | 互換 attribute は秘密配送 API ではない。標準経路外で秘密を渡せば Python object に残るため、メモリ消去／全 system 無保持の保証はしない。 |

直接の Office call sites は DOCX native `3918`／fallback `4070`、XLSX native `4384`／fallback `4566`、PPTX native `4857`／fallback `5244`。すべて `office_source` の直後に `validate_ooxml_archive` を呼ぶ。native import 不足による fallback (`3908–3916,4377–4382,4850–4855`) も同じ入口を通る。今回は archive validation／parser 呼出し順序の変更はない。

`zipfile.is_zipfile` が真であるだけでは plain・安全・OOXML と確定しない。既存 `validate_ooxml_archive:506–549` が required parts や encrypted-member flag (`547`) などを検査する。暗号化 ZIP member を「通常 ZIP」として parser へ無検証に渡す変更ではない。元からの is_zipfile→後続open の間の filesystem race をこの差分で解消したとの主張もしない。

Probe の main dispatch `3763–3798` は `.docx/.xlsx/.pptx` を対象とする。拡張子が真の `.doc/.xls/.ppt` の旧形式は今回の office_source を通らず `extract_other:7345–7356` の metadata-only/deferred である。非 ZIP `.docx` 等の説明に「旧形式の可能性」を含めることと、全旧 Office reader 対応は別。

## 秘密・診断・互換性

新しい ValueError は固定文で、path、候補、読み取った bytes を interpolation しない。候補配列の iteration、キー試行、成功キーの log／平文ファイル出力は除去されている。`scripts/` と app/engine で password_candidates／load_key／decrypt／discovery の参照を検索した範囲に、別の batch-key consumer は見当たらなかった。これは依存 package や全プロセスの秘密情報監査ではない。

managed `process_file:1101–1137` は例外を捕捉し、未commit Evidence/Relation を捨て、`Probe.record_failure:3203–3210` で failed Document を保存し、source hash を再照合して commit する。新しい error string 自体は秘密を含まない。失敗レコードの source path/hash 等の通常 provenance は残る。

ここで status が専用 `needs_human_review` に変わるわけではない。現 actual status は `failed`、error に `office_source_requires_human_review` が入る。通常 batch は `complete_with_failures` と failed_documents を出す (`build_intermediate_records.py:1311–1339`)。`--fail-fast` は失敗記録後に例外を投げる (`1282–1287`)。Probe diagnostic CLI は `7400` の extract 例外がそのまま終了し、後続 input や最終 write へ進まない。したがって例外文だけを見て UI が適切な一件単位保留を既に実装したとは判断しない。

各 reader に残る `if decrypted: ... decrypted in memory` warning は current office_source の実経路では到達しない。既存 `test_layer1_pipeline.py:696–712,737–753` は office_source を `(BytesIO, True)` に mock して archive gate を検証するため、その control は password 復号成功の証拠ではないが安全ゲート回帰としては維持できる。テストを削除する提案ではない。

`msoffcrypto-tool` の dependency metadata 登録 (`build_intermediate_records.py:80–86`) は残るが、これは当該キー試行の実行ではない。不要依存の除去や build script 変更は今回の reviewer 範囲外。Probe は processing code fingerprint 対象 (`64–79,727–737`) なので、変更前 shard の reuse や旧 registration の current 判定は新 hash で検証し直す必要がある (`1032–1072`)。旧 F11a 成果物を現在も PASS と宣伝しない。

## 保存済み test の評価

`tests/test_no_password_guessing.py` 全文読了。actual source から対象 FunctionDef の AST を取り出し compile/exec する構成で、OCR／model 依存を含む full Probe module は import しない。この reviewer はそれを実行していない。

`password-no-guess-001` の実 log/result は 3 methods ok、skip 0、expected failures 0、status passed、exit 0、elapsed 0.144189 s、log 440 bytes。log hash は result と一致する。

- discovery は mock root への全 calls が空であることを検査し、rglob 禁止を実関数で確認。
- non-ZIP は batch sentinel を渡し、instance の calls が空、ValueError に `requires_human_review` があり sentinel が含まれないことを確認。
- ZIP は `(original path string, False)` を確認。

限界：is_zipfile は mock、actual ZIP／native parser／stdlib fallback／managed output／UI は未実行。non-ZIP の exact error 全文やゼロ Evidence／他ファイル継続／stdout・stderr・state を通した secret 非出力もこの三つだけでは確認していない。小型 test として有効だが全経路の代替ではない。

## package 前の最小 regression 提案 — 未実行

| 順 | 目的と小型 fixture | 固定期待 |
| --- | --- | --- |
| 1 | tiny 正常 OOXML 三種を actual Probe native と明示 stdlib fallback の各 route へ。必須 parts を持つ人工 ZIP ≤16 KiB、画像なし。 | 元の本文／セル・formula・saved value／slide gold と source hash を保持。`decrypted` warning なし、msoffcrypto import/load_key/decrypt 呼出し 0。fallback の既存 partial 表示は成功に潰さない。 |
| 2 | 非 ZIP bytes を `.docx/.xlsx/.pptx` として六 route / managed process_file に渡す。候補に人工 sentinel を明示注入。 | exact fixed reason、parser／key試行 0、failed Document 一件・Evidence/Relation 0、source bytes 不変、秘密が exception/stdout/stderr/保存state/log に現れない。空／破損／擬似旧形式を確定的な「パスワードが違う」にしない。 |
| 3 | tiny 有効 ZIP だが required part 欠落／encrypted flag／unsafe XML の対照。 | no-guess が既存 archive gate を迂回しない。actual parser 前に既存 reason で拒否。既存 native gate 三件は利用候補だが、full test side effects／skip／fixture上限を root が先に再確認。1 MB bomb fixture を今回の小型条件へ無断投入しない。 |
| 4 | 正常一件＋読めない一件の managed batch、fail-fast、diagnostic CLI を別 entry で検査。 | batch は正常一件を保持し失敗件数・理由を正しく提示、失敗本文を後段索引／answerへ入れない。CLI の実終了値・未完了と per-file 状態を混同しない。package UI で「読めませんでした／要確認」が利用者へ届くこと。 |
| 5 | changed Probe fingerprint＋旧 generation／新 bundle。 | 旧 bytes と設定を保存し、新 code を使う extraction／Reader registration／index gate を再確認。旧 PASS／古い decrypt済み shard を無検証で current としない。配布 app の実 shipped source hash を照合。 |

今後 per-file input を実装するときは、入力した一件・source identity/hash・明示操作に限定し、他文書への使い回し、argv／URL／環境変数／CONFIG／ログ／平文永続化を避ける契約と人工 sentinel 回帰が別途必要。cancel／失敗時も候補推測へ戻らない。安全な配送・保持期間を未実装のまま全項目完了にしない。

上記は実行許可済み runner ではない。root が fixtures／依存 import／guard を全文 review し、owned tmp、30 s／1 MiB log、モデル・network・実資料なしの新 run ID で実施して初めて実績になる。package・release・commit/push の最終承認を本書は代行しない。

## SHA-256 references

workspace root `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。`runs/` は `design/local-memory-v1-hardening/runs/`。以下は今回の read-only 実 hash。

| source | SHA-256 |
| --- | --- |
| `runs/safety-release-scope.v1.md` | `85ac5cf2fcb67225f03b2daa1c9ca47a1a0e2918782aa2149d39ef101b7d961e` |
| `scripts/probe_intermediate_records.py` | `cfe33b6aaf7fd66a366421133828efaa492358a01df2d444cc0a38234415a164` |
| `runs/f11a-image-repair-after-probe.v1.py` | `1320cda390ab671dc2df82678ed2237e12aa4828b29666e3fb3d5b8a734f456d` |
| `scripts/build_intermediate_records.py` | `8d9dd31a7acba7d6b8f3e5a841b86565ea3e6e85d932fa08f77e3f3641ae59dd` |
| `tests/test_no_password_guessing.py` | `3c1fdf3db19a36f297724f24807e20d50da6f6aa903f55be0d4154289ece0240` |
| `runs/password-no-guess-001/result.json` | `f94b3df08482c9c98003944ec1e7ac65087ae5e7f0571e97253a09b2c5238bb0` |
| `runs/password-no-guess-001/unittest.log` | `5140ee4b09ed780e6881ea332189dadefc5021a7e966fbc07324766fcca392fd` |
| `tests/test_layer1_pipeline.py` | `63619ca448d88352beca3ee8e4679b25f762b3a12298398603c33f03f9f7255e` |
| `tests/test_local_embedded_visual_pipeline.py` | `892658aa919cad1dd35a7fdc19aba9c8b0ef1b931e83f3137fc6112b5a150508` |
