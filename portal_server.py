#!/usr/bin/env python3
"""fleetdeck portal — the front door to every server and agent on this machine.

A single-screen launcher: one tile per local service, each with its own glyph,
a live status lamp, and a link that actually resolves from the phone. Plus one
tile per launchd agent, which is the half a port scan can never see.

The honesty rule: a tile NEVER shows a link the portal has not resolved from
live system state. Three sources, all read fresh on every scan:

  1. `lsof -nP -iTCP -sTCP:LISTEN` — is the port listening, and what did it bind?
     A service on 127.0.0.1 is unreachable from the phone no matter how much we
     would like it to be, and it gets a "host only" badge, not a dead link.
  2. `tailscale serve status --json` — the escape hatch for (1). Loopback-bound
     services that Tailscale already proxies get their real public URL, so they
     are tappable after all.
  3. `launchctl list` + `~/Library/LaunchAgents/*.plist` — the scheduled jobs.
     These mostly do not listen on anything, so (1) and (2) are blind to them.

Nothing about a URL is hardcoded. Move a service to a new port, add a
`tailscale serve` mapping, kill a container — the next scan tells the truth
about it without an edit here.

Configuration: config.json next to this file (brand, machine, ports, filters).
Registry:      services.json (hot-reloaded per scan — adding an app is one line).

Access: no password. The socket binds 127.0.0.1 and `tailscale serve` fronts it
with real HTTPS on the tailnet:

    tailscale serve --bg --https=8790 http://127.0.0.1:8790

That front matters more than it looks. Served raw on 0.0.0.0 this failed three
ways an iPhone reports identically as "it won't load":

  1. https://…:8790 was a hard TLS failure (`tlsv1 alert protocol version`).
     Safari upgrades typed hostnames to HTTPS first, so every open paid a failed
     handshake and a fallback — and any saved https bookmark simply never loaded.
  2. A plain-HTTP origin is not a secure context: no service worker, and
     Add to Home Screen degrades.
  3. Reached over the local Wi-Fi instead of the tailnet, the request arrived
     from 192.168.x.x and got a bare 403.

Behind `tailscale serve` all three go away: one https:// URL with a real
Let's Encrypt cert, a secure origin, and requests that arrive from 127.0.0.1.
The tailnet boundary is unchanged — Tailscale enforces it at the proxy now
instead of this process enforcing it per-request, so the local Wi-Fi still
cannot enumerate the box. The per-request check below stays as defence in
depth for the loopback socket. FLEETDECK_OPEN=1 lifts it; FLEETDECK_BIND
overrides the bind if the front is ever removed.

Stdlib only. No build step, no pip, no node.
"""

import ipaddress
import json
import os
import plistlib
import re
import fcntl
import base64
import binascii
import hmac
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlsplit
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(HERE, "config.json")
REGISTRY = os.path.join(HERE, "services.json")
GLYPHS = os.path.join(HERE, "glyphs.json")
ICONS = os.path.join(HERE, "icons")     # per-service PNGs from make-icons.py
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
TSBIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# The one built-in glyph. Everything else lives in glyphs.json, which
# make-icons.py reads too — the board and the home-screen icons are drawn from
# the same paths, so they cannot drift. If that file is unreadable the board
# still renders, every tile just wearing this.
FALLBACK_GLYPH = '<path d="M3 4h18v6H3zM3 14h18v6H3z"/><path d="M7 7h.01M7 17h.01"/>'

DEFAULTS = {
    "brand": "fleetdeck",
    "machine": "",           # "" → resolved from Tailscale at boot
    "label_prefix": "",      # e.g. "com.acme" — stripped from agent tile names
    "ports": {"portal": 8790, "chat": 8783, "ttyd": 8784, "adopt": 8793},
    "agents": {"show": True, "actions": False, "include": [], "exclude": []},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CFG_FILE) as fh:
            user = json.load(fh)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        sys.stderr.write(f"fleetdeck: bad config.json ({e}) — using defaults\n")
    return cfg


CONF = load_config()
SETUP_STATE_PATH = os.path.expanduser(os.environ.get(
    "FLEETDECK_SETUP_STATE_PATH", "~/.wideband/setup/state.json"))
FIRST_GOAL_STATUS_PATH = os.path.expanduser(os.environ.get(
    "FLEETDECK_FIRST_GOAL_STATUS_PATH", "~/.wideband/first-goal/status.json"))


def _private_json(path):
    """Read a bounded regular JSON file owned by this user with mode 0600."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 64 * 1024):
                return None
            raw = os.read(fd, 64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return None
            data = json.loads(raw)
        finally:
            os.close(fd)
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def setup_onboarding_state():
    """Read only display choices from the private setup state."""
    raw = _private_json(SETUP_STATE_PATH)
    if not raw:
        return None
    data = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else raw
    return {key: data.get(key) for key in (
        "os_name", "agent_name", "first_goal", "first_project_url",
        "head_session")}


def _phone_url(url):
    """Only an HTTPS URL can leave the Mac for the installed phone app."""
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        return ""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.netloc
                and not parsed.username and not parsed.password):
            return url
    except ValueError:
        pass
    return ""


def first_goal_status(info):
    """Project links come only from a private status file or live service scan."""
    data = _private_json(FIRST_GOAL_STATUS_PATH) or {}
    if (data.get("goal") != info["first_goal"]
            or data.get("os_name") != info["os_name"]
            or data.get("agent_name") != info["agent_name"]):
        return {"status": "pending", "phone_url": "",
                "local_only": False, "identity_match": False}
    return {
        "status": str(data.get("status") or "pending")[:32],
        "phone_url": _phone_url(data.get("phone_url"))
        if data.get("status") == "ready" else "",
        "local_only": bool(data.get("local_url")),
        "identity_match": True,
    }


def onboarding_config():
    """The installer supplies display data; missing data keeps the operator UI."""
    data = (CONF["onboarding"] if "onboarding" in CONF
            else setup_onboarding_state())
    if not isinstance(data, dict) or not data.get("os_name") or not data.get("agent_name"):
        return None
    goal = data.get("first_goal")
    if goal not in ("research", "website", "proposal"):
        goal = "research"
    session = data.get("head_session") or "wb-head"
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
        session = "wb-head"
    url = data.get("first_project_url") or ""
    url = _phone_url(url)
    return {
        "os_name": str(data["os_name"])[:64],
        "agent_name": str(data["agent_name"])[:64],
        "first_goal": goal,
        "first_project_url": url,
        "head_session": session,
    }


CONTROL_TOKEN = os.environ.get("FLEETDECK_CONTROL_TOKEN", "")


def control_authorized(header):
    """Basic auth is an explicit unlock for portal controls in customer mode."""
    if len(CONTROL_TOKEN) < 16 or not header or not header.startswith("Basic "):
        return False
    try:
        userpass = base64.b64decode(header[6:], validate=True).decode("utf-8")
        _, password = userpass.split(":", 1)
    except (ValueError, UnicodeError, binascii.Error):
        return False
    return hmac.compare_digest(password, CONTROL_TOKEN)


def tailnet_name():
    """This machine's MagicDNS name. Resolved once, at boot, from Tailscale
    itself — never hardcoded, because a hardcoded tailnet name is the single
    thing most likely to be wrong on a machine that is not the author's."""
    for exe in (TSBIN, "tailscale"):
        try:
            raw = subprocess.run([exe, "status", "--json"], capture_output=True,
                                 text=True, timeout=8).stdout
            return json.loads(raw)["Self"]["DNSName"].rstrip(".")
        except Exception:
            continue
    return os.uname().nodename


# Who may read the portal. 100.64/10 is Tailscale's CGNAT range and
# fd7a:115c:a1e0::/48 its IPv6 ULA — between them, every tailnet peer.
# Loopback covers `fleetdeck status`.
ALLOWED_NETS = [
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]
OPEN_TO_ALL = os.environ.get("FLEETDECK_OPEN") == "1"

BRAND = os.environ.get("FLEETDECK_BRAND", CONF["brand"])
HOST = os.environ.get("FLEETDECK_HOST") or CONF["machine"] or tailnet_name()
PORT = int(os.environ.get("FLEETDECK_PORT", CONF["ports"]["portal"]))
BIND = os.environ.get("FLEETDECK_BIND", "127.0.0.1")
MACHINE = HOST.split(".")[0]
SCAN_TTL = 4.0  # seconds; a phone poll every 10s should not fork lsof each time

SHOW_AGENTS = CONF["agents"].get("show", True)
# Actions mutate the machine from a surface that has NO password — the tailnet
# bind is the only boundary. That is a defensible read-only posture and a much
# bigger claim for start/stop, so it is off unless switched on deliberately.
AGENT_ACTIONS = (os.environ.get("FLEETDECK_AGENT_ACTIONS") == "1"
                 or CONF["agents"].get("actions", False))
# Vendor agents are not the operator's fleet — they are the OS and its tenants.
AGENT_NOISE = re.compile(
    r"^(com\.apple\.|com\.google\.|com\.microsoft\.|com\.adobe\.|com\.valve"
    r"|homebrew\.mxcl\.|com\.docker\.|org\.mozilla\.|com\.electron\."
    r"|com\.tailscale\.|com\.zoom\.|us\.zoom\.|com\.spotify\.|com\.dropbox)",
    re.I,
)

# Commands that listen but are never "apps" — OS services, editors, VM plumbing.
NOISE = re.compile(
    r"(rapportd|ControlCe|ARDAgent|sharingd|Adobe|Creative|dynamicli|TeamProje"
    r"|identityservices|cloudflar|lmlink|Google|chrome|Slack|Spotify|Docker"
    r"|limactl|Code|Electron|ttyd|LM.{0,4}Stu|node.?_?modules)",
    re.I,
)
# Ports that back a registered app rather than standing on their own (the chat
# server spawns its own ttyd and proxies it under /t, so that port is plumbing,
# not an app, and listing it would invite a tap that lands nowhere).
INTERNAL = {int(CONF["ports"].get("ttyd", 8784))}


def esc_html(s):
    """Escape before interpolating into a page. HOST is env-supplied and the
    client IP comes off the socket — both are constrained, neither is trusted."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def sh(cmd, timeout=15):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


# ── system truth ──────────────────────────────────────────────────────────────

def listeners():
    """port -> {cmd, cls} where cls is the most permissive bind seen.

    open   — bound to * / 0.0.0.0 / [::]  → reachable across the tailnet
    tailnet— bound to the 100.x tailscale address → reachable across the tailnet
    host   — loopback only → NOT reachable from the phone
    """
    rank = {"host": 0, "tailnet": 1, "open": 2}
    found = {}
    for line in sh("lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null").splitlines()[1:]:
        f = line.split()
        if len(f) < 3 or not f[-1].startswith("("):
            continue
        cmd, addr = f[0], f[-2]
        if ":" not in addr:
            continue
        bind, _, port = addr.rpartition(":")
        if not port.isdigit():
            continue
        if bind in ("*", "0.0.0.0", "[::]", "::"):
            cls = "open"
        elif bind.startswith("100."):
            cls = "tailnet"
        elif bind in ("127.0.0.1", "[::1]", "::1"):
            cls = "host"
        else:
            cls = "open"  # a LAN address still answers over the tailnet
        p = int(port)
        prev = found.get(p)
        if prev is None or rank[cls] > rank[prev["cls"]]:
            found[p] = {"cmd": cmd, "cls": cls}
    return found


def serve_map():
    """local port -> public tailscale URL, for anything `tailscale serve` proxies."""
    raw = sh(f"{TSBIN} serve status --json 2>/dev/null")
    if not raw.strip():
        return {}
    try:
        j = json.loads(raw)
    except Exception:
        return {}
    tcp = j.get("TCP") or {}
    out = {}
    for hostport, cfg in (j.get("Web") or {}).items():
        host, _, hp = hostport.rpartition(":")
        scheme = "https" if (tcp.get(hp) or {}).get("HTTPS") else "http"
        base = f"{scheme}://{host}" if hp in ("443", "80") else f"{scheme}://{host}:{hp}"
        for path, h in (cfg.get("Handlers") or {}).items():
            proxy = h.get("Proxy") or ""
            m = re.search(r":(\d+)/?$", proxy)
            if not m:
                continue
            url = base + (path if path != "/" else "")
            out.setdefault(int(m.group(1)), url)
    return out


# ── launchd ───────────────────────────────────────────────────────────────────
#
# The half a port scan cannot see. Most of an operator's fleet is scheduled work
# — nudges, watchdogs, backups, digests — and none of it listens on a socket, so
# `lsof` reports it as simply absent. Two sources, joined on the label:
#
#   ~/Library/LaunchAgents/*.plist  — what is INSTALLED (and its schedule)
#   launchctl list                  — what is LOADED (and how it last exited)
#
# Reading both is what makes "installed but never bootstrapped" visible, which is
# the most common way a launchd job is quietly doing nothing at all.

INTERPRETERS = re.compile(
    r"^(env|bash|sh|zsh|python3?(\.\d+)?|node|ruby|perl|osascript|caffeinate)$")


def launchd_state():
    """label -> {pid, exit}. Exit status may be negative — that is a signal
    number (-15 = SIGTERM), not a failure code, and gets read as such below."""
    out = {}
    for line in sh("launchctl list", timeout=10).splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 3:
            continue
        pid, status, label = f[0].strip(), f[1].strip(), f[2].strip()
        def num(v):
            try:
                return int(v)
            except ValueError:
                return None
        out[label] = {"pid": num(pid), "exit": num(status)}
    return out


def schedule_of(p):
    """Human schedule, in the words the operator would use."""
    if p.get("StartInterval"):
        n = int(p["StartInterval"])
        if n % 3600 == 0:
            return f"every {n // 3600}h"
        if n % 60 == 0:
            return f"every {n // 60}m"
        return f"every {n}s"
    cal = p.get("StartCalendarInterval")
    if cal:
        times = cal if isinstance(cal, list) else [cal]
        stamps = []
        for c in times:
            if not isinstance(c, dict):
                continue
            h, m = c.get("Hour"), c.get("Minute", 0)
            stamps.append(f"{h:02d}:{m:02d}" if h is not None else f":{m:02d}")
        if len(stamps) == 1:
            return f"daily {stamps[0]}"
        if stamps:
            return f"{len(stamps)}x daily · {stamps[0]}…"
        return "scheduled"
    if p.get("WatchPaths") or p.get("QueueDirectories"):
        return "on file change"
    if p.get("KeepAlive"):
        return "always on"
    if p.get("RunAtLoad"):
        return "at login"
    return "manual"


def program_of(p):
    """Best label for what the job actually runs. `/bin/bash -c foo.sh` should
    read as foo.sh, not bash — the interpreter is never the interesting part."""
    args = p.get("ProgramArguments") or ([p["Program"]] if p.get("Program") else [])
    args = [str(a) for a in args if isinstance(a, (str, bytes))]
    if not args:
        return "—"
    first = os.path.basename(args[0])
    if INTERPRETERS.match(first):
        for a in args[1:]:
            if a.startswith("-"):
                continue
            base = os.path.basename(a.split()[0]) if a.split() else ""
            if base and not INTERPRETERS.match(base):
                return base
    return first


def launch_agents():
    if not SHOW_AGENTS:
        return []
    state = launchd_state()
    inc = [s.lower() for s in CONF["agents"].get("include", [])]
    exc = [s.lower() for s in CONF["agents"].get("exclude", [])]
    prefix = (CONF.get("label_prefix") or "").rstrip(".")
    out = []
    try:
        files = sorted(os.listdir(AGENT_DIR))
    except OSError:
        return []
    for fn in files:
        if not fn.endswith(".plist"):
            continue
        label = fn[:-6]
        # An unreadable plist used to fall through as an empty dict, which
        # rendered a tile with no program, no schedule and "no log" — the exact
        # shape of a badly configured job, for a file that is simply not
        # parseable. Say which it is. This is not hypothetical: XML forbids `--`
        # inside a comment, `plutil -lint` accepts it anyway, and two agents on
        # this machine were silently blank because of a hyphen in a comment.
        unreadable = None
        try:
            with open(os.path.join(AGENT_DIR, fn), "rb") as fh:
                p = plistlib.load(fh)
            label = p.get("Label", label)
        except Exception as err:
            # NB: not `as exc` — that name is the exclude list a few lines down,
            # and Python unbinds an `except ... as` target when the block ends.
            p = {}
            unreadable = str(err)
        low = label.lower()
        if AGENT_NOISE.search(label) and not any(s in low for s in inc):
            continue
        if inc and not any(s in low for s in inc):
            continue
        if any(s in low for s in exc):
            continue

        st = state.get(label)
        running = bool(st and st.get("pid"))
        code = st.get("exit") if st else None
        # A periodic job that is not running right now is NORMAL — it already
        # ran and exited 0. Treating "no PID" as "down" would paint a healthy
        # board red, which is the fastest way to make an operator stop reading
        # it. Only a non-zero, non-signal exit is a failure.
        if st is None:
            health = "off"      # installed on disk, never bootstrapped
        elif running:
            health = "run"
        elif code in (0, None):
            health = "ok"
        elif code < 0:
            health = "ok"       # negative == killed by signal (SIGTERM on reload)
        else:
            health = "fail"

        # launchd keeps no last-run time. Not in `launchctl list`, not in
        # `launchctl print` — there is a cumulative `runs` count and a last exit
        # code, and no clock anywhere. So the honest proxy is when the job last
        # WROTE something: stat the log it declares.
        #
        # This is last OUTPUT, not last run, and the UI says so. A job that runs
        # silently leaves it blank, and conflating the two is exactly how a
        # quiet healthy job comes to look dead.
        #
        # StandardOutPath is already in the plist being parsed here, so this
        # costs one stat and no subprocess. `launchctl print` per agent would be
        # 79 subprocesses on the render path.
        last_output, logged = None, False
        for key in ("StandardOutPath", "StandardErrorPath"):
            path = p.get(key)
            if not isinstance(path, str) or not path:
                continue
            logged = True
            try:
                mtime = os.stat(os.path.expanduser(path)).st_mtime
            except OSError:
                continue  # declared but absent — the job has written nothing
            last_output = mtime if last_output is None else max(last_output, mtime)

        name = label
        if prefix and name.startswith(prefix + "."):
            name = name[len(prefix) + 1:]
        out.append({
            "kind": "agent",
            "label": label,
            "name": name,
            "program": ("plist will not parse" if unreadable else program_of(p)),
            "schedule": schedule_of(p),
            "unreadable": unreadable,
            "health": health,
            "pid": st.get("pid") if st else None,
            "exit": code,
            # None is a real answer: no log path configured, or nothing written
            # yet. The board is rescanned rather than remembered, so an invented
            # timestamp costs more than a blank one. `logged` separates the two
            # absences so the tile can say which it is.
            "last_output": last_output,
            "logged": logged,
        })
    out.sort(key=lambda a: ({"fail": 0, "off": 1, "run": 2, "ok": 3}[a["health"]],
                            a["name"]))
    return out


def load_registry():
    try:
        with open(REGISTRY) as fh:
            return json.load(fh)
    except Exception as e:
        return {"groups": [], "services": [], "error": str(e)}


def glyph_map():
    """Read glyphs.json fresh, like the registry — editing a glyph is a reload,
    not a restart. Keys starting with '_' are comments, not icons."""
    try:
        with open(GLYPHS) as fh:
            return {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    except Exception as exc:
        sys.stderr.write(f"fleetdeck: glyphs.json unreadable ({exc})\n")
        sys.stderr.flush()
        return {"server": FALLBACK_GLYPH}


def scan():
    live, served, reg = listeners(), serve_map(), load_registry()
    known = set()
    services = []

    for s in reg.get("services", []):
        port = s.get("port")
        known.add(port)
        hit = live.get(port)
        # A skinned service answers on two ports: its own, and the skin_server
        # front that dresses it. The tile should open the dressed one — but only
        # if it is actually up, so a dead front degrades to the bare app rather
        # than to a dead link. Both ports count as known, or the front would
        # show up under "unregistered".
        face = port
        skin_port = (s.get("skin") or {}).get("port")
        if skin_port:
            known.add(skin_port)
            if live.get(skin_port):
                face = skin_port
        url = None
        if hit:
            if face in served:
                url = served[face]
            elif live[face]["cls"] in ("open", "tailnet"):
                url = f"http://{HOST}:{face}"
            if url and s.get("path"):
                url += s["path"]
        # An "api" service has no browser UI: tapping its root lands on a 404 or
        # `{"error":"Unexpected endpoint or method. (GET /)"}`. Badging that was
        # not enough — the tile stayed tappable and led straight to the error.
        # API tiles are therefore never links. The blurb says where to USE them.
        linkable = bool(url) and s.get("kind") != "api"
        services.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "blurb": s.get("blurb", ""),
            "icon": s.get("icon", "server"),
            "group": s.get("group", "core"),
            # "api" == answers HTTP but has no browser UI; tapping it lands on a
            # 404 or raw JSON. Badged, not hidden — sometimes you want /docs.
            "kind": s.get("kind", "app"),
            "port": port,
            "up": bool(hit),
            "proc": hit["cmd"] if hit else None,
            # up + no url == listening but loopback-bound and unproxied
            "reach": ("link" if url else ("host" if hit else "down")),
            "url": url,
            "linkable": linkable,
            # Registry-declared: this surface serves a real apple-touch-icon.
            # The install sheet lists only these, so it never promises a tile
            # that would come out as a screenshot of the page instead.
            "install": bool(s.get("install")),
        })

    # Anything listening that the registry does not know about. Kept separate and
    # unstyled — discovery, not curation. High ephemeral ports are noise.
    extra = []
    for port, hit in sorted(live.items()):
        if (port in known or port in INTERNAL or port == PORT or port >= 49000
                or port < 1024 or NOISE.search(hit["cmd"])):
            continue
        url = served.get(port) or (
            f"http://{HOST}:{port}" if hit["cls"] in ("open", "tailnet") else None
        )
        extra.append({
            "port": port, "proc": hit["cmd"], "url": url,
            "reach": "link" if url else "host",
        })

    agents = launch_agents()
    return {
        "host": HOST,
        "brand": BRAND,
        "machine": MACHINE,
        "groups": reg.get("groups", []),
        "services": services,
        "extra": extra,
        "agents": agents,
        "agentActions": AGENT_ACTIONS,
        "scanned": time.time(),
        "error": reg.get("error"),
    }


_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def cached_scan():
    with _lock:
        now = time.time()
        if _cache["data"] is None or now - _cache["at"] > SCAN_TTL:
            _cache["data"] = scan()
            _cache["at"] = now
        return _cache["data"]


# ── access ────────────────────────────────────────────────────────────────────

def allowed(addr):
    """True for tailnet peers and loopback. No password anywhere in the path."""
    if OPEN_TO_ALL:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):  # ::ffff:127.0.0.1 style clients
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS)


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<!-- The unprefixed form is what Chrome reads; the apple- one above is
     deprecated there but still the only one iOS honours. Both, therefore. -->
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="__MACHINE__">
<meta name="theme-color" content="#05070a">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-180.png">
<title>__MACHINE__ // portal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%2305070a'/><path d='M6 12h6l4 10 4-16 4 12h2' stroke='%234fe3c1' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>__VT__
  :root{
    --bg:#05070a; --panel:#0a0e13; --line:#18222b;
    --ink:#8fa3b0; --bright:#d6e4ec; --dim:#4a5b68;
    --on:#4fe3c1; --off:#2b3a45; --warn:#d9a441;
    /* idle == a scheduled job resting between runs. Deliberately its own
       colour: not the green of "serving", not the grey of "dead". */
    --idle:#3f6d7d; --bad:#e26a6a;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;background:var(--bg);color:var(--ink)}
  body{
    font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace;
    padding:0 16px calc(40px + env(safe-area-inset-bottom));
    background-image:
      linear-gradient(var(--line) 1px,transparent 1px),
      linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:64px 64px;
    background-position:-1px -1px;
    background-attachment:fixed;
  }
  body::before{ /* scanline veil — texture, not a light show */
    content:"";position:fixed;inset:0;pointer-events:none;z-index:2;
    background:repeating-linear-gradient(180deg,rgba(0,0,0,.22) 0 1px,transparent 1px 3px);
    opacity:.5;
  }
  header{
    position:sticky;top:0;z-index:3;margin:0 -16px 22px;padding:14px 16px;
    padding-top:calc(14px + env(safe-area-inset-top));
    background:rgba(5,7,10,.93);backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line);
    display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  }
  .brand{color:var(--on);letter-spacing:.16em;font-weight:600;text-transform:uppercase}
  .cursor{display:inline-block;width:7px;height:13px;background:var(--on);
    vertical-align:-2px;animation:blink 1.2s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .meta{color:var(--dim);margin-left:auto;font-size:11px;letter-spacing:.06em}
  .meta b{color:var(--on);font-weight:600}
  /* The one labelled control in a header of icon buttons, and labelled on
     purpose: the three beside it are modes of THIS board, where a glyph is
     enough because there is nowhere else to end up. This one leaves for a
     different service, and a 30px square would have to be learned before it
     could be used. It sits before `.meta` — which holds the right edge with
     `margin-left:auto` — so it reads immediately after the machine name
     instead of being filed away with the window controls. */
  #netmap{
    flex:none;margin-left:6px;padding:5px 10px;
    border:1px solid var(--line);border-radius:3px;
    background:transparent;color:var(--ink);text-decoration:none;
    font:inherit;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
    white-space:nowrap;display:inline-flex;align-items:center;gap:7px;
    align-self:center;
  }
  #netmap:hover,#netmap:focus-visible{border-color:var(--on);color:var(--on);outline:0}
  #netmap:active{background:rgba(79,227,193,.07)}
  /* Same chip, no lamp. The lamp on its neighbour means "something is moving
     at the other end of this link"; this one is a ledger, and a pulsing dot
     beside a cash figure would read as an alert. */
  #cashflow{
    flex:none;margin-left:6px;padding:5px 10px;
    border:1px solid var(--line);border-radius:3px;
    background:transparent;color:var(--ink);text-decoration:none;
    font:inherit;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
    white-space:nowrap;align-self:center;
  }
  #cashflow:hover,#cashflow:focus-visible{border-color:var(--on);color:var(--on);outline:0}
  #cashflow:active{background:rgba(79,227,193,.07)}
  /* Lit, slowly. The target is a live view and this is the header's only hint
     that something there is moving; it breathes rather than blinks so it does
     not compete with the caret two elements to its left. */
  #netmap i{width:5px;height:5px;border-radius:50%;background:var(--on);
    flex:none;animation:netpulse 2.6s ease-in-out infinite}
  @keyframes netpulse{0%,100%{opacity:.35}50%{opacity:1}}
  /* Narrow screens: the header already wraps, and the label is the first
     thing worth keeping whole when it does. */
  @media(max-width:560px){
    #netmap{margin-left:0;order:9;width:100%;justify-content:center;
      padding:8px 10px;margin-top:4px}
    /* Side by side on the wrapped row rather than a third full-width bar —
       "Cashflow" is one short word and does not need the whole width. */
    #cashflow{order:10;flex:1;margin-left:0;text-align:center;
      padding:8px 10px;margin-top:4px}
    #netmap{flex:2}
  }
  #fs{
    display:none;margin-left:10px;flex:none;
    width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
  }
  #fs.on{display:inline-flex;align-items:center;justify-content:center}
  #fs:active,#fs:hover{border-color:var(--on);color:var(--on)}
  /* Launched from the Home Screen: no browser chrome, so the status bar sits on
     top of the page. env(safe-area-inset-top) is already applied to the header;
     standalone just needs a little more breathing room and no fullscreen button
     (it is already fullscreen). */
  body.standalone header{padding-top:calc(20px + env(safe-area-inset-top))}
  body.standalone #fs{display:none !important}
  /* Add to Home Screen cannot be triggered from script on iOS — Safari only
     offers it from the Share menu, and beforeinstallprompt does not exist
     there. So this is a checklist, not a button that installs: it opens each
     surface in turn and remembers which ones are done, because working through
     a dozen of them from memory is how you end up with four. */
  #ins{
    margin-left:8px;flex:none;width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
    display:inline-flex;align-items:center;justify-content:center;
  }
  #ins:active,#ins:hover{border-color:var(--on);color:var(--on)}
  /* Which surface answers `/`. A link and not a script toggle, so it also
     navigates to the surface it selects — the state is never something you
     have to read off a button, because you are looking at it. */
  #simple{
    margin-left:8px;flex:none;width:30px;height:30px;padding:0;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--ink);cursor:pointer;line-height:0;
    display:inline-flex;align-items:center;justify-content:center;
  }
  #simple:active,#simple:hover{border-color:var(--on);color:var(--on)}
  /* Lit = the simple screen is this device's home. Tapping it then hands `/`
     back to the board, without leaving the board you are already on. */
  #simple.on{border-color:var(--on);color:var(--on);background:rgba(79,227,193,.09)}
  #sheet{
    position:fixed;inset:0;z-index:20;display:none;overflow:auto;
    background:rgba(5,7,10,.88);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
    padding:24px 16px calc(32px + env(safe-area-inset-bottom));
  }
  #sheet.on{display:block}
  #sheet .card{
    max-width:520px;margin:0 auto;background:var(--panel);
    border:1px solid var(--line);border-radius:4px;padding:16px 16px 8px;
  }
  #sheet h3{
    margin:0 0 4px;font-size:11px;font-weight:600;letter-spacing:.22em;
    text-transform:uppercase;color:var(--bright);
  }
  #sheet .lead{color:var(--dim);font-size:11px;line-height:1.7;margin:0 0 14px}
  #sheet .lead b{color:var(--ink);font-weight:600}
  .irow{
    display:flex;align-items:center;gap:11px;padding:9px 4px;
    border-top:1px solid var(--line);color:var(--ink);
  }
  /* the tick is a sibling of the link, not inside it — a button nested in an
     anchor is invalid, and every tap would have navigated */
  .irow .go{
    display:flex;align-items:center;gap:11px;flex:1;min-width:0;
    color:inherit;text-decoration:none;
  }
  .irow .ico{width:19px;height:19px;flex:none;color:var(--on)}
  .irow .nm{flex:1;min-width:0;font-size:12.5px;color:var(--bright)}
  .irow .nm span{display:block;color:var(--dim);font-size:10.5px;letter-spacing:.06em}
  .irow.off{opacity:.4}
  .irow.off .ico{color:var(--off)}
  .tick{
    flex:none;width:22px;height:22px;border:1px solid var(--line);border-radius:50%;
    background:transparent;color:var(--off);cursor:pointer;padding:0;
    display:inline-flex;align-items:center;justify-content:center;line-height:0;
  }
  .tick.done{border-color:var(--on);color:var(--on)}
  #sheet .foot{
    display:flex;justify-content:space-between;align-items:center;gap:10px;
    border-top:1px solid var(--line);margin-top:6px;padding:11px 4px 8px;
    color:var(--dim);font-size:10.5px;letter-spacing:.06em;
  }
  #sheet .foot button{
    background:none;border:0;color:var(--on);font:inherit;
    text-decoration:underline;cursor:pointer;padding:0;
  }
  #sheet .empty{color:var(--dim);font-size:11px;line-height:1.7;padding:10px 4px 16px}
  #hint{
    display:none;margin:0 0 18px;padding:10px 12px;
    border:1px solid var(--line);border-left:2px solid var(--on);border-radius:3px;
    color:var(--dim);font-size:11px;line-height:1.6;
  }
  #hint.on{display:block}
  #hint b{color:var(--ink);font-weight:600}
  #hint button{
    background:none;border:0;color:var(--on);font:inherit;
    text-decoration:underline;cursor:pointer;padding:0;margin-left:6px;
  }
  h2{
    display:flex;align-items:center;gap:12px;margin:26px 0 12px;
    font-size:11px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;
    color:var(--dim);
  }
  h2::after{content:"";flex:1;height:1px;background:var(--line)}
  .grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .tile{
    position:relative;display:flex;flex-direction:column;gap:9px;
    padding:14px;min-height:104px;
    background:var(--panel);border:1px solid var(--line);border-radius:3px;
    color:inherit;text-decoration:none;overflow:hidden;
    transition:border-color .14s,transform .14s,background .14s;
  }
  a.tile:active{transform:scale(.975);border-color:var(--on);background:#0d1319}
  @media(hover:hover){a.tile:hover{border-color:var(--on);background:#0d1319}}
  a.tile:hover .name,a.tile:active .name{color:var(--on)}
  .tile.down,.tile.host{opacity:.55}
  .tile.down .name,.tile.host .name{color:var(--ink)}
  .top{display:flex;align-items:flex-start;justify-content:space-between}
  .ico{width:24px;height:24px;color:var(--on);flex:none}
  .tile.down .ico,.tile.host .ico{color:var(--off)}
  .lamp{width:6px;height:6px;border-radius:50%;flex:none;margin-top:3px;
    background:var(--off);box-shadow:none}
  .tile.up .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(79,227,193,.14)}
  .tile.host .lamp{background:var(--warn);box-shadow:none}
  .name{color:var(--bright);font-weight:600;letter-spacing:.02em;line-height:1.25}
  .blurb{color:var(--dim);font-size:11px;line-height:1.35;
    display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .foot{display:flex;justify-content:space-between;align-items:center;
    margin-top:auto;padding-top:8px;font-size:10.5px;color:var(--dim);letter-spacing:.06em}
  .badge{color:var(--warn);border:1px solid rgba(217,164,65,.3);
    border-radius:2px;padding:0 4px;font-size:9.5px;letter-spacing:.08em}
  .badge.api{color:var(--dim);border-color:var(--line)}
  .badge.bad{color:var(--bad);border-color:rgba(226,106,106,.35)}
  /* ── agents ──────────────────────────────────────────────────────────────
     A scheduled job that is not running right now is HEALTHY, so its lamp is
     calm, not dark. Only a non-zero exit earns red, and only an unloaded plist
     earns amber. Anything louder and the board stops being readable. */
  .tile.agent{min-height:92px}
  .tile.agent .ico{color:var(--dim)}
  .tile.agent .when{margin-top:2px;font-size:10px;letter-spacing:.06em;color:var(--dim)}
  /* An absent timestamp is stated, not hidden — but quietly, because on a
     board where most jobs are silent it is the common case, not a fault. */
  .tile.agent .when.none{opacity:.45}
  .tile.agent.run .lamp{background:var(--on);box-shadow:0 0 0 3px rgba(79,227,193,.14)}
  .tile.agent.run .ico{color:var(--on)}
  .tile.agent.ok .lamp{background:var(--idle)}
  .tile.agent.off{opacity:.55}
  .tile.agent.off .lamp{background:var(--warn)}
  .tile.agent.fail{border-color:rgba(226,106,106,.4)}
  .tile.agent.fail .lamp{background:var(--bad);box-shadow:0 0 0 3px rgba(226,106,106,.14)}
  .tile.agent.fail .ico,.tile.agent.fail .name{color:var(--bad)}
  .sched{color:var(--ink);font-size:10.5px;letter-spacing:.04em}
  .acts{display:flex;gap:6px;margin-top:8px}
  .acts button{flex:1;background:transparent;border:1px solid var(--line);
    border-radius:2px;color:var(--dim);font:10px/1.8 inherit;letter-spacing:.1em;
    text-transform:uppercase;cursor:pointer;padding:0}
  .acts button:hover,.acts button:active{border-color:var(--on);color:var(--on)}
  .rows{display:flex;flex-wrap:wrap;gap:7px}
  .row{display:inline-flex;align-items:center;gap:7px;padding:6px 10px;
    background:var(--panel);border:1px solid var(--line);border-radius:3px;
    color:var(--dim);text-decoration:none;font-size:11px}
  a.row:active,a.row:hover{border-color:var(--on);color:var(--bright)}
  .row .lamp{margin:0}
  .err{border:1px solid var(--warn);color:var(--warn);padding:10px;border-radius:3px}
  footer{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);
    color:var(--dim);font-size:10.5px;letter-spacing:.06em;line-height:1.7}
</style></head><body>
<header>
  <span class="brand" id="brand">__BRAND__</span>
  <span style="color:var(--dim)">// __MACHINE__</span><span class="cursor"></span>
  <!-- New tab, deliberately. This is a live map you watch while doing
       something else, and same-tab would throw away the board you were
       reading it against — on the desk that is the whole cost of the trip.
       It changes nothing on an installed Android app, where a cross-origin
       target opens in a Custom Tab either way (see NETMAP_URL), so new tab is
       strictly better in one place and neutral in the other.
       `rel=noopener` because the target gets no business with this window. -->
  <a id="netmap" href="__NETMAP__" target="_blank" rel="noopener"
     title="Live Terminal Network — the live fleet map"><i></i>Live Terminal Network</a>
  <!-- Same tab, unlike its neighbour, and the difference is not cosmetic:
       this one is served from this origin, so it behaves like /notes — it
       stays inside the installed app and Back returns. Opening an internal
       surface in a new tab would leave a stack of Fleetdeck tabs behind. -->
  <a id="cashflow" href="/cashflow"
     title="Cashflow — the accountant's cash view">__CASHFLOW_LABEL__</a>
  <span class="meta"><b id="n">—</b> online<span id="clock"></span></span>
  <button id="fs" title="Fullscreen" aria-label="Toggle fullscreen">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path id="fsi" d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>
    </svg>
  </button>
  <button id="ins" title="Add to Home Screen" aria-label="Add apps to Home Screen">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M12 8v7M8.5 11.5h7"/>
    </svg>
  </button>
  <!-- Four big keys, not a phone. The button beside this one is already a phone
       outline, and two phone glyphs a hair apart is a header you have to read
       twice; this says what the simple screen IS — fewer keys, larger. Drawn at
       17px against its neighbours' 14 because it is four separate shapes inset
       from the viewBox edge: same ink, more room to keep them apart. -->
  <a id="simple" class="__SIMPLE_CLASS__" href="__SIMPLE_HREF__"
     title="__SIMPLE_TITLE__" aria-label="__SIMPLE_TITLE__">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
      <rect x="2.5" y="2.5" width="8.5" height="8.5" rx="1.5"/>
      <rect x="13" y="2.5" width="8.5" height="8.5" rx="1.5"/>
      <rect x="2.5" y="13" width="8.5" height="8.5" rx="1.5"/>
      <rect x="13" y="13" width="8.5" height="8.5" rx="1.5"/>
    </svg>
  </a>
</header>
<div id="hint"></div>
<main id="app"></main>
<div id="sheet"><div class="card">
  <h3>add to home screen</h3>
  <p class="lead">iOS only offers this from the Share menu, so nothing here can
    install for you. Open one, tap <b>Share &rarr; Add to Home Screen</b>, come
    back and tick it off.</p>
  <div id="ilist"></div>
  <div class="foot"><span id="idone">—</span>
    <span><button id="ireset">reset</button> · <button id="iclose">close</button></span>
  </div>
</div></div>
<footer>
  every link resolved live from <span style="color:var(--ink)">lsof</span> binds +
  <span style="color:var(--ink)">tailscale serve</span>; every agent from
  <span style="color:var(--ink)">launchctl</span> + its plist — nothing hardcoded.<br>
  <span class="badge">host only</span> = listening on loopback, not reachable from this device.
  add a mapping with <span style="color:var(--ink)">tailscale serve</span> to make it tappable.
  <span class="badge api">api</span> = answers, but has no browser UI — shown, not linked,
  so a tap can never land on an error page.<br>
  <span class="badge">unloaded</span> = plist installed but never bootstrapped — it is doing nothing.
  <span class="badge bad">exit N</span> = last run failed.<br>
  registry: <span id="regpath">services.json</span> · scan <span id="ago">—</span>
</footer>
<script>
const I=__GLYPHS__;

const svg=k=>`<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${I[k]||I.server}</svg>`;
// registry text is operator-authored, but `proc` comes off lsof — escape both
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let last=0, DATA=null;
function render(d){
  last=d.scanned*1000; DATA=d;
  const up=d.services.filter(s=>s.up).length;
  document.getElementById('n').textContent=up+'/'+d.services.length;
  let h=d.error?`<div class="err">registry: ${d.error}</div>`:'';
  for(const g of d.groups){
    const list=d.services.filter(s=>s.group===g.id);
    if(!list.length) continue;
    // reachable first, then still-listening, then dark — taps before diagnostics
    const w={link:0,host:1,down:2};
    list.sort((a,b)=>w[a.reach]-w[b.reach]||a.name.localeCompare(b.name));
    h+=`<h2>${g.label}</h2><div class="grid">`;
    for(const s of list){
      const cls=s.reach==='link'?'up':(s.reach==='host'?'host up':'down');
      const tag=s.reach==='host'?'<span class="badge">host only</span>':
                (!s.up?'<span style="letter-spacing:.08em">offline</span>':
                (s.kind==='api'?'<span class="badge api">api</span>':''));
      const body=`<div class="top">${svg(s.icon)}<span class="lamp"></span></div>
        <div><div class="name">${esc(s.name)}</div><div class="blurb">${esc(s.blurb)}</div></div>
        <div class="foot"><span>:${s.port}</span>${tag}</div>`;
      h+=(s.url&&s.linkable)?`<a class="tile ${cls}" href="${esc(s.url)}">${body}</a>`
              :`<div class="tile ${cls}">${body}</div>`;
    }
    h+='</div>';
  }
  // ── agents ────────────────────────────────────────────────────────────────
  // Sorted server-side by health, so failures are the first thing on screen and
  // the count in the heading answers "is anything wrong?" without scrolling.
  if(d.agents&&d.agents.length){
    const bad=d.agents.filter(a=>a.health==='fail').length;
    const off=d.agents.filter(a=>a.health==='off').length;
    let note='';
    if(bad) note+=` <span class="badge bad">${bad} failing</span>`;
    if(off) note+=` <span class="badge">${off} unloaded</span>`;
    h+=`<h2>agents · ${d.agents.length}${note}</h2><div class="grid">`;
    for(const a of d.agents){
      const s=a.schedule||'';
      const ico = s==='always on' ? 'bolt'
                : s==='manual'    ? 'hand'
                : s==='on file change' ? 'eye'
                : s.startsWith('every') ? 'loop' : 'clock';
      // A signal exit (negative) is a normal reload, not a fault — server-side
      // health already folded that in; only surface a code we called a failure.
      const tag = a.health==='fail' ? `<span class="badge bad">exit ${a.exit}</span>`
                : a.health==='off'  ? '<span class="badge">unloaded</span>'
                : a.health==='run'  ? `<span style="letter-spacing:.08em">pid ${a.pid}</span>`
                : '';
      // launchd has no last-run time, so this is when the job last WROTE to
      // its log — a different claim, and labelled as one. Absent is rendered,
      // never filled in: "no log" means the plist declares no output path,
      // "no output" means it declares one and nothing has been written to it.
      const when = a.last_output ? `out ${rel(a.last_output)}`
                 : a.logged      ? 'no output'
                 : 'no log';
      const acts = d.agentActions ? `<div class="acts">
          <button data-act="start" data-label="${esc(a.label)}">run</button>
          <button data-act="stop"  data-label="${esc(a.label)}">stop</button>
        </div>` : '';
      h+=`<div class="tile agent ${a.health}">
        <div class="top">${svg(ico)}<span class="lamp"></span></div>
        <div><div class="name">${esc(a.name)}</div>
             <div class="blurb">${esc(a.program)}</div></div>
        <div class="foot"><span class="sched">${esc(s)}</span>${tag}</div>
        <div class="foot when${a.last_output?'':' none'}">${esc(when)}</div>
        ${acts}</div>`;
    }
    h+='</div>';
  }
  if(d.extra.length){
    h+=`<h2>unregistered</h2><div class="rows">`;
    for(const e of d.extra){
      const b=`<span class="lamp" style="background:var(--on)"></span>${esc(e.proc)} :${e.port}`;
      h+=e.url?`<a class="row" href="${esc(e.url)}">${b}</a>`:`<span class="row">${b}</span>`;
    }
    h+='</div>';
  }
  document.getElementById('app').innerHTML=h;
}
// Delegated, because render() replaces the whole subtree on every poll and
// per-tile listeners would be rebound (and leak) ten times a minute.
document.getElementById('app').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  b.disabled=true; b.textContent='…';
  try{
    await fetch('api/agent',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:b.dataset.act,label:b.dataset.label})});
  }catch(_){}
  poll();
});
// Unix seconds -> a coarse age. Coarse on purpose: this is a proxy for when a
// job last ran, and reporting it to the second would dress a guess up as a
// measurement.
function rel(ts){
  const s=Math.max(0,Math.round(Date.now()/1000-ts));
  if(s<90) return 'just now';
  if(s<5400) return Math.round(s/60)+'m ago';
  if(s<172800) return Math.round(s/3600)+'h ago';
  return Math.round(s/86400)+'d ago';
}

function tick(){
  const t=new Date();
  document.getElementById('clock').textContent=' · '+
    String(t.getHours()).padStart(2,'0')+':'+String(t.getMinutes()).padStart(2,'0');
  const s=last?Math.round((Date.now()-last)/1000):0;
  document.getElementById('ago').textContent=last?(s<60?s+'s ago':Math.round(s/60)+'m ago'):'—';
}
async function poll(){
  try{ render(await (await fetch('api/status',{cache:'no-store'})).json()); }catch(e){}
}
// ── add to home screen ───────────────────────────────────────────────────────
// A checklist, not an installer. `beforeinstallprompt` does not exist on iOS
// and this board is built for a phone, so the honest thing is to hand over the
// list and remember your place in it. Which surfaces appear is registry-declared
// ("install": true) — the sheet never offers a tile that would still come out as
// a screenshot of its own page, the same rule the board follows for links it
// has not resolved.
const sheet = document.getElementById('sheet');
const ticked = () => { try { return JSON.parse(localStorage.getItem('fd-installed') || '[]'); }
                       catch (e) { return []; } };
const setTicked = a => localStorage.setItem('fd-installed', JSON.stringify(a));

function drawSheet(){
  if(!DATA) return;
  const done=ticked(), rows=(DATA.services||[]).filter(s=>s.install);
  const list=document.getElementById('ilist');
  if(!rows.length){
    list.innerHTML='<div class="empty">No service is marked installable yet. '
      +'Add <b>"install": true</b> to an entry in services.json once it serves '
      +'an apple-touch-icon — see the README.</div>';
    document.getElementById('idone').textContent='0 of 0';
    return;
  }
  let h='';
  for(const s of rows){
    // reachability comes off the same live scan the tiles use — a surface that
    // is down or loopback-bound is shown greyed with the reason, not offered
    const open=s.url&&s.linkable;
    const why=s.reach==='host'?'host only':(s.reach==='down'?'offline':':'+s.port);
    const inner=`${svg(s.icon)}<span class="nm">${esc(s.name)}<span>${why}</span></span>`;
    h+=`<div class="irow${open?'':' off'}">`
      +(open?`<a class="go" href="${esc(s.url)}" target="_blank" rel="noopener">${inner}</a>`
            :`<span class="go">${inner}</span>`)
      +`<button class="tick${done.includes(s.id)?' done':''}" data-id="${esc(s.id)}"`
      +` aria-label="Mark ${esc(s.name)} added">`
      +`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"`
      +` stroke-width="3" stroke-linecap="round" stroke-linejoin="round">`
      +`<path d="m5 13 5 5L20 7"/></svg></button></div>`;
  }
  list.innerHTML=h;
  const n=done.filter(id=>rows.some(s=>s.id===id)).length;
  document.getElementById('idone').textContent=n+' of '+rows.length+' added';
}

document.getElementById('ilist').addEventListener('click',e=>{
  const b=e.target.closest('.tick'); if(!b) return;
  const a=ticked(), i=a.indexOf(b.dataset.id);
  if(i<0) a.push(b.dataset.id); else a.splice(i,1);
  setTicked(a); drawSheet();
});
const closeSheet=()=>sheet.classList.remove('on');
document.getElementById('ins').addEventListener('click',()=>{drawSheet();sheet.classList.add('on')});
document.getElementById('iclose').addEventListener('click',closeSheet);
document.getElementById('ireset').addEventListener('click',()=>{setTicked([]);drawSheet()});
sheet.addEventListener('click',e=>{if(e.target===sheet)closeSheet()});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeSheet()});

// ── fullscreen ───────────────────────────────────────────────────────────────
// Three different platforms, three different answers, so feature-detect rather
// than assume:
//   desktop / Android  — the Fullscreen API works; show the toggle.
//   iOS Safari in a tab — requestFullscreen does not exist for elements. No
//                         button is shown, because a button that silently does
//                         nothing is worse than none. Point at Add to Home
//                         Screen instead, which is the real fullscreen on iOS.
//   launched from Home Screen — already fullscreen; hide both.
const standalone = window.matchMedia('(display-mode: standalone)').matches
                || window.navigator.standalone === true;
const el = document.documentElement;
const canFS = !!(el.requestFullscreen || el.webkitRequestFullscreen);
const fsBtn = document.getElementById('fs');
const hint  = document.getElementById('hint');

if (standalone) document.body.classList.add('standalone');

if (canFS && !standalone) {
  fsBtn.classList.add('on');
  const paint = () => {
    const on = !!(document.fullscreenElement || document.webkitFullscreenElement);
    // arrows point out to enter, in to exit
    document.getElementById('fsi').setAttribute('d', on
      ? 'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5'
      : 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5');
    fsBtn.title = on ? 'Exit fullscreen' : 'Fullscreen';
  };
  fsBtn.addEventListener('click', () => {
    const on = document.fullscreenElement || document.webkitFullscreenElement;
    if (on) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    else    (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
  });
  document.addEventListener('fullscreenchange', paint);
  document.addEventListener('webkitfullscreenchange', paint);
  paint();
} else if (!standalone && /iPhone|iPad|iPod/.test(navigator.userAgent)) {
  // Dismissible, and the dismissal sticks — a permanent banner on a launcher
  // you open twenty times a day is its own kind of broken.
  if (localStorage.getItem('wb-hint') !== 'off') {
    hint.innerHTML = 'Fullscreen on iOS is <b>Add to Home Screen</b> — ' +
      'Share, then Add to Home Screen. It opens with no browser chrome.' +
      '<button id="hx">dismiss</button>';
    hint.classList.add('on');
    document.getElementById('hx').addEventListener('click', () => {
      localStorage.setItem('wb-hint', 'off');
      hint.classList.remove('on');
    });
  }
}

render(window.__DATA__); tick();
setInterval(tick,1000);
setInterval(poll,10000);
// a launcher is usually reopened, not left open — rescan the moment it returns
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
</script></body></html>"""


# ── /call ────────────────────────────────────────────────────────────────────
# A spoken briefing, assembled from live state and read out in the operator's
# own cloned voice by the local TTS router on :8890.
#
# Why this and not a phone call: there is no telephony on this machine. No
# Twilio, no SignalWire, no number. A real PSTN call needs a cloud provider and
# a monthly line. What IS here is a full local voice stack — Chatterbox TTS with
# a voice reference at ~/wb-voice/refs/zayed_ref.wav, and whisper.cpp for the
# return leg — so a voice session over the tailnet costs nothing and leaves the
# machine. That is what this is. It is a callback, not a ring.
#
# The briefing is DETERMINISTIC. No model writes it: it is counts and names
# read out of the same scan the board renders and the same graph the lenses
# query. A model in this path would be a model that can invent an outage.
#
# :8890 is not on the tailnet, so the audio is proxied through here rather than
# linked. One origin, and the phone never needs a second port opened.
VOICE_URL = os.environ.get("WB_VOICE_URL", "http://127.0.0.1:8890/v1/audio/speech")
GRAPH_URL = os.environ.get("WB_GRAPH_URL", "http://127.0.0.1:4180")
SPEAK_MAX = 1200          # characters; a runaway briefing is a runaway TTS job


def _get_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        # An unreachable graph is a fact about the briefing, not a crash. The
        # caller says so out loud rather than reporting a healthy system it
        # could not actually see.
        return None


ROUTING = os.path.expanduser("~/.imsg-routing.json")


def fleet_state():
    """Every live tmux session as an agent, with what it is for and what it last said.

    The session LIST always comes from tmux, never from a file — the same rule
    imsg-router follows, and for the same reason: a hardcoded roster goes stale
    the first time a session is opened or killed, and then the fleet report is
    confidently wrong. Descriptions come from ~/.imsg-routing.json, which is
    also what the iMessage router reads, so Trace and the router cannot disagree
    about what a session is for.

    A session with no description is REPORTED as undescribed rather than hidden.
    An agent nobody has written a purpose for is the thing worth knowing about.
    """
    try:
        desc = json.load(open(ROUTING)).get("sessions") or {}
    except Exception:
        desc = {}
    raw = sh("tmux list-sessions -F '#{session_name}|#{session_attached}|#{session_activity}' 2>/dev/null")
    now, out = time.time(), []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, attached, activity = parts[0], parts[1] != "0", parts[2]
        try:
            idle = int(now - int(activity))
        except ValueError:
            idle = None
        # The last non-empty line of the pane. This is "reading the tunnel":
        # what the agent in there is actually showing right now. Truncated hard
        # — a pane can hold anything, including a stack trace.
        tail = ""
        for ln in reversed(sh(f"tmux capture-pane -p -t {name} -S -6 2>/dev/null").splitlines()):
            if ln.strip():
                tail = ln.strip()[:120]
                break
        out.append({
            "name": name,
            "attached": attached,
            "idle_secs": idle,
            "purpose": desc.get(name),
            "last_line": tail,
        })
    out.sort(key=lambda s: (s["purpose"] is None, s["name"].lower()))
    return out


# Patterns redacted before any pane content leaves this machine. A tmux pane is
# a scrollback of whatever the operator did in it — a cat of a .env, a curl with
# a bearer token, a printed key. That content is about to be posted to a cloud
# LLM, so it is scrubbed here, at the only point where the whole line is still
# visible. This is the same instinct the knowledge graph follows in refusing to
# store file bodies at all.
SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), "<redacted-key>"),
    (re.compile(r"(?i)\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}"), "<redacted-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"(?i)\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "<redacted-jwt>"),
    (re.compile(r"(?i)(authorization|bearer)\s*[:=]?\s*\S{12,}"), r"\1 <redacted>"),
    # KEY=value / KEY: value where the key name smells like a credential.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"),
     r"\1=<redacted>"),
    (re.compile(r"(?i)\bpostgres(?:ql)?://[^\s]*:[^\s@]*@"), "postgres://<redacted>@"),
]


def redact(text):
    for pat, sub in SECRET_PATTERNS:
        text = pat.sub(sub, text)
    return text


CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


def transcript_for(cwd, turns=8):
    """The actual conversation in a tunnel, read from Claude Code's own log.

    THIS IS WHY capture-pane WAS NOT ENOUGH. Every agent pane runs in the
    alternate screen buffer (`alternate_on = 1`), so tmux holds no scrollback
    for it — the TUI owns the screen and repaints a viewport, which for the
    `pool` session is ten lines tall. Capturing it yields the input box and the
    status line, never the conversation. tmux is the wrong place to look.

    Claude Code writes each session to ~/.claude/projects/<encoded-cwd>/*.jsonl,
    one JSON record per line, and THAT is the conversation. The directory name
    is the working directory with every slash replaced by a dash.

    Returns the last `turns` exchanges, text only. Tool calls and file snapshots
    are skipped: they are most of the file by volume and none of it is what a
    human means by "what is it talking about".
    """
    if not cwd:
        return None
    encoded = cwd.replace("/", "-")
    d = os.path.join(CLAUDE_PROJECTS, encoded)
    if not os.path.isdir(d):
        return None
    logs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    if not logs:
        return None
    newest = max(logs, key=os.path.getmtime)

    out = []
    try:
        with open(newest, "r", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") not in ("user", "assistant"):
                    continue
                c = (r.get("message") or {}).get("content")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and b.get("type") == "text")
                c = " ".join(str(c or "").split())
                if not c:
                    continue
                out.append({"role": r["type"], "text": redact(c)[:600]})
    except Exception:
        return None

    return {
        "source": os.path.basename(newest),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(os.path.getmtime(newest))),
        "total_turns": len(out),
        "turns": out[-int(turns):],
    }


def tunnel_context(name, lines=60):
    """Everything readable about one tunnel, structured.

    The earlier version returned ONE line of 120 characters, which is not
    context — it was enough to say a pane existed and nothing about what was
    happening in it. This returns the working directory, the running command,
    the git branch, and the tail of the scrollback, which together are what a
    human means by "what is that terminal doing".

    Scrollback is redacted on the way out (see SECRET_PATTERNS): the caller is
    a cloud model, and a pane is an unfiltered record of whatever was typed.
    """
    live = {f["name"] for f in fleet_state()}
    if name not in live:
        return {"found": False, "live": sorted(live)}

    def disp(fmt):
        return sh(f"tmux display-message -p -t {name} '{fmt}' 2>/dev/null").strip()

    cwd = disp("#{pane_current_path}")
    cmd = disp("#{pane_current_command}")
    panes = disp("#{window_panes}")
    branch = ""
    if cwd:
        branch = sh(f"git -C {cwd} rev-parse --abbrev-ref HEAD 2>/dev/null").strip()

    raw = sh(f"tmux capture-pane -p -t {name} -S -{int(lines)} 2>/dev/null")
    kept = [ln.rstrip() for ln in raw.splitlines()]
    # Box-drawing rules are most of a Claude Code pane by line count and carry
    # no information; dropping them roughly doubles the useful context that
    # fits in one tool response.
    kept = [ln for ln in kept if ln.strip() and not re.fullmatch(r"[\s\u2500-\u257f\-_=]+", ln)]

    try:
        desc = json.load(open(ROUTING)).get("sessions") or {}
    except Exception:
        desc = {}

    hit = next((f for f in fleet_state() if f["name"] == name), {})
    return {
        "found": True,
        "name": name,
        "purpose": desc.get(name),
        "idle_secs": hit.get("idle_secs"),
        "attached": hit.get("attached"),
        "cwd": cwd or None,
        "running": cmd or None,
        "git_branch": branch or None,
        "panes": int(panes) if panes.isdigit() else None,
        # An agent prompt vs a bare shell is the difference between a session
        # that can be given work and one that cannot.
        "has_agent": bool(cmd) and cmd.lower() not in {"zsh", "bash", "sh", "fish"},
        "scrollback": redact("\n".join(kept[-int(lines):])),
        "scrollback_lines": len(kept),
        # The pane is a viewport; this is the conversation. When both exist,
        # this is the one worth reading.
        "conversation": transcript_for(cwd),
        "pane_is_tui": disp("#{alternate_on}") == "1",
        "redacted": True,
    }


def agent_report(name):
    """Trace's read of ONE agent. Spoken, so it stays to a few sentences."""
    fleet = fleet_state()
    hit = next((f for f in fleet if f["name"] == name), None)
    if not hit:
        live = ", ".join(f["name"] for f in fleet[:8])
        return f"There is no session called {name}. Live right now: {live}."
    bits = [f"{hit['name']}."]
    bits.append(hit["purpose"] or "No purpose is written down for this one, which is worth fixing.")
    if hit["idle_secs"] is not None:
        mins = hit["idle_secs"] // 60
        bits.append("Active in the last minute." if mins < 1 else
                    f"Last activity {mins} minutes ago." if mins < 90 else
                    f"Quiet for {mins // 60} hours.")
    if hit["last_line"]:
        bits.append(f"Its pane currently ends with: {hit['last_line']}")
    return " ".join(bits)


OLLAMA = os.environ.get("WB_OLLAMA", "http://127.0.0.1:11434/api/generate")
ROUTER_MODEL = os.environ.get("WB_ROUTER_MODEL", "qwen3.8:27b-mlx")
OPERATOR_PHONE = os.environ.get("WB_OPERATOR_PHONE", "+16465490064")
# The Homebrew binary, NOT the ~/bin wrapper. TCC grants Automation per exact
# binary path, and the grant lives on /opt/homebrew/Cellar/imsg/.../imsg. The
# wrapper runs under zsh, which a launchd-spawned server does not inherit a
# grant for — so going through it fails with "authorization denied (code: 23)".
IMSG = "/opt/homebrew/bin/imsg"

# Trace's outbox. READ-ONLY from this process, always. The launchd worker
# com.wideband.trace-outbox is the only thing that puts a message on the wire;
# the portal reports what it has ALREADY sent by reading the `sent` directory.
# Nothing here may ever call send_text() — that is a second outbound iMessage
# path and the whole boundary is that there is exactly one.
TRACE_SESSION = os.environ.get("WB_TRACE_SESSION", "trace")

# Grace — Trace's local model assistant. Same qwen the router already uses to
# classify inbound messages; this gives that existing classifier a voice.
#
# `think` is False and that is load-bearing, not a preference: with thinking on,
# this model spends the whole token budget reasoning and returns an EMPTY
# content field. Measured 584ms with it off against 3.5s and no answer with it
# on. If Grace ever goes silent, check this flag first.
#
# Grace has READ tools and exactly one escalation. She has no outbox, no imsg,
# no way to message anyone. Trace remains the only thing that speaks outward,
# so a prompt-injected pane or page can at worst make her say something odd.
GRACE_MODEL = os.environ.get("WB_GRACE_MODEL", "qwen3.8:27b-mlx")
OLLAMA_CHAT = os.environ.get("WB_OLLAMA_CHAT", "http://127.0.0.1:11434/api/chat")

# DEPRECATED 2026-09-16. The operator reaches Trace by iMessage voice note now,
# so the spoken half of /call-trace — the mic loop, Grace, and the hand-off she
# performs — is off by default. The TEXT half of that page is untouched: it goes
# through router().deliver(), which is the same iMessage pipeline, and is the
# thing the retirement is in favour of rather than against.
#
# DEFAULT OFF, exactly like the cockpit's NEXT_PUBLIC_TRACE_CALL. An absent
# variable means retired, so a fresh checkout does not quietly bring the mic
# back. WB_GRACE=1 restores it for a session.
#
# Phase 2 deletes GRACE_SYSTEM, grace_chat, grace_json, grace_turn, /api/grace
# and the hands-free client JS outright. The list is in the cockpit's
# docs/DEPRECATION-call-trace-and-grace.md, which covers both repos.
GRACE_ENABLED = os.environ.get("WB_GRACE") == "1"
GRACE_SYSTEM = """You are Grace, the local assistant to Trace, on Zayed's Mac.
You speak out loud, so: short sentences, no markdown, no lists, no headings.
One or two sentences is normal. Be warm and direct.

You can see his tmux fleet. You answer quickly yourself when you can.
Trace is the senior agent: he runs in a tmux pane, owns the outbox, and is the
only one who can text Zayed or change anything. When something needs real work,
judgement, or action, hand it to Trace.

Reply with ONLY a JSON object, no prose around it:
  {"say": "<what you say out loud>", "action": null}
  {"say": "<e.g. let me look>", "action": "read", "session": "<name>"}
  {"say": "<e.g. asking trace now>", "action": "ask_trace", "text": "<the request, in full>"}

Use "read" to look at a session's scrollback before answering about it.
Use "ask_trace" for anything that changes something, needs his tools, or that
you cannot answer from what you can see. Never claim you did something yourself
that you handed to Trace."""


def grace_chat(messages, predict=200):
    """One turn against the local model. Returns text, or None."""
    payload = json.dumps({
        "model": GRACE_MODEL, "stream": False, "think": False,
        "messages": messages,
        "options": {"num_predict": predict, "temperature": 0.4},
    }).encode()
    req = urllib.request.Request(OLLAMA_CHAT, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (json.load(r).get("message", {}).get("content") or "").strip()
    except Exception:
        return None


def grace_json(text):
    """Pull the JSON object out of a model reply that may be wrapped in prose."""
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(text[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def grace_turn(user_text, history):
    """Grace answers, optionally reading a pane or escalating to Trace.

    Bounded at ONE tool hop on purpose. This runs inside a spoken conversation,
    so an open-ended agent loop would mean unbounded silence; if she cannot
    settle it in one look, handing to Trace is the better answer anyway.
    """
    live = [f["name"] for f in fleet_state()]
    convo = [{"role": "system",
              "content": GRACE_SYSTEM + "\n\nLive sessions: " + ", ".join(live)}]
    for h in history[-6:]:
        role = "assistant" if h.get("role") == "grace" else "user"
        convo.append({"role": role, "content": str(h.get("text", ""))[:500]})
    convo.append({"role": "user", "content": user_text[:1200]})

    d = grace_json(grace_chat(convo)) or {}
    say = str(d.get("say") or "").strip()
    action = d.get("action")

    if action == "read":
        target = str(d.get("session") or "").strip()
        if target in live:
            tail = pane_tail(target, 120)
            convo.append({"role": "assistant", "content": json.dumps(d)})
            convo.append({"role": "user",
                          "content": f"Scrollback from {target}:\n{tail}\n\n"
                                     "Now answer out loud in one or two sentences. "
                                     'Reply as {"say": "...", "action": null}.'})
            d2 = grace_json(grace_chat(convo)) or {}
            say = str(d2.get("say") or say).strip()
        else:
            say = say or f"I don't see a session called {target}."
        return {"say": say, "handed_off": False}

    if action == "ask_trace":
        ask = str(d.get("text") or user_text).strip()
        out = dispatch(TRACE_SESSION, ask[:1500])
        if out.get("ok"):
            return {"say": say or "Asking Trace now.", "handed_off": True}
        return {"say": "I couldn't reach Trace just then.", "handed_off": False}

    return {"say": say or "I didn't catch that.", "handed_off": False}


def pane_tail(name, lines=120):
    """Read-only scrollback. Grace never types into a pane; dispatch() does."""
    try:
        p = subprocess.run(["tmux", "capture-pane", "-p", "-S", f"-{int(lines)}",
                            "-t", name], capture_output=True, text=True, timeout=12)
        return (p.stdout or "")[-6000:]
    except Exception:
        return ""
TRACE_OUT = os.path.expanduser("~/trace/outbox")
TRACE_SENT = os.path.join(TRACE_OUT, "sent")


ROUTER_PATH = os.path.expanduser("~/bin/imsg-router")
WHISPER_BIN = os.environ.get("WB_WHISPER", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = os.environ.get(
    "WB_WHISPER_MODEL", os.path.expanduser("~/eveng2/scripts/models/ggml-base.en.bin"))
_router = None


def router():
    """imsg-router, imported as a module rather than reimplemented.

    Its delivery is not trivial — it refuses a session that is not live, refuses
    a pane sitting at a bare shell (keys typed there run as shell commands
    instead of reaching an agent), types the payload, waits, then sends Enter,
    and appends the ticket footer that lets the agent close its own ticket. A
    second copy of that in this file would be a second dispatcher free to drift
    from the first, and the two would eventually disagree about what "delivered"
    means. One dispatcher, two front doors — iMessage and this.

    It has no import-time side effects: everything below its constants is a
    def, and execution is behind `if __name__ == "__main__"`.
    """
    global _router
    if _router is None:
        import importlib.util
        import importlib.machinery
        spec = importlib.util.spec_from_loader(
            "imsg_router",
            importlib.machinery.SourceFileLoader("imsg_router", ROUTER_PATH))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _router = mod
    return _router


def transcribe(audio_bytes, suffix=".webm"):
    """Browser audio -> text, via ffmpeg to 16k mono and whisper.cpp.

    Measured at half a second for a sentence on this box, which is what makes
    talking to it feel like talking rather than like filling in a form.
    """
    import tempfile
    if not os.path.exists(WHISPER_MODEL):
        return None, f"no whisper model at {WHISPER_MODEL}"
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "in" + suffix)
        wav = os.path.join(td, "in.wav")
        with open(raw, "wb") as fh:
            fh.write(audio_bytes)
        conv = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", raw,
             "-ar", "16000", "-ac", "1", wav],
            capture_output=True, text=True, timeout=60)
        if conv.returncode != 0 or not os.path.exists(wav):
            return None, "could not decode the recording"
        p = subprocess.run(
            [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav, "-nt", "-np"],
            capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            return None, "transcription failed"
        # -nt drops timestamps; whisper still emits bracketed non-speech markers
        # like [BLANK_AUDIO], which are not words the operator said.
        text = re.sub(r"\[[^\]]*\]", " ", p.stdout)
        text = " ".join(text.split())
        return (text or None), (None if text else "nothing audible")


def dispatch(target, instruction):
    """Actually send an instruction into a tunnel. The only mutating path here.

    Deliberately requires an EXPLICIT target from the caller. The router's own
    policy note says a model guess can be wrong — it once sent a message naming
    'media' to 'ops' — so an inferred route is proposed and the operator
    confirms. This function is what the confirmation calls, never the classifier.
    """
    r = router()
    ticket_id = None
    try:
        t = subprocess.run(
            [os.path.expanduser("~/bin/req"), "file", instruction[:120],
             "--target", target, "--origin", "agent"],
            capture_output=True, text=True, timeout=25)
        m = re.search(r"REQ-\d+", (t.stdout or "") + (t.stderr or ""))
        ticket_id = m.group(0) if m else None
    except Exception:
        # A missing ticket must not block delivery; it is recorded as absent so
        # the reply can say the instruction went out untracked.
        ticket_id = None
    try:
        ok, detail = r.deliver(target, instruction, [], ticket_id)
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "ticket": ticket_id}
    return {"ok": bool(ok), "detail": str(detail), "ticket": ticket_id}


def explicit_target(text):
    """(session, instruction) when the operator NAMED a session, else (None, text).

    Explicit beats inferred — the router's own policy, and the reason it fires
    a named target without proposing anything. The classifier is for when you
    did not say where it goes; running it when you did is the system second-
    guessing a decision you already made, which is what made the suggestions
    feel wrong so often.

    "media: redo the hero" and "media redo the hero" both address media.
    Longest name first, so 'pro' never shadows 'prod'. Reuses imsg-router's own
    separators so the two front doors parse an address the same way.
    """
    names = sorted((f["name"] for f in fleet_state()), key=len, reverse=True)
    stripped = (text or "").strip()
    low = stripped.lower()
    for n in names:
        nl = n.lower()
        if not low.startswith(nl):
            continue
        rest = stripped[len(n):]
        if not rest:
            continue
        if rest[0] in (":", "-", " ", ","):
            body = rest[1:].strip()
            if body:
                return n, body
    return None, stripped


def route_task(text):
    """Which tunnel should this go to? Trace proposes; he does not dispatch.

    Same model and same session-list-from-tmux rule as imsg-router, so a
    proposal here and a proposal over iMessage cannot disagree. PROPOSES ONLY:
    the router's own policy note says a model guess can be wrong — it once sent
    a message naming 'media' to 'ops' — so the guess is offered and the operator
    confirms. Nothing is typed into a pane from this surface.
    """
    fleet = fleet_state()
    if not fleet:
        return {"target": None, "why": "No tmux sessions are running, so there is nowhere to send it."}
    menu = "\n".join(
        f"- {f['name']}: {f['purpose'] or 'no description'}" for f in fleet)
    prompt = (
        "You route one instruction to exactly one tmux session. Reply with JSON only: "
        '{\"target\":\"<session name>\",\"why\":\"<one short sentence>\"}. '
        "The target MUST be one of these session names.\n\n"
        f"Sessions:\n{menu}\n\nInstruction: {text}\n"
    )
    body = json.dumps({"model": ROUTER_MODEL, "stream": False,
                       "format": "json", "prompt": prompt}).encode()
    try:
        req = urllib.request.Request(
            OLLAMA, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = json.loads(r.read().decode()).get("response", "{}")
        pick = json.loads(raw)
    except Exception as exc:
        return {"target": None,
                "why": f"The local router did not answer ({type(exc).__name__})."}
    names = {f["name"] for f in fleet}
    target = pick.get("target")
    if target not in names:
        # A model naming a session that does not exist is the failure mode the
        # live-list rule exists for. Refuse it rather than pass it on.
        return {"target": None,
                "why": f"It proposed '{target}', which is not a live session."}
    return {"target": target, "why": (pick.get("why") or "").strip()[:200]}


def send_text(body_text):
    """Text the operator. One recipient, fixed — this is a notify channel, not a sender."""
    if not os.path.exists(IMSG):
        return False, "imsg wrapper not found"
    try:
        p = subprocess.run(
            [IMSG, "send", "--to", OPERATOR_PHONE, "--service", "imessage",
             "--text", body_text[:900]],
            capture_output=True, text=True, timeout=30)
        out = (p.stdout or "") + (p.stderr or "")
        ok = p.returncode == 0 and "denied" not in out.lower()
        if not ok and "authorization denied" in out.lower():
            # Named precisely, because the remedy is a one-time TCC grant and
            # not a code change: System Settings > Privacy & Security >
            # Automation, allow the portal's python to control Messages.
            out = ("Messages automation is not granted to the portal process "
                   "(TCC). Sending from a launchd job needs that grant.")
        return ok, out.strip()[:220]
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def brief_text():
    """The spoken briefing. Plain sentences — this is read aloud, not printed."""
    scan_now = cached_scan()
    svcs = scan_now.get("services", [])
    down = [s for s in svcs if not s.get("up")]
    parts = ["Trace here."]

    # The fleet comes first because it is the half that has people in it.
    fleet = fleet_state()
    if fleet:
        undesc = [f["name"] for f in fleet if not f["purpose"]]
        quiet = [f["name"] for f in fleet
                 if f["idle_secs"] is not None and f["idle_secs"] > 6 * 3600]
        parts.append(f"{len(fleet)} agent sessions are up.")
        if quiet:
            parts.append(f"Quiet for over six hours: {', '.join(quiet[:4])}.")
        if undesc:
            parts.append(f"{len(undesc)} have no purpose written down: {', '.join(undesc[:4])}.")

    if down:
        names = ", ".join(s.get("name") or s.get("id") for s in down[:5])
        more = f", and {len(down) - 5} more" if len(down) > 5 else ""
        parts.append(f"{len(down)} of {len(svcs)} services are down: {names}{more}.")
    else:
        parts.append(f"All {len(svcs)} services are up.")

    stats = _get_json(f"{GRAPH_URL}/api/stats")
    if stats is None:
        parts.append("The knowledge graph is unreachable, so I have nothing to say about it.")
    else:
        parts.append(
            f"The graph holds {stats.get('nodes', 0)} nodes and "
            f"{stats.get('edges', 0)} edges, with {stats.get('rejects', 0)} rejects.")
        ghosts = stats.get("ghosts") or 0
        contra = stats.get("contradictions") or 0
        if ghosts or contra:
            bits = []
            if ghosts:
                bits.append(f"{ghosts} cited documents that do not exist")
            if contra:
                bits.append(f"{contra} contradictions between canonical sources")
            parts.append("Worth a look: " + ", and ".join(bits) + ".")
        else:
            parts.append("No ghosts and no contradictions.")

    lex = _get_json(f"{GRAPH_URL}/api/ask/lexicon")
    if lex and not lex.get("honest_absence"):
        a = lex.get("answer") or {}
        checked, off = a.get("checked", 0), a.get("off_grammar", 0)
        parts.append(
            f"All {checked} agents follow the naming grammar."
            if not off else
            f"{off} of {checked} agent names are off grammar.")

    return " ".join(parts)


# ── which surface is home ────────────────────────────────────────────────────
# Two front doors now exist and one of them has to answer `/`. That choice is
# per DEVICE, not per machine: the board belongs on the desk where there is room
# for forty tiles, and the simple screen belongs on the phone. A setting in
# config.json would force one answer onto both, so this is a cookie.
#
# The canonical paths never lie — `/board` is always the board and `/phone` is
# always the simple screen, whatever the cookie says. Only `/` follows the
# preference, which is what the installed Home Screen tile opens (start_url is
# `/`), and so what "change to simple UI" actually has to change.
#
# Set through a GET that redirects rather than from script, because the simple
# screen is server-rendered and carries almost no JS — and a preference that
# needs JS to stick is a preference that fails on the surface most likely to be
# opened when something is already wrong.
HOME_COOKIE = "fd_home"
HOME_CHOICES = ("board", "simple")
HOME_MAX_AGE = 60 * 60 * 24 * 365


def home_pref(cookie_header):
    """Which surface this device wants at `/`. Board unless told otherwise."""
    if not cookie_header:
        return "board"
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return "board"          # a malformed jar is not worth a 500
    got = jar.get(HOME_COOKIE)
    value = got.value if got else ""
    return value if value in HOME_CHOICES else "board"


# ── /phone ───────────────────────────────────────────────────────────────────
# The board shows everything on the machine, which is what a board is for. This
# is the opposite surface: a clock and six buttons sized for a thumb, for the
# times you already know where you are going.
#
# The list is DATA, not markup, so changing the front screen is editing one line
# rather than editing a page. Ids resolve against the same registry the board
# uses, so a button's URL is still worked out live from lsof binds and
# `tailscale serve` — nothing here hardcodes a link that can rot.
#
# A service that is registered but down still gets its button, dimmed and
# unclickable, labelled `down`. Hiding it would make a dead service and an
# unregistered one look identical from the one screen most likely to be opened
# when something is wrong.
PHONE_APPS = ["chat", "messages", "cockpit", "graph", "terminal", "pm"]

# Where the CALL key goes. Trace's conversational surface lives in the cockpit
# app, because that is where the ElevenLabs SDK, the admin session and the tool
# dispatcher already are — this server is stdlib Python and cannot host it.
#
# Resolved from the registry rather than hardcoded, like every other link on
# this board: the cockpit's reachable URL comes from lsof plus `tailscale
# serve`, and only its PATH is swapped. A pasted https:// link here would rot
# the first time the port or the tailnet name moved.
#
# If the cockpit is down the key falls back to /call — the local briefing, which
# needs nothing but this machine. Degrading to something that still works beats
# a dead button, and the label says which one you are getting.
# `?call=1` asked the cockpit for the CALL SCREEN rather than the Trace console.
# Without it the key landed on an admin surface whose own call button was a
# chip in the corner: a call screen handing off to a text UI containing a
# second, smaller CALL. One intent, one screen.
#
# NOW /admin/trace/local, WHICH IS A DIFFERENT VOICE STACK. ElevenLabs began
# refusing sessions on a billing problem (code 1002, "payment issue") and there
# was no way to see it from here — the API key lacks `user_read`, so the quota
# reads 401. A key that opens a call which dies on arrival is worse than one
# that opens a call which works, so it points at the stack that costs nothing:
# whisper.cpp for the ear, the operator's own cloned voice on :8890 for the
# mouth, and the same tool gate behind both.
#
# The trade is push-to-talk instead of full duplex — whisper transcribes a
# finished recording, so there is nothing to hear until you stop speaking.
# ?call=1 still works and is unchanged; it is one edit back if the account is
# topped up and full duplex is wanted again.
# NOW SERVED HERE, at /call-trace on this port.
#
# The cockpit surface it used to point at was a different voice stack on a
# different port whose liveness this server could not vouch for, and the label
# still promised "full duplex" long after the target became push-to-talk. This
# one has no such gap: it is the same process that renders the menu, so if the
# menu loads, the call surface loads.
#
# More importantly it mirrors the iMessage pipeline exactly rather than being a
# second way in. Text goes to router().deliver(), the same call chatbind makes
# for a text message; voice goes through the same local whisper.cpp that
# /api/listen already used. Replies are read back out of Trace's outbox, so the
# outbound boundary is untouched.
# CALL_TARGET_ID = "cockpit" lived here until 2026-09-16 and was read by
# nothing. It was left over from when this key opened a page on the cockpit,
# and it outlived that by long enough to convince a reader that /call-trace was
# still served on :3939 rather than by this process. A dead constant that names
# the wrong machine is worse than no constant.
CALL_TARGET_PATH = "/call-trace"

# NOW THE NATIVE MESSAGES APP, 2026-09-23.
#
# /call-trace is still served (below) and still works, but it was a second
# inbox for a conversation that already had one. Trace is talked to over
# iMessage \u2014 the daemon listens there, the replies land there, the history is
# there \u2014 so a web chat on the phone screen meant reading half the thread in
# one place and half in another, and answering in whichever one you happened to
# have open. The operator's word for it was that it "doesn't do anything".
#
# `sms:` is the scheme rather than a link because the destination is not a page.
# The handle is the address the Mac actually sends FROM \u2014 `last_addressed_handle`
# on the thread with the operator is `studio@wideband.ai`, so this opens the
# existing thread rather than starting a new one beside it. A phone number here
# would look more natural and be wrong: the Mac has no number, it has an Apple
# ID, and addressing the number would open the operator's thread with himself.
#
# No `&body=` prefill. This key is opened mid-conversation as often as at the
# start of one, and a draft dropped into a live thread is something to delete
# before you can type.
TRACE_IMESSAGE_HANDLE = "studio@wideband.ai"


def call_destination(by_id):
    """(href, label, sub) for the CALL key.

    Nothing to resolve and nothing to degrade to: the target is an app on the
    device, not a service on this machine, so there is no up/down branch and no
    promise here that this process could fail to keep.

    The sub-label dropped "voice" on 2026-09-16 when the spoken half was
    retired, and names Messages now that the key leaves the browser entirely.
    This page promised full duplex against a push-to-talk target once already;
    saying anything but where the tap actually lands would be that mistake
    twice.
    """
    return ("sms:" + TRACE_IMESSAGE_HANDLE, "Message Trace",
            "opens messages \u00b7 the real thread")


# \u2500\u2500 the live terminal network \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# The fleet map, which is its own service and not one of the scanned ones \u2014
# it is absent from services.json, so the registry cannot resolve it the way
# every other link on these surfaces is resolved. Written out in full here,
# once, rather than pasted into the two pages that link to it.
#
# NOT a mistake that the outside port and the inside port differ by two digits:
# `tailscale serve` publishes :18970 and proxies it to 127.0.0.1:18790. Checked
# against `tailscale serve status` on 2026-09-27 rather than assumed, because
# 18970/18790 is exactly the pair a reader corrects by hand and breaks.
#
# This is a DIFFERENT ORIGIN from this server \u2014 a different port is a different
# origin \u2014 which has one visible consequence worth stating where the constant
# lives: on the installed Android app, following this link leaves the
# standalone shell and lands in a Custom Tab with an address bar. That is the
# same cause as the six service keys on the phone screen and is not fixable
# from the link itself; it needs the target served under this origin.
NETMAP_URL = "https://brainwave.tailacfa70.ts.net:18970/fleet-map"
NETMAP_LABEL = "Live Terminal Network"


# ── cashflow ──────────────────────────────────────────────────────────────────
#
# The accountant session's cash view, served from THIS origin rather than
# through a tunnel.
#
# There was no tunnel to wire up — checked on 2026-09-27: no ssh port-forward
# running, nothing in ~/.ssh/config, no listener, and `start-accountant.sh`
# only launches codex in tmux. The page existed solely as a local file the
# agent had opened with `open file:///…/cashflow.html`, which a browser will
# not follow from an https page and which means nothing at all from a phone.
#
# Serving it here instead of behind a new port is the better end state anyway,
# and for the reason the operator has already run into twice: a different port
# is a different origin, so a tunnel would have dropped the installed Android
# app into a Custom Tab with an address bar, exactly like the six service keys
# and the fleet map. On this origin the cash view stays inside the app.
#
# Read from disk per request, never cached in this process: the accountant
# rewrites this file as the numbers change, and a monitoring surface that
# shows a copy taken at boot is worse than no surface. `_send` already sets
# Cache-Control: no-store, so the browser will not hold one either.
#
# The path is a CONSTANT and takes nothing from the request. There is no
# parameter here that could be pointed at another file.
CASHFLOW_PATH = os.path.expanduser("~/finance-ops/cashflow.html")
CASHFLOW_LABEL = "Cashflow"

# Injected at serve time, not written into the file — the agent regenerates it
# and anything edited in place would be gone by the next run. Needed because
# the installed app has no address bar and therefore no visible way back, and
# because this page is the accountant's artefact rather than one of ours: it
# has no header of its own to add a link to.
# A BAR AT THE TOP OF <body>, not a floating pill over it. The pill was the
# first version and it sat on top of the page's own headline — this document is
# somebody else's layout and there is no corner here that is reliably free.
# Sticky rather than fixed so it occupies its own height and pushes the content
# down instead of covering it, and still follows you down a long table.
#
# Deliberately in Fleetdeck's colours against what is a light-themed page: it
# is not trying to look like part of the cash view, it is the frame around it,
# and reading as a seam is the honest outcome.
# Styled INLINE, every rule of it, and that is not laziness. This markup gets
# injected into documents this server did not write — the accountant's page
# today, whatever an agent generates next — where there is no stylesheet to add
# to and no way to know what a class name would collide with. Inline styles are
# the only ones that cannot be overridden by a host page's cascade.
FLEET_BAR = """
<div id="fd-back" style="position:sticky;top:0;z-index:2147483647;flex:none;
 display:flex;align-items:center;justify-content:space-between;gap:12px;
 padding:10px max(12px,env(safe-area-inset-left));
 padding-top:max(10px,env(safe-area-inset-top));
 background:#05070a;border-bottom:1px solid #1d5f52;
 font:11px/1 ui-monospace,SF Mono,Menlo,monospace;letter-spacing:.14em;
 text-transform:uppercase">
 <a href="/phone" style="color:#4fe3c1;text-decoration:none">&lsaquo; Fleetdeck</a>
 <span style="color:#2b3a45">__SUB__</span>
</div>
"""


# ── transitions ───────────────────────────────────────────────────────────────
#
# Cross-document view transitions. Three declarations turn seven separate page
# loads into something that moves like one app, and the reason it is safe is
# that a browser without support ignores the at-rule entirely and navigates
# exactly as it does today. There is no JavaScript path to go wrong, no
# interception of clicks, no single-page rewrite, and nothing to unwind.
#
# `navigation: auto` has to be present on BOTH documents — the one being left
# and the one being entered — or the browser does the ordinary hard swap. That
# is why this is interpolated into every same-origin surface rather than only
# the home screen.
#
# The numbers are the whole difference between polish and clunk. 120ms out and
# 200ms in is under the threshold where a transition starts to feel like
# waiting; the 6px rise is small enough to read as the screen settling rather
# than as a slide. Anything longer and every tap has a toll booth on it.
#
# The rise cannot show a seam because every surface here is #05070a on #05070a.
#
# What is deliberately NOT here: named elements. Morphing the tapped key into
# the next screen's header is the demo everyone builds, and it is also where
# this gets fragile — one renamed class and the morph half-plays. A crossfade
# has nothing to misalign.
VIEW_TRANSITION_CSS = """
 @view-transition{navigation:auto}
 ::view-transition-old(root){animation:fd-out 120ms ease both}
 ::view-transition-new(root){animation:fd-in 200ms cubic-bezier(.2,0,0,1) both}
 @keyframes fd-out{to{opacity:0}}
 @keyframes fd-in{from{opacity:0;transform:translateY(6px)}}
 /* Both forms on purpose. Nesting @view-transition in a conditional group is
    the correct way to say this and is not everywhere yet; killing the
    animations says it again in a way that has been valid CSS for a decade. An
    at-rule a browser does not understand is dropped, so the pair costs
    nothing and cannot disagree. */
 @media(prefers-reduced-motion:reduce){
   @view-transition{navigation:none}
   ::view-transition-old(root),::view-transition-new(root){animation:none}
 }
"""


def fleet_bar(sub):
    """The seam. One bar, one look, wherever a foreign surface is framed.

    It exists because the installed app has no address bar and therefore no
    browser-provided way back — the thing that makes these surfaces feel like
    one app is also what strands you inside them.
    """
    return FLEET_BAR.replace("__SUB__", esc_html(sub))

# Said plainly rather than as a 404, because the realistic cause is not "this
# is broken" — it is that the accountant has not written the file yet, or has
# moved it. Naming the path it looked for is the difference between a dead
# button and a one-line fix.
CASHFLOW_MISSING = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>cashflow // not written yet</title>
<style>
 html,body{margin:0;height:100%;background:#05070a;color:#8fa3b0;
   font:14px/1.6 ui-monospace,"SF Mono",Menlo,monospace;
   display:grid;place-items:center;padding:24px;text-align:center}
 h1{font-size:13px;letter-spacing:.24em;text-transform:uppercase;color:#4fe3c1;
   font-weight:400;margin:0 0 14px}
 code{color:#d6e4ec;font-size:12px;word-break:break-all}
 p{margin:0 0 10px;max-width:34em}
 a{color:#4a5b68;text-decoration:none;font-size:12px;letter-spacing:.12em}
</style></head><body><div>
 <h1>Nothing to show yet</h1>
 <p>The accountant session has not written its cash view.</p>
 <p><code>__PATH__</code></p>
 <p><a href="/phone">&lsaquo; back to Fleetdeck</a></p>
</div></body></html>"""


# \u2500\u2500 framed apps \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# Cashflow got to keep the installed app's fullscreen because it is a static
# file this server can read and serve from its own origin. A live service on
# another port cannot be handled that way: proxying it would mean rewriting
# every absolute path it fetches, and these apps all fetch absolute paths.
#
# So the top-level document stays here and the service is framed inside it.
# The browser never leaves this origin, so the PWA never breaks out into a
# Custom Tab \u2014 while inside the frame the app talks to its own port exactly as
# it always did, because a frame resolves its URLs against its own origin.
# Nothing about the framed service changes; it does not know it is framed.
#
# Verified before building rather than assumed (2026-09-27): none of the six
# phone targets send X-Frame-Options or CSP frame-ancestors, and :8782 loaded
# in a cross-origin frame with its /api/chats returning 200 and no request
# failures. The known risk is storage partitioning \u2014 a framed document gets its
# own cookie and localStorage jar \u2014 which costs nothing here because these
# services have no login, their gate being the tailnet itself.
#
# A CLOSED SET, keyed by registry id. The URL is resolved from the scan like
# every other link on these surfaces; nothing in the request names a URL, so
# this cannot be pointed at an arbitrary origin by editing the address.
# Every key on the phone screen, so none of them breaks out of the installed
# app. Keys are registry ids — `chat` is what the screen calls FLEET, `pm` is
# what it calls BOARD.
#
# All six were loaded in a real cross-origin frame before being added here, and
# the two that could have failed did not: `chat` frames its own ttyd terminals,
# so it is a frame inside a frame with a websocket at the bottom, and
# `terminal` is ttyd directly. Both connected (`wss://…:8783/t/ws`,
# `wss://…:8781/ws`) and rendered live sessions. `graph` draws to a canvas and
# `pm` is a plain page; neither had anything to lose.
SHELLED_APPS = {
    "chat": "Fleet", "messages": "Messages", "cockpit": "Cockpit",
    "graph": "Graph", "terminal": "Terminal", "pm": "Board",
}

# Permissions a framed document does NOT inherit from its parent and has to be
# granted by name. Deliberately per-app and as short as possible: `cockpit` is
# where Trace listens, and it is the only surface here with any reason to reach
# a microphone. Granting it to all six would cost nothing visible and would be
# the kind of default nobody revisits.
SHELL_ALLOW_DEFAULT = "clipboard-read; clipboard-write"
SHELL_ALLOW = {"cockpit": "microphone; clipboard-read; clipboard-write"}

# Framed surfaces with no registry entry to resolve, as (label, url).
#
# The fleet map is not in services.json, so the scan cannot find it and the
# id→service lookup the six keys use does not apply. Kept in its own map rather
# than faked into the registry: a fabricated service entry would appear as a
# tile on the board, be scanned for liveness on a port the scanner does not
# own, and lie about where it came from.
#
# The trade here is real and worth naming: there is no up/down branch for these,
# because liveness comes from the scan and these are not scanned. A stopped
# service behind one of these shows the browser's own error inside the frame.
#
# This one also needed the OTHER SERVICE to allow it. The map sent
# `frame-ancestors 'none'`, which refuses every framer including itself; it now
# names this origin. Without that edit in ~/srv/fleetdeck-authoring the frame
# here loads an empty document and the browser cancels the request — which is
# what it did when this was first attempted.
SHELLED_STATIC = {"netmap": (NETMAP_LABEL, NETMAP_URL)}

APP_SHELL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__LABEL__ // __MACHINE__</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__LABEL__">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>__VT__
 html,body{margin:0;height:100%;background:#05070a;overflow:hidden}
 /* Column, not a scrolling document: the bar takes its own height and the
    frame takes the rest exactly. Any page scroll here would be the shell
    scrolling behind a frame that scrolls too \u2014 two scrollbars for one list. */
 body{display:flex;flex-direction:column;height:100dvh}
 iframe{flex:1 1 auto;width:100%;border:0;display:block;background:#05070a}
</style></head><body>
__BAR__
<iframe src="__URL__" title="__LABEL__" allow="__ALLOW__"></iframe>
</body></html>"""

# Down is a state, not an error: the frame would otherwise show the browser's
# own connection-failed page, which is the one screen guaranteed to look like
# Fleetdeck is broken rather than the service being off.
APP_SHELL_DOWN = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__LABEL__ // not running</title>
<style>
 html,body{margin:0;height:100%;background:#05070a;color:#8fa3b0;
   font:14px/1.6 ui-monospace,"SF Mono",Menlo,monospace;
   display:grid;place-items:center;padding:24px;text-align:center}
 h1{font-size:13px;letter-spacing:.24em;text-transform:uppercase;color:#4fe3c1;
   font-weight:400;margin:0 0 14px}
 p{margin:0 0 10px;max-width:32em}
 a{color:#4a5b68;text-decoration:none;font-size:12px;letter-spacing:.12em}
</style></head><body><div>
 <h1>__LABEL__ is not running</h1>
 <p>The service is registered but nothing is answering on this machine.</p>
 <p><a href="/phone">&lsaquo; back to Fleetdeck</a></p>
</div></body></html>"""


# \u2500\u2500 notes \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
#
# A capture surface for voice-note transcripts and half-formed ideas, which
# until now landed in whatever app was open and were never seen again.
#
# The shape is the point. A generic list turns into a graveyard because every
# row costs the same to add and nothing says what to DO with it. A note here is
# four fields \u2014 what it is, what it actually means, the single next move, and a
# status \u2014 so an idea is either executable or visibly not yet distilled. Status
# is a closed set, not free text, because the value of a status is that it
# sorts, and free text does not sort.
#
# ONE file, rewritten whole. This is a single-operator surface holding a few
# hundred notes at the very most; a database would be more machinery than the
# problem has. The write is tmp-then-rename so a crash mid-write leaves the
# previous file intact rather than a truncated one \u2014 the failure that loses
# everything rather than the last entry.
# A test instance can set a private path before import. The default remains the
# operator's existing store; tests must never point their write path there.
NOTES_PATH = os.path.expanduser(
    os.environ.get("FLEETDECK_NOTES_PATH", "~/.fleetdeck-notes.json"))

# Ordered: this is also the display order and the cycle order of the chip.
NOTE_STATUSES = ("inbox", "ready", "parked", "done")

# Caps, applied server-side on every path in. They are generous enough that no
# honest note hits them and small enough that the file cannot be grown without
# bound by something automated \u2014 Trace writes here too, and an agent in a loop
# is the realistic way this file gets ruined.
NOTE_LIMITS = {"title": 140, "concept": 600, "next": 400, "original": 12000}
NOTES_MAX = 500

_notes_lock = threading.Lock()


@contextmanager
def _notes_guard():
    """Serialize read-modify-write across threads and separate server processes."""
    with _notes_lock:
        lock_fd = os.open(NOTES_PATH + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

# A random suffix keeps ids unique across separate portal processes as well as
# threads. The timestamp keeps a human-readable order in backups.


# The first note is the operator's own, transcribed from a voice message on
# 2026-09-25. It is seeded rather than typed so the surface is never empty on
# first open \u2014 an empty list teaches nothing about what a good note looks like,
# and this one is the worked example: one strong idea, distilled, with a move.
SEED_NOTES = [{
    "title": "Instant iMessage Agent Installer",
    "original": (
        "Reduce installation to the simplest path. A person has an Apple Account and a "
        "computer; install a minimal Wideband layer with no optional features "
        "so they can text one assigned head agent immediately. That agent then "
        "guides them into optional outcomes, such as scaffolding a website and "
        "dev server."),
    "concept": (
        "Apple Account plus computer leads to a minimal Wideband install, immediate "
        "iMessage access, one head agent, then guided capability expansion."),
    "next": (
        "Map and prototype the shortest verified path from blank computer to "
        "first successful text reply, separating mandatory setup from later "
        "add-ons."),
    "status": "inbox",
    "source": "voice",
}]


def _notes_read():
    """The list on disk, or the seed if there is nothing there yet.

    Returns [] rather than raising on a corrupt file: this is read on the way
    to rendering a page, and a parse error should cost the operator the notes,
    not the whole front screen. The bad file is left alone to be looked at.
    """
    try:
        with open(NOTES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("notes") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except FileNotFoundError:
        return None                      # distinct from empty \u2014 see notes_all()
    except Exception as e:
        sys.stderr.write("fleetdeck: notes unreadable (%s)\n" % e)
        return []


def _notes_write(items):
    """Whole file, atomically. Caller holds _notes_lock."""
    directory = os.path.dirname(NOTES_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix=".fleetdeck-notes-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"notes": items}, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, NOTES_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def notes_all():
    """Every note, seeded on first use.

    FileNotFoundError is the only condition that seeds. A file that exists and
    holds an empty list means the operator deleted the seed, and putting it
    back every time would be the surface arguing with him.
    """
    with _notes_guard():
        items = _notes_read()
        if items is None:
            items = ([] if onboarding_config() else
                     [notes_normalise(n) for n in SEED_NOTES])
            _notes_write(items)
        return items


def _note_field(payload, key):
    """One content field, coerced, stripped and capped.

    Module-level rather than a closure inside notes_normalise() because the
    edit path needs exactly the same treatment. A second copy of this that
    drifted by one cap would mean a field you can type into but not save.
    """
    v = payload.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return v.strip()[:NOTE_LIMITS[key]]


def _note_title(title, original):
    """A note with no title but a body is normal — it is what a raw voice dump
    looks like before anyone distils it. Naming it from its own first line
    beats showing a blank card, and is replaced the moment a real title
    arrives. Shared with the edit path so clearing a title behaves the same as
    never having typed one."""
    if title:
        return title
    first = (original.splitlines() or [""])[0].strip()
    if len(first) > 70:
        return first[:70].rstrip() + "…"
    return first or "untitled"


def notes_normalise(payload):
    """A trusted note from an untrusted dict. The ONLY door into the store.

    This is the write path Trace will use when he turns a voice transcript into
    an organised note, so it is written for a caller that may be wrong rather
    than one that is merely careless:

      * Only the five content keys are read. Anything else in the payload \u2014
        `id`, `created`, a path, a flag \u2014 is dropped on the floor rather than
        merged, so no caller can overwrite another note by naming its id, and
        no future field can be set by a client that happens to guess its name.
      * `id` and `created` are assigned HERE, from the server's clock and
        counter. A client-supplied id is the difference between adding a note
        and silently replacing one.
      * Every string is coerced, stripped and truncated. `status` must be one
        of four literals or it becomes `inbox`; there is no path by which an
        arbitrary string reaches the file.

    The result is that the worst a confused agent can do to this file is add a
    badly-worded note, which the operator can see and delete. It cannot reach
    anything else on the machine, because nothing here takes a path.
    """
    if not isinstance(payload, dict):
        payload = {}

    def field(key):
        return _note_field(payload, key)

    status = payload.get("status")
    status = status if status in NOTE_STATUSES else "inbox"

    title = _note_title(field("title"), field("original"))
    original = field("original")

    # `source` is a label, not a capability \u2014 it says where the note came from
    # so the list can show that Trace wrote it. Closed set, same reasoning as
    # status.
    source = payload.get("source")
    source = source if source in ("phone", "voice", "trace") else "phone"

    return {
        "id": "n%d-%s" % (time.time() * 1000, uuid.uuid4().hex),
        "created": int(time.time()),
        "title": title,
        "original": original,
        "concept": field("concept"),
        "next": field("next"),
        "status": status,
        "source": source,
    }


# The mini-format. One textarea is the whole add form, because a four-field
# form is four decisions at the moment the operator has the least patience for
# them \u2014 the idea is in his head and the point is to get it out.
#
# So: type anything and it is captured raw. Label the lines and they are read.
# Nothing is mandatory and the raw text is kept either way, so this can only
# add structure, never lose it.
NOTE_LABELS = {"title": "title", "concept": "concept", "idea": "concept",
               "next": "next", "move": "next", "next move": "next"}
_NOTE_LABEL_RE = re.compile(r"^\s*([a-z ]{3,9})\s*:\s*(.*)$", re.I)


def note_from_text(raw):
    """Labelled lines into fields. Everything is kept as `original` regardless.

    The raw text is preserved verbatim and unconditionally \u2014 a transcript is
    the evidence, and a distillation that loses what was actually said cannot
    be checked against anything.
    """
    raw = (raw or "").strip()
    out = {"original": raw, "source": "phone"}
    current = None
    for line in raw.splitlines():
        m = _NOTE_LABEL_RE.match(line)
        key = NOTE_LABELS.get(m.group(1).strip().lower()) if m else None
        if key:
            out[key] = m.group(2).strip()
            current = key
        elif current and line.strip() and line[:1].isspace():
            # A labelled value continued on the next line — and INDENTED, which
            # is the whole condition. Continuing on any non-empty line instead
            # was the first version, and it quietly ate the sentence after the
            # last label: "next: pick the scheduler" followed by a line of
            # ordinary prose became a next move with someone's narration
            # stapled to it. A textarea soft-wraps without inserting newlines,
            # so a real wrap never reaches here anyway; every line break in
            # this text was typed on purpose, and indenting one is a thing a
            # person does deliberately. Unindented prose stays out of the
            # fields and is still kept whole in `original`, so nothing is lost
            # by reading it conservatively.
            out[current] = (out[current] + " " + line.strip()).strip()
        else:
            current = None
    return out


def notes_add(payload):
    """Normalise, prepend, cap, persist. Returns the stored note."""
    with _notes_guard():
        note = notes_normalise(payload)
        items = _notes_read()
        if items is None:
            items = ([] if onboarding_config() else
                     [notes_normalise(n) for n in SEED_NOTES])
        items.insert(0, note)
        del items[NOTES_MAX:]
        _notes_write(items)
    return note


def notes_set_status(note_id, status):
    """Returns the updated note, or None if the id or status is not real."""
    if status not in NOTE_STATUSES:
        return None
    with _notes_guard():
        items = _notes_read() or []
        for n in items:
            if n.get("id") == note_id:
                n["status"] = status
                _notes_write(items)
                return n
    return None


def notes_update(note_id, payload):
    """Edit an existing note's content in place. Returns it, or None.

    PARTIAL by design: only keys actually present in the payload are touched.
    A caller that knows about three fields cannot blank a fourth it has never
    heard of, which is what makes this safe to point a future version of Trace
    at — he can correct a concept without having to resend the transcript.

    What is NOT editable here is as deliberate as what is. `id`, `created` and
    `source` are what the note IS rather than what it says; letting an edit
    rewrite them would mean a note could quietly become a different note, and
    `source` in particular is the only thing distinguishing what the operator
    wrote from what an agent did. `status` is left alone because it has its own
    endpoint — it is a different gesture, made from the list without opening
    anything, and folding it in here would mean every edit had to carry a
    status or risk resetting one.
    """
    if not isinstance(payload, dict):
        return None
    with _notes_guard():
        items = _notes_read() or []
        for n in items:
            if n.get("id") != note_id:
                continue
            edited = {k: _note_field(payload, k)
                      for k in ("title", "concept", "next", "original")
                      if k in payload}
            merged = dict(n, **edited)
            # Every field blanked would persist a note that renders as an
            # untitled ghost with no way back to what it said. Refused rather
            # than written, because the undo for this does not exist.
            if not (merged["title"] or merged["original"] or merged["concept"]):
                return None
            merged["title"] = _note_title(merged["title"], merged["original"])
            merged["edited"] = int(time.time())
            n.clear()
            n.update(merged)
            _notes_write(items)
            return n
    return None


def notes_summary():
    """The sub-label on the phone key: what is waiting, not how many exist.

    A total is a number you stop reading after the first week. The two counts
    that change behaviour are what has not been distilled yet and what is ready
    to act on, so those are the two that get said out loud.
    """
    try:
        items = notes_all()
    except Exception:
        return "notes"
    inbox = sum(1 for n in items if n.get("status") == "inbox")
    ready = sum(1 for n in items if n.get("status") == "ready")
    if not items:
        return "nothing captured"
    if not inbox and not ready:
        return "all clear"
    return " · ".join(p for p in (("%d in inbox" % inbox) if inbox else "",
                                  ("%d ready" % ready) if ready else "") if p)


def notes_delete(note_id):
    with _notes_guard():
        items = _notes_read() or []
        keep = [n for n in items if n.get("id") != note_id]
        if len(keep) == len(items):
            return False
        _notes_write(keep)
        return True


NOTES_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__MACHINE__ // notes</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="notes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>__VT__
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#05070a;color:#d6e4ec;
   font:16px/1.45 ui-monospace,"SF Mono",Menlo,monospace;
   padding:max(14px,env(safe-area-inset-top)) 14px max(22px,env(safe-area-inset-bottom))}
 header{display:flex;align-items:baseline;gap:10px;padding:4px 2px 14px}
 header h1{font-size:13px;font-weight:400;letter-spacing:.26em;
   text-transform:uppercase;color:#4fe3c1}
 header .sub{flex:1;font-size:11px;letter-spacing:.16em;color:#2b3a45}
 header a{font-size:12px;letter-spacing:.12em;color:#4a5b68;text-decoration:none}
 header a:active{color:#4fe3c1}

 /* ── capture ──────────────────────────────────────────────────────────────
    One field. A four-input form is four decisions at the moment the operator
    has least patience for them, and the idea is already leaving his head. */
 .add{border:2px solid #18222b;border-radius:14px;background:#0a0e13;padding:11px}
 .add:focus-within{border-color:#1d5f52}
 .add textarea{width:100%;min-height:62px;resize:none;border:0;outline:0;
   background:none;color:#d6e4ec;font:inherit;line-height:1.5}
 .add textarea::placeholder{color:#2f414d}
 .addrow{display:flex;align-items:center;gap:10px;padding-top:9px}
 .hint{flex:1;font-size:10px;letter-spacing:.1em;color:#2b3a45}
 .add button{border:2px solid #1d5f52;border-radius:11px;background:#0b1a17;
   color:#4fe3c1;font:inherit;font-size:12px;letter-spacing:.18em;
   text-transform:uppercase;padding:9px 17px;min-height:40px}
 .add button:active{border-color:#4fe3c1;background:#10241f}
 .add button[disabled]{opacity:.35}

 /* ── filters ──────────────────────────────────────────────────────────── */
 .tabs{display:flex;gap:6px;overflow-x:auto;padding:16px 0 12px;
   scrollbar-width:none}
 .tabs::-webkit-scrollbar{display:none}
 .tab{flex:0 0 auto;border:1px solid #18222b;border-radius:999px;
   background:none;color:#4a5b68;font:inherit;font-size:10.5px;
   letter-spacing:.14em;text-transform:uppercase;padding:7px 12px}
 .tab b{font-weight:400;color:#2b3a45;padding-left:5px}
 .tab.on{border-color:#1d5f52;background:#0b1a17;color:#4fe3c1}
 .tab.on b{color:#2f7d6d}

 /* ── the card ─────────────────────────────────────────────────────────────
    Title, concept, move. Collapsed it is a claim and an action; that is the
    whole reason this is not a list of sentences. */
 .card{border:2px solid #18222b;border-radius:14px;background:#0a0e13;
   padding:13px 14px;margin-bottom:10px}
 .card.op{border-color:#243542}
 .card h2{font-size:16px;font-weight:400;line-height:1.3;color:#d6e4ec;
   letter-spacing:.01em}
 .card .cc{margin-top:7px;font-size:13.5px;line-height:1.5;color:#8fa3b0}
 /* Clamped until opened. A card that can be any height is a list you scroll
    past rather than read. */
 .card:not(.op) .cc{display:-webkit-box;-webkit-line-clamp:2;
   -webkit-box-orient:vertical;overflow:hidden}
 .card .mv{margin-top:9px;font-size:13px;line-height:1.45;color:#4fe3c1;
   display:flex;gap:7px}
 .card .mv i{font-style:normal;color:#2f7d6d;flex:0 0 auto}
 .card:not(.op) .mv span{display:-webkit-box;-webkit-line-clamp:1;
   -webkit-box-orient:vertical;overflow:hidden}
 .card.done h2{color:#4a5b68}
 .card.done .cc,.card.done .mv{opacity:.45}

 /* The original, only once opened, and scrolled inside the card rather than
    lengthening the page — a 900-word transcript must not push every other
    idea below the fold. */
 .orig{margin-top:12px;padding-top:11px;border-top:1px solid #18222b;
   max-height:38vh;overflow-y:auto;-webkit-overflow-scrolling:touch;
   font-size:12.5px;line-height:1.6;color:#6f8593;white-space:pre-wrap;
   overflow-wrap:anywhere}
 .orig em{display:block;font-style:normal;font-size:9.5px;letter-spacing:.18em;
   text-transform:uppercase;color:#2b3a45;margin-bottom:6px}

 .foot{display:flex;align-items:center;gap:8px;margin-top:11px}
 /* The status chip IS the control. One tap advances it, which is the fastest
    thing a thumb can do and needs no menu, no sheet and no precision. */
 .chip{border:1px solid;border-radius:999px;background:none;font:inherit;
   font-size:10px;letter-spacing:.17em;text-transform:uppercase;
   padding:6px 11px;min-height:32px}
 .chip.inbox{color:#8fa3b0;border-color:#2c3c48}
 .chip.ready{color:#4fe3c1;border-color:#1d5f52;background:#0b1a17}
 .chip.parked{color:#d8a657;border-color:#4a3c22}
 .chip.done{color:#3f6b5e;border-color:#20342e}
 .foot .when{flex:1;font-size:10px;letter-spacing:.12em;color:#2b3a45}
 .foot .src{font-size:9px;letter-spacing:.16em;text-transform:uppercase;
   color:#2b3a45}
 .foot .del,.foot .edit{border:0;background:none;color:#2b3a45;font:inherit;
   font-size:11px;letter-spacing:.1em;padding:6px 4px}
 /* Both only exist once the card is open. Edit and delete on a collapsed card
    would put a destructive target a thumb's width from the status chip, on the
    one screen most likely to be used while walking. */
 .card:not(.op) .del,.card:not(.op) .edit{display:none}
 .foot .del:active{color:#c2554d}
 .foot .edit:active{color:#4fe3c1}

 /* ── the editor ───────────────────────────────────────────────────────────
    The same four fields the card shows, in the same order, in place. Opening
    a separate screen to edit would break the one rule this surface has — that
    detail happens where the idea already is. */
 /* No top rule. The editor REPLACES the card body rather than following it, so
    a separator here draws a line under nothing. */
 .ed label{display:block;font-size:9.5px;letter-spacing:.18em;
   text-transform:uppercase;color:#2b3a45;margin:11px 0 5px}
 .ed label:first-child{margin-top:0}
 .ed input,.ed textarea{width:100%;border:1px solid #1b2731;border-radius:9px;
   background:#05070a;color:#d6e4ec;font:inherit;font-size:13.5px;line-height:1.5;
   padding:9px 10px;resize:vertical;-webkit-appearance:none}
 .ed input:focus,.ed textarea:focus{outline:0;border-color:#1d5f52}
 /* Heights are set in script to fit the text — see autosize(). These are the
    floor for an empty field and the ceiling for a long transcript, which is
    the one field that can be any length at all. */
 .ed textarea{min-height:58px;max-height:44vh}
 .foot .save{border:1px solid #1d5f52;border-radius:999px;background:#0b1a17;
   color:#4fe3c1;font:inherit;font-size:10px;letter-spacing:.17em;
   text-transform:uppercase;padding:6px 13px;min-height:32px}
 .foot .save:active{border-color:#4fe3c1;background:#10241f}
 .foot .cancel{border:0;background:none;color:#4a5b68;font:inherit;font-size:11px;
   letter-spacing:.1em;padding:6px 4px}

 .empty{text-align:center;color:#2b3a45;font-size:12px;letter-spacing:.12em;
   padding:38px 0}
 .err{color:#c2554d;font-size:11px;letter-spacing:.1em;padding:8px 2px}
 @media(min-width:620px){body{max-width:620px;margin:0 auto}}
</style></head><body>
<header><h1>Notes β</h1><span class="sub" id="count"></span><a href="/phone">&lsaquo; home</a></header>

<form class="add" id="add">
 <textarea id="txt" rows="2" placeholder="paste a transcript, or type the idea" autocapitalize="sentences"></textarea>
 <div class="addrow">
   <span class="hint">title: concept: next: to distil &middot; raw is always kept</span>
   <button type="submit" id="go">Capture</button>
 </div>
</form>
<div class="err" id="err" hidden></div>

<div class="tabs" id="tabs"></div>
<div id="list"></div>

<script>
 var STATUSES = __STATUSES__;
 var notes = [], filter = 'all', open = null;
 // The note being edited, and a pristine copy of it taken on the way in so
 // cancel has something true to restore. Keystrokes are written straight into
 // the note object rather than read off the DOM at save time, so a redraw
 // from anywhere — a filter tap, a status change elsewhere — re-renders what
 // you had typed instead of throwing it away.
 var editing = null, backup = null;
 var D = function(i){ return document.getElementById(i) };

 function esc(s){
   return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
     return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c] });
 }

 function ago(ts){
   var s = Math.max(0, Math.floor(Date.now()/1000 - ts));
   if(s < 90) return 'just now';
   if(s < 5400) return Math.round(s/60) + 'm ago';
   if(s < 129600) return Math.round(s/3600) + 'h ago';
   return Math.round(s/86400) + 'd ago';
 }

 // Status is the sort. Inbox first because undistilled ideas are the ones that
 // rot; done sinks because it is a record, not a queue.
 function rank(n){ var i = STATUSES.indexOf(n.status); return i < 0 ? 0 : i }

 // Sorting is applied to `notes` at chosen moments rather than on every draw,
 // and that is the whole point of it being a separate function.
 //
 // Re-sorting inside draw() meant a status tap moved the card out from under
 // the thumb that tapped it: promote an inbox note to ready and it leaves the
 // inbox block mid-gesture, so the next tap lands on whatever slid up into the
 // gap. The list settles on arrival, on a filter change and on a return to the
 // tab — never as a consequence of touching something.
 function resort(){
   notes.sort(function(a, b){
     return rank(a) - rank(b) || (b.created || 0) - (a.created || 0) });
 }
 function ordered(){
   return notes.filter(function(n){ return filter === 'all' || n.status === filter });
 }

 function drawTabs(){
   var all = [['all', notes.length]].concat(STATUSES.map(function(s){
     return [s, notes.filter(function(n){ return n.status === s }).length] }));
   D('tabs').innerHTML = all.map(function(p){
     return '<button class="tab' + (filter === p[0] ? ' on' : '') +
            '" data-f="' + p[0] + '">' + p[0] + '<b>' + p[1] + '</b></button>' }).join('');
 }

 function drawList(){
   var rows = ordered();
   if(!rows.length){
     D('list').innerHTML = '<div class="empty">nothing ' +
       (filter === 'all' ? 'captured yet' : 'in ' + filter) + '</div>';
     return;
   }
   D('list').innerHTML = rows.map(function(n){
     var isOpen = n.id === open, isEd = n.id === editing;

     // Editing replaces the card's body, not the card. The title you are
     // typing stays where the title was.
     var body = isEd
       ? '<div class="ed">' +
           '<label>title</label>' +
           '<input data-f="title" value="' + esc(n.title) + '">' +
           '<label>concept</label>' +
           '<textarea data-f="concept">' + esc(n.concept) + '</textarea>' +
           '<label>next move</label>' +
           '<textarea data-f="next">' + esc(n.next) + '</textarea>' +
           '<label>original</label>' +
           '<textarea data-f="original">' + esc(n.original) + '</textarea>' +
         '</div>'
       : '<h2>' + esc(n.title) + '</h2>' +
         (n.concept ? '<div class="cc">' + esc(n.concept) + '</div>' : '') +
         (n.next ? '<div class="mv"><i>&rarr;</i><span>' + esc(n.next) + '</span></div>' : '') +
         (isOpen && n.original
            ? '<div class="orig"><em>original</em>' + esc(n.original) + '</div>' : '');

     // While editing, the foot is save/cancel and nothing else. Leaving the
     // status chip and delete in reach of a thumb that is aiming at Save is
     // how you lose a note you were in the middle of fixing.
     var foot = isEd
       ? '<button class="cancel" data-cancel="' + esc(n.id) + '">cancel</button>' +
         '<span class="when"></span>' +
         '<button class="save" data-save="' + esc(n.id) + '">save</button>'
       : '<button class="chip ' + esc(n.status) + '" data-chip="' + esc(n.id) + '">' +
            esc(n.status) + '</button>' +
         '<span class="when">' + ago(n.created) +
            (n.edited ? ' &middot; edited' : '') + '</span>' +
         (n.source && n.source !== 'phone'
            ? '<span class="src">' + esc(n.source) + '</span>' : '') +
         '<button class="edit" data-edit="' + esc(n.id) + '">edit</button>' +
         '<button class="del" data-del="' + esc(n.id) + '">delete</button>';

     return '<article class="card ' + esc(n.status) +
       (isOpen || isEd ? ' op' : '') + '" data-id="' + esc(n.id) + '">' +
       body + '<div class="foot">' + foot + '</div></article>';
   }).join('');
 }

 function draw(){ drawTabs(); drawList(); if(editing) autosizeAll();
   D('count').textContent = notes.length + (notes.length === 1 ? ' idea' : ' ideas'); }

 function fail(m){ var e = D('err'); e.textContent = m; e.hidden = !m }

 async function pull(){
   try{
     var r = await fetch('/api/notes');
     if(!r.ok) throw new Error('HTTP ' + r.status);
     notes = (await r.json()).notes || [];
     resort();
     fail('');
   }catch(e){ fail('could not load notes — ' + e.message) }
   draw();
 }

 // ── capture ───────────────────────────────────────────────────────────────
 D('add').addEventListener('submit', async function(e){
   e.preventDefault();
   var t = D('txt').value.trim();
   if(!t) return;
   D('go').disabled = true;
   try{
     var r = await fetch('/api/notes', {method:'POST',
       headers:{'Content-Type':'application/json'}, body: JSON.stringify({text: t})});
     if(!r.ok) throw new Error('HTTP ' + r.status);
     var n = (await r.json()).note;
     D('txt').value = '';
     notes.unshift(n);
     // Straight to open: the one thing you want after capturing a raw dump is
     // to see what it became, and on a phone that is otherwise a scroll away.
     open = n.id; filter = 'all';
     fail(''); draw();
   }catch(e){ fail('not saved — ' + e.message) }
   D('go').disabled = false;
 });

 // Enter sends from a real keyboard; on a phone Return must still insert a
 // newline, because a transcript pasted in has them and a note being typed
 // wants them.
 D('txt').addEventListener('keydown', function(e){
   if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){
     e.preventDefault(); D('add').requestSubmit();
   }
 });

 D('tabs').addEventListener('click', function(e){
   var b = e.target.closest('.tab'); if(!b) return;
   filter = b.dataset.f; resort(); draw();
 });

 // A textarea at a fixed height clips its own text halfway down a line, which
 // reads as damage rather than as scrolling. Fitting it to its content means
 // you can see the whole field you are being asked to correct — capped, since
 // `original` holds transcripts and an honest fit for one of those would be
 // taller than the phone.
 function autosize(el){
   el.style.height = 'auto';
   el.style.height = Math.min(el.scrollHeight, innerHeight * 0.44) + 'px';
 }
 function autosizeAll(){
   Array.prototype.forEach.call(document.querySelectorAll('.ed textarea'), autosize);
 }

 // Typing goes straight into the note. No redraw, so the caret stays put.
 D('list').addEventListener('input', function(e){
   var f = e.target.dataset && e.target.dataset.f;
   if(!f || !editing) return;
   var n = notes.find(function(x){ return x.id === editing });
   if(n) n[f] = e.target.value;
   if(e.target.tagName === 'TEXTAREA') autosize(e.target);
 });

 function stopEditing(restore){
   if(restore && backup){
     var n = notes.find(function(x){ return x.id === editing });
     if(n) Object.assign(n, backup);
   }
   editing = null; backup = null;
 }

 D('list').addEventListener('click', async function(e){
   var chip = e.target.closest('[data-chip]');
   var del  = e.target.closest('[data-del]');
   var ed   = e.target.closest('[data-edit]');
   var save = e.target.closest('[data-save]');
   var canc = e.target.closest('[data-cancel]');
   var card = e.target.closest('.card');

   if(ed){
     var n0 = notes.find(function(x){ return x.id === ed.dataset.edit });
     if(!n0) return;
     editing = n0.id; open = n0.id;
     backup = {title:n0.title, concept:n0.concept, next:n0.next, original:n0.original};
     draw();
     var first = document.querySelector('.ed input'); if(first) first.focus();
     return;
   }

   if(canc){ stopEditing(true); draw(); return }

   if(save){
     var n1 = notes.find(function(x){ return x.id === save.dataset.save });
     if(!n1) return;
     try{
       var r3 = await fetch('/api/notes/edit', {method:'POST',
         headers:{'Content-Type':'application/json'},
         body: JSON.stringify({id:n1.id, title:n1.title, concept:n1.concept,
                               next:n1.next, original:n1.original})});
       if(!r3.ok){
         // 404 here is the server refusing to persist a note with nothing
         // left in it. Say that, rather than the status code.
         throw new Error(r3.status === 404
           ? 'a note needs a title, a concept or its original text'
           : 'HTTP ' + r3.status);
       }
       // The server's copy wins — it did the truncating, and the title it
       // derived for a cleared field is the one that is actually stored.
       Object.assign(n1, (await r3.json()).note);
       stopEditing(false); fail(''); draw();
     }catch(err){ fail('not saved — ' + err.message) }   // stays in the editor
     return;
   }

   // A tap inside the editor is aimed at a field, not at the card.
   if(e.target.closest('.ed')) return;

   if(chip){
     // Cycles. Four states, so the worst case is three taps and every one of
     // them is reversible by tapping again — no menu, no undo to design.
     var n = notes.find(function(x){ return x.id === chip.dataset.chip });
     if(!n) return;
     var want = STATUSES[(STATUSES.indexOf(n.status) + 1) % STATUSES.length];
     var was = n.status;
     n.status = want; draw();                    // optimistic: the tap must feel free
     try{
       var r = await fetch('/api/notes/status', {method:'POST',
         headers:{'Content-Type':'application/json'},
         body: JSON.stringify({id: n.id, status: want})});
       if(!r.ok) throw new Error('HTTP ' + r.status);
       fail('');
     }catch(err){ n.status = was; draw(); fail('status not saved — ' + err.message) }
     return;
   }

   if(del){
     var id = del.dataset.del;
     if(!confirm('Delete this note? The original text goes with it.')) return;
     try{
       var r2 = await fetch('/api/notes/delete', {method:'POST',
         headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: id})});
       if(!r2.ok) throw new Error('HTTP ' + r2.status);
       notes = notes.filter(function(x){ return x.id !== id });
       fail(''); draw();
     }catch(err){ fail('not deleted — ' + err.message) }
     return;
   }

   // Collapsing the card you are editing would hide the fields mid-sentence.
   if(card && card.dataset.id === editing) return;
   if(card){ open = (open === card.dataset.id) ? null : card.dataset.id; draw() }
 });

 pull();
 // Trace writes to this file too. Coming back to the tab is the moment a note
 // he added while you were away should appear, and is cheaper than a poll.
 document.addEventListener('visibilitychange', function(){
   // Not while editing. Switching apps mid-sentence — to check a number, to
   // read the transcript again — is normal, and a refresh on the way back
   // would replace what you had typed with what is still on disk.
   if(document.visibilityState === 'visible' && !editing) pull();
 });
</script>
</body></html>"""


CALL_TRACE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>Call Trace</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Call Trace">
<link rel="apple-touch-icon" href="/trace-192.png">
<link rel="icon" href="data:,">
<style>
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#05070a;color:#d6e4ec;font:15px/1.5 ui-monospace,"SF Mono",Menlo,monospace;
   display:flex;flex-direction:column;
   padding:max(14px,env(safe-area-inset-top)) 14px max(14px,env(safe-area-inset-bottom))}
 header{display:flex;align-items:center;gap:10px;padding-bottom:12px;flex:0 0 auto}
 header img{width:34px;height:34px;border-radius:50%;box-shadow:0 0 0 1px #1d2b33}
 header b{color:#4fe3c1;font-weight:400;letter-spacing:.2em;text-transform:uppercase;font-size:14px}
 header .sub{color:#4a5b68;font-size:10px;letter-spacing:.1em;display:block}
 header a{color:#4a5b68;text-decoration:none;font-size:11px;letter-spacing:.14em}
 button.tog{margin-left:auto;height:28px;padding:0 10px;border-radius:9px;background:#101a22;
   color:#4a5b68;font-size:10px;letter-spacing:.12em}
 button.tog.on{background:#1d5a4a;color:#8affd8;box-shadow:0 0 0 1px #2f8f74}
 header .sub.live{color:#7fe6cd}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
 header .sub.busy{animation:pulse 1.3s ease-in-out infinite}
 .m.sp{border-color:#2c6c72}
 #log{flex:1 1 auto;overflow-y:auto;display:flex;flex-direction:column;gap:10px;
   padding:4px 0 12px;-webkit-overflow-scrolling:touch}
 .m{max-width:88%;padding:9px 12px;border-radius:13px;white-space:pre-wrap;
   word-break:break-word;font-size:14px;line-height:1.5}
 .me{align-self:flex-end;background:#123a4a;color:#dff3fb;border-bottom-right-radius:4px}
 .tr{align-self:flex-start;background:#0f1620;color:#cfe0ea;border:1px solid #1b2833;
   border-bottom-left-radius:4px}
 .sys{align-self:center;color:#4a5b68;font-size:11px;letter-spacing:.08em;text-align:center}
 .err{align-self:center;color:#e2766a;font-size:12px;text-align:center}
 form{flex:0 0 auto;display:flex;gap:8px;align-items:flex-end;padding-top:10px;
   border-top:1px solid #131e26}
 textarea{flex:1;background:#0b1219;color:#d6e4ec;border:1px solid #1b2833;border-radius:12px;
   padding:11px 12px;font:15px/1.45 inherit;resize:none;max-height:132px;min-height:44px}
 textarea:focus{outline:none;border-color:#2c6c72}
 button{flex:0 0 auto;height:44px;min-width:44px;border:0;border-radius:12px;
   background:#14313d;color:#7fe6cd;font:13px/1 inherit;letter-spacing:.1em;padding:0 14px}
 button:disabled{opacity:.4}
 button.rec{background:#4a1d1d;color:#ff9c8f}
 .hint{flex:0 0 auto;color:#33424d;font-size:10px;letter-spacing:.08em;text-align:center;
   padding-top:8px}
</style></head><body>
<header>
  <img src="/trace-192.png" alt="">
  <div><b>Call Trace</b><span class="sub" id="stat">ready</span></div>
  __HANDS_FREE_BTN__
  <a href="/phone">close</a>
</header>
<div id="log"><div class="sys">__INTRO__</div></div>
<form id="f">
  <textarea id="t" rows="1" placeholder="message trace" autocomplete="off"></textarea>
  <button type="submit" id="go">SEND</button>
</form>
<div class="hint">__HINT__</div>
<script>
const log=document.getElementById('log'),ta=document.getElementById('t');
const f=document.getElementById('f'),go=document.getElementById('go');
let since=Math.floor(Date.now()/1000),polling=null,rec=null,chunks=[];
const seenNames=new Set();
function add(cls,txt){const d=document.createElement('div');d.className='m '+cls;d.textContent=txt;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
function note(cls,txt){const d=document.createElement('div');d.className=cls;d.textContent=txt;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,132)+'px';});
// Enter sends on a physical keyboard; on a phone the return key inserts a newline.
ta.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey&&window.matchMedia('(min-width:760px)').matches){
    e.preventDefault();f.requestSubmit();}
});
async function poll(){
  try{
    const r=await fetch('/api/trace-replies?since='+since);
    if(!r.ok)return;
    const d=await r.json();
    for(const rep of (d.replies||[])){
      // Same basename in pending and sent — show and speak it exactly once.
      if(seenNames.has(rep.name))continue;
      seenNames.add(rep.name);
      for(const part of (rep.parts||[])){add('tr',part);say(part);}
      if(rep.at>since)since=rep.at;
    }
  }catch(e){}
}
function watch(){if(polling)clearInterval(polling);polling=setInterval(poll,3000);
  setTimeout(()=>{if(polling){clearInterval(polling);polling=null;}},240000);}

// ── voice out ────────────────────────────────────────────────────────
// Replies are spoken through /api/speak (the operator's own cloned voice on
// :8890). Queued and played strictly in order: two overlapping <audio> objects
// is the one thing that makes a walkie-talkie unusable.
// Absent when Grace is retired, which is the default. Every hands-free path
// below is guarded on this one element rather than on a separate flag, so there
// is one fact — the button is there or it is not — instead of two that can
// disagree. A stale wbhf=1 in localStorage must NOT survive the button: it
// would leave handsFree true with no mic and no way to turn it off, and
// epochOk() would start admitting turns that can never arrive.
const hf=document.getElementById('hf');
let handsFree=!!hf&&localStorage.getItem('wbhf')==='1';
let speakQ=[],speaking=false,curAudio=null;   // items: {text, voice}
let micStream=null,ac=null,analyser=null,vadTimer=null,graceHist=[];
let callEpoch=0;

// Every turn is stamped with the epoch it began in. Ending a call bumps the
// epoch, so a transcription or a Grace reply that lands afterwards is dropped
// instead of speaking, re-arming the mic, or routing to Trace after hang-up.
function epochOk(e){return e===callEpoch&&handsFree;}
function paintHF(){if(!hf)return;hf.classList.toggle('on',handsFree);
  hf.textContent=handsFree?'END CALL':'HANDS FREE';}
const stat=document.getElementById('stat');
function setStat(s,busy,live){stat.textContent=s;
  stat.classList.toggle('busy',!!busy);stat.classList.toggle('live',!!live);}

// ── one mic, two jobs ────────────────────────────────────────────────
// The stream stays open for the whole call and a single analyser drives both
// end-of-turn detection and barge-in. Opening a fresh getUserMedia per turn
// costs a permission check and ~300ms of warm-up on Safari, which is audible
// as a clipped first word.
const SPEAK_RMS=0.020;   // above this is speech, not room noise
const MIN_SPEECH_MS=350; // a click or a chair creak is not a turn
const TAIL_MS=600;       // speaker ring-out before the mic is trusted again
let lastSpoken='';

// Normalised compare, used to throw away the mic hearing Grace through the
// speaker. Echo cancellation does not survive a laptop speaker at volume.
function looksLikeEcho(heard){
  const norm=s=>s.toLowerCase().replace(/[^a-z0-9 ]/g,'').replace(/[ ]+/g,' ').trim();
  const a=norm(heard),b=norm(lastSpoken);
  if(!a||!b)return false;
  if(b.includes(a)&&a.length>8)return true;
  const aw=new Set(a.split(' ')),bw=b.split(' ');
  if(!bw.length)return false;
  const hit=bw.filter(w=>aw.has(w)).length/bw.length;
  return hit>0.6;
}
const HANG_MS=1200;      // silence this long ends your turn
const MAX_TURN_MS=30000; // a stuck-open mic must not record forever
function rms(){
  if(!analyser)return 0;
  const buf=new Float32Array(analyser.fftSize);
  analyser.getFloatTimeDomainData(buf);
  let s=0;for(let i=0;i<buf.length;i++)s+=buf[i]*buf[i];
  return Math.sqrt(s/buf.length);
}
async function openMic(){
  if(micStream)return true;
  if(!navigator.mediaDevices){note('err','no microphone on this browser');return false;}
  try{
    micStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
    ac=new (window.AudioContext||window.webkitAudioContext)();
    if(ac.state==='suspended')await ac.resume();
    analyser=ac.createAnalyser();analyser.fftSize=1024;
    ac.createMediaStreamSource(micStream).connect(analyser);
    return true;
  }catch(e){note('err','microphone blocked');return false;}
}
function closeMic(){
  if(vadTimer){clearInterval(vadTimer);vadTimer=null;}
  if(rec&&rec.state==='recording'){discardRec=true;rec.stop();}
  if(micStream){micStream.getTracks().forEach(t=>t.stop());micStream=null;}
  if(ac){try{ac.close();}catch(e){}ac=null;}
  analyser=null;
}
let discardRec=false;
function listen(){
  if(!handsFree||!micStream)return;
  chunks=[];discardRec=false;
  rec=new MediaRecorder(micStream);
  rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data);};
  const turnEpoch=callEpoch;
  rec.onstop=async()=>{
    if(vadTimer){clearInterval(vadTimer);vadTimer=null;}
    if(discardRec){discardRec=false;return;}
    if(!epochOk(turnEpoch))return;          // call ended mid-utterance
    setStat('thinking…',true,true);
    try{
      const r=await fetch('/api/listen',{method:'POST',body:new Blob(chunks,{type:'audio/webm'})});
      const d=await r.json();
      if(!epochOk(turnEpoch))return;
      if(!d.text||looksLikeEcho(d.text)){
        // Empty or the mic hearing Grace. Back off instead of re-arming
        // instantly — a tight retry loop is what made the status flicker
        // between thinking and speaking without anyone saying a word.
        // Nothing usable — wait a beat and listen again. No counter, no
        // self-muting: an assistant that switches itself off mid-conversation
        // is worse than one that waits.
        if(handsFree)setTimeout(()=>{if(handsFree)listen();},600);
        else setStat('ready',false,false);
        return;
      }
      add('me',d.text);
      graceHist.push({role:'user',text:d.text});
      const g=await fetch('/api/grace',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({text:d.text,history:graceHist})});
      const gd=await g.json();
      if(!epochOk(turnEpoch))return;
      if(gd.say){add('tr',gd.say);graceHist.push({role:'grace',text:gd.say});say(gd.say,'grace');}
      // Handed to Trace: his answer arrives through the outbox like any other
      // reply, so start watching for it and it gets spoken when it lands.
      if(gd.handed_off){since=Math.floor(Date.now()/1000)-1;watch();note('sys','handed to trace');}
      else if(!gd.say&&handsFree&&micStream)listen();
    }catch(e){if(!epochOk(turnEpoch))return;
      note('err','grace unreachable');if(handsFree&&micStream)listen();}
  };
  rec.start();
  setStat('listening — speak now',false,true);
  let voicedMs=0,quietSince=0;const t0=Date.now(),TICK=120;
  vadTimer=setInterval(()=>{
    if(!rec||rec.state!=='recording')return;
    const v=rms();
    if(v>SPEAK_RMS){voicedMs+=TICK;quietSince=0;}
    else if(voicedMs>=MIN_SPEECH_MS){
      // Only a turn that actually contained speech can end on silence.
      if(!quietSince)quietSince=Date.now();
      else if(Date.now()-quietSince>HANG_MS){rec.stop();return;}
    }
    if(Date.now()-t0>MAX_TURN_MS){
      // Nothing worth sending — drop it and listen again rather than shipping
      // a minute of room noise to whisper.
      if(voicedMs<MIN_SPEECH_MS)discardRec=true;
      rec.stop();
      if(discardRec&&handsFree)setTimeout(()=>{if(handsFree&&micStream)listen();},200);
    }
  },TICK);
}
if(hf)hf.addEventListener('click',async()=>{
  handsFree=!handsFree;localStorage.setItem('wbhf',handsFree?'1':'0');paintHF();
  if(handsFree){
    if(!(await openMic())){handsFree=false;paintHF();return;}
    callEpoch++;
    note('sys','hands free on — just talk, it hears you stop');
    listen();
  }else{
    callEpoch++;closeMic();speakQ.length=0;
    if(curAudio){curAudio.pause();curAudio=null;}
    setStat('ready',false,false);note('sys','call ended');
  }
});
paintHF();

// Read-only diagnostics. Everything here is closure-scoped, which is correct
// for the app and useless for verifying the mic gate from outside — so this
// exposes the two facts a test (or a confused operator in the console) needs,
// and nothing that can change state.
window.wbCall={
  get inCall(){return handsFree;},
  get micEnabled(){
    if(!micStream)return null;
    const t=micStream.getAudioTracks()[0];
    return t?t.enabled:null;
  },
};
async function drain(){
  if(speaking)return;
  speaking=true;
  // HALF DUPLEX. The loop that made this unusable was Grace hearing herself
  // through the speaker, transcribing it, and answering it. Barge-in is the
  // casualty and that is the right trade: a feedback loop is worse than having
  // to wait your turn. With headphones the echo path does not exist and this
  // costs nothing.
  if(micStream)micStream.getAudioTracks().forEach(t=>{t.enabled=false;});
  while(speakQ.length){
    const item=speakQ.shift();
    lastSpoken=item.text||'';
    setStat(item.voice==='grace'?'grace is speaking':'trace is speaking',false,true);
    try{
      const r=await fetch('/api/speak',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({text:item.text,voice:item.voice||''})});
      if(!r.ok)throw new Error(r.status);
      const url=URL.createObjectURL(await r.blob());
      const a=new Audio(url);curAudio=a;
      await new Promise(res=>{a.onended=res;a.onerror=res;a.play().catch(res);});
      curAudio=null;URL.revokeObjectURL(url);
    }catch(e){}
  }
  speaking=false;
  // Let the speaker stop ringing before trusting the mic again, or the tail of
  // Grace's last word becomes the start of your next turn.
  await new Promise(r=>setTimeout(r,TAIL_MS));
  if(micStream)micStream.getAudioTracks().forEach(t=>{t.enabled=true;});
  if(handsFree&&micStream)listen();
  else setStat('ready',false,false);
}
function say(text,voice){speakQ.push({text:text,voice:voice||''});drain();}
</script></body></html>
"""


CALL_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>Trace</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Trace">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="icon" href="data:,">
<style>
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#05070a;color:#d6e4ec;font:14px/1.55 ui-monospace,"SF Mono",Menlo,monospace;
   display:flex;flex-direction:column;
   padding:max(16px,env(safe-area-inset-top)) 16px max(16px,env(safe-area-inset-bottom))}
 header{display:flex;align-items:center;gap:10px;padding-bottom:10px}
 header b{color:#4fe3c1;font-weight:400;letter-spacing:.2em;text-transform:uppercase;font-size:13px}
 header .sub{color:#4a5b68;font-size:11px;letter-spacing:.1em}
 header a{color:#4a5b68;text-decoration:none;font-size:11px;letter-spacing:.14em}
 button.tog{margin-left:auto;height:28px;padding:0 10px;border-radius:9px;background:#101a22;
   color:#4a5b68;font-size:10px;letter-spacing:.12em}
 button.tog.on{background:#1d5a4a;color:#8affd8;box-shadow:0 0 0 1px #2f8f74}
 header .sub.live{color:#7fe6cd}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
 header .sub.busy{animation:pulse 1.3s ease-in-out infinite}
 .m.sp{border-color:#2c6c72}
 .hide{display:none}

 .dock{border:2px solid #1d5f52;border-radius:14px;background:#0b1a17;padding:11px;
   display:flex;flex-direction:column;gap:9px;margin-bottom:11px}
 .dock.min{padding:7px 11px;flex-direction:row;align-items:center;gap:9px}
 .dock.min #st,.dock.min .ask{display:none}
 .row{display:flex;align-items:center;gap:8px}
 button{font:inherit;cursor:pointer}
 #go{flex:1;min-height:50px;border-radius:10px;border:2px solid #1d5f52;background:#0d221d;
   color:#4fe3c1;font-size:15px;letter-spacing:.2em;text-transform:uppercase}
 #go:active{border-color:#4fe3c1}
 #go[disabled]{opacity:.45}
 .dock.min #go{min-height:32px;font-size:12px;flex:0 0 88px}
 .chip{border:1px solid #1d5f52;background:transparent;color:#4fe3c1;border-radius:8px;
   font-size:11px;letter-spacing:.12em;text-transform:uppercase;padding:7px 10px}
 .chip:active{background:#10241f}
 .chip[disabled]{opacity:.4}
 .chip.rec{background:#3a1220;border-color:#a33;color:#ff8fa3}
 #st{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#4a5b68;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .lamp{width:9px;height:9px;border-radius:50%;background:#2b3a45;flex:0 0 auto}
 .lamp.on{background:#4fe3c1;animation:p 1.4s ease-in-out infinite}
 @keyframes p{0%,100%{opacity:1}50%{opacity:.25}}
 .ask{display:flex;gap:7px}
 .ask input{flex:1;background:#05070a;border:1px solid #1d5f52;border-radius:8px;
   color:#d6e4ec;font:inherit;font-size:13px;padding:9px 10px;min-width:0}
 .ask input::placeholder{color:#2b3a45}

 .tr{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:9px;padding:2px 0 10px}
 .msg{border-left:2px solid #1d5f52;padding:5px 0 5px 11px}
 .msg .who{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#4fe3c1}
 .msg .when{color:#2b3a45;margin-left:7px}
 .msg p{color:#8fa3b0;margin-top:3px;white-space:pre-wrap}
 .msg.you{border-left-color:#2b3a45}
 .msg.you .who{color:#4a5b68}
 .msg .act{margin-top:7px;display:flex;gap:6px;flex-wrap:wrap}
 .empty{color:#2b3a45;font-size:12px;text-align:center;padding:22px 0}

 h2{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#4a5b68;margin:4px 0 7px}
 .fleet{display:flex;flex-wrap:wrap;gap:6px;padding-bottom:6px}
 .ag{border:1px solid #18222b;background:#0a0e13;color:#d6e4ec;border-radius:8px;
   font-size:12px;padding:6px 10px;display:flex;align-items:center;gap:6px}
 .ag:active{border-color:#4fe3c1}
 .ag[disabled]{opacity:.4}
 .ag i{font-style:normal;width:6px;height:6px;border-radius:50%;background:#4fe3c1}
 .ag.q i{background:#2b3a45}
 .ag.nd{border-style:dashed;color:#8fa3b0}
</style></head><body>
 <header><span class="lamp" id="lamp"></span><b>Trace</b>
   <span class="sub" id="who">the fleet steward</span><a href="/phone">&lsaquo; home</a></header>

 <div class="dock" id="dock">
   <div class="row">
     <button id="go">Call</button>
     <button class="chip" id="talk">Talk</button>
     <button class="chip" id="stop" disabled>Stop</button>
     <button class="chip" id="mini">Min</button>
   </div>
   <form class="ask" id="askf" autocomplete="off">
     <input id="q" placeholder="say 'pool: fix the editor' to send it straight there" />
     <button class="chip" type="submit" id="send">Ask</button>
   </form>
   <div id="st">tap call for the fleet briefing</div>
 </div>

 <div id="body">
   <h2>Agents &mdash; tap one and Trace reads that tunnel</h2>
   <div class="fleet" id="fleet"></div>
   <h2>Transcript</h2>
 </div>
 <div class="tr" id="tr"><div class="empty">nothing yet</div></div>

<script>
 var go=D('go'), st=D('st'), tr=D('tr'), dock=D('dock'), lamp=D('lamp'),
     body=D('body'), mini=D('mini'), stop=D('stop'), who=D('who'),
     q=D('q'), askf=D('askf'), send=D('send'), talk=D('talk');
 function D(i){return document.getElementById(i)}
 var audio=new Audio(); audio.preload='auto';

 // iOS refuses play() outside the tap that started it. Awaiting anything first
 // loses the gesture and Safari throws NotAllowedError — "the request is not
 // allowed". So the element is unlocked SYNCHRONOUSLY on the first tap with a
 // silent wav; after that its src can be swapped from an async callback.
 var SILENCE='data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=';
 var unlocked=false;
 function unlock(){ if(unlocked) return;
   audio.src=SILENCE; var p=audio.play(); if(p&&p.catch) p.catch(function(){}); unlocked=true; }

 // ── one call at a time, and it ALWAYS ends ───────────────────────────────
 // The first cut had neither. Tapping a second agent while the first was in
 // flight left both writing to the transcript, and the superseded audio never
 // fired onended — so the Call button stayed disabled forever and the only way
 // out was reloading the page. Every call now takes a ticket, and finish()
 // runs on every exit path including the ones that throw.
 var ticket=0, blobUrl=null;
 function busy(on){
   go.disabled=on; send.disabled=on; stop.disabled=!on;
   if(!recording) talk.disabled=on;
   [].forEach.call(document.querySelectorAll('.ag'),function(b){b.disabled=on});
   lamp.classList.toggle('on',on);
 }
 function finish(msg){ busy(false); st.textContent=msg||'ready'; }
 function halt(){
   ticket++;                       // invalidates anything in flight
   try{audio.pause()}catch(e){}
   if(blobUrl){URL.revokeObjectURL(blobUrl);blobUrl=null}
   finish('stopped');
 }
 stop.onclick=halt;

 function stamp(){var d=new Date();
   return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
 function say(name,text,mine){
   var e=tr.querySelector('.empty'); if(e) e.remove();
   var d=document.createElement('div');
   d.className='msg'+(mine?' you':'');
   d.innerHTML='<div class="who">'+name+'<span class="when">'+stamp()+'</span></div>';
   var p=document.createElement('p'); p.textContent=text; d.appendChild(p);
   tr.appendChild(d); tr.scrollTop=tr.scrollHeight; return d;
 }

 // The unlock plays a zero-length silent wav, which fires onended immediately.
 // Without this guard that reset ran a few milliseconds INTO every call and
 // re-enabled the agent chips, so a second tap could still race the first —
 // the original lock-up wearing a different hat.
 function isSilence(){ return (audio.currentSrc||audio.src||'').indexOf('data:audio/wav')===0 }
 audio.onended=function(){ if(!isSilence()) finish('done'); };
 audio.onerror =function(){ if(!isSilence()) finish('audio failed'); };

 mini.onclick=function(){
   var m=dock.classList.toggle('min');
   body.classList.toggle('hide',m);
   mini.textContent=m?'Max':'Min';
 };

 async function speak(text,mine){
   var mine2=mine;
   st.textContent='synthesising';
   var r=await fetch('/api/speak',{method:'POST',
     headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})});
   if(mine2!==ticket) return;            // superseded while the TTS ran
   if(!r.ok){var j=await r.json().catch(function(){return{}});
     throw new Error(j.error||('tts '+r.status));}
   if(blobUrl) URL.revokeObjectURL(blobUrl);
   blobUrl=URL.createObjectURL(await r.blob());
   audio.src=blobUrl;
   st.textContent='speaking';
   await audio.play();
 }

 async function call(agent){
   unlock();
   var mine=++ticket; busy(true);
   st.textContent=agent?('reading '+agent):'assembling';
   if(agent) say('you','read '+agent,true);
   try{
     var url='/api/brief'+(agent?('?agent='+encodeURIComponent(agent)):'');
     var b=await (await fetch(url)).json();
     if(mine!==ticket) return;
     say('Trace',b.text);
     await speak(b.text,mine);
     if(mine!==ticket) return;
   }catch(e){ if(mine===ticket){ say('Trace','I could not finish that. '+e.message);
     finish('failed'); } }
 }
 go.onclick=function(){call(null)};

 // ── ask / route ──────────────────────────────────────────────────────────
 askf.onsubmit=function(ev){ ev.preventDefault();
   var text=q.value.trim(); if(text){ q.value=''; ask(text); } };

 async function ask(text){
   unlock();
   var mine=++ticket; busy(true);
   say('you',text,true); st.textContent='routing';
   try{
     var r=await (await fetch('/api/route',{method:'POST',
       headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})})).json();
     if(mine!==ticket) return;
     // Named a session? It just goes. That is the router's own policy —
     // explicit beats inferred, and proposing a target you already chose is the
     // system second-guessing you, which is what made the suggestions feel
     // wrong so often. Only a GUESS gets a confirmation step.
     if(r.explicit&&r.target){
       st.textContent='sending to '+r.target;
       var res=await (await fetch('/api/dispatch',{method:'POST',
         headers:{'Content-Type':'application/json'},
         body:JSON.stringify({target:r.target,text:r.text||text})})).json();
       var msg=res.ok
         ? ('Sent to '+r.target+(res.ticket?'. Ticket '+res.ticket+'.':'. No ticket filed.'))
         : ('Could not deliver that. '+res.detail);
       say('Trace',msg);
       await speak(msg,mine);
       return;
     }
     var line = r.target
       ? ('That goes to '+r.target+'. '+(r.why||''))
       : ('I cannot place that. '+(r.why||''));
     var node=say('Trace',line);
     // Trace proposes; the operator decides. Nothing is typed into a pane from
     // here — the iMessage router owns dispatch, and two dispatchers would be
     // two things that can send the same instruction twice.
     var act=document.createElement('div'); act.className='act';
     var t=document.createElement('button'); t.className='chip'; t.textContent='Text me this';
     t.onclick=async function(){
       t.disabled=true; t.textContent='sending';
       var res=await (await fetch('/api/text',{method:'POST',
         headers:{'Content-Type':'application/json'},
         body:JSON.stringify({text:'Trace: '+line+'\\n\\nTask: '+text})})).json();
       t.textContent=res.ok?'texted':'text failed';
     };
     act.appendChild(t);
     // Dispatch is a SEPARATE, explicit tap. The router's own policy is that a
     // model guess proposes and the operator confirms — this button is that
     // confirmation, and it is the only thing on this surface that can type
     // into a pane.
     if(r.target){
       var d=document.createElement('button'); d.className='chip';
       d.textContent='Send to '+r.target;
       d.onclick=async function(){
         d.disabled=true; d.textContent='sending';
         var res=await (await fetch('/api/dispatch',{method:'POST',
           headers:{'Content-Type':'application/json'},
           body:JSON.stringify({target:r.target,text:text})})).json();
         d.textContent=res.ok?('sent'+(res.ticket?' · '+res.ticket:'')):'refused';
         say('Trace', res.ok
           ? ('Delivered to '+r.target+(res.ticket?'. Ticket '+res.ticket+'.':'. No ticket was filed.'))
           : ('I could not deliver that. '+res.detail));
       };
       act.appendChild(d);
     }
     node.appendChild(act);
     await speak(line,mine);
   }catch(e){ if(mine===ticket){ say('Trace','Routing failed. '+e.message); finish('failed'); } }
 };

 // ── voice in ────────────────────────────────────────────────────────────
 // Tap to start, tap to stop. Hold-to-talk loses the recording every time a
 // notification steals the touch, which on a phone is often.
 var rec=null, chunks=[], recording=false;
 talk.onclick=async function(){
   unlock();
   if(recording){ rec && rec.state!=='inactive' && rec.stop(); return; }
   if(!navigator.mediaDevices||!window.MediaRecorder){
     say('Trace','This browser will not give me a microphone.'); return; }
   try{
     var stream=await navigator.mediaDevices.getUserMedia({audio:true});
     rec=new MediaRecorder(stream); chunks=[];
     rec.ondataavailable=function(e){ if(e.data.size) chunks.push(e.data); };
     rec.onstop=async function(){
       stream.getTracks().forEach(function(t){t.stop()});
       recording=false; talk.textContent='Talk'; talk.classList.remove('rec');
       var blob=new Blob(chunks,{type:rec.mimeType||'audio/webm'});
       if(blob.size<1200){ st.textContent='too short'; return; }
       busy(true); st.textContent='transcribing';
       try{
         var r=await (await fetch('/api/listen',{method:'POST',
           headers:{'Content-Type':blob.type},body:blob})).json();
         busy(false);
         if(!r.text){ st.textContent=r.why||'nothing heard'; return; }
         ask(r.text);                    // straight into the same routing path
       }catch(e){ busy(false); st.textContent='transcription failed'; }
     };
     rec.start(); recording=true;
     talk.textContent='Stop rec'; talk.classList.add('rec');
     st.textContent='listening';
   }catch(e){
     say('Trace','Microphone permission was refused, so I cannot hear you.');
     st.textContent='no mic';
   }
 };

 (async function(){
   var f=(await (await fetch('/api/fleet')).json()).fleet||[];
   who.textContent=f.length+' tunnels';
   var el=D('fleet');
   f.forEach(function(a){
     var b=document.createElement('button');
     var quiet=a.idle_secs!=null&&a.idle_secs>6*3600;
     b.className='ag'+(quiet?' q':'')+(a.purpose?'':' nd');
     b.innerHTML='<i></i>';
     b.appendChild(document.createTextNode(a.name));
     b.title=(a.purpose||'no purpose written down')+(a.last_line?('\\n\\n'+a.last_line):'');
     b.onclick=function(){call(a.name)};
     el.appendChild(b);
   });
 })();
</script>
</body></html>"""



# The registry name is what the board shows; a few read better big and short.
PHONE_LABELS = {"chat": "FLEET", "cockpit": "COCKPIT", "graph": "GRAPH",
                "pm": "BOARD", "messages": "MESSAGES", "terminal": "TERMINAL"}


ONBOARD_PHONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__OS__ · Wideband</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__OS__">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="manifest" href="/phone.webmanifest">
<style>__VT__
*{box-sizing:border-box}html,body{margin:0;min-height:100%}
body{background:#0b1016;color:#e0edf4;font:15px/1.5 ui-monospace,"SF Mono",Menlo,monospace;
  padding:max(25px,env(safe-area-inset-top)) 20px max(25px,env(safe-area-inset-bottom))}
main{max-width:620px;margin:0 auto}.eyebrow{color:#65ddf2;font-size:11px;letter-spacing:.24em}
h1{font-size:clamp(34px,11vw,58px);font-weight:500;letter-spacing:-.06em;
  line-height:1.04;margin:15px 0 8px}.intro{color:#8ca7b3;margin:0 0 30px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.card{min-height:146px;padding:17px;border:1px solid #29404a;border-radius:13px;
  color:inherit;text-decoration:none;background:#111b23;display:flex;flex-direction:column;
  justify-content:space-between}
.card:active,.card:focus-visible{border-color:#65ddf2;outline:none;background:#172832}
.card b{font-size:16px;font-weight:500}.card small{color:#8ca7b3;font-size:11px;line-height:1.4}
.card .num{color:#65ddf2;font-size:11px;letter-spacing:.18em}
.card.wide{grid-column:1/-1;min-height:95px;flex-direction:row;align-items:center;gap:12px}
.card.wide div{display:flex;flex-direction:column;gap:5px}
.card.off{opacity:.56;cursor:default}.card.off:active{border-color:#29404a;background:#111b23}
.beta{color:#65ddf2;font-size:10px;letter-spacing:.14em;border:1px solid #327284;
  border-radius:4px;padding:2px 5px}
.foot{margin-top:22px;color:#6f8995;font-size:11px}
.foot a{color:#8ca7b3}
@media(max-width:370px){.grid{gap:9px}.card{padding:13px;min-height:133px}}
@media(prefers-reduced-motion:no-preference){.card{transition:background .18s,border-color .18s}}
</style></head><body><main>
<div class="eyebrow">WIDEBAND / YOUR OS</div>
<h1>__OS__</h1>
<p class="intro">Your agent and first workspace, from your phone.</p>
<div class="grid">
  <a class="card" href="/agent"><span class="num">01 / AGENT</span>
    <b>__AGENT__</b><small>Message your head agent</small></a>
  __PROJECT_KEY__
  <a class="card" href="/graph"><span class="num">03 / GRAPH</span>
    <b>Knowledge graph</b><small>See how your OS is connected</small></a>
  <a class="card" href="/watch"><span class="num">04 / TERMINAL</span>
    <b>Live terminal</b><small>Watch only · no keyboard access</small></a>
  <a class="card wide" href="/notes"><div><span class="num">05 / IDEAS</span>
    <b>Notes <span class="beta">BETA</span></b></div>
    <small>Capture and organize ideas</small></a>
</div>
<div class="foot">Wideband __CONTROL__</div>
</main></body></html>"""

ONBOARD_DETAIL_STYLE = """<style>
*{box-sizing:border-box}html,body{margin:0;min-height:100%}
body{background:#0b1016;color:#e0edf4;font:15px/1.6 ui-monospace,"SF Mono",Menlo,monospace;
  padding:max(26px,env(safe-area-inset-top)) 20px max(26px,env(safe-area-inset-bottom))}
main{max-width:680px;margin:0 auto}.back{color:#8ca7b3;text-decoration:none;font-size:12px}
.eyebrow{color:#65ddf2;font-size:11px;letter-spacing:.2em;margin-top:32px}
h1{font-size:clamp(30px,9vw,48px);font-weight:500;line-height:1.12;margin:11px 0 18px}
p{color:#a1b7c2;margin:0 0 16px}.panel{border:1px solid #29404a;background:#111b23;
  border-radius:12px;padding:19px;margin:18px 0}
.panel b{color:#e0edf4;font-weight:500}.panel small{color:#8ca7b3}
.node-list{list-style:none;padding:0;margin:0;display:grid;gap:10px}
.node-list li{border:1px solid #29404a;border-radius:9px;padding:11px 14px;background:#111b23}
.node-list li span{display:block;color:#65ddf2;font-size:10px;letter-spacing:.16em}
.arrow{color:#65ddf2;text-align:center;font-size:19px;line-height:1}
.button{display:inline-block;color:#0b1016;background:#65ddf2;border-radius:8px;
  padding:10px 14px;text-decoration:none;margin:8px 0}
pre{background:#05090d;border:1px solid #29404a;border-radius:9px;padding:14px;
  min-height:50vh;overflow:auto;white-space:pre;color:#d5e5ec;font:12px/1.45
  ui-monospace,"SF Mono",Menlo,monospace}
.state{color:#8ca7b3;font-size:11px;letter-spacing:.1em}
</style>"""

ONBOARD_AGENT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__AGENT__ · __OS__</title>__STYLE__</head><body><main>
<a class="back" href="/phone">‹ Home</a>
<div class="eyebrow">__OS__ / HEAD AGENT</div><h1>__AGENT__</h1>
<div class="panel"><b>Talk in Messages</b><p>Open the conversation with the separate
Apple Account signed into Messages on this Mac, then send your first text.</p>
<small>Your Apple Account password and verification code stay in Apple's sign-in.</small></div>
<a class="button" href="/watch">Watch __AGENT__ work →</a>
</main></body></html>"""

ONBOARD_GRAPH_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Knowledge graph · __OS__</title>__STYLE__</head><body><main>
<a class="back" href="/phone">‹ Home</a>
<div class="eyebrow">__OS__ / KNOWLEDGE GRAPH</div><h1>Your starter map</h1>
<p>This map shows the setup choices and message path. Add project knowledge as
your agent works.</p>
<ol class="node-list">
 <li><span>OS</span>__OS__</li><li class="arrow" aria-hidden="true">↓</li>
 <li><span>HEAD AGENT</span>__AGENT__</li><li class="arrow" aria-hidden="true">↓</li>
 <li><span>MESSAGING ROUTE</span>Messages listener → binding → session → guarded reply worker</li>
 <li class="arrow" aria-hidden="true">↓</li>
 <li><span>FIRST JOB</span>__GOAL__</li>
</ol>
__FULL_GRAPH__
</main></body></html>"""

ONBOARD_WATCH_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Live terminal · __OS__</title>__STYLE__</head><body><main>
<a class="back" href="/phone">‹ Home</a>
<div class="eyebrow">__OS__ / LIVE TERMINAL</div><h1>Watch __AGENT__</h1>
<p>This view shows the head agent's terminal. It is read only.</p>
<div class="state" id="state" role="status">Connecting…</div>
<pre id="pane" aria-label="Head agent terminal"></pre>
</main><script>
async function refresh(){
 try{
  const r=await fetch('/api/watch',{cache:'no-store'});
  const d=await r.json();
  document.getElementById('state').textContent=d.running?'LIVE · READ ONLY':'SESSION NOT RUNNING';
  document.getElementById('pane').textContent=d.running?d.text:
    'The head agent session is not running yet. Return after setup finishes.';
 }catch(e){document.getElementById('state').textContent='CONNECTION LOST';}
}
refresh();setInterval(refresh,3000);
</script></body></html>"""


def head_session_snapshot(session):
    """Bounded tmux capture with no command, keystroke, or control channel."""
    try:
        names = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=3, check=False)
        if names.returncode or session not in names.stdout.splitlines():
            return None
        captured = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session + ":", "-S", "-120"],
            capture_output=True, text=True, timeout=3, check=False)
        if captured.returncode:
            return None
        return captured.stdout[-32768:]
    except (OSError, subprocess.TimeoutExpired):
        return None

# The moving mark. 235KB of h264, not the 6MB master in the cockpit's public/ —
# this screen is opened on a phone, often on cell data, and a brand button is
# not worth a second of somebody's connection. No webm twin: h264 plays in every
# browser that can reach this page, and the vp9 encode came out twice the size.
#
# `muted` and `playsinline` are both load-bearing on iOS, not decoration —
# without either one the video refuses to autoplay and the mark is a still
# frame. `disablepictureinpicture` keeps a long-press off a menu nobody wants
# for a logo.
MARK_VIDEO = ('<video src="/wb-logo-256.mp4" autoplay muted loop playsinline '
              'preload="auto" disablepictureinpicture aria-hidden="true"></video>')

PHONE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>__MACHINE__</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="__MACHINE__">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon-192.png">
<link rel="manifest" href="/phone.webmanifest">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' fill='%2305070a'/><path d='M6 12h6l4 10 4-16 4 12h2' stroke='%234fe3c1' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<style>__VT__
 *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
 html,body{height:100%}
 body{background:#05070a;color:#d6e4ec;
   font:16px/1.4 ui-monospace,"SF Mono",Menlo,monospace;
   display:flex;flex-direction:column;
   padding:max(18px,env(safe-area-inset-top)) 18px max(18px,env(safe-area-inset-bottom))}
 /* The clock is the point of the screen, so it gets the room. */
 .clock{text-align:center;padding:22px 0 4px}
 .time{font-size:clamp(56px,19vw,104px);line-height:1;letter-spacing:.02em;
   color:#4fe3c1;font-weight:400;font-variant-numeric:tabular-nums}
 .date{margin-top:10px;font-size:13px;letter-spacing:.22em;text-transform:uppercase;color:#4a5b68}
 /* The mark, between the clock and the keys. It is the only moving thing on
    the screen, so it does not need size to be found — at 76px it reads as a
    mark and not as a seventh key.

    `mix-blend-mode:screen` is doing the work that an alpha channel would.
    The loop is bright cyan lines on pure black; screened against the page,
    black goes to nothing and the mark floats with no box, no border and no
    plate under it. The alternative was a transparent encode, which on this
    screen means either 10x the bytes (yuva444p10le) or a mask that clips the
    hexagon's own corners. */
 .mark{display:block;width:76px;height:76px;margin:14px auto 0;padding:0;
   border:0;background:none;line-height:0;transition:transform .18s ease}
 .mark video{width:100%;height:100%;display:block;mix-blend-mode:screen;
   pointer-events:none}
 a.mark:active{transform:scale(.94)}
 a.mark:active video{filter:brightness(1.45)}
 /* Registered but down: the mark stays, because the brand is not a status
    lamp, but it stops pretending to be a link. */
 span.mark video{opacity:.34}
 .grid{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:12px;
   align-content:center;padding:14px 0 18px}
 a.key,span.key{display:flex;flex-direction:column;align-items:center;justify-content:center;
   gap:8px;min-height:104px;border:2px solid #18222b;border-radius:14px;
   background:#0a0e13;color:#d6e4ec;text-decoration:none;
   font-size:15px;letter-spacing:.14em;text-transform:uppercase}
 a.key:active{border-color:#4fe3c1;background:#101820}
 span.key{opacity:.34}
 .key svg{width:30px;height:30px;stroke:#4fe3c1;stroke-width:1.6;fill:none;
   stroke-linecap:round;stroke-linejoin:round}
 span.key svg{stroke:#4a5b68}
 /* Notes is served by this process, not discovered as a registered service, so
    it is not one of the six tiles and should not pretend to be. It spans the
    row beneath them — full width reads as a place rather than a seventh app,
    and it keeps the 2x3 grid the thumb has already learned. */
 a.key.wide{grid-column:1/-1;flex-direction:row;min-height:62px;gap:12px}
 a.key.wide svg{width:22px;height:22px}
 a.key.wide em{font-style:normal;font-size:9.5px;letter-spacing:.14em;
   color:#2f414d;text-transform:uppercase}
 /* Notes and Cashflow share a row. They are the two surfaces this server
    hosts itself rather than links out to, so they belong together, and a
    pair costs 12px of height where a second full-width bar would have cost
    74 — on a screen whose whole job is to fit without scrolling. Shorter
    than a service tile because neither needs a 30px glyph to be recognised
    at this point in the list. */
 a.key.half{min-height:74px;gap:5px;font-size:13px}
 a.key.half svg{width:22px;height:22px}
 a.key.half em{font-style:normal;font-size:8.5px;letter-spacing:.11em;
   color:#2f414d;text-transform:uppercase;line-height:1.3;
   padding:0 4px;text-align:center}
 /* The live map. Same wide key as Notes, with the accent border the CALL key
    uses, because like CALL it is the one that leaves this machine. The dot is
    the only thing on the grid that moves — it says the thing on the other end
    is live without needing a word for it. */
 a.key.net{border-color:#1d5f52;background:#0a1512;color:#4fe3c1}
 a.key.net:active{border-color:#4fe3c1;background:#10241f}
 a.key.net i{width:6px;height:6px;border-radius:50%;background:#4fe3c1;
   flex:none;animation:netpulse 2.6s ease-in-out infinite}
 @keyframes netpulse{0%,100%{opacity:.3}50%{opacity:1}}
 .st{font-size:10px;letter-spacing:.16em;color:#4a5b68}
 /* The call key gets the width and the only colour on the screen, because it
    is the one button that does something rather than going somewhere. */
 a.call{display:flex;align-items:center;justify-content:center;gap:11px;
   min-height:66px;border:2px solid #1d5f52;border-radius:14px;background:#0b1a17;
   color:#4fe3c1;text-decoration:none;font-size:16px;letter-spacing:.2em;
   text-transform:uppercase;margin-bottom:12px}
 a.call:active{border-color:#4fe3c1;background:#10241f}
 a.call em{font-style:normal;font-size:9.5px;letter-spacing:.14em;color:#2f7d6d;
   text-transform:uppercase}
 a.call .face{width:34px;height:34px;border-radius:50%;object-fit:cover;
   border:2px solid rgba(79,227,193,.45);
   animation:tbreathe 2.8s ease-in-out infinite}
 @keyframes tbreathe{0%,100%{box-shadow:0 0 10px rgba(79,227,193,.20)}
   50%{box-shadow:0 0 16px rgba(79,227,193,.40)}}
 .foot{text-align:center;font-size:11px;letter-spacing:.14em;color:#2b3a45;padding-top:4px}
 .foot a{color:#4a5b68;text-decoration:none}
 .foot a:active{color:#4fe3c1}
 /* Lit when this screen already owns `/`, so the sentence reads as a state
    before it reads as a button. */
 .foot a.on{color:#4fe3c1}
 .foot .sep{padding:0 8px;color:#18222b}

 /* Tapping CALL acknowledges here and connects THERE. The cockpit answers
    ?call=1 with the connecting screen — the same figure, the same rings — so
    drawing one here too was two overlays for one action, and the operator has
    to sit through both. This screen's job is now only to say the tap landed:
    the key lights, the sound plays, and the surface that is actually
    connecting is the one that shows connecting. */
 a.call.opening{border-color:#4fe3c1;background:#10241f}
 a.call.opening span{opacity:.55}

 /* ── short screens ────────────────────────────────────────────────────────
    This screen's one job is to be a home screen: everything on it reachable
    without scrolling. It had already stopped being that on a small phone —
    measured at 375x667, the content ran 105px past the fold BEFORE this
    change, which put CALL below it — and adding the live-map key took that to
    179px. A home screen you have to scroll to reach the call button is a
    worse fault than anything it was carrying.

    So: on a short screen everything gives a little. The proportions are
    unchanged and nothing is removed — the clock is still the largest thing on
    the screen and the keys are still comfortably past the 44px minimum.

    Two tiers rather than one, because the shortfall is not the same
    everywhere. A 360x800 Android — the common one, and the device this was
    reported from — is a few dozen px short and only needs a trim. A 375x667
    is far further short and needs the lot. Compacting both by the same amount
    would shrink a phone that had almost enough room for no reason.

    The first breakpoint tracks the content's own full-size height and has to
    move when the screen gains a row: it was 844 for the six tiles plus Notes
    plus the fleet map, and 864 once Cashflow joined Notes on a shared row.
    Set it below the real requirement and a phone lands in the gap and
    scrolls — which is exactly what 393x852 did when this was left at 844. */
 @media(max-height:864px){
   .clock{padding:14px 0 3px}
   .date{margin-top:8px}
   .mark{width:64px;height:64px;margin:10px auto 0}
   .grid{gap:11px;padding:12px 0 14px}
   a.key,span.key{min-height:92px}
   a.key.wide{min-height:56px}
   a.call{min-height:60px}
 }
 @media(max-height:740px){
   body{padding:max(12px,env(safe-area-inset-top)) 14px
         max(14px,env(safe-area-inset-bottom))}
   .clock{padding:8px 0 2px}
   .time{font-size:clamp(44px,15vw,76px)}
   .date{margin-top:6px;font-size:11px;letter-spacing:.18em}
   .mark{width:54px;height:54px;margin:8px auto 0}
   .grid{gap:9px;padding:10px 0 12px}
   a.key,span.key{min-height:74px;gap:6px;font-size:13.5px}
   .key svg{width:24px;height:24px}
   a.key.wide{min-height:50px}
   a.call{min-height:56px;margin-bottom:9px}
   a.call .face{width:30px;height:30px}
   .foot{font-size:10px}
 }
</style></head><body>
 <div class="clock"><div class="time" id="t">--:--</div><div class="date" id="d">&nbsp;</div></div>
 __MARK__
 <div class="grid">__KEYS__</div>
 <a class="call" href="__CALL_HREF__"><img src="/trace-192.png" alt="" class="face"><span>__CALL_LABEL__</span><em>__CALL_SUB__</em></a>
 <div class="foot"><a href="/board">all __N__ services &rsaquo;</a><span class="sep">·</span>__HOME_TOGGLE__</div>

<script>
 function tick(){
   var n=new Date();
   var h=n.getHours(), m=String(n.getMinutes()).padStart(2,'0');
   document.getElementById('t').textContent=h+':'+m;
   document.getElementById('d').textContent=n.toLocaleDateString(undefined,
     {weekday:'long', day:'numeric', month:'long'});
 }
 tick(); setInterval(tick, 10000);


 // ── the tap ──────────────────────────────────────────────────────────────
 // Sound only. It used to be sound only because the page this led to drew the
 // connecting state and two overlays for one action is one too many; it is
 // sound only now because the key hands off to another app entirely, and a
 // connecting screen for a handoff the OS animates itself would be a third.
 //
 // The hold is 160ms: long enough for the key to light and the first blip to
 // land, short enough that it reads as the button responding rather than as a
 // wait. The line-opening sweep is deliberately gone with the overlay — a sound
 // describing a screen that is no longer here was the audio half of the same
 // duplication.
 (function(){
   var call = document.querySelector('a.call');
   if(!call) return;
   var href = call.getAttribute('href');
   var armed = false;

   // Built inside the tap and nowhere else: iOS starts every AudioContext
   // suspended and only resume() inside a user gesture lifts it.
   var AC = window.AudioContext || window.webkitAudioContext, actx = null;
   function blip(f, dur, t, type, peak){
     var o = actx.createOscillator(), g = actx.createGain();
     o.type = type; o.frequency.setValueAtTime(f, t);
     g.gain.setValueAtTime(0.0001, t);
     g.gain.linearRampToValueAtTime(peak, t + 0.012);
     g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
     o.connect(g); g.connect(actx.destination);
     o.start(t); o.stop(t + dur + 0.03);
   }
   function seize(){
     if(!AC) return;
     try{
       if(!actx) actx = new AC();
       if(actx.state === 'suspended') actx.resume();
       var t = actx.currentTime;
       blip(1180, .035, t, 'square', .07);          // the key going down
       blip(523.25, .30, t + .05, 'sine', .075);    // and the line taken
       blip(659.25, .30, t + .05, 'sine', .05);
     }catch(e){}
   }

   // `sms:` is the documented scheme and takes a phone number; every phone
   // honours it. Addressed to an Apple ID it is normal iOS behaviour but not
   // written down anywhere Apple will commit to, so there is a second way
   // through: if we are still here and still focused a beat later, nothing
   // launched, and `imessage://` gets a turn. The guard is both
   // visibilityState AND hasFocus — a scheme that fails on iOS often leaves
   // the page visible behind a "Cannot Open Page" sheet, which visibility
   // alone reads as success.
   function leave(){
     location.href = href;
     if(href.indexOf('sms:') !== 0) return;
     var alt = 'imessage://' + href.slice(4);
     setTimeout(function(){
       if(document.visibilityState === 'visible' && document.hasFocus()){
         location.href = alt;
       }
     }, 1200);
   }

   call.addEventListener('click', function(e){
     e.preventDefault();
     if(armed) return;              // a second tap is not a second call
     armed = true;
     call.classList.add('opening');
     seize();
     setTimeout(leave, 160);
   });

   function disarm(){ armed = false; call.classList.remove('opening'); }

   // Back from a page restores this one from the back-forward cache with the
   // key still lit, on a screen the operator has finished with.
   window.addEventListener('pageshow', function(ev){
     if(ev.persisted) disarm();
   });

   // And this is the one that matters now the key leaves the browser instead
   // of navigating. Messages opens OVER this page; the document never
   // unloads, so coming back fires no pageshow and `armed` would stay true
   // for the life of the tab — one tap, then a dead button forever, which is
   // indistinguishable from the thing this key was changed to fix. Returning
   // to the screen is the signal that the trip is over.
   document.addEventListener('visibilitychange', function(){
     if(document.visibilityState === 'visible') disarm();
   });
 })();
</script>
</body></html>"""


# A refusal is the one failure mode a phone reports as nothing at all. Plain text
# gave the operator a blank-looking screen with no next action; this names the
# cause (wrong network) and the fix, in the portal's own visual language.
DENIED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark"><title>%(machine)s // off-tailnet</title>
<style>
 html,body{margin:0;background:#05070a;color:#8fa3b0;
   font:13px/1.6 ui-monospace,"SF Mono",Menlo,monospace}
 main{max-width:34rem;margin:0 auto;padding:14vh 22px}
 h1{color:#d9a441;font-size:12px;letter-spacing:.22em;text-transform:uppercase;margin:0 0 18px}
 p{margin:0 0 14px}b{color:#d6e4ec;font-weight:600}
 code{color:#4fe3c1}
 .box{border:1px solid #18222b;border-left:2px solid #d9a441;border-radius:3px;padding:14px}
</style></head><body><main>
<h1>off tailnet</h1>
<div class="box">
<p>This device reached %(machine)s from <b>%(ip)s</b> — the local Wi&#8209;Fi, not the
tailnet. The portal only answers over Tailscale.</p>
<p>Open the <b>Tailscale</b> app and switch it on, then load
<code>https://%(host)s:%(port)s</code> again.</p>
</div>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "fleetdeck"
    protocol_version = "HTTP/1.1"
    timeout = 30  # don't let an abandoned keep-alive socket hold a thread forever

    def log_message(self, *a):
        pass  # the LaunchAgent log is for failures, not for every poll

    def handle_one_request(self):
        """Swallow the normal ways a phone ends a connection.

        Mobile Safari opens speculative connections and drops them without a
        close — with HTTP/1.1 keep-alive that surfaces as ConnectionResetError /
        BrokenPipeError, and the stdlib's default is to dump a full traceback and
        kill the thread. The log filled with them. They are routine client
        behaviour, not server faults; close the connection and move on.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
            self.close_connection = True

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _discard_body(self):
        """Read and throw away the request body. Call before ANY early refusal.

        A route that answers a POST without consuming its body leaves those
        bytes sitting in the socket. On a direct connection that is invisible,
        because the client is about to close it anyway — which is why this went
        unnoticed. Through `tailscale serve` it is not invisible at all: the
        proxy keeps the backend connection alive and sends the next request
        down the same one, where it lands on the tail of the last request's
        JSON. The server parses `"history": []}` as a request line and answers
        400, and the failure is attributed to whichever innocent page happened
        to be next.

        Observed exactly that way on 2026-09-25: POST /api/grace (which refuses
        with 410 before reading, deliberately) followed by GET /notes returned
        400 every time over the tailnet and never over loopback.

        When the body is too large to be worth draining, or its length is not
        knowable, the connection is dropped instead. Closing is always correct
        here; reusing a connection whose state you cannot account for is not.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True      # chunked: no safe length to read
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if n <= 0:
            return
        if n > 1024 * 1024:
            self.close_connection = True      # not worth reading to discard
            return
        try:
            self.rfile.read(n)
        except Exception:
            self.close_connection = True

    def _control_refusal(self):
        if len(CONTROL_TOKEN) < 16:
            return self._send(404, "controls are not enabled\n", "text/plain")
        return self._send(401, "operator unlock required\n", "text/plain", {
            "WWW-Authenticate": 'Basic realm="Wideband controls"'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/healthz":
            return self._send(200, "ok\n", "text/plain")

        if not allowed(self.client_address[0]):
            # Logged loudly and on purpose: a refusal is the one failure mode a
            # phone reports only as "it won't load". Without this line there is
            # nothing to diagnose it from.
            ip = self.client_address[0]
            sys.stderr.write(f"DENIED {ip} -> {path}\n")
            sys.stderr.flush()
            body = DENIED_PAGE % {"ip": esc_html(ip), "host": esc_html(HOST),
                                  "machine": esc_html(MACHINE), "port": PORT}
            return self._send(403, body, "text/html; charset=utf-8")

        if onboarding_config() and (
                path in ("/board", "/call", "/call-trace", "/cashflow",
                         "/api", "/api/status", "/api/tunnel", "/api/fleet",
                         "/api/brief", "/api/trace-replies")
                or (path.startswith("/app/") and path != "/app/graph")):
            if not control_authorized(self.headers.get("Authorization")):
                return self._control_refusal()

        # Fullscreen on iPhone is not the Fullscreen API — Safari on iOS refuses
        # requestFullscreen for anything but <video>. The only real fullscreen
        # there is Add to Home Screen, which needs a manifest and a PNG icon
        # (iOS ignores SVG for apple-touch-icon). Hence these two routes. They
        # sit behind the tailnet check like everything else.
        if path == "/api/tunnel":
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            name = (q.get("name") or [""])[0]
            try:
                lines = max(10, min(200, int((q.get("lines") or ["60"])[0])))
            except ValueError:
                lines = 60
            if not name:
                return self._send(400, json.dumps({"error": "name is required"}),
                                  "application/json")
            out = tunnel_context(name, lines)
            return self._send(200 if out.get("found") else 404,
                              json.dumps(out), "application/json")

        if path == "/api/fleet":
            return self._send(200, json.dumps({"fleet": fleet_state()}),
                              "application/json")

        if path == "/api/brief":
            # ?agent=<session> narrows the briefing to one tunnel. The session
            # name is checked against the LIVE list inside agent_report, so an
            # arbitrary string cannot reach tmux.
            q = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
            who = (q.get("agent") or [""])[0]
            text = agent_report(who) if who else brief_text()
            return self._send(200, json.dumps({"text": text, "agent": who or None}),
                              "application/json")

        if path == "/call-trace":
            # The user-facing Call Trace surface. Same pipeline as a text
            # message: what it POSTs to /api/dispatch reaches
            # router().deliver(), which is the exact call imsg-chatbind makes
            # when a text arrives. Nothing new can be reached from here.
            #
            # The spoken half is retired (GRACE_ENABLED, default off), so the
            # HANDS FREE button is not rendered at all rather than rendered
            # disabled. A control that is present and does nothing is the
            # failure this page already had once, when its label promised full
            # duplex against a push-to-talk target.
            page = CALL_TRACE_PAGE.replace("__TRACE_SESSION__", esc_html(TRACE_SESSION))
            if GRACE_ENABLED:
                page = (page
                        .replace("__HANDS_FREE_BTN__",
                                 '<button type="button" id="hf" class="tog" '
                                 'title="hands free">HANDS FREE</button>')
                        .replace("__INTRO__",
                                 "Type to reach Trace directly — replies also arrive on "
                                 "your phone. Press HANDS FREE to talk to Grace, who "
                                 "answers instantly and hands real work to Trace.")
                        .replace("__HINT__",
                                 "HANDS FREE talks to Grace &middot; typing goes "
                                 "straight to Trace"))
            else:
                page = (page
                        .replace("__HANDS_FREE_BTN__", "")
                        .replace("__INTRO__",
                                 "Type to reach Trace — replies also arrive on your "
                                 "phone. Talking to him is retired; send a voice note "
                                 "in Messages instead.")
                        .replace("__HINT__", "typing goes straight to Trace"))
            return self._send(200, page, "text/html; charset=utf-8")

        if path == "/api/trace-replies":
            # READ-ONLY. This lists what the outbox worker has ALREADY sent, by
            # stat-ing ~/trace/outbox/sent. It sends nothing and can send
            # nothing: com.wideband.trace-outbox remains the single outbound
            # path for iMessage, and this endpoint only mirrors its result back
            # into the browser so the operator can read a reply where he asked
            # the question. Deliberately NOT reading the pending outbox dir —
            # an unsent file is not a reply yet, and showing it would claim a
            # delivery that has not happened.
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                since = float((q.get("since") or ["0"])[0])
            except (TypeError, ValueError):
                since = 0.0
            # Both directories, pending FIRST. A reply is written to the outbox
            # and moves to sent/ when the worker confirms delivery, so scanning
            # only sent/ costs up to a full worker tick before it can be spoken.
            # Reading pending lets the voice loop answer as soon as Trace has
            # actually written the words.
            #
            # The same file therefore appears twice over its life, under the
            # SAME basename. That name is the dedup key here and in the browser,
            # so a reply is never shown or spoken twice, and `pending` tells the
            # caller whether delivery to the phone is confirmed yet.
            seen = {}
            for d, pending in ((TRACE_OUT, True), (TRACE_SENT, False)):
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for fn in names:
                    if not fn.endswith(".txt") or fn.startswith("undelivered-"):
                        continue
                    if fn in seen:
                        continue
                    fp = os.path.join(d, fn)
                    if not os.path.isfile(fp):
                        continue
                    try:
                        mt = os.path.getmtime(fp)
                        if mt <= since:
                            continue
                        with open(fp, encoding="utf-8", errors="replace") as fh:
                            seen[fn] = (mt, fh.read(), pending)
                    except OSError:
                        continue
            out = []
            for fn, (mt, body, pending) in seen.items():
                # Same split the worker uses: a line that is exactly three
                # dashes is a message break, so the browser shows the same
                # number of bubbles that arrived as separate texts.
                parts = []
                cur = []
                for line in body.splitlines():
                    if line.strip() == "---":
                        parts.append("\n".join(cur).strip())
                        cur = []
                    else:
                        cur.append(line)
                parts.append("\n".join(cur).strip())
                parts = [p for p in parts if p]
                if parts:
                    out.append({"at": mt, "name": fn, "parts": parts,
                                "pending": pending})
            out.sort(key=lambda r: r["at"])
            return self._send(200, json.dumps({"replies": out[-12:]}),
                              "application/json")

        if path == "/call":
            return self._send(200, CALL_PAGE, "text/html; charset=utf-8")

        if path == "/phone.webmanifest":
            installed_name = (onboarding_config() or {}).get("os_name") or MACHINE
            return self._send(200, json.dumps({
                "name": installed_name, "short_name": installed_name,
                "start_url": "/phone", "scope": "/",
                "display": "fullscreen",
                "display_override": ["fullscreen", "standalone", "minimal-ui"],
                "background_color": "#05070a", "theme_color": "#05070a",
                "orientation": "portrait",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            }), "application/manifest+json")

        # `/home?ui=…` is the only thing that writes the preference. It answers
        # with a redirect to the surface it just selected, so choosing and
        # arriving are one tap and there is no state to disbelieve.
        if path == "/home":
            q = {}
            if "?" in self.path:
                from urllib.parse import parse_qs
                q = parse_qs(self.path.split("?", 1)[1])
            want = (q.get("ui") or [""])[0]
            if want not in HOME_CHOICES:
                return self._send(400, "ui must be board or simple\n", "text/plain")
            dest = "/phone" if want == "simple" else "/board"
            cookie = ("%s=%s; Path=/; Max-Age=%d; SameSite=Lax"
                      % (HOME_COOKIE, want, HOME_MAX_AGE))
            # 303: this was a GET that changed something, and the browser should
            # not re-issue it when the operator hits back.
            return self._send(303, "", "text/plain",
                              {"Location": dest, "Set-Cookie": cookie})

        if path in ("/phone", "/"):
            if path == "/" and onboarding_config():
                return self._phone()
            if path == "/" and home_pref(self.headers.get("Cookie")) != "simple":
                return self._board()
            return self._phone()

        # The installer-backed customer surfaces never expose chat_server's
        # writable tmux endpoints. /watch captures one configured head session
        # and serves text; there is no control request in this process.
        if onboarding_config() and path in ("/agent", "/graph", "/watch"):
            return self._onboard_detail(path)
        if onboarding_config() and path == "/api/watch":
            session = onboarding_config()["head_session"]
            snapshot = head_session_snapshot(session)
            return self._send(200, json.dumps({
                "running": snapshot is not None,
                "text": snapshot or "",
            }), "application/json")

        # Notes. Same origin, same `allowed()` gate as everything else on this
        # port — there is no second stack here and nothing new is listening.
        if path == "/notes":
            page = (NOTES_PAGE
                    .replace("__VT__", VIEW_TRANSITION_CSS)
                    .replace("__MACHINE__", esc_html(MACHINE))
                    .replace("__STATUSES__", json.dumps(list(NOTE_STATUSES))))
            return self._send(200, page, "text/html; charset=utf-8")

        if path == "/api/notes":
            return self._send(200, json.dumps({"notes": notes_all()}),
                              "application/json")

        # A registered service, framed so the shell never leaves this origin.
        # The id is looked up in SHELLED_APPS before anything else happens, so
        # the only URLs reachable here are ones the registry resolved for ids
        # this file names — the request cannot supply one.
        if path.startswith("/app/"):
            app_id = path[len("/app/"):]
            label = SHELLED_APPS.get(app_id)
            if label:
                # Registry-resolved, like every other link on these surfaces,
                # so it cannot rot when a port or the tailnet name moves — and
                # liveness comes free with the lookup.
                svc = {s["id"]: s
                       for s in cached_scan().get("services", [])}.get(app_id)
                if not svc or not svc.get("linkable"):
                    return self._send(200, APP_SHELL_DOWN.replace(
                        "__LABEL__", esc_html(label)), "text/html; charset=utf-8")
                url = svc["url"]
            elif app_id in SHELLED_STATIC:
                label, url = SHELLED_STATIC[app_id]
            else:
                return self._send(404, "not framed here\n", "text/plain")
            page = (APP_SHELL_PAGE
                    .replace("__VT__", VIEW_TRANSITION_CSS)
                    # The host, not the app's own name. Every framed service
                    # draws its own header immediately below this bar, so
                    # naming it here reads as a stutter — "MESSAGES · LIVE"
                    # sitting on top of "MESSAGES". The machine is the thing
                    # the bar can say that the framed app cannot, and it
                    # matches how the board header reads.
                    .replace("__BAR__", fleet_bar("%s · live" % MACHINE))
                    .replace("__URL__", esc_html(url))
                    .replace("__ALLOW__", esc_html(
                        SHELL_ALLOW.get(app_id, SHELL_ALLOW_DEFAULT)))
                    .replace("__LABEL__", esc_html(label))
                    .replace("__MACHINE__", esc_html(MACHINE)))
            return self._send(200, page, "text/html; charset=utf-8")

        # The accountant's cash view. Same origin, same tailnet gate as the
        # rest of this port — which matters more here than anywhere else on
        # the board, because this is the one surface that is somebody's actual
        # money. It is never reachable from outside the tailnet for the same
        # reason nothing else here is: the check above this one.
        if path == "/cashflow":
            try:
                with open(CASHFLOW_PATH, "r", encoding="utf-8") as fh:
                    page = fh.read()
            except FileNotFoundError:
                return self._send(200, CASHFLOW_MISSING.replace(
                    "__PATH__", esc_html(CASHFLOW_PATH)), "text/html; charset=utf-8")
            except Exception as e:
                sys.stderr.write("fleetdeck: cashflow unreadable (%s)\n" % e)
                return self._send(200, CASHFLOW_MISSING.replace(
                    "__PATH__", esc_html("%s — %s" % (CASHFLOW_PATH, e))),
                    "text/html; charset=utf-8")
            # Inserted directly after the <body> tag so the bar is the first
            # thing in flow and the page starts below it. Falls back to
            # prepending if there is no <body> — a fragment still renders, and
            # a cash view with no way back is the one outcome to avoid.
            # The agent's file is taken as found; nothing in it is rewritten.
            # The transition rule goes in here too, because a cross-document
            # transition needs BOTH documents to opt in — without this, every
            # other surface would glide and this one would hard-cut, which
            # reads as this page being the broken one.
            bar = ("<style>%s</style>" % VIEW_TRANSITION_CSS
                   + fleet_bar("cashflow · accountant"))
            m = re.search(r"<body\b[^>]*>", page, re.I)
            page = (page[:m.end()] + bar + page[m.end():]) if m else bar + page
            return self._send(200, page, "text/html; charset=utf-8")

        if path == "/board":
            return self._board()

        if path == "/manifest.webmanifest":
            return self._send(200, json.dumps({
                "name": f"{BRAND} // {MACHINE}",
                "short_name": MACHINE,
                "start_url": "/",
                "scope": "/",
                # Ask for the whole screen and let the platform climb down.
                # Android honours `fullscreen` and drops the status bar
                # entirely; iOS ignores it and gives standalone, which with
                # black-translucent below is already its ceiling. Listing the
                # fallbacks explicitly means a browser that supports neither
                # lands on minimal-ui rather than a browser tab.
                "display": "fullscreen",
                "display_override": ["fullscreen", "standalone", "minimal-ui"],
                "background_color": "#05070a",
                "theme_color": "#05070a",
                "orientation": "portrait",
                "icons": [
                    {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {"src": "icon-512.png", "sizes": "512x512", "type": "image/png",
                     "purpose": "maskable"},
                ],
            }), "application/manifest+json")

        # Trace's likeness, lifted from zaydr so he is one character across both
        # products rather than two things sharing a name.
        if path.startswith("/trace-") and path.endswith(".png"):
            icon = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        if path.startswith("/icon-") and path.endswith(".png"):
            icon = os.path.join(HERE, "assets", os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        # The moving mark on the front screen. Read whole and handed over in one
        # piece: 235KB is under the size where range requests start to matter,
        # and this server does not speak them — a partial-content request would
        # get the whole file with a 200, which every browser accepts for a file
        # this small but which would stall a large one mid-scrub.
        if path == "/wb-logo-256.mp4":
            vid = os.path.join(HERE, "assets", "wb-logo-256.mp4")
            if os.path.exists(vid):
                with open(vid, "rb") as fh:
                    return self._send(200, fh.read(), "video/mp4")
            return self._send(404, "no mark\n", "text/plain")

        # One PNG per service, written by make-icons.py off the same glyph
        # library the board draws. Apps that can serve their own static files
        # should be given a copy (see `icon_dest` in services.json) so their
        # icon does not depend on this process; this route is the master, for
        # eyeballing the output and for surfaces that cannot host a file of
        # their own. basename() is the traversal guard — nothing outside
        # icons/ is reachable.
        if path.startswith("/icons/") and path.endswith(".png"):
            icon = os.path.join(ICONS, os.path.basename(path))
            if os.path.exists(icon):
                with open(icon, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, "no icon\n", "text/plain")

        if path in ("/api/status", "/api"):
            return self._send(200, json.dumps(cached_scan()), "application/json")

        self._send(404, "not here\n", "text/plain")

    # ── the two front doors ──────────────────────────────────────────────────
    # Both are reachable by a canonical path that always renders them, and `/`
    # picks between them from the cookie. Keeping them as methods rather than
    # branches in do_GET is what lets `/` do that without duplicating either.

    def _board(self):
        simple_is_home = home_pref(self.headers.get("Cookie")) == "simple"
        data = json.dumps(cached_scan()).replace("</", "<\\/")
        glyphs = json.dumps(glyph_map(), separators=(",", ":")).replace("</", "<\\/")
        page = (PAGE
                .replace("__VT__", VIEW_TRANSITION_CSS)
                .replace("__BRAND__", esc_html(BRAND))
                .replace("__MACHINE__", esc_html(MACHINE))
                .replace("__SIMPLE_CLASS__", "on" if simple_is_home else "")
                .replace("__SIMPLE_HREF__",
                         "/home?ui=board" if simple_is_home else "/home?ui=simple")
                .replace("__SIMPLE_TITLE__",
                         "Simple screen is this device's home — tap for the board"
                         if simple_is_home else "Switch to the simple screen")
                .replace("__NETMAP__", esc_html(NETMAP_URL))
                .replace("__CASHFLOW_LABEL__", esc_html(CASHFLOW_LABEL))
                .replace("__GLYPHS__", glyphs)
                .replace("window.__DATA__", f"JSON.parse({json.dumps(data)})"))
        return self._send(200, page, "text/html; charset=utf-8")

    def _phone(self):
        if onboarding_config():
            return self._onboard_phone()
        simple_is_home = home_pref(self.headers.get("Cookie")) == "simple"
        scan_now = cached_scan()
        by_id = {s["id"]: s for s in scan_now.get("services", [])}
        glyphs = glyph_map()
        keys = []
        for app_id in PHONE_APPS:
            s = by_id.get(app_id)
            if not s:
                # Named on the front screen and absent from the registry is an
                # editing mistake, not a state. Say so rather than quietly
                # rendering five buttons where six were asked for.
                keys.append(
                    '<span class="key"><span class="st">%s not registered</span></span>'
                    % esc_html(app_id))
                continue
            label = PHONE_LABELS.get(app_id, s.get("name") or app_id)
            icon = glyphs.get(s.get("icon"), glyphs.get("server", FALLBACK_GLYPH))
            svg = '<svg viewBox="0 0 24 24" aria-hidden="true">%s</svg>' % icon
            if s.get("linkable"):
                # A framed app is reached through this origin so the installed
                # app keeps its fullscreen; everything else still links
                # straight at the service, which is what it has always done.
                href = "/app/%s" % app_id if app_id in SHELLED_APPS else s["url"]
                keys.append('<a class="key" href="%s">%s<span>%s</span></a>'
                            % (esc_html(href), svg, esc_html(label)))
            else:
                why = "down" if not s.get("up") else "no browser UI"
                keys.append('<span class="key">%s<span>%s</span>'
                            '<span class="st">%s</span></span>'
                            % (svg, esc_html(label), why))
        # Notes closes the grid. It is not resolved from the registry like the
        # six above it because it is not a service — it is a route on this
        # process, so if this page rendered, it is up. There is no down branch
        # to write and no liveness to check.
        keys.append('<a class="key half" href="/notes">%s<span>Notes β</span>'
                    '<em>%s</em></a>'
                    % ('<svg viewBox="0 0 24 24" aria-hidden="true">%s</svg>'
                       % glyphs.get("script", FALLBACK_GLYPH),
                       esc_html(notes_summary())))

        # Cashflow sits beside Notes for the same reason Notes needs no
        # liveness check: both are routes on this process. The sub-label names
        # the session that owns the numbers rather than quoting one of them —
        # a figure here would be a second place for the balance to be wrong,
        # and this server does not read the ledger, it serves the page.
        keys.append('<a class="key half" href="/cashflow">%s<span>%s</span>'
                    '<em>accountant</em></a>'
                    % ('<svg viewBox="0 0 24 24" aria-hidden="true">%s</svg>'
                       % glyphs.get("pulse", FALLBACK_GLYPH),
                       esc_html(CASHFLOW_LABEL)))

        # The live map closes the grid, below Notes. It is on this screen as
        # well as in the board header because the board header is not reachable
        # from a phone without going through the board first: on a device whose
        # home is the simple screen, a control that exists only in the desk
        # header may as well not exist. New tab for the same reason as there.
        # Through the shell and in the same tab, where the board header still
        # links straight at it in a new one. Not an inconsistency: the desk has
        # an address bar and a board worth keeping open behind the map, and the
        # phone has neither. This was the last key that broke the installed app
        # out of fullscreen.
        keys.append('<a class="key wide net" href="/app/netmap">'
                    '<i></i><span>%s</span></a>' % esc_html(NETMAP_LABEL))

        # The mark under the clock goes to the platform itself — the one link on
        # this screen that is the product rather than a tool for running it.
        # Resolved from the registry like every other link here, so it cannot
        # rot when the port or the tailnet name moves. If the platform is down
        # the mark stays and stops being a link, rather than becoming a button
        # that goes nowhere: a logo is not a status lamp.
        app = by_id.get("app")
        if app and app.get("linkable"):
            mark = ('<a class="mark" href="%s" aria-label="%s">%s</a>'
                    % (esc_html(app["url"]),
                       esc_html(app.get("name") or "Wideband"), MARK_VIDEO))
        else:
            mark = '<span class="mark">%s</span>' % MARK_VIDEO

        href, label, sub = call_destination(by_id)
        toggle = ('<a class="on" href="/home?ui=board">unset as home</a>'
                  if simple_is_home
                  else '<a href="/home?ui=simple">set as home</a>')
        page = (PHONE_PAGE
                .replace("__VT__", VIEW_TRANSITION_CSS)
                .replace("__MACHINE__", esc_html(MACHINE))
                .replace("__KEYS__", "".join(keys))
                .replace("__MARK__", mark)
                .replace("__CALL_HREF__", esc_html(href))
                .replace("__CALL_LABEL__", esc_html(label))
                .replace("__CALL_SUB__", esc_html(sub))
                .replace("__HOME_TOGGLE__", toggle)
                .replace("__N__", str(len(scan_now.get("services", [])))))
        return self._send(200, page, "text/html; charset=utf-8")

    def _onboard_phone(self):
        info = onboarding_config()
        goal = {"research": "Research", "website": "Build a website",
                "proposal": "Create a proposal"}[info["first_goal"]]
        project_state = first_goal_status(info)
        service = {s["id"]: s for s in cached_scan().get("services", [])}.get(
            "first-project")
        # A down service wins over a stale URL elsewhere. An up, loopback-only
        # service can still have a verified phone_url in the goal status when
        # Tailscale Serve succeeded but the scanner has not resolved that map.
        if service:
            if (project_state["identity_match"]
                    and project_state["status"] == "ready"
                    and service.get("up", service.get("linkable", False))):
                project_url = (_phone_url(service.get("url"))
                               if service.get("linkable") else "")
                project_url = project_url or project_state["phone_url"]
            else:
                project_url = ""
        else:
            project_url = (project_state["phone_url"] or
                           info["first_project_url"])
        if project_url:
            project = ('<a class="card" href="%s"><span class="num">'
                       '02 / FIRST PROJECT</span><b>%s</b>'
                       '<small>Open your first project</small></a>'
                       % (esc_html(project_url), esc_html(goal)))
        else:
            pending = ("Running on this Mac · phone link pending"
                       if project_state["local_only"] else
                       "Your project link will appear when it is ready")
            project = ('<div class="card off" aria-disabled="true">'
                       '<span class="num">02 / FIRST PROJECT</span><b>%s</b>'
                       '<small>%s</small></div>'
                       % (esc_html(goal), esc_html(pending)))
        page = (ONBOARD_PHONE_PAGE
                .replace("__VT__", VIEW_TRANSITION_CSS)
                .replace("__OS__", esc_html(info["os_name"]))
                .replace("__AGENT__", esc_html(info["agent_name"]))
                .replace("__PROJECT_KEY__", project)
                .replace("__CONTROL__", (
                    '· <a href="/board">Operator controls</a>'
                    if len(CONTROL_TOKEN) >= 16 else "")))
        return self._send(200, page, "text/html; charset=utf-8")

    def _onboard_detail(self, path):
        info = onboarding_config()
        goal = {"research": "Research", "website": "Build a website",
                "proposal": "Create a proposal"}[info["first_goal"]]
        if path == "/agent":
            page = ONBOARD_AGENT_PAGE
        elif path == "/watch":
            page = ONBOARD_WATCH_PAGE
        else:
            graph = {s["id"]: s for s in cached_scan().get("services", [])}.get("graph")
            full = ('<p><a class="button" href="/app/graph">Open full graph →</a></p>'
                    if graph and graph.get("linkable") else "")
            page = ONBOARD_GRAPH_PAGE.replace("__FULL_GRAPH__", full)
        page = (page.replace("__STYLE__", ONBOARD_DETAIL_STYLE)
                .replace("__OS__", esc_html(info["os_name"]))
                .replace("__AGENT__", esc_html(info["agent_name"]))
                .replace("__GOAL__", esc_html(goal)))
        return self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        """The one mutating route. Off unless agents.actions is switched on —
        see AGENT_ACTIONS. Even then it can only touch labels the scan already
        returned, so a caller cannot name an arbitrary launchd job."""
        path = self.path.split("?")[0].rstrip("/") or "/"
        if not allowed(self.client_address[0]):
            self._discard_body()
            return self._send(403, "no\n", "text/plain")
        if onboarding_config() and path not in (
                "/api/notes", "/api/notes/status", "/api/notes/edit",
                "/api/notes/delete"):
            self._discard_body()
            if not control_authorized(self.headers.get("Authorization")):
                return self._control_refusal()

        # Speech is a POST because the text can be long, and a proxy rather than
        # a link because :8890 is not on the tailnet — the phone would have
        # nothing to fetch. Nothing here is stored: text in, audio out.
        if path == "/api/listen":
            # Raw audio body, not JSON — a base64 round trip of a voice clip
            # doubles the payload for nothing.
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 8 * 1024 * 1024:
                    self._discard_body()
                    return self._send(400, json.dumps({"error": "empty or oversized clip"}),
                                      "application/json")
                blob = self.rfile.read(n)
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            text, why = transcribe(blob)
            if not text:
                return self._send(200, json.dumps({"text": None, "why": why or "nothing heard"}),
                                  "application/json")
            return self._send(200, json.dumps({"text": text}), "application/json")

        # ── notes ────────────────────────────────────────────────────────────
        #
        # NOT behind AGENT_ACTIONS. That flag guards the routes that can touch
        # launchd jobs on this machine, and the blast radius there is the Mac
        # itself. These three write rows to one JSON file in the operator's
        # home directory, take no path, name no process, and run nothing —
        # every value that reaches disk has been through notes_normalise(),
        # which reads five keys and drops the rest. Putting them behind the
        # same flag would mean either leaving it off and having no notes, or
        # switching it on and widening the surface that actually matters.
        #
        # This is the write path Trace inherits. He posts the organised shape
        # to /api/notes — title, concept, next, original, status — and gets the
        # same validation as the phone form, because it is the same function.
        # Nothing about iMessage routing changes to make that work; when the
        # time comes it is an HTTP call to a port he can already reach.
        if path in ("/api/notes", "/api/notes/status", "/api/notes/edit",
                    "/api/notes/delete"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                # A body cap before the parse, not after: the limits in
                # notes_normalise() protect the file, and this protects the
                # process from being asked to hold 200MB of JSON first.
                if n > 256 * 1024:
                    self._discard_body()
                    return self._send(413, json.dumps({"error": "note too large"}),
                                      "application/json")
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            if not isinstance(body, dict):
                return self._send(400, json.dumps({"error": "expected an object"}),
                                  "application/json")

            if path == "/api/notes":
                # Two shapes, one door. `text` is the phone's single textarea
                # and gets the labelled-line parse; the explicit fields are the
                # organised shape. Both end in notes_normalise(), so neither
                # can write a key the other could not.
                if isinstance(body.get("text"), str):
                    payload = note_from_text(body["text"])
                    payload["status"] = body.get("status")
                else:
                    payload = body
                if not (payload.get("text") or payload.get("original")
                        or payload.get("title") or payload.get("concept")):
                    return self._send(400, json.dumps({"error": "empty note"}),
                                      "application/json")
                return self._send(200, json.dumps({"note": notes_add(payload)}),
                                  "application/json")

            if path == "/api/notes/edit":
                note = notes_update((body.get("id") or "").strip(), body)
                if not note:
                    return self._send(404, json.dumps(
                        {"error": "no such note, or the edit would empty it"}),
                        "application/json")
                return self._send(200, json.dumps({"note": note}), "application/json")

            if path == "/api/notes/status":
                note = notes_set_status((body.get("id") or "").strip(),
                                        body.get("status"))
                if not note:
                    return self._send(404, json.dumps(
                        {"error": "no such note, or status not one of %s"
                                  % ", ".join(NOTE_STATUSES)}), "application/json")
                return self._send(200, json.dumps({"note": note}), "application/json")

            ok = notes_delete((body.get("id") or "").strip())
            return self._send(200 if ok else 404,
                              json.dumps({"deleted": ok}), "application/json")

        if path == "/api/dispatch":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            target = (body.get("target") or "").strip()
            instruction = (body.get("text") or "").strip()
            if not target or not instruction:
                return self._send(400, json.dumps({"error": "target and text are required"}),
                                  "application/json")
            # The target is checked against the LIVE session list, so a crafted
            # body cannot name a session that is not running.
            if target not in {f["name"] for f in fleet_state()}:
                return self._send(404, json.dumps(
                    {"ok": False, "detail": f"'{target}' is not a live session"}),
                    "application/json")
            out = dispatch(target, instruction[:1500])
            return self._send(200 if out["ok"] else 502, json.dumps(out),
                              "application/json")

        if path in ("/api/route", "/api/text"):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "no text"}),
                                  "application/json")
            if path == "/api/route":
                named, body = explicit_target(text[:800])
                if named:
                    # No model call at all. You said where it goes.
                    return self._send(200, json.dumps({
                        "target": named, "text": body, "explicit": True,
                        "why": "You named it.",
                    }), "application/json")
                out = route_task(text[:800])
                out["explicit"] = False
                out["text"] = body
                return self._send(200, json.dumps(out), "application/json")
            ok, detail = send_text(text)
            return self._send(200 if ok else 502,
                              json.dumps({"ok": ok, "detail": detail}),
                              "application/json")

        if path == "/api/grace":
            # Grace answers by voice. She reads the fleet and hands real work to
            # Trace; she has no outbound path of her own, so nothing she decides
            # can reach anyone. Escalation goes through the SAME dispatch() the
            # text surface uses, which means the same live-session check and the
            # same refusal to type into a bare shell.
            #
            # DEPRECATED 2026-09-16. Refused before the body is read, so a tab
            # left open on the old page — or a phone that cached it — stops here
            # instead of loading a 27B model to answer a conversation nobody is
            # having. 410 rather than 404: the endpoint is retired, not missing.
            if not GRACE_ENABLED:
                # Refused without being read, but NOT without being drained —
                # see _discard_body(). Skipping the drain here is what made an
                # unrelated GET answer 400 through the tailnet proxy.
                self._discard_body()
                return self._send(410, json.dumps(
                    {"error": "Grace is retired. Send Trace an iMessage voice note."}),
                    "application/json")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "no text"}),
                                  "application/json")
            hist = body.get("history") or []
            if not isinstance(hist, list):
                hist = []
            try:
                out = grace_turn(text, hist)
            except Exception as exc:
                return self._send(502, json.dumps(
                    {"error": f"grace unavailable: {type(exc).__name__}"}),
                    "application/json")
            return self._send(200, json.dumps(out), "application/json")

        if path == "/api/speak":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                _b = json.loads(self.rfile.read(n) or b"{}")
                text = (_b.get("text") or "").strip()
                _voice = (_b.get("voice") or "").strip().lower()[:24]
            except Exception:
                return self._send(400, json.dumps({"error": "bad body"}),
                                  "application/json")
            if not text:
                return self._send(400, json.dumps({"error": "no text"}),
                                  "application/json")
            # Bounded on purpose. TTS runs about three and a half seconds a
            # sentence on this box, so an unbounded body is an unbounded job.
            text = text[:SPEAK_MAX]
            # `voice` selects a named reference on the TTS side (grace -> a
            # different cloned voice). Unknown names fall back to the default
            # there, so this cannot 500 on a typo.
            payload = json.dumps({"model": "wb-voice", "voice": _voice,
                                  "input": text}).encode()
            req = urllib.request.Request(
                VOICE_URL, data=payload, headers={"Content-Type": "application/json"})
            try:
                # Generous: the first call after a restart pays ~20s of model
                # load. Later ones are a few seconds.
                with urllib.request.urlopen(req, timeout=90) as r:
                    return self._send(200, r.read(), "audio/mpeg")
            except Exception as exc:
                sys.stderr.write(f"speak failed: {exc}\n")
                sys.stderr.flush()
                return self._send(502, json.dumps(
                    {"error": f"voice router unreachable ({type(exc).__name__})"}),
                    "application/json")

        if path != "/api/agent":
            return self._send(404, "not here\n", "text/plain")
        if not AGENT_ACTIONS:
            self._discard_body()
            return self._send(403, json.dumps(
                {"ok": False, "error": "agent actions disabled"}),
                "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, json.dumps({"ok": False, "error": "bad body"}),
                              "application/json")

        label, action = body.get("label", ""), body.get("action", "")
        # Allowlist by identity, not by pattern: the label must be one this
        # portal actually discovered. Anything else is refused even if it is a
        # perfectly valid launchd label.
        known = {a["label"] for a in launch_agents()}
        if label not in known:
            return self._send(404, json.dumps({"ok": False, "error": "unknown label"}),
                              "application/json")
        uid = os.getuid()
        if action == "start":
            cmd = ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]
        elif action == "stop":
            cmd = ["launchctl", "kill", "SIGTERM", f"gui/{uid}/{label}"]
        else:
            return self._send(400, json.dumps({"ok": False, "error": "bad action"}),
                              "application/json")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            ok = r.returncode == 0
            out = (r.stderr or r.stdout).strip()
        except Exception as e:
            ok, out = False, str(e)
        with _lock:                      # force the next poll to read fresh state
            _cache["at"] = 0.0
        sys.stderr.write(f"AGENT {action} {label} -> {'ok' if ok else out}\n")
        sys.stderr.flush()
        self._send(200 if ok else 500,
                   json.dumps({"ok": ok, "detail": out}), "application/json")


def main():
    if not os.path.exists(REGISTRY):
        sys.stderr.write(f"fleetdeck: no registry at {REGISTRY}\n")
        sys.exit(1)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    # Loopback means the reachable URL is the `tailscale serve` front, not this
    # socket. Announcing http:// here sends the operator to a URL that 400s.
    front = "https" if BIND.startswith("127.") else "http"
    sys.stderr.write(
        f"fleetdeck portal {BIND}:{PORT} -> {front}://{HOST}:{PORT}"
        f"  (agents={'on' if SHOW_AGENTS else 'off'}"
        f" actions={'on' if AGENT_ACTIONS else 'off'})\n")
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
