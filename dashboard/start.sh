#!/bin/bash
set -e

exec streamlit run dashboard/app.py \
  --server.port "${PORT:-8080}" \
  --server.headless true \
  --server.address 0.0.0.0 \
  --browser.gatherUsageStats false
