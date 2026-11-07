#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Market Monitor wordt gestart..."
cd /app || exit 1
exec python3 -m market_monitor --data-dir /data --port 8099
