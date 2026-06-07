# Fallback image for `claudebot --container`, used when the project has no
# Dockerfile of its own. claude runs as root so it can install whatever it
# needs. /root is bind-mounted from container-home/ on the host, so login
# state and dotfiles persist; system-level installs last for the lifetime of
# one claude session.
#
# The node base is NOT about node projects: Claude Code is an npm package, so
# the image needs node to run claude at all — node:22-bookworm is just Debian
# with node preinstalled. NOTE: install claude OUTSIDE /root (npm -g goes to
# /usr/local), because the container-home mount shadows /root at runtime.
FROM node:22-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget ca-certificates sudo build-essential procps \
    python3 python3-pip python3-venv jq ripgrep less vim nano \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# md viewer (markdown browser + live Claude transcript viewer) — served on
# 8085 and exposed through a cloudflared quick tunnel that the entrypoint
# announces to the Discord channel at startup
COPY --from=golang:1.25-bookworm /usr/local/go /usr/local/go
# cache-bust: this URL's content changes with every commit to md, so the
# clone layer below rebuilds exactly when the repo does
ADD https://api.github.com/repos/danroblewis/md/commits/main /tmp/md-head.json
RUN git clone --depth 1 https://github.com/danroblewis/md.git /opt/md \
 && cd /opt/md && /usr/local/go/bin/go build -o /usr/local/bin/md . \
 && rm -rf /opt/md /root/go /root/.cache

# cloudflared (quick tunnels need no account)
RUN curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/cloudflared

COPY dev-entrypoint.sh /usr/local/bin/dev-entrypoint.sh
RUN chmod +x /usr/local/bin/dev-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/dev-entrypoint.sh"]

# Lets claude run with --dangerously-skip-permissions as root
ENV IS_SANDBOX=1

WORKDIR /root
CMD ["bash"]
