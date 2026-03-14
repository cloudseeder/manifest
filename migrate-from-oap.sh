#!/usr/bin/env bash
set -euo pipefail

# Run this on the Mac Mini to migrate from oap-dev to manifest.
# Usage: ~/manifest/scripts/migrate-from-oap.sh

OAP_DIR="$HOME/oap-dev"
MANIFEST_DIR="$HOME/manifest"

echo "=== Migrate from oap-dev to manifest ==="
echo ""

# --- Step 1: Stop discovery and agent services ---

echo "Stopping services..."
for label in com.oap.discovery com.oap.agent com.oap.crawler; do
    if launchctl list "$label" &>/dev/null; then
        launchctl unload "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null || true
        echo "  Stopped $label"
    else
        echo "  $label not running"
    fi
done
echo ""

# --- Step 2: Copy config files (gitignored, not in the repo) ---

echo "Copying config files..."
for pair in \
    "reference/oap_discovery/config.yaml:discovery/config.yaml" \
    "reference/oap_discovery/credentials.yaml:discovery/credentials.yaml" \
    "reference/oap_agent/config.yaml:agent/config.yaml" \
    "reference/oap_email/config.yaml:email/config.yaml" \
    "reference/oap_reminder/config.yaml:reminder/config.yaml"; do
    src="$OAP_DIR/$(echo "$pair" | cut -d: -f1)"
    dst="$MANIFEST_DIR/$(echo "$pair" | cut -d: -f2)"
    if [ -f "$src" ]; then
        cp "$src" "$dst"
        echo "  $(echo "$pair" | cut -d: -f1) → $(echo "$pair" | cut -d: -f2)"
    fi
done
echo ""

# --- Step 3: Move database files ---

echo "Moving database files..."
for pair in \
    "reference/oap_discovery/oap_experience.db:discovery/oap_experience.db" \
    "reference/oap_discovery/oap_data:discovery/oap_data" \
    "reference/oap_agent/oap_agent.db:agent/oap_agent.db" \
    "reference/oap_reminder/oap_reminder.db:reminder/oap_reminder.db" \
    "reference/oap_email/oap_email.db:email/oap_email.db"; do
    src="$OAP_DIR/$(echo "$pair" | cut -d: -f1)"
    dst="$MANIFEST_DIR/$(echo "$pair" | cut -d: -f2)"
    if [ -e "$src" ]; then
        mv "$src" "$dst"
        echo "  $(echo "$pair" | cut -d: -f1) → $(echo "$pair" | cut -d: -f2)"
    fi
done
echo ""

# --- Step 4: Install packages in venv ---

echo "Installing packages..."
source "$HOME/.oap-venv/bin/activate"
pip install -e "$MANIFEST_DIR/discovery"
pip install -e "$MANIFEST_DIR/agent"
pip install -e "$MANIFEST_DIR/reminder"
pip install -e "$MANIFEST_DIR/email"
echo ""

# --- Step 5: Run setup ---

echo "Running setup.sh..."
cd "$MANIFEST_DIR"
./setup.sh
