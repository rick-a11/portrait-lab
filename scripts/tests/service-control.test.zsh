#!/usr/bin/env zsh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVICE="$PROJECT_ROOT/scripts/portrait-lab-service.zsh"
RUNNER="$PROJECT_ROOT/scripts/tests/fixtures/fake-api-runner.zsh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/portrait-lab-service-test.XXXXXX")"
TEST_SIGNATURE="$TEST_ROOT/portrait-lab-test-api"
EXTERNAL_SIGNATURE="$TEST_ROOT/portrait-lab-external-api"
export PORTRAIT_LAB_TEST_SIGNATURE="$TEST_SIGNATURE"
export PORTRAIT_LAB_LAUNCH_MODE="direct"
external_pid=""
external_log="$TEST_ROOT/external-api.log"
launch_target=""
launch_agent_file=""

cleanup() {
  local pid_file="$TEST_ROOT/.runtime/portrait-lab-api.pid"
  local port_file="$TEST_ROOT/.runtime/portrait-lab-api.port"
  local log_file="$TEST_ROOT/.runtime/portrait-lab-api.log"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(<"$pid_file")"
    if [[ "$pid" == <-> ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
    rm "$pid_file"
  fi
  [[ -f "$port_file" ]] && rm "$port_file"
  [[ -f "$log_file" ]] && rm "$log_file"
  if [[ -n "$launch_target" ]]; then
    launchctl bootout "$launch_target" >/dev/null 2>&1 || true
  fi
  [[ -f "$launch_agent_file" ]] && rm "$launch_agent_file"
  if [[ -n "$external_pid" ]] && kill -0 "$external_pid" 2>/dev/null; then
    kill -TERM "$external_pid" 2>/dev/null || true
  fi
  [[ -f "$external_log" ]] && rm "$external_log"
  rmdir "$TEST_ROOT/.runtime/liveportrait/assets/examples/driving" 2>/dev/null || true
  rmdir "$TEST_ROOT/.runtime/liveportrait/assets/examples" 2>/dev/null || true
  rmdir "$TEST_ROOT/.runtime/liveportrait/assets" 2>/dev/null || true
  rmdir "$TEST_ROOT/.runtime/liveportrait" 2>/dev/null || true
  [[ -d "$TEST_ROOT/.runtime" ]] && rmdir "$TEST_ROOT/.runtime"
  rmdir "$TEST_ROOT"
}
trap cleanup EXIT

PORT="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$SERVICE" start
)"

pid_file="$TEST_ROOT/.runtime/portrait-lab-api.pid"
[[ "$output" == *"已启动"* ]]
[[ -f "$pid_file" ]]
pid="$(<"$pid_file")"
[[ "$pid" == <-> ]]
kill -0 "$pid"
curl --fail --silent "http://127.0.0.1:$PORT/api/health" | grep -q '"status": "ok"'

status_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$SERVICE" status
)"
[[ "$status_output" == *"正在运行且健康"* ]]

stop_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$SERVICE" stop
)"
[[ "$stop_output" == *"已停止"* ]]
[[ ! -f "$pid_file" ]]
! kill -0 "$pid" 2>/dev/null
! curl --fail --silent --max-time 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1

start_command="$PROJECT_ROOT/Start Portrait Lab.command"
status_command="$PROJECT_ROOT/Check Portrait Lab Status.command"
stop_command="$PROJECT_ROOT/Stop Portrait Lab.command"
drivers_command="$PROJECT_ROOT/Open Driving Clips.command"

wrapper_start_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$start_command"
)"
[[ "$wrapper_start_output" == *"已启动"* ]]

wrapper_status_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$status_command"
)"
[[ "$wrapper_status_output" == *"正在运行且健康"* ]]

wrapper_stop_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$stop_command"
)"
[[ "$wrapper_stop_output" == *"已停止"* ]]

mkdir -p "$TEST_ROOT/.runtime/liveportrait/assets/examples/driving"
drivers_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_OPEN_COMMAND=printf \
  "$drivers_command"
)"
[[ "$drivers_output" == *".runtime/liveportrait/assets/examples/driving"* ]]
[[ "$drivers_output" == *"Opened the local LivePortrait driving-clips folder"* ]]

port_pair="$(python3 - <<'PY'
import socket

for port in range(49152, 60000):
    first = socket.socket()
    second = socket.socket()
    try:
        first.bind(("127.0.0.1", port))
        second.bind(("127.0.0.1", port + 1))
    except OSError:
        continue
    finally:
        first.close()
        second.close()
    print(f"{port} {port + 1}")
    break
else:
    raise SystemExit("No free consecutive ports available")
PY
)"
primary_port="${port_pair%% *}"
fallback_port="${port_pair##* }"

PORTRAIT_LAB_PORT="$primary_port" PORTRAIT_LAB_TEST_SIGNATURE="$EXTERNAL_SIGNATURE" "$RUNNER" > "$external_log" 2>&1 &
external_pid=$!
for _ in {1..20}; do
  if curl --fail --silent --max-time 1 "http://127.0.0.1:$primary_port/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
curl --fail --silent "http://127.0.0.1:$primary_port/api/health" >/dev/null

fallback_start_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PRIMARY_PORT="$primary_port" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$SERVICE" start
)"
[[ "$fallback_start_output" == *":$fallback_port/api/health"* ]]
[[ "$(<"$TEST_ROOT/.runtime/portrait-lab-api.port")" == "$fallback_port" ]]

fallback_stop_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PRIMARY_PORT="$primary_port" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  "$SERVICE" stop
)"
[[ "$fallback_stop_output" == *"已停止"* ]]

LAUNCH_PORT="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
LAUNCH_LABEL="com.portraitlab.test.${RANDOM}.${RANDOM}"
LAUNCH_TARGET="gui/$(id -u)/$LAUNCH_LABEL"
launch_target="$LAUNCH_TARGET"
launch_agent_file="$TEST_ROOT/.runtime/$LAUNCH_LABEL.plist"
TEST_LAUNCH_PATH="$TEST_ROOT/test-bin:$PATH"

launch_start_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$LAUNCH_PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  PORTRAIT_LAB_LAUNCH_MODE="launchctl" \
  PORTRAIT_LAB_LAUNCH_LABEL="$LAUNCH_LABEL" \
  PORTRAIT_LAB_LAUNCH_PATH="$TEST_LAUNCH_PATH" \
  "$SERVICE" start
)"
[[ "$launch_start_output" == *"已启动"* ]]
launchctl print "$LAUNCH_TARGET" >/dev/null
[[ "$(plutil -extract EnvironmentVariables.PATH raw "$launch_agent_file")" == "$TEST_LAUNCH_PATH" ]]

launch_stop_output="$(
  PORTRAIT_LAB_PROJECT_ROOT="$TEST_ROOT" \
  PORTRAIT_LAB_PORT="$LAUNCH_PORT" \
  PORTRAIT_LAB_RUNNER="$RUNNER" \
  PORTRAIT_LAB_PROCESS_SIGNATURE="$TEST_SIGNATURE" \
  PORTRAIT_LAB_LAUNCH_MODE="launchctl" \
  PORTRAIT_LAB_LAUNCH_LABEL="$LAUNCH_LABEL" \
  PORTRAIT_LAB_LAUNCH_PATH="$TEST_LAUNCH_PATH" \
  "$SERVICE" stop
)"
[[ "$launch_stop_output" == *"已停止"* ]]
! launchctl print "$LAUNCH_TARGET" >/dev/null 2>&1

print "PASS: scripts, double-click commands, port fallback, and launchd lifecycle manage Portrait Lab"
