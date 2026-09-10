# F01 / F07 — Executor v3（修正ラウンド 2 / 2）

状態: **別担当の再監査待ち**。自己承認・コミット・製品全体の合格判定はしていない。

## 今回の修正

監査 v2 の必須指摘 F01F07-R04 に対応。既存 schema テストの成功 mock 六か所を、新しい返り値形式 `{lineage_validation: 元のstate, document_version_graph: None}` に変更した。元の state・改ざん条件・安全性 assert は維持。六つの wrapper だけを元に戻した AST が HEAD の AST と完全一致することを確認した。

前回「既存の直接呼出しは拒否系だけ」とした互換性説明は不十分だった。成功 mock も契約の利用者であり、監査の指摘どおり五つのテストで安全性 assert まで到達できていなかった。今回は全 20 件を実行して確認した。

並行 F16 改善により、一時フォルダの `/var/folders` エイリアスが実体ディレクトリ限定ルールに拒否されることも観測した。親の許可で E2E と migration の一時 root 二行だけを `resolve()` した。F16 境界・製品コードは今回変更していない。F01 元 issue の再発と混同しない。

所有ファイルは累計 **6 ファイル**（製品 3、テスト 3）。今回の変更はテスト 3 ファイルだけ。実 hash・ログ・コマンドは同名 JSON に記載。

## 検証結果

| 段階 | 結果 |
| --- | --- |
| 旧 mock の RED | schema 20 件中 5 ERROR（KeyError） |
| 修正後 schema | 20 PASS、skip 0 |
| F16 統合時の初回 E2E | 15 件中 5 ERROR（一時パスの symlink ancestor） |
| 一時 root 実体化後 E2E | 15 PASS、skip 0 |
| lineage + migration | 8 + 7 PASS、skip 0 |
| 旧監査の反証 2 件 | 標準 unittest runner で 2 PASS、skip 0 |
| diff check | PASS |

必須 Python 3.14.6 の 4 suite は計 **52 test executions PASS**。E2E / 関連回帰の前後は所有 6 + 共通依存 4 ファイルの hash 不変を保存し、最後の反証 replay でも共通依存 4 ファイルの hash 不変を保存した。F16 は `621746dc0d0ff0857db2c21844e7c042b9bf12debd47b7c145840d89479f8fdd`。

旧監査スクリプトを直接実行した一回は、unittest の後に hash 行が出るため、厳格化した supervisor の判定が `no_tests` になった。これは PASS に数えていない。同じ未変更の反証二つを module として読み、標準 runner で再実行した結果だけを採用した。

補助 Python 3.9.6 は schema 20 件中 1 ERROR（authorizer 解除後の SELECT が not authorized）。同じ venv のまま HEAD projector を読み込む比較でも同じ場所で再現した。旧環境の既存制約として保存し、今回の必須合格や Python 3.9 対応には数えない。追加修正はしていない。

## 安全性・残件

全 suite は 30 秒 / 出力 1 MiB 上限、合成入力、一時 SUPPORT / CONFIG / STATE / index、HTTP / model / Popen guard 下。実モデル・実データ・Keychain・GUI・ダウンロード・追加承認は使用していない。HEAD 比較だけは test guard 前に read-only の git show を実行した。

adapter skill に従い最小差分と再現証跡を残し、audit skill に従って実装者判断と別担当監査を分離した。F18・版選択の意味規則・実モデル品質・実 GUI / HTTP・製品全体の適合認証は未検証のまま。次の独立監査で新たな必須問題が残れば、上限 2 ラウンドに従い blocked として引き継ぐ。

機械可読記録: [F01-004-executor.v3.json](F01-004-executor.v3.json)

