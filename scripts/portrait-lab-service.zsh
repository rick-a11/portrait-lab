#!/usr/bin/env zsh
set -euo pipefail

DEFAULT_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOT="${PORTRAIT_LAB_PROJECT_ROOT:-$DEFAULT_PROJECT_ROOT}"
REQUESTED_PORT="${PORTRAIT_LAB_PORT:-}"
PRIMARY_PORT="${PORTRAIT_LAB_PRIMARY_PORT:-5000}"
LAUNCH_MODE="${PORTRAIT_LAB_LAUNCH_MODE:-launchctl}"
LAUNCH_LABEL="${PORTRAIT_LAB_LAUNCH_LABEL:-com.portrait-lab.api}"
LAUNCH_PATH="${PORTRAIT_LAB_LAUNCH_PATH:-$PATH}"
LAUNCH_DOMAIN="gui/$(id -u)"
LAUNCH_TARGET="$LAUNCH_DOMAIN/$LAUNCH_LABEL"
RUNTIME_DIR="$PROJECT_ROOT/.runtime"
PID_FILE="$RUNTIME_DIR/portrait-lab-api.pid"
PORT_FILE="$RUNTIME_DIR/portrait-lab-api.port"
LOG_FILE="$RUNTIME_DIR/portrait-lab-api.log"
LAUNCH_AGENT_FILE="$RUNTIME_DIR/$LAUNCH_LABEL.plist"
RUNNER="${PORTRAIT_LAB_RUNNER:-$PROJECT_ROOT/scripts/run-gfpgan-api.sh}"
PROCESS_SIGNATURE="${PORTRAIT_LAB_PROCESS_SIGNATURE:-$PROJECT_ROOT/.venv-gfpgan/bin/python -m flask --app backend.app}"

is_valid_port() {
  [[ "$1" == <-> ]] && (( $1 >= 1 && $1 <= 65535 ))
}

set_port() {
  PORT="$1"
  HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
}

saved_port() {
  local value
  [[ -f "$PORT_FILE" ]] || return 1
  value="$(<"$PORT_FILE")"
  is_valid_port "$value" || return 1
  print "$value"
}

if ! is_valid_port "$PRIMARY_PORT"; then
  print -u2 "无效的默认端口：$PRIMARY_PORT"
  exit 64
fi

case "$LAUNCH_MODE" in
  direct | launchctl) ;;
  *)
    print -u2 "未知的启动模式：$LAUNCH_MODE"
    exit 64
    ;;
esac

if [[ -n "$REQUESTED_PORT" ]]; then
  if ! is_valid_port "$REQUESTED_PORT"; then
    print -u2 "无效的指定端口：$REQUESTED_PORT"
    exit 64
  fi
  PORT_WAS_EXPLICIT=1
  set_port "$REQUESTED_PORT"
else
  PORT_WAS_EXPLICIT=0
  set_port "$(saved_port || print "$PRIMARY_PORT")"
fi

is_healthy() {
  curl --fail --silent --max-time 2 "$HEALTH_URL" >/dev/null 2>&1
}

is_managed_pid() {
  local pid="$1"
  local command_line

  [[ "$pid" == <-> ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command_line" == *"$PROCESS_SIGNATURE"* ]]
}

first_managed_pid() {
  local pid command_line
  while read -r pid command_line; do
    if [[ "$command_line" == *"$PROCESS_SIGNATURE"* ]] && is_managed_pid "$pid"; then
      print "$pid"
      return 0
    fi
  done < <(ps -ax -o pid= -o command=)
  return 1
}

port_number_is_busy() {
  lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

port_is_busy() {
  port_number_is_busy "$PORT"
}

next_available_port() {
  local candidate
  for candidate in $((PRIMARY_PORT + 1)) $((PRIMARY_PORT + 2)) $((PRIMARY_PORT + 3)); do
    (( candidate <= 65535 )) || continue
    if ! port_number_is_busy "$candidate"; then
      print "$candidate"
      return 0
    fi
  done
  return 1
}

clear_runtime_markers() {
  [[ -f "$PID_FILE" ]] && rm "$PID_FILE"
  [[ -f "$PORT_FILE" ]] && rm "$PORT_FILE"
  [[ -f "$LAUNCH_AGENT_FILE" ]] && rm "$LAUNCH_AGENT_FILE"
  return 0
}

launch_agent_is_loaded() {
  [[ "$LAUNCH_MODE" == "launchctl" ]] && launchctl print "$LAUNCH_TARGET" >/dev/null 2>&1
}

write_launch_agent() {
  command -v plutil >/dev/null 2>&1 || {
    print -u2 "未找到 macOS 的 plutil，无法创建本地服务。"
    return 1
  }

  [[ -f "$LAUNCH_AGENT_FILE" ]] && rm "$LAUNCH_AGENT_FILE"
  plutil -create xml1 "$LAUNCH_AGENT_FILE"
  plutil -insert Label -string "$LAUNCH_LABEL" "$LAUNCH_AGENT_FILE"
  plutil -insert ProgramArguments -array "$LAUNCH_AGENT_FILE"
  plutil -insert ProgramArguments.0 -string "$RUNNER" "$LAUNCH_AGENT_FILE"
  plutil -insert WorkingDirectory -string "$PROJECT_ROOT" "$LAUNCH_AGENT_FILE"
  plutil -insert EnvironmentVariables -dictionary "$LAUNCH_AGENT_FILE"
  plutil -insert EnvironmentVariables.PORTRAIT_LAB_PORT -string "$PORT" "$LAUNCH_AGENT_FILE"
  plutil -insert EnvironmentVariables.PATH -string "$LAUNCH_PATH" "$LAUNCH_AGENT_FILE"
  if [[ -n "${PORTRAIT_LAB_TEST_SIGNATURE:-}" ]]; then
    plutil -insert EnvironmentVariables.PORTRAIT_LAB_TEST_SIGNATURE -string "$PORTRAIT_LAB_TEST_SIGNATURE" "$LAUNCH_AGENT_FILE"
  fi
  plutil -insert StandardOutPath -string "$LOG_FILE" "$LAUNCH_AGENT_FILE"
  plutil -insert StandardErrorPath -string "$LOG_FILE" "$LAUNCH_AGENT_FILE"
  plutil -insert RunAtLoad -bool YES "$LAUNCH_AGENT_FILE"
  plutil -insert KeepAlive -bool YES "$LAUNCH_AGENT_FILE"
  plutil -insert ProcessType -string Background "$LAUNCH_AGENT_FILE"
  plutil -insert ThrottleInterval -integer 5 "$LAUNCH_AGENT_FILE"
  plutil -lint "$LAUNCH_AGENT_FILE" >/dev/null
}

start_launch_agent() {
  command -v launchctl >/dev/null 2>&1 || {
    print -u2 "未找到 macOS 的 launchctl，无法启动本地服务。"
    return 1
  }
  launchctl bootout "$LAUNCH_TARGET" >/dev/null 2>&1 || true
  write_launch_agent
  launchctl bootstrap "$LAUNCH_DOMAIN" "$LAUNCH_AGENT_FILE"
}

stop_launch_agent() {
  launchctl bootout "$LAUNCH_TARGET" >/dev/null 2>&1
}

start_service() {
  local existing_pid pid attempt fallback_port

  existing_pid="$(first_managed_pid || true)"
  if [[ -n "$existing_pid" ]]; then
    if is_healthy; then
      mkdir -p "$RUNTIME_DIR"
      print "$existing_pid" > "$PID_FILE"
      print "$PORT" > "$PORT_FILE"
      print "Portrait Lab 服务已启动并健康：$HEALTH_URL"
      return 0
    fi
    print -u2 "Portrait Lab 进程存在但健康检查未通过；请查看：$LOG_FILE"
    return 1
  fi

  if port_is_busy; then
    if (( PORT_WAS_EXPLICIT )); then
      print -u2 "端口 $PORT 已被其他服务占用；指定端口不会自动替换。"
      return 1
    fi
    fallback_port="$(next_available_port || true)"
    if [[ -z "$fallback_port" ]]; then
      print -u2 "默认端口 $PRIMARY_PORT 已被占用，且自动备选端口均不可用。"
      return 1
    fi
    set_port "$fallback_port"
  fi

  if port_is_busy; then
    print -u2 "端口 $PORT 已被其他服务占用；为避免误操作，未启动它。"
    return 1
  fi

  if [[ ! -x "$RUNNER" ]]; then
    print -u2 "找不到可执行的模型启动脚本：$RUNNER"
    return 1
  fi

  mkdir -p "$RUNTIME_DIR"
  pid=""
  if [[ "$LAUNCH_MODE" == "launchctl" ]]; then
    if ! start_launch_agent; then
      clear_runtime_markers
      print -u2 "无法加载 Portrait Lab 的 macOS 本地服务。"
      return 1
    fi
  else
    export PORTRAIT_LAB_PORT="$PORT"
    nohup "$RUNNER" </dev/null >> "$LOG_FILE" 2>&1 &
    pid=$!
    disown "$pid" 2>/dev/null || true
    print "$pid" > "$PID_FILE"
  fi

  for attempt in {1..30}; do
    pid="$(first_managed_pid || true)"
    if [[ -n "$pid" ]] && is_healthy; then
      print "$pid" > "$PID_FILE"
      print "$PORT" > "$PORT_FILE"
      print "Portrait Lab 服务已启动并健康：$HEALTH_URL"
      return 0
    fi
    sleep 1
  done

  if launch_agent_is_loaded; then
    stop_launch_agent || true
  elif [[ -n "$pid" ]] && is_managed_pid "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
  clear_runtime_markers
  print -u2 "Portrait Lab 未能在 30 秒内通过健康检查；日志：$LOG_FILE"
  return 1
}

status_service() {
  local existing_pid

  existing_pid="$(first_managed_pid || true)"
  if [[ -n "$existing_pid" ]]; then
    if is_healthy; then
      print "Portrait Lab 服务正在运行且健康：$HEALTH_URL（进程 $existing_pid）"
      return 0
    fi
    print -u2 "Portrait Lab 进程正在运行，但健康检查未通过；日志：$LOG_FILE"
    return 1
  fi

  if launch_agent_is_loaded; then
    print -u2 "Portrait Lab 本地服务已加载，但尚未通过健康检查；请查看：$LOG_FILE"
    return 1
  fi

  if is_healthy || port_is_busy; then
    print -u2 "端口 $PORT 有其他服务在运行；为避免误操作，不将其视为 Portrait Lab。"
    return 2
  fi

  print "Portrait Lab 服务未启动。"
  return 3
}

stop_service() {
  local existing_pid remaining_pid attempt

  existing_pid="$(first_managed_pid || true)"
  if [[ -z "$existing_pid" ]]; then
    if launch_agent_is_loaded; then
      if ! stop_launch_agent; then
        print -u2 "无法卸载 Portrait Lab 的 macOS 本地服务。"
        return 1
      fi
      clear_runtime_markers
      print "Portrait Lab 服务已停止。"
      return 0
    fi
    if is_healthy || port_is_busy; then
      print -u2 "端口 $PORT 有其他服务在运行；为避免误操作，未停止它。"
      return 2
    fi
    clear_runtime_markers
    print "Portrait Lab 服务已停止。"
    return 0
  fi

  if launch_agent_is_loaded; then
    if ! stop_launch_agent; then
      print -u2 "无法卸载 Portrait Lab 的 macOS 本地服务。"
      return 1
    fi
  else
    kill -TERM "$existing_pid" 2>/dev/null || {
      print -u2 "无法向 Portrait Lab 进程 $existing_pid 发送停止请求。"
      return 1
    }
  fi

  for attempt in {1..15}; do
    remaining_pid="$(first_managed_pid || true)"
    if [[ -z "$remaining_pid" ]]; then
      clear_runtime_markers
      print "Portrait Lab 服务已停止。"
      return 0
    fi
    sleep 1
  done

  print -u2 "Portrait Lab 仍在停止中；未强制结束进程。请稍后运行状态检查。"
  return 1
}

case "${1:-start}" in
  start)
    start_service
    ;;
  status)
    status_service
    ;;
  stop)
    stop_service
    ;;
  *)
    print -u2 "用法：$0 {start|stop|status}"
    exit 64
    ;;
esac
