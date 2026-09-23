# 固定JSONを既存アプリの回答処理へ接続する小テスト

作業ID: LMS-SNAPSHOT-APP-20260923。状態: CR-SNAPSHOT-CONTEXT-8192 比較完了。全文到達は改善、実用回答の合格は未達。

## 承認と目的

ユーザーの直接指示「このJSONをアプリに読ませてテストしてみましょう」を根拠に、保存済み `deliverables/local-memory-json-20260923/reading-snapshot.json` 1件を、分離した索引で既存アプリの検索・Gemma回答・最終監査へ接続する。本文・表先行R2bの接続工程として実施する。AGENTS.mdとcontrolled-execution-planの保護条項は維持。

原資料を再抽出しない。原本のhash/サイズと採用版の既存照合は保持し、「読取時点の固定データ」を「いつでも最新版」とは扱わない。原本不在でアプリ全体が使えることは今回の合格条件にしない。

## 今回の編集集合

- 本カード、`design/local-memory-text-first-r1-r2-20260923.md` の結果記録。
- 新規 `scripts/materialize_reading_snapshot.py`: snapshot専用producerとして検証済みJSONを展開。元Readerの状態を偽装せず、由来と全レコード一致を独立検査する。
- `scripts/adapt_layer1_to_local_memory.py`: snapshot専用の検証済み入口だけ許可し、未読情報・失敗情報を保持。通常のtext-first直接入力拒否は維持。
- `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py`、`validate_adaptive_semantic_graph.py`: snapshot入力の選択範囲・出典・検索unit・構造関係を既存検証へ接続。
- 新規 `distribution/macos-local-memory/engine/reading_snapshot_context.py` と既存 `build_local_semantic_index.py`、`answer_local_memory.py`、`answer_local_memory_v2.py`: 全対象のcoverageを索引へ結合し、検索に残らない資料の未読も保持。生成前に読取範囲を伝える。
- `distribution/macos-local-memory/app/final_answer_audit.py`、`local_memory_server.py`: 同じcoverageを既存監査と結果表示へ渡す。監査方式・検索順・モデルは変更しない。
- 新規 `scripts/test_local_reading_snapshot.py`、`tests/test_snapshot_reader_connection.py`、`tests/test_snapshot_answer_context.py`: 分離実行の再現手順と合成回帰。
- private生成物は `.tmp/local-memory-snapshot-app-20260923/` の新規実行ディレクトリのみ。正解要件/結果は索引入力と別に保存しGitへ入れない。

## 変更しないもの

原資料・固定JSON・通常/試用app・既存CONFIG/索引・Readerの既定動作・既存テスト期待値・モデル取得・外部送信・commit/pushは変更しない。既存未コミット差分を保持する。セキュリティ判定、版確認、引用検査、最終監査を迂回しない。別回答器へ置換しない。

## 合格条件・上限

1. snapshotと展開後の全Document/Evidence/Relationが一致し、対象一覧と原本hashが一致する。改変/対象混在/不完全snapshotを拒否。
2. 本文・表の原文/セル位置/関係を保持。未読14箇所だけでなく、選択範囲全体の失敗・警告を保持して索引/生成/最終監査/結果に結合する。画像未読を全質問への一律拒否にも完全読取扱いにもしない。
3. 既存の検索、原文束、グラフ補佐、Gemma、最終監査を使う。mock成功を実モデル成功にしない。
4. 索引準備は1回、まず既存のバリスタ稼働時間と案内の1問。その後は同じR1–R2の4問まで（この1問を含む）。修正後の追加比較は既定1巡まで。新しいモデル比較や全画像OCRを開始しない。
5. 準備/回答/監査の時間、回答内容と根拠到達、失敗を記録する。旧ログと版が違う場合に速度改善率を出さない。CLIのアプリ内部経路だけの成功はGUI受入完了と呼ばない。

実行は新規support/索引への限定で戻せるようにし、稼働中の8766は停止・更新しない。完了報告では内部接続、実Gemma回答、画面反映のどこまで確認できたかを分ける。

## 実施結果（2026-09-23）

- 固定JSONを専用importerとして展開し、全レコード・出典・採用版を独立検証。全inventoryの版検査は維持し、本文読取の対象はJSON manifestの1冊だけに限定した。隣接資料の追加読取なし。
- 元の1,314 Evidence／1,314 Relationを保持し、既存SearchUnit投影で検索用Evidenceは1,892件。既存security gate・lineage・safe index検証を使用。原本の再抽出、OCR/VLMは0回。
- 対象全体の読取範囲を索引と結合し、検索に残らない失敗文書や未読情報も保持する。生成・再監査・workflow独立再構成へ同じscopeを渡す。従来v1直接CLIはscope未対応なのでsnapshot索引を明示拒否し、アプリが使うv2のみ接続。
- private出力: `.tmp/local-memory-snapshot-app-20260923/run-01/`。最初にテストのsecurity保存先作成漏れで停止。正規validatorで既存成果物を再検証し、同じ未完世代の後半だけ再開した。再抽出・再projection・既存索引再作成なし。preparation_count=1、resume_count=1、失敗履歴を保持。
- 索引作成21.035秒。準備全体の処理時間28.548秒（失敗前＋再開後の稼働時間であり、コード修正中の待ち時間は含まない）。原資料/固定JSON hashは前後で不変。
- 質問1回、実モデル `gemma4:12b`、既存 `answer_query` → 項目監査 → 最終監査。ハーネス実測114.288秒。モックではない。結果は `answer_status=insufficient`、回答「わかりません」。**回答合格ではない。**
- 本体回答処理82.386秒（plan16.034／検索2.397／項目監査62.879）。一括監査は `supported_field_contract_invalid`、個別fallbackは `field_audit_item_mismatch`。最後の監査はqualifiedだが、これは質問に回答できたという意味ではない。
- 必要な曜日・時間のセル原文はJSON・安全索引に存在。同内容を含む行Evidenceもモデル入力候補へ含まれた。一方で値セル単体は選択されておらず、値セル引用を要求する既存契約への影響は未解決。
- 同一実行時刻のOllamaログで入力切り詰めを確認。06:04:30〜06:05:33、通常項目監査のcontext4096、一括4807→2051、個別4301→2051、4285→2051。後段の最終監査だけcontext8192でinput6977/output69、done=true/stop。前段の切り詰めと契約違反の唯一因果は未確定だが、全文未到達は確定。runnerのtruncated=0だけで前段切り詰めなしとは扱わない。
- optional cross-document補佐は `validated_storage_registration_missing` により非適格。5フラグを無効化せず、登録やKeychainを偽造せず、既存判定に任せた。今回の質問はgraph_route.used=false。全GraphRAGが動いたという証拠にはしない。
- main実行の関連106テストPASS（snapshot31、graph index20、workflow final audit12、text-first28、frozen JSON15）。別途既存監査fixture1件とlineage fixture4件はHEADインメモリ対照でも同じ失敗。既存期待値を変えず、全リポジトリ成功とは報告しない。
- 通常/試用app、既存CONFIG/索引、原資料、固定JSONを未変更。配布物作成、HTTP/GUI受入、commit/pushなし。

## 残件と次に確認する変更

1. まずsnapshotを扱う通常項目監査の入力枠を8192へ明示し、同じ索引・質問で限定1回再比較する案。監査基準、検索順、モデル、出典検査は維持。入力の脱落を避ける目的だが、メモリや所要時間が増える可能性がある。現在この容量変更と再試験は未実行・未承認。
2. 上記でも不合格なら、値セルが選択されず同じ行だけが届く接続と、監査JSONの実返答を分離して調べる。正解埋込、監査省略、カフェ固有ルールは入れない。
3. snapshotの全warning/errorは索引に保持しているが、モデル向けcompact scopeは件数を中心にし、画像以外の失敗理由・場所の完全提示は未完。汎用的な失敗文書の受入完了とはしない。
4. インストール版へ必要なsnapshot helper同梱、reader世代契約、初回取込み/再起動/画面の受入は別工程。今回のbackend専用CONFIGをapp-readyに偽装しない。

元に戻す方法: 今回追加したsnapshot入口/新規補助コードの差分だけをレビューして取り消す。既存ユーザー差分は破棄しない。未公開の分離世代しか使っていないため、使用中アプリを戻す操作は不要。

## CR-SNAPSHOT-CONTEXT-8192（2026-09-23、承認済み）

直前の提案「途中の監査にも8,192トークンを明示し、同じ質問を1回再テストしてよいですか？」に対するユーザーの「おｋ」を承認根拠とする。上記の未承認記録は当時の履歴として保持。

- 変更: snapshotを扱う通常の一括/個別項目監査にnum_ctx=8192を明示。従来のworkflow用16384は下げず、snapshotでない通常経路は変更しない。
- 編集対象: `engine/answer_local_memory_v2.py`（package配下）、`scripts/test_local_reading_snapshot.py`、`tests/test_snapshot_answer_context.py`、本カードとR1–R2記録、既存privateテスト出力。
- 同一JSON・分離索引・モデル・質問を再使用。再抽出/再索引は0回。質問の再比較は1回限定とし、比較理由を履歴へ保存。既存失敗記録や重複試行拒否は削除せず、明示比較だけを1回認める。
- 合格確認: 単体テストで一括/個別8192、workflow16384、従来経路不変を検査。実行時ログを対応付け、入力切り詰めの有無、回答と原文根拠の一致、時間を別々に判定する。容量修正だけで正答すると決めつけない。
- 今回しないこと: 検索/順位/値セル引用規則/監査基準の変更、モデル変更・取得、追加質問や自動再試行、本番/試用CONFIG/索引/app更新、commit/push。
- 戻し方: 今回の容量設定・比較専用フラグの差分だけを戻す。元索引/JSON/初回結果は不変なので再作成不要。

### CR-SNAPSHOT-CONTEXT-8192 結果

- `field_audit_context_options`を両項目監査へ接続。snapshot通常8192、workflow16384維持、snapshot以外の通常経路は無指定のまま。prompt/schema/temperature/生成上限/検索順/監査検証は変更なし。
- `--compare-context-8192`は同一質問の完了済みinsufficient結果に対し1回だけ明示指定可能。開始前に比較履歴を保存し、中断・失敗や壊れたmarkerでも回数は復活しない。既存の通常重複拒否は維持。
- 実モデル再比較は承認通り1回のみ。結果は `run-01/question-02.json`。原本/JSON/索引/分離CONFIGは前後hash一致。初回結果・出典の正解要件を変更していない。初回と質問・models・index dict・一括監査context SHAも一致。
- 今回のOllamaログ：11:59:37〜12:00:58、plannerは従来4096/input353/output164、項目監査は8192/input4807/output241、最終監査は8192/input6567/output39。該当区間にinput truncation/context shiftなし。項目監査のtokensはrunnerログ由来、最終監査はAPI usage done=true/stopとも一致。runnerのtruncated=0だけではなく前段ログも照合した。
- 時間：全体114.287742→80.701271秒、回答処理82.386→53.736秒、項目監査62.879→34.874秒。今回は一括監査が通り、個別fallback2回がなくなった。各条件1回だけの比較であり、恒常的な改善率・正答率は主張しない。
- プログラム上の結果：answer_status=answered、2項目supported、final audit=verified。グラフ経路used=falseで、今回の成功をGraphRAG発動の証拠にしない。
- **人間側の固定期待値では回答不合格。** 時刻は正しく抽出されたが、稼働曜日が欠け、案内文には原文のプレースホルダーが残り、関連する説明も省かれた。主張の根拠検査を通過することと、利用者が知りたい項目を満たすことは別。既存監査のverifiedを実用合格へ読み替えない。
- 関連52テストPASS（snapshot39［今回追加8］、workflow final12、guidance1）。一括/個別の実送信payloadで容量以外の不変を確認。`git diff --check`成功。以前からのfixture失敗は別記録のまま、全体成功とは扱わない。
- 使用中アプリ/本番・試用CONFIG/索引、原資料未変更。再抽出/索引再作成/追加モデル取得/追加質問/commit/pushなし。GUI・配布アプリ反映未実施。

再開候補（まだ実装・追加実験しない）：日時への質問で曜日/時刻/適用条件を落とさず、原文のプレースホルダーと別セルの値を混同なく案内へつなぐ汎用処理。回答の事実支持と要求充足を別々に点検する。特定業務の曜日・時間・セリフをコードに埋め込まない。容量変更以外の修正は次の承認範囲を定めてから行う。
