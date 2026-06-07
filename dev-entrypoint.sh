#!/bin/bash
# claudebot container entrypoint: start the md viewer + a cloudflared quick
# tunnel, announce the tunnel URL to the Discord channel, then exec the real
# command (claude). The announce flow runs in the background so claude starts
# immediately even if the tunnel is slow or fails.
{
    MD_URL="http://127.0.0.1:8085"
    # serve the project directory (docker -w puts us there); flags go first
    md -depth 5 -listen 0.0.0.0:8085 . >/tmp/md.log 2>&1 &
    for _ in $(seq 50); do
        curl -s -o /dev/null --max-time 2 "$MD_URL/" && break
        sleep 0.2
    done
    # http2: QUIC suffers from UDP buffer limits inside containers (tunnel
    # registers but the data path stalls)
    cloudflared tunnel --no-autoupdate --protocol http2 --url "$MD_URL" \
        >/tmp/cloudflared.log 2>&1 &
    TUNNEL=""
    for _ in $(seq 150); do
        TUNNEL=$(grep -om1 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' /tmp/cloudflared.log) && break
        sleep 0.2
    done
    READY=""
    if [ -n "$TUNNEL" ]; then
        # The subdomain is brand-new: querying it too early gets an NXDOMAIN
        # that resolvers negative-cache, poisoning every later retry. Give
        # Cloudflare DNS a moment, then check over DoH to bypass the local
        # resolver chain entirely.
        sleep 10
        for _ in $(seq 60); do
            [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
                  --doh-url https://1.1.1.1/dns-query "$TUNNEL/")" = "200" ] && READY=1 && break
            sleep 2
        done
    fi
    if [ -n "$READY" ] && [ -n "$CLAUDEBOT_DISCORD_TOKEN" ] && [ -n "$CLAUDEBOT_CHANNEL_ID" ]; then
        curl -s -X POST "https://discord.com/api/v10/channels/$CLAUDEBOT_CHANNEL_ID/messages" \
            -H "Authorization: Bot $CLAUDEBOT_DISCORD_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"content\":\"🌐 md viewer: $TUNNEL\"}" >/dev/null
    fi
} &
exec "$@"
