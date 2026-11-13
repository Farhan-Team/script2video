@echo off
REM Start the web UI and give it a free public https link via Cloudflare Tunnel.
REM Install cloudflared first:  winget install --id Cloudflare.cloudflared
cd /d %~dp0
set S2V_HEADLESS=1
start "script2video" python app.py
timeout /t 6 >nul
cloudflared tunnel --url http://127.0.0.1:7860
