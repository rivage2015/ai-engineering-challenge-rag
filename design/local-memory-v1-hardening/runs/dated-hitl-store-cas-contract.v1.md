# Dated HITL decision-store CAS contract v1

## Goal

日付付き資料のHuman判断を、表示後に別の判断が保存された場合でも取り違えずに保存する。これはUI接続前の保存層だけの限定実装であり、回答時freshness、Human本人性、実資料、配布アプリを認証しない。

## Target and ownership

- 製品変更: `distribution/macos-local-memory/engine/document_version_resolver.py`
- 新規テスト: `tests/test_dated_consent_store_cas.py`
- 原本資料、CONFIG、公開索引、Keychain、アプリ本体、既存判断storeは触らない。

## Closed invariant

- 専用の同一lockを排他取得してから、判断storeの存在とraw bytesを一度だけ読む。
- `expected_sha256=None` はstore不在だけに一致し、存在する空storeのhashとは区別する。
- 表示時hashと現在raw bytesのhashが違えば `dated_consent_store_changed` で停止し、書き込まない。
- 完全なschema 2.0 dated consent recordだけを受け付ける。
- 同じ表示revisionを二度使った保存、別groupへの同時保存も二回目を停止する。
- 成功時は他groupのactive recordを保持し、同一groupの旧recordは`inactive_decisions`へ原形のまま移してから、schema 2.0 envelopeを同一lock中にatomic replaceする。
- malformed、duplicate key、nonfinite JSON、symlink/non-regular store、過大storeは失敗し、既存bytesを保持する。

## Validation and rollback

- 一時directory内の合成JSONだけで、正常、absent/present、stale、二writer、履歴、malformed、symlinkを確認する。
- resolver以外の回帰を実装完了とは数えない。UIは次の別slice。
- rollbackはこのsliceのresolver差分と新テストだけを対象とし、過去のdated consent 0.1.6差分を戻さない。

