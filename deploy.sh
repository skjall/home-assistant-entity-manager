#!/bin/bash
# Fast deploy script for Home Assistant Entity Manager
# Copies files directly into running container via SSH
#
# Setup: Copy .env.example to .env and configure your settings

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env file if exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Configuration (from .env or environment)
HA_HOST="${HA_HOST:-}"
SSH_PORT="${SSH_PORT:-22}"
SSH_USER="${SSH_USER:-root}"
CONTAINER="${CONTAINER:-}"
APP_PATH="${APP_PATH:-/app}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Validate required config
if [ -z "$HA_HOST" ]; then
    echo -e "${RED}ERROR: HA_HOST not set${NC}"
    echo ""
    echo "Create a .env file with:"
    echo "  HA_HOST=your-ha-ip-address"
    echo ""
    echo "Or run with:"
    echo "  HA_HOST=192.168.1.100 ./deploy.sh"
    exit 1
fi

echo -e "${YELLOW}=== Entity Manager Deploy ===${NC}"
echo "Target: $SSH_USER@$HA_HOST:$SSH_PORT"
echo "Container: $CONTAINER"
echo ""

# Check SSH connection
echo -e "[1/4] Testing SSH connection..."
if ! ssh -q -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "exit" 2>/dev/null; then
    echo -e "${RED}ERROR: Cannot connect to $HA_HOST${NC}"
    exit 1
fi
echo -e "${GREEN}SSH OK${NC}"

# Check container is running
echo -e "[2/4] Checking container..."
if ! ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker ps -q -f name=$CONTAINER" | grep -q .; then
    echo -e "${RED}ERROR: Container $CONTAINER is not running${NC}"
    exit 1
fi
echo -e "${GREEN}Container running${NC}"

# Deploy files
echo -e "[3/4] Deploying files to container..."
tar czf - \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='node_modules' \
    --exclude='.idea' \
    --exclude='.vscode' \
    --exclude='deploy.sh' \
    --exclude='CLAUDE.md' \
    --exclude='.github' \
    --exclude='*.md' \
    --exclude='.gitignore' \
    --exclude='.pre-commit-config.yaml' \
    --exclude='pyproject.toml' \
    --exclude='.ruff.toml' \
    --exclude='.env' \
    --exclude='.env.example' \
    *.py templates/ static/ translations/ 2>/dev/null | \
    ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker exec -i $CONTAINER tar xzf - -C $APP_PATH"

echo -e "${GREEN}Files deployed${NC}"

# Restart container
echo -e "[4/4] Restarting container..."
ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker restart $CONTAINER"

echo ""
echo -e "${GREEN}=== Deploy complete! ===${NC}"
echo "Container is restarting. Wait a few seconds, then test at:"
echo "  http://$HA_HOST:8123 -> Add-ons -> Entity Manager -> Open Web UI"
