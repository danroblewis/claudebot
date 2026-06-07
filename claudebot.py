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
CONTEXT_LIMIT = 1_000_000  # assume the 1M context window
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


def ps_by_name(ps_output: str, session_id: str, all_claudes: bool = False) -> dict | None:
    """{label: (cpu%, rss_kb)} with one entry per process — no aggregation;
    labels are "name·pid" (pid keeps line identity stable across samples).
    Roots: the bridged claude — or, with all_claudes (container mode), every
    claude process, so orchestrator setups with multiple sessions are fully
    covered. The bridged claude is labeled "claude"; other instances
    "claude:2", "claude:3", ..."""
    procs, children, claude_pid = parse_ps(ps_output, session_id)
    parent_of = {kid: p for p, kids in children.items() for kid in kids}

    def cmd_name(cmd: str) -> str:
        parts = cmd.split()
        return Path(parts[0]).name if parts else "?"

    claude_pids = {pid for pid, t in procs.items()
                   if cmd_name(t[3]) == "claude" and "<defunct>" not in t[3]}
    if claude_pid is not None:
        claude_pids.add(claude_pid)

    if all_claudes:
        def under_claude(pid: int) -> bool:
            parent = parent_of.get(pid)
            while parent is not None:
                if parent in claude_pids:
                    return True
                parent = parent_of.get(parent)
            return False
        roots = sorted(p for p in claude_pids if not under_claude(p))
    else:
        roots = [claude_pid] if claude_pid is not None else []
    if not roots:
        return None

    labels = {p: f"claude:{i + 2}"
              for i, p in enumerate(sorted(p for p in claude_pids if p != claude_pid))}
    if claude_pid is not None:
        labels[claude_pid] = "claude"

    agg: dict[str, tuple[float, int]] = {}
    def walk(pid: int) -> None:
        cpu, rss, _, cmd = procs[pid]
        if pid not in claude_pids and (any(p in cmd for p in PS_INFRA)
                                       or "<defunct>" in cmd):
            return
        label = labels.get(pid) or f"{cmd_name(cmd)}·{pid}"
        agg[label] = (cpu, rss)
        for kid in children.get(pid, []):
            walk(kid)
    for root in roots:
        walk(root)
    return agg


# Discord dark-theme embed colors
CHART_BG = "#2b2d31"      # embed background — chart blends in seamlessly
CHART_FG = "#80848e"      # Discord secondary text
# line palette, paired with emoji for the text legend in the embed footer
CHART_COLORS = (("🟨", "#f0b132"), ("🟦", "#5865f2"), ("🟥", "#ed4245"),
                ("🟩", "#57f287"), ("🟪", "#a55ee8"), ("🟧", "#e67e22"),
                ("⬜", "#b5bac1"))


def chart_window(samples, seconds: float) -> list:
    cutoff = samples[-1][0] - seconds
    return [s for s in samples if s[0] >= cutoff]


def window_means(samples, seconds: float = 120) -> dict:
    """{name: (mean cpu%, mean rss_kb)} over the trailing window; a process
    absent from a sample counts as 0 (so deaths pull the mean down)."""
    if not samples:
        return {}
    recent = chart_window(samples, seconds)
    names = {n for _, procs in recent for n in procs}
    means = {}
    for name in names:
        cpus = [procs.get(name, (0.0, 0))[0] for _, procs in recent]
        rsss = [procs.get(name, (0.0, 0))[1] for _, procs in recent]
        means[name] = (sum(cpus) / len(recent), sum(rsss) / len(recent))
    return means


def change_score(prev: dict, cur: dict) -> float:
    """How different the world looks vs the last render. Units: ~1.0 means
    'a process swung by a full core' or 'half a GB moved'."""
    score = 0.0
    for name in set(prev) | set(cur):
        p_cpu, p_rss = prev.get(name, (0.0, 0))
        c_cpu, c_rss = cur.get(name, (0.0, 0))
        score += abs(c_cpu - p_cpu) / 100 + abs(c_rss - p_rss) / 512000
        if (name in cur) != (name in prev) and max(p_cpu, c_cpu) > 5:
            score += 0.5  # a real process appeared or vanished
    return score


def chart_fingerprint(samples) -> tuple:
    """Value signature of the window, timestamp-free and quantized (CPU to
    1%, RSS to 10MB) — when this is unchanged, the rendered chart would look
    identical, so the image upload can be skipped (e.g. everything idle)."""
    return tuple(
        tuple(sorted((name, round(vals[0]), vals[1] // 10240,
                      vals[2] if len(vals) > 2 else 1)
                     for name, vals in procs.items()))
        for _, procs in samples
    )


def render_chart(samples) -> tuple[bytes, str]:
    """Side-by-side panels (CPU% left, RSS right), one line per process that
    showed CPU > 0 anywhere in the window. CPU axis spans at least 0-100%
    but expands for multi-core (>100%) processes; RSS axis tops out at
    observed max but never below 100MB.
    Returns (png, footer line) — legend text is cheaper than pixels.
    samples = [(t, {name: (cpu, rss_kb)})]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    now = samples[-1][0]
    xs = [(s[0] - now) / 60 for s in samples]  # minutes ago (<= 0)
    peak: dict[str, float] = {}
    peak_rss_mb = 0.0
    for _, procs in samples:
        for label, vals in procs.items():
            peak[label] = max(peak.get(label, 0.0), vals[0])
            peak_rss_mb = max(peak_rss_mb, vals[1] / 1024)
    # lines are per-PID ("name·pid"), but color and legend group by name
    def group_of(label: str) -> str:
        return label.rsplit("·", 1)[0]
    group_peak: dict[str, float] = {}
    members: dict[str, list[str]] = {}
    for label, p in peak.items():
        if p <= 0:
            continue
        grp = group_of(label)
        group_peak[grp] = max(group_peak.get(grp, 0.0), p)
        members.setdefault(grp, []).append(label)
    active_groups = sorted(group_peak, key=lambda g: -group_peak[g])
    shown_groups = active_groups[:len(CHART_COLORS)]
    shown = [lbl for grp in shown_groups for lbl in members[grp]]

    rss_max_mb = max(100.0, peak_rss_mb * 1.05)  # minimum-max 100MB
    use_gb = rss_max_mb >= 1000
    rss_div = 1048576 if use_gb else 1024  # kb -> GB or MB
    rss_unit = "GB" if use_gb else "MB"
    # CPU axis: floor of 100%, but expands so multi-core processes (>100%)
    # stay on screen instead of clipping at the ceiling
    cpu_max = max([100.0] + [peak[lbl] * 1.05 for lbl in shown])

    fig, (ax_cpu, ax_rss) = plt.subplots(1, 2, figsize=(5.8, 1.6), dpi=80)
    fig.patch.set_facecolor(CHART_BG)
    nan = float("nan")
    for (_, color), grp in zip(CHART_COLORS, shown_groups):
        for label in members[grp]:
            cpu_series = [s[1][label][0] if label in s[1] else nan for s in samples]
            rss_series = [s[1][label][1] / rss_div if label in s[1] else nan for s in samples]
            # markers so short-lived processes (isolated samples between NaN
            # gaps) are still visible as dots
            ax_cpu.plot(xs, cpu_series, color=color, linewidth=1.2,
                        marker=".", markersize=2.2)
            ax_rss.plot(xs, rss_series, color=color, linewidth=1.2,
                        marker=".", markersize=2.2)
    ax_cpu.set_ylim(0, cpu_max)
    ax_rss.set_ylim(0, rss_max_mb / (1024 if use_gb else 1))
    for ax in (ax_cpu, ax_rss):
        ax.set_facecolor(CHART_BG)
        ax.locator_params(axis="y", nbins=4)
        ax.locator_params(axis="x", nbins=5)
        ax.tick_params(labelsize=7, colors=CHART_FG)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.grid(True, color="#404249", linewidth=0.4, alpha=0.5)
    fig.tight_layout(pad=0.3, w_pad=1.0)

    raw = io.BytesIO()
    fig.savefig(raw, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    # palette-quantize: line charts have few colors, so this halves the size
    raw.seek(0)
    img = Image.open(raw).convert("RGB").quantize(colors=48)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)

    legend = " · ".join(
        f"{emoji} {grp}" + (f" ×{len(members[grp])}" if len(members[grp]) > 1 else "")
        for (emoji, _), grp in zip(CHART_COLORS, shown_groups))
    if len(active_groups) > len(shown_groups):
        legend += f" · +{len(active_groups) - len(shown_groups)} more"
    footer = f"{legend} · left CPU % · right RSS {rss_unit} · x = min ago"
    return out.getvalue(), footer


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

TASK_ICONS = {"completed": "🟩", "in_progress": "🟨", "pending": "⬜",
              "cancelled": "⬛", "blocked": "🟥"}


def load_tasks(cfg: dict, session_id: str) -> list[dict]:
    """Claude Code's task list: ~/.claude/tasks/<session-id>/N.json."""
    home = CONTAINER_HOME if container_enabled(cfg) else Path.home()
    task_dir = home / ".claude" / "tasks" / session_id
    tasks = []
    for path in task_dir.glob("*.json") if task_dir.is_dir() else []:
        try:
            task = json.loads(path.read_text())
            if isinstance(task, dict) and task.get("subject"):
                tasks.append(task)
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(tasks, key=lambda t: (len(str(t.get("id", ""))), str(t.get("id", ""))))


def render_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "no tasks in this session"
    done = sum(1 for t in tasks if t.get("status") == "completed")
    lines = [f"**Tasks** ({done}/{len(tasks)} done)"]
    for t in tasks:
        icon = TASK_ICONS.get(t.get("status"), "❔")
        subject = str(t.get("subject", ""))[:90]
        line = f"{icon} {t.get('id', '?')} {subject}"
        if t.get("blockedBy"):
            line += f" · ← {','.join(str(b) for b in t['blockedBy'])}"
        lines.append(line)
    out = "\n".join(lines)
    return out[:1990] + "…" if len(out) > 1995 else out


def context_from_usage(usage: dict) -> int:
    """Context consumed by an API call = its full input + output."""
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("output_tokens") or 0))


def fmt_context(tokens: int) -> str:
    return f"ctx {tokens / 1000:.0f}k/1M ({tokens / CONTEXT_LIMIT:.0%})"


def latest_context_tokens(path: Path) -> int | None:
    """Context size from the newest assistant line in a transcript (reads
    only the file tail — used when the live watcher hasn't seen a line yet)."""
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 262144))
            data = f.read()
    except OSError:
        return None
    best = None
    for line in data.splitlines():  # first line may be partial; json skips it
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            ctx = context_from_usage(entry.get("message", {}).get("usage") or {})
            if ctx:
                best = ctx
    return best


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
        self.context_tokens: int | None = None  # latest API call's total input+output
        self.sidechain_seen = 0.0  # last time a subagent line streamed

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
        if entry.get("type") != "assistant":
            return
        if entry.get("isSidechain"):
            self.sidechain_seen = time.time()  # a subagent is actively working
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
        usage = msg.get("usage") or {}
        self.turn_tokens += usage.get("output_tokens") or 0
        ctx = context_from_usage(usage)
        if ctx:
            self.context_tokens = ctx
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


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    s = line.strip()
    return _is_table_row(line) and set(s) <= set("|-: ")


def _render_table(rows: list[str]) -> str:
    """Markdown table rows -> column-aligned text in a code block."""
    parsed = []
    for row in rows:
        cells = [c.strip().replace("**", "").replace("`", "")
                 for c in row.strip().strip("|").split("|")]
        parsed.append(cells)
    has_separator = len(parsed) > 1 and all(
        c and set(c) <= set("-: ") for c in parsed[1])
    if has_separator:
        parsed.pop(1)
    ncols = max(len(p) for p in parsed)
    for p in parsed:
        p += [""] * (ncols - len(p))
    widths = [max(len(p[col]) for p in parsed) for col in range(ncols)]
    def fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    lines = [fmt(parsed[0])]
    if has_separator:
        lines.append("  ".join("─" * w for w in widths))
    lines.extend(fmt(p) for p in parsed[1:])
    return "```\n" + "\n".join(lines) + "\n```"


def convert_tables(text: str) -> str:
    """Discord never renders markdown tables — re-render them as aligned
    text in code blocks. Tables already inside code fences are left alone."""
    lines = text.split("\n")
    out: list[str] = []
    i, in_fence = 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if (not in_fence and _is_table_row(line)
                and i + 1 < len(lines) and _is_table_separator(lines[i + 1])):
            block = []
            while i < len(lines) and _is_table_row(lines[i]):
                block.append(lines[i])
                i += 1
            out.append(_render_table(block))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


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

    # --- live !ps monitor: auto-updates until Claude actually replies ------
    ps_msg: discord.Message | None = None
    ps_task: asyncio.Task | None = None
    ps_ctl: dict = {}  # {"force": True} -> immediate render + ladder reset
    last_activity = time.time()

    def touch() -> None:
        nonlocal last_activity
        last_activity = time.time()

    # --- TUI spinner scrape: are tokens actually streaming right now? ------
    # The spinner's `↑ N tokens` counter only moves while the model streams
    # output; it freezes during tool runs / API waits. Granularity above 1k
    # is 0.1k, which flips every ~1-2s at normal streaming speed.
    TOKEN_RE = re.compile(r"↑\s*([\d.,]+k?)\s*tokens")
    token_state = {"value": None, "changed_at": 0.0, "spinner": False}

    async def tui_poller() -> None:
        while True:
            try:
                match = TOKEN_RE.search(await session.capture())
                if match:
                    token_state["spinner"] = True
                    if match.group(1) != token_state["value"]:
                        token_state["value"] = match.group(1)
                        token_state["changed_at"] = time.time()
                else:
                    token_state["spinner"] = False
                    token_state["value"] = None
            except Exception:
                pass
            await asyncio.sleep(3)

    def generating() -> bool:
        return token_state["spinner"] and time.time() - token_state["changed_at"] < 10

    # continuous resource sampling so a freshly opened monitor has history
    samples: deque = deque(maxlen=360)  # (t, {name: (cpu%, rss_kb)}); 1h at 10s

    async def sampler() -> None:
        in_container = container_enabled(session.config)
        while True:
            try:
                by_name = ps_by_name(await session.ps_output(),
                                     session.state["session_id"],
                                     all_claudes=in_container)
                if by_name is not None:
                    samples.append((time.time(), by_name))
            except Exception:
                pass  # container down between sessions etc.
            await asyncio.sleep(10)

    PS_TICK = 30  # evaluate every 30s; edits follow the decay ladder
    PS_LADDER = (30, 60, 300, 600, 1200, 1800)  # 30s -> 1m -> 5m -> ... -> 30m
    # the reaction shows the current cadence; tapping it snaps back to ⚡
    SPEED_EMOJIS = ("⚡", "🐇", "🐈", "🐢", "🐌", "🦥")

    async def ps_updater() -> None:
        # Plain message, no embed: embeds waste space and Discord bounces
        # replaced attachments in/out of them between edits.
        #
        # Adaptive cadence: every render with no significant change steps the
        # ladder down; a significant change (habituation-adjusted: scored
        # against the session's recent churn via EMA), a 🔄 reaction, or a
        # user message snaps back to 30s.
        nonlocal ps_msg
        legend = ""
        last_fp = None
        ladder = 0
        last_render = 0.0
        last_means: dict | None = None
        ema = 0.0
        shown_speed: str | None = None
        try:
            while True:
                try:
                    means = window_means(list(samples)) if samples else {}
                    score = change_score(last_means, means) if last_means is not None else 0.0
                    significant = last_means is not None and score > max(0.5, 3 * ema)
                    ema = 0.95 * ema + 0.05 * score
                    forced = ps_ctl.pop("force", False)
                    due = time.time() - last_render >= PS_LADDER[ladder]
                    if not (significant or forced or due):
                        await asyncio.sleep(PS_TICK)
                        continue
                    if significant or forced:
                        if ladder and significant:
                            LOG.info(f"ps monitor re-engaged (score {score:.2f}, ema {ema:.2f})")
                        ladder = 0
                    elif due:
                        ladder = min(ladder + 1, len(PS_LADDER) - 1)
                    last_render = time.time()
                    last_means = means

                    new_chart = None
                    window = chart_window(list(samples), PS_LADDER[ladder] + 600) if samples else []
                    if len(window) >= 2:
                        fp = chart_fingerprint(window)
                        if fp != last_fp:  # skip upload when the picture wouldn't change
                            last_fp = fp
                            png, legend = await asyncio.to_thread(render_chart, window)
                            new_chart = discord.File(io.BytesIO(png), "ps.png")
                    if generating():
                        status = f"⚡ generating · ↑{token_state['value']} tokens"
                    elif token_state["spinner"]:
                        waiting = fmt_duration(time.time() - token_state["changed_at"])
                        status = f"⏳ tokens static {waiting} — waiting on tool/API"
                    else:
                        status = "💤 no turn running"
                    if watcher and watcher.context_tokens:
                        status += f" · {fmt_context(watcher.context_tokens)}"
                    status += f" · {SPEED_EMOJIS[ladder]}{fmt_duration(PS_LADDER[ladder])}"
                    active = [t for t in load_tasks(session.config, session.state["session_id"])
                              if t.get("status") == "in_progress" and t.get("activeForm")]
                    if active:
                        status += "\n-# 🟨 " + " · ".join(t["activeForm"][:60] for t in active[:3])
                    content = status + (f"\n-# {legend}" if legend else "")
                    if ps_msg is None:
                        ps_msg = await (await get_chan()).send(
                            content,
                            file=new_chart if new_chart is not None else discord.utils.MISSING)
                        await ps_msg.add_reaction("❌")  # close
                    elif new_chart is not None:
                        await ps_msg.edit(content=content, attachments=[new_chart])
                    else:
                        await ps_msg.edit(content=content)
                    # keep the speed reaction in sync with the cadence
                    desired = SPEED_EMOJIS[ladder]
                    if desired != shown_speed:
                        try:
                            if shown_speed:
                                await ps_msg.remove_reaction(shown_speed, client.user)
                            await ps_msg.add_reaction(desired)
                            shown_speed = desired
                        except discord.HTTPException:
                            pass
                except discord.HTTPException as err:
                    LOG.warning(f"ps monitor update failed: {err}")  # retry next tick
                await asyncio.sleep(PS_TICK)
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
        # claude's canned no-op when an automated event (e.g. a background
        # task completion notification) needs no reply — noise in Discord
        if text.strip().rstrip(".") == "No response requested":
            LOG.info("relay: suppressed no-op reply")
            return
        touch()
        await clear_ps()  # a real reply supersedes the live process view
        text = convert_tables(text)
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
        # NB: deliberately does NOT touch() the idle timer — background-task
        # polling produces steady tool events, and the monitor should open on
        # "no messages for a while" even when tools are ticking
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
        ctx_tokens = (watcher.context_tokens or 0) if watcher else 0
        LOG.info(f"turn end: {fmt_duration(stats['seconds'])}, "
                 f"{stats['tools']} tools, {stats['output_tokens']:,} output tokens"
                 + (f", {fmt_context(ctx_tokens)}" if ctx_tokens else ""))
        # context note only when it's getting tight
        warn = (f" · ⚠️ ctx {ctx_tokens / CONTEXT_LIMIT:.0%}"
                if ctx_tokens > CONTEXT_LIMIT * 0.8 else "")
        await finalize_status(f"-# {fmt_duration(stats['seconds'])} · "
                              f"{stats['tools']} tool calls · "
                              f"{stats['output_tokens']:,} output tokens{warn}")
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
            asyncio.create_task(tui_poller())

    IDLE_AFTER = 60  # auto-open the process monitor after 1 quiet minute

    async def idle_watchdog(watcher: TranscriptWatcher) -> None:
        nonlocal ps_task
        while True:
            await asyncio.sleep(15)
            try:
                # "working" = any of: main turn active, a subagent streaming
                # recently, or monitored processes burning CPU (covers
                # backgrounded tasks grinding between turns)
                mid_turn = watcher.turn_started is not None
                subagents = time.time() - watcher.sidechain_seen < 180
                busy_procs = bool(samples) and any(
                    v[0] >= 10 for k, v in samples[-1][1].items()
                    if not k.startswith("claude"))
                working = mid_turn or subagents or busy_procs
                quiet = time.time() - last_activity > IDLE_AFTER
                ps_idle = ps_msg is None and (ps_task is None or ps_task.done())
                # a long generation isn't a stall — tokens are visibly flowing
                if working and quiet and ps_idle and not generating():
                    reason = ("turn" if mid_turn else
                              "subagent" if subagents else "processes")
                    LOG.info(f"no messages for {IDLE_AFTER}s while working "
                             f"({reason}) — auto-opening process monitor")
                    ps_task = asyncio.create_task(ps_updater())
            except Exception:
                LOG.exception("idle watchdog error")

    @client.event
    async def on_raw_reaction_add(payload) -> None:
        if (ps_msg is None or payload.message_id != ps_msg.id
                or payload.user_id != user_id):
            return
        if str(payload.emoji) == "❌":
            LOG.info("ps monitor closed via ❌ reaction")
            touch()  # give the watchdog a fresh idle window before reopening
            await clear_ps()
        elif str(payload.emoji) in SPEED_EMOJIS:
            LOG.info("ps monitor cadence reset via speed reaction")
            ps_ctl["force"] = True
            try:  # remove the user's tap so the indicator stays clean
                channel = await get_chan()
                msg = await channel.fetch_message(payload.message_id)
                await msg.remove_reaction(payload.emoji, discord.Object(id=user_id))
            except discord.HTTPException:
                pass

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
        if content.startswith("!/") and len(content) > 2:
            slash = content[1:]
            LOG.info(f"slash command: {slash.split()[0]}")
            await session.send(slash)
            await message.add_reaction("✅")
            return
        if content == "!help":
            enqueue(
                "**claudebot commands**\n"
                "`!peek` — show the live claude TUI pane\n"
                "`!ps` — live activity monitor: status line + per-process CPU/RSS "
                "chart. Updates fast (30s) while metrics move, decaying to every "
                "30min when things are steady (⚡🐇🐈🐢🐌🦥 reaction = current speed; "
                "tap it to snap back to ⚡). Significant changes or any message from "
                "you also re-engage. React ❌ to close. Auto-opens after 1 "
                "message-quiet minute mid-turn\n"
                "`!tasks` — Claude's task list for this session (🟩 done · 🟨 active · "
                "⬜ pending)\n"
                "`!esc` — interrupt the current turn (escapes until the prompt returns)\n"
                "`!bg` — move the running Bash tool to the background (ctrl+b) so the "
                "turn continues and queued messages get read\n"
                "`!new` — start a fresh claude session in the same directory\n"
                "`!status` — session card: workspace, session id, container, transcript\n"
                "`!/<command>` — send any claude slash command (Discord hijacks bare "
                "`/`): `!/compact`, `!/clear`, `!/goal do thing`… TUI-only output "
                "(e.g. `!/cost`) is visible via `!peek`\n"
                "`!help` — this message\n\n"
                "Anything else is forwarded to Claude — 👀 while it works, 📨 if queued "
                "into a turn that's already running (read between tool calls, full "
                "context). Attachments are saved and their paths passed along. Replies "
                "longer than ~5 messages arrive as a `reply.md` attachment.\n"
                "-# from the terminal: `claudebot` (re)attaches · `claudebot --continue` "
                "resumes · `claudebot --stop` tears down")
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
        if content == "!tasks":
            enqueue(render_tasks(load_tasks(session.config, session.state["session_id"])))
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
            tasks = load_tasks(session.config, session.state["session_id"])
            if tasks:
                counts = {}
                for t in tasks:
                    counts[t.get("status", "?")] = counts.get(t.get("status", "?"), 0) + 1
                embed.add_field(name="Tasks",
                                value=" · ".join(f"{TASK_ICONS.get(s, '❔')} {n}"
                                                 for s, n in sorted(counts.items())),
                                inline=True)
            ctx = ((watcher and watcher.context_tokens)
                   or latest_context_tokens(transcript))
            if ctx:
                bar_fill = round(ctx / CONTEXT_LIMIT * 10)
                embed.add_field(name="Context",
                                value=f"{'▰' * bar_fill}{'▱' * (10 - bar_fill)} "
                                      f"{fmt_context(ctx)}",
                                inline=False)
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
        if ps_task is not None and not ps_task.done():
            ps_ctl["force"] = True  # you're back: fresh chart, fast cadence
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
