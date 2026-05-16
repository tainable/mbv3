@echo off
echo Starting SOCKS5 server (127.0.0.1:1081, exits via Mullvad VPN tunnel)...
start "" "C:\Program Files\Python312\pythonw.exe" "%~dp0vpn_proxy_bridge.py"
timeout /t 2 /nobreak >nul
"C:\Program Files\Python312\python.exe" -c "import socket; s=socket.create_connection(('127.0.0.1',1081),3); print('Bridge is reachable'); s.close()"
