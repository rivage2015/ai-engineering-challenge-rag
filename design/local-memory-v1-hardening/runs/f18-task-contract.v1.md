# F18: 不変なReader世代の再検証

task_id: lms-v1-00-hardening-2026-09-09-f18

状態: 契約固定、実装前。最大監査修正往復2回。局所契約でありV1全体の合格ではない。

## 守ること

通常の `validate(output, source_root, inventory, version_graph)` は、Readerの既存出力を削除・更新・修復しない。独立に再構築したlineageのbytesと既存2成果物を比較する。失敗時も既存世代のbytes・inode・mtimeとdirectory membershipを保つ。保存PASSだけで回答を認可せず、呼出しごとの検証失敗を上位へ返す。F01の明示version bindingを維持する。

初回の未公開世代だけ、明示keyword `initialize_lineage=True`／CLI `--initialize-lineage` を使って検証後のlineageを初期作成する。通常modeで欠落していても自動修復しない。初期作成でも既存2成果物のどちらか、dangling symlink、directoryがある場合は上書き／削除せず失敗。2個目の作成失敗時には今回作成した同一inodeの出力だけを回収し、外部が作ったファイルは触らない。新世代はまだ未公開であり、2ファイル公開の一般的atomicityや電源断耐久を主張しない。

## 対象と影響

- `engine/validate_adaptive_semantic_graph.py`: 開始時clearを廃止、既存成果物のread-only比較と明示初期作成を分ける。read-only比較はsymlinkを追わず、期待payload長+1までのbounded read。初期作成は排他的no-replaceと自分の生成物だけのcleanupを使う。
- `app/bootstrap.py`: 既存の新規未公開semantic directoryを作る `run_semantic_pipeline` の初回CLIだけflagを渡す。通常projectorの `_attest_lineage_context` はread-onlyのまま。
- 初回Validatorがlineageを生成すると仮定していた既存tests/fixture setupだけを明示初期化へ変更する。失敗時に旧lineageを消すことを期待した旧試験は、本契約に沿い不変を期待し、その変更理由を保存する。改竄検出・版判断・source bindingのassertionを弱めない。
- 新常設 `tests/test_immutable_lineage_validation.py` とf18-prefixed証跡。Core原則、版方針、Reader抽出、ユーザーbuild/docsを変更しない。

## 反証と合格

先に、検証がbuilder_invalidで止まるだけでも既存lineage2本が消えるREDと、publisherが既存ファイルを上書きするREDを保存する。続いて初回成功→反復read-only成功、source変更、Reader artifact改竄、lineage改竄／欠落／symlink、不正version binding、初期化の既存片側／両側、途中write／link failureと外部置換を検査。読み取り専用にした世代でも通常再検証できること、エラー時に呼出し元が保存PASSをfallbackにしないことを確認する。

既存lineage／security partition／versioned stub E2E／migration関連を副作用preflight後に再実行し、初期作成flagは本番CLIを通して検査する。合成小型CSV中心、1 worker 30秒、log1MiB、合成source text累計1MiB、モデル・HTTP・子processをworker内で禁止。既存F04a guardとF01 dispatcherは読んで再利用するが、OS sandboxや全derived disk／RSS上限の証明にはしない。Python3.14.6を製品配線に使う。

## 基準hashと復旧

- Validator: 1a3aff494b724882a600adc6978fda3e0f3955da6e2f11291180a92d7f898e1a
- bootstrap: 069c36f8d03634583dec27a6647ec100ae90bbbe7408b4275b164ee1b4b16192
- projector（読取対象）: 2bfaf8174e8249087d720e49070c9812ce9ecf93b21dd5276f057e145965dfb2
- 既存lineage test: 1656f08019db1f1910e1a82329048c603b3525980f685e362d9bc30dd8934732
- 既存F01 E2E: c16cc5bfafe493ad4096eadbf84722c8d9f1d476dcb03bfc36bb26959983341b

基点HEAD 79a69260fb1a42efb3c0e2c0093ed6101470f2a1、既存dirtyを保持。初期化API変更後のコードidentityは既存Reader世代contractに含まれる。実際の公開索引やCONFIGを移行・再構築しない。rollbackはF18の個別差分だけで、F01/F16等の受理済み差分やユーザー変更をresetしない。

全OS race／global snapshot／F02-F06の独立版partition／F13 freshness／F14資源／モデル・配布物はこの契約外。独立監査は別contextが実コード・反例を確認し、正式schema/hash/参照を親が検証する。
