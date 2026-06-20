# claudebot

A personal Discord ↔ Claude Code bridge. `cd` into any project and run
`claudebot`: it starts an interactive `claude` session in tmux, attaches you to
it, and bridges it to a Discord channel — messages you post there are pasted
into the session, and Claude's prose replies are relayed back by tailing the
JSONL transcript under `~/.claude/projects`.

## Usage

```sh
cd ~/my_project
claudebot [options] [claude options...]   # start (or re-attach) and attach
claudebot --continue                      # resume this dir's latest session
claudebot --resume <session-id>           # resume a specific session
claudebot --stop                          # tear everything down
```

Detaching (`ctrl-b d`) never loses anything — the session keeps running and
`claudebot` re-attaches. After a real `--stop` (or reboot), `claudebot`
starts fresh; use `--continue` to pick the old conversation back up (the
session id is shown by `!status` and in `~/.claudebot-sessions.json`).

Unrecognized options are passed through to claude
(e.g. `claudebot --model opus`). Detach with `ctrl-b d`; everything keeps
running and stays usable from Discord.

Each project runs its **own** session: the tmux session name defaults to the
working directory's name, so `claudebot` in `~/foo` and `~/bar` run
concurrently (sessions `foo` and `bar`), each bridging its own channel.
Running `claudebot` again in the same directory re-attaches. If two different
directories would collide on a name (or you want two sessions in one dir),
pass `--tmux-session NAME`. claudebot will **refuse** to start over a session
of the same name running a different directory rather than kill it — stop that
one first with `claudebot --stop`.

## Configuration

KEY=VALUE lines, resolved in order (later wins):

1. `~/.claudebot` — global defaults (typically `DISCORD_TOKEN`, `USER_ID`)
2. `./.claudebot` — per-project (typically `CHANNEL_ID`, `TMUX_SESSION`);
   add it to the project's `.gitignore`
3. CLI flags: `--discord-token`, `--channel-id`, `--user-id`, `--tmux-session`

You can also bind by **name** instead of ID: `claudebot --channel evident`
looks the name up via the bot token across the bot's guilds (text channels
and active threads; errors with a list if the name is ambiguous).

| Key | Meaning |
| --- | --- |
| `DISCORD_TOKEN` | Bot token (Developer Portal → Bot → Token) |
| `CHANNEL_ID` | Channel **or thread** ID the bridge listens in / replies to |
| `USER_ID` | Your Discord user ID — only your messages are forwarded |
| `TMUX_SESSION` | tmux session name (default: the project dir name; each dir runs its own session) |
| `CONTAINER` | docker container mode — on by default; `0` to run on the host (flags: `--container` / `--no-container`) |
| `CONTAINER_IMAGE` | existing docker image to use as-is (no build) |
| `DOCKERFILE` | Dockerfile to build the container image from |

## Container mode

Container mode is **on by default** — claudebot runs the claude process inside
a docker container **as root**, so it can `apt-get install` / `npm i -g`
whatever it needs without touching the host. Pass `--no-container` (or set
`CONTAINER=0` in a `.claudebot` file) to run on the host instead:

- the project dir is bind-mounted at the same path; the bridge stays on the
  host and everything (attach, Discord, `!new`, `!esc`) works the same
- `container-home/` is mounted at `/root`, so claude's login and dotfiles
  persist across sessions; system-level installs last for one session
- `IS_SANDBOX=1` is passed so claude permits `--dangerously-skip-permissions`
  as root

The image is chosen in this order:

1. `CONTAINER_IMAGE` / `--container-image` — an existing image, used as-is
2. `DOCKERFILE` / `--dockerfile` — built (tagged `claudebot-<project-name>`)
3. `./Dockerfile` in the project — built automatically, same tag
4. the bundled `Dockerfile` — built as `claudebot` (the node base is only
   because Claude Code is an npm package; it's general-purpose Debian)

### The md viewer + tunnel

The bundled image ships the [md viewer](https://github.com/danroblewis/md)
(a markdown/file browser with a live Claude-transcript viewer) and
`cloudflared`. On container start the entrypoint serves the project on
port 8085, opens a cloudflared quick tunnel, waits until the tunnel
actually answers (fresh trycloudflare subdomains take a while to resolve —
it verifies over DoH to dodge negative DNS caching), and posts
`🌐 md viewer: https://….trycloudflare.com` to the Discord channel. Since
the container's `/root/.claude` is the persistent `container-home/`, that
link also lets you read the live session transcript from any device.

The announce works because claudebot passes `CLAUDEBOT_DISCORD_TOKEN` and
`CLAUDEBOT_CHANNEL_ID` into every container — your own scripts can use them
to post to the bridged channel too.

Builds rerun on every launch, so Dockerfile edits are picked up (docker's
layer cache makes unchanged builds near-instant).

**Bring-your-own-Dockerfile requirements:** the image must have claude
installed **outside `/root`** (the `container-home/` mount shadows `/root`
at runtime — `npm install -g @anthropic-ai/claude-code` lands safely in
`/usr/local`) and should run as root so the persistent home lines up:

```dockerfile
FROM ubuntu:24.04   # any base you like
RUN apt-get update && apt-get install -y curl ca-certificates git \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y nodejs \
 && npm install -g @anthropic-ai/claude-code
```

To get the md viewer + tunnel announce in a custom image, add:

```dockerfile
# md viewer, built from source
COPY --from=golang:1.25-bookworm /usr/local/go /usr/local/go
ADD https://api.github.com/repos/danroblewis/md/commits/main /tmp/md-head.json
RUN git clone --depth 1 https://github.com/danroblewis/md.git /opt/md \
 && cd /opt/md && /usr/local/go/bin/go build -o /usr/local/bin/md . \
 && rm -rf /opt/md /root/go /root/.cache

# cloudflared (quick tunnels need no account)
RUN curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/cloudflared

# starts md + tunnel, announces to Discord, then execs claude.
# copy it from this repo into your project (or COPY from the claudebot
# image: COPY --from=claudebot /usr/local/bin/dev-entrypoint.sh ...)
COPY dev-entrypoint.sh /usr/local/bin/dev-entrypoint.sh
RUN chmod +x /usr/local/bin/dev-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/dev-entrypoint.sh"]
```

(see `~/evident/Dockerfile.dev` for a real example that adds rust + a
pinned Z3 on top of this pattern)

> **First container run only:** the container has its own claude login
> (macOS keychain credentials don't carry over). You'll be attached to the
> TUI — pick a theme and `/login` once; it persists in `container-home/`.

## How it works

- `claudebot` creates a tmux session with two windows: `claude` (the TUI,
  launched as `claude --session-id <uuid> --dangerously-skip-permissions
  <your opts>` in the current directory) and `bridge` (the Discord relay;
  its logs live there — `ctrl-b n` to peek).
- The fixed session id makes the transcript path deterministic:
  `~/.claude/projects/<munged-work-dir>/<session-id>.jsonl`.
- [Agent teams](https://code.claude.com/docs/en/agent-teams) are enabled for
  every session (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, injected at launch
  in host mode and via `docker run -e` in container mode).
- Incoming Discord messages (from your `USER_ID`, in `CHANNEL_ID`) are
  injected via tmux bracketed paste + Enter.
- The bridge tails the transcript, picks out assistant `text` blocks (skipping
  thinking, tool calls, and subagent sidechains), and posts them to the
  channel, chunked to Discord's 2000-char limit. Turns you type directly in
  the TUI are relayed to Discord too — it's one shared session.

## Setup

1. **Create the Discord bot** at https://discord.com/developers/applications:
   - Bot → enable **Message Content Intent** (required)
   - Copy the bot token
   - OAuth2 → URL Generator → scope `bot`, permissions *View Channels*,
     *Send Messages*, *Add Reactions* → invite it to your server
2. **Configure** `~/.claudebot` (see above; enable Developer Mode in Discord
   settings to get Copy ID context menus)
3. **Install**:
   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   printf '#!/bin/sh\nexec %s/.venv/bin/python %s/claudebot.py "$@"\n' "$PWD" "$PWD" \
     > /opt/homebrew/bin/claudebot && chmod +x /opt/homebrew/bin/claudebot
   ```

> **First run in a new directory:** claude shows a one-time folder-trust
> prompt in the TUI. You'll see it since `claudebot` attaches you — answer it
> once, then Discord messages flow.

## Commands (in the channel)

| Command | Effect |
| --- | --- |
| `!new` | Start a fresh claude session (same directory) |
| `!/<command>` | Send any claude slash command — Discord hijacks bare `/`, so `!/compact`, `!/clear`, `!/goal …` etc. TUI-only output is visible via `!peek` |
| `!tasks` | Claude's task list for the session (🟩 done · 🟨 active · ⬜ pending, with blocker edges), read from `~/.claude/tasks/<session>/` |
| `!esc` | Interrupt Claude: escapes until the prompt returns (backs out of rewind if overshot) |
| `!bg` | Ctrl+B: move the currently running Bash tool to the background so the turn (and your queued messages) proceed immediately |
| `!clear` | Dismiss a stuck interactive prompt (sends Esc) — un-hoses a session blocked on a TUI selector |
| `!peek` | Show the live claude TUI pane in a code block |
| `!ps` | Live activity monitor: a status line (⚡ generating / ⏳ waiting / 💤 idle · context usage · next-update countdown) plus a per-process CPU/RSS chart (~3KB image). Adaptive cadence: 30s → 1m → 5m → 10m → 20m → 30m as metrics stay steady (habituation: significance is judged against the session's recent churn); significant changes, a 🔄 reaction, or any message from you snap it back to 30s. The chart window is the update interval + 10 min, so slow updates show everything since the last one. React ❌ to close; cleared when Claude replies. Auto-opens whenever Claude's last message is 15s old and it isn't still generating — i.e. when it goes quiet (usually because it's running something). Sampling runs every 10s continuously |
| `!status` | Embed card: workspace, session, container, transcript activity |
| anything else | Forwarded to Claude — including **mid-turn**: messages sent while Claude works are queued (📨 reaction) and read between tool calls, with full context |

While Claude works, your message gets a 👀 reaction and the bot shows the
typing indicator. A single **status message** appears when tools start
running and is edited live with the recent tool calls (`🔧 Bash: npm test`),
then collapses into a one-line summary at turn end
(`✅ 1m42s · 12 tool calls · 8,341 output tokens`).

Discord **attachments** (images, files) are saved to `/tmp/claudebot-uploads/`
(mounted into the container in container mode) and their paths are passed to
Claude with your message — drop a screenshot in the channel and ask about it.

Replies longer than ~5 Discord messages are sent as a preview plus the full
text attached as `reply.md` instead of a wall of chunks.

### Interactive prompts

Claude Code's `AskUserQuestion` selector renders an arrow-key menu in the TUI
that can't be answered by pasting text — left alone it blocks the session. The
bridge detects it by **scraping the live pane** (the transcript's `tool_use`
line only lands *after* the choice is made, too late to relay), parses the
question and options, and posts them to Discord with a button per option plus
a dismiss button. Clicking a button (or replying `!N`) navigates the real
selector — arrow keys to the chosen row, then Enter — so Claude's tool returns
your actual choice. Typing a freeform answer instead sends Esc to dismiss and
steers Claude with your text. `!clear` force-dismisses a stuck prompt. The
claude pane is launched tall (200 lines) so big prompts fit for `!peek`. To
avoid them entirely, tell Claude in `CLAUDE.md` to ask in plain prose.
