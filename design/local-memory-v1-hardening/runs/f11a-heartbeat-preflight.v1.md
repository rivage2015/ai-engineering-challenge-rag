# F11a fresh heartbeat investigation preflight

2026-09-09 12:51 JST。製品変更・F11a受理前。F05b限定受理を継続点にした読み取り再開。

RESUME/checkpoint/凍結計画を読み、Git status/diff statを確認。保護3file・計画・F05b受理receiptのhash不変。全旧runのstarted.jsonに対応するresult.jsonあり。live agentsは前担当全員completed、新規にf05a_executorへ静的F11a API/依存調査だけを割当てた。OS全processのpsはoperation not permittedで未確認。追加権限を求めず、この制約を保持。pmsetではPID17714の3 sleep prevention assertions有効、12:49時点で10217秒残存（15:39頃まで）。

再現対象は既存f11a-binding-observe.v1.py全2methodsと固定f11-next-fixture.v1.ipynbのみ。rootはobserver/runner/fixture、F04guard、supervisor全文、Probe初期化/dispatch/notebook本文/write、2Validatorの対象経路を読取。Notebookは4cells・6text Evidence、画像/添付なし。保存code内のraise文字列は読むだけ。direct Probeのvisual_observation_mode=suppressed、diagnostic=False。旧loop同様、既存Python3.9/jsonschemaを使用。

新exclusive run ID f11a-binding-observation-002で、既存runnerの許可済みIDを使う。single child -I -B -u、30秒wall/1MiB log。F04guardがnetwork/process/範囲外openとowned tmp外writeを拒否。固定入力は16KiB以内の1fileを2casesへcopyするだけ、意味結果は1document/6evidence/6relation。元sourceとfixture不変をcleanupで照合。モデル/GUI/原本/公開CONFIG/index/Keychain/新依存なし。

期待する観測は、状態metadata欠落と偽candidate notebook_stateの両方が現行2Validator（stream schema on/offを含む計3呼出し）を通ること。成功しても新fieldは未実装であり、製品の改善やapp回答への侵入の証明に数えない。現在の不足を再確認する調査で、正式受入用の真REDは別契約後に固定する。旧証跡は変更しない。追加のfixture/gold/実装はstatic preflightの未決事項を整理してから。
