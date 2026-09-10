# Parent validator runtime correction

2026-09-09 12:25 JST。F05b formal product repair0のまま。監査試験失敗ではない。

親validator v1（074f4fdfb751221a516126db51724e980dac4451007fa8ced5c36b23aa0b99eb）を既存jsonschemaのあるPython3.9.6で初回実行したところ、f05b-freeze.v1.py line100 `assert methods == snapshot["methods"]` でAssertionError、exit1。正式schema/hash/参照の後、3.14で固定したAST表現を3.9で生成したことによる不一致だった。初回をPASS扱いしない。v1ソースは不変で残した。

同じ `def f(): pass` を実際に両runtimeでast.dumpした。3.9.6は空args/decorator_list/type_ignores等を列挙し、3.14.6は省略する。製品/goldのbyte変更ではないことを確認した。

v2（18a346db1261bab529b01c3f974e40c088ed7fce228662f34201520f15e763c0）はschema検証を既存3.9に維持し、凍結AST hashの再構築と独立integrity再実行だけを作成時と同じ/opt/homebrew/bin/python3（3.14.6）で30秒上限のread-only childとして実行する。全旧gold/hash照合を省略せず、比較条件も弱めていない。親内の26→27→28比較は同じ3.9同士の構造比較を追加で維持する。

v2の実行はexit0/pass_local_f05b。210sources・107manifest・13逆差分・30過去attempt・18監査logs・全補助hash・元gold不変を照合。独立integrity JSONも再実行結果との完全一致。Ruby公式validatorも別途exit0 `VALID: audit report is structurally and referentially consistent`。最終受理記録はf05b-validation-result.v1.json。これらは限定F05bの確認であり全V1/全形式完成ではない。
