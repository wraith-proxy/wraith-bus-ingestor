#!/usr/bin/env bash
#
# Install system updates WITHOUT touching the GPU stack.
#
#   sudo ops/safe_upgrade.sh            # hold NVIDIA, update, upgrade, verify
#   ops/safe_upgrade.sh --check         # show what it would do (no sudo, no changes)
#
# Why: an NVIDIA driver bump replaces the userspace libraries while the old
# kernel module is still loaded ("driver/library version mismatch"), and only a
# clean DKMS rebuild for the kernel you boot into fixes that. So every NVIDIA
# package (driver, DKMS source, utils, libnvidia-*, and the container toolkit
# that lets Docker use the GPU for ollama/immich) is put on `apt-mark hold`;
# everything else is upgraded with a plain `apt upgrade` (never full-upgrade, so
# nothing is removed or swapped). Kernels still update, they always have via
# unattended-upgrades, so the script confirms DKMS built the NVIDIA module for
# every installed kernel, and that the previous kernel is still installed as a
# fallback (pick it under GRUB "Advanced options" if the new one misbehaves).
#
# Upgrading docker-ce restarts dockerd, and live-restore is OFF, so every
# container (Tailscale included, its state is on a bind mount and survives)
# bounces once. Run this right before a reboot, not mid-service.
#
# To take the GPU stack later, deliberately, on a day you can reboot and check:
#   sudo apt-mark unhold $(apt-mark showhold | grep -i nvidia)
#   sudo apt-get install --only-upgrade <those packages>   # then reboot, then ops/post_boot_check.sh
#
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECK=0; [[ "${1:-}" == "--check" ]] && CHECK=1
ts() { date -u +%H:%M:%S; }
major() { echo; echo "================================================================="; echo "[$(ts)] ### $*"; echo "================================================================="; }
minor() { echo "[$(ts)]   -> $*"; }
good()  { echo "[$(ts)]   OK   $*"; }
bad()   { echo "[$(ts)]   FAIL $*"; FAILED=1; }
FAILED=0

if [[ $CHECK -eq 0 ]]; then
  [[ $EUID -eq 0 ]] || { echo "run with sudo:  sudo $0   (or --check)"; exit 1; }
  LOG="$REPO/ops/logs/safe-upgrade-$(date -u +%Y%m%dT%H%M%SZ).log"; mkdir -p "$REPO/ops/logs"
  exec > >(tee -a "$LOG") 2>&1
  echo "log: $LOG"
fi

gpu_state() {
  minor "GPU:    $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1 | head -1)"
  minor "kernel: running $(uname -r); installed $(dpkg -l 'linux-image-[0-9]*' | awk '/^ii/{print $2}' | sed 's/linux-image-//' | tr '\n' ' ')"
  dkms status 2>/dev/null | grep -i nvidia | sed 's/^/        /'
}
nvidia_pkgs() { dpkg -l | awk '/^ii/ && $2 ~ /nvidia/ {print $2}'; }

major "STAGE 1  State before"
gpu_state
minor "already on hold: $(apt-mark showhold | tr '\n' ' ')"
[[ -f /var/run/reboot-required ]] && minor "a reboot is already pending"

major "STAGE 2  Hold the NVIDIA stack"
PKGS=$(nvidia_pkgs | sort -u)
minor "$(echo "$PKGS" | wc -l) NVIDIA packages (driver, dkms, utils, libnvidia-*, container toolkit)"
if [[ $CHECK -eq 1 ]]; then minor "[--check] would hold: $(echo $PKGS | tr '\n' ' ' | cut -c1-200)..."
else
  echo "$PKGS" | xargs apt-mark hold >/dev/null && good "held"
fi

major "STAGE 3  What would be installed (simulation, before touching anything)"
[[ $CHECK -eq 0 ]] && apt-get update -qq && good "package lists refreshed"
SIM=$(apt-get -s upgrade 2>/dev/null | awk '/^Inst/{print $2}')
if [[ $CHECK -eq 1 ]]; then   # nothing is held yet in --check: show the set as it will be once they are
  minor "[--check] would be held back instead: $(echo "$SIM" | grep -i nvidia | tr '\n' ' ')"
  SIM=$(echo "$SIM" | grep -vi nvidia)
fi
minor "$(echo "$SIM" | grep -c .) package(s) would be upgraded"
echo "$SIM" | tr '\n' ' ' | fold -w 110 | sed 's/^/        /'; echo
if echo "$SIM" | grep -qiE "nvidia"; then bad "an NVIDIA package is still in the upgrade set -- refusing to continue"; exit 1; fi
good "no NVIDIA package in the upgrade set"
[[ $CHECK -eq 1 ]] && { echo; echo "  --check: nothing changed. Run for real with:  sudo $0"; exit 0; }

major "STAGE 4  apt upgrade (never full-upgrade)"
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -o Dpkg::Options::=--force-confold
rc=$?; [[ $rc -eq 0 ]] && good "apt upgrade exited 0" || bad "apt upgrade exited $rc"

major "STAGE 5  Verify the GPU stack survived"
gpu_state
for k in $(dpkg -l 'linux-image-[0-9]*' | awk '/^ii/{print $2}' | sed 's/linux-image-//'); do
  dkms status 2>/dev/null | grep -i "nvidia" | grep -q "$k.*installed" && good "NVIDIA module built for $k" || bad "NVIDIA module NOT built for $k -- do not boot it"
done
[[ $(dpkg -l 'linux-image-[0-9]*' | grep -c '^ii') -ge 2 ]] && good "a previous kernel is still installed as a fallback" || bad "only one kernel installed: no fallback"
apt-mark showhold | grep -qi nvidia && good "NVIDIA stack still held ($(apt-mark showhold | grep -ci nvidia) packages)" || bad "NVIDIA hold missing"
nvidia-smi >/dev/null 2>&1 && good "nvidia-smi answers" || bad "nvidia-smi failed"
minor "containers running: $(docker ps -q | wc -l)  (docker was upgraded: they restart on their own, give it a minute)"

major "SUMMARY"
[[ -f /var/run/reboot-required ]] && minor "reboot required: $(cat /var/run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')"
if [[ $FAILED -eq 0 ]]; then echo "  RESULT: PASS -- safe to shut down and fit the RAM."; else echo "  RESULT: FAIL -- do NOT reboot until the FAIL lines above are understood."; exit 1; fi
