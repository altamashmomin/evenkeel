#!/usr/bin/env bash
# pi-welcome.sh — the SSH login greeting: a curated command list so you're
# oriented the moment you land. Printed by ~/.bashrc on interactive SSH login
# (see the one-time setup in deploy/CHEATSHEET.md's header notes / the repo).
# A curated SUBSET on purpose — the full reference is deploy/CHEATSHEET.md.
# Plain text so it reads clean in any terminal; no deps.

b=""; d=""; r=""
if [ -t 1 ]; then b="$(printf '\033[1m')"; d="$(printf '\033[2m')"; r="$(printf '\033[0m')"; fi

cat <<EOF

${b}Ledger — you're on the Pi (~/pifinance). Common commands:${r}

${b}Deploy${r}      deploy/deploy.sh origin/main
${b}Sync now${r}    venv/bin/python simplefin_sync.py --force        ${d}# --force only during bank setup${r}
${b}Add a bank${r}  venv/bin/python simplefin_sync.py --claim 'TOKEN'
${b}Backup${r}      deploy/nightly-backup.sh
${b}Is it up?${r}   curl -s localhost:8080/api/status
${b}Health${r}      cat ~/pifinance-ops/ops-status.txt
${b}Timers${r}      systemctl list-timers 'pifinance*'
${b}Logs${r}        journalctl -u pifinance -e                       ${d}# or -u pifinance-sync / ledger-mcp${r}
${b}Restart${r}     sudo systemctl restart pifinance ledger-mcp

${d}Full list: less ~/pifinance/deploy/CHEATSHEET.md   ·   Reset (careful): cat ~/pifinance/deploy/reset-money.md${r}
EOF
