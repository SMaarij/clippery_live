#!/usr/bin/env bash
# Bring up a free Cloudflare WARP tunnel as a local SOCKS proxy (wgcf + wireproxy) and keep getting a fresh WARP
# identity (a new exit address) until YouTube will open the given URL through it, up to N tries.
#
#   scripts/warp_until_ok.sh <youtube url> [tries=8]
#
# On success: exports WARP_SOCKS (and WARP_TRIES) to $GITHUB_ENV and exits 0. On failure: no WARP_SOCKS, exit 1.
# Needs GH_TOKEN (to look up the release downloads) and yt-dlp + node installed.
set -uo pipefail
URL="$1"; TRIES="${2:-8}"; PORT=1080
asset() { curl -sSL -H "Authorization: Bearer $GH_TOKEN" "https://api.github.com/repos/$1/releases/latest" | grep browser_download_url | grep "$2" | head -1 | cut -d '"' -f4; }
[ -x ./wgcf ] || { curl -sSL "$(asset ViRb3/wgcf linux_amd64)" -o wgcf && chmod +x wgcf; }
[ -x ./wireproxy ] || curl -sSL "$(asset windtf/wireproxy linux_amd64.tar.gz)" | tar xz wireproxy

for i in $(seq 1 "$TRIES"); do
  pkill -f "wireproxy -c wireproxy.conf" 2>/dev/null; sleep 1
  rm -f wgcf-account.toml wgcf-profile.conf
  if ! ./wgcf register --accept-tos >/dev/null 2>&1 || ! ./wgcf generate >/dev/null 2>&1; then
    echo "try $i: WARP registration failed"; sleep 5; continue
  fi
  cp wgcf-profile.conf wireproxy.conf
  printf '\n[Socks5]\nBindAddress = 127.0.0.1:%s\n' "$PORT" >> wireproxy.conf
  nohup ./wireproxy -c wireproxy.conf > wireproxy.log 2>&1 &
  sleep 4
  colo=$(curl -s --max-time 15 --socks5-hostname 127.0.0.1:$PORT https://www.cloudflare.com/cdn-cgi/trace | grep -E '^colo=' | cut -d= -f2)
  if [ -z "$colo" ]; then echo "try $i: tunnel did not answer"; continue; fi
  out=$(timeout 60 yt-dlp --no-warnings --proxy "socks5h://127.0.0.1:$PORT" --js-runtimes node --remote-components ejs:github \
        --skip-download --print "%(is_live)s %(id)s" "$URL" 2>&1 | tail -1)
  if echo "$out" | grep -qE '^(True|False) '; then
    echo "try $i: YouTube accepted this WARP address (colo $colo): $out"
    { echo "WARP_SOCKS=socks5h://127.0.0.1:$PORT"; echo "WARP_TRIES=$i"; } >> "${GITHUB_ENV:-/dev/null}"
    exit 0
  fi
  echo "try $i: blocked (colo $colo): $(echo "$out" | cut -c1-110)"
done
pkill -f "wireproxy -c wireproxy.conf" 2>/dev/null
echo "no WARP address YouTube accepts after $TRIES tries"
exit 1
