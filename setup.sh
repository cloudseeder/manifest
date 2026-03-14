#!/usr/bin/env bash
set -euo pipefail

# Manifest Setup Script
# Creates launchd plist files for all services and loads them.
# Run from the repo root: ./setup.sh

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$HOME/.oap-venv"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
USER_NAME="$(whoami)"

echo "=== Manifest Service Setup ==="
echo "Repo:  $REPO_DIR"
echo "Venv:  $VENV_DIR"
echo "User:  $USER_NAME"
echo ""

# --- Verify prerequisites ---

if [ ! -d "$VENV_DIR" ]; then
    echo "ERROR: Virtual environment not found at $VENV_DIR"
    echo "Create it first: \$(brew --prefix python@3.12)/bin/python3.12 -m venv $VENV_DIR"
    exit 1
fi

for cmd in oap-api oap-agent-api oap-reminder-api oap-email-api; do
    if [ ! -f "$VENV_DIR/bin/$cmd" ]; then
        echo "ERROR: $cmd not found in $VENV_DIR/bin/"
        echo "Install services first:"
        echo "  source $VENV_DIR/bin/activate"
        echo "  pip install -e discovery"
        echo "  pip install -e agent"
        echo "  pip install -e reminder"
        echo "  pip install -e email"
        exit 1
    fi
done

if ! command -v ollama &>/dev/null; then
    echo "ERROR: ollama not found. Install from https://ollama.com/download"
    exit 1
fi

# --- Generate or prompt for backend secret ---

if [ -n "${OAP_BACKEND_SECRET:-}" ]; then
    SECRET="$OAP_BACKEND_SECRET"
    echo "Using OAP_BACKEND_SECRET from environment."
elif [ -f "$HOME/.oap-secret" ]; then
    SECRET="$(cat "$HOME/.oap-secret")"
    echo "Using existing secret from ~/.oap-secret"
else
    SECRET="$(openssl rand -hex 32)"
    echo "$SECRET" > "$HOME/.oap-secret"
    chmod 600 "$HOME/.oap-secret"
    echo "Generated new secret, saved to ~/.oap-secret (chmod 600)"
fi

echo ""

# --- Create LaunchAgents directory if needed ---

mkdir -p "$LAUNCH_DIR"

# --- Helper to write a plist ---

write_plist() {
    local label="$1"
    local program="$2"
    local workdir="$3"
    local include_secret="$4"
    local interval="${5:-}"
    if [ $# -ge 5 ]; then
        shift 5
        local extra_args=("$@")
    else
        local extra_args=()
    fi
    local plist_path="$LAUNCH_DIR/$label.plist"

    # Unload if already loaded
    launchctl list "$label" &>/dev/null && launchctl unload "$plist_path" 2>/dev/null || true

    local env_block=""
    if [ "$include_secret" = "yes" ]; then
        env_block="    <key>EnvironmentVariables</key>
    <dict>
        <key>OAP_BACKEND_SECRET</key>
        <string>$SECRET</string>
    </dict>"
    fi

    local schedule_block=""
    if [ -n "$interval" ]; then
        schedule_block="    <key>StartInterval</key>
    <integer>$interval</integer>"
    else
        schedule_block="    <key>KeepAlive</key>
    <true/>"
    fi

    local args_block="        <string>$program</string>"
    if [ ${#extra_args[@]} -gt 0 ]; then
        for arg in "${extra_args[@]}"; do
            args_block="$args_block
        <string>$arg</string>"
        done
    fi

    cat > "$plist_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
$args_block
    </array>
    <key>WorkingDirectory</key>
    <string>$workdir</string>
$env_block
$schedule_block
    <key>StandardOutPath</key>
    <string>/tmp/$label.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/$label.err</string>
</dict>
</plist>
PLIST

    echo "  Created $plist_path"
}

# --- Write all plist files ---

echo "Creating launchd plist files..."

write_plist "com.oap.discovery" \
    "$VENV_DIR/bin/oap-api" \
    "$REPO_DIR/discovery" \
    "yes"

write_plist "com.oap.agent" \
    "$VENV_DIR/bin/oap-agent-api" \
    "$REPO_DIR/agent" \
    "no"

write_plist "com.oap.reminder" \
    "$VENV_DIR/bin/oap-reminder-api" \
    "$REPO_DIR/reminder" \
    "no"

write_plist "com.oap.email" \
    "$VENV_DIR/bin/oap-email-api" \
    "$REPO_DIR/email" \
    "no"

write_plist "com.oap.email-scan" \
    "/usr/bin/curl" \
    "$REPO_DIR/email" \
    "no" \
    "900" \
    "-s" "-X" "POST" "http://localhost:8305/scan"

write_plist "com.oap.crawler" \
    "$VENV_DIR/bin/oap-crawl" \
    "$REPO_DIR/discovery" \
    "no" \
    "3600" \
    "--once"

echo ""

# --- Log rotation script ---

LOG_ROTATE_SCRIPT="$REPO_DIR/rotate-logs.sh"
echo "Creating log rotation script..."

cat > "$LOG_ROTATE_SCRIPT" <<'ROTATE'
#!/usr/bin/env bash
# Log rotation — copy-then-truncate (preserves launchd file descriptors)
MAX_SIZE=$((5 * 1024 * 1024))  # 5MB
KEEP=3

for logfile in /tmp/com.oap.*.log /tmp/com.oap.*.err; do
    [ -f "$logfile" ] || continue
    size=$(stat -f%z "$logfile" 2>/dev/null || echo 0)
    [ "$size" -lt "$MAX_SIZE" ] && continue

    # Shift existing archives
    i=$KEEP
    while [ $i -gt 1 ]; do
        prev=$((i - 1))
        [ -f "${logfile}.${prev}.gz" ] && mv "${logfile}.${prev}.gz" "${logfile}.${i}.gz"
        i=$prev
    done

    # Copy current to .1.gz, then truncate in place
    gzip -c "$logfile" > "${logfile}.1.gz"
    : > "$logfile"
done
ROTATE
chmod +x "$LOG_ROTATE_SCRIPT"
echo "  Created $LOG_ROTATE_SCRIPT"

# Schedule rotation via launchd (runs hourly)
write_plist "com.oap.log-rotate" \
    "$LOG_ROTATE_SCRIPT" \
    "/tmp" \
    "no" \
    "3600"
echo "  Scheduled hourly log rotation"

# Remove stale newsyslog config if present
if [ -f "/etc/newsyslog.d/oap.conf" ]; then
    echo "  Removing old /etc/newsyslog.d/oap.conf (requires sudo)..."
    sudo rm -f "/etc/newsyslog.d/oap.conf"
fi

echo ""

# --- Load all services ---

echo "Loading services..."
for label in com.oap.discovery com.oap.agent com.oap.reminder com.oap.email com.oap.email-scan com.oap.crawler com.oap.log-rotate; do
    launchctl load "$LAUNCH_DIR/$label.plist"
    echo "  Loaded $label"
done

echo ""

# --- Wait for services to start ---

echo "Waiting for services to start (up to 60s)..."

OK=0
FAIL=0
MAX_WAIT=60
INTERVAL=5
ELAPSED=0

# Build list of services to check
REMAINING="8300:Discovery:discovery:/health 8303:Agent:agent:/v1/agent/health 8304:Reminder:reminder:/reminders 8305:Email:email:/health"

while [ $ELAPSED -lt $MAX_WAIT ] && [ -n "$REMAINING" ]; do
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
    STILL_WAITING=""
    for port_name in $REMAINING; do
        port="$(echo "$port_name" | cut -d: -f1)"
        name="$(echo "$port_name" | cut -d: -f2)"
        path="$(echo "$port_name" | cut -d: -f4)"
        if curl -sf -H "X-Backend-Token: $SECRET" "http://localhost:$port$path" >/dev/null 2>&1; then
            echo "  $name (:$port) — OK (${ELAPSED}s)"
            OK=$((OK + 1))
        else
            STILL_WAITING="$STILL_WAITING $port_name"
        fi
    done
    REMAINING="$(echo "$STILL_WAITING" | xargs)"
done

# Mark anything still not responding as failed
for port_name in $REMAINING; do
    port="$(echo "$port_name" | cut -d: -f1)"
    name="$(echo "$port_name" | cut -d: -f2)"
    label="$(echo "$port_name" | cut -d: -f3)"
    echo "  $name (:$port) — FAILED after ${MAX_WAIT}s (check /tmp/com.oap.$label.err)"
    FAIL=$((FAIL + 1))
done

echo ""
echo "=== Setup complete: $OK healthy, $FAIL failed ==="
echo ""
echo "Backend secret: saved in ~/.oap-secret"
echo ""
echo "Open http://localhost:8303 to start chatting."
echo ""
echo "Logs:"
echo "  tail -f /tmp/com.oap.discovery.log"
echo "  tail -f /tmp/com.oap.agent.log"
echo "  tail -f /tmp/com.oap.reminder.log"
echo "  tail -f /tmp/com.oap.email.log"
echo "  tail -f /tmp/com.oap.crawler.log"
