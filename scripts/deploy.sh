#!/bin/bash
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────
REMOTE="${DEPLOY_HOST:-ubuntu@YOUR_EC2_IP}"
DEPLOY_DIR="/opt/trade-dashboard"

echo "=== Deploying Trade Dashboard to $REMOTE ==="

# ── 1. Build frontend ────────────────────────────────────────
echo "[1/4] Building frontend..."
cd frontend
npm ci --silent
npm run build
cd ..

# ── 2. Copy frontend build into nginx directory ──────────────
echo "[2/4] Preparing nginx build..."
rm -rf nginx/dist
cp -r frontend/dist nginx/dist

# ── 3. Sync code to EC2 ─────────────────────────────────────
echo "[3/4] Syncing code to EC2..."
rsync -avz --delete \
    --exclude 'node_modules' \
    --exclude 'data' \
    --exclude '.env' \
    --exclude 'frontend/node_modules' \
    --exclude 'frontend/dist' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude 'backend/.env' \
    --exclude 'migration_checkpoint.json' \
    ./ "$REMOTE:$DEPLOY_DIR/"

# ── 4. Rebuild and restart on EC2 ───────────────────────────
echo "[4/4] Building and restarting containers..."
ssh "$REMOTE" "cd $DEPLOY_DIR && docker compose build backend nginx && docker compose up -d backend nginx"

echo "=== Deploy complete ==="
