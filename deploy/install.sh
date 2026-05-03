#!/bin/bash
# AI Platform — full deployment script
set -e

echo "=== AI Platform Installer ==="

# Check Python
python3 --version || { echo "Python3 required"; exit 1; }

# Install dependencies
echo "Installing Python packages..."
pip3 install fastapi uvicorn sqlalchemy aiosqlite pydantic python-jose passlib bcrypt httpx

# Set up directories
PROJECT_DIR="/root/project/ai/claude/projects/active/ai-platform"
DEPLOY_DIR="$PROJECT_DIR/deploy"

# Copy systemd service
echo "Installing systemd service..."
cp "$DEPLOY_DIR/ai-platform.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable ai-platform
systemctl restart ai-platform

# Ensure SSH is running
systemctl enable sshd --now 2>/dev/null || true

# Make user manager executable
chmod +x "$DEPLOY_DIR/user_manager.sh"

echo ""
echo "=== AI Platform Installed ==="
echo "Web:  http://139.180.220.20:8000"
echo "SSH:  ssh root@139.180.220.20"
echo ""
echo "Next steps:"
echo "  1. Set DEEPSEEK_API_KEY in /etc/systemd/system/ai-platform.service"
echo "  2. Set SECRET_KEY in /etc/systemd/system/ai-platform.service"
echo "  3. systemctl restart ai-platform"
echo "  4. Open http://139.180.220.20:8000 in browser"
