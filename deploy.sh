#!/bin/bash
# Fast deploy script for Home Assistant Entity Manager
# Copies files directly into running container via SSH
#
# Usage: ./deploy.sh [git-ref]
#   no arg   -> deploy the current working tree
#   git-ref  -> deploy a specific branch/tag/commit, e.g. ./deploy.sh feat/foo
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

# Optional first argument: a git ref (branch/tag/commit) to deploy instead of
# the current working tree. Files come from a temporary detached worktree of
# that ref; the locally built (gitignored) static/ assets are copied along.
DEPLOY_REF="${1:-}"
SOURCE_DIR="$SCRIPT_DIR"
if [ -n "$DEPLOY_REF" ]; then
    echo -e "[0/4] Preparing files from ref '$DEPLOY_REF'..."
    # Resolve the ref locally; fall back to the remote-tracking ref so a bare
    # branch name (e.g. feat/foo that only exists as origin/feat/foo) just works.
    RESOLVED_REF="$DEPLOY_REF"
    if ! git -C "$SCRIPT_DIR" rev-parse --verify --quiet "${DEPLOY_REF}^{commit}" >/dev/null; then
        if git -C "$SCRIPT_DIR" rev-parse --verify --quiet "origin/${DEPLOY_REF}^{commit}" >/dev/null; then
            RESOLVED_REF="origin/${DEPLOY_REF}"
            echo -e "${YELLOW}Note: '$DEPLOY_REF' not local, using 'origin/$DEPLOY_REF'${NC}"
        else
            echo -e "${RED}ERROR: git ref '$DEPLOY_REF' not found (also tried origin/$DEPLOY_REF)${NC}"
            exit 1
        fi
    fi
    SOURCE_DIR="$(mktemp -d)"
    cleanup() { git -C "$SCRIPT_DIR" worktree remove --force "$SOURCE_DIR" 2>/dev/null || rm -rf "$SOURCE_DIR"; }
    trap cleanup EXIT
    git -C "$SCRIPT_DIR" worktree add --quiet --detach "$SOURCE_DIR" "$RESOLVED_REF"
    # static/ is gitignored (built locally) and thus absent from the ref -- bring
    # the current build along so the deployed frontend isn't empty.
    if [ -d "$SCRIPT_DIR/static" ]; then
        cp -r "$SCRIPT_DIR/static" "$SOURCE_DIR/"
    fi
    echo -e "${GREEN}Prepared '$DEPLOY_REF' (+ current built static/)${NC}"
fi

echo -e "${YELLOW}=== Entity Manager Deploy ===${NC}"
echo "Target: $SSH_USER@$HA_HOST:$SSH_PORT"
echo "Container: $CONTAINER"
echo "Source: ${DEPLOY_REF:-working tree}"
echo ""

# Check SSH connection
echo -e "[1/5] Testing SSH connection..."
if ! ssh -q -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "exit" 2>/dev/null; then
    echo -e "${RED}ERROR: Cannot connect to $HA_HOST${NC}"
    exit 1
fi
echo -e "${GREEN}SSH OK${NC}"

# Check container is running
echo -e "[2/5] Checking container..."
if ! ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker ps -q -f name=$CONTAINER" | grep -q .; then
    echo -e "${RED}ERROR: Container $CONTAINER is not running${NC}"
    exit 1
fi
echo -e "${GREEN}Container running${NC}"

# Deploy files
echo -e "[3/5] Deploying files to container..."
( cd "$SOURCE_DIR" && tar czf - \
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
    *.py requirements.txt templates/ static/ translations/ 2>/dev/null ) | \
    ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker exec -i $CONTAINER tar xzf - -C $APP_PATH"

echo -e "${GREEN}Files deployed${NC}"

# Ensure Python dependencies are present in the container.
# deploy.sh is a volatile, test-only deploy into the running container's
# writable layer (gone on a Supervisor recreate). The base image only has the
# deps from the last add-on build, so a freshly added requirement (e.g. a new
# pip package in requirements.txt) would be missing and the app would crash on
# import. Installing here makes the deployed code actually runnable for testing;
# the durable install still comes from an add-on rebuild off main.
echo -e "[4/5] Ensuring dependencies (pip install -r requirements.txt)..."
if ! ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" \
    "docker exec $CONTAINER pip install --root-user-action=ignore -r $APP_PATH/requirements.txt"; then
    echo -e "${RED}ERROR: dependency installation failed -- aborting before restart${NC}"
    exit 1
fi
echo -e "${GREEN}Dependencies ready${NC}"

# Restart container
echo -e "[5/5] Restarting container..."
ssh -p "$SSH_PORT" "$SSH_USER@$HA_HOST" "docker restart $CONTAINER"

echo ""
echo -e "${GREEN}=== Deploy complete! ===${NC}"
echo "Container is restarting. Wait a few seconds, then test at:"
echo "  http://$HA_HOST:8123 -> Add-ons -> Entity Manager -> Open Web UI"
