# V1-LOCAL-READER-01：ローカルPDF・PowerPoint読取の復旧

- 状態：局所修正・限定検証・試用app更新は完了（2026-09-23）。全フォルダ再取込・質問回答の再評価は未実施。親計画：LMS-CONTROLLED-PLAN v1.8。保護条項は変更しない。
- 承認根拠：アプリ側でGoogleスライドを読めるようにする直接依頼に対し、「1：PCに保存したPDF・PowerPoint版を読む」「2：URLから取得する」を確認し、ユーザーが「１」を選択。既存のローカル読取不具合を直す範囲とする。
- 目的：既存のPDFページ画像・PowerPoint埋込画像のGemma読取を復旧し、返答検証の失敗と実際の処理停止を区別する。
- 編集対象：`scripts/local_visual_observation.py`、`tests/test_local_visual_observation.py`、本記録。追加の診断生成物は`.tmp/`に限定。検証後、現在の別名試用appの同梱readerだけを退避して更新する。
- 変更しない：通常版、原資料、現在の索引・CONFIG、検索順、グラフ設計、品質の格上げ判定、モデル・依存ライブラリ取得、クラウド接続。Google URL取得は実装しない。commit/pushは行わない。
- 合格条件：実際のローカルOllama返答との互換性を確認する。完了確認済み返答の内容検証失敗だけを当該画像に限定する。未完了・通信断・timeout・不正worker・未回収processは引き続き停止する。既存テストを削除せず回帰試験する。実PDFページとPPTX画像の既存読取経路を限定検証し、未検証範囲を明記する。
- 上限：既存の1画像180秒、応答サイズ、入力サイズ、モデル固定を維持。自動再試行を追加しない。初回は実データの1ページと1埋込画像の限定試験。全フォルダの再取込は行わない。
- 戻し方：本作業差分のみ逆適用。試用appは更新前readerと署名を検証可能な形で退避し、通常版・索引を復旧手段として上書きしない。

## 調査の確認事項

- 最初の失敗は`loopback Ollama chat response contains an error or unknown field`。この記録だけでは未知フィールドとモデルエラーのどちらか断定できない。
- 以降の画像は同一取込process内の停止ラッチで読取されなかった。最初の失敗自体をhard timeoutと断定しない。
- 通信切断後はworker終了済みでもOllamaが計算中の可能性がある。単にexit 2だから次画像へ進む修正にはしない。
- この復旧は汎用的な意味グラフ生成・V1全体完成の保証ではない。

## 結果

1. 実Ollama 0.34.2は`done=true`、`done_reason=stop`、`error`なしで返答していたが、`prompt_eval_cached_count`が未許可フィールドだった。これは[公式APIのキャッシュ入力件数](https://github.com/ollama/ollama/blob/main/docs/api/usage.mdx)。この項目だけ非負整数に限定して対応。未知項目や不正な本文は採用しない。
2. 二次的に、人物のdescriptionが既存の許容値`person/人/人物`を外れて拒否された。検証を変更せず、プロンプトにdescriptionを`person`だけにする既存条件を明記。
3. 完了確認済み応答の検証失敗を専用エラーに分離。正常なworker通知・終了コード2・回収確認・期限内のときだけ後続画像を継続。失敗した画像は不採用のまま。通信断・timeout・未回収・通知不正・モデル不一致は従来どおり停止する。
4. 実PDFの1ページと実PPTXの1埋込画像をローカルGemmaで限定確認。PPTXはスライド7・内部画像部品と結びつく暫定Evidenceまで保持。原資料・現在の索引へ結果を混入しない。
5. 視覚照合で文字誤読も確認した（例：画像の「乾杯」を「賛成」と出力）。形式検査通過は正確な転記や正しい回答の保証ではない。暫定扱い・既存品質検査は維持。グラフ回答や全質問の合格とはしない。

### 回帰確認

- `test_local_visual_observation.py`：32 PASS（既存テストの変更・削除なし、9メソッド追加）。
- `test_local_*visual_pipeline.py`：26 PASS（PDF・Office出典接続）。
- `test_*local_image_ocr*.py`：43 PASS、既存CC0画像fixture未指定の1件SKIP。
- `test_generic_structured_reader_hardening.py`：17 PASS。
- `test_trial_bundle.py`：16 PASS。
- 合計134 PASS、1 SKIP。`git diff --check` PASS。
- 最初のシステムPythonにはpptx/openpyxl、Codex付属Pythonにはjsonschemaがないため一部テストが実行できなかった。前者は既存Codexランタイム、後者は既存`.venv-paddleocr`で再実行してPASS。追加インストールなし。試用appの依存制限を解消したという意味ではない。

### 試用appへの反映

- 対象：`deliverables/Local-Memory-Search-Trial-20260922-y07GpM/Local Memory Search 試用版 2026-09-22.app`。同梱`engine/layer1/scripts/local_visual_observation.py`のみコピーし、既存方式のad-hoc再署名と厳格な署名検証を実施。
- 退避：`.tmp/local-reader-20260923/trial-before-reader-fix.zip`（展開前のzipとして保存し、旧appのLaunchServices再登録はしない）。
- 読取モジュール：0.3.1、SHA-256 `e100f7622e6743c1faa7b078a5e00d46e793598c224702ab8d3c8dc8ac30dd03`。repoとbundle一致。
- 再起動後health：build `05ad2c4b68add4cb571a5c26e95593c3dae6593d049529904abe594b8307c6cf`、startup_state `ready`。
- UI確認：`http://127.0.0.1:8766/`で質問欄・フォルダ選択欄を表示。`reader_migration_required`と「Step 7 Reader索引を再構築」が表示される。再構築ボタンは押していない。旧失敗の表示は現在の索引の履歴であり、今回の限定検証結果ではない。
- 試用CONFIG・既存safe-answer-index・通常版readerは更新前後のSHA-256一致。通常版8765、原資料、モデルを変更していない。commit/pushなし。

## 次の境界

選択済みフォルダの再取込は本カードでは実行していない。ユーザーの確認後、試用appの再構築導線から実施し、読取数・停止箇所・実質問の回答を改めて評価する。今回の原文抽出の修正を、未承認の汎用意味グラフ再設計の許可にしない。

## V1-LOCAL-READER-REBUILD-01：再読込みの承認（2026-09-23）

- 承認根拠：「いま選択中の『■川崎Le_Furutier』フォルダを、修正版で再読み込みしてよいですか？」に対するユーザーの「おｋ」。上記の未実施記録は、この承認前の履歴として保持する。
- 対象：`/Users/takashifukutomi/Desktop/オリィ研究所/■川崎Le_Furutier`。試用版8766の既存Reader再構築UIから1回実行する。
- 変更範囲：試用profile内で通常の再構築が生成・更新する索引、状態、ログと本記録。別のフォルダは選ばない。通常版・原資料・モデル・検索設計・検証基準・処理上限は変更しない。commit/pushなし。
- 確認条件：実際の開始と終了状態、処理件数、Gemma視覚読取の結果、失敗箇所を確認する。暫定Evidence生成と正しい回答の完成を区別する。再構築が正常に使用可能となった場合は、既出の住所／来店手順の質問で必要な確認を行う。
- 失敗時：自動再試行や閾値緩和を行わず、原因と残件を報告する。旧世代・通常版を無断で上書きして復旧しない。
- 状態：2026-09-23 01:39:32 JSTに試用appの再構築UIから開始。`generation-1a0dc459207c45b3b2ada527821f9958`、phase=`building`。対象フォルダ表示と版検証PASSを確認し、本文・画像抽出へ進行中。完了は未確認。

### 実行途中の確認（01:53 JST）

- 14ファイル中2ファイルの処理が確定。最初のExcelは既存代替readerで73件、最初のPDFは507件のEvidence。
- PDF `20250801ランチタイムLe・Fruitier プレゼンタースライド20260701.pdf` は12ページ分の`local_vlm_visual_observation_provisional`を保存。前回はこの経路が0件だった。文書errorsは空、unknown-field／一括停止の警告なし。
- 画像由来結果は引き続き暫定扱いであり、正確な転記・質問回答・汎用意味グラフ完成の合格とはしない。
- 最初のPDFの処理確定まで約13分。全体の完了・新索引公開・実質問の再評価はまだ確認していない。再開時はこのgenerationの状態を確認し、同じ再構築を重複開始しない。
