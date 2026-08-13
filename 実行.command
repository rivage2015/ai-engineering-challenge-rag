#!/bin/bash
# ダブルクリックで RAG パイプラインを実行する。
cd "$(dirname "$0")" || exit 1
BASE="$PWD"
cd "$BASE/rag" || exit 1

echo "=============================================="
echo " AI Engineering Challenge - RAG 実行"
echo "=============================================="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見つかりません。"
  echo "ターミナルで  xcode-select --install  を実行してから、もう一度お試しください。"
  echo; read -r -p "Enterで閉じます"; exit 1
fi

if [ ! -d .venv ]; then
  echo "初回セットアップ: 仮想環境を作成しています…"
  python3 -m venv .venv || { echo "仮想環境の作成に失敗しました"; read -r -p "Enterで閉じます"; exit 1; }
  ./.venv/bin/pip install --quiet --upgrade pip
  echo "必要なライブラリをインストールしています… (数分かかります)"
  ./.venv/bin/pip install --quiet -r requirements.txt || { echo "インストールに失敗しました"; read -r -p "Enterで閉じます"; exit 1; }
  echo "セットアップ完了"; echo
fi
PY="$BASE/rag/.venv/bin/python"
PIP="$BASE/rag/.venv/bin/pip"

if [ -f .env ]; then . ./.env; fi
export RAG_BACKEND="${RAG_BACKEND:-ollama}"
export RAG_MODEL="${RAG_MODEL:-gemma4:12b}"

ask_for_key() {
  echo "OpenAI の API キーを入力してください。"
  echo "  取得元: https://platform.openai.com/api-keys"
  echo "  （入力内容は画面に表示されません。このフォルダ内の .env にのみ保存されます）"
  printf "APIキーを貼り付けて Enter: "
  read -r -s KEY; echo
  KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
  if [ -z "$KEY" ]; then echo "キーが入力されませんでした。"; return 1; fi
  LEN=${#KEY}
  echo "  受け取った文字数: $LEN"
  if [ "$LEN" -lt 40 ]; then
    echo "  ※ 短すぎます。コピーが途中で切れている可能性があります。"
  fi
  case "$KEY" in
    sk-*) ;;
    *) echo "  ※ sk- で始まっていません。別の文字列を貼り付けていないか確認してください。" ;;
  esac
  printf 'export OPENAI_API_KEY=%s\n' "$KEY" > .env
  chmod 600 .env
  export OPENAI_API_KEY="$KEY"
  echo "  保存しました。"; echo
}

if [ "$RAG_BACKEND" = "openai" ] && [ -z "$OPENAI_API_KEY" ]; then
  echo "OpenAI の API キーが未設定です。"
  ask_for_key || { read -r -p "Enterで閉じます"; exit 1; }
fi

if [ "$RAG_BACKEND" = "openai" ] && [ -n "$OPENAI_API_KEY" ]; then
  echo "APIキー: 設定済み（${#OPENAI_API_KEY}文字 / 末尾 ...${OPENAI_API_KEY: -4}）"
  echo
fi

echo "回答方式: $RAG_BACKEND / モデル: $RAG_MODEL"
if [ "$RAG_BACKEND" = "openai" ]; then
  echo "※ ここではAPIキーを貼り付けないでください（画面に表示されてしまいます）"
  echo "  キーを変更するときは、先に 5 を選んでください。"
fi
echo
echo "何を実行しますか？"
echo "  1) 検証: 30問に回答して、ローカルで採点する（まずはこちら）"
echo "  2) 提出用: 100問に回答して submission.zip を作る"
echo "  3) 検索結果だけ確認する（LLMを呼ばない・課金なし）"
echo "  4) 資料の解析をやり直す（ファイルを更新したとき）"
echo "  5) APIキーを入れ直す"
printf "番号を入力して Enter [1]: "
read -r CHOICE
CHOICE=${CHOICE:-1}
echo

score_locally() {
  echo
  echo "----- ローカル採点 -----"
  if ! "$PY" -c "import tiktoken, tqdm, pandas" >/dev/null 2>&1; then
    echo "採点用ライブラリをインストールしています…"
    "$PIP" install --quiet tiktoken tqdm pandas || { echo "インストール失敗"; return 1; }
  fi
  mkdir -p "$BASE/evaluation/submit"
  cp "$BASE/rag/out/predictions.csv" "$BASE/evaluation/submit/predictions.csv" || return 1
  ( cd "$BASE/evaluation" && "$PY" crag.py )
  echo
  echo "詳細は evaluation/result/scoring.csv を参照してください。"
}

case "$CHOICE" in
  sk-*)
    clear
    echo "=============================================="
    echo " 警告: APIキーがこの画面に表示されました"
    echo "=============================================="
    echo
    echo "メニュー欄に貼り付けられたため、キーが画面に出てしまいました。"
    echo "このキーは漏れたものとして扱ってください。"
    echo
    echo "  1. https://platform.openai.com/api-keys を開く"
    echo "  2. 該当のキーを Revoke（削除）する"
    echo "  3. 新しいキーを作り、このスクリプトのメニュー5で登録する"
    echo
    echo "このターミナルウィンドウは閉じてください。"
    echo "=============================================="
    ;;
  1) "$PY" main.py --valid && score_locally ;;
  2) "$PY" main.py ;;
  3) "$PY" main.py --dry-run ;;
  4) "$PY" main.py --rebuild --dry-run ;;
  5) ask_for_key && echo "次回の実行から新しいキーが使われます。" ;;
  *) echo "1〜5 で指定してください" ;;
esac

echo
echo "=============================================="
read -r -p "Enterで閉じます"
