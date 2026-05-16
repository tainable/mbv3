@echo off
:: Route sx_bet and polymarket through Mullvad's SOCKS5 proxy.
:: Matchbook bypasses the proxy via NO_PROXY (urllib doesn't support SOCKS5 anyway).
:: Requires Mullvad to be connected before running.
set HTTPS_PROXY=socks5h://127.0.0.1:1080
set NO_PROXY=api.matchbook.com,matchbook.com
python scan.py %*
