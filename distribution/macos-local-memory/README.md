# Local Memory Search macOS package

非技術者向けの未署名macOS試作パッケージです。GitHubの操作は不要です。

## ビルド

```bash
./distribution/macos-local-memory/build/build_package.sh
```

生成物:

- `deliverables/Local-Memory-Search-macOS-unsigned.dmg`
- `deliverables/Local-Memory-Search-macOS-unsigned.zip`
- `deliverables/Local-Memory-Search-macOS-unsigned.sha256.txt`

DMGにはユーザーデータ、既存索引、回答ログ、モデル本体を含めません。

## 設計上の境界

- 原本は読み取りのみ。
- パス棚卸し→形式・領域別Reader→位置付きEvidence→関係を保つSearchUnit→コンテンツ安全分離→安全索引の順で作る。
- Validatorは処理の目的ではなく、読取りの欠落や不確実性を見つけ、次のReader・再読・留保へ分岐させるために使う。
- XLSXはセル位置と数式をnative構造から読む。画像はApple Visionと、利用可能な場合のTesseractをローカルで切り替える。
- 画像の独立した複数観測の一致は高信頼、単独観測や同一engine内の一致は暫定とする。位置付きOCRが空な場合は導入済みGemmaの座標なし全体文字起こしも暫定で残し、暫定Evidenceだけの確定回答を機械検証で停止する。
- XLSXの数式は式とファイル保存時の値を別Evidenceで保持し、未再計算であることを明記してcell/rowの両検索経路へ渡す。
- 長い抽出結果は全形式共通のsemantic境界で1,600文字以下のexact shardへ置き換え、hashと文字offsetで全文復元を検証する。埋め込みや回答監査でpacketの後半を黙って切らない。
- AIを読取に使ったかはLayer 1 Evidenceのprovenanceから派生し、semantic stateの申告とvalidatorで照合する。
- 初回はReader/security検証後にモデルを取得し、Gemmaが新規取得され画像がある場合だけ、別の空ディレクトリでsemantic/securityを再構築・再検証してから公開する。
- 世代にbuild IDとowner PIDを持たせ、起動時に中断を判定する。未公開の中断世代だけを整理して再実行へ案内し、公開済み世代はreadyに復旧する。
- SQLite index schema `0.3`は、後続のEvidence Graph投入用に空の`graph_nodes`と`graph_edges`を持つ。現段階は`graph_status=schema_only`、`graph_retrieval_enabled=false`で、既存の回答検索には使用しない。
- 認可済みDocument／Evidenceと検証済みnative structural Relationの独立projectorはfixture検証までとし、索引builder・bootstrap・回答経路へはまだ接続しない。
- 回答は `gemma4:12b`、別コンテキストの最終監査も `gemma4:12b`、埋め込みは `embeddinggemma:latest`。
- 回数・合計質問は、ベクトル検索の前にQuestion Evidence Graphを作る。質問の対象、`SUM`範囲、全行coverage、再集計値、保存値をNode/Edgeで結び、一致したEvidenceを回答実行者へ先頭挿入する。
- 構造化集計がない、範囲が欠ける、暫定読取を含む、保存値と再集計が違う、または複数候補が曖昧な回数質問は `hold`にし、通常検索へ逃がさない。
- 回答と監査の間で、質問契約・主張グラフ・Evidence参照を決定論的に検証する。
- 最終監査は回答セルだけでなく、Question Evidence Graphが指定した全集計EvidenceをSQLiteから再読込し、Graph hash、Claim Node、Edge端点、数値一致を機械検査してから別コンテキスト監査を行う。
- 監査完了後のログに、回答コンテキストと最終監査コンテキストの実行役割を別々に記録する。
- WebとOllamaはloopbackに限定。HomebrewのCLI-only Ollamaも検出し、daemon停止時は専用ログ付きで`ollama serve`をloopback起動する。
- 資料内の命令文は命令として実行しない。
- 監査不合格は「わかりません」に停止する。
