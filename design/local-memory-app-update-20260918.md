# APP-UPDATE-20260918

状態：完了（アプリ更新・起動確認のみ）。準拠：LMS-CONTROLLED-PLAN 1.2と本ユーザー承認。

## 承認と範囲

ユーザーは現在のアプリのバックアップ付き上書き更新・起動確認に「お願いします」と承認し、「バージョンは日付を入れて管理」と指定した。

- 現在のリポジトリのアプリを2026.09.18版（ビルド20260918.1）として生成する。同日の追加更新は末尾番号を増やす。
- 対象：build/build_package.shの版設定、本記録、生成パッケージ、/Applications/Local Memory Search.appとそのバックアップ。
- 現在のコードを配布する作業であり、検索順・グラフ処理・回答仕様は変更しない。構想との完全一致や回答精度の合格を意味しない。
- 原資料・本番索引・CONFIGは変更しない。再取り込み・モデル取得・commit・pushは行わない。起動に伴う通常のログ・稼働状態記録は発生する。
- 合格条件：旧アプリの保存、生成物の署名検証、配置内容の一致、更新アプリからのサーバー起動と画面表示。
- 戻し方：起動中処理がないことを確認してアプリを停止し、保存した旧アプリを同じ配置先へ戻す。資料・索引・設定は上書きしない。

## 結果

- 配置先：`/Applications/Local Memory Search.app`。版2026.09.18、ビルド20260918.1をInfo.plistで確認。
- 旧アプリ保存先：リポジトリ内`.tmp/app-update-20260918-1/Local Memory Search-before.app`（変更前にdiff一致確認）。元の実体も同ディレクトリの`original-installed.app`に保持。以前のパッケージ作業領域も`previous-package-stage`に保持。
- 配布物：`deliverables/Local-Memory-Search-v2026.09.18-macOS-unsigned.dmg`、同名ZIPとSHA256ファイル。DMG検証・ZIP検査成功。最初のサンドボックス内DMG作成は失敗し、権限付き再実行で成功。
- 生成アプリと配置アプリのdiff一致、codesign --verify --deep --strict成功。署名はアドホックであり、Apple公証を意味しない。
- openによる起動後、配置先のlocal_memory_server.pyを実行するPID 52677とhealthのstartup_state=readyを確認。build_id=`a42fc18468d9daa7240c1921ce70654a2c6ede8a0560f23ad8c750817f80c779`。
- ブラウザで`http://127.0.0.1:8765/`の質問入力欄・「知りたいことを相談する」ボタン・Ollama起動中を確認。検索対象はオリィ研究所。既存の「意味グラフ回答：validated_shadow_required（従来経路）」と部分読取107件は残存。再構築ボタンは押していない。
- CONFIGのSHA256：`197a23a7fc56cc3e28380763b27500bc17abf4c1c47b592017ec7c3eeb7f212d`。既存safe-answer-index.sqlite3：`798eb1e4cb6124e6de776d45a57322489f5dc8acda47b1221d000e8f928c909b`。更新前後で一致。
- 回答生成・回答精度・グラフ検索の合否は今回未検証。起動成功をRAGの合格として扱わない。commit・pushなし。
