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
- パス棚卸し→意味Evidence→コンテンツ安全分離→安全索引の順で作る。
- 回答モデルは `qwen3.5:9b`、独立最終監査は `gemma4:12b`、埋め込みは `embeddinggemma:latest`。
- 監査完了後のログに、回答モデルと独立最終監査モデルの実行役割を別々に記録する。
- WebとOllamaはloopbackに限定。
- 資料内の命令文は命令として実行しない。
- 監査不合格は「わかりません」に停止する。
