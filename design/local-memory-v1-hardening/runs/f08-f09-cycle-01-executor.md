# F08 / F09 修正サイクル1 — 実行担当の引継ぎ

状態: **13 test methodsがGREEN、別担当監査待ち。製品V1.00の合格ではない。**

## 変更と判断

- `Probe.extract_xml`: mixed contentの`element.text`と各子の`tail`を原文順に保存。tailは子の本文ではなく親の次のtext nodeとして`/text()[n]`へ結合する。空白だけの非空文字列もraw_textに残す。
- 汎用XMLの入口で既存`validate_xml_bytes`を再利用する。`read_text`に任意の`byte_validator`を加え、別読込ではなく、実際にdecodeする同じbytesを検査する。他形式はキーワードを渡さないため従来の挙動を保持する。UTF-8、UTF-16、UTF-32の小型危険fixtureがElementTree到達前に拒否されることを確認した。
- `Probe.extract_json`: `object_pairs_hook`で各objectの重複名を検出し、`ValueError("duplicate JSON object key")`を返す。競合値を勝手に選ばず、ログへ任意のkey文字列を埋め込まない。同じ名前でも別objectなら許容する。
- schemaを調べ、独自のpartial/heldを新設せず、既存のfile transactionに従う明示失敗を選んだ。`process_file`はEvidence/Relationをrollbackし、source path/hashと理由付きのfailed Documentを保存する。重複JSONを読めたふりで索引へ渡さない。

この修正はリポジトリAdapterの責務であり、Graph Engineering Core・共有schema・抽出バージョン定数・全体fingerprint設計を変えていない。`codex-graph-engineering-adapter`の差分／検証／rollback境界と、`graph-engineering-agentic-audit`の自己承認禁止・別担当引継ぎを適用した。

## 実行結果

詳しい条件・hash・コマンドは同名のJSONへ保存した。

1. RED: 10 methods、10 failuresと6 errors（subtest込み）。XMLの「前/中/深/内後/後/終」が「前/中/深」だけ、JSON重複6例は例外なしで再現した。危険XMLの6 errorsは安全検査なしでmock parserまで到達した結果であり、安全拒否成功とは数えていない。
2. 最小修正後: 10 methods / 0.109秒 / PASS。
3. 回帰・下流確認追加後: 13 methods / 0.102秒 / PASS、skip 0。180秒timeoutと8 MiB出力確認を付けたrunnerで実施した。
4. `git diff --check`: exit 0。

統合fixtureは7 files。通常XML/JSON、重複JSON2例、壊れたJSON/XML、DTD XMLを同じ一時rootに置いた。本番Intermediate builder → batch/streaming Validator → SearchUnit builder → batch/streaming Validator → semantic adapterまで実行し、失敗5 filesはEvidence/Relation/SearchUnitが0、adapterは`unresolved / extraction_failed / evidence_ids=[]`、正常2 filesは保持された。「後」がSearchUnitの`/p[1]/text()[2]`とsemantic Evidenceへ残ること、全原本hash不変も確認した。

モデル／外部通信／個人資料の使用は0。視覚観測はsuppressed。fingerprint、Paddle session、password discoveryのみ隔離用stubを使用し、Ollama metadata関数をAssertionErrorで禁止した。安全SQLiteの公開・回答まで通したとの主張はしない。

## 残る境界と互換性

- XML初期text locatorは`/text()`から`/text()[1]`へ変わる。既存locator規約はexpanded tag nameと全子順であり、一般的なXPath実装ではない。
- parserによる改行・entity表記・CDATAの正規化は従来どおり。raw_textはparserが観測した文字列であり、元XML bytesの完全再現ではない。元ファイルは変更せずhashで参照する。
- 大型JSON/XMLは既存の`bounded-text-stream / partial`へ入り、構造を解釈せずliteral textを保存する。この経路を「JSON/XML構造完全読取」として合格にしていない。
- 深さ／要素数、全encoding、コメント・PI、非有限JSON値等はこのF08/F09修正の合格範囲外。全形式の安全性を保証しない。
- 既存`processing_fingerprint`はProbeのコードhashを含む。再build時の再抽出対象化は静的に確認したが、既存公開索引を更新・再公開していない。配布物の再buildも未実施。
- 全`test_layer1_pipeline`のclass setupはsubprocess fingerprintを呼ぶため今回は実行していない。新しい隔離suiteで該当するencoding・file transaction・下流契約を実行したのであり、既存全suiteのPASSではない。

## 次とrollback

親担当が、scoped diff・JSON記録・gold fixtureだけを別監査役へ渡す。実装者はF08/F09を監査PASSにしない。ユーザー既存変更と他agentの差分は触っていない。

rollbackが必要なら、`read_text`のoptional hookと`extract_json / extract_xml`の本サイクル差分だけを逆適用する。一括resetをせず、試験・証跡は保存する。旧Readerで生成した過去索引へ無条件に戻すことは、既知のtail欠落／last-winsを復活させるので安全なrollbackとは呼ばない。
