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

# Lets claude run with --dangerously-skip-permissions as root
ENV IS_SANDBOX=1

WORKDIR /root
CMD ["bash"]
