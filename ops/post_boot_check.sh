#!/usr/bin/env bash
#
# After a reboot (new RAM, kernel or drivers): is everything back? No sudo, no
# changes, safe to run any time. Exit 0 = all good.
#
#   ops/post_boot_check.sh
#
# Checks: RAM, the GPU (host and inside Docker), the archive drives (the archiver
# refuses to run without its sentinel), every container is up, Tailscale and its
# serve mappings, the public site, ingest, and the timers. The bits that bite
# after a reboot: ghost-web (nginx) resolves upstream names once at start, so if
# it came up before its backends it needs `docker exec ghost-web nginx -s reload`.
#
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0
ok()   { echo "  OK    $*"; }
bad()  { echo "  FAIL  $*"; FAILED=1; }
warn() { echo "  WARN  $*"; }
sec()  { echo; echo "== $* =="; }

sec "Memory"
total_gb=$(free -g | awk 'NR==2{print $2}'); echo "  total: $(free -h | awk 'NR==2{print $2}') ($total_gb GB)   swap: $(free -h | awk 'NR==3{print $2}')"
if [[ $total_gb -ge 28 ]]; then ok "RAM looks doubled (was 15Gi)"; else warn "still ~$total_gb GB: are both new sticks seated / recognised? (sudo dmidecode -t memory | grep -E 'Size|Speed')"; fi

sec "GPU"
if nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1 | sed 's/^/  /'; then ok "nvidia-smi answers"; else bad "nvidia-smi failed (driver/library mismatch? module not built for this kernel?)"; fi
dkms status 2>/dev/null | grep -i nvidia | sed 's/^/  /'
if docker exec ollama nvidia-smi >/dev/null 2>&1; then ok "GPU visible inside Docker (ollama)"; else bad "GPU NOT visible inside Docker: check nvidia-container-toolkit / /etc/docker/daemon.json"; fi

sec "Drives"
for m in /mnt/vault1 /mnt/archive; do mountpoint -q "$m" && ok "$m mounted ($(df -h "$m" | awk 'NR==2{print $4" free"}'))" || bad "$m NOT mounted"; done
[[ -f /mnt/archive/wraith-bus-ingestor-archive/.this-is-vault2 ]] && ok "archive sentinel present" || bad "archive sentinel missing: the archiver will refuse to run"

sec "Containers (expect none stopped)"
down=$(docker ps -a --filter status=exited --filter status=created --filter status=restarting --format '{{.Names}}: {{.Status}}')
[[ -z "$down" ]] && ok "$(docker ps -q | wc -l) running, none stopped" || { echo "$down" | sed 's/^/  /'; bad "containers not running (above)"; }
unhealthy=$(docker ps --filter health=unhealthy --format '{{.Names}}'); [[ -z "$unhealthy" ]] && ok "none unhealthy" || bad "unhealthy: $unhealthy"

sec "Tailscale"
docker exec tailscale tailscale status 2>/dev/null | head -1 | sed 's/^/  /'
n=$(docker exec tailscale tailscale serve status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('Web',{})))" 2>/dev/null)
[[ "${n:-0}" -ge 7 ]] && ok "$n serve mappings present" || bad "only ${n:-0} serve mappings (expected 7): see docs/operations.md, re-add with 'tailscale serve --bg'"
docker exec tailscale tailscale serve status --json 2>/dev/null | grep -q "ghost-stack-1" && warn "stale -1 hostname in serve config" || ok "serve config on the current hostname"

sec "Public site and the stack"
for u in https://www.thomaspeoples.com/grafana/api/health https://slaycoach.quest/bus-map/ https://slaycoach.quest/api/buses https://slaycoach.quest/tiles/great-britain/0/0/0; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$u"); [[ "$code" == "200" ]] && ok "$code $u" || bad "$code $u  (if 502/000 and the containers are up: docker exec ghost-web nginx -s reload)"
done
st=$(curl -s --max-time 30 https://slaycoach.quest/api/model-info | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('marts_state'), d.get('duckdb'))" 2>/dev/null)
[[ "$st" == "ok True" ]] && ok "API marts_state/duckdb = $st (ETAs on)" || bad "API marts_state/duckdb = ${st:-unreachable}"
age=$(curl -sG "http://localhost:9001/exec" --data-urlencode "query=select datediff('s', max(timestamp), now()) from bus_positions" | python3 -c "import json,sys; print(json.load(sys.stdin)['dataset'][0][0])" 2>/dev/null)
[[ -n "$age" && "$age" -lt 300 ]] && ok "ingest is live (newest row ${age}s old)" || warn "newest bus_positions row is ${age:-unknown}s old (the feed resumes within a poll or two)"

sec "Timers"
for t in wraith-bus-build.timer wraith-bus-precleanup.timer; do systemctl is-active --quiet "$t" && ok "$t active, next: $(systemctl show "$t" -p NextElapseUSecRealtime --value | cut -c1-25)" || bad "$t not active"; done
diff -q /etc/systemd/system/wraith-bus-build.service "$REPO/ops/systemd/wraith-bus-build.service" >/dev/null 2>&1 && ok "installed build unit matches the repo" || warn "installed wraith-bus-build.service differs from the repo (sudo cp it, then daemon-reload)"

echo; [[ $FAILED -eq 0 ]] && echo "RESULT: PASS" || echo "RESULT: FAIL: see the FAIL lines"; exit $FAILED
