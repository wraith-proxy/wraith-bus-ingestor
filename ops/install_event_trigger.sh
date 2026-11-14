#!/usr/bin/env bash
# OWNER runs this (needs sudo): sudo ops/install_event_trigger.sh
# Installs the extract service/timer and the path unit that starts the build on
# the ready marker, and disables the old 03:00 build timer in the same run.
set -euo pipefail
R=/home/thomaspeoples/pjt-nas/nas/compose-data/wraith-bus-ingestor
LOG=${LOG:-$R/ops/logs/install_event_trigger-$(date +%Y%m%d-%H%M%S).log}
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
stage() { echo "==> [$(date +%T)] $*"; }

stage "1/5 copy units to /etc/systemd/system"
for u in wraith-bus-build.service wraith-bus-extract.service wraith-bus-extract.timer wraith-bus-extract.path; do
  cp "$R/ops/systemd/$u" /etc/systemd/system/$u
done
stage "2/5 daemon-reload"
systemctl daemon-reload
stage "3/5 disable the old build timer"
systemctl disable --now wraith-bus-build.timer
stage "4/5 enable the extract timer and the path unit"
systemctl enable --now wraith-bus-extract.timer
systemctl enable --now wraith-bus-extract.path
stage "5/5 verify"
systemctl is-enabled wraith-bus-extract.timer wraith-bus-extract.path
! systemctl is-enabled --quiet wraith-bus-build.timer
stage "done; log at $LOG"
