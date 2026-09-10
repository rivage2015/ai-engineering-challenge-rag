# F02a — F18 freeze解除後に必要な差分

状態: 計画のみ。2026-09-09。F18監査中のresolver・既存テスト・E2Eには本担当は触れていない。既存テストのhashは初回F02 review後にF18担当が更新しているため、`f02a-source-manifest.v1.json` のsnapshotを使い、実装時に再確認する。

1. **resolver:** `automatic_selection()` 241行の `unique_latest_explicit_year` の成功returnだけを、selected=None・reason=`year_order_does_not_establish_supersession`・全candidate pathのconflictsへ変える。年branchから版branchへのfallthroughはさせない。version/policy説明を更新する。既存F04aや人の判断の優先・stale判定は維持する。
2. **既存resolver/Readerテスト:** `test_unique_latest_explicit_year_is_selected` の誤った年選択goldを理由付きで更新する。`test_answer_policy_keeps_active_and_holds_other_candidates` と `test_reader_end_to_end_indexes_only_active_version` は、正常な一件選択fixtureを年なしver1/ver2へ移し、active/historical/ungroupedの回帰を残す。年だけの専用caseで全candidateがheld、連絡先だけeligible、review=2、historical=0、active Edgeなしを検査する。
3. **既存stub E2E:** 現在の `seed()` は `業務内容2024.csv / 業務内容2025.csv` でbobだけが選択される前提。標準正常fixtureを年なしver1/ver2へ移し、公開path gold、legacy両件、human選択・内容変更、旧版retrieval不存在のpathを対応させる。stubの正常回答・最終監査をinsufficientへ一括変更して消さない。年のみseedを別に用意し、全heldと無関係資料の公開を確認する。
4. **F18変更との統合:** F18が加えたValidator結果・一時出力・mock返値等の差分を読んで維持する。既存テストを旧F04a snapshotから書き戻さない。共有hashのfreezeを親が解除するまで実装しない。
5. **全候補heldの場合:** Reader `build_adaptive_semantic_graph.py` は `blocked_no_supported_files` を保存して `adaptive_reader_no_supported_files` をraiseする。空indexを成功公開せず、以前のCONFIG/index hashを維持することを後続のguarded統合試験で確認する。この安全停止を独立した年次資料の正常利用の合格にしない。

本F02aのresolver APIや候補schemaは変わらないため、Reader・index・serverの製品コード変更はこのsliceでは予定しない。Readerは既存のneeds_human_reviewを除外・集計できる。ただしF05の候補完全性検証、F06/F19のreview/公開世代結合、一件選択しかできないHITLは別の未解決である。新しく保留となる全候補を「未対応」と呼ぶUX不足も別に残す。

独立した年次資料を両方利用するには、人が確認した関係と対象期間を保持し、完全partition・独立検証・複数資料保持HITLを組み合わせる後続契約が必要。ファイル名からannual認定したり、年なしへ改名して回避したり、全activeにして今のresolved=一件規則へ押し込んだりしない。F02aの5件がGREENになってもF02全体は完了ではない。

実装後は新しいrun directoryへGREENを保存し、`f02a-red-001` を上書きしない。新規testのcurrent-marker classは残存witnessであり、年次対応の改善PASSの件数に含めない。最大2 repair rounds、30秒／fixture・source・log各1 MiBの上限、実資料・モデル・network・GUI不使用を維持する。
