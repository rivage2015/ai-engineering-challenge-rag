# F08/F09 再提出 v2 — 監査差し戻しの修正ラウンド1

状態: **17常設テスト＋監査反例4テストがGREEN。別担当の再監査待ち。** 既存v1の監査・実行記録・反例ファイルは変更していない。

## 必須修正 F08F09-A06

監査で、UTF-8 BOMの「あ」とUTF-16のサロゲートペアが、XML安全検査の2048-byte標本境界で切れると、有効なファイルまで拒否する回帰が見つかった。

`validate_xml_bytes`の標本decodeをstrict incremental decoderへ変更した。標本末尾はEOFとみなさず、途中文字をdecoder stateへ保持する。その後も同じ元bytesを64 KiBずつstrictに検証し、本当のEOFでfinalizeする。不正byteや本当に切断された末尾を無視する修正ではない。DTD/entityの全bytes走査、BOM/signature識別、宣言encoding-family照合は維持した。

共通gateを使うOOXMLにも影響するため、小型ZIPのXML memberを使い、通常の境界入力の受入とDTD拒否を確認した。Office形式のReader本体や関係解析は変更していない。

## RED → GREEN

- 修正前: 境界テストを追加した16 methodsで10 failures / 14 errors（subtest込み）、0.114秒。通常XMLの誤拒否を再現した。
- 修正後の常設suite: 17 methods、0.135秒、失敗・error・skip 0。
- 変更していない監査反例v1: 4 methods、0.087秒、失敗・error・skip 0。
- `git diff --check`: exit 0。

UTF-8 BOM／UTF-16 LE・BEは8つの境界ずらしとProbeの読取結果、UTF-32 LE・BEはbyte gateのみを検査した。不正encodingと真の途中EOF、標本より後のDTD/entity、encoding宣言不一致も拒否される。

統合fixtureを9 filesへ拡張し、監査で落ちた2つの通常XMLを含めた。失敗5 filesはEvidence／SearchUnitなし、正常4 filesは両Intermediate Validator → 両SearchUnit Validator → semantic adapterまで保持され、元ファイルhashも不変だった。

全実行は180秒timeout／8 MiB出力確認のrunner、合成資料のみ。モデル・HTTP・外部送信・個人資料・追加承認要求は0。実モデル、SQLite公開、回答、配布版は引き続き未検証。全XML対応やV1.00完成とは判定しない。

詳細なコマンド・SHA・変更境界は`f08-f09-executor.v2.json`を参照。Adapterの最小差分と、Agentic Auditの修正ラウンド上限・自己承認禁止に従い、親担当へ再監査を依頼する。
