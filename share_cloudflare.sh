#!/usr/bin/env bash
# Start the web UI and give it a free public https link via Cloudflare Tunnel.
# Needs cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cd "$(dirname "$0")"
S2V_HEADLESS=1 python3 app.py &
APP=$!
trap "kill $APP" EXIT
sleep 5
cloudflared tunnel --url http://127.0.0.1:7860
