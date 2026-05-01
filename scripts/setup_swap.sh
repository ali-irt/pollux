#!/usr/bin/env bash
# setup_swap.sh — add a 4 GB swap file on the host (run once as root)
#
# Why: XTTS + MusicGen + Whisper can briefly spike past available RAM during
# model load. Without swap the kernel OOM-killer kills the container.
# Swap lets the spike absorb instead of crash, at the cost of disk I/O.
#
# Usage:  sudo bash scripts/setup_swap.sh
#         sudo bash scripts/setup_swap.sh 8G   # custom size

set -euo pipefail

SWAP_SIZE="${1:-4G}"
SWAP_FILE="/swapfile"

if swapon --show | grep -q "$SWAP_FILE"; then
    echo "Swap already active on $SWAP_FILE — nothing to do."
    exit 0
fi

echo "Creating ${SWAP_SIZE} swap file at ${SWAP_FILE}..."
fallocate -l "$SWAP_SIZE" "$SWAP_FILE" 2>/dev/null \
    || dd if=/dev/zero of="$SWAP_FILE" bs=1M count="${SWAP_SIZE//[^0-9]/}000" status=progress

chmod 600 "$SWAP_FILE"
mkswap "$SWAP_FILE"
swapon "$SWAP_FILE"

# Persist across reboots
if ! grep -q "$SWAP_FILE" /etc/fstab; then
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
fi

# Reduce swap aggressiveness — only use swap when RAM is >90% full
sysctl -w vm.swappiness=10
if ! grep -q "vm.swappiness" /etc/sysctl.conf; then
    echo "vm.swappiness=10" >> /etc/sysctl.conf
fi

echo "Done. Active swap:"
swapon --show
free -h
