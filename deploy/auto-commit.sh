#!/bin/bash
# Auto-commit and push AI Platform changes
REPO="/root/project/ai/claude/projects/active/ai-platform"
cd "$REPO" || exit 1

# Only commit if there are changes
if [[ -z $(git status --porcelain) ]]; then
    exit 0
fi

git add -A
git commit -m "auto: periodic sync $(date '+%Y-%m-%d %H:%M')" --no-verify
git push origin main --no-verify
