# Multi-model Evidence / Answer Orchestrator

## 1. 目的

第4回提出の `0.40000` 経路を固定したまま、複数のOCR・レイアウト・表・グラフ・計算モデルをローカルで逐次実行し、各モデルの得意な観測だけを根拠付きで統合する。

最終的な単位は「モデルの回答文」ではない。

- 入力側: `QuestionGraph`
- 資料側: `DocumentGraph / EvidenceGraph`
- 出力側: `AnswerGraph`

この3つを決定的validatorで結び、最後にだけ文章へレンダリングする。

## 2. 多数決にしない

モデル数を増やすだけで精度は上がらない。同系統のモデルは同じ間違いをするため、多数決は相関した誤りを強化する。

統合規則は次の優先順にする。

1. native sourceから決定的に復元・再計算できる値
2. source hashとbboxを持つ複数の独立観測が合意する値
3. 一つのモデル候補だが、別の構造・算術validatorで再計算できる値
4. 不一致のままの複数候補

4は無理に1つに選ばず、`unresolved` として候補を全保持する。

## 3. 逐次実行とキャッシュ

同時実行は必要ない。ローカル実行は次のkeyでキャッシュする。

```text
asset_sha256
+ region_bbox_or_page
+ engine_fingerprint_sha256
+ preprocessing_profile_sha256
= observation_cache_key
```

モデルは1回ロードし、同じ種類のregionをbatch処理する。後続実行は差分assetと未解決regionだけに限る。

## 4. 役割別のmodel registry

| stage | 役割 | 現在の候補 |
|---|---|---|
| native | Office/CSV/JSON/Notebookの正本抽出 | 既存native parser |
| region | page、段組み、table、pictureの分割 | Docling Heron、後続PP-DocLayout |
| text | bbox付き生文字候補 | Apple Vision、PP-OCRv6、NDLOCR-Lite、Tesseract |
| table | row/column/cell/span/header候補 | Docling TableFormer、後続Table Transformer |
| chart | axis/series/legend/value候補 | source recovery優先、後続DePlot/VLM |
| arithmetic | filter/group/mean/diff/ratio/arg-extreme | deterministic executor |
| render | AnswerGraphから文章化 | deterministic renderer、説明のみgrounded LLM |
| verify | 根拠、全件性、型、単位、桁 | deterministic validators |

registryは少なくとも次を固定する。

- engine / model / code / configuration hash
- license SPDX、source URL、attribution
- 対応asset / region / language / writing mode
- cold/warm runtime、memory、hard timeout
- held-outの形式別信頼度
- 本線、shadow、rejectedのpromotion status

## 5. Observationと統合record

各モデルは最終回答を返さず、変更しない観測を返す。

```json
{
  "observation_id": "obs_<hash>",
  "asset_sha256": "...",
  "region": {"page": 3, "bbox": [120, 80, 440, 90]},
  "engine_fingerprint_sha256": "...",
  "kind": "text_line",
  "raw_value": "採否・閾値根拠記録",
  "confidence": 0.94,
  "raw_sidecar_sha256": "...",
  "status": "observed"
}
```

次に、geometryで同一対象の観測をalignment groupへまとめる。

```json
{
  "alignment_group_id": "alg_<hash>",
  "member_observation_ids": ["obs_a", "obs_b", "obs_c"],
  "candidate_values": [
    {"raw_value": "採否・閾値根拠記録", "support": ["obs_a", "obs_b"]},
    {"raw_value": "採否・開値根拠記録", "support": ["obs_c"]}
  ],
  "resolution": "observed_consensus",
  "selected_value": "採否・閾値根拠記録"
}
```

model confidenceは完全性の証明に使わない。選択はgeometry、独立性、形式別held-out、native照合、下流の再計算で決める。

## 6. DocumentGraph / EvidenceGraph

OCRとDoclingの出力は直接回答に使わない。

```text
raw observations
  -> alignment / disagreement preservation
  -> Document / Page / Region / Table / Row / Cell / ChartElement
  -> Evidence / Relation
  -> SearchUnit
  -> lexical + semantic index
```

不一致を含むEvidenceは両候補とbboxを残し、検索ブーストと最終Claimのexactnessを制限する。

## 7. AnswerGraph

QuestionGraphの必須出力ごとに、Evidence、値、演算、Claim、render slotを結ぶ。

```text
Evidence --supports--> SourceValue
SourceValue --input_to--> OperationResult
OperationResult --derives--> Claim
Claim --fulfills--> RequestedOutput
RequestedOutput --renders_as--> AnswerTextSpan
```

`ready` はモデルの自己申告にしない。次をvalidatorが再計算できた場合だけ認定する。

- QuestionGraphの全terminal outputにClaimがある
- 各Claimからsource hash・bboxまで追跡できる
- 演算DAGを再実行すると同じ値になる
- `all` / `count` に全件性証明がある
- 単位・型・桁・丸め・出力個数が契約と一致する
- unresolved Evidenceが必須Claimに流れていない
- 反証Evidenceを省略していない

自然文はAnswerGraphの後で作る。文章を再parseし、Claim、値、単位、件数をround-trip照合する。

## 8. 現PoCが示したこと

21個の人手検証済みregionに対する診断結果は次の通り。このsetは既存OCR観測を参考に選んだため、未知held-outではない。

| engine | exact | text CER | important span recall | warm p50 |
|---|---:|---:|---:|---:|
| Apple Vision | 21/21 | 0.0000 | 1.0000 | 152.19ms |
| PP-OCRv6 medium | 17/21 | 0.0316 | 0.9355 | 99.39ms |
| NDLOCR-Lite | 14/21 | 0.1107 | 0.9032 | 492.66ms |
| Tesseract | 12/21 | 0.2213 | 0.8387 | 92.13ms |

Doclingの同一の表画像比較では、Tesseract接続が8×3の外形を検出した一方、24セル中11セルしか文字を保持しなかった。OcrMac（macOS Vision backend）接続では8×3と24セルを保持した。一方、複雑な二段組み・複数表ページは両構成とも1表へ誤統合した。

これは「最良モデルを1個選ぶ」のではなく、「Doclingでregion/provenance、Apple/Paddle/NDLOCRで文字候補、別のregion splitterで複雑ページ分割、決定的executorで値を復元」が必要なことを示す。

## 9. 導入順

1. 全engineのraw sidecar・hash・license・hard timeoutを揃える。
2. 人手正解付きのphoto / handwriting / vertical / complex table held-outを追加する。
3. Observationとalignment groupのclosed schemaを実装する。
4. 表はDocling構造候補とApple/Paddle文字候補をcell bboxで結ぶ。
5. 複雑ページを段組み・表regionに分けてからTableFormerを再実行する。
6. resolved EvidenceだけをSearchUnitの本線へ、unresolvedは別candidate channelへ入れる。
7. structured deterministic回答からAnswerGraphをshadow構築する。
8. 全validatorを通ったAnswerGraphだけ第4回baseline回答と置換する。

現時点では、1〜2の比較基盤とDocling構造PoC、および本設計までが完了している。Observationの統合、Evidenceへの昇格、AnswerGraphのruntime生成はまだ本線未実装であり、個々のモデル出力を多数決で回答へ採用してはいない。

## 10. promotion gate

モデルをダウンロードしただけで本線に入れない。

- 質問・正解・過去回答を使わない抽出PoC
- 形式別held-outで既存構成以上
- 関係ない形式を退行させない
- 不一致と失敗を消さない
- model self-confidenceだけで候補を決めない
- cacheを含む全出力がsource/model/config hashで再現できる
- timeoutでworkerを強制終了できる
- 闇雲な1件選択、黙った文字正規化、自由な幻覚補完を行わない

このgateを通るまでは `shadow` とし、第4回baseline経路を変更しない。
