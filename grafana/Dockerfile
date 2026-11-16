# Grafana with this repo's datasource and dashboards built in, published to
# ghcr.io/vmenon04/whoop-grafana by .github/workflows/ci.yml. The datasource reads
# WHOOP_DB_NAME and GRAFANA_DB_PASSWORD from the environment; it holds no secrets.
FROM grafana/grafana-oss:12.0.0
LABEL org.opencontainers.image.source=https://github.com/vmenon04/whoop
COPY provisioning /etc/grafana/provisioning
COPY dashboards /etc/grafana/dashboards
