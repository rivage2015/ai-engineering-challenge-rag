# Dated document HITL authority — bounded static review v1

2026-09-09。対象は `dated-hitl-scope.v1.md` の日付違いの業務資料の候補・Human 承認・取込／索引／回答への伝播。使用 skill: codex-graph-engineering-adapter / graph-engineering-agentic-audit。既存 reviewer context の手続的レビューであり、正式 artifact 監査／製品 PASS ではない。

書込みは新規本書のみ。製品・test・既存記録の編集、Python import／実行、server／model 起動、実資料／実 CONFIG の読取、network／GUI／追加 agent は行っていない。既存 F05 受理は維持。F06/F13/F19 は未解決、F11a の freeze／未受理状態と既存修理回数を変更しない。以下の race は source に基づく反証候補であり、今回実行して成立を確認した攻撃ではない。

## 結論

**日付を family key から除くだけでは、今回のユーザー要件を満たさない。** 現行は「ファイル名由来の同一家族 → 自動または一つの active 選択 → 世代 snapshot 内で一貫した選択」を実装する。一方で「同じ業務の改訂か／独立した年次記録か」「現在の適用対象」「その内容の取込・回答利用許可」の別判断と、その判断を表示 revision・公開 generation・回答時点へつなぐ権限が足りない。

最小の次段階は、候補認識の RED を保持したまま、Human authority の状態と表示時 revision を先に固定し、次に decision→generation の競合制御、最後に通常回答も含む freshness／利用ゲートを固定すること。旧世代の不変性と「今は利用してよい」の判定は分ける。承認失効時に旧世代を書き換えて失効を表すことは提案しない。

## 初期 RED が示す範囲

`tests/test_dated_document_human_gate.py` 全文と `dated-hitl-red-001` の log/result を読んだ。保存結果は **5 methods / 3 assertion FAIL + 2 ok / skip 0**、status failed、exit 1、elapsed 0.051879 s、log 3724 bytes。実 log SHA は result の値と一致。

- compact 完全年月日：`受付手順_20240101.xlsx` と `受付手順_20250202.xlsx` の key が一致しない。
- 日本語年月日：年だけ除去され、`受付手順年1月1日` と `受付手順年2月2日` が別 key。
- dated current-marker：`受付手順_現行_2025.xlsx` が自動選択され、Human approval 必須という新しい期待に反する。
- 無関係な受付／配膳の分離、year-only peers の保留は ok。

fixture は全候補に同じ literal source hash を置く pure records。したがって content 差替え、完全 inventory からの group 構成、取込拒否、UI、競合、回答 freshness をこの 5 methods で検証したとはいえない。初期三つの FAIL はそのまま保持する。

## exact source gaps と残っている保護

以下の短名は末尾の SHA 表の source を指す。

### A. 候補機構は関係の承認ではない

resolver `37–49,91–113,149–185` は YEAR / version / status marker の除去と directory＋stem＋extension の exact key を用いる。compact 日付は YEAR の digit boundary に一致せず、日本語年月日の month/day は残る。`402–420` は同じ key の少なくとも二候補、かつ一つ以上 version signal がある group だけを作る。改名・別 directory・別 extension・無信号 peers・未読候補が同じ業務かという問題はこの規則では閉じない。

resolver `218–245` は矛盾しない unique current marker を Human なしで採用する。`246–260` の year-only hold は保持されているが、その前の marker 分岐を止めない。`261–273` の numeric-version 自動選択も、日付付き資料に適用する範囲は新契約で明示が必要。単に日付 token を除いた結果を同一業務／置換関係の証明にしない。

adaptive builder `192–202` は attested disposition が historical / needs_human_review のものを除外するが、disposition のない `version_ungrouped` は取込へ進める。これは F05 の完全な「現行ポリシーの再構築」と整合する一方、未認識の dated peer が承認を免れる入口となる。全ファイルを無断で拒否対象へ拡大するのでなく、今回の対象 family の発見範囲と「membership 不明」の扱いを契約で固定する必要がある。

### B. 一問ずつの判断と利用許可の記録がない — F06

server `646–703` は status `needs_human_review` だけを列挙し、全 group の form を同時表示する。質問は「どれを現在使う資料にしますか？」、必須 radio、一つの採用＋索引再構築 button のみ。自動 resolved の訂正、別業務／独立年次、分からない／保留、後からの撤回、取込／回答の利用許可を表す UI はない。

resolver `663–695` の保存内容も group、candidate hash、selected path/hash、actor、時刻だけ。`295–309` はその hash/path が一致すれば `human_confirmed_active` とする。関係の承認と利用許可を別に確認しないまま、非選択候補を `historical` にする (`326–333`)。独立年次と回答したときに双方の期間を保持する表現が不足している。

`decided_by: local-ui-human` は POST/CLI が渡す文字列であり、hash やその文字列だけが Human origin を証明するわけではない。既存の localhost UI 認可／CSRF (`server 2675–2682`) は残すべき保護で、今回それを突破したとは主張しない。保存権限と監査可能な Human 操作経路の限定は authority 契約の対象。

### C. 見た候補と承認した候補が同じ revision とは限らない — F19

server form `690–695` の hidden 値は CSRF と group ID。POST `2700–2717` は group ID と selected path だけを resolver CLI に渡す。resolver `670–687` はその時点の共有 review graph を再読込し、その graph の candidate/selected hash を保存する。**表示 G0 → 同じ group/path の内容変更または候補追加を含む G1 に共有 graph 更新 → 古い画面から POST** では、G1 の hash を Human が見たものとして記録できる構造。現在の graph 自己 hash 一致では表示 G0 との違いを検出できない。

decision 保存は `load_decisions` → dict 変更 → atomic replace (`682–695`)。atomic replace (`71–84`) は torn write を抑えるが read-modify-write の CAS / revision / lease ではない。同じ初期 store を二 writer が読むと、別 group の一方の決定を失う候補がある。同じ group の二重送信／異なる選択の順序も明示されない。

server `2694` の building 判定と、保存後 `2729` の thread 起動は一体のトランザクションではない。`build_worker 770–784` は lock が取れなければ返るが、POST は「再構築を開始しました」と返し得る。bootstrap の build lease (`159–197,3653`) と CONFIG CAS (`262–276,3874–3877`) は重要な既存保護だが、decision store の revision を対象にしていない。queue 受理／build 開始／revision 公開完了を同じ成功表示にしない。

### D. F05b snapshot は守られるが、最新承認の公開保証ではない

bootstrap `471–498` は strict-validated bytes の exclusive generation-local snapshot を作る。`3755–3769` の graph build/attest、`3778,3808–3817` の Reader と model-ready 再走行、`3820–3829` の projector は同じ snapshot descriptor を使う。`690–743` は CONFIG 登録 hash を先に照合し、保存 generation の snapshot で契約を再構築する。これは受理済み F05b の保証として保持する。

しかし review graph は `3771–3774` で Reader／index 成功より先に共有 path へ公開され、active CONFIG の index 切替は `3863–3879`。build 失敗で新 generation が片付けられても (`4308–4315`)、共有 review がどの公開世代に対応するかを UI が確定しない。保存 D0 で build 中に shared decision D1 となっても、snapshot の D0 不変性は D1 公開完了を意味しない。

`f05b-task-contract.v1.md` は Human authenticity、publication revision/leases、cross-key classification、whole-tree freshness を明示的に未解決としている。これらを今回「F05 が無効」とは扱わない。旧 generation D0 の正しい登録を D1 と比較して破壊するのではなく、現 authority revision への**現在利用資格**を別ゲートで判定する。

### E. 通常回答には現 Human authority／原本候補集合の入口検査がない — F13

server `answer_query 2359–2373` は CONFIG の index path を読み、Ollama 起動後に通常 answer CLI を呼ぶ。そこまでに Reader contract status、Human approval revision、現原本 hash、新候補 inventory の確認はない。`/ask 2735–2753` は ready / ready_with_limits phase を確認するが、approval の有効性ではない。`_begin_active_work 393–399` は shutdown 管理用 counter で、decision/buildとの相互排他ではない。

answer v2 `1508–1544` は `--index` を受け、SQLite graph contract を検証してから planning/retrieval へ進む。base `validate_answer_graph_contract 176–522` と `load_answer_evidence_records 525–554` は SQLite 内の record/hash/partition/eligible set を検証する。**現在の source root の編集・削除・新しい sibling candidate を発見する inventory 更新とは別**。この範囲に現在の Human authority を trusted input として受け取る API もない。

これは「無検証で回答」ではない。内部 graph の不整合や unsafe index を拒否する既存保護はあり、v2 `1486–1494,1799–1800` は cache を fail-closed 無効化している。問題を stale cache の再利用と混同しない。新規候補は index 内に存在しないので、索引済み source hash だけの照合でも検出できない。

### F. 旧資料への fallback を明示的に止める層が必要

server `510–525` は Reader migration 時にも「再構築までは現在の索引で回答」と表示し、`710,731–733` は ready phase と index 存在で ask を出す。これは現在の既存互換方針だが、今回の「dated approval pending なら旧版で代用しない」要件にそのまま転用できない。

semantic promotion の CONFIG lease (`2120–2137`) は graph answer の swap を保護するが、失敗時は既に監査した legacy answer を保持する (`2142–2200`)。**legacy 検索経路であること自体が旧版を意味するわけではない**。ただし authority gate を promotion 側だけへ置くと、同じ未承認／失効 authority の legacy answer へ戻る余地が残るため、通常・promotion 両方より前と回答公開時に共通 gate が必要。

bootstrap の build 失敗後は phase error となり、通常 `/ask` は保留する (`4308–4315`, server `2735–2740`)。したがって「build が失敗すれば常に UI が旧版回答する」とは断定しない。直接 answer entry、decision 保存から phase 更新まで、別 revision の ready、回答実行中の撤回／新候補という未保証境界を分けて回帰化する。

## 最小の sequenced regression 提案 — 未作成・未実行

まず exact API・状態遷移を親が契約化する。下表は必要な意味条件の提案であり、原規範にない field 名や型制約を今回勝手に追加したものではない。

| 順 | 小型 gold / entry | 固定すべき期待と反例 |
| --- | --- | --- |
| 1 | 既存 pure 5 methods を保持＋日付解釈の小型対照 | compact/Japanese の group 化後も自動承認なし。無効 calendar／数字の識別番号／無関係業務を誤結合しない。別年の独立実績と改訂手順は別 gold。追加日時／mtime は「現行」の根拠にしない。 |
| 2 | 一 family、三段階の actual UI action / resolver persistence | 一問ずつ「同じ業務の版か」→「今の適用先」→「取込・回答利用を許可するか」。保留・拒否で Reader/embedding/answer 到達 0。独立年次は双方保持し、automatic resolved も訂正・撤回可。成功表示は保存した判断と一致。 |
| 3 | stale form の deterministic G0/G1 比較 | selected path を同じに保ち、G0 表示後に非選択候補追加／選択 bytes 変更。古い expected revision は拒否し store/CONFIG/旧世代不変。未変更 G0 正例も必須。API は表示した complete candidate set と source bytes の識別を渡す。 |
| 4 | 二 writer の bounded interleave / duplicate POST | 同 group 競合、別 group lost update、同 revision 二重送信。受理 revision が一意であること、他の決定を失わないこと、replay の扱い、busy/queued/published の正確な表示。単なる二 build 排他の成功で代替しない。 |
| 5 | actual bootstrap D0 capture → D1 decision → publish境界 | D0 内は同 snapshot、D1 受理を D0 公開完了と誤表示しない。D1 が必要なら停止／保留／明示再queue。model-ready 再走行と projector に同 authority、旧 generation の byte-map は不変。 |
| 6 | approval-use gate の Reader→index 回帰 | 関係だけ承認、現行だけ選択、利用拒否、完全承認の小型組合せ。許可外 text の取込／index／retrieval packet 0。完全承認のみ指定 content/hash/set/revision が通る。未分類対象を `version_ungrouped` で迂回させない。 |
| 7 | build 後の edit/delete/new peer → actual answer entry | tiny source 編集、削除、別名等の新候補と unchanged 正例。失効／不明はモデル／embedding query前に止め、index と旧 generation を修復しない。新候補検査は既存 indexed paths の hash照合だけにしない。 |
| 8 | answer開始後の revoke/revision切替 → 最終表示 | 有効時点で開始後、返却前に approval 失効。通常 answer、promotion失敗、ready旧index、失敗buildの各経路で旧資料を代用せず、理由付き保留。cache無効controlは維持し、時点付き履歴回答を自動許可しない。 |

1→2で Human authority の意味を固定、3→5で保存・公開の一貫性、6→8で内容利用の端から端を確認する。必要 fixtures は literal tiny CSV/path records、モデル／HTTP／OS subprocess は既存 reviewed in-process mocks、30 s / 1 MiB log と owned tmp。型/API誤接続を semantic RED と呼ばず、root が全 source と guard を読んだ後にだけ実行する。巨大 corpus／全 race の保証は対象外。

### 実装前に契約で明示したい最小事項

- 「取込を許可」の前に candidate 発見用の列挙／hash 読取／比較表示まで許可されるかと、内容抽出／embedding／回答利用の境界。現 build は approval 前に Path Graph を作るため、これを曖昧にしない。
- relation と applicable-now と use permission は一つの authoritative record に結合しつつ別判断として保持する。質問は一つずつ。独立年次・不明・拒否・撤回を一 active ファイルに無理に圧縮しない。具体的な schema/API 名は次契約で固定。
- candidate-set の完全性をどの scope/root identity と候補規則で評価するか。改名・移動・拡張子変更・新規追加を見落としたまま承認済みとしない。unknown/上限/読取不能は未確認と区別する。全 directory 監視を無断導入する提案ではない。
- D0 artifact の正当性と、D1変更後の現在利用不可を両立させる。承認時／build公開時／回答開始時／回答公開時の revision の意味と、失効後の fallback 禁止を共通仕様にする。過去時点の質問を許す場合は別の明示的モードで、今回の保留を回避させない。

## 固定 source hash

workspace root は `/Users/takashifukutomi/Documents/ChatGPT/AIエンジニアリングチャレンジ`。以下 `runs/` は `design/local-memory-v1-hardening/runs/`。実ファイルの SHA-256 を今回 read-only で確認。製品変更の許可・全 source freeze 検証ではない。

| source | SHA-256 |
| --- | --- |
| `runs/dated-hitl-scope.v1.md` | `67a6dd83073af3f6fc58e17e657504bacd0059d1aa2f1d7909eec870c1826a49` |
| `tests/test_dated_document_human_gate.py` | `ee62aaf42632bca032c80a40a54893a817cd78f72c5d95e1c1078ba96a69586a` |
| `runs/dated-hitl-red-001/result.json` | `a8d05723d302d21b19c6008f73848bf4a50ce141794b447a20ee05bbc52ac252` |
| `runs/dated-hitl-red-001/unittest.log` | `52c1111409180893248821190933a970c3bb3a7342e803f483d00ab76bc792ee` |
| `distribution/macos-local-memory/app/local_memory_server.py` | `3acb859916ccb9fb1a1c26cc0ebfa511bea2d8114fa83103ace40183fa899ee6` |
| `distribution/macos-local-memory/engine/document_version_resolver.py` | `14ecce2c01d68bfbfaa27d3022bfc6076a97465a331eb03443af0e6d50cc006f` |
| `distribution/macos-local-memory/app/bootstrap.py` | `e6248aae9ffa89e3cd6a43839af7f41e4d5941482f8f00a48f466de5524b526b` |
| `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py` | `5d2883e2a053776935d71b2486180b177078fe71b5d7a0a02bf8741e8e041c0a` |
| `distribution/macos-local-memory/engine/build_local_semantic_index.py` | `c70f36d98012cca29877af72e4d345c30a555e1fef24e34ebc057b4f823e7229` |
| `distribution/macos-local-memory/engine/answer_local_memory_v2.py` | `8677d5d49c37e081ad2c2ab06bd9c4734280ef5d3e4b2941a1fe570061388e60` |
| `distribution/macos-local-memory/engine/answer_local_memory.py` | `33f2b25e9d434e00b216be162d3e60dd8d322409cb108d07e26e455f4a1f34b2` |
| `runs/f05b-task-contract.v1.md` | `e136a468b3ae2a16f094b3189788f7e86a4c5bd089458a27f7d963193af8fd1b` |
| `design/local-memory-search-v1-00-hardening-plan-2026-09-09.md` | `e49801e3af62fb0a4e8f73a1379d105c2ff5b62946606d69ddf2522f4935f456` |

本書は追加製品編集の解除でも正式 PASS でもない。初期 RED と未解決を保持し、限定契約・gold・runner review の再開点とする。
