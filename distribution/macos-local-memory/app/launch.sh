#!/bin/zsh
set -u

APP_SUPPORT="$HOME/Library/Application Support/LocalMemorySearch"
CONFIG="$APP_SUPPORT/config.json"
LOG_DIR="$APP_SUPPORT/logs"
CACHE_DIR="$HOME/Library/Caches/LocalMemorySearch"
mkdir -p "$LOG_DIR" "$CACHE_DIR"

RESOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

find_python() {
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    if [ -x "$candidate" ]; then
      "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1 && { print -r -- "$candidate"; return 0; }
    fi
  done
  return 1
}

PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  ANSWER="$(/usr/bin/osascript -e 'button returned of (display dialog "初回セットアップにPythonが必要です。Python Software Foundationの公式インストーラを自動取得して開きます。" buttons {"キャンセル", "取得して開く"} default button "取得して開く" with icon caution)' 2>/dev/null || true)"
  [ "$ANSWER" = "取得して開く" ] || exit 0
  PYTHON_PKG="$CACHE_DIR/python-3.14.7-macos11.pkg"
  /usr/bin/curl -fL --retry 3 --connect-timeout 20 "https://www.python.org/ftp/python/3.14.7/python-3.14.7-macos11.pkg" -o "$PYTHON_PKG.part" >>"$LOG_DIR/launcher.log" 2>&1 || exit 1
  /bin/mv "$PYTHON_PKG.part" "$PYTHON_PKG"
  /usr/sbin/pkgutil --check-signature "$PYTHON_PKG" 2>&1 | /usr/bin/grep -qi "Python Software Foundation" || { /bin/rm -f "$PYTHON_PKG"; exit 1; }
  /usr/bin/open "$PYTHON_PKG"
  /usr/bin/osascript -e 'display dialog "表示されたPythonインストーラをクリックで完了し、その後Local Memory Searchをもう一度開いてください。" buttons {"OK"} default button "OK"'
  exit 1
fi

if [ ! -f "$CONFIG" ]; then
  SOURCE="$(/usr/bin/osascript -e 'POSIX path of (choose folder with prompt "曖昧な記憶から探したいフォルダを選んでください")' 2>/dev/null || true)"
  [ -n "$SOURCE" ] || exit 0
  "$PYTHON" "$RESOURCE_DIR/bootstrap.py" configure "$SOURCE" >>"$LOG_DIR/launcher.log" 2>&1 || exit 1
fi

if [ ! -x /usr/local/bin/ollama ] && [ ! -x /opt/homebrew/bin/ollama ] && [ ! -x /Applications/Ollama.app/Contents/Resources/ollama ] && [ ! -x "$HOME/Applications/Ollama.app/Contents/Resources/ollama" ]; then
  ANSWER="$(/usr/bin/osascript -e 'button returned of (display dialog "ローカルAIの実行にOllamaが必要です。Ollama公式DMGを自動取得し、このユーザーのアプリケーションに導入します。" buttons {"キャンセル", "許可して続ける"} default button "許可して続ける" with icon caution)' 2>/dev/null || true)"
  [ "$ANSWER" = "許可して続ける" ] || exit 0
  OLLAMA_DMG="$CACHE_DIR/Ollama.dmg"
  OLLAMA_MOUNT="$CACHE_DIR/OllamaMount"
  /usr/bin/curl -fL --retry 3 --connect-timeout 20 "https://ollama.com/download/Ollama.dmg" -o "$OLLAMA_DMG.part" >>"$LOG_DIR/launcher.log" 2>&1 || exit 1
  /bin/mv "$OLLAMA_DMG.part" "$OLLAMA_DMG"
  /bin/mkdir -p "$OLLAMA_MOUNT" "$HOME/Applications"
  /usr/bin/hdiutil attach -readonly -nobrowse -mountpoint "$OLLAMA_MOUNT" "$OLLAMA_DMG" >>"$LOG_DIR/launcher.log" 2>&1 || exit 1
  /usr/bin/codesign --verify --deep --strict "$OLLAMA_MOUNT/Ollama.app" >>"$LOG_DIR/launcher.log" 2>&1 || { /usr/bin/hdiutil detach "$OLLAMA_MOUNT" >/dev/null 2>&1; exit 1; }
  TEAM_ID="$(/usr/bin/codesign -dv --verbose=4 "$OLLAMA_MOUNT/Ollama.app" 2>&1 | /usr/bin/awk -F= '/TeamIdentifier=/{print $2}')"
  [ "$TEAM_ID" = "3MU9H2V9Y9" ] || { /usr/bin/hdiutil detach "$OLLAMA_MOUNT" >/dev/null 2>&1; exit 1; }
  /usr/bin/ditto "$OLLAMA_MOUNT/Ollama.app" "$HOME/Applications/Ollama.app"
  /usr/bin/hdiutil detach "$OLLAMA_MOUNT" >>"$LOG_DIR/launcher.log" 2>&1
fi

if [ -d /Applications/Ollama.app ]; then /usr/bin/open -gja Ollama; fi
if [ -d "$HOME/Applications/Ollama.app" ]; then /usr/bin/open -gj "$HOME/Applications/Ollama.app"; fi

PORT="$($PYTHON -c 'import json,os; p=os.path.expanduser("~/Library/Application Support/LocalMemorySearch/config.json"); print(json.load(open(p)).get("port",8765))')"
if ! /usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
  /usr/bin/nohup "$PYTHON" "$RESOURCE_DIR/local_memory_server.py" --port "$PORT" >>"$LOG_DIR/server-console.log" 2>&1 &
  sleep 1
fi
/usr/bin/open "http://127.0.0.1:$PORT/"
