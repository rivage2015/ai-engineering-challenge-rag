# Local Memory Search 新規スレッド引き継ぎ

更新日: 2026-08-25
作業ルート: リポジトリのルートディレクトリ

## 1. ユーザーの本来の目的

自分のMac内にあるWord、Excel、PowerPoint、PDF、テキスト等を完全ローカルで読み、「あの頃どうしたっけ」「あの人と何を話したっけ」という曖昧な記憶から資料を探し、根拠付きで回答するシステムを完成させる。

ローカルデザイン知識エンジンは面白い派生案だが、本筋は「Mac内の汎用記憶検索」である。

## 2. 重要な設計方針

- 原本は読み取りのみ。原本を書き換えない。
- パス棚卸し → 内容Evidence → 危険な命令文の事前分離 → 安全な検索索引の順で作る。
- JSON/JSONLのノードとエッジを「しおり・案内板」として使う。
- Graph RAGに加え、回答と別の文脈/モデルで敵対的監査するGraph Engineering的構成。
- 資料内の命令文は実行しない。プロンプトインジェクション候補は質問と無関係に事前隔離する。
- 根拠不足、矛盾、対象・時点・版の曖昧さが残るときは、理由付きの「わかりません」で停止する。
- 通常回答は断言しすぎず、必要な場合だけ留保を付ける。「自分で確認して」の定型句は約10回に1回程度に抑える。
- 通常利用時は外部へ資料を送らない。OllamaとWeb UIはloopbackのみで動かす。

## 3. 現在のローカルAI構成

- 回答用: `qwen3.5:9b`（実測約6.6GB）
- 独立最終監査: `gemma4:12b`（実測約7.6GB）
- 埋め込み検索: `embeddinggemma:latest`（実測約621MB）
- 実行箱: Ollama
- 記憶層: JSON/JSONL + SQLite埋め込み索引
- UI: `127.0.0.1:8765`
- Ollama API: `127.0.0.1:11434`

## 4. macOS配布パッケージ

ソース:

`distribution/macos-local-memory/`

主要ファイル:

- `app/launch.sh`: 初回診断、Python/Ollama導入、フォルダ選択、サーバー起動
- `app/bootstrap.py`: モデル取得と索引構築の統括
- `app/local_memory_server.py`: ローカルWeb UI
- `app/final_answer_audit.py`: Gemma 4による独立最終監査
- `engine/`: パスGraph、意味Evidence、セキュリティゲート、索引、回答処理
- `docs/START-HERE.html`: 非技術者向けの最初の案内
- `docs/はじめにお読みください.md`: 詳細ガイド
- `build/build_package.sh`: DMG/ZIP生成
- `tests/test_package.py`: 配布パイプラインのテスト

生成済み:

- `deliverables/Local-Memory-Search-macOS-unsigned.dmg`
- `deliverables/Local-Memory-Search-macOS-unsigned.zip`
- `deliverables/Local-Memory-Search-macOS-unsigned.sha256.txt`

DMG SHA-256:

`5aed61259a4126840b6bf96cd9955b0d02fd28070f22716a5395937d195e1002`

ZIP SHA-256:

`d611378078b537e7ee8de506a573daafc7879feb2e090aecd4c9b43406b8ea3c`

パッケージ本体はDMG・ZIPともに1MB未満。約15GBのAIモデルは初回に相手のMacが公式配布先から自動取得する。

## 5. 配布相手の想定操作

GitHub、ターミナル、コマンド入力は不要。

1. DMGを開く
2. アプリをApplicationsへドラッグ
3. 初回起動を許可
4. Pythonがなければ公式署名済みインストーラをクリックで完了
5. Ollamaの自動導入を許可
6. 検索対象フォルダを選択
7. 「初回セットアップ」を押す

推奨: macOS 14以降、Apple Silicon、メモリ24GB以上、空き40GB以上。

## 6. 対応形式と限界

本文対応:

- TXT、Markdown、CSV、JSON、YAML、HTML
- DOCX、PPTX、XLSX
- テキスト抽出可能なPDF

ファイル名/メタデータのみ:

- 画像、音声、動画、圧縮ファイル

未対応/変換必要:

- 古い `.doc` / `.xls` / `.ppt`
- 画像だけのPDF
- 手書きOCR
- 暗号化ファイル

## 7. 検証済み事項

- Pythonコード構文検査
- zsh構文検査
- TXT/XLSX → パスGraph → 意味Evidence → 安全分離のテスト
- Content Security Gateの `never_execute` 契約
- Web UIのloopbackバインド限定
- Python/Ollamaの公式配布先と署名確認コード
- DMGチェックサム
- DMG内アプリのコード署名整合性
- DMG内の個人データ、SQLite索引、JSONL、ログ非混入
- 回答用Qwenと独立最終監査用Gemma 4の役割分離とモデル記録
- `python3 -m unittest -v distribution/macos-local-memory/tests/test_package.py` は5テストPASS

## 8. Git状態

- ブランチ: `codex/visual-classification-v1`
- リモート: `https://github.com/rivage2015/ai-engineering-challenge-rag`（private）
- macOS配布ソースは `distribution/macos-local-memory/` でバージョン管理する。
- DMG/ZIPはdeny-default `.gitignore` によりGit対象外。

## 9. 新規スレッドの再開手順

1. 最初にこのファイルを全文読む。
2. `git status --short`、上記ハッシュ、生成物の実在を読み取りで確認する。
3. 現状確認と相違を先に報告し、いきなり実装・削除しない。
4. ユーザーの次の依頼から継続する。

## 10. 次に検討しやすい項目

- 実際の別Macで初回導入を行い、クリック数と権限プロンプトを記録する。
- Apple Developer IDによる署名・公証が必要か決める。現在はadhoc署名の試作版。
- PDF本文抽出の `pdftotext` 自動導入をどこまで含めるか決める。
- モデルの初回取得途中の進捗表示をより分かりやすくする。
- 回答・監査の実データ回帰テストを追加する。
