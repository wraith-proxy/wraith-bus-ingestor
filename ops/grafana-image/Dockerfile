# backend/ holds a grafana 11.6.16 backend rebuilt from source with current Go and
# kin-openapi 0.144 (gitignored, ~380 MB). Without it, drop the three COPY lines to
# fall back to the upstream binary.
FROM grafana/grafana:11.6.16
USER root
COPY backend/grafana /usr/share/grafana/bin/grafana
COPY backend/grafana-cli /usr/share/grafana/bin/grafana-cli
COPY backend/grafana-server /usr/share/grafana/bin/grafana-server
RUN apk upgrade --no-cache
USER grafana
