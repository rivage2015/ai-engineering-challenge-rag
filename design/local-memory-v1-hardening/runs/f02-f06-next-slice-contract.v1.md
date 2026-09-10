# F02–F06 次の最小実装契約 v1

2026-09-09。状態: **planned_not_implemented / needs_review**。製品コードは変更していない。V1計画の作り直しではなく、次の1差分の境界を固定する。基点は HEAD `79a69260fb1a42efb3c0e2c0093ed6101470f2a1` と併記したdirty source hash。F01/F07の局所監査PASSは親taskからの入力で、本作業が再監査した結果ではない。

## 最初の1差分: F04a 現行マーカーと新版番号の矛盾を保留

`automatic_selection()` が唯一の現行候補を返す直前に、現行候補より大きい明示版番号を持つ候補が同じfamilyにないか確認する。比較可能な数値版の矛盾があれば、自動でどちらかを採用せず `needs_human_review` に戻す。先に実装する理由は、年次資料か改訂版かという人の意味判断を必要とせず、現在採用している数値版規則内の明確な矛盾を直せるため。

| 入力・条件 | 現在の結果 | このsliceの期待結果 |
|---|---|---|
| `現行/業務_ver1.xlsx` と `業務_ver2.xlsx`、人の決定なし | ver1をactive、ver2をhistorical | 選択なし、両候補をneeds_human_review、理由 `current_marker_conflicts_with_latest_version` |
| `現行/業務_ver1.2.xlsx` と `業務_ver1.10.xlsx` | ver1.2をactive | 数値tupleで1.10を大きい版と確認し、両候補を保留 |
| `現行/業務_ver2.xlsx` と `業務_ver1.xlsx` | ver2をactive | 同じ結果。無矛盾の既存正常系を全保留にしない |
| 上の競合集合に、集合hash・選択先hashとも一致する人の明示選択 | 人の選択を採用 | 同じ結果。人の決定は自動規則の前に評価する |
| 人の選択後に候補集合または内容hashが変化 | stale_human_decision | 同じ保留。新しい自動規則で失効を救済しない |

実装範囲は、現行候補と比較相手それぞれが単一の明示版番号を持つケース。相手がdraft／historicalでも、より大きい番号の存在を黙って無視して現行を確定しない。既存の年競合チェックを残し、版番号を年・現行状態へ一方的に優先させない。複数の異なる版token、無印、年と版が互いに逆順の場合などは、このsliceでF04全体を閉じず、別反例としてneeds_reviewを保持する。

予定変更は `distribution/macos-local-memory/engine/document_version_resolver.py` の `automatic_selection()` とresolver version/policy説明、および `distribution/macos-local-memory/tests/test_document_version_resolver.py` 内の関連単体試験だけ。group／候補schema、family_key、HITL、bootstrap、既存索引を同時に変更しない。Core原則を変えない。実装開始時にファイル所有とhashを再確認する。

## 実装前後に守る回帰

- まず上の2競合例が現在失敗する試験を固定し、その後に修正する。成功例・期待値を既知ファイル名で分岐しない。
- 現行が新版、現行なしの `ver1.2 < ver1.10`、複数現行、既存の年競合、draft／historicalの保留、人の選択再利用／失効を確認する。候補順を逆にしても結果集合とhashが変わらないことを確認する。
- `resolve_group()` のstatus・selected・reason・候補dispositionと `graph_projection()` のactive edge不存在をセットで検査する。失敗を理由文字列だけで検査しない。
- F02を固定している既存の「年の最大値がactive」試験は、このsliceでは変更しない。ただし望ましい製品仕様の証明には数えない。F02の仕様変更時に理由付きで期待値を変更する。
- 実装後、純粋resolver試験→Reader policyの候補除外→F01/F07のstub配線試験の順に必要範囲を確認する。既存suiteのimportがReader/Validatorまで読み込むため、実行前に依存と副作用を再確認する。実モデルの起動は別条件。
- 数値上限は合成fixture合計1 MiB、log 1 MiB、1回30秒。network／モデル／GUI／Keychain／実資料は禁止。必須skip、guard停止、timeoutはPASSにしない。

## F02–F06で残る契約と反例

| ID | 今回確認したbefore | 後続に必要なafter・未決 |
|---|---|---|
| F02 | `実績2024.csv / 実績2025.csv` を同familyにし2025を選ぶ。2024はReaderで除外される規則 | 年の数字だけでreplacement関係を作らない。年次記録と確認できた資料は2024をhistorical失効させない。改訂／年次／別資料が不明なら分類自体をneeds_reviewに保つ。「実績」という単語による自動分類を導入しない。過去質問に答えるには別途時点付き索引契約が必要 |
| F03 | `業務内容2024.xlsx / 業務内容.xlsx` の無印がcandidate=Noneとなり、単独候補groupも出ない | 候補の発見とreplacement判断を分け、無印を検討集合から黙って落とさない。拡張子変更・移動・改名の同一性は現在のsuffix＋directoryキーでは保証不能。全拡張子／別部署を自動mergeせず、疑わしい関係と未確定理由を保存する。追加・削除・移動後の人の判断失効を確認 |
| F04 | 現行ver1がver2より優先 | 上のF04aで一部を修正。年と版の逆順、複数token、不足signalは別の未解決として残す |
| F05 | inventory hashが一致し、groups/nodes/edgesを空にして自己hashを計算したgraphがvalidate=PASS | callerが与える信頼済みinventoryとdecision contextから候補全集合・完全なpartition・選択根拠・counts・nodes/edgesを独立に確認する。候補欠落/重複/別family混入/偽active/全消去を拒否。graphのgroupsを正解にせず、graph内decisions_pathを無条件に開かない。build()を再実行して一致しただけでは独立検証と呼ばない |
| F06 | UIはneeds_human_reviewのみ。bootstrapはReader/index完成前に共有reviewを上書きする | 自動解決済み候補も訂正可能にし、root・family・候補hash・graph hash・世代・decision revisionを同じ選択要求へ束ねる。公開reviewは公開CONFIGの世代から参照し、未公開の確認候補はpendingとして明示する。選択受理と索引反映成功を分ける。別資料／どれも違う／保留を人が選べる契約が必要 |

F05を先へ進めるには、F02/F03の候補全集合・ungrouped／held／excludedの完全分割の定義を固定する。現行policyに対する限定検証を先行しても、F02/F03が解決したとはしない。low-level hash/正規化の共有はあり得るが、候補欠落の独立gold、異なる経路での集合比較、別担当のholdoutを必要とする。

F06はF19の選択revision／更新lease／CASと重なる。古い画面・二重送信・build中・失敗後・root切替の選択を拒否または明示保留にし、未反映を成功表示しない。全候補が保留で公開索引がまだない場合でも、人がpending候補を確認できる必要があるため、「公開後にしかreviewを表示しない」だけの修正は採らない。この表示と選択方式は未実装の依存契約。

## 証跡・限界・rollback

`f02-f06-counterexamples.v1.py` を `python3 -I -B` で実行。F02–F05の4観測を計2,008 bytesのメモリ内fixture、0.008347秒で再現した。詳細は同名JSON。resolver全体を読み、stdlibを事前ロード後にfilesystem open・network・process等を拒否するaudit hookと30秒alarmを設定した。これはOS sandboxの証明ではない。F05はin-memory pathを渡したValidatorの論理検査で、実ファイル公開攻撃の成立を確認した結果ではない。

F06は静的確認のみ。製品コード修正、実資料へのアクセス、HITL実操作、Reader/index/質問までの実行、既存全suite、実モデル、配布物検査は今回未実行。F04aを実装してもF02/F03/F05/F06/F19、freshness、全V1受入は未解決のまま。

参照箇所: resolver `candidate:143 / automatic_selection:194 / resolve_group:249 / build:356 / validate:408 / record_decision:433`、Reader `apply_document_version_policy:192`、adaptive Validator `2530–2558,2582–2594`、server `646–704,2693–2733`、bootstrap `3655–3672`（review作成）と `3769–3774`（索引公開）。同時作業で行番号やhashは変わり得るため、実装前に再確認する。

rollbackはF04aの個別差分だけを戻す。ユーザーや他担当のdirty差分をresetしない。resolverコードのidentityはReader世代契約へ既に入るため、過去の公開索引が新policyで検証されたとは扱わない。再構築は未公開世代で行い、失敗時に旧policyの索引を無条件で「最新」として再公開しない。
