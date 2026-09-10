"""Read-only isolated README delta, verified against the frozen before hash."""
from pathlib import Path
import difflib
import hashlib
import json

ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "distribution/macos-local-memory/README.md"
OLD = "版判定器の`validate`は、呼出し側が指定したinventoryと、必要なら明示した`--decisions`から候補・選択・保留・Node/Edge・件数を再構築して照合します。人の選択を使ったグラフを検証する場合は、その選択ファイルも明示してください。グラフ内のパスを新しい読取先としてたどりません。アプリは版グラフ作成直後、Readerの前にこの検証を行い、不一致なら新しい索引の作成を止めます。これは指定された入力との一致確認であり、人の判断の真正性や資料の現在の鮮度を証明するものではありません。後続Reader／索引側への再構築検証の接続と、保存済み世代用の固定された選択snapshotは今後の課題です。"

def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()

def main():
    current = PATH.read_text()
    start = current.index("版判定器の`validate`は")
    end = current.index("\n\nPaddleOCRを使うには", start)
    before = current[:start] + OLD + current[end:]
    assert sha(before) == "2d2566607a500e0d195df5bb8f7bf1ee28ba78422ecc4bf24e396d4b090e4750"
    assert sha(current) == "594cfcd92a4bab7609933864dc7518008758d148a36fc35a63b2f6a716a7d392"
    patch = "".join(difflib.unified_diff(before.splitlines(True), current.splitlines(True), fromfile=str(PATH), tofile=str(PATH)))
    print(json.dumps(dict(path=str(PATH), before_sha256=sha(before), after_sha256=sha(current), before_source=before,
                         isolated_unified_diff=patch, scope="snapshot and selection explanation; explicitly still under synthetic verification, no release claim"), ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
