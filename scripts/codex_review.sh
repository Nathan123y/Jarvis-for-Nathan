#!/bin/zsh
# Second-opinion code review by Codex on the ChatGPT Plus login (no API key, no extra cost).
#   scripts/codex_review.sh            review the latest commit
#   scripts/codex_review.sh --uncommitted
#   scripts/codex_review.sh --base main    review this branch against main
C=$(ls -d ~/.vscode/extensions/openai.chatgpt-*/bin/macos-aarch64/codex 2>/dev/null | sort | tail -1)
[ -x "$C" ] || C=$(command -v codex)
[ -x "$C" ] || { echo "Codex CLI not found"; exit 1; }
unset OPENAI_API_KEY CODEX_API_KEY
cd "$(git rev-parse --show-toplevel)" || exit 1
if [ $# -eq 0 ]; then set -- --commit HEAD; fi
exec "$C" review "$@"
