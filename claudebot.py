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
import logging
import logging.handlers
import os
import re
import shlex
import subprocess
import sys
import time
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


LOG = logging.getLogger("claudebot")


def log(msg: str) -> None:
    if LOG.handlers:  # bridge mode: real logging
        LOG.info(msg)
    else:             # launcher mode: plain terminal output
        print(msg, flush=True)


def setup_logging(tmux_session: str) -> None:
    """Bridge logging: rotating file in logs/ plus stdout (the tmux window)."""
    logs_dir = SCRIPT_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / f"{tmux_session}.log", maxBytes=5_000_000, backupCount=3)
    stream_handler = logging.StreamHandler()
    for handler in (file_handler, stream_handler):
        handler.setFormatter(fmt)
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


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
    for flag in ("--resume", "-resume", "-r"):
        if flag in args:
            wants_resume = True
            i = args.index(flag)
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt and re.fullmatch(r"[0-9a-fA-F-]{36}", nxt):
                resume_id = nxt
                del args[i:i + 2]
            else:
                del args[i]
    if any(f in args for f in ("-c", "--continue", "-continue")):
        wants_resume = True
        args = [a for a in args if a not in ("-c", "--continue", "-continue")]
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
               "--init",  # tini as PID 1 to reap zombies (claude isn't an init system)
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


async def run_out(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


# infrastructure we run ourselves — not Claude's tool subprocesses
PS_INFRA = ("md -depth", "cloudflared tunnel", "dev-entrypoint")


def human_mem(rss_kb: int) -> str:
    if rss_kb >= 1048576:
        return f"{rss_kb / 1048576:.1f}G"
    if rss_kb >= 1024:
        return f"{rss_kb // 1024}M"
    return f"{rss_kb}K"


def human_etime(etime: str) -> str:
    """ps etime ([[dd-]hh:]mm:ss) -> '17m', '1h02m', '3d4h'."""
    days, rest = (etime.split("-") + [""])[:2] if "-" in etime else ("0", etime)
    parts = [int(p) for p in rest.split(":")]
    h, m, s = ([0] * (3 - len(parts)) + parts)
    d = int(days)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def parse_ps(ps_output: str, session_id: str):
    """Parse `ps -o pid,ppid,pcpu,rss,etime,args` output. Returns
    (procs, children, claude_pid) where claude is found by session id."""
    procs: dict[int, tuple] = {}
    children: dict[int, list[int]] = {}
    claude_pid = None
    for line in ps_output.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) < 6 or not parts[0].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        cmd = " ".join(parts[5].split())
        procs[pid] = (float(parts[2]), int(parts[3]) if parts[3].isdigit() else 0,
                      parts[4], cmd)
        children.setdefault(ppid, []).append(pid)
        if claude_pid is None and session_id in cmd and "docker" not in cmd:
            claude_pid = pid
    return procs, children, claude_pid


def ps_totals(ps_output: str, session_id: str) -> tuple[float, int] | None:
    """(total CPU %, total RSS KB) for claude + tool subprocesses."""
    procs, children, claude_pid = parse_ps(ps_output, session_id)
    if claude_pid is None:
        return None
    cpu_total, rss_total = 0.0, 0
    def walk(pid: int) -> None:
        nonlocal cpu_total, rss_total
        cpu, rss, _, cmd = procs[pid]
        if pid != claude_pid and (any(p in cmd for p in PS_INFRA) or "<defunct>" in cmd):
            return
        cpu_total += cpu
        rss_total += rss
        for kid in children.get(pid, []):
            walk(kid)
    walk(claude_pid)
    return cpu_total, rss_total


# Discord dark-theme embed colors
CHART_BG = "#2b2d31"      # embed background — chart blends in seamlessly
CHART_FG = "#80848e"      # Discord secondary text
CHART_CPU = "#f0b132"     # 🟨
CHART_RSS = "#5865f2"     # 🟦


def render_chart(samples) -> bytes:
    """Tiny PNG matching Discord's dark embed: CPU% + RSS over time.
    Axis titles/legend live in the embed footer (text is cheaper than
    pixels); palette quantization keeps the file ~5KB."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    now = samples[-1][0]
    xs = [(s[0] - now) / 60 for s in samples]  # minutes ago (<= 0)
    cpu = [s[1] for s in samples]
    rss = [s[2] / 1048576 for s in samples]    # GB
    fig, ax1 = plt.subplots(figsize=(5.8, 1.7), dpi=80)
    fig.patch.set_facecolor(CHART_BG)
    ax1.set_facecolor(CHART_BG)
    ax2 = ax1.twinx()
    ax1.plot(xs, cpu, color=CHART_CPU, linewidth=1.2)
    ax2.plot(xs, rss, color=CHART_RSS, linewidth=1.2)
    ax1.set_ylim(bottom=0)
    ax2.set_ylim(bottom=0)
    ax1.locator_params(axis="y", nbins=4)
    ax2.locator_params(axis="y", nbins=4)
    ax1.locator_params(axis="x", nbins=7)
    for ax, color in ((ax1, CHART_CPU), (ax2, CHART_RSS)):
        ax.tick_params(labelsize=7, colors=CHART_FG)
        for label in ax.get_yticklabels():
            label.set_color(color)
        for spine in ax.spines.values():
            spine.set_visible(False)
    ax1.grid(True, color="#404249", linewidth=0.4, alpha=0.5)
    fig.tight_layout(pad=0.3)
    raw = io.BytesIO()
    fig.savefig(raw, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    # palette-quantize: line charts have few colors, so this halves the size
    raw.seek(0)
    img = Image.open(raw).convert("RGB").quantize(colors=32)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def format_ps_tree(ps_output: str, session_id: str) -> str:
    """Markdown tree of the claude process (found by its session id on the
    command line) and its descendants, minus our own infra."""
    procs, children, claude_pid = parse_ps(ps_output, session_id)
    if claude_pid is None:
        return "❓ couldn't find the claude process"

    def heat(cpu: float) -> str:
        return "🔥" if cpu >= 50 else "⚙️" if cpu >= 5 else "💤"

    def fmt(pid: int, depth: int, label: str | None = None) -> str:
        cpu, rss, etime, cmd = procs[pid]
        if label is None:
            argv = cmd.split()
            label = f"**{Path(argv[0]).name}**"
            args = " ".join(argv[1:])[:40]
            if args:
                label += f" `{args}`"
        indent = "   " * depth + ("└─ " if depth else "")
        return (f"{indent}{heat(cpu)} {label} — "
                f"{cpu:.0f}% · {human_mem(rss)} · {human_etime(etime)}")

    lines = [fmt(claude_pid, 0, "**claude**")]
    zombies = 0
    def walk(pid: int, depth: int) -> None:
        nonlocal zombies
        for kid in sorted(children.get(pid, [])):
            cmd = procs[kid][3]
            if any(p in cmd for p in PS_INFRA):
                continue
            if "<defunct>" in cmd:  # dead, awaiting reaping — fold into a count
                zombies += 1
                continue
            lines.append(fmt(kid, depth))
            walk(kid, depth + 1)
    walk(claude_pid, 1)
    if len(lines) == 1:
        lines.append("   💤 no subprocesses running")
    if zombies:
        lines.append(f"   💀 {zombies} defunct (already dead, pending reaping)")
    return "\n".join(lines)


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

    async def background_tool(self) -> None:
        """Ctrl+B: move the currently running Bash tool to the background so
        the turn continues (and queued messages get read) immediately."""
        await tmux("send-keys", "-t", claude_win(self.name), "C-b")

    async def interrupt(self) -> None:
        """Escape until the turn actually stops. The number of escapes needed
        varies, so: send while the pane says "esc to interrupt" (max ~5s),
        then if we overshot into the rewind panel, one more Escape exits it."""
        sent = 0
        for _ in range(10):
            if "esc to interrupt" not in (await self.capture()).lower():
                break
            await tmux("send-keys", "-t", claude_win(self.name), "Escape")
            sent += 1
            await asyncio.sleep(0.5)
        if not sent:  # wasn't visibly working; send one anyway
            await tmux("send-keys", "-t", claude_win(self.name), "Escape")
        await asyncio.sleep(0.4)
        if "rewind" in (await self.capture()).lower():
            await tmux("send-keys", "-t", claude_win(self.name), "Escape")

    async def capture(self) -> str:
        """Return the visible contents of the claude TUI pane."""
        out = await run_out("tmux", "capture-pane", "-p", "-t", claude_win(self.name))
        lines = [ln.rstrip() for ln in out.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    async def ps_output(self) -> str:
        if container_enabled(self.config):
            return await run_out("docker", "exec", container_name(self.name),
                                 "ps", "-eo", "pid,ppid,pcpu,rss,etime,args")
        return await run_out("ps", "-axo", "pid,ppid,pcpu,rss,etime,args")

    async def process_tree(self) -> str:
        """Markdown tree: claude + its tool subprocesses with CPU/mem/elapsed."""
        return format_ps_tree(await self.ps_output(), self.state["session_id"])


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
                LOG.exception("transcript watcher tick failed")
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

    setup_logging(tmux_session)
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

    WORKING = "👀"  # fresh turn; 📨 = queued into an already-running turn
    pending: list[tuple[discord.Message, str]] = []
    turn_done = asyncio.Event()
    typing_task: asyncio.Task | None = None
    watcher: TranscriptWatcher | None = None

    async def get_chan():
        return client.get_channel(channel_id) or await client.fetch_channel(channel_id)

    def fmt_duration(seconds: float) -> str:
        minutes, secs = divmod(int(seconds), 60)
        return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"

    # --- outbound queue: serialized, paced, retried ------------------------
    # The watcher consumes transcript bytes exactly once, so a failed send
    # must never bubble up and lose the message. Everything user-visible goes
    # through here; a single consumer preserves order.
    out_q: asyncio.Queue = asyncio.Queue()
    SEND_ATTEMPTS = 6

    def enqueue(content: str | None = None, embed=None,
                file_bytes: bytes | None = None, filename: str | None = None) -> None:
        out_q.put_nowait({"content": content, "embed": embed,
                          "file_bytes": file_bytes, "filename": filename})

    async def sender_loop() -> None:
        while True:
            item = await out_q.get()
            for attempt in range(1, SEND_ATTEMPTS + 1):
                try:
                    channel = await get_chan()
                    file = (discord.File(io.BytesIO(item["file_bytes"]), item["filename"])
                            if item["file_bytes"] is not None else discord.utils.MISSING)
                    await channel.send(item["content"],
                                       embed=item["embed"] or discord.utils.MISSING,
                                       file=file)
                    if attempt > 1:
                        LOG.info(f"send succeeded on attempt {attempt}")
                    break
                except Exception as err:
                    wait = min(2 ** attempt, 30)
                    LOG.warning(f"send failed (attempt {attempt}/{SEND_ATTEMPTS}): "
                                f"{type(err).__name__}: {err} — retrying in {wait}s")
                    await asyncio.sleep(wait)
            else:
                LOG.error(f"DROPPED message after {SEND_ATTEMPTS} attempts: "
                          f"{str(item['content'])[:120]!r}")
            await asyncio.sleep(0.3)  # gentle pacing under discord.py's own limiter

    # --- live !ps embed: auto-updates until Claude actually replies -------
    ps_msg: discord.Message | None = None
    ps_task: asyncio.Task | None = None
    last_activity = time.time()

    def touch() -> None:
        nonlocal last_activity
        last_activity = time.time()

    # continuous resource sampling so a freshly opened monitor has history
    samples: deque = deque(maxlen=360)  # (t, cpu%, rss_kb); 1h at 10s

    async def sampler() -> None:
        while True:
            try:
                totals = ps_totals(await session.ps_output(),
                                   session.state["session_id"])
                if totals:
                    samples.append((time.time(), *totals))
            except Exception:
                pass  # container down between sessions etc.
            await asyncio.sleep(10)

    async def ps_updater(interval: float = 5, max_ticks: int = 120) -> None:
        nonlocal ps_msg
        chart_every = max(1, round(30 / interval))  # re-render image every ~30s
        try:
            for tick in range(max_ticks):
                try:
                    tree = (await session.process_tree())[:4000]
                    embed = discord.Embed(color=0x5865F2, description=tree)
                    embed.set_footer(text="🟨 CPU % · 🟦 RSS GB · x = minutes ago · "
                                          f"updates every {interval:.0f}s · "
                                          "cleared when Claude replies")
                    new_chart = None
                    if len(samples) >= 2 and tick % chart_every == 0:
                        png = await asyncio.to_thread(render_chart, list(samples))
                        new_chart = discord.File(io.BytesIO(png), "ps.png")
                    if new_chart is not None:
                        embed.set_image(url="attachment://ps.png")
                        if ps_msg is None:
                            ps_msg = await (await get_chan()).send(embed=embed, file=new_chart)
                        else:
                            await ps_msg.edit(embed=embed, attachments=[new_chart])
                    else:
                        if ps_msg is not None and ps_msg.attachments:
                            embed.set_image(url="attachment://ps.png")  # keep old chart
                        if ps_msg is None:
                            ps_msg = await (await get_chan()).send(embed=embed)
                        else:
                            await ps_msg.edit(embed=embed)
                except discord.HTTPException as err:
                    LOG.warning(f"ps embed update failed: {err}")  # retry next tick
                await asyncio.sleep(interval)
            # lifetime expired naturally: don't leave a stale embed behind
            if ps_msg is not None:
                try:
                    await ps_msg.delete()
                except discord.HTTPException:
                    pass
                ps_msg = None
        except asyncio.CancelledError:
            pass

    async def clear_ps() -> None:
        nonlocal ps_msg, ps_task
        if ps_task is not None:
            ps_task.cancel()
            ps_task = None
        if ps_msg is not None:
            try:
                await ps_msg.delete()
            except discord.HTTPException:
                pass
            ps_msg = None

    async def relay(text: str) -> None:
        touch()
        await clear_ps()  # a real reply supersedes the live process view
        chunks = chunk_message(text)
        if len(chunks) > 5:  # multi-page reply: attach as a file, don't spam
            preview = chunk_message(text, 1800)[0]
            LOG.info(f"relay: {len(text):,} chars as file attachment")
            enqueue(f"{preview}\n-# 📄 long reply — full text attached ({len(text):,} chars)",
                    file_bytes=text.encode(), filename="reply.md")
            return
        LOG.info(f"relay: {len(text):,} chars in {len(chunks)} chunk(s)")
        for chunk in chunks:
            enqueue(chunk)

    # --- live tool-status message: one message edited as tools run -------
    tool_log: deque[str] = deque(maxlen=5)
    status_msg: discord.Message | None = None
    last_status_edit = 0.0

    def render_status() -> str:
        return "\n".join(["⚙️ **working**"] + [f"-# 🔧 {d}" for d in tool_log])

    async def on_tool(desc: str) -> None:
        nonlocal status_msg, last_status_edit
        touch()
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
        LOG.info(f"turn end: {fmt_duration(stats['seconds'])}, "
                 f"{stats['tools']} tools, {stats['output_tokens']:,} output tokens")
        await finalize_status(f"-# ✅ {fmt_duration(stats['seconds'])} · "
                              f"{stats['tools']} tool calls · "
                              f"{stats['output_tokens']:,} output tokens")
        await clear_working()

    async def clear_working() -> None:
        turn_done.set()  # stops the typing indicator
        while pending:
            msg, emoji = pending.pop()
            try:
                await msg.remove_reaction(emoji, client.user)
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
        touch()
        turn_done.clear()
        if typing_task is None or typing_task.done():
            typing_task = asyncio.create_task(typing_until_done())

    @client.event
    async def on_ready() -> None:
        nonlocal watcher_started, watcher
        log(f"Logged in as {client.user}; bridging {session.work_dir} "
            f"<-> channel {channel_id}")
        if not watcher_started:
            watcher_started = True
            watcher = TranscriptWatcher(
                session, relay, on_turn_end=on_turn_end, on_tool=on_tool)
            asyncio.create_task(sender_loop())
            asyncio.create_task(watcher.run())
            asyncio.create_task(idle_watchdog(watcher))
            asyncio.create_task(sampler())

    IDLE_AFTER = 120  # auto-open the process monitor after 2 quiet minutes

    async def idle_watchdog(watcher: TranscriptWatcher) -> None:
        nonlocal ps_task
        while True:
            await asyncio.sleep(15)
            try:
                mid_turn = watcher.turn_started is not None
                quiet = time.time() - last_activity > IDLE_AFTER
                ps_idle = ps_msg is None and (ps_task is None or ps_task.done())
                if mid_turn and quiet and ps_idle:
                    LOG.info(f"no visible activity for {IDLE_AFTER}s mid-turn — "
                             "auto-opening process monitor (30s refresh)")
                    ps_task = asyncio.create_task(ps_updater(interval=30, max_ticks=240))
            except Exception:
                LOG.exception("idle watchdog error")

    @client.event
    async def on_message(message: discord.Message) -> None:
        nonlocal ps_task
        if message.author.bot:
            return
        if message.channel.id != channel_id or message.author.id != user_id:
            return
        content = message.content.strip()
        if not content and not message.attachments:
            return
        if content == "!new":
            LOG.info("command: !new")
            await session.start_fresh()
            await finalize_status("-# 🆕 session restarted")
            await clear_working()
            await clear_ps()
            enqueue("🆕 Started a fresh Claude session.")
            return
        if content == "!esc":
            LOG.info("command: !esc")
            await session.interrupt()
            await finalize_status("-# 🛑 interrupted")
            await clear_working()  # interrupted turns never write end_turn
            await clear_ps()
            await message.add_reaction("🛑")
            return
        if content == "!bg":
            LOG.info("command: !bg")
            await session.background_tool()
            await message.add_reaction("⏬")
            return
        if content == "!peek":
            pane = (await session.capture()).replace("```", "`​``")
            enqueue(f"```\n{pane[-1900:] or '(empty pane)'}\n```")
            return
        if content == "!ps":
            await clear_ps()  # restart fresh if one is already live
            ps_task = asyncio.create_task(ps_updater())
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
            enqueue(embed=embed)
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
        LOG.info(f"forward -> claude: {len(content)} chars"
                 + (f", {len(message.attachments)} attachment(s)" if message.attachments else ""))
        mid_turn = watcher is not None and watcher.turn_started is not None
        await session.send("\n\n".join(parts))
        begin_turn()
        try:
            emoji = "📨" if mid_turn else WORKING  # queued into a running turn?
            await message.add_reaction(emoji)
            pending.append((message, emoji))
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
