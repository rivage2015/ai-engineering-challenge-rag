# F11a image regression — bounded repair design v1

Task: `lms-v1-f11a-notebook-metadata-2026-09-09`。2026-09-09。独立 reviewer による静的な修復案であり、製品実装・実行・正式監査・PASS 判定ではない。使用 skill: codex-graph-engineering-adapter、graph-engineering-agentic-audit。同モデル別 context の手続的分離であり、モデル多様性による独立性ではない。

今回の新規編集は本書のみ。製品・既存 test・独立 gold・過去 run は変更していない。以下の API・分類名は root の契約補足／実装許可を待つ提案であり、元契約に既にある義務と混同しない。

## 1. 結論と失敗の意味

Probe 内に親画像 record の実参照を受け取る共通分類を置き、Notebook の native textual Evidence と provisional visual text を区別する。visual のラベルだけでは除外しない。適正な image-parent・origin・producer location をすべて確認できた visual text のみ Notebook metadata の checked/unchecked 双方から除外する。Search の二箇所の呼出しと両 validator へ同じ親 lookup を通す。

親実行 `f11a-regression-image-001` は 3 methods、2 assertion failures・7 errors（subtest 含む）、skip 0。通常の producer VLM baseline が `notebook_rebuild_required: textual Evidence lacks notebook_state` で止まっており、negative forgery の検査には到達していない。失敗を fixture の期待値訂正や negative 成功に振り替えない。旧 log/result と gold は保持する。

元契約は画像/OCR state・raw 本文対応・全 membership を認証しない一方、既存 Notebook image mock 経路の保持を要求する。正しい visual text に saved-output state を作る修正、partial を success にする修正、visual という自己申告を一律免除する修正は不可。

## 2. 現行の二つの原因

- `Probe.notebook_evidence_state`（2718–2763）は `.ipynb` の `text_block` をすべて native textual と扱い、現在の producer が作る VLM provisional `text_block` に state を要求する。
- `notebook_document_binding`（2766–2818）は type だけで集計するため、visual に対して単に `None` を返すと `notebook_state_unparsed` と unchecked が増える。分類を正してもこの集計を直さなければ誤報が残る。
- Search builder の `consume`（818）と `add_direct_text`（756）はどちらも同 helper を呼ぶ。片方だけを変更しても通常経路は回復しない。
- `flush_image`（577–654）は active image の ID/locator を消す。producer は suppressed child VLM task を親 document へ投影し、deferred flush で後から emit できる（Probe 3354、3469、5921–5957）。active image だけでは正しい親が見つからない。

## 3. 提案する正確な内部 API

以下は Probe の既存出荷 module 内に置く。新 helper module／配布 build script の変更は不要。

```python
# Callable[[str], dict[str, Any] | None]; ID による record 解決だけを許す。
# source path / content_ref / visual_origin から file I/O する API ではない。
def classify_notebook_evidence(
    evidence, document=None, *, allow_unparsed=False, parent_lookup=None
) -> tuple[str, dict | None]:
    ...

# 既存 state API は互換 wrapper とし、成功結果の第二要素のみを返す。
# visual の妥当性確認を省く permissive flag は追加しない。
def notebook_evidence_state(
    evidence, document=None, *, allow_unparsed=False, parent_lookup=None
) -> dict | None:
    return classify_notebook_evidence(
        evidence, document, allow_unparsed=allow_unparsed,
        parent_lookup=parent_lookup,
    )[1]

def notebook_document_binding(
    document, evidence, source_root, *, parent_lookup=None
) -> dict:
    ...  # evidence は単回 iterable のまま。
```

分類は閉じた 4 値とする。

| kind | state | 意味と集計 |
| --- | --- | --- |
| `native_text` | strict state | 現行 canonical source/saved-output 検査を保持。原本 facts と比較した場合 checked、比較できなければ unchecked。 |
| `visual_text` | `None` | §4 の全条件を満たす Notebook provisional VLM child。両 count と `notebook_state_unparsed` から除外。 |
| `unparsed_text` | `None` | 現行の限定された genuine partial/raw fallback のみ。unchecked と `notebook_state_unparsed`。 |
| `not_applicable` | `None` | non-Notebook、又は独立 image/OCR/chart 等の既存対象外 record。state 注入の拒否は保持。 |

不正な visual 候補は `not_applicable` や `unparsed_text` に落とさず ValueError。`notebook_state` の存在、method/origin/location の visual signals の矛盾も検査する。たとえば state を持つ native record に visual method を加えた場合は「どちらか成功」で通さない。`.ipynb` applicability は従来どおり Document の path/extension を基準にし、parser/version の自己申告で変えない。

`parent_lookup` がない／親がない場合、visual 除外は失敗する。lookup が返す record は、要求した ID と `evidence_id` が一致し、child と同じ `document_id`、`evidence_type == image` でなければ失敗する。document が渡された場合はその ID とも一致させる。lookup 自体のプログラミング例外を UNVERIFIED/成功に変換しない。呼出し側の既存 duplicate/dangling/ID/hash 検査も残す。

## 4. visual 除外の限定された契約

Notebook textual record を visual と認めるには、少なくとも以下を全部必要とする。

1. type は `text_block`、method は既存二つ `local_vlm_visual_observation_provisional` 又は `local_vlm_unlocated_transcript_provisional`。既存 provenance 構造検査を維持する。未知の `local_vlm_*` は免除しない。
2. `native_properties` は object、`quality_tier == provisional`、marker は `[暫定読取]`、`question_independent is True`。`notebook_state` は null を含め key 自体がないこと。これらは分類条件であって、VLM の内容正しさや実行履歴の証明ではない。
3. 実 ID lookup から得た親 image が上記 ID/type/document 条件を満たす。
4. 親 origin の kind は `notebook_embedded_image`。親 origin/source location/materialization の既存整合検査をすべて通し、child origin と親 origin を strict JSON 等値で比較する。Document がある validator では relative_path/sha256/format が Document source と一致すること。画像 file を再読しない。
5. 親と child の location が下記の current producer 形であり、native `cell=N` / `cell=N;output=M` とは重ならないこと。indices の bool は int として受け取らない。新しい role の逃げ道として余分な location keys を許さない。
6. unlocated transcript は既存の `location_status == unlocated`、`transcript_type == whole_image_faithful_transcript`、geometry 不在も必要。chunk/character fields との整合は下記に限定し、本文を画像から再証明しない。

### 4.1 親 image の実 producer location

`N` は 1-based cell、`I` は Notebook 全体の image emission index（output index ではない）。どちらも strict positive int。親の exact location は `{notebook_cell_index:N, object_index:I, locator_text:L}`。親 ordinal は I。

L は現在の producer が作る以下三形だけ。

- saved output image: `cell=N;output=M;output-image=I`（M は strict positive output index）。
- Markdown data URI: `cell=N;source-image=I`。
- attachment: `cell=N;attachment-image=I;attachment=A`。A は親 native `attachment_name` を `urllib.parse.quote(name, safe="-._~")` したもの。parent/source location と child prefix は同じ encoded bytes を使う。

これは現行 producer の形確認であり、画像 membership／Notebook 原文への画像 pointer 認証ではない。複数画像に同じ output M があっても I を M に置き換えない。全 image 順番の完全性を新たに要求しない。

### 4.2 visual observation child

Probe `extract_image` は observation の location prefix に `object_index:1` を渡す（6680–6685）。ordinal は `len(read_lines)+1` であり、**object_index と等しいとは限らない**。投影後の exact location は:

```text
{notebook_cell_index:N, image_object_index:I, object_index:1,
 locator_text:L + ";visual_observation=whole_image"}
```

ordinal は既存の型／範囲条件を保持し、誤って 1 に固定しない。source location との結合は `_merge_visual_location` の current rule に一致させる。native saved-output の object_index M は image_object_index I の代用にならない。

### 4.3 unlocated transcript child

`C/K` は native `transcript_chunk_index/transcript_chunk_count`、`a/b` は `character_start/character_end`。C、K、J は strict positive int、1 <= C <= K、a/b は strict int かつ 0 <= a < b、offset basis は `zero_based_half_open`。J は child ordinal と同じ。投影後の exact location は:

```text
{notebook_cell_index:N, image_object_index:I, object_index:J,
 locator_text:L + ";location_status=unlocated;source=image;"
               + "chunk=C/K;characters=(a+1)-b"}
```

この形は Probe 6620–6668 の既存 fields から検査可能。全 OCR sibling を集めて J の base を再計算する、画像を開いて transcript 本文を検証する、全 chunk membership を立証する拡張はしない。current gold は observation 経路であり、unlocated transcript の成功をこの 3 methods の成功だけで主張しない。

### 4.4 既存 origin helper の再利用と循環回避

`validate_search_units._visual_origin_errors(parent, visual_sources, expected_container, document=None)`（366–473）は純粋な record 検査で、file/model/network I/O をしない。これを同じ semantics と error 群のまま Probe へ移す案を推奨する。必要な suffix/container mapping と hash pattern も既存値を共有し、Search 側に同名 import alias/wrapper を残す。intermediate stream の既存 import も壊さない。

Probe から Search validator を import して再利用する案は避ける。Search validator が既に Probe を import するため循環となり、producer を downstream validator に依存させる。新規共通 module は現在の十 file/配布制約を拡張するため不要。既存 helper の Python dict equality を新しい Notebook 境界の strict equality の代わりにしない。新 classifier 側の canonical JSON 等値＋明示型検査で補う。非 Notebook image packet の既存規則まで便乗変更しない。

## 5. 各経路への親 lookup 配線

| 呼出し箇所 | 正確な lookup / lifetime | 留意点 |
| --- | --- | --- |
| intermediate native | 既存 `evidence_by_id.get` を `notebook_document_binding(..., parent_lookup=...)` へ渡す。 | 既に全 record index がある。iterable を新たに list 化しない。 |
| intermediate schema/structural stream | 既存 SQLite に `SELECT record_json FROM evidence WHERE id=?` を parameter bind、`fetchone()`、一件だけ JSON decode する callable。 | outer document/evidence iterator と別の `connection.execute` cursor を使う。既存 DB 全件 load 後なので先後依存なし。same-document は共通 helper が確認する。 |
| Search native validator | `notebook_search_unit_contract_errors(..., *, parent_lookup=None)` を追加し、未指定時は既存 evidence map の `.get`。 | Document map と実 parent の両方を helper に渡す。existing image contract と context exact 検査を削らない。 |
| Search stream validator | 上記 notebook helper に同じ SQLite one-row lookup を明示で渡す。 | 現行 `notebook_sources` は SearchUnit が直接参照した child だけ（499–511）。parent が unit.source_evidence_ids にないことは正当。parent を SearchUnit の source IDs に加えて帳尻を合わせない。 |
| Search `DocumentDeriver` | constructor の既存四 positional args を保存し、keyword-only `parent_lookup=None, document=None` を追加。self の lookup を consume/add_direct_text の両方へ渡す。 | 同じ helper の厳密検査を使い、caller が渡した state を検査せず信用する shortcut を追加しない。 |

Search builder の通常の file-stream build には既存 SQLite がないため、最小変更は document 寿命の **Notebook image record 専用** private map とする。consume で親 image を受け取った時、その record の snapshot を `evidence_id` で登録する。raw image bytes / OCR / native text / VLM text は保存しない。`flush_image` ではこの map を消さず、document deriver の終了／破棄で解放する。衝突 ID を静かに上書きしない。外部 lookup が与えられた場合はそれを使用でき、独立した SQLite store を既存 validators に重ねて作らない。

これは O(当該 Notebook の image records) の追加保持であり O(全 stream records) ではないが、aggregate job/RSS の新しい上限保証ではない。既存 metadata 資源定数をこの map のために緩めたり、silent eviction によって genuine deferred VLM を拒否したりしない。現行 producer は親 image を child より先に emit する（Probe 5833–5949）。通常の build はこの既存順序を利用し、外部 supplied lookup は全 ID index から順不同の入力にも対応可能。一般的な任意順 Evidence の streaming 再構成を新たに完成したとは主張しない。

builder の `document=None` は既存直接 API の互換用。これは child/parent の同 document ID・origin・location の整合に留まり、原本 source attestation ではない。両 validator は実 Document を渡し source identity まで比較する。builder のために原本 Notebook や content_ref を開き直す変更は不要。Search の原本結合は元契約どおり、同 immutable generation への intermediate validation と組み合わせる。

## 6. 集計・エラー・未検証の保持

`notebook_document_binding` は分類を先に検査し、`visual_text/not_applicable` なら metadata 集計だけ skip する。raw document/evidence/relation counts は従来どおり全 record を数える。分類済み visual は state 不在を `notebook_state_unparsed` にしない。native の missing state、visual-parent 不正、state 注入、既知の元 metadata mismatch は ValueError のままであり、partial/rootless で覆い隠さない。

partial/failed reason `notebook_extraction_incomplete` はそのまま残す。今回の通常 fixture は source metadata checked=1、unchecked=0、reason はこの一個だけ、outer UNVERIFIED / binding unverified。saved text 併存 fixture は checked=2、unchecked=0、同 reason。旧 `validate` は `notebook_metadata_binding_unverified` 例外のまま、CLI code 2 と caller gate を回避しない。画像しかない／native textual が 0 の Notebook は依然 `no_textual_records_checked` となり、visual があるだけで PASS にはならない。

Search の visual text は従来どおり provisional `text_chunk` で、exact context は container_kind/quality_tier/provisional_marker のみ。Notebook state を追加しない。native source/saved-output SearchUnit の state exact copy・same-ID context 改変／欠落拒否はそのまま。image packet・VLM の品質 quarantine と adapter verified graph からの除外を保持する。

## 7. 修復後の固定期待と未証明項目

既存 `f11a-image-regression-gold.v1.py` の 3 METHODS は変更せず、新 run ID で再実行する。期待は以下。まだ到達／成功したという意味ではない。

1. 現 producer の通常 image + OCR + VLM + native source が既存 `_assert_pipeline` を通る。intermediate native/schema-stream/structural-stream は exact UNVERIFIED を返し、両 Search validators は既定 counts を返す。原本 bytes 不変。
2. real saved-output の native body/location を保ったまま actual VLM provenance/native labels を移植する 3 ケース（parent 無し・real image parent・wrong type parent）が全 intermediate paths で失敗する。baseline 成功後に negative へ実際に到達する。
3. genuine visual への state 注入・wrong parent type・wrong origin source hash が全 intermediate paths と両 Search validators で失敗する。Search state の outer hash は gold が再結合済みなので、stale outer hash だけの拒否に逃げない。

gold 3 methods の後にも、変更箇所に対応する小型 control として、少なくとも (a) 複数 image の後に最初の image の deferred VLM が来る lookup lifetime、(b) current producer の unlocated transcript location、(c) attachment/dataURI の親 prefix 互換、(d) missing/cross-document parent の拒否、(e) existing non-Notebook image packet の挙動保存を確認する価値がある。これは追加試験の提案であり、本書は test source や runner 実行を新たに許可しない。root は既存 controls の実到達で満たせる項目を先に選び、追加が必要なら小型 synthetic gold/guard を別途固定する。

重要な残存: attacker が body/location/parent/origin を全て整合的に作り替えた record の内容正しさ、親画像自体の元 Notebook image membership、全 child membership、VLM 実行や認識の正しさはこの metadata-only 修復では証明しない。特に「native canonical location を保つラベル偽装を拒否」と「任意の coherent visual forgery を原本から排除」は別命題。後者をこの修復の PASS 条件へ無断で追加せず、原本 body/membership の別契約に残す。全 F11/V1、配布済み app 全経路、全形式の完成にも昇格しない。

## 8. 読取対象の固定参照

すべて workspace 内。下記 hash は本書作成時に read-only で再確認。製品 v1 は親 freeze のまま。本書に記す行番号はこの版の参照。

| path | SHA-256 |
| --- | --- |
| `scripts/probe_intermediate_records.py` | `5a3c443a76f02b198367c036017967a33a75f22ec0826091ab5c8b54a988baa4` |
| `scripts/build_search_units.py` | `08d768bcc772c8d16c82d973d7cb8b212a1ee4bb1d8469bec5063e3707e0167b` |
| `scripts/validate_search_units.py` | `34b949755780e5699e549320730127af486f232c09dae724275ae36a855121f7` |
| `scripts/validate_search_units_streaming.py` | `cfdbfaedaf7557cb5f09c16f6be4020a32cd4fd6ea8d918858dab93d1a57173a` |
| `scripts/validate_intermediate_records.py` | `1b64c531e061c2faabef0aa912fab66a006b925755c9c60872261fe5ee5f9c46` |
| `scripts/validate_intermediate_records_streaming.py` | `db5d5e92518ce1c919dda2c796551adcb2646e2305a2c70b38df578ff9229199` |
| `runs/f11a-image-regression-diagnosis.v1.md` | `c60342c81fb92c2290a86ab826c2771ee239b8bf702b3d3456835304f9f173d1` |
| `runs/f11a-image-regression-gold.v1.py` | `75573e3ba7ea4a464b20feaf09b6a9e4d4a9133ceabe519399785f9638046913` |
| `runs/f11a-regression-image-001/result.json` | `b2c2bf6e0ab99a522ccc78e59dc9b8ad9363a2360868cda4ec4bd42ec75a5577` |
| `runs/f11a-regression-image-001/unittest.log` | `dfa209febe47a573e7111eae819b2fb60df34031601acb9ee9f51412d8089b46` |

ここで `runs/` は `design/local-memory-v1-hardening/runs/`。規範は同所 `f11a-task-contract.v1.md`（`20dfd742490462e101ce952e661c6d23a5a40ce0b89b3cf921204b973fc66d15`）と addendum v1（`2542f29b2776127a5d919639c69505d31b4efdf46968d9ff80a0af7d930f6f8f`）。変更には root の bounded repair gate が必要で、正式製品監査は別途 source freeze と immutable artifact の後に行う。
