# F02 次の限定契約 v1 — 年の順序だけによる自動失効を止める

作成: 2026-09-09 JST。task_id: `lms-v1-00-hardening-2026-09-09-f02-next-contract`。状態: **proposed / not_implemented / separate_review_pending**。この文書と証跡は追記型で固定し、修正はv2へ残す。本担当は製品・恒久テストを変更していない。F04aは既存受理記録の対象だけを維持し、再実装しない。

## 1. 結論と、このsliceの境界

次の実装をF02aと呼ぶ。`automatic_selection()` の年比較branchが最後に行う `unique_latest_explicit_year` による選択だけを、**候補集合全体の `needs_human_review`** に変える。年は観測された文字列signalとして残す。ファイル名に「実績」「手順」「報告」があるかで年次／改訂を分類しない。

これは、年の順序から勝手にreplacement関係を作る経路を止める安全修正である。「2024年分と2025年分は独立した年次資料だから両方利用できる」という通常機能は、独立関係の確認・表現・HITLがそろう後続契約まで未完成。保留により当該資料を回答索引へ入れなくなるため、F02全体の完成、年次資料の利用対応、保留率の改善とは呼ばない。

この実装提案に新しい人の価値判断は含めない。資料の同一性、改訂関係、独立保持、必要な時点、どれを使うかは人が確認できる状態へ残す。就寝中のユーザーの代わりに候補を選択・分類しない。

## 2. 調査基点と現状

HEAD `79a69260fb1a42efb3c0e2c0093ed6101470f2a1`、branch `codex/visual-classification-v1`。HEADとは異なる既存dirty状態をレビューした。全hashと読取範囲は `f02-next-source-manifest.v1.json`。

実装候補のbefore hash:

| ファイル | SHA-256 |
|---|---|
| `distribution/macos-local-memory/engine/document_version_resolver.py` | `f42db568267befe8a041d036242795599544ba6a46236866a643b91d4f93811b` |
| `distribution/macos-local-memory/tests/test_document_version_resolver.py` | `6031c30fbfc1b8eda0159b926ef07bbcedee1b0c2c92747b3135f9be6f6efd17` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `c16cc5bfafe493ad4096eadbf84722c8d9f1d476dcb03bfc36bb26959983341b` |

resolver全文の現状:

- `candidate:143` は年・版・状態signalがない資料を返さず、`family_key:95` は年・版・状態を除いたdirectory/stemとsuffixで候補familyを作る。これは同一性の確認ではない。
- `automatic_selection:194` は現行marker、全候補に年があるbranch、全候補に版があるbranchの順に評価する。年の最後のreturnは、全候補に版番号がありその順序が年と逆でも、年だけで選ぶ。
- `resolve_group:261` は選択ありなら一件をactive、残りをhistorical、選択なしなら全件をneeds_human_reviewにする。人の候補集合・内容hash一致を自動規則より先に評価し、stale判断は自動規則を再度止める。
- `graph_projection:321` は候補Edgeを `candidate / normalized_filename_family` として残すが、選択すると `active_version` Edgeを作る。独立年次資料を表す状態はない。
- Reader `build_adaptive_semantic_graph.py:192` はhistoricalとneeds_human_reviewを両方除外し、ungroupedを利用可能候補として扱う。保留は独立保持と同じではない。
- adaptive Validator `2530–2594` はhash／bindingを検査し、graph上でheldになっているpathのmanifest混入を拒否する。graph側の完全性・意味を独立に証明するF05は未解決。
- `record_decision:449` とserver `646–710,2693–2733` は一件選択のみ。UIは「どれを現在使う資料にしますか？」を示し、独立／別資料／保留の明示決定は表現できない。

## 3. いま実施した小型観測

`python3 -I -B design/local-memory-v1-hardening/runs/f02-next-probe.v1.py`、exit 0、Python 3.14.6。3,588 bytesの合成fixture、0.004178秒、30秒上限。sourceとstdlibを読み込んだ後、audit hookでopen・directory列挙・network・process・filesystem変更を拒否した。OS sandboxの保証ではない。

`f02-next-probe-result.v1.json` に実結果を保存した。resolverの純粋関数だけを使い、Reader／本番build／UIは動かしていない。

| ケース | 観測した現状 | F02aの期待 |
|---|---|---|
| `記録/実績2024.csv / 実績2025.csv` | 2025 active、2024 historical | 全件held、選択なし |
| `記録/資料2024.csv / 資料2025.csv` | 同上 | 同上。語彙で結果を変えない |
| `2024年/記録.csv / 2025年/記録.csv` | 同上 | 同上。basenameだけに限定しない |
| `手順2024_ver2.csv / 手順2025_ver1.csv` | 2025 ver1をactive | 全件held。年branchを削除してver2へ自動fallbackすることも不可 |
| `実績2024.csv / 現行/実績2025.csv` | 現行2025 active、2024 historical | このsliceで不変。現行labelが年次独立性を否定する証拠にはならず、F02/F04残存例 |
| 年なし `ver1.2 / ver1.10` | ver1.10 active | 既存の正常制御例として不変 |
| 年なし `現行/ver1 / ver2` | F04a理由で全件held | 不変 |
| `実績2024.csv / 実績.csv` | 無印candidate=None | F03残存例。F02aの合格対象ではない |
| 年候補にhash一致の人の選択／その後の内容変化 | 人の選択を採用／stale保留 | 不変 |

候補順を逆にした7組でgroupsが一致した。これは現状観測であり、実装後の合格試験として流用しない。

## 4. 具体的な最小差分

1. resolver `automatic_selection()` 内の `return proposed["relative_path"], "unique_latest_explicit_year", []` を、`None`、理由 `year_order_does_not_establish_supersession`、全候補のrelative_pathのリストを返す処理へ置換する。candidateに同一性の確定済みというfieldを勝手に追加しない。
2. 同branchの前段にある `latest_year_not_unique / latest_year_is_draft / latest_year_marked_historical` の保留は維持する。current markerの既存年競合・F04a数値版競合も維持する。新しい保留returnから版branchへfallthroughさせない。
3. docstring、`RESOLVER_VERSION`（0.1.1から0.1.2）、`policy.automatic_rule`を実際の規則に合わせる。policyの年による自動選択宣言を除き、`year_order_establishes_supersession: false` の追加は説明metadataとして可。`SCHEMA_VERSION`、groups/candidates/decision形式はこのsliceで変えない。
4. 変更は上の3ファイルのみ。Reader、Validator、bootstrap、server、共有schema、公開indexは編集しない。恒久テストとE2Eテストの現状期待値を理由付きで変え、正常な回答経路の試験を残す。

実装開始前に所有者と3hashを再照合する。別担当の変更を検出したら差分を確認し、古いsnapshotで上書きしない。F04a監査ファイル・既存counterexamples・旧ログは変更しない。

## 5. 恒久回帰の期待値

`test_document_version_resolver.py`:

- `test_unique_latest_explicit_year_is_selected` は年順序では失効しないテストへ名称・期待値を更新する。groups=1、resolved=0、needs_human_review=1、selected=None、resolution_basis=None、理由一致、候補数保持、全disposition=needs_human_review、active_version Edge=0を検査する。既存失敗を削除して通したことにしない。
- basename／年directory、語彙置換、順序逆転、異なる数値年、年と版の逆順、同じ年／複数年token、最新draft/historicalの境界を小型fixtureで検査する。F02a対象外の現行marker年例は残存例として別記し、年次対応の成功例へ数えない。
- 人が古い年を選んでも有効な決定なら採用する。選択先変更／非選択先変更／候補追加で決定を失効させ、全held・active Edgeなしを確認する。候補削除・移動等はF03の発見範囲内外を区別する。
- `test_answer_policy_keeps_active_and_holds_other_candidates` の正常fixtureは年なしver1/ver2へ変更し、active/historical/ungroupedの既存回帰を残す。別の年のみfixtureで、Reader eligibleが無関係の連絡先だけ、version_needs_human_review=2、version_historical=0、version_active=0、version_ungrouped=1になることを確認する（Counterの0未出力はgetで確認）。
- `test_reader_end_to_end_indexes_only_active_version` の正常fixtureも年なしver1/ver2へ変更して現行版だけのReader・Validator回帰を残す。年のみのReader caseを別に追加し、manifestは連絡先だけ、historical_version_files_held=0、version_files_needing_human_review=2、Validator PASS、原本hash不変を確認する。
- F04aの現行より大きい単一版、draft/historicalにある大きい単一版、現行が同版／新版、複数現行、candidate順、human/staleの既存試験は維持する。数値版なしのcurrent/historical folder正常例も維持する。

`test_versioned_safe_index_e2e.py`:

- `seed()` の標準「選択済み改訂」fixtureを年なし `業務内容_ver1.csv / 業務内容_ver2.csv` に変更する。path goldを明示的に対応させる（公開path、unversioned両件、human選択・変更、retrievedの旧版不在）。`test_human_choice_rebuild_and_source_change_remain_version_bound` の旧候補を現行folderへ移すケースは、受理済みF04aで初回heldになる。
- stubがbobを返す正常な回答／最終監査テストを残す。期待をinsufficientへ一括変更して成功経路を消さない。
- 年のみの別seedを用意し、公開indexのEvidenceが連絡先だけ、版graph全held、現在世代のbinding一致、原本hash不変を確認する。必要な問いへの答えが無い場合にstubからbobを注入して合格にしない。
- 全件が年候補で他のeligibleが無い場合は、現行Readerが `blocked_no_supported_files` となる既知仕様を記録する。空indexを成功公開せず、失敗時は既存CONFIG／index hashを保持する試験を追加する。この期待は正常な年次保持の完成を意味しない。UIの「未対応」表現不足は残す。
- F01/F07の引数欠落、別graph、code identity変更、内容変化、人の選択、失敗buildで旧世代保持の試験を維持する。rootのF18がValidator変更中ならfreezeされた更新hashで統合し、同時編集をしない。

試験実行は既存bounded runnerと副作用を読んだguard wrapperを使う。各focused run30秒、fixture合計1 MiB、出力1 MiB、実資料・network・モデル・GUI・Keychain禁止。必須skip／timeout／guard失敗／欠落結果はPASSにしない。純粋resolver → Reader policy → guarded Reader → stub E2Eの順で、該当する失敗だけを修正し、別担当の未見fixtureで監査する。最大2 repair rounds。

## 6. 独立年次資料と未確定保留の製品契約

次の表は後続の意味契約であり、このsliceでschemaを実装する指示ではない。

| 関係の根拠 | 保存・利用の期待 | 禁止する短絡 |
|---|---|---|
| 人が確認した独立した年次記録 | 各年を独立資料として残し、それぞれの対象期間付きでReader・安全性検証を経て利用可能にする。年をまたぐ比較／集計には両方の根拠が必要 | 最新年以外をhistorical失効、年なしへ改名して逃がす、一件だけ選ばせる |
| 同名familyの年signalだけ | どの関係かunknownのまま全候補を保持し、回答根拠はheld。根拠のないsupersessionも独立関係も作らない | 「実績」という語でannual認定、全資料を無条件active、保留を独立保持の完成として数える |
| 人が確認した改訂／正本選択 | 明示選択とその根拠・候補全集合・内容・世代を保持し、選択先だけを現在用途へ使う。失効時は再確認 | 作成日／年の最大値を人の選択へ見せかける |
| 本当に無関係と確認した別資料 | 不要な版familyを作らず、独立に処理する | suffixや共通語だけでmerge／partitionする |

今のresolverは一件選択のgroupしか表せず、`validate()` はresolvedごとのactive=1を要求する。独立保持を全activeで押し込むとこの契約と衝突する。新しい関係状態にするか、明示的な完全partitionと資料identityを追加するかは、F03/F05/HITLの契約で決める。root・候補集合・内容hash・世代・decision revisionを束ね、確認済みの根拠自体も失効可能にする。

HITLには「独立した資料」「改訂版として一件選択」「どれも違う」「保留」の意味が必要。どの具体資料がそれに当たるかはHumanの判断であり、自動ルールや一括採用で代替しない。必要な質問を一つずつ行い、現物のpath・年・版signal・プレビュー／差分・理由を見せる。既存の「どれを現在使う資料にしますか？」だけで年次分類が解決したとはしない。

## 7. 他findingとの依存と残存リスク

- **F03:** 無印、移動、改名、拡張子変更の発見は直らない。groupに入らない資料はReaderでungroupedになり得る。F02aは候補全集合の完全性を主張しない。F05の完全partitionはF03の候補契約を先に必要とする。
- **F04:** 既存年競合／F04a単一版競合を維持。年と版の逆順は新しいyear holdへ入り安全になるが、複数token・current label・部分的に年を持つ集合は未解決。current labelと年次独立性の両立も残る。
- **F05:** graph自己hash一致は意味保証にならない。空groupsや偽dispositionに対する独立検証は本sliceでは増えない。新policyをValidatorが再計算していると誤記しない。将来のpartitionはinventory側の独立goldと異なる実装経路でも確認する。
- **F06/F19:** reviewはbootstrapでReader成功より先に共有pathへ書かれる。選択受理、索引反映、公開世代の結合は未完成。年hold増加でこの画面を使う機会が増えるが、その安全性が改善したとは言わない。現行UIが独立保持を表せないことがF02完了を阻む。
- **F13:** 原本を残すことと過去質問に答えられることは異なる。heldやhistoricalは通常索引に無く、過去時点／期間別検索・最新確認は別契約。今回のholdにより答えられない範囲と候補数を隠さない。
- **F18:** Validator再検証の不変性はrootが別作業中。こちらは所有・編集しない。実行時の共通source snapshotを合わせる。

最大の可用性リスクは、これまで年で一件だけ採用できたfamilyが全保留になること。無関係資料や年なしの明示版の正常処理を維持し、保留数を表示する。全保留になった入力のbuild失敗は安全停止として記録し、通常年次業務に十分とみなさない。

## 8. GE監査記録・次の一手・rollback

Build: 候補family → 年の最大値 → active → 他候補historical → Reader除外、という現経路を確認した。year orderとsupersessionの間の根拠不足が問題で、年次資料の名称は証拠にしない。

Critique: 年branchの単純削除では年と版の逆順が版branchへ流れる反例、全active化では古い改訂版が混入する反例、全保留を「独立保持」と呼ぶ誤り、正常回答fixtureまで消す回帰を検討した。current label経路と無印漏れは別残存例として実測した。

Optimize: 年branch最終選択の一か所をheldにする案へ限定し、候補発見・HITL・F05は追加契約へ分離した。Primary Pathは「未支持の自動失効を止める → 全候補と未確定理由を残す → 人が関係を確認できるHITL・独立資料契約へ進む」。本レビューは実装の監査PASSではない。

次のbounded action: rootがこの契約を別担当レビューし、3ファイルの所有者とlive hashを固定する。Executorは新しい実装task契約を作成してREDを保存し、最小差分・回帰・別担当監査へ進む。現contractを後から実装済みに書き換えず、新成果物から参照する。

rollbackはF02a固有の差分だけを手動で逆適用する。git reset／一括checkoutを使わず、F04a、F01/F07、既存ユーザー変更、root F18を保持する。新resolver code identityで旧Reader registrationが失効するため、既存公開indexを新policyで検証済みとは扱わない。未公開世代で再構築し、失敗したときに旧policyを無条件で「最新」として公開しない。

本レビューで未実施: 製品修正、恒久テスト変更、Reader/index/質問の実行、UI操作、実資料、モデル・OCR、全形式、配布物、性能受入。変更したのは本担当所有の新しい `runs/f02-next-*` のみ。
