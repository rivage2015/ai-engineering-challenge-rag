# V1.00 改善ループ進捗

2026-09-10 19:12追記: Chromeの質問POSTが`403 {"status":"forbidden"}`になる実際のUI不具合を確認。当初の「単なる古い画面」説明を撤回し、Chromeの送信headerを一時loopback serverで検査。same-origin HTML formのFetch Metadataは正常で、現serverから取得したCSRF tokenによる状態変更なしprobeは403を通過。Chromeのno-store/back-forward cache等で復元された非空tokenのみに対し、Host、exact Origin、Referer、Sec-Fetch-Site/Mode/Dest、Content-Typeが全て同一loopback HTML formと一致する場合だけ受理する例外を実装。外部Origin、tokenなし、不完全contextは403のまま。指定質問「分身ロボットカフェの受付でお客様に一番最初にすべきお声がけは何ですか？」を実local POSTしHTTP 200。検索は候補PDFへ到達したが直接Evidence不足のため`insufficient`/「わかりません」を独立監査`verified`で返した。加えて起動後の`__pycache__`が.app署名を無効化する包装不具合を発見し、launcher全体に`PYTHONDONTWRITEBYTECODE=1`を追加。再build/install/launch後にhealth ready、source/bundle 2file hash一致、codesign合格、app内`__pycache__`0、DMG/ZIP checksum合格を確認。93件=92 PASS+1明示SKIP、0 failure/error。最新DMG SHA256 `049bfecba0747bdfb06f9991dca518e2ec69e3bc6a4fbe65d47c8ed4d7886e87`、ZIP `610bace787c918aa13575e35913a28264631a29e5f888bda7eeec90909bf385a`。旧appは`/Applications/Local Memory Search 1.0 build 7 pre-CSRF-cache-fix backup 2026-09-10.app`と`... pre-bytecode-fix ...`に保持。一時診断serverは終了。commit/push未実施。

2026-09-10 18:38追記: ユーザーの明示操作により、検索対象`/Users/takashifukutomi/Desktop/オリィ研究所`の初回セットアップを実行。実データのmacOS NFDパスで、Reader入力のNFC投影とmanifestの生文字列を比較するとカバレッジ検査が誤って失敗する不具合をbuilderと1箇所、validatorと2箇所に分けて検出。各未完了generationは公開されず、NFC境界と衝突拒否を局所修正。新規Unicode 6件とpackage/versioned関連を含む93件は92 PASS + 1明示SKIP、0 failure/error。最終世代`generation-15b7573442f24278b80f80c66582c918`を公開し、SQLite `integrity_check=ok`、Evidence 27,722、Graph Node 27,844、Edge 2,907。UIで質問欄と回答ボタンを実機確認。状態は`ready_with_limits`で、部分読取107、抽出後空1、完全失敗0、未対応7、policy除外11、版HITL待ち0。任意のcross-document semantic graph shadowは未抽出1文書で`held`となったが、仕様どおり公開済みsafe-answer indexと従来検索経路は停止していない。最新DMG SHA256 `15eabd8ec5cb320108bc76f0ecfdd15a9fd3e17573681597976e9c754b1715de`、ZIP `e0fc490514159bddcd670c48ed989ae65ca3b343a6f04acdb3affa12d857e3a9`。commit/push未実施。

2026-09-09 22:47追記: Human承認後、配布表記をapp `1.0` / build `7`へ更新。build script、README、START-HERE、導入ガイド、package testの版表記だけを限定変更し、package+versioned87=86PASS+1SKIPを再確認。正式`deliverables/Local-Memory-Search-v1.0-macOS-unsigned.{dmg,zip,sha256.txt}`を生成し、DMG/ZIP/checksum、ad-hoc codesign、同梱3主要code hash、生成データ非混入を確認。旧serverはhealth/identity/UID/process/script/token一致後に認証shutdownで正常終了。旧appは削除せず`/Applications/Local Memory Search 0.6 build 6 backup 2026-09-09.app`へ退避し、新appを`/Applications/Local Memory Search.app`へ配置。installed plist 1.0/7、source hash一致、server health/build/instance/processはready。実UIは初回セットアップボタンを表示し、質問フォームはまだ非表示。実CONFIGにactive_generationがなく既存indexもreadyでないためで、実資料取込を無断実行していない。初回セットアップには検索対象の実資料索引化が伴うためHuman操作待ち。commit/push未実施。

2026-09-09 22:08追記: 既存deliverablesを上書きしない`/private/tmp/lms-package-check.scASuG`隔離コピーで配布buildを実行。sandbox内初回は`hdiutil: 装置が構成されていません`で失敗、許可済み外部実行でDMG/ZIP生成、DMG verify、ZIP test、ad-hoc codesign verify、checksum再照合に成功。最初のchecksum再照合は作業directory誤りでfile not found、成果物directoryからの訂正再実行でDMG/ZIPともOK。stage/ZIP内のbootstrap/server/resolver hashは現sourceと一致し、`.sqlite3/.jsonl/.log`混入なし。既存repo deliverablesとApplicationsのアプリは変更していない。生成物のInfo.plistはまだ0.6 build6であり、V1.00として置換する前に保護対象build scriptのversion表記を1.0へ上げるかHuman判断が必要。commit/push未実施。

2026-09-09 22:00追記: dated HITLを実localhost HTTPまで通し、最初の4件PASS後に別context敵対監査を実施。監査で、質問前後がどちらも`current=True`でもG0/D0→G1/D1へ世代交替すると旧G0回答を表示できる競合を発見。修正前HTTPで200漏出を再現し、開始時のgeneration/generation path/decision snapshot/CONFIG全体を回答生成へ束縛、終了時identity相違を409で非表示に修正。追加で、HTTP正例が`validate_source=True`を必ず要求、実Path Graph合成環境で表示後の2025資料変更を`review_source_changed`として拒否、invalid/unknown date-like候補もtrusted ticketなら完全な同意フォームへ送ることを確認。限定15件PASS、package+versioned87件=86PASS+1SKIP、失敗0。途中の監査前HTTP競合testは実`home()`を1回通りローカル設定/Ollama状態の読取診断に触れた可能性があるためscope逸脱として保持し、その後全負例をsmall_homeへ隔離。モデル推論・実資料取込・書換えはしていない。artifact前後hashは残存変更を検出するがABA完全保証ではない。アプリ更新/commit/push未実施。

2026-09-09 21:27追記: 上のpackage失敗を切り分け。macOSの`/var`が`/private/var`へのsymlinkであるため、安全なPath builderが合成fixtureの未解決rootを拒否していた。安全条件は緩めず、対象testの一時rootだけ実パス化。過去に追加したsnapshot-target保護に対する古い文字列期待も現行の安全条件へ同期。package69=68PASS+1SKIP、resolver20PASS、versioned E2E17PASS、新HITL13PASS、失敗0。実resolverによるUI ticket→prepare→CAS保存と新資料の選択/現行確認/利用承認を追加検証。独立年次と判断保留ボタンもUIに追加し、その場合は再buildしない。未検証は実HTTPソケット/実GUI、ビルド済み.app、実資料、別context監査。アプリ更新/commit/push未実施。

2026-09-09 21:17追記: dated HITLのUI確認票、active generation/Path Graph/inventory/storeへのハッシュ結合、専用排他lease下CAS保存、判断更新後の旧索引回答停止、質問開始時/回答表示直前の二重revision確認を実装。合成の新規12件、既存resolver20件、表示1件は全PASS。構文/diff-check PASS。全package69件は64成功・1skip・3error・1fail。うち3errorは既存Path builder起動失敗、1failは既存cleanup条件に対する古い文字列テスト期待で、今回の新規集中テストは通過。ただし全回帰GREENでないためアプリ更新に進まず、原因切り分けと追加実経路E2Eが次。commit/push未実施。

2026-09-09 20:48追記: 日付付き資料の新承認ゲート第1スライスをresolver 0.1.6に実装。旧選択、不完全/変更済み/否認/独立/保留記録は全て選択不可、同一業務・現行性・取込/索引/回答利用の明示承認と一式hashが一致する場合だけ選択。固定合成テストはseed 10/10、post 17/17 PASS、skip/error 0。過去suiteは初期5/5 PASS、temporalはversion文字1件、yearは旧承認を正当とする古い期待2件が失敗し、意図した契約更新として保存。UI/CAS/回答直前の再確認は未実装。アプリ更新・commit・pushはまだ行わない。

2026-09-09 20:20追記: 日付候補31試験PASSの引継ぎ確認、正式受理はまだ。旧承認が新しい利用確認を迂回する残余1件を次の必須修正へ。別担当は新承認契約/gold準備と日付差分review中。root追加実経路2試験PASS: 非ZIP Office失敗をDocumentに記録しEvidenceを出さず、次の正常text処理は成功。原本/公開索引不変、アプリ更新/commit/push未実施。

2026-09-09 18:40の中間記録。改善ループは継続。版1.00のリリース宣言ではない。

## 日付違いの業務資料の確認漏れを再現

ユーザー最新指示を最優先に反映。新しい合成5試験で、年月日違いを同一候補にまとめられない2ケースと、「現行」ラベルで人の承認を飛ばす1ケースがFAIL。既存正常2ケースPASS、error/skip0。製品修正前の再現結果を保存。別担当2名が日付候補の限定設計と承認から回答までの不足経路を調査中。F11a正式監査は未完了のまま保留し、元証跡は保持。

### 17:43時点の記録

## 新版正常処理と旧版拒否を確認

追加2試験PASS・skip0。新版Search0.7では元の関係/ID/順序検証を最後まで実行、旧0.6は正確な理由で拒否。製品gateと元testを変更せず、旧失敗はそのまま保持。別担当が新試験を静的点検中、実装担当が正式監査向け証跡generatorを準備中。正式監査・全lineage suite・全V1の合格ではない。再開場所と担当をcheckpoint/RESUMEへ保存。

### 17:09時点の記録

## 関連回帰9件PASS・1件の来歴エラーを確認

新たな10件で、繰返し検証/索引移行/安全除外等9件はPASS、旧Search0.6.0のliteral fixtureを使う1件は新版pin検証でERROR。skip0。契約とfixtureの整合を別担当で診断中。正式監査は保留し、失敗を保持。SmartArt全経路は試験サイズ条件が未確認で未実行。

### 16:36時点の記録

## 修正後の回帰40件を確認

親が現製品でmetadata受入30＋app4＋追加画像境界6の全40件PASS/skip0を確認。残課題witness3は別枠で確認し、解決件数へ加算しない。57fileのpacket hash一致、既存画像6の保存ログも確認。製品freezeを維持して、関連回帰preflightと正式監査用packetを別担当で準備中。正式監査の合格ではない。

### 16:04時点の記録

## 画像誤分類の限定修正へ

修復設計を親が全文確認し、6製品の画像分類/実親lookup/streaming/集計修正を単独担当へ許可。別担当は追加反証goldを準備中。既存画像失敗を含むgoldは変更しない。修正後のGREENはまだ未確認。既存metadata30受入＋3残余の新ID runnerを準備し、source freeze待ち。スリープ防止は旧期限終了を確認後、16:02から一時12時間再開した。

### 15:32時点の記録

## 索引との新版互換を修正

managed0.12.0を索引側のnative/SmartArt/支持検証が認識するようexact3箇所を追加。旧版・未知版拒否・根拠照合は保持。新9件と元アプリ4件が全PASS/skip0となり、rootも差分・ログを確認した。画像VLM textの分類回帰は未修正で、合成3methodsの失敗を保存し、親参照とstreamingを含む修正設計中。正式監査・V1完成ではない。

### 14:54時点の記録

## 追加回帰で連携エラーを発見

親の固定境界4件はGREEN。アプリ4件は1PASS/3ERROR、画像3件は2PASS/1ERROR（skip0）。非Notebook初期索引の構造関係照合と、Notebook画像Evidenceのmetadata判定を別担当で読み取り診断中。ログを全保存し、正式受理を保留した。10製品freeze/逆差分/元gold不変と保護3fileを再照合済み。実資料への適用はしていない。

### 14:20時点の記録

## F11aのコード修正に着手

33method gold（受入30＋残余3）・元AST不変・10before bytes/hashを親が確認し、単独Executorに限定10製品fileの実装を許可。14:20にProbeの新しいコード差分を確認した。修正途中であり、GREENや正式製品監査はまだ。

rootはアプリ停止/旧generation保護/明示再build/配布copy-listの4controlと既存画像mock3のrunnerを用意。別静的reviewで、初版の失敗generationが残るという期待を既存cleanupに合わせて事前訂正し、元goldと訂正理由を保存。訂正版reviewに追加blockerなし、未実行。実装freeze後に実行する。原本/公開索引は不変、全F11/V1は未完了。

### 13:35時点の記録

## F11a契約を固定し、修正前の失敗を保存

13:39追記: 設計gateは補足込みで解消（製品PASSではない）。実装担当側の初期9方法も8semanticFAIL＋非Notebook1PASS、エラー/skip0で再現。rootが実ログを全確認した。現在は残りの上限・公開API・移行等のgold追加準備中。コード編集は全gold固定後であり、今回はまだF11a製品修正済みとは言わない。

metadata-onlyの原本照合、明示UNVERIFIED、事前parser上限を契約＋補足に固定。root新規境界4methodsは10assertion失敗・エラー/skip0で真REDとなった。各正常controlが先に通っており、新API不存在ではなく既存検証の見逃しとSearch metadata欠落を実証した。`runs/f11a-root-red-assessment.v1.md` に保存。

単独Executorが10製品のbefore bytesを保存し、親も全10一致確認。現在は専用gold/runner準備中で、製品編集許可はまだ。別担当が補足込み契約gateを点検中。次は凍結goldとrunnerを読み、限定修正→同じテスト→別監査へ進める。本文/全membership/鮮度/F11b回答伝播は未完了。

### 13:03時点の記録

## F11aの不足を再確認、検証契約を事前監査中

固定合成Notebookによる2観測method/6validator呼出しで、metadata欠落・偽の候補fieldを検出しない現状を再確認。`runs/f11a-fresh-observation-result.v1.json` に実ログと現在のsource hashを保存した。観測成功は修正完了ではなく、既存にない候補fieldの観測をアプリ回答への侵入実証とは呼ばない。この回の製品変更はまだない。

実装担当・別監査担当は静的preflightのみ。metadataの原本照合を、本文や全Evidenceの完全性保証と混同しない契約、原本なし/partial時のUNVERIFIED非0、SearchUnit照合範囲、同一読取snapshot、旧世代と配布を確認中。契約・gold・before hash・所有権を固定してからREDと実装へ進む。再開入口を更新し、既存210source hashは不変確認済み。全形式/全V1は未完了。

### 12:27時点の受理済み記録

## F05b限定範囲を受理

判断記録を世代ごとの固定snapshotにして後続処理へ渡し、全inventoryから読むべき資料・順序・件数を再構築して照合する修正を実装・別担当監査・親検証まで完了。独立18runs/205methods=202受入/制御/回帰＋3残課題witness、失敗/skip0。20件は独立追加の攻撃・境界試験。全V1/全形式の完成ではない。

受理記録 `runs/f05b-validation-result.v1.json`（37576299...）。親は正式Draft202012/Ruby・210source・107manifest・13逆差分・30過去attempt・18監査log・72補助artifact・元gold不変を確認。最終親validatorは固定AST比較のPython版差を解消したv2で通過し、初回失敗も記録に保持した。formal product repair0。次は保存済みF11a提案のfresh preflightから、再び限定契約→RED→修正→別監査へ進む。原本/本番索引/commit/pushは変更なし。

### 12:19時点の記録（以下は歴史的状態）

独立再実行は18runs/205methods（202受入・制御・回帰＋3残課題witness）、失敗・skip0まで完了。追加13＋補助7の攻撃/境界試験を含む。親も全18result/logのhash・上限・終端件数・exit codeを照合した。旧0.1登録は実際の旧serializer/statusでcurrentを確認後、現行側migration/no-repairを確認した限定fixtureであり、全旧app再現ではない。正式report/evidenceの提出・親の形式検証はまだで、受理前。

## 現在: F05b実装・回帰を終え、正式独立監査へ

固定した選択記録snapshotをReader/Validator/索引/世代登録へ渡し、全inventoryから読むべき資料・順序・件数を再構築する修正を実装。root初期6見逃しは同じ元テストで6GREEN、追加16controlsも予算付きでGREEN。関連版判定・lineage・移行等も再実行済み。Excel安全性は最初1SKIPだったため既存対応環境で再実行して1PASS。試験中の失敗・fixture訂正・skipはすべて履歴に保持。

artifact v1（721db8b3...、210sources）をfreezeして別担当へ正式監査を依頼した。独立holdout13は準備・root読取点検済みだが未実行、runner点検待ち。製品受理はまだ。保存結果/再開入口に継続点を記録し、原本/本番索引/commit/pushは触れていない。

### 11:34時点の記録（以下は歴史的状態）

## 現在: F05bの見逃し6件を実証、実装へ

F05b契約・API・所有者を固定し、新app6methodで6FAIL/0ERRORの真REDを記録。固定snapshotなし、Reader/Validator/projectorのauthority省略受理、初期検証後の偽active受理、整合的なContact欠落受理を再現した。`runs/f05b-root-red-assessment.v1.md` に実ログと範囲を保存。製品修正前に正解を固定しており、API未実装のTypeErrorを問題の実証にしていない。

単独Executorが27pure test/goldと5before bytesを保存し、5製品の実装へ着手。rootは追加app13controlsを固定済み（未実行）。固定選択snapshotを全工程へ渡し、全inventoryから読むべき資料一覧を再構築する設計。別担当のテスト事前点検から、登録時の検証省略や自己整合な保存記録偽装も追加した。元テストを変えず履歴・ASTを保存。まだGREEN/正式監査前、F05b未完了。20分報告と再開記録を継続。

### F05aの受理済み範囲

版graphの自己hashだけではなく、明示inventoryと選択記録から候補・判定・Node/Edge・件数を再構築して照合する修正を実装。アプリ直後の検証へ選択入力を渡し、旧実装で通った不正4件をReader前に拒否する試験が通過。別context監査と親の最終検証を経て、11:02にF05a限定範囲を受理した。後続consumerと保存世代の固定選択snapshotは未実装であり、F05全体の完了ではない。

受理記録は `runs/f05a-validation-result.v1.json`（66225c3e...）。正式report v2（4863dcd2...）/evidence v2（57b7aef7...）に対し、親が正式schema/Ruby・115sources・全参照・6逆差分・24result/log・37追加監査artifact・executor manifest37項目を確認。独立111実行＝108受入/制御/回帰＋3残存witness、skip/expected failure0。元gold23件不変＋追加2件をASTでも照合。全失敗履歴と、監査証跡のクラス名誤記を作者が訂正したv1/v2、親の相対pathによる補助再現エラーも保持。formal repair0。固定artifactは歴史的snapshotとして保持し、次の別契約用にsource freezeを解除できる。

F05bの事前調査・契約補足・具体API案も保存済み。固定decisionの伝播に加え、「保留が混じっていない」だけでなく「読むべき資料が全て揃っている」ことを全inventoryから照合する条件を追加検討。引数省略でappの固定snapshot検証を迂回しない設計。次に製品用容量上限と最終契約/真REDを固定する。まだ提案で、製品変更・試験は未実施。

## 局所修正と別担当監査を完了

- F08/F09: XMLの子要素後の文・空白を保存し、JSON同一object内の重複keyを黙って上書きせず失敗にする。監査で出たUTF-8/16の文字境界誤拒否も修正。常設17件と独立反例6件が合格。XML locator変更後の派生成果物は再構築が必要。
- F15（引用の区切りのみ）: 資料本文・ファイル名・位置・補足のタグ文字を、回答と監査の4つの入力箇所でescapeする。原文とEvidence IDは変更しない。常設6件、関連26件、独立2件合格。モデルが資料中の命令を意味的に無視する万能保証ではない。
- H0試験監督: 時間・ログ量上限、再利用しないrun ID、中断記録、SIGTERM/SIGHUP時の自分の子process group回収、終端unittest結果のみの判定を実装。11件の独立再実行合格。スキップ・期待失敗・0件・FAILEDをplain PASSにしない。OS隔離やSIGKILL時の回収は保証外。
- F01/F07: 版graphの明示伝播と検証済みbindingの引渡し、既存mockの互換修正まで局所監査v3 PASS。独立52件、skip 0。親担当も正式schema・artifact hash・全8source hash・参照を確認。実モデルや版選択の意味はこの合格に含めない。
- F16 Path builder: FD/no-follow読取と、検出した祖先の一時入替直後の全hash無効化まで局所監査v2 PASS。実inodeの入替・復旧を使うREDから修正し、独立19件をPython3.14/3.9それぞれで合格。v2内のpatch_snapshotは旧v1の履歴であり、最終コードの代替ではないとvalidation-resultへ追記済み。
- F16 Path Validator: 明示root、現在の全path集合、nodeと実sourceの照合、Path codeの世代fingerprintまで局所監査v1 PASS。新18件を各Python3.14/3.9、builder19件、最新parserでの合成app15件とmigration/lineage15件、追加3件の独立試験が合格。package試験2本は呼出し互換差分の静的確認のみ。
- F10: XLSX fallbackの明示binding、URI偽装part/terminal dot変種拒否、全sheetData先行検証まで最終監査v3 PASS。独立47件、skip 0。2回の修正往復と全旧反例・失敗記録を保持。全OPC/cell機能/native openpyxl/モデル/配布版の認証ではない。
- Directory hash: rootをdepth0に分ける最小修正で子directoryを先に計算し、子file hashの変更をrootまで反映。局所監査v1 PASS。常設は厳密gold4件＋性質テスト2件、さらに独立Unicode分岐holdoutと全関係回帰が合格。既存graphや公開索引を自動で再構築していない。
- F04a: 「現行」候補より大きい単一版番号の候補がある矛盾を、人の確認へ保留する。局所監査v1 PASS。独立17 resolver/Reader＋15合成app＋7追加合格例、skip 0。別の1件は複数版tokenの残存問題を示す反例で、合格仕様には数えない。親もschema/hash/全11artifact source・追加13source・ログを確認。
- F18: 通常の再検証で保存済みlineageを消去・書換えせず、元資料から導いた内容とread-onlyで照合する。初回作成は明示指定で、既存fileを上書きしない。局所監査v1 PASS。独立68件（追加8件を含む）、skip 0。親も正式schema・全37source・13result/log・全参照を確認。無限の並行race／全fault／電源断、package/embedded全runtime等は未認証。
- F02a: 年号だけで最大年の資料を選び、他を旧版にする最後の自動分岐を停止。候補全てを理由付きで保留する。局所監査v1 PASS。独立63実行＝60受入・制御＋3残存witness、skip 0。親が全52source・3逆差分＋README・13log・追加18監査artifactを正式schemaとともに確認。正常回答、F04a、人の選択とstale検知を維持。全保留時に以前のCONFIG/indexを壊さないが、その旧資料の鮮度を保証するわけではない。

- F03a: 同じ既存family_keyに属する無印資料も候補に含め、印付き資料との混在時は自動決定せず人の確認へ保留する修正を局所受理。全無印・単独資料は従来どおり版groupなし。独立100method＝97受入・制御・関連回帰＋3残存witness、skip/expected failure0。親が正式schema/Ruby・全97source・23result/log・4逆差分＋README・executor manifest67項目を確認し、f03a-validation-result.v1.jsonに保存。2回のfixture名訂正と全RED/失敗記録を保持。formal repair0。F03aのfreezeは解除。

## 次の改善対象

- F05: 初期の偽graph3観測からF05aの実装とアプリ検証へ進んだ（上記）。F05bの後続再検証・全選別照合・保存decision snapshotが未実装。graphが示すdecisions_pathを新たな信頼元にしない。

- F02aの限定修正は受理済み。F02全体の独立年次資料の両件利用、current marker／複数signal、候補の同一性・完全性、HITL世代・競合は未完了。過去の失敗記録を残し、全保留を通常年次業務の完成とは呼ばない。
- F11は小型観測3件と契約案、追加のsource-binding観測2件（6validator呼出し）を保存した段階で、製品未変更。別contextの静的確認で、同一snapshotの上限付き解析、旧producerを名乗ってstateを省略する攻撃、配布・identity依存の契約追加が必要と確認。f11a-binding-review.v1.md参照。Notebookの保存出力に実行事実・未検証の鮮度を保持する設計で、コードと保存出力の違いだけで古いと断定しない。
- F02年次/改訂、F03候補統合、F04の複数signal、F05独立候補検証、F06/F19のHITLと世代・競合。F04aのsource freezeは解除。初期fixture/guard失敗やPython3.9の既存E2E非互換を保存し、最新の局所合格と混同しない。

## 未完了

年次記録と改訂版の区別、別family_key・改名・異形式間の候補漏れ、全無印資料の同一性とgroup解消時の扱い、版graphの独立再構築、HITLの競合、最新source再確認、Readerの残りの欠落、資源上限、実機モデル／GUI／配布／全形式の受入。計画F01〜F20とG1〜G9を満たすまでは完成扱いしない。

## 原本・再開・権限

原本資料、本番索引、Keychainを変更していない。既存のbuildと説明書3ファイルは開始時hashを保持。コミット・pushなし。変更はworking tree、監査と試験記録はこのdirectoryとartifacts/local-memory-v1-hardening/runsに保存。再開入口はRESUME.mdとcheckpoint.json。

同じタスクを20分ごとに再開確認するautomation v1-00がある。ユーザーの追加依頼で毎回短い稼働報告を行う設定に変更済み、10:40に実TOMLを再確認。F05a担当は終了し、F05bの最終契約から再開可能。重複workerは起動しない。Mac本体と画面のスリープ防止は09:46に実確認済み、一時的に2026-09-09 15:39頃まで（蓋を開けAC電源接続のままにする）。恒久設定は変更していない。
