# F05b decision snapshot propagation — read-only preflight

作成: 2026-09-09 09:36 JST（00:36 UTC）。担当: `/root/f02a_independent_audit`。状態: **proposal / not_implemented / not_a_formal_audit**。

本担当が新設したのはこのファイルだけ。製品・既存test・本番CONFIG/index・Keychain・原本・共有decision実データは変更も読取もしていない。ネットワーク、モデル、GUI、試験実行はない。F02a等の過去監査記録は不変。

## 1. 前提と開始条件

F05aの凍結契約 `runs/f05a-task-contract.v1.md`（SHA-256 `89960be4254752ba3ae387610359c3f6f8200cb57c3d7a9190398c723473e4df`）は、standalone resolver API/CLIとbootstrap直後のgateだけを対象にする。本メモはその残件である世代内decision snapshotと各consumerへの明示伝播を検討するもので、F05aの実装範囲を広げない。

**F05aの正式監査・親Validator受理が終わるまで、F05bの製品変更を開始しない。** 受理後に実際の新signature、before hash、担当ファイル、真RED、予算を新契約で固定する。本メモのAPI名・配置案は未実装であり、承認済みコードや確定受入仕様ではない。

00:36 UTCの観測ではresolverはまだF03a版 `c2b98254ec28a82e8cc7b5ef3f5e780739bcc1b6983ef2a9252609156d4b672f`、bootstrapはF05aの直後validate引数を加えた途中状態 `cad0bfee06fbdf0af0319d6fed2d467310ef697e848a9b6d170eacf061f31b55`。この組を実行可能なfreezeや受理済みsnapshotと扱わない。下記行番号もこの読取時点のlocatorであり、実装前に再確認する。

## 2. 現在の経路と不足

対象パスはrepository rootからの相対パス。

| 現在の入口 | 読取時点の実装 | F05bで必要な接続 |
|---|---|---|
| `app/bootstrap.py:34,3562,3658` | `SUPPORT/document-version-decisions.json`を共有。新しい`generation-<uuid>/01-path`を作り、resolver build/直後validateへ共有pathを渡す | 新世代の所有者が一度だけsnapshotを作り、両方へ同じ固定path/hashを渡す |
| `engine/document_version_resolver.py:386,441` | buildのsourceにdecision path/hash。F05aは明示入力から再構築する予定 | 受理済みF05aを再利用し、consumerが同じ読取snapshotの検証済み結果とbindingを受け取れる入口を用意する |
| `engine/build_adaptive_semantic_graph.py:192,328,355,467,496` | `apply_document_version_policy(selected, version_graph_path, expected_inventory_sha256=None)`、`build(..., version_graph_path=None)`。自己hashを確認したgraphのdispositionで選別。stateに版graph bindingのみ | explicit inventoryとsnapshot authorityを受け、F05再構築した同じ内容から選別。snapshot bindingをstateへ含める |
| `engine/validate_adaptive_semantic_graph.py:2558,2584,2636,3048,3077` | `validate(output, source_root, inventory, version_graph=None, *, initialize_lineage=False)`。graph自己hash・inventory hash・manifestへのheld混入を確認し、実際に確認した版bindingを返す | 毎回F05再構築を呼び、同一snapshot由来のheld集合とdecision bindingを確認・返却する |
| `engine/build_local_semantic_index.py:83,834,861,1240,1253,2451,2537` | lineage contextは無版3項目または版graph付き4項目だけ。Validatorへ明示pathを渡す | versioned contextとCLIへsnapshot path/hashの対を明示追加。lineage/securityの両context検査を更新する |
| `engine/build_local_semantic_index.py:963,1352,2010,2032,2064,2692` | Validatorが返したdetached版bindingを最後まで運び、producer stateを後から読み直して索引metadataにしない | 同じdetached bindingに確認済みsnapshotを含め、途中で落としたり未確認producer値で補ったりしない |
| `app/bootstrap.py:434,476,584,607` | Reader generation contractは固定`01-path/document-version-graph.json`を使い、code identityとartifact hashをCONFIG登録へ結ぶ | snapshotも固定位置の必須artifactとして登録・照合。旧世代へ共有decisionを後付けしない |
| `app/bootstrap.py:2165,2174,2180,3709,3718` | Reader、Validator、モデル導入後の別semantic再構築、index CLIの順 | 全経路に同じsnapshot descriptorを渡す。model-ready再実行でもcaptureし直さない |

表の`app/`と`engine/`は `distribution/macos-local-memory/` 配下。同じgraphを最初のgateだけで検証しても、後のReader/Validator/projectorは現在その完全再構築を呼ばない。この伝播を完了するまでF05全体の保証にしない。

## 3. 推奨する最小保存契約

新しいapp buildだけで、bootstrapをsnapshot生成の唯一の所有者にする。既存のbuild lease内で、新しい`01-path`が作られPath検証が成功した後、resolver buildより前にcaptureする。

```text
generation-<uuid>/
  01-path/
    path-source-inventory.jsonl
    document-version-decisions.snapshot.json   # 新設案、既存なら拒否
    document-version-graph.json
  02-semantic/                                # または02-semantic-model-ready
    adaptive-reader-state.json
    reader-generation-contract.json           # bootstrap.py:49の既存定数
  safe-answer-index.sqlite3
```

snapshot payloadは既存resolverが読むdecision JSON形式を維持する。共有ファイルが存在する場合は、上限内で一度取得したbytesを厳密解析し、そのbytesをそのまま保存する。wrapperを足したり一部groupだけ抜いたりしない。未使用に見える古いdecisionも、後続candidateとのstale判定に必要になり得るので勝手に消さない。

共有ファイルが初回未作成の場合だけ、固定の空payload `{"schema_version":"1.0","decisions":[]}` とLFを生成してsnapshotを実体化する。以後の段階は「必須snapshotが存在しない」を空decisionへ変換しない。元の共有ファイルの新設や変更はしない。capture時に共有入力が未作成だったことは、必要なら世代のcapture記録へ残すが、後からその共有pathを検証のために開かない。

新snapshotは排他的作成で既存file/directory/symlinkを拒否し、書込後に再利用用のpath・raw SHA-256・byte数を得る。固定コピーとは「このbuildが以後更新しない、検証時にhashで照合する」という契約であり、ownerが変更不能なOS属性や全filesystem race耐性の保証ではない。capture失敗はその未公開buildを停止し、既存CONFIG/indexを保持する。

## 4. API/CLIとhashの伝え方

提案は新しいkeyword対 `version_decisions_path` / `version_decisions_sha256` と、対応する `--version-decisions` / `--version-decisions-sha256`。具体名はF05a受理後のAPIに合わせる。pathのみ/hashのみ、null混在、余分なcontext keyは拒否する。bootstrapはcaptureで得たhashを使い、graphの自己申告hashから「期待hash」を取り出さない。

保存世代の再検証では、期待hashは既存CONFIGに結び付いて検証済みのReader generation contractから得る。検証対象のsnapshotを今読んで計算したhashを、そのまま外部の「期待hash」として渡すのは固定の証明にならない。新buildはcapture時のdescriptor、保存世代は登録済みdescriptorという入力元を区別する。

Readerの既存`selected`リストは形式選別後の部分集合なので、それだけでresolverの全candidate membershipを再構築してはいけない。`apply_document_version_policy()`には、builderが明示的に受け取った全inventoryの同一読取snapshotまたはその明示pathを追加で渡す。現在の任意`expected_inventory_sha256`だけでは全体再構築の入力にならない。

resolverのbuild/validateにはF05aの明示`decisions_path`としてsnapshotの固定pathを渡す。F05aがまだ外部期待digestや検証済みpayloadを返せない場合、F05bでその最小入口を追加する必要がある。

重要なのは、`validate(path)`がPASSした後でReaderが同じpathを別に読み直してdispositionを使わないこと。推奨する内部単位は、graph/inventory/decisionsの各bytesを一度取得し、その同じbytesから解析・hash照合・F05再構築を行い、**検証済みgraphまたは再構築済み選別結果＋detached binding**を返すもの。F05aの純粋再構築関数を使い、consumerごとに別の版規則を実装しない。外部期待snapshot hashも同じ読取bytesに対して照合する。単に別関数でhashを読んでから既存validateに再読させる方式は、その2読取間の差替えを残す。

既存 `document_version_graph` bindingの中に、例えば `decision_snapshot: {path, sha256}` を追加すると、F01/F07のdetached binding伝播を再利用しやすい。これは追加未実装案。Reader stateの自己申告値を返すのではなく、Validatorが実際に検証したcaller inputからこのfieldを組み立てる。projectorはその値をlineage→security→projection report→SQLite metadataまで保持する。未確認の余分なnested fieldは引き継がない。

`run_semantic_pipeline(source, paths, semantic, security, log)`には、capture済みdescriptorをkeywordで渡す案が明瞭。trusted `paths`から固定snapshot位置を確認し、渡されたhashで検証する。最初のReader、`--initialize-lineage`付き初回Validator、`02-semantic-model-ready`への再構築、索引projectionの全部が同じdescriptorを使う。

## 5. 正規位置と世代登録

appのsnapshot正規位置は常に `semantic.parent / "01-path" / "document-version-decisions.snapshot.json"`。現在の版graph検査 `bootstrap.py:478` と同じく、producer JSONが示したpathを先にresolve/stat/openしない。trusted generation rootと固定ファイル名から作ったpathだけを対象にし、producerのpath文字列は一致照合する。別世代、共有SUPPORT、`..`、絶対外部path、symlink、dangling symlink、directoryはartifactとして拒否する。symlinkをresolveしてから正常なfileに見せる処理は避ける。

汎用CLIではapp固定layoutを暗黙に要求せず、呼出し側が明示したinventory/graph/snapshotだけをauthorityとする。graph内の`source.decisions_path`を入力発見に使わないというF05a境界を維持する。graphとsnapshot双方を書き換え可能な主体に対し、自己hashだけで真正性を保証する設計にはしない。

Reader generation contractの `generation_artifacts` に固定snapshotのfile identityを追加する。既存CONFIGに登録されたcontract hash/logical hashとの比較に含まれるため、後のsnapshot変更・削除が `reader_migration_required` になる。`_reader_generation_contract_body()`の通常照合で新snapshotを作成・更新・修復しない。初回登録と保存世代の再検証で、同じ固定path規則を使う。

新contract/producer版を定め、code identityの変更とともに旧世代を識別する。F05b以前のversioned generationにsnapshotが無い場合、現在の共有decisionを読んで補完したり「同じ選択なのでOK」と扱ったりしない。移行理由と明示的な再buildの既存経路を使い、旧CONFIG/index自体を書き換えない。

## 6. 初回・変更・staleの期待

| 状況 | 提案する期待 |
|---|---|
| 共有decision未作成で新app build | 空JSON snapshotを排他的に作成。通常の自動選択・保留が成功する。共有ファイルは作らない |
| snapshot作成後に共有decisionが変更・削除・破損 | 現buildと保存世代の検証はsnapshotだけを使う。共有変更だけで結果やbindingが変化しない |
| 次の明示build | その時点の共有bytesを別generationへ再captureする。古いsnapshotは保持する |
| source内容・候補集合が変わった次のbuild | captureしたdecisionに対してF05a/F03aのfull-set stale判定を再現する。以前の選択を自動で有効化しない |
| snapshotの欠落・変更・別世代置換 | 明示input hash/graph binding/世代contractのいずれかで停止。共有decisionへfallbackしない |
| 共有decisionが初めから破損・曖昧duplicate・通常読取失敗 | 初回欠損と混同せず停止。空mapにしない |
| legacy無版Reader | 従来の無版contextを明示的に保持し、version/snapshot fieldが持ち込まれたら拒否する |
| standaloneの版graphだが明示decisionなし | F05aの空authority契約と全再構築を維持できる。新appのsnapshot必須経路と区別し、app consumerの入力欠落をこの経路で救済しない |

「snapshot後の共有変更だけでは変わらない」は、sourceや当該snapshot自体が変わらない条件での期待。原本変更をF18等が検出する失敗まで隠さない。

build開始後にUIで新しいdecisionが書かれた場合、snapshotはその前の判断を保持し得る。UIのbuilding表示だけはleaseによる直列化ではない。snapshotを一定にすることと、最新のHuman revisionを公開したことは別問題であり、後者はF06/F19のpublication revision/CAS契約で扱う。

## 7. 最小変更一覧とtest/mockへの影響

製品候補はresolverの検証済みsnapshot入口、Reader、Validator、index projector、bootstrapの5ファイル。既存READMEの説明更新と、下記の関係するtestだけを別契約で割り当てる。serverのUIフォームや共有decisionへの保存先をこの作業だけで変える必要はなく、その安全性が改善したとは主張しない。

| 既存test/caller | 確認した影響 |
|---|---|
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py:238` | `unpublished_reader()`がresolver/Reader/Validatorを直呼びし、snapshotなしでversioned generationを作る。新しい正常versioned fixtureを空snapshotで作り、全CLIへ明示伝播する。無版fixtureは別に維持する |
| 同`:319,350` | attestation後producer-state差替え試験をsnapshot bindingにも広げる。最終metadataは実際にValidatorが返したdetached値と一致すべきで、後読みしたspoofを通さない |
| 同`:353,361` | strict lineage context key/null検査と、`{"status":"PASS"}`だけのmockを拒否する試験を維持。snapshot付き正常mockは実bindingを返す。余分なkeyを許す変更で通さない |
| 同`:389`、`tests/test_unmarked_version_e2e.py:98,121` | 共有decisionへ正規Human choiceを記録して再buildする正常経路を残す。新buildは新snapshot、旧buildは旧snapshotというgoldを追加する |
| `distribution/macos-local-memory/tests/test_reader_generation_migration.py:81,111,116` | `_current_config()`がresolver.buildとrun_semantic_pipelineを直呼びする。新しいcurrent fixtureにsnapshot/明示引数/登録artifactが必要。旧config fixtureの不足を勝手に補完しない |
| `tests/test_immutable_lineage_validation.py:178,196` | appで作った保存世代をValidatorへ直接渡す2箇所に、その世代の固定snapshot authorityが必要。通常再検証前後の全hash不変・失敗時の既存lineage不変を維持する |
| `distribution/macos-local-memory/tests/test_document_version_resolver.py:351,393,431` | Reader/Validator直呼びのnormal・year hold・current conflict fixtureを確認する。APIの無版/版no-decision契約を明示し、関係のない理由goldを変更しない |
| `distribution/macos-local-memory/tests/test_runtime_recovery.py:30,1169,1401,2494,3673` | 共通semantic fixtureと4つの5引数`fake_semantic`がsignature追加の影響を受ける。新keywordを受けるだけでなく、generation一致/descriptor伝播を検査する。model-readyの2回目でも同一snapshotを確認する |
| `distribution/macos-local-memory/tests/test_package.py:1379,4368` | source-bodyの順序assertionとreview表示fixtureを確認。build順序（Reader検証→モデル）を保持。review fixtureをsnapshot真正性の合格試験へ数えない |
| F05a新test、F03a/F02a旧test | F05a受理後の実ファイルとmockを再調査し、必要なauthority引数のみ理由付きで更新。凍結済み過去artifact/test snapshotは変更しない |

真のbuilder/Validator結果を単にmockのPASSへ差し替えて正常経路を維持したことにしない。guard内で使うfixture全体を新契約へ合わせる。test suiteの件数・時間・既存のskip条件は再点検してから実行対象を決める。

## 8. 次回の具体的RED候補（未実行）

基礎fixtureは小型CSVの版候補2件＋無関係な連絡先。正常numeric版でbobの回答・最終監査が成立する既存経路も残す。Humanケースは候補全集合hashに結び付いたD0/D1を明示的に用意する。製品API追加前にTypeErrorをREDとして数えない。

1. **新app世代のsnapshot実体がない。** 初回shared欠損から実build後、固定snapshotの存在・空payload・graph/state/metadata/登録contractの同じhashを要求する。現実装はsnapshotを作らないので、存在/binding assertionによる真REDを得られる。
2. **共有decision変更が同じgenerationの再検証へ影響する。** D0で作った世代を保持し、共有ファイルだけをD1/破損/欠損へ変えてValidatorとprojectorを再実行する。D0 snapshotへの明示authorityが維持され、選択・Evidence・bindingが変わらないこと。まず存在/bindingのRED、次に伝播段階のfailureを区別する。
3. **直後resolver gate後に自己整合graphを差し替える。** mixedまたはyear-held候補を偽activeへ変更し、graph・Reader側のhashだけを再封印する。Reader入口または少なくとも公開前Validator/projectorがF05完全再構築で拒否し、旧CONFIG/indexを保持する。初期gateで止めた成功を後続consumerの合格にしない。
4. **snapshot欠落・1byte変更・別generationコピー。** 保存された空/実decision snapshotを変え、metadataの自己hashだけを直しても失敗を要求する。新しいshareddecisionが存在していてもfallbackしない。検証はgeneration/lineage/indexを一切修復しない。
5. **untrusted pathを一切開かない。** graph.source、Reader state、producer snapshot descriptorへ外部canary/共有SUPPORT/`..`/symlinkを記載し、open/stat/resolveの監視でそのpathに触れないことを要求する。明示caller側に正規snapshotを渡して比較で拒否する。API省略をproducer metadataで救済する経路も拒否する。
6. **parse/hash/selectionの二重読取。** 検証後の二度目のopenで別graph/snapshotを返す注入を行い、選別は実際に検証した同じbytesに基づくことを確認する。post-attestation model probeでproducer stateを書き換える既存反例もsnapshot fieldへ拡張する。
7. **新旧Human decisionとstale。** D0の旧世代は固定、D1を共有へ書いた新世代だけ新選択になる。さらに選択先/非選択先の内容変更・candidate追加で新buildはstale全保留。正規の古い版選択も引き続き利用可能にする。
8. **互換と失敗保持。** legacy無版成功、明示no-decisionのstandalone制御、new-appのsnapshot必須、旧versioned世代のmigration理由を分ける。全held rebuild失敗・snapshot作成既存先拒否でCONFIG/index/原本を保持し、正常回答テストを全保留へ置換しない。
9. **model-ready二回目のReader。** モデルはstubのまま、shareddecisionを2回のsemantic pipelineの間で変更し、2回目も最初のsnapshot/hashを使う。新しい実モデル取得は行わない。

各ケースは未実行の期待であり、現在の製品失敗件数やPASSではない。新契約では1run30秒、log1MiB、source fixture16KiB、decision8KiB、graph64KiB、全書込合計1MiB等を具体的に固定する候補がある。既存guardのsource-only計数にsnapshot/graph書込まで含まれるとは言わず、追加fixture計数が必要。必要な正常回帰・欠落・timeout・skipはPASSにしない。

## 9. 保証しないこと

snapshotは「そのbuildがどのdecision bytesで処理したか」を固定する。UIがどの世代の候補を見せたか、クリックした人の真正性、共有reviewと公開世代の一致、Human revisionとbuild leaseの直列化、最新選択を公開したことは保証しない。`local_memory_server.py:646,2693`は共有reviewとSTATE表示を使い、`record_decision()`も現時点ではgraph自己hashを基に保存する。これらF06/F19の境界を、snapshotで修正済みとはしない。

さらに独立した年次資料の複数保持、別family/cross-key同一性、全Path inventoryの物理的真正性、source freshness、全ファイル形式、OS全体のrace/資源上限、配布物/実モデル/UI受入は別契約。F05a/F05bで同じ決定規則を再利用しても、その規則自体の業務上の正しさを独立に証明したことにはならない。

## 10. 読取の再現用hash

以下は00:36 UTCに読み取ったcode/testの観測値であり、次の実装beforeを固定するものではない。

| パス | SHA-256 |
|---|---|
| `distribution/macos-local-memory/engine/build_adaptive_semantic_graph.py` | `19b5c55c64959dc136cd4d6b2081151ed1e39560874fb083e9796edb8812adfa` |
| `distribution/macos-local-memory/engine/validate_adaptive_semantic_graph.py` | `49138ecc11bde5709f129e879cbc5140717dca716a8563ebafe77dda4e37187d` |
| `distribution/macos-local-memory/engine/build_local_semantic_index.py` | `2bfaf8174e8249087d720e49070c9812ce9ecf93b21dd5276f057e145965dfb2` |
| `distribution/macos-local-memory/app/local_memory_server.py` | `3acb859916ccb9fb1a1c26cc0ebfa511bea2d8114fa83103ace40183fa899ee6` |
| `distribution/macos-local-memory/tests/test_versioned_safe_index_e2e.py` | `41540a858d17f8abb322c15353032ec4dd3099eac4e767090f7b9dbabfbcf64a` |
| `distribution/macos-local-memory/tests/test_reader_generation_migration.py` | `7d92425d39eb0316687ca9b8b0250768444652dcd39b7dfa9324380396b72d80` |
| `tests/test_immutable_lineage_validation.py` | `89da7502c783e78fa3f3f11ba25a0f09680d35602548ea858d203ff9c8339989` |
| `tests/test_unmarked_version_e2e.py` | `146a69f210c47c8f36390238d68c5f37bbb955b3f6faa3729febe38f3c014105` |
| `distribution/macos-local-memory/tests/test_runtime_recovery.py` | `f4a146b54526f279bf6923029e1887c7bea0298680913b768496502ade89ae77` |

次の一手はF05a受理後にこの提案を新しい実装契約へ具体化し、所有者・accepted-source hash・API/CLI・literal REDを固定すること。現在はこのpreflightの保存だけで完了する。
