<!-- Copyright (c) 2026 Weave Thinker Contributors -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Vibeweaver — Script Templates (§A2): build / start / stop / restart

**Router:** lazy companion (R6 trigger — writing scripts/config/captures).
Moved verbatim from APPENDIX.md §A2 (W26: APPENDIX crossed the 45KB one-Read
budget; D-W20c-1 sanctioned split, W21 AUDIT_PLUGIN precedent). Lint-pinned by
`tests/test_win_scripts.py`; dev-tree parity: `script/linux/*.sh` +
`script/windows/*.bat`.

## §A2 Script Templates (Default: FastAPI + React + Vite — Adapt for Other Stacks)

### Linux/macOS: `script/linux/project_build.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "[BUILD] Building frontend..."
cd "$PROJECT_DIR/frontend"
npm install
npm run build
cd "$PROJECT_DIR"

echo "[BUILD] Mounting to backend/static..."
rm -rf "$PROJECT_DIR/backend/static"
mkdir -p "$PROJECT_DIR/backend/static"
cp -r "$PROJECT_DIR/frontend/dist/"* "$PROJECT_DIR/backend/static/"

echo "[BUILD] Done. Files in backend/static:"
ls -la "$PROJECT_DIR/backend/static/"
```

### Linux/macOS: `script/linux/start.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[START] Starting FastAPI server..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_DIR/backend"

source ../.venv/bin/activate

# Read host/port from config.toml
HOST=$(python -c "import tomllib;cfg=tomllib.load(open('../config.toml','rb'));print(cfg.get('server',{}).get('host','127.0.0.1'))")
PORT=$(python -c "import tomllib;cfg=tomllib.load(open('../config.toml','rb'));print(cfg.get('server',{}).get('port',8000))")

fastapi run app/main.py --host "$HOST" --port "$PORT" &
echo $! > .pid
echo "[START] Server started. PID: $(cat .pid)"
```

### Linux/macOS: `script/linux/stop.sh`

> ⚠ HOST-SAFETY: never replace this with `pkill -f "uvicorn app.main"` —
> pattern-kill hits unrelated services on shared hosts. Always kill the PID
> recorded in `.pid` by start.sh.

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ -f "$PROJECT_DIR/backend/.pid" ]; then
    PID=$(cat "$PROJECT_DIR/backend/.pid")
    echo "[STOP] Stopping server (PID: $PID)..."
    kill "$PID" || true
    rm "$PROJECT_DIR/backend/.pid"
    echo "[STOP] Server stopped."
else
    echo "[STOP] No PID file found."
fi
```

### Linux/macOS: `script/linux/restart.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

echo "[RESTART] Restarting server..."
bash "$(dirname "$0")/stop.sh"
sleep 2
bash "$(dirname "$0")/start.sh"
echo "[RESTART] Done."
```

### Windows rules (hard-won; lint-pinned by `tests/test_win_scripts.py`)

- ship `.bat` **CRLF** (repo `.gitattributes` pins batch bytes: `*.bat -text` +
  physical CRLF) — an LF `.bat` dies in cmd.exe (label/goto class failures);
- anchor every script with `cd /D "%~dp0"` (works from any cwd, any drive);
- propagate failures: `|| exit /b 1` / `if errorlevel 1 exit /b 1`, final
  `exit /b %ERRORLEVEL%` — never bare `exit`;
- directory copy via `robocopy <src> <dst> /E` — success is exit **0-7**:
  guard `if errorlevel 8 exit /b 1`; never `xcopy` (deprecated);
- stop/restart kill ONLY the recorded PID: `taskkill /PID <pid> /T /F`;
  `taskkill /IM` / `Stop-Process -Name` are pattern-kills = HOST-SAFETY
  violation (same class as `pkill -f`);
- find Python 3 via `py -3` → `python` → `python3` fallback — Windows has no
  reliable `python3` (App Execution Alias conflicts).

### Windows: `script/windows/lifecycle.py`

Shared core (stdlib-only) — the `.bat` shims delegate here so spawn/pid/kill
logic lives in ONE testable place. Runs on POSIX too. Behavior-tested by
`tests/test_win_scripts.py`, which executes THIS template.

```python
#!/usr/bin/env python3
"""vibeweaver lifecycle core — one testable place for the dangerous parts.

start — spawn the server detached and record ONLY its PID in the pid file
stop   — kill ONLY the recorded PID (HOST-SAFETY: never pattern-kill)
config — read host/port from config.toml (tomllib -> tomli -> regex fallback)
"""
import argparse
import os
import re
import signal
import subprocess
import sys


def read_config(path, key):
    """server.host / server.port with safe defaults; never raises."""
    host, port = "127.0.0.1", "8000"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            try:
                import tomli as tomllib
            except ImportError:
                tomllib = None  # D-W26-2: last-resort [server] scan below
        if tomllib is not None:
            srv = tomllib.loads(raw.decode("utf-8")).get("server", {})
            host = str(srv.get("host", host))
            port = str(srv.get("port", port))
        else:
            sec = re.search(b"\\[server\\](.*?)(\\n\\[|\\Z)", raw, re.S)
            if sec:
                m = re.search(b"host\\s*=\\s*[\"']?([^\"'\\s#]+)", sec.group(1))
                if m:
                    host = m.group(1).decode()
                m = re.search(b"port\\s*=\\s*[\"']?(\\d+)", sec.group(1))
                if m:
                    port = m.group(1).decode()
    except (OSError, ValueError):  # ValueError covers TOML/decode errors
        pass
    return {"host": host, "port": port}[key]


def cmd_start(args):
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("[START] usage: lifecycle.py start [--pid-file P] [--cwd D] -- <server command>")
        return 2
    # Server output goes to a log next to the pid file: a detached child must
    # NOT inherit the caller's pipes (wrappers capturing stdout would hang).
    log_path = args.pid_file + ".log"
    log = open(log_path, "ab")
    try:
        if os.name == "nt":
            proc = subprocess.Popen(cmd, cwd=args.cwd, close_fds=True,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=(
                                        subprocess.DETACHED_PROCESS |
                                        subprocess.CREATE_NEW_PROCESS_GROUP))
        else:
            proc = subprocess.Popen(cmd, cwd=args.cwd, start_new_session=True,
                                    stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()
    with open(args.pid_file, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    print("[START] Server started. PID: %d (log: %s)" % (proc.pid, log_path))
    return 0


def cmd_stop(args):
    if not os.path.exists(args.pid_file):
        print("[STOP] No PID file found.")
        return 0
    with open(args.pid_file, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not re.fullmatch(r"\d+", raw):
        print("[STOP] Refusing: %r is not a PID (file kept for inspection)." % raw)
        return 1
    pid = int(raw)
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("[STOP] PID %d already gone." % pid)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            print("[STOP] PID %d already gone." % pid)
    os.remove(args.pid_file)
    print("[STOP] Server stopped.")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description="vibeweaver lifecycle core")
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start")
    p.add_argument("--pid-file", required=True)
    p.add_argument("--cwd", default=None)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p = sub.add_parser("stop")
    p.add_argument("--pid-file", required=True)
    p = sub.add_parser("config")
    p.add_argument("--config", required=True)
    p.add_argument("--key", choices=("host", "port"), required=True)
    args = ap.parse_args(argv)
    if args.action == "start":
        return cmd_start(args)
    if args.action == "stop":
        return cmd_stop(args)
    print(read_config(args.config, args.key))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

### Windows: `script/windows/project_build.bat`

```batch
@echo off
setlocal
rem project root as sane cwd (every path below is %~dp0-anchored anyway)
cd /D "%~dp0..\.."
echo [BUILD] Building frontend...
cd /D "%~dp0..\frontend"
call npm install
if errorlevel 1 exit /b 1
call npm run build
if errorlevel 1 exit /b 1
echo [BUILD] Mounting to backend\static...
if exist "%~dp0..\backend\static" rmdir /s /q "%~dp0..\backend\static"
mkdir "%~dp0..\backend\static"
robocopy "%~dp0..\frontend\dist" "%~dp0..\backend\static" /E /NFL /NDL /NJH /NJS
if errorlevel 8 (echo [BUILD] robocopy failed & exit /b 1)
echo [BUILD] Done.
dir /b "%~dp0..\backend\static"
exit /b 0
```

### Windows: `script/windows/start.bat`

```batch
@echo off
setlocal
cd /D "%~dp0"
rem Find Python 3: py launcher first (Windows has no reliable python3)
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if not defined PY (echo [ERR] Python 3 not found on PATH & exit /b 1)
for /f "usebackq delims=" %%h in (`%PY% "%~dp0lifecycle.py" config --config "%~dp0..\config.toml" --key host`) do set "VW_HOST=%%h"
for /f "usebackq delims=" %%p in (`%PY% "%~dp0lifecycle.py" config --config "%~dp0..\config.toml" --key port`) do set "VW_PORT=%%p"
if not defined VW_HOST set "VW_HOST=127.0.0.1"
if not defined VW_PORT set "VW_PORT=8000"
%PY% "%~dp0lifecycle.py" start --pid-file "%~dp0..\backend\.pid" --cwd "%~dp0..\backend" -- fastapi run app/main.py --host %VW_HOST% --port %VW_PORT%
exit /b %ERRORLEVEL%
```

### Windows: `script/windows/stop.bat`

```batch
@echo off
setlocal
cd /D "%~dp0"
rem Find Python 3 (see start.bat)
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY python3 -c "import sys" >nul 2>nul && set "PY=python3"
if not defined PY (echo [ERR] Python 3 not found on PATH & exit /b 1)
%PY% "%~dp0lifecycle.py" stop --pid-file "%~dp0..\backend\.pid"
exit /b %ERRORLEVEL%
```

### Windows: `script/windows/restart.bat`

```batch
@echo off
setlocal
cd /D "%~dp0"
call "%~dp0stop.bat"
if errorlevel 1 exit /b 1
rem portable 2s delay (timeout breaks under output redirection)
ping -n 3 127.0.0.1 >nul
call "%~dp0start.bat"
exit /b %ERRORLEVEL%
```

