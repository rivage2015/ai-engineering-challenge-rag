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
- SQLite safe-answer index schema `0.3`は、検証済みの`graph_nodes`と`graph_edges`、Graph hash、安全partitionを埋め込みと同じ未公開DBへ書き、全検査成功後だけ`graph_status=validated_safe_partition`、`graph_retrieval_enabled=true`として原子的に公開する。prompt-library indexは`schema_only`のまま回答には使わない。
- semantic validatorはSearchUnitとLayer 1 Evidenceから`derived_from`を独立再構築し、完全なfan-inだけを`semantic-lineage-relations.jsonl`へ昇格する。長文shardや未投影binaryを含むfan-inは理由付きで保留する。
- native structural RelationはLayer 1の`parent_evidence_id`と`preceding_heading_evidence_id`から再構築し、IDだけでなくRelation全フィールドと集合の完全一致を要求する。呼び出し側が`structural`や許可済みproducer名を自己申告してもEvidence間Edgeに昇格できない。
- ChartTable containmentは専用のsource-bound再構築contractができるまで`not_explicit`として保留する。producer名だけでverified Edgeにしない。
- Content Security Gateのvalidatorは入力から分類と6成果物を独立再生成し、自己整合した偽の`safe-answer-evidence.jsonl`も拒否する。safe Graphは安全な枝だけを残し、1つでも除外sourceを含むlineage fan-inは全Edgeを原子的に保留する。
- 保留されたderived Evidenceが回答用safe streamに残る場合、Nodeは削除せず`unresolved`と理由を付ける。除外Evidenceをendpoint/supportに持つstructural Relationは付け替えず保留し、全明示Relationを`promoted`または`held`のどちらかに完全分割する。
- 認可済みDocument／Evidenceと、source-bound validatorをprojector自身が再実行して得たnative structural／verified lineage Relationを、索引builderとbootstrapへ接続する。回答v1/v2、Question Evidence Graph、最終監査は同じGraph hash・安全partition・埋め込み空間を再検査し、`unresolved` Evidenceを取得・再挿入・引用できない。回答cacheは、現在のGraphからcanonical回答を完全再構築する契約ができるまでfail-closedで無効化する。呼び出し側が作ったPASS辞書だけではRelationを投入できない。
- 回答は `gemma4:12b`、別コンテキストの最終監査も `gemma4:12b`、埋め込みは `embeddinggemma:latest`。
- 回数・合計質問は、ベクトル検索の前にQuestion Evidence Graphを作る。同じSQLite read snapshotの永続GraphをDocumentから`contains`順方向・`derived_from`逆方向へ実際に辿り、使用したNode hashとRelation IDを質問Graphへ固定する。その上で質問の対象、`SUM`範囲、各行、再集計値、保存値をNode/Edgeで結び、一致したEvidenceを回答実行者へ先頭挿入する。
- 構造化レコード参照は、現在`owner / review_date / unit_cost / seats / budget`の5項目に限定する。質問に明記された項目、一意の対象行、必要なら`Approved / Final / Finalized`状態を確認し、項目ごとにrow・header・value Evidenceをverified explicit `derived_from`で結ぶ。各枝のEvidenceは対応する項目の検索先頭に限定して挿入する。
- 構造化集計がない、範囲が欠ける、暫定読取を含む、保存値と再集計が違う、または複数候補が曖昧な回数質問は `hold`にし、通常検索へ逃がさない。
- 構造化レコード参照で、質問要求の計画漏れ、未対応項目、状態や対象の曖昧性、必須lineage欠落、Graph枝の未使用がある場合も`hold`にし、通常検索へ逃がさない。
- 回答と監査の間で、質問契約・主張グラフ・Evidence参照を決定論的に検証する。
- 最終監査はQuestion Evidence Graphの選択・検証EvidenceをSQLiteから再読込し、保存Graph traversalを再構築する。集計では保存値と再集計値、レコード参照ではGraph枝とfield runの1対1、検索先頭挿入、回答値と枝の値の一致、値セルEvidenceの支持を機械検査する。別コンテキスト監査まで含む全ゲートがPASSした場合だけOrchestratorが回答をacceptする。
- 監査完了後のログに、回答コンテキストと最終監査コンテキストの実行役割を別々に記録する。
- WebとOllamaはloopbackに限定。HomebrewのCLI-only Ollamaも検出し、daemon停止時は専用ログ付きで`ollama serve`をloopback起動する。
- 資料内の命令文は命令として実行しない。
- 監査不合格は「わかりません」に停止する。
