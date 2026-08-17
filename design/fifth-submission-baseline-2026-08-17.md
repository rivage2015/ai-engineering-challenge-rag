# 第5回提出 基準記録

記録日: 2026-08-17

## 結果

- 第4回 Public score: `0.40000`
- 第5回 Public score: `0.43333333333333335`
- 差分: `+0.03333333333333335`
- 第5回投稿日時: `2026-08-17 21:43:14`

集約スコアだけから、変更した4問のうちどの問題が正解だったかは判定しない。30問相当の評価単位なら正味1単位の改善に相当するが、問題別の正誤は復元できない。

## Git基準点

第5回は、次の第4回コミットを親として生成した未コミット増分から作成した。

```text
44917ede59fcc30440398cda814fcb3c1ed33174
2026-08-17 第4回提出 スコア0.40000
```

## 提出物

```text
rag/out/submission_graph_incremental_test_20260817_v17.zip
SHA-256: a32390d9a4a0ec1a6273869af901b2a65767df90ab06fe6379da8208600e040f

ZIP member: predictions.csv
payload SHA-256: 3c5941c7b0a3e205a3631212f08feeb4c67a539d132ab3b202c276911e80e03a
```

第4回base:

```text
rag/out/submission_graph_bold_test_audited_20260817_v16.zip
SHA-256: 1b0acb3bc98fbd82833c9560735b7af653f2ac34badb40b712e7f368c8e1bd47
```

## 採用方針

- 全100問のGraphPlanとstructured candidateを先に計算した。
- strict pass、resolved、非空回答、output contract違反なしをすべて要求した。
- 第4回の具体回答58件は変更しなかった。
- 第4回が完全一致で `わかりません` だった行だけを置換した。
- eligible 19件のうち、置換対象だった4件だけを採用した。
- adopted 4件、changed 4件、gate bypass 0件。

採用index:

```text
5, 39, 65, 83
```

## 実装した証明経路

- index 5: leaderboard、metrics、run summary、config、training code ASTを横断したbest-model parameterの厳格整数解決。
- index 39: XLSX ChartExの内部data schemaに基づくseries field解決。
- index 65: visible semantic sheetとexact ARGBのconditional-format predicate解決。
- index 83: 一意な係数表と対象行によるraw Decimal線形回帰、最終ROUND_HALF_UP。
- certified extended graphをgeneric table readerより先に実行し、source-specific executorが証明できない場合はhold。
- identifierの単一内部空白とASCII comma listを安全に扱うoutput validator修正。

## 検証

第5回関連targeted suite:

```text
76 tests
OK
```

2026-08-17夜の全suite:

```text
Ran 481 tests in 32.106s
OK
```

追加確認:

- Python compile成功。
- Git whitespace check成功。
- ZIPはroot直下に `predictions.csv` 1件のみ。
- ZIP timestampは固定。
- 2回生成でCSV／ZIP bytesが一致。
- ZIP、CSV payload、監査logのhashを実ファイルから再照合済み。

## 次回以降

第5回を新しい提出baseとして扱う。次回も既存の具体回答を保ち、原本とoperation graphで証明できた候補だけを増分採用する。Public scoreから問題別正誤を逆算してルールやOCRを調整しない。
