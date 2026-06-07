#!/usr/bin/env python3
"""claudebot — Discord <-> Claude Code bridge.

Usage:
    cd ~/my_project && claudebot [options] [claude options...]
        Start a tmux-wrapped claude session in the current directory with a
        Discord bridge, and attach to it. Unrecognized options are passed
        through to claude. If claudebot is already running in this directory,
        just attach.

    claudebot --stop
        Tear down the tmux session (claude + bridge).

Configuration (KEY=VALUE lines): DISCORD_TOKEN, CHANNEL_ID, USER_ID,
TMUX_SESSION. Resolution order, later wins:

    ~/.claudebot  <  ./.claudebot (project dir)  <  command line flags

The tmux session has two windows: "claude" (the claude TUI) and "bridge"
(this script in --bridge mode: forwards Discord messages into the claude
window and relays Claude's prose replies — tailed from the JSONL transcript
under ~/.claude/projects — back to the Discord channel).
"""

import argparse
import asyncio
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent
GLOBAL_CONFIG = Path.home() / ".claudebot"
STATE_FILE = Path.home() / ".claudebot-sessions.json"  # keyed by TMUX_SESSION
PROJECTS_DIR = Path.home() / ".claude" / "projects"
CONTAINER_HOME = SCRIPT_DIR / "container-home"  # mounted at /root in the container
UPLOAD_DIR = Path("/tmp/claudebot-uploads")  # Discord attachments land here
POLL_INTERVAL = 0.5
DISCORD_LIMIT = 2000
DEFAULT_SESSION = "claudebot"
DEFAULT_IMAGE = "claudebot"

CONFIG_KEYS = ("DISCORD_TOKEN", "CHANNEL_ID", "USER_ID", "TMUX_SESSION",
               "CONTAINER", "CONTAINER_IMAGE", "DOCKERFILE")


def log(msg: str) -> None:
    print(msg, flush=True)


def resolve_config(opts) -> dict:
    """Merge config: ~/.claudebot < ./.claudebot < CLI flags."""
    cfg = {}
    for path in (GLOBAL_CONFIG, Path.cwd() / ".claudebot"):
        if path.is_file():
            cfg.update({k: v for k, v in dotenv_values(path).items()
                        if k in CONFIG_KEYS and v})
    for key in ("DISCORD_TOKEN", "CHANNEL_ID", "USER_ID", "TMUX_SESSION",
                "CONTAINER_IMAGE", "DOCKERFILE"):
        value = getattr(opts, key.lower(), None)
        if value:
            cfg[key] = value
    if getattr(opts, "container", False):
        cfg["CONTAINER"] = "1"
    if getattr(opts, "no_container", False):
        cfg["CONTAINER"] = "0"
    cfg.setdefault("TMUX_SESSION", DEFAULT_SESSION)
    return cfg


def container_enabled(cfg: dict) -> bool:
    return str(cfg.get("CONTAINER", "")).lower() in ("1", "true", "yes", "on")


def container_name(tmux_session: str) -> str:
    return f"claudebot-{tmux_session}"


def _read_sessions() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_state(tmux_session: str) -> dict:
    return _read_sessions().get(tmux_session, {})


def save_state(state: dict) -> None:
    sessions = _read_sessions()
    sessions[state["config"]["TMUX_SESSION"]] = state
    STATE_FILE.write_text(json.dumps(sessions, indent=1))
    STATE_FILE.chmod(0o600)  # contains the Discord token


def munge(work_dir: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", work_dir)


def projects_root(cfg: dict) -> Path:
    # In container mode claude's home is container-home/, mounted at /root.
    return (CONTAINER_HOME / ".claude" / "projects"
            if container_enabled(cfg) else PROJECTS_DIR)


def latest_session_id(cfg: dict, work_dir: str) -> str | None:
    """Most recently active claude session for this workspace, or None."""
    project_dir = projects_root(cfg) / munge(work_dir)
    transcripts = sorted(project_dir.glob("*.jsonl"),
                         key=lambda p: p.stat().st_mtime) if project_dir.is_dir() else []
    return transcripts[-1].stem if transcripts else None


def resolve_resume(claude_args: list[str], cfg: dict, work_dir: str):
    """Honor --continue / -c / --resume [id] in the passthrough args.

    Returns (session_id_to_resume or None, claude_args with those flags
    stripped) — claudebot re-adds --resume/--session-id itself so the bridge
    always knows which transcript to tail."""
    args = list(claude_args)
    resume_id = None
    wants_resume = False
    if "--resume" in args:
        wants_resume = True
        i = args.index("--resume")
        nxt = args[i + 1] if i + 1 < len(args) else None
        if nxt and re.fullmatch(r"[0-9a-fA-F-]{36}", nxt):
            resume_id = nxt
            del args[i:i + 2]
        else:
            del args[i]
    if "-c" in args or "--continue" in args:
        wants_resume = True
        args = [a for a in args if a not in ("-c", "--continue")]
    if wants_resume and resume_id is None:
        resume_id = latest_session_id(cfg, work_dir)
        if resume_id is None:
            sys.exit(f"claudebot: no previous session found for {work_dir}")
    return resume_id, args


def claude_command(state: dict) -> str:
    session_flag = "--resume" if state.get("resume") else "--session-id"
    cmd = ["claude", session_flag, state["session_id"],
           "--dangerously-skip-permissions", *state.get("claude_args", [])]
    cfg = state["config"]
    if container_enabled(cfg):
        wd = state["work_dir"]
        cmd = ["docker", "run", "--rm", "-it",
               "--name", container_name(cfg["TMUX_SESSION"]),
               "-e", "IS_SANDBOX=1",
               # so in-container tooling (e.g. entrypoint scripts) can post
               # to the bridged channel
               "-e", f"CLAUDEBOT_DISCORD_TOKEN={cfg.get('DISCORD_TOKEN', '')}",
               "-e", f"CLAUDEBOT_CHANNEL_ID={cfg.get('CHANNEL_ID', '')}",
               "-v", f"{CONTAINER_HOME}:/root",
               "-v", f"{UPLOAD_DIR}:{UPLOAD_DIR}",  # same path inside, so paths we tell claude work
               "-v", f"{wd}:{wd}", "-w", wd,
               cfg.get("CONTAINER_IMAGE", DEFAULT_IMAGE), *cmd]
    return " ".join(shlex.quote(c) for c in cmd)


def transcript_path(state: dict) -> Path:
    # The project is mounted at its host path in container mode, so the
    # munged dir matches either way.
    return (projects_root(state["config"]) / munge(state["work_dir"])
            / f"{state['session_id']}.jsonl")


# tmux targets ("=" forces exact-match; ":claude" targets the window)
def session_t(name: str) -> str:
    return f"={name}"


def claude_win(name: str) -> str:
    return f"={name}:claude"


def bridge_win(name: str) -> str:
    return f"={name}:bridge"


# ---------------------------------------------------------------------------
# Channel lookup by name (Discord REST API)
# ---------------------------------------------------------------------------

def discord_get(token: str, path: str):
    import time
    import urllib.error
    import urllib.request
    for _ in range(5):
        req = urllib.request.Request(
            f"https://discord.com/api/v10{path}",
            headers={"Authorization": f"Bot {token}",
                     "User-Agent": "DiscordBot (claudebot, 0.1)"})
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            time.sleep(float(err.headers.get("Retry-After") or 1) + 0.1)
    sys.exit(f"claudebot: Discord API rate limited on {path}")


def resolve_channel_by_name(token: str, name: str) -> str:
    """Find a text channel or active thread by name across the bot's guilds."""
    want = name.lstrip("#").lower()
    matches = []
    try:
        for guild in discord_get(token, "/users/@me/guilds"):
            for c in discord_get(token, f"/guilds/{guild['id']}/channels"):
                if c.get("type") in (0, 5) and c.get("name", "").lower() == want:
                    matches.append((guild["name"], c["name"], c["id"], "channel"))
            threads = discord_get(token, f"/guilds/{guild['id']}/threads/active")
            for t in threads.get("threads", []):
                if t.get("name", "").lower() == want:
                    matches.append((guild["name"], t["name"], t["id"], "thread"))
    except OSError as err:
        sys.exit(f"claudebot: channel lookup failed: {err}")
    if not matches:
        sys.exit(f"claudebot: no text channel or active thread named '{name}' found")
    if len(matches) > 1:
        listing = "\n".join(f"  {m[2]}  #{m[1]} ({m[3]} in {m[0]})" for m in matches)
        sys.exit(f"claudebot: '{name}' is ambiguous, use --channel-id:\n{listing}")
    guild_name, ch_name, ch_id, kind = matches[0]
    print(f"claudebot: resolved '{name}' -> {ch_id} ({kind} #{ch_name} in {guild_name})")
    return ch_id


# ---------------------------------------------------------------------------
# Launcher (sync, runs in the user's terminal)
# ---------------------------------------------------------------------------

def tmux_sync(*args: str) -> int:
    return subprocess.run(["tmux", *args], capture_output=True).returncode


def attach(name: str) -> None:
    """Replace this process with a tmux client showing the claude window."""
    tmux_sync("select-window", "-t", claude_win(name))
    if os.environ.get("TMUX"):  # already inside tmux: switch, don't nest
        os.execvp("tmux", ["tmux", "switch-client", "-t", claude_win(name)])
    os.execvp("tmux", ["tmux", "attach", "-t", session_t(name)])


def resolve_image(cfg: dict, work_dir: str) -> str:
    """Pick the docker image: an explicit CONTAINER_IMAGE is used as-is;
    otherwise build from DOCKERFILE / ./Dockerfile / the bundled Dockerfile."""
    if cfg.get("CONTAINER_IMAGE"):
        image = cfg["CONTAINER_IMAGE"]
        if subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode != 0:
            sys.exit(f"claudebot: image '{image}' not found — "
                     "pull/build it, or use DOCKERFILE to build one")
        return image
    project_tag = "claudebot-" + (
        re.sub(r"[^a-z0-9_.-]+", "-", Path(work_dir).name.lower()).strip("-._") or "project")
    if cfg.get("DOCKERFILE"):
        dockerfile = Path(cfg["DOCKERFILE"]).expanduser().resolve()
        if not dockerfile.is_file():
            sys.exit(f"claudebot: DOCKERFILE not found: {dockerfile}")
        tag = project_tag
    elif (Path(work_dir) / "Dockerfile").is_file():
        dockerfile = Path(work_dir) / "Dockerfile"
        tag = project_tag
    else:
        dockerfile = SCRIPT_DIR / "Dockerfile"
        tag = DEFAULT_IMAGE
    print(f"claudebot: building image '{tag}' from {dockerfile} ...")
    if subprocess.run(["docker", "build", "-t", tag, "-f", str(dockerfile),
                       str(dockerfile.parent)]).returncode != 0:
        sys.exit("claudebot: docker build failed")
    return tag


def launch(cfg: dict, claude_args: list[str]) -> None:
    missing = [k for k in ("DISCORD_TOKEN", "CHANNEL_ID", "USER_ID") if not cfg.get(k)]
    if missing:
        flags = ", ".join(f"--{k.lower().replace('_', '-')}" for k in missing)
        sys.exit(f"claudebot: missing {', '.join(missing)} — "
                 f"set in ./.claudebot, {GLOBAL_CONFIG}, or via {flags}")

    name = cfg["TMUX_SESSION"]
    cwd = str(Path.cwd())

    if container_enabled(cfg):
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            sys.exit("claudebot: container mode requires docker (is Docker Desktop running?)")
        cfg["CONTAINER_IMAGE"] = resolve_image(cfg, cwd)  # persists via state for !new
        CONTAINER_HOME.mkdir(exist_ok=True)
    if tmux_sync("has-session", "-t", session_t(name)) == 0:
        state = load_state(name)
        if state.get("work_dir") == cwd:
            print(f"claudebot already running in {cwd} — attaching (ctrl-b d to detach)")
            attach(name)
        print(f"claudebot '{name}' was running in {state.get('work_dir', '?')} — replacing it")
        tmux_sync("kill-session", "-t", session_t(name))

    resume_id, claude_args = resolve_resume(claude_args, cfg, cwd)
    session_id = resume_id or str(uuid.uuid4())
    state = {"session_id": session_id, "work_dir": cwd, "resume": bool(resume_id),
             "claude_args": claude_args, "config": cfg}
    save_state(state)
    if resume_id:
        print(f"claudebot: resuming claude session {resume_id}")

    if container_enabled(cfg):  # clear any leftover container from a dead session
        subprocess.run(["docker", "rm", "-f", container_name(name)], capture_output=True)

    tmux_sync("new-session", "-d", "-s", name, "-n", "claude", "-c", cwd)
    tmux_sync("send-keys", "-t", claude_win(name), claude_command(state), "Enter")

    bridge_cmd = (f"exec {shlex.quote(str(SCRIPT_DIR / '.venv/bin/python'))} "
                  f"{shlex.quote(str(SCRIPT_DIR / 'claudebot.py'))} --bridge "
                  f"--tmux-session {shlex.quote(name)}")
    tmux_sync("new-window", "-d", "-t", session_t(name), "-n", "bridge", "-c", str(SCRIPT_DIR))
    tmux_sync("send-keys", "-t", bridge_win(name), bridge_cmd, "Enter")

    print(f"claudebot: session {session_id} in {cwd}, "
          f"bridging Discord channel {cfg['CHANNEL_ID']}")
    attach(name)


def stop(cfg: dict) -> None:
    name = cfg["TMUX_SESSION"]
    if tmux_sync("kill-session", "-t", session_t(name)) == 0:
        print(f"claudebot '{name}' stopped")
    else:
        print(f"claudebot '{name}' is not running")
    state = load_state(name)
    if state and container_enabled(state["config"]):
        subprocess.run(["docker", "rm", "-f", container_name(name)], capture_output=True)


# ---------------------------------------------------------------------------
# tmux helpers (async, bridge mode)
# ---------------------------------------------------------------------------

async def tmux(*args: str, input_bytes: bytes | None = None) -> int:
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate(input_bytes)
    return proc.returncode


class ClaudeSession:
    """Talks to the claude TUI window; session metadata lives in the state file."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.work_dir: str = state["work_dir"]
        self.config: dict = state["config"]
        self.name: str = self.config["TMUX_SESSION"]

    @property
    def transcript_path(self) -> Path:
        return transcript_path(self.state)

    async def start_fresh(self) -> None:
        """Replace the claude window with a brand-new claude session."""
        self.state["session_id"] = str(uuid.uuid4())
        self.state["resume"] = False
        save_state(self.state)
        await tmux("kill-window", "-t", claude_win(self.name))  # ok if already gone
        if container_enabled(self.config):
            proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", container_name(self.name),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
        await tmux("new-window", "-d", "-t", session_t(self.name), "-n", "claude",
                   "-c", self.work_dir)
        await tmux("send-keys", "-t", claude_win(self.name),
                   claude_command(self.state), "Enter")
        log(f"Started fresh claude session {self.state['session_id']} in {self.work_dir}")

    async def send(self, text: str) -> None:
        """Paste text into the claude prompt (bracketed paste so embedded
        newlines don't submit early), then press Enter."""
        await tmux("load-buffer", "-b", "claudebot", "-", input_bytes=text.encode())
        await tmux("paste-buffer", "-p", "-d", "-b", "claudebot", "-t", claude_win(self.name))
        await asyncio.sleep(0.3)
        await tmux("send-keys", "-t", claude_win(self.name), "Enter")

    async def interrupt(self) -> None:
        await tmux("send-keys", "-t", claude_win(self.name), "Escape")

    async def capture(self) -> str:
        """Return the visible contents of the claude TUI pane."""
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-p", "-t", claude_win(self.name),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        lines = [ln.rstrip() for ln in out.decode(errors="replace").splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transcript watcher
# ---------------------------------------------------------------------------

def tool_desc(block: dict) -> str:
    """One-line human description of a tool_use block."""
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    for key in ("command", "file_path", "path", "pattern", "url",
                "description", "prompt", "query"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            value = " ".join(value.split())
            return f"{name}: {value[:80]}" + ("…" if len(value) > 80 else "")
    return name


class TranscriptWatcher:
    """Tails the session's JSONL transcript and emits assistant prose.

    Callbacks: on_text(text) per prose message; on_tool(desc) as each tool
    call starts; on_turn_end(stats) when the turn finishes, with stats =
    {"seconds", "tools", "output_tokens"}."""

    def __init__(self, session: ClaudeSession, on_text,
                 on_turn_end=None, on_tool=None) -> None:
        self.session = session
        self.on_text = on_text
        self.on_turn_end = on_turn_end
        self.on_tool = on_tool
        self.path: Path | None = None
        self.offset = 0
        self.seen_uuids: set[str] = set()
        self.seen_order: deque[str] = deque()
        self.turn_started: float | None = None
        self.turn_tools = 0
        self.turn_tokens = 0

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                traceback.print_exc()
            await asyncio.sleep(POLL_INTERVAL)

    async def tick(self) -> None:
        path = self.session.transcript_path
        if path != self.path:
            # Attaching to a pre-existing transcript: skip its history.
            # A fresh session's file doesn't exist yet, so offset starts at 0.
            self.path = path
            self.offset = path.stat().st_size if path.exists() else 0
            log(f"Watching transcript {path} from offset {self.offset}")
        if not path.exists():
            return
        size = path.stat().st_size
        if size < self.offset:  # truncated/replaced; don't replay
            self.offset = size
            return
        if size == self.offset:
            return
        with path.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        nl = data.rfind(b"\n")
        if nl == -1:  # only a partial line so far
            return
        self.offset += nl + 1
        for line in data[:nl + 1].splitlines():
            await self.handle_line(line)

    async def handle_line(self, line: bytes) -> None:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            return
        line_uuid = entry.get("uuid")
        if line_uuid:
            if line_uuid in self.seen_uuids:
                return
            self.seen_uuids.add(line_uuid)
            self.seen_order.append(line_uuid)
            if len(self.seen_order) > 2000:
                self.seen_uuids.discard(self.seen_order.popleft())
        msg = entry.get("message", {})
        content = msg.get("content")
        if not isinstance(content, list):
            return
        if self.turn_started is None:
            self.turn_started = time.time()
        tools = [b for b in content
                 if isinstance(b, dict) and b.get("type") == "tool_use"]
        self.turn_tools += len(tools)
        self.turn_tokens += (msg.get("usage") or {}).get("output_tokens") or 0
        if self.on_tool:
            for block in tools:
                await self.on_tool(tool_desc(block))
        texts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n\n".join(t for t in texts if t.strip())
        if not text:
            return
        await self.on_text(text)
        # A turn can write several end_turn lines (thinking, then text); the
        # prose one is the real finale, so gate turn-end on text being present.
        if msg.get("stop_reason") == "end_turn":
            stats = {"seconds": time.time() - self.turn_started,
                     "tools": self.turn_tools, "output_tokens": self.turn_tokens}
            self.turn_started, self.turn_tools, self.turn_tokens = None, 0, 0
            if self.on_turn_end:
                await self.on_turn_end(stats)


def open_fence(text: str) -> str | None:
    """If text ends inside a ``` code block, return its opening fence line."""
    fence = None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = None if fence else f"```{line.lstrip()[3:].strip()[:12]}"
    return fence


def chunk_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks, preferring paragraph breaks.
    Code fences split across chunks are closed and reopened so each chunk
    renders correctly on its own."""
    size = limit - 24  # room to close/reopen a code fence at chunk borders
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    fixed, carry = [], None
    for chunk in chunks:
        if carry:
            chunk = f"{carry}\n{chunk}"
        carry = open_fence(chunk)
        fixed.append(f"{chunk}\n```" if carry else chunk)
    return fixed


# ---------------------------------------------------------------------------
# Discord client (bridge mode)
# ---------------------------------------------------------------------------

def run_bridge(tmux_session: str) -> None:
    import discord

    state = load_state(tmux_session)
    if not state:
        sys.exit(f"claudebot: no state file for tmux session '{tmux_session}'")
    session = ClaudeSession(state)
    cfg = session.config
    channel_id = int(cfg["CHANNEL_ID"])
    user_id = int(cfg["USER_ID"])

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    watcher_started = False

    WORKING = "👀"  # added to forwarded messages, removed when the turn ends
    pending: list[discord.Message] = []
    turn_done = asyncio.Event()
    typing_task: asyncio.Task | None = None

    async def get_chan():
        return client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    def fmt_duration(seconds: float) -> str:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"

    async def relay(text: str) -> None:
        channel = await get_chan()
        chunks = chunk_message(text)
        if len(chunks) > 5:  # multi-page reply: attach as a file, don't spam
            preview = chunk_message(text, 1800)[0]
            await channel.send(
                f"{preview}\n-# 📄 long reply — full text attached ({len(text):,} chars)",
                file=discord.File(io.BytesIO(text.encode()), "reply.md"))
            return
        for chunk in chunks:
            await channel.send(chunk)

    # --- live tool-status message: one message edited as tools run -------
    tool_log: deque[str] = deque(maxlen=5)
    status_msg: discord.Message | None = None
    last_status_edit = 0.0

    def render_status() -> str:
        return "\n".join(["⚙️ **working**"] + [f"-# 🔧 {d}" for d in tool_log])

    async def on_tool(desc: str) -> None:
        nonlocal status_msg, last_status_edit
        tool_log.append(desc)
        try:
            if status_msg is None:
                status_msg = await (await get_chan()).send(render_status())
            elif time.time() - last_status_edit > 1.5:  # respect edit rate limits
                await status_msg.edit(content=render_status())
            else:
                return
            last_status_edit = time.time()
        except discord.HTTPException:
            pass

    async def finalize_status(line: str) -> None:
        nonlocal status_msg
        if status_msg is not None:
            try:
                await status_msg.edit(content=line)
            except discord.HTTPException:
                pass
            status_msg = None
        tool_log.clear()

    async def on_turn_end(stats: dict) -> None:
        await finalize_status(f"-# ✅ {fmt_duration(stats['seconds'])} · "
                              f"{stats['tools']} tool calls · "
                              f"{stats['output_tokens']:,} output tokens")
        await clear_working()

    async def clear_working() -> None:
        turn_done.set()  # stops the typing indicator
        while pending:
            msg = pending.pop()
            try:
                await msg.remove_reaction(WORKING, client.user)
            except discord.HTTPException:
                pass

    async def typing_until_done() -> None:
        try:
            async with (await get_chan()).typing():
                await turn_done.wait()
        except discord.HTTPException:
            pass

    def begin_turn() -> None:
        nonlocal typing_task
        turn_done.clear()
        if typing_task is None or typing_task.done():
            typing_task = asyncio.create_task(typing_until_done())

    @client.event
    async def on_ready() -> None:
        nonlocal watcher_started
        log(f"Logged in as {client.user}; bridging {session.work_dir} "
            f"<-> channel {channel_id}")
        if not watcher_started:
            watcher_started = True
            asyncio.create_task(TranscriptWatcher(
                session, relay, on_turn_end=on_turn_end, on_tool=on_tool).run())

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.channel.id != channel_id or message.author.id != user_id:
            return
        content = message.content.strip()
        if not content and not message.attachments:
            return
        if content == "!new":
            await session.start_fresh()
            await finalize_status("-# 🆕 session restarted")
            await clear_working()
            await message.channel.send("🆕 Started a fresh Claude session.")
            return
        if content == "!esc":
            await session.interrupt()
            await finalize_status("-# 🛑 interrupted")
            await clear_working()  # interrupted turns never write end_turn
            await message.add_reaction("🛑")
            return
        if content == "!peek":
            pane = (await session.capture()).replace("```", "`​``")
            await message.channel.send(f"```\n{pane[-1900:] or '(empty pane)'}\n```")
            return
        if content == "!status":
            embed = discord.Embed(title="claudebot", color=0xCC785C)
            embed.add_field(name="Workspace", value=f"`{session.work_dir}`", inline=False)
            embed.add_field(name="Claude session",
                            value=f"`{session.state['session_id']}`", inline=False)
            embed.add_field(name="tmux", value=f"`{session.name}`", inline=True)
            embed.add_field(name="Container",
                            value=(session.config.get("CONTAINER_IMAGE", "?")
                                   if container_enabled(session.config) else "host"),
                            inline=True)
            transcript = session.transcript_path
            if transcript.exists():
                stat = transcript.stat()
                embed.add_field(name="Transcript",
                                value=f"{stat.st_size // 1024} KB · active "
                                      f"{fmt_duration(time.time() - stat.st_mtime)} ago",
                                inline=True)
            await message.channel.send(embed=embed)
            return

        parts = [message.content] if content else []
        if message.attachments:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            saved = []
            for att in message.attachments:
                base = Path(att.filename).name
                dest, n = UPLOAD_DIR / base, 1
                while dest.exists():
                    dest = UPLOAD_DIR / f"{Path(base).stem}-{n}{Path(base).suffix}"
                    n += 1
                try:
                    await att.save(dest)
                    saved.append(str(dest))
                except discord.HTTPException:
                    log(f"failed to save attachment {att.filename}")
            if saved:
                parts.append("Files attached to this message (saved locally):\n"
                             + "\n".join(saved))
        if not parts:
            return
        await session.send("\n\n".join(parts))
        begin_turn()
        try:
            await message.add_reaction(WORKING)
            pending.append(message)
        except discord.HTTPException:
            pass  # cosmetic only — the message was already delivered

    client.run(cfg["DISCORD_TOKEN"])


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claudebot", allow_abbrev=False,
        usage="claudebot [options] [claude options...] | claudebot --stop",
        description="Run claude in tmux in the current directory, bridged to Discord. "
                    "Unrecognized options are passed through to claude. "
                    "Config from ~/.claudebot then ./.claudebot (KEY=VALUE lines); "
                    "flags below override both.",
    )
    parser.add_argument("--discord-token", help="Discord bot token")
    parser.add_argument("--channel-id", help="Discord channel/thread ID to bridge")
    parser.add_argument("--channel",
                        help="Discord channel/thread NAME to bridge (looked up "
                             "via the bot token across the bot's guilds)")
    parser.add_argument("--user-id", help="Discord user ID allowed to talk to Claude")
    parser.add_argument("--tmux-session", help=f"tmux session name (default: {DEFAULT_SESSION})")
    parser.add_argument("--container", action="store_true",
                        help="run claude inside a docker container (as root)")
    parser.add_argument("--no-container", action="store_true",
                        help="override CONTAINER=1 from a config file")
    parser.add_argument("--container-image",
                        help="existing docker image to use for --container (no build)")
    parser.add_argument("--dockerfile",
                        help="Dockerfile to build the container image from "
                             "(default: ./Dockerfile if present, else the bundled one)")
    parser.add_argument("--stop", action="store_true",
                        help="tear down the claudebot tmux session")
    parser.add_argument("--bridge", action="store_true", help=argparse.SUPPRESS)
    opts, claude_args = parser.parse_known_args()

    if opts.bridge:
        run_bridge(opts.tmux_session or DEFAULT_SESSION)
        return
    cfg = resolve_config(opts)
    if opts.channel and not opts.stop:
        if not cfg.get("DISCORD_TOKEN"):
            sys.exit("claudebot: --channel needs DISCORD_TOKEN (config file or --discord-token)")
        cfg["CHANNEL_ID"] = resolve_channel_by_name(cfg["DISCORD_TOKEN"], opts.channel)
    if opts.stop:
        stop(cfg)
    else:
        launch(cfg, claude_args)


if __name__ == "__main__":
    main()
