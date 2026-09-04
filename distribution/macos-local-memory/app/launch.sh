#!/bin/zsh
set -u
umask 077

APP_SUPPORT="$HOME/Library/Application Support/LocalMemorySearch"
CONFIG="$APP_SUPPORT/config.json"
LOG_DIR="$APP_SUPPORT/logs"
CACHE_DIR="$HOME/Library/Caches/LocalMemorySearch"
mkdir -p "$LOG_DIR" "$CACHE_DIR"

RESOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BOOTSTRAP_LOCK_FILE="$APP_SUPPORT/.python-bootstrap-v2.lock"
PYTHON_BOOTSTRAP_LOCK_FD=""

release_python_bootstrap_lock() {
  if [ -n "${PYTHON_BOOTSTRAP_LOCK_FD:-}" ]; then
    zsystem flock -u "$PYTHON_BOOTSTRAP_LOCK_FD" 2>/dev/null || true
    PYTHON_BOOTSTRAP_LOCK_FD=""
  fi
}

acquire_python_bootstrap_lock() {
  if [ -L "$PYTHON_BOOTSTRAP_LOCK_FILE" ] || { [ -e "$PYTHON_BOOTSTRAP_LOCK_FILE" ] && [ ! -f "$PYTHON_BOOTSTRAP_LOCK_FILE" ]; }; then
    return 1
  fi
  : >>"$PYTHON_BOOTSTRAP_LOCK_FILE" || return 1
  /bin/chmod 600 "$PYTHON_BOOTSTRAP_LOCK_FILE" || return 1
  zmodload zsh/system 2>/dev/null || return 1
  # zsystem uses a kernel fcntl lock.  The descriptor is released by this
  # function or automatically on exit, forced termination, and power loss, so no
  # stale PID directory or reaper race exists.
  zsystem flock -t 120 -i 0.25 -f PYTHON_BOOTSTRAP_LOCK_FD \
    "$PYTHON_BOOTSTRAP_LOCK_FILE"
}

trap release_python_bootstrap_lock EXIT
trap 'exit 130' HUP INT TERM

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
  if ! acquire_python_bootstrap_lock; then
    print -r -- "python_bootstrap_lock_timeout" >>"$LOG_DIR/launcher.log"
    exit 1
  fi
  # Another click may have completed installation while this one waited.
  PYTHON="$(find_python || true)"
  if [ -z "$PYTHON" ]; then
    ANSWER="$(/usr/bin/osascript -e 'button returned of (display dialog "初回セットアップにPythonが必要です。Python Software Foundationの公式インストーラを自動取得して開きます。" buttons {"キャンセル", "取得して開く"} default button "取得して開く" with icon caution)' 2>/dev/null || true)"
    [ "$ANSWER" = "取得して開く" ] || exit 0
    PYTHON_PKG="$CACHE_DIR/python-3.14.7-macos11.pkg"
    PYTHON_PKG_PART="$PYTHON_PKG.part.$$"
    /usr/bin/curl -fL --retry 3 --connect-timeout 20 "https://www.python.org/ftp/python/3.14.7/python-3.14.7-macos11.pkg" -o "$PYTHON_PKG_PART" >>"$LOG_DIR/launcher.log" 2>&1 || { /bin/rm -f "$PYTHON_PKG_PART"; exit 1; }
    /bin/mv "$PYTHON_PKG_PART" "$PYTHON_PKG"
    /usr/sbin/pkgutil --check-signature "$PYTHON_PKG" 2>&1 | /usr/bin/grep -qi "Python Software Foundation" || { /bin/rm -f "$PYTHON_PKG"; exit 1; }
    /usr/bin/open "$PYTHON_PKG"
    /usr/bin/osascript -e 'display dialog "表示されたPythonインストーラをクリックで完了し、その後Local Memory Searchをもう一度開いてください。" buttons {"OK"} default button "OK"'
    exit 1
  fi
fi
release_python_bootstrap_lock

# Serialize the complete setup/restart flow across repeated Finder clicks.
# The Python helper retains the fcntl descriptor while this zsh child runs and
# closes it before returning; server and setup subprocesses never inherit it.
if [ "${LOCAL_MEMORY_LAUNCH_LEASE_HELD:-}" != "1" ]; then
  exec "$PYTHON" "$RESOURCE_DIR/launcher_lease.py" \
    "$APP_SUPPORT/.launcher-v1.lock" "$0" "$@"
fi
unset LOCAL_MEMORY_LAUNCH_LEASE_HELD

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

SERVER_PROTOCOL_VERSION="local-memory-search-step5-v1"
SERVER_HEALTH_PATH="/__local_memory_health"
SERVER_SHUTDOWN_PATH="/__local_memory_shutdown"
SERVER_IDENTITY="$APP_SUPPORT/server-identity-v1.json"
SERVER_SCRIPT="$RESOURCE_DIR/local_memory_server.py"

show_startup_error() {
  print -r -- "$1" >>"$LOG_DIR/launcher.log"
  /usr/bin/osascript -e 'display dialog "Local Memory Searchを安全に起動できませんでした。詳細はlogs/launcher.logを確認してください。" buttons {"OK"} default button "OK" with icon stop' >/dev/null 2>&1 || true
}

show_legacy_server_error() {
  print -r -- "legacy_or_unverified_server_is_listening_on_port=$PORT" >>"$LOG_DIR/launcher.log"
  /usr/bin/osascript -e 'display dialog "旧版のLocal Memory Searchが動作中です。誤ったプロセスを停止しないため自動終了はしません。旧版を終了してから、このアプリをもう一度開いてください。" buttons {"OK"} default button "OK" with icon caution' >/dev/null 2>&1 || true
}

EXPECTED_SERVER_BUILD_ID="$(PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -c 'import sys
sys.path.insert(0,sys.argv[1])
import local_memory_server
print(local_memory_server.SERVER_BUILD_ID)' "$RESOURCE_DIR" 2>>"$LOG_DIR/launcher.log" || true)"
if ! "$PYTHON" -c 'import re,sys; raise SystemExit(0 if re.fullmatch(r"[0-9a-f]{64}",sys.argv[1]) else 1)' "$EXPECTED_SERVER_BUILD_ID"; then
  show_startup_error "expected_server_build_identity_unavailable"
  exit 1
fi

health_response() {
  /usr/bin/curl --noproxy '*' -fsS --max-time 1 --max-filesize 4096 \
    "http://127.0.0.1:$PORT$SERVER_HEALTH_PATH" 2>/dev/null || true
}

health_matches_current_protocol() {
  "$PYTHON" -c 'import json,sys
try:
    value=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
required={"service","protocol_version","build_id","instance_id","graceful_restart","startup_state"}
valid=(isinstance(value,dict) and set(value)==required
    and value.get("service")=="LocalMemorySearch"
    and value.get("protocol_version")==sys.argv[2]
    and value.get("build_id")==sys.argv[3]
    and value.get("graceful_restart") is True
    and value.get("startup_state") in {"recovering","ready","failed"})
raise SystemExit(0 if valid else 1)' "$1" "$SERVER_PROTOCOL_VERSION" "$EXPECTED_SERVER_BUILD_ID"
}

health_matches_known_protocol() {
  "$PYTHON" -c 'import json,re,sys
try:
    value=json.loads(sys.argv[1])
except Exception:
    raise SystemExit(1)
required={"service","protocol_version","build_id","instance_id","graceful_restart","startup_state"}
valid=(isinstance(value,dict) and set(value)==required
    and value.get("service")=="LocalMemorySearch"
    and value.get("protocol_version")==sys.argv[2]
    and isinstance(value.get("build_id"),str)
    and re.fullmatch(r"[0-9a-f]{64}",value["build_id"])
    and isinstance(value.get("instance_id"),str)
    and re.fullmatch(r"[0-9a-f]{32}",value["instance_id"])
    and value.get("graceful_restart") is True
    and value.get("startup_state") in {"recovering","ready","failed"})
raise SystemExit(0 if valid else 1)' "$1" "$SERVER_PROTOCOL_VERSION"
}

health_field() {
  "$PYTHON" -c 'import json,sys
value=json.loads(sys.argv[1]); field=value.get(sys.argv[2])
if not isinstance(field,str): raise SystemExit(1)
print(field)' "$1" "$2" 2>/dev/null
}

validated_identity_pid() {
  "$PYTHON" -c 'import fcntl,json,os,re,stat,sys
health=json.loads(sys.argv[1])
path,port,script=sys.argv[2],int(sys.argv[3]),os.path.realpath(sys.argv[4])
lock_path=os.path.join(os.path.dirname(path),".server-identity-v1.lock")
flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)
lock_fd=os.open(lock_path,flags,0o600)
try:
    os.fchmod(lock_fd,0o600)
    fcntl.flock(lock_fd,fcntl.LOCK_SH)
    info=os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_uid!=os.getuid()
            or info.st_nlink!=1 or info.st_size>4096):
        raise SystemExit(1)
    with open(path,encoding="utf-8") as handle:
        identity=json.load(handle)
finally:
    fcntl.flock(lock_fd,fcntl.LOCK_UN)
    os.close(lock_fd)
required={"schema_version","service","protocol_version","build_id","instance_id","pid","uid","host","port","server_script","shutdown_token"}
pid=identity.get("pid")
token=identity.get("shutdown_token")
valid=(isinstance(identity,dict) and set(identity)==required
    and identity.get("schema_version")=="0.1"
    and identity.get("service")==health.get("service")=="LocalMemorySearch"
    and identity.get("protocol_version")==health.get("protocol_version")
    and identity.get("build_id")==health.get("build_id")
    and isinstance(identity.get("build_id"),str)
    and re.fullmatch(r"[0-9a-f]{64}",identity["build_id"])
    and identity.get("instance_id")==health.get("instance_id")
    and health.get("graceful_restart") is True
    and isinstance(pid,int) and not isinstance(pid,bool) and pid>1
    and identity.get("uid")==os.getuid()
    and identity.get("host")=="127.0.0.1" and identity.get("port")==port
    and os.path.realpath(identity.get("server_script",""))==script
    and isinstance(token,str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}",token))
if not valid:
    raise SystemExit(1)
print(pid)' "$1" "$SERVER_IDENTITY" "$PORT" "$SERVER_SCRIPT"
}

listener_pids() {
  /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null \
    | /usr/bin/awk 'NF && !seen[$1]++ {print $1}'
}

process_matches_server() {
  PROCESS_UID="$(/bin/ps -o uid= -p "$1" 2>/dev/null | /usr/bin/tr -d '[:space:]')"
  PROCESS_COMMAND="$(/bin/ps -ww -o command= -p "$1" 2>/dev/null || true)"
  CURRENT_UID="$(/usr/bin/id -u)"
  [ "$PROCESS_UID" = "$CURRENT_UID" ] && "$PYTHON" -c 'import re,sys
command,script,port=sys.argv[1:]
script_ok=re.search(r"(^|\s)"+re.escape(script)+r"(\s|$)",command) is not None
port_ok=re.search(r"(^|\s)--port(?:=|\s+)"+re.escape(port)+r"(\s|$)",command) is not None
raise SystemExit(0 if script_ok and port_ok else 1)' "$PROCESS_COMMAND" "$SERVER_SCRIPT" "$PORT"
}

request_authenticated_shutdown() {
  "$PYTHON" -c 'import fcntl,json,os,sys,urllib.error,urllib.request
path,instance,port=sys.argv[1],sys.argv[2],int(sys.argv[3])
lock_path=os.path.join(os.path.dirname(path),".server-identity-v1.lock")
lock_fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0),0o600)
try:
    os.fchmod(lock_fd,0o600)
    fcntl.flock(lock_fd,fcntl.LOCK_SH)
    with open(path,encoding="utf-8") as handle:
        identity=json.load(handle)
finally:
    fcntl.flock(lock_fd,fcntl.LOCK_UN)
    os.close(lock_fd)
if identity.get("instance_id")!=instance:
    raise SystemExit(1)
request=urllib.request.Request(
    f"http://127.0.0.1:{port}/__local_memory_shutdown",
    data=b"",
    headers={"X-Local-Memory-Shutdown-Token":identity["shutdown_token"]},
    method="POST",
)
try:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=2) as response:
        body=json.loads(response.read().decode("utf-8"))
        if response.status!=202 or body.get("status")!="shutting_down":
            raise SystemExit(1)
except urllib.error.HTTPError as error:
    raise SystemExit(9 if error.code==409 else 1)' "$SERVER_IDENTITY" "$1" "$PORT"
}

stop_validated_server() {
  if request_authenticated_shutdown "$2"; then
    :
  else
    return $?
  fi
  for attempt in {1..20}; do
    if ! /bin/ps -p "$1" >/dev/null 2>&1 && [ -z "$(listener_pids || true)" ]; then
      return 0
    fi
    /bin/sleep 0.25
  done
  return 8
}

PORT="$("$PYTHON" -c 'import json,os
p=os.path.expanduser("~/Library/Application Support/LocalMemorySearch/config.json")
value=json.load(open(p,encoding="utf-8")).get("port",8765)
if not isinstance(value,int) or isinstance(value,bool) or not 1<=value<=65535:
    raise SystemExit(1)
print(value)' 2>>"$LOG_DIR/launcher.log" || true)"
case "$PORT" in
  ''|*[!0-9]*) show_startup_error "invalid_non_numeric_port"; exit 1 ;;
esac
if (( PORT < 1 || PORT > 65535 )); then
  show_startup_error "invalid_port_range=$PORT"
  exit 1
fi

HEALTH_BODY="$(health_response)"
if health_matches_known_protocol "$HEALTH_BODY"; then
  IDENTITY_PID="$(validated_identity_pid "$HEALTH_BODY" 2>/dev/null || true)"
  LISTENER_SET="$(listener_pids || true)"
  LISTENER_COUNT="$(print -r -- "$LISTENER_SET" | /usr/bin/awk 'NF {count++} END {print count+0}')"
  LISTENER_PID="$(print -r -- "$LISTENER_SET" | /usr/bin/awk 'NF {print; exit}')"
  if [ -z "$IDENTITY_PID" ] || [ "$LISTENER_COUNT" -ne 1 ] || [ "$LISTENER_PID" != "$IDENTITY_PID" ]; then
    show_startup_error "known_protocol_identity_or_listener_verification_failed"
    exit 1
  fi
  if ! process_matches_server "$IDENTITY_PID"; then
    show_startup_error "known_protocol_process_verification_failed"
    exit 1
  fi
  HEALTH_INSTANCE="$(health_field "$HEALTH_BODY" instance_id || true)"
  if health_matches_current_protocol "$HEALTH_BODY"; then
    STARTUP_STATE="$(health_field "$HEALTH_BODY" startup_state || true)"
    if [ "$STARTUP_STATE" = "recovering" ]; then
      READY=false
      FAILED=false
      # Recovery can hash a large local index. The bound listener and
      # instance identity prevent a second click from spawning a duplicate.
      for attempt in {1..480}; do
        /bin/sleep 0.25
        HEALTH_BODY="$(health_response)"
        if ! health_matches_current_protocol "$HEALTH_BODY"; then
          continue
        fi
        OBSERVED_INSTANCE="$(health_field "$HEALTH_BODY" instance_id || true)"
        OBSERVED_PID="$(validated_identity_pid "$HEALTH_BODY" 2>/dev/null || true)"
        [ "$OBSERVED_INSTANCE" = "$HEALTH_INSTANCE" ] || continue
        [ "$OBSERVED_PID" = "$IDENTITY_PID" ] || continue
        STARTUP_STATE="$(health_field "$HEALTH_BODY" startup_state || true)"
        if [ "$STARTUP_STATE" = "ready" ]; then
          READY=true
          break
        fi
        if [ "$STARTUP_STATE" = "failed" ]; then
          FAILED=true
          break
        fi
      done
      if [ "$FAILED" = true ]; then
        if stop_validated_server "$IDENTITY_PID" "$HEALTH_INSTANCE"; then
          :
        else
          STOP_STATUS=$?
          show_startup_error "failed_server_shutdown_status=$STOP_STATUS"
          exit 1
        fi
        show_startup_error "server_startup_recovery_failed"
        exit 1
      fi
      if [ "$READY" != true ]; then
        show_startup_error "server_startup_recovery_timeout"
        exit 1
      fi
    elif [ "$STARTUP_STATE" = "failed" ]; then
      if stop_validated_server "$IDENTITY_PID" "$HEALTH_INSTANCE"; then
        :
      else
        STOP_STATUS=$?
        show_startup_error "failed_server_shutdown_status=$STOP_STATUS"
        exit 1
      fi
      show_startup_error "server_startup_recovery_failed"
      exit 1
    elif [ "$STARTUP_STATE" != "ready" ]; then
      show_startup_error "server_startup_state_invalid"
      exit 1
    fi
    /usr/bin/open "http://127.0.0.1:$PORT/"
    exit 0
  fi

  # A different build with the Step 5 handshake can be stopped without
  # probing the potentially slow home page. Unknown servers are never killed.
  if stop_validated_server "$IDENTITY_PID" "$HEALTH_INSTANCE"; then
    :
  else
    SHUTDOWN_STATUS=$?
    if [ "$SHUTDOWN_STATUS" -eq 9 ]; then
      show_startup_error "server_busy_graceful_restart_deferred"
    elif [ "$SHUTDOWN_STATUS" -eq 8 ]; then
      show_startup_error "graceful_shutdown_timeout"
    else
      show_startup_error "authenticated_graceful_shutdown_failed"
    fi
    exit 1
  fi
else
  ROOT_RESPONDS=false
  if /usr/bin/curl --noproxy '*' -sS --max-time 1 -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then
    ROOT_RESPONDS=true
  fi
  if [ "$ROOT_RESPONDS" = true ]; then
    show_legacy_server_error
    exit 1
  fi
  if [ ! -x /usr/sbin/lsof ]; then
    show_startup_error "lsof_unavailable_cannot_prove_port_free"
    exit 1
  fi
  if [ -n "$(listener_pids || true)" ]; then
    show_startup_error "non_http_listener_occupies_port"
    exit 1
  fi
fi

/usr/bin/nohup "$PYTHON" "$SERVER_SCRIPT" --port "$PORT" >>"$LOG_DIR/server-console.log" 2>&1 &
NEW_SERVER_PID=$!
STARTED=false
STARTUP_FAILED=false
NEW_SERVER_INSTANCE=""
for attempt in {1..480}; do
  HEALTH_BODY="$(health_response)"
  if health_matches_current_protocol "$HEALTH_BODY"; then
    VERIFIED_NEW_PID="$(validated_identity_pid "$HEALTH_BODY" 2>/dev/null || true)"
    if [ "$VERIFIED_NEW_PID" = "$NEW_SERVER_PID" ]; then
      NEW_SERVER_INSTANCE="$(health_field "$HEALTH_BODY" instance_id || true)"
      STARTUP_STATE="$(health_field "$HEALTH_BODY" startup_state || true)"
      if [ "$STARTUP_STATE" = "ready" ]; then
        STARTED=true
        break
      fi
      if [ "$STARTUP_STATE" = "failed" ]; then
        STARTUP_FAILED=true
        break
      fi
    fi
  fi
  /bin/sleep 0.25
done
if [ "$STARTED" != true ]; then
  if [ "$STARTUP_FAILED" = true ]; then
    if stop_validated_server "$NEW_SERVER_PID" "$NEW_SERVER_INSTANCE"; then
      show_startup_error "new_server_startup_recovery_failed"
    else
      STOP_STATUS=$?
      show_startup_error "new_failed_server_shutdown_status=$STOP_STATUS"
    fi
  else
    show_startup_error "new_server_health_or_identity_verification_failed"
  fi
  exit 1
fi
/usr/bin/open "http://127.0.0.1:$PORT/"
