# General Memory v0.1

大量の既存実装を移動する前に、二つの検索経路を同じ既知正解で比較するための
小規模な合成評価セットです。実在人物・顧客・社内資料は含みません。

- `corpus/`: 人が先に作った合成資料
- `cases.jsonl`: 資料パスを正解とする曖昧検索・複数資料・時点競合・安全隔離ケース
- 正解は検索結果から逆算せず、`human_authored_synthetic` として固定
- v0.1 の対象はテキスト、CSV、DOCX。画像、複雑な表レイアウトは次段階

実行例:

```bash
rag/.venv/bin/python scripts/evaluate_general_memory_shadow.py \
  --dataset evaluation/general-memory-v0.1 \
  --out /tmp/general-memory-shadow-v0.1
```

DOCX fixtureは`scripts/build_general_memory_docx_fixtures.py`で再生成でき、文章、表、
旧版デコイ、文書内命令の安全隔離を含みます。

DOCX fixture本文は、現在のLibreOffice headless環境で日本語システムフォントが
PDF出力時に解決されないため英語で固定しています。これはDOCX構造・抽出・検索・
安全隔離を他の変数から分離して測るためで、日本語DOCXの表示互換を確認済みとするものではありません。

既定実行は外部通信を行わず、旧配布経路は語彙スコアの代理評価、新Layer 1は
実BM25で評価します。したがって「両者の完全な最終性能比較」ではなく、移動前の
再現可能な下限確認です。
