# Dated HITL temporal slice — independent static review v1

2026-09-09。対象は resolver `11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8` の日付候補／自動選択保留差分のみ。同一モデル・別担当コンテキストによる手続的分離であり、モデル多様性や正式な全製品監査ではない。codex-graph-engineering-adapter と graph-engineering-agentic-audit を使用した。新規の本記録だけを保存し、製品・test・旧 gold・旧記録を編集していない。テスト／製品 import・実行、実資料／CONFIG 読み取り、モデル、ネット、GUI、Git 操作はしていない。既存修正回数をリセットしない。

以下の `runs/` は `design/local-memory-v1-hardening/runs/`、その他はリポジトリ相対パス。根ディレクトリは `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。行番号は照合時のソースに対するもの。

## 結論と範囲

凍結された有限 grammar と「同じ候補 family が形成され、再利用可能な Human decision がない場合、日付信号から current／数値版のどちらにも自動選択しない」という条件付き slice について、追加の製品修正を要求する静的な逸脱は確認しなかった。固定 gold と実ログはこの狭い結論を支持する。一方、実 attestation 攻撃・Reader／app 移行・回答拒否の新しい実行証拠は五バッチにはないため、パッケージ／全安全要件の受理には進めない。

一致する旧 Human selection は今も dated family を選択できる。この一件を「新しい取込・回答使用許可」として受理してはならない。年別の独立資料、未知 family、単独候補、対応外拡張子、新候補検知、失効／競合、旧索引への回答 fallback は別の未完了境界である。先行 F05 の限定受理を否定も拡張もせず、F06／F13／F19 等の未完了と F11a の旧証跡の歴史性を維持する。

## 静的境界照合

| 境界 | 現ソースと固定 gold の根拠 | 判定できる範囲／残存 |
| --- | --- | --- |
| 202／203 の最大数値 token | resolver:38–44,126–159。NFKC 後、数字の途中から開始せず、連続数字と対応する数値複合部分を先に捕捉。gold:85–108 は `202`、`203`、5／9／12 桁、不正日、混在 separator、先頭に別数字を持つ ID を literal key/year/signal で点検。 | 正規の先頭年だけを不正な認識済み span から消す fallback はない。これはこの有限 syntax の範囲であり、任意の日付／時刻／範囲／和暦表記の完全解析ではない。 |
| 暦の妥当性 | resolver:98–123。完全一致の4桁年／年度、8桁日、同一 separator の年月／年月日、日本語年月日を選別し、固定長数値だけを `date` に渡す。gold:55–101 は2024閏日、2023非閏日、月0／13相当、不正日、全角、2039、月、年度を含む。 | 大きい未知数値を丸ごと int 化しない。月の day=1 はその年月の妥当性検査のためで、保存日時や適用開始日を作らない。日0／32の個別 literal は設計案にあったが現 gold にはない。追加するなら補助 control として区別する。 |
| 既存の year／version 注意 | resolver:135–150。明示 ver/version span は従来の年注意を保持。gold:129–143 は `ver2024.2.30`、`2024年` 親の残字、undated current／数値比較と F04 conflict。 | 不正日風の版名に変えたことで従来の year hold を失う経路は静的に認めない。新202／203範囲の外にも既存20xx年の注意は残る。 |
| family 衝突は候補に限定 | resolver:171–184,220–249,401–419,424–470。親の各 component、stem、拡張子を使い、原 relative_path を保持。gold:42–52,80–83,110–127 は全候補 hold、selected/basis null、candidate edge 数、active edge 不在、別業務／別親／別拡張子分離。 | 暦として有効な ID も family 衝突し得るが、それだけでは同一業務／置換の証明にならない。`ID20240101` と `ID20300101` の明示的な衝突ペア control は追加候補。未知数字は保持されるため、異なる未知 token の非 group 化を承認済みと読めない。 |
| current 側の正の出口 | resolver:322–326。既存 conflict の後、current を返す直前に全候補の temporal 信号を保留。gold:110–116 は dated current、undated current＋dated peer、同じ不正数値の current peer を検査。 | 前段 conflict の理由を保持したまま、自動 active への出口を閉じる。単に `status` だけを見る gold ではなく候補／disposition／edge も確認している。 |
| 数値版側の正の出口 | resolver:327–358。全 year の旧保留を維持し、数値比較より前にも temporal 信号を保留。gold:118–120 は一部だけ日付を持つ numeric peer を検査。 | current 側だけの修正ではない。undated numeric の正 control と F04 conflict も残る。 |
| 信号と完全な集合の再構築 | resolver:252–279,599–626。未知日付信号は既に set hash に入る relative_path から導出。新規の graph 自己申告信号を信用しない。gold:145–167 は新 resolver/policy、2候補、counts、順序不変、peer hash 変更を検査。 | `_validation_components` は graph を引数に持たず、caller の inventory／decisions から候補・family・groups・projection を再構築する。gold の正例は実ファイル `attest` の偽造拒否実行の代替ではない。 |
| 入力 bytes と authority | resolver:629–739 の snapshot 読み取り／strict JSON／attest は差分対象外。attest は一度得た raw を parse と hash に使い、graph 内 source path を開かず、expected 全 key を canonical 比較する。 | 0.1.4 graph を自称 hash の再計算だけで0.1.5契約へ通す静的経路はない。producer `build`:495–519 の load→別 hash read は従来の別読みのまま。下流 attest を迂回して producer metadata だけを source 結合保証にしてはいけない。 |
| 旧決定と移行 | resolver:380–394 は候補集合／選択 path／選択 source hash の一致を先に採用し、391–394で human とする。gold:170–179 はこの残存をそのまま証明。resolver:32,474–487 は0.1.5／新 policy。 | 新 family/key/hash の変化は一部の旧決定を失効させるが、同じ year-only family の旧決定は再利用し得る。バージョン更新は新しい同意の代わりにならない。 |

設計 v1 の「full-date 中心／新範囲2000–2099／month・年度未解析」に対し、executor preflight は最新ユーザー条件として新202／203 suspect run・全角・年月・年度を明記している。固定 gold もこれを実装前に literal 化している。この版付き refinement を隠れた条件削減とは扱わない。ただし元設計が列挙した全 integration／attack gold が実行済みだとはしない。

## 五つの実ログと失敗履歴

全五 `result.json` と `unittest.log` を読み、実 SHA-256 を再計算した。いずれも supervisor の30秒／1MiB上限、実行 Python3.14、skip0／expectedFailure0。自分による再実行ではない。

| runs/ 配下の run ID | actual methods / outcome | このレビューでの扱い |
| --- | --- | --- |
| dated-hitl-executor-red-temporal-001 | 16、11 failing methods／31 assertion failures、5 ok、ERROR0 | before `14ecce2c…` に対する元失敗。subTest の31を31 methodsにしない。family形成の前提 assertion で先に止まるケースもあり、全失敗を個別の自動採用到達証拠とはしない。 |
| dated-hitl-executor-green-temporal-001 | 16 PASS | 新日付 slice の正／負 control。 |
| dated-hitl-executor-green-initial-001 | 5 PASS | 元 root gold の再実行。 |
| dated-hitl-executor-green-year-001 | 10 PASS | 既存 year-only class の限定回帰。旧 Human choice 許容 control を含み、新 use consent の10件ではない。 |
| dated-hitl-executor-residual-legacy-001 | 1 PASS | `residual_not_acceptance: true`。未完成動作を観測する witness。安全性の正件数に足さない。 |

現 after の正／回帰は **31 methods**、別に **残存 witness 1 method**。RED 16 methodsを成功件数へ加算しない。root元 `dated-hitl-red-001/unittest.log` も読み直した：5 methods中3 assertion FAIL／2 ok。current-marker の元失敗は実際に selected path が非nullになる assertion であり、単なる新 helper 不在の ERROR ではない。

executor byte verifier の v1 は誤った既存 test path、v2 は異なる diff renderer 間の byte equality という検証側の仮定で失敗した。両 failure 記録を読んだ。v3 source と result は、11 input hash、gold/current-after一致、保存差分の前後適用、6 changed／3 added functions・その他21元関数AST不変を報告している。これは補助機械記録であり製品テスト／監査受理ではない。本レビューは同 verifier を実行していないが、現在 resolver=保存after と現在 test=凍結gold は独立の `cmp` でも一致を確認した。

runner は有限 profile／method 数と test hash を固定し、import後は open／directory変更／network／process を拒否する。guard は OS／RSS隔離ではない。また header の resolver/test hash read と loader の再openは別操作なので、headerだけで「実行bytesと同じ読み取りの暗号学的証明」とは言えない。保存before/after、現在hashと実ログを合わせた限定証拠として扱う。

## 次段階に必要な最小の検証（新たな実行／編集の許可ではない）

1. **同意境界**：既存の旧選択 witness を保持し、別担当の新契約で旧decisionを同一業務／現在適用／取込・回答許可へ自動昇格しない control を固定する。今回はその契約／gold 作成を重複して行わない。独立年次資料を一律 historical にしないこと、全 discovered set と revision の結合、失効後の旧索引不使用もその未完了条件。
2. **現0.1.5の再構築・consumer**：実 snapshot と完全な literal inventory から正常 graph を作り、欠落候補／偽 active edge／偽日付信号／旧policy・旧versionを各々自己hash再計算しても actual attest が拒否する小型 control。Readerは held dated member を出さず無関係controlだけを保持。strict JSON と既存 authority-mode controls を省略して全F05再受理とはしない。
3. **移行・旧世代保持**：現在bootstrap:352–367 は resolver file identity を含み、690–750 は CONFIG登録／snapshot付きcontractを再構成して mismatch をmigrationへ返す。ただし今回五runに実app移行はない。旧0.1.4登録／新0.1.5登録を区別したsynthetic control、失敗時の旧世代全byte map／CONFIG不変、回答側が旧世代の保存を使用許可と扱わない control が必要。照合時bootstrap SHAは `1e9a51a71544d4c3f8ad0c3fee1bf6f118804f37e7576e051b96daa75cf8b653` で、設計時pin `e6248aae…` と異なる。これは現在の補助静的参照であり、凍結済み統合試験証拠にしない。
4. **互換・配布**：旧 resolver test:78 と unmarked test:284,287 の0.1.4／policy literal、旧 dated-current positive/residual は不変保存されており、今回full suiteは未実行。固定旧期待を黙って書き換えず、現契約companionと旧契約拒否を分ける。package に0.1.5実bytesが入り、正規native入力／拒否／移行が動く実証拠を別に取る。古いF11aやF05のrunを現在の再実行と表示しない。

## 照合済み SHA-256

| パス | SHA-256 |
| --- | --- |
| runs/dated-hitl-scope.v1.md | 67a6dd83073af3f6fc58e17e657504bacd0059d1aa2f1d7909eec870c1826a49 |
| runs/dated-hitl-resolver-design.v1.md | 62e4df6905ea344e0083b8db4161567b0535ab1083c628e2a61ada79a83e6b2c |
| runs/dated-hitl-executor-preflight.v1.md | eafb4514dcc9eb2fa4cb0b4cd44e98cfe12dcc7c5bf37fae57c15b6af689bd54 |
| runs/dated-hitl-executor-summary.v1.md | 559f299eda8b569a73161ed0c9808d2cacab1bebadbf4f731a9443a10fc187da |
| runs/dated-hitl-executor-before-resolver.v1.py | 14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f |
| distribution/macos-local-memory/engine/document_version_resolver.py、runs/dated-hitl-executor-after-resolver.v1.py（双方同一） | 11218cc2b5170ba59a69203bcc549a50dc6eb6f88e487d191fb55a76048973a8 |
| runs/dated-hitl-executor-resolver-delta.v1.patch | 2ccb23aa6a63413b76fa56ca13c74faf552a312d4dae1c82a7d9e5a0a5ed461d |
| tests/test_dated_temporal_candidates.py、runs/dated-hitl-executor-gold.v1.py（双方同一） | 6a9a877771117441b33cdb94f5bc4f459b067bf682c538dbade00593189c7154 |
| tests/test_dated_document_human_gate.py | ee62aaf42632bca032c80a40a54893a817cd78f72c5d95e1c1078ba96a69586a |
| tests/test_year_only_supersession.py | 70a1226cb30daa6692533d2c6f1fe270fe301a8f759c0db56a142acbc945b8c0 |
| distribution/macos-local-memory/tests/test_document_version_resolver.py | 594089cdee5e0298c5c732e7da369417e14de7fea6282594acc43a55c6a4a126 |
| tests/test_unmarked_version_candidates.py | 7bf83e2823742cc1449fdd69890d780e8c8b8daf69911905ce6cb20af2ad1f29 |
| runs/dated-hitl-executor-run.v1.py | 8932d3d909c903678af4ed5ad5f0abf6352b4487ba20739765b62e494e80350b |
| runs/dated-hitl-executor-byte-check.v3.py | 5e393dfa1b4cc77392f4fc7972a299d8ae39dc5013231307c56c7c7f21330217 |
| runs/dated-hitl-executor-byte-check-result.v3.json | c476d63409d155033712bd0aa167429f8e4ee5f714db0d50091a2cec6a20fe8e |
| runs/dated-hitl-executor-byte-check-failure.v1.md | 6e8b22e00f5b5b5a84e02d0e72d06c8e6915e710c3d441b148d4eedf5a9d71a9 |
| runs/dated-hitl-executor-byte-check-failure.v2.md | 0996414f553421292187ac7c74ce6dc36d5e444c8f1cc79325fb4623a15ba160 |

以下は各runの `result.json`／`unittest.log` の順。

| runs/ 配下の run ID | result SHA-256 | log SHA-256 |
| --- | --- | --- |
| dated-hitl-executor-red-temporal-001 | 0fe77c56596f65676f5dc239af73306e99d2781316c1b00eb00993ac675fda7f | c836a32b59776da5918fede5b64f88d99b6606addd8456bc932e74ccf3b00bf4 |
| dated-hitl-executor-green-temporal-001 | b3c4b5374b818e2173b07ef42f229d6f9caced1f167a8d9e1726c50a5ed8c5f1 | f1a41e2b393fae862a1e6daa77ca982fb2a4972ee8323141b664ee202897af4d |
| dated-hitl-executor-green-initial-001 | 2d9a150fcca45f72ae1ea39dc73274482b39e00004fedf048b521f234d38b7c4 | 6d525f66ab448507abf55909573f02f1ce496a37311409d7571ef7d6c38636a4 |
| dated-hitl-executor-green-year-001 | 2b0887641269d66d30965e8d29e448491b92a9b1a64b97d77f782807a0095f23 | 68458f6302f43dd3b28ee791495ff6926f686232acb62fdf7c6fc8b71138a22e |
| dated-hitl-executor-residual-legacy-001 | 4a636946e6be9fdfd29a24f1e395ce8e6ff294799b729c4eefbdb2f92d6447dc | 5f8b23226b4512aa8fa764fc78d9516cee79362f6fff31ec8841515b2c727407 |

この記録の結論は静的 slice review であり、正式 skill schema の PASS report、全安全要件／全V1／配布許可の代わりではない。
