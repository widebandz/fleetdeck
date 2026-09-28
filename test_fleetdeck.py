#!/usr/bin/env python3
"""Assertions for fleetdeck — the unit half runs offline, the live half proves
the deployed surface.

Weighted toward the agent classification, because that is where a wrong answer
is both easy to write and expensive: launchd reports a signal exit as a
negative number, and reading -15 as a failure paints a healthy board red —
including the portal's own tile, on the page it is serving.

    python3 test_fleetdeck.py           # unit + live
    python3 test_fleetdeck.py --unit    # unit only (no tailnet needed)
"""

import importlib.util
import json
import os
import plistlib
import re
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    "portal_server", os.path.join(HERE, "portal_server.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

BASE = f"https://{P.HOST}:{P.PORT}"
_fails = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}"
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def agents_from(plists, state):
    """Run launch_agents() against a directory we control, so the assertions
    are about the rule rather than about whatever this machine happens to be
    running today."""
    with tempfile.TemporaryDirectory() as d:
        for label, body in plists.items():
            with open(os.path.join(d, f"{label}.plist"), "wb") as fh:
                plistlib.dump({"Label": label, **body}, fh)
        real_dir, real_state = P.AGENT_DIR, P.launchd_state
        P.AGENT_DIR = d
        P.launchd_state = lambda: state
        try:
            return {a["label"]: a for a in P.launch_agents()}
        finally:
            P.AGENT_DIR, P.launchd_state = real_dir, real_state


# ── health classification ────────────────────────────────────────────────────
# The regression most likely to ship, written first.

BASIC = {"ProgramArguments": ["/bin/true"]}

# The load-bearing one. `launchctl list` reports a signal exit as a negative
# number, and -15 is SIGTERM — what every agent shows after a reload. Reading
# that as a failure paints a healthy board red, the portal's own tile included.
# A live PID classifies as `run` rather than `ok`, which is the more precise of
# the two healthy states; what matters is that it is never `fail`.
a = agents_from({"com.acme.reloaded": BASIC},
                {"com.acme.reloaded": {"pid": 4242, "exit": -15}})
check("UNIT-1 live PID with exit -15 is healthy, never fail",
      a["com.acme.reloaded"]["health"] == "run",
      f"got {a['com.acme.reloaded']['health']}")

a = agents_from({"com.acme.broken": BASIC},
                {"com.acme.broken": {"pid": None, "exit": 1}})
check("UNIT-2 no PID with exit 1 is fail",
      a["com.acme.broken"]["health"] == "fail",
      f"got {a['com.acme.broken']['health']}")

a = agents_from({"com.acme.rested": BASIC},
                {"com.acme.rested": {"pid": None, "exit": 0}})
check("UNIT-3 no PID with exit 0 is not fail — a rested periodic job is healthy",
      a["com.acme.rested"]["health"] == "ok",
      f"got {a['com.acme.rested']['health']}")

a = agents_from({"com.acme.never": BASIC}, {})
check("UNIT-4 on disk but never bootstrapped is off",
      a["com.acme.never"]["health"] == "off",
      f"got {a['com.acme.never']['health']}")

a = agents_from({"com.acme.signal9": BASIC},
                {"com.acme.signal9": {"pid": None, "exit": -9}})
check("UNIT-5 a signal exit with no PID is still ok",
      a["com.acme.signal9"]["health"] == "ok",
      f"got {a['com.acme.signal9']['health']}")

# ── last output ──────────────────────────────────────────────────────────────
# Absence is a real answer and has to survive as one.

a = agents_from({"com.acme.silent": BASIC},
                {"com.acme.silent": {"pid": None, "exit": 0}})
t = a["com.acme.silent"]
check("UNIT-6 no StandardOutPath yields no timestamp",
      t["last_output"] is None and t["logged"] is False,
      f"last_output={t['last_output']} logged={t['logged']}")

with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as fh:
    fh.write(b"x")
    logfile = fh.name
a = agents_from({"com.acme.chatty": {**BASIC, "StandardOutPath": logfile}},
                {"com.acme.chatty": {"pid": None, "exit": 0}})
t = a["com.acme.chatty"]
check("UNIT-7 an existing log yields its mtime",
      isinstance(t["last_output"], float) and t["logged"] is True,
      f"last_output={t['last_output']}")
os.unlink(logfile)

a = agents_from({"com.acme.declared": {**BASIC,
                                       "StandardOutPath": "/tmp/definitely-not-here.log"}},
                {"com.acme.declared": {"pid": None, "exit": 0}})
t = a["com.acme.declared"]
check("UNIT-8 a declared but missing log is absence, not a fallback time",
      t["last_output"] is None and t["logged"] is True,
      f"last_output={t['last_output']} logged={t['logged']}")

# A plist that will not parse is a different thing from a job with nothing
# configured, and used to render identically to one. XML forbids `--` inside a
# comment; `plutil -lint` accepts it and plistlib does not, which is how two
# agents on this machine went blank over a hyphen.
with tempfile.TemporaryDirectory() as d:
    open(os.path.join(d, "com.acme.malformed.plist"), "w").write(
        '<?xml version="1.0"?><!-- a -- b --><plist version="1.0"><dict/></plist>')
    real_dir, real_state = P.AGENT_DIR, P.launchd_state
    P.AGENT_DIR, P.launchd_state = d, lambda: {}
    try:
        t = P.launch_agents()[0]
    finally:
        P.AGENT_DIR, P.launchd_state = real_dir, real_state
check("UNIT-11 an unparseable plist says so rather than rendering blank",
      t["unreadable"] and t["program"] == "plist will not parse",
      f"program={t['program']!r} unreadable={t['unreadable']!r}")

# ── curation ─────────────────────────────────────────────────────────────────

a = agents_from({"com.apple.somedaemon": BASIC, "com.acme.mine": BASIC},
                {"com.apple.somedaemon": {"pid": 1, "exit": 0},
                 "com.acme.mine": {"pid": 2, "exit": 0}})
check("UNIT-9 com.apple.* is never a tile",
      "com.apple.somedaemon" not in a and "com.acme.mine" in a,
      f"saw {sorted(a)}")

a = agents_from({"com.acme.z-ok": BASIC, "com.acme.a-fail": BASIC},
                {"com.acme.z-ok": {"pid": 9, "exit": 0},
                 "com.acme.a-fail": {"pid": None, "exit": 1}})
first = next(iter(a))
check("UNIT-10 failures sort first, before name",
      first == "com.acme.a-fail", f"first tile was {first}")

# ── which surface is home ────────────────────────────────────────────────────
# The cookie decides what `/` renders, and `/` is the installed tile's start_url.
# Every way of not saying "simple" — absent, empty, junk, or a value invented by
# a newer build — has to mean the board, because the board is the surface that
# can reach everything and so the only safe thing to fall back to.
check("UNIT-12 no cookie is the board", P.home_pref(None) == "board")
check("UNIT-13 a stated preference is honoured",
      P.home_pref("a=1; fd_home=simple; b=2") == "simple",
      P.home_pref("a=1; fd_home=simple; b=2"))
for label, jar in (("junk", "fd_home=;;;=junk"),
                   ("an unknown value", "fd_home=hologram"),
                   ("another app's cookie", "session=abc")):
    check(f"UNIT-14 {label} falls back to the board",
          P.home_pref(jar) == "board", P.home_pref(jar))

# ── live ─────────────────────────────────────────────────────────────────────

if "--unit" in sys.argv:
    print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED"))
    sys.exit(1 if _fails else 0)

ctx = ssl.create_default_context()


def get(path, cookie=None):
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    return urllib.request.urlopen(req, timeout=20, context=ctx)


class _NoFollow(urllib.request.HTTPRedirectHandler):
    """`/home` is only interesting for the headers it answers with, and
    following the redirect throws them away."""

    def redirect_request(self, *a):
        return None


def raw(path):
    """(status, headers) without chasing a Location."""
    opener = urllib.request.build_opener(_NoFollow,
                                         urllib.request.HTTPSHandler(context=ctx))
    try:
        r = opener.open(BASE + path, timeout=20)
        return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


try:
    body = get("/api/status").read().decode()
    d = json.loads(body)
    check("LIVE-1 /api/status answers over the tailnet", True)
except Exception as e:
    check("LIVE-1 /api/status answers over the tailnet", False, str(e))
    d = {}

check("LIVE-2 the services grid still renders",
      len(d.get("services", [])) > 0,
      f"{len(d.get('services', []))} services")

ags = d.get("agents", [])
check("LIVE-3 agents are present", len(ags) > 0, f"{len(ags)} agents")
check("LIVE-4 no com.apple.* leaked into the board",
      not any(x["label"].startswith("com.apple.") for x in ags))
check("LIVE-5 every agent carries a last_output key, null or float",
      all("last_output" in x and (x["last_output"] is None
                                  or isinstance(x["last_output"], float))
          for x in ags))
check("LIVE-6 no running agent is classified fail",
      not any(x["health"] == "fail" and x["pid"] for x in ags),
      str([x["label"] for x in ags if x["health"] == "fail" and x["pid"]]))
check("LIVE-7 failures sort ahead of healthy tiles",
      [x["health"] for x in ags] == sorted(
          [x["health"] for x in ags],
          key=lambda h: {"fail": 0, "off": 1, "run": 2, "ok": 3}[h]))

# Every browser-facing tile should resolve to https. A plain-http origin is not
# a secure context, so Add to Home Screen degrades on exactly the tiles worth
# installing, and anything bound wider than loopback is also answering on the
# local Wi-Fi — a network that is not a boundary. `kind: api` tiles are exempt:
# they are never linked, so no browser ever lands on one.
insecure = [s for s in d.get("services", [])
            if (s.get("url") or "").startswith("http://")
            and s.get("kind") != "api" and s.get("linkable")]
check("LIVE-9 no linkable tile is served over plain http",
      not insecure,
      ", ".join(f"{s['name']} {s['url']}" for s in insecure))

try:
    page = get("/").read().decode()
    check("LIVE-8 the board renders with glyphs interpolated",
          "__GLYPHS__" not in page and "const I={" in page)
except Exception as e:
    check("LIVE-8 the board renders with glyphs interpolated", False, str(e))

# ── the home toggle, end to end ──────────────────────────────────────────────
# The failure this guards against is not cosmetic: get it wrong and `/` serves
# one surface while the toggle claims the other, on a phone, with no way back.

try:
    board_default = get("/").read().decode()
    simple_at_root = get("/", "fd_home=simple").read().decode()
    board_forced = get("/board", "fd_home=simple").read().decode()
    phone = get("/phone").read().decode()

    # `const I={` is the board's glyph payload; `id="t"` is the simple screen's
    # clock. Each surface is identified by something only it has, and asserted
    # absent from the other, so a half-rendered page cannot pass as either.
    check("LIVE-10 `/` is the board until told otherwise",
          "const I={" in board_default and 'id="t"' not in board_default)
    check("LIVE-11 `/` follows the cookie to the simple screen",
          'id="t"' in simple_at_root and "const I={" not in simple_at_root)
    # The canonical paths are the escape hatch. If `/board` ever started
    # honouring the cookie there would be no route back from a phone that had
    # chosen the simple screen.
    check("LIVE-12 `/board` renders the board whatever the cookie says",
          "const I={" in board_forced)
    check("LIVE-13 the board's toggle reflects the current home",
          'id="simple" class="on" href="/home?ui=board"' in board_forced
          and 'href="/home?ui=simple"' in board_default)
    check("LIVE-14 no placeholder survives to either surface",
          "__SIMPLE_" not in board_default and "__HOME_TOGGLE__" not in phone
          and "__CALL_HREF__" not in phone and "__CALL_DEST__" not in phone
          and "__MARK__" not in phone and "__N__" not in phone)
    # The CALL key acknowledges the tap here and connects somewhere else, which
    # draws the only connecting screen. Drawing a second one here is the
    # regression this guards: one action, one overlay.
    #
    # "somewhere else" became the native Messages app on 2026-09-23, so the
    # href is asserted against call_destination() rather than CALL_TARGET_PATH
    # — that constant still names the retired web chat, which is still served
    # and no longer linked from here.
    check("LIVE-17 the phone screen draws no connecting screen of its own",
          'id="conn"' not in phone and "class=\"rings\"" not in phone
          and "a.call.opening" in phone
          and 'href="%s"' % P.call_destination(None)[0] in phone)

    # ── the mark ─────────────────────────────────────────────────────────────
    # Added 2026-09-23. Three things have to hold together or it renders as a
    # black square, a still frame, or nothing.
    #
    # Position is asserted because the whole request was where it sits: under
    # the clock, above the keys. An element that renders in the wrong band of
    # the screen passes every test that only looks for its presence.
    i_clock, i_mark, i_grid = (phone.find('class="clock"'),
                               phone.find('class="mark"'),
                               phone.find('class="grid"'))
    check("LIVE-24 the mark sits between the clock and the keys",
          -1 < i_clock < i_mark < i_grid, f"{i_clock} {i_mark} {i_grid}")

    # `muted` and `playsinline` are the two attributes iOS silently requires;
    # without either, autoplay is refused and the mark freezes on frame one.
    # That failure is invisible from a desktop browser, which plays it anyway.
    check("LIVE-25 the mark is allowed to autoplay on iOS",
          all(a in phone for a in ("autoplay", "muted", "loop", "playsinline")))

    # The loop is cyan-on-black with no alpha; `screen` is what makes the black
    # disappear into the page. Lose it and the mark gains a visible box.
    check("LIVE-26 the mark is screened, not boxed",
          "mix-blend-mode:screen" in phone)

    mp4 = get("/wb-logo-256.mp4")
    blob = mp4.read()
    check("LIVE-27 the mark's video is actually served",
          mp4.status == 200 and mp4.headers.get("Content-Type") == "video/mp4"
          and blob[4:8] == b"ftyp",
          f"{mp4.status} {mp4.headers.get('Content-Type')} {len(blob)}B")
    # It is on the critical path of the front screen, on cell data. The master
    # in the cockpit's public/ is 6MB; shipping that by accident is a one-line
    # mistake with no visible symptom on the desk.
    check("LIVE-28 the mark stays small enough for cell data",
          len(blob) < 600_000, f"{len(blob)}B")
except Exception as e:
    check("LIVE-10..14 the two surfaces render", False, str(e))

for ui, dest in (("simple", "/phone"), ("board", "/board")):
    status, hdrs = raw(f"/home?ui={ui}")
    check(f"LIVE-15 /home?ui={ui} sets the cookie and lands on {dest}",
          status == 303 and hdrs.get("Location") == dest
          and f"{P.HOME_COOKIE}={ui}" in (hdrs.get("Set-Cookie") or ""),
          f"{status} {hdrs.get('Location')} {hdrs.get('Set-Cookie')}")

status, hdrs = raw("/home?ui=hologram")
check("LIVE-16 an unknown ui is refused and writes nothing",
      status == 400 and not hdrs.get("Set-Cookie"),
      f"{status} {hdrs.get('Set-Cookie')}")

# ── Grace, retired 2026-09-16 ────────────────────────────────────────────────
#
# The operator moved to iMessage voice notes, so the spoken half of /call-trace
# is off by default and the TEXT half must survive untouched. These assert both
# directions, because the way this goes wrong is not "Grace still answers" — it
# is a half-removal that takes the text surface down with her, or leaves a
# button that no longer does anything.
#
# Written against the live server on purpose. GRACE_ENABLED is read at import
# and the page is assembled per request, so only a real fetch proves which of
# the two branches this process is actually serving.

try:
    call_page = get("/call-trace").read().decode()

    # A control that is present and does nothing is the failure this page had
    # once already, when its label promised full duplex against a push-to-talk
    # target. Gone means gone from the DOM, not disabled.
    #
    # ASSERTED ON THE ELEMENT, NOT THE WORDS. The first version of this test
    # also required that "HANDS FREE" appear nowhere in the response, and it
    # failed — correctly. The string is still in the client script, in the line
    # that would relabel the button if it existed, and that script is retained
    # ON PURPOSE while this is a deprecation rather than a deletion. Searching
    # for the label conflates "the control is gone" with "the code is gone",
    # and only the first of those is true today. `class="tog"` is the button's
    # own class and appears nowhere else.
    check("LIVE-18 the hands-free control is absent, not merely inert",
          'id="hf"' not in call_page and 'class="tog"' not in call_page)

    # Both branches of the template substitute, and a missed one would ship a
    # literal __INTRO__ to the operator's phone.
    check("LIVE-19 no deprecation placeholder survives to the page",
          "__HANDS_FREE_BTN__" not in call_page
          and "__INTRO__" not in call_page and "__HINT__" not in call_page)

    # THE HALF THAT MUST NOT DIE. Typing reaches Trace through
    # router().deliver() — the same pipeline a text message takes — and that is
    # the surface the retirement is in favour of, not against.
    check("LIVE-20 the text half of Call Trace still renders",
          'id="t"' in call_page and 'id="f"' in call_page
          and 'id="go"' in call_page,
          "the textarea, form or send button went with Grace")

    # The script is still shipped whole while deprecated rather than deleted,
    # so the one thing that can break the text half is an unguarded reference
    # to the button that is no longer there.
    check("LIVE-21 no unguarded hands-free wiring is left to throw",
          "if(hf)hf.addEventListener" in call_page
          and "function paintHF(){if(!hf)return;" in call_page,
          "a null #hf would throw and take the whole script with it")

    # A stale wbhf=1 in localStorage must not outlive the button.
    check("LIVE-22 a remembered hands-free state cannot survive the button",
          "let handsFree=!!hf&&localStorage.getItem('wbhf')==='1';" in call_page)
except Exception as e:
    check("LIVE-18..22 the retired call surface renders", False, str(e))

# 410, not 404: the endpoint is retired, not missing, and a tab left open on the
# old page should be told the difference. Checked with a well-formed body so a
# pass cannot come from the 400 that a bad body would earn anyway.
try:
    req = urllib.request.Request(
        BASE + "/api/grace", method="POST",
        data=json.dumps({"text": "are you there", "history": []}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20, context=ctx)
    check("LIVE-23 /api/grace is retired", False, "answered 200")
except urllib.error.HTTPError as e:
    check("LIVE-23 /api/grace is retired", e.code == 410, f"got {e.code}")
except Exception as e:
    check("LIVE-23 /api/grace is retired", False, str(e))

# The CALL key's own promise. It said "text · voice · same pipeline" while the
# voice half existed; saying it now would be the page's old mistake repeated.
#
# The href assertion used to be `== CALL_TARGET_PATH` and had to change with
# the key on 2026-09-23: it now leaves the browser for the native Messages
# app, because the web chat was a second inbox for a conversation that already
# lived on iMessage.
href, label, sub = P.call_destination(None)
check("UNIT-CALL the key no longer advertises voice",
      "voice" not in sub.lower(), f"{label!r} {sub!r}")

# The address is the whole correctness of this key. `sms:` to the handle the
# Mac SENDS FROM opens the existing thread; anything else opens an empty one
# beside it, which looks like it worked and silently loses the history — the
# exact failure mode that is hardest to notice from a phone.
check("UNIT-CALL-ADDR the key opens Messages at the Mac's own handle",
      href == "sms:" + P.TRACE_IMESSAGE_HANDLE and "@" in P.TRACE_IMESSAGE_HANDLE,
      f"{href!r}")

# A prefilled body lands as a draft in a live thread, which is something to
# delete before you can type. Guarded because the temptation to add one back
# ("text Trace: ...") is real and the cost is only visible mid-conversation.
check("UNIT-CALL-BODY the key prefills nothing",
      "body=" not in href, f"{href!r}")

# A POST that refuses before reading its body leaves those bytes in the socket.
# Direct, nothing notices — the client closes the connection anyway. Through
# `tailscale serve` the proxy REUSES that connection, so the next request is
# parsed starting from the tail of the last one's JSON and answers 400. Found
# on 2026-09-25 as "/notes intermittently 400s", which it had nothing to do
# with; /api/grace was three tests earlier.
#
# Asserted over BASE specifically, because that is the tailnet path and this
# bug does not reproduce over loopback.
try:
    for _ in range(2):
        try:
            urllib.request.urlopen(urllib.request.Request(
                BASE + "/api/grace", method="POST",
                data=json.dumps({"text": "x", "history": []}).encode(),
                headers={"Content-Type": "application/json"}), timeout=20, context=ctx)
        except urllib.error.HTTPError:
            pass
        after = get("/healthz").status
    check("LIVE-24 a refused POST does not poison the next request", after == 200,
          f"GET after a refused POST answered {after}")
except Exception as e:
    check("LIVE-24 a refused POST does not poison the next request", False, str(e))

# ── the live terminal network ────────────────────────────────────────────────
#
# The URL is asserted literally, which is unusual here — every other link on
# these surfaces is resolved from the registry and the tests check the
# resolution, not the string. This one has no registry entry to resolve from,
# and its two ports differ by a digit swap (:18970 published, :18790 local).
# That is precisely the pair a reader "fixes" by hand, so the approved value is
# written down somewhere a change has to argue with.
check("UNIT-NET the approved destination is unchanged",
      P.NETMAP_URL == "https://brainwave.tailacfa70.ts.net:18970/fleet-map",
      f"{P.NETMAP_URL!r}")
check("UNIT-NET-LABEL the button says what was approved",
      P.NETMAP_LABEL == "Live Terminal Network", f"{P.NETMAP_LABEL!r}")

try:
    _board, _phone = get("/board").read().decode(), get("/phone").read().decode()

    # On BOTH surfaces, and that is the point rather than belt-and-braces: the
    # desk header is unreachable from a phone whose home is the simple screen
    # without going through the board first.
    check("LIVE-33 the live map is reachable from the board header",
          'id="netmap"' in _board and P.NETMAP_URL in _board
          and P.NETMAP_LABEL in _board)
    check("LIVE-34 the live map is reachable from the phone screen",
          'class="key wide net"' in _phone and P.NETMAP_LABEL in _phone)

    # THE TWO SURFACES DIVERGED on 2026-09-27 and the divergence is the point.
    #
    # The desk still links straight at the map in a new tab: it has an address
    # bar already, and a board worth keeping open behind what you are reading.
    # The phone goes through the frame in the SAME tab, because there the whole
    # cost of a new tab is the installed app dropping out of fullscreen — which
    # is the thing the frame exists to prevent. Asserting "new tab on both",
    # as this did until the map was framed, would now be asserting the bug.
    _i = _board.index(P.NETMAP_URL)
    _tag = _board[max(0, _i - 260):_i + 260]
    check("LIVE-35 the board link opens a new tab, with noopener",
          'target="_blank"' in _tag and "noopener" in _tag, _tag[-160:])

    _j = _phone.index('href="/app/netmap"')
    check("LIVE-35 the phone link stays in the installed app",
          'target="_blank"' not in _phone[_j - 160:_j + 160],
          "a new tab from the phone leaves the standalone shell")

    check("LIVE-36 no placeholder survives to the board", "__NETMAP__" not in _board)
except Exception as e:
    check("LIVE-33 the live map is reachable from the board header", False, str(e))

# The destination is another service on this tailnet, so it can be down while
# this one is fine. Asserted anyway: a button to a dead map is the failure the
# operator would report as "the new button does nothing", and this is the only
# place that would say otherwise first.
try:
    _r = urllib.request.urlopen(urllib.request.Request(P.NETMAP_URL),
                                timeout=20, context=ctx)
    check("LIVE-37 the live map answers", _r.status == 200, f"got {_r.status}")
except Exception as e:
    check("LIVE-37 the live map answers", False, str(e))


# ── transitions ──────────────────────────────────────────────────────────────
#
# A cross-document view transition needs `navigation: auto` on BOTH documents.
# Miss one surface and it does not degrade to "no animation there" — every
# route into or out of it hard-cuts while the rest glide, which reads as that
# page being broken. So this is asserted on the whole set, not on a sample.
try:
    _surfaces = ["/phone", "/board", "/notes", "/cashflow"] + \
                ["/app/%s" % i for i in P.SHELLED_APPS] + ["/app/netmap"]
    for _u in _surfaces:
        _b = get(_u).read().decode()
        check(f"LIVE-56 {_u} opts into view transitions",
              "@view-transition" in _b and "navigation:auto" in _b.replace(" ", "")
              and "__VT__" not in _b)

    # Motion is a preference, and this one is an accessibility setting rather
    # than a taste. Verified live in a reduced-motion context: the transition
    # does not run at all.
    check("LIVE-57 reduced motion is honoured",
          "prefers-reduced-motion" in get("/phone").read().decode())
except Exception as e:
    check("LIVE-56 every surface opts into view transitions", False, str(e))

# The durations ARE the feature. Measured end-to-end at 251-265ms on this
# machine; the declared numbers are what keep it there. This is a guard against
# the one edit that would ruin it — somebody reaching for a more visible
# animation and putting a toll booth on every tap.
_ms = [int(v) for v in re.findall(r"(\d+)ms", P.VIEW_TRANSITION_CSS)]
check("UNIT-VT the transition stays under the threshold where it feels slow",
      _ms and max(_ms) <= 250, f"declared durations: {_ms}")

# No named elements, deliberately: a morph that half-plays after a class is
# renamed is worse than a crossfade that cannot misalign.
check("UNIT-VT-SIMPLE the transition names no elements to misalign",
      "view-transition-name" not in P.VIEW_TRANSITION_CSS)


# ── framed apps ──────────────────────────────────────────────────────────────
#
# The point of the shell is one property: the TOP-LEVEL document never leaves
# this origin, so the installed app never drops into a Custom Tab. Everything
# below is in service of that, and the test that matters most is the closed
# set — a route that frames a URL is a route that must never frame one the
# request chose.
try:
    _scan = {s["id"]: s for s in json.loads(get("/api/status").read().decode())
             .get("services", [])}
    for _id, _label in P.SHELLED_APPS.items():
        _sh = get("/app/%s" % _id).read().decode()
        check(f"LIVE-45 the {_id} shell frames the registry's own URL",
              ('src="%s"' % _scan[_id]["url"]) in _sh and "<iframe" in _sh
              and _label in _sh,
              f"expected {_scan[_id]['url']!r}")
        # The bar is the only way back: there is no address bar in the
        # installed app and the framed service has no idea it is framed.
        check(f"LIVE-46 the {_id} shell carries the way back",
              'id="fd-back"' in _sh and 'href="/phone"' in _sh)

    # Nothing in the request names a URL. These are the three shapes someone
    # would try, and a pass here is what makes the iframe safe to keep.
    # `health` is registered and deliberately NOT framed, which is the case
    # that matters: being in the registry is not what makes a URL frameable,
    # being named in SHELLED_APPS is. (This slot used to be `terminal`, until
    # terminal was framed — a reminder to pick an id nobody is about to add.)
    for _bad, _why in (("health", "a registered service that is not framed"),
                       ("../etc/passwd", "traversal"),
                       ("https%3A%2F%2Fevil.com", "an arbitrary origin")):
        try:
            _c = get("/app/%s" % _bad).status
        except urllib.error.HTTPError as e:
            _c = e.code
        check(f"LIVE-47 /app refuses {_why}", _c == 404, f"got {_c}")

    _ph = get("/phone").read().decode()
    # Framed services are reached through this origin; everything else is
    # untouched and still links straight at the service.
    for _id in P.SHELLED_APPS:
        check(f"LIVE-48 the phone reaches {_id} through this origin",
              ('href="/app/%s"' % _id) in _ph)
    for _id, _svc in _scan.items():
        if _id in P.SHELLED_APPS and _svc.get("linkable"):
            check(f"LIVE-49 no direct link to {_id} survives on the phone",
                  ('href="%s"' % _svc["url"]) not in _ph,
                  "a direct link would break out of the installed app")
    # The mark under the clock goes to the platform, which is not a framed app
    # and must still link straight at it. Every key IS framed now, so this is
    # what proves the shell was applied to a set rather than to everything
    # that happens to be a link.
    check("LIVE-50 unframed links still go straight through",
          ('href="%s"' % _scan["app"]["url"]) in _ph)

    # The fleet map has no registry entry, so it is framed from a static map
    # instead of a scan lookup. Asserted separately because it is the one
    # framed surface whose URL is a constant rather than a resolution.
    _nm = get("/app/netmap").read().decode()
    check("LIVE-52 the fleet map is framed from its constant",
          ('src="%s"' % P.NETMAP_URL) in _nm and P.NETMAP_LABEL in _nm
          and 'id="fd-back"' in _nm)
    check("LIVE-53 the phone reaches the fleet map through this origin",
          'href="/app/netmap"' in _ph and P.NETMAP_URL not in _ph,
          "a direct link would break out of the installed app")

    # THE OTHER SERVICE HAS TO ALLOW THIS. The map sends frame-ancestors, and
    # it named 'none' until 2026-09-27 — which refuses every framer, so the
    # frame above would load an empty document and the browser would cancel
    # the request. If ~/srv/fleetdeck-authoring is ever reverted or
    # redeployed from an older copy, THIS is the test that says why the map
    # went blank, rather than it being hunted for in this repo.
    try:
        _csp = urllib.request.urlopen(
            urllib.request.Request(P.NETMAP_URL), timeout=20, context=ctx
        ).headers.get("Content-Security-Policy") or ""
    except Exception as _e:
        _csp = "unreachable: %s" % _e
    check("LIVE-54 the fleet map still permits this origin to frame it",
          "frame-ancestors" in _csp and "brainwave.tailacfa70.ts.net:8790" in _csp,
          f"frame-ancestors is {_csp.split('frame-ancestors')[-1].strip()!r} "
          "— fix in ~/srv/fleetdeck-authoring/portal_server.py")
    # And no wider than that. The relaxation named ONE origin; a later edit
    # reaching for '*' or dropping the directive would pass the test above
    # and quietly make the map frameable by anything.
    check("LIVE-55 the fleet map is frameable by this origin only",
          "frame-ancestors" in _csp and "*" not in _csp.split("frame-ancestors")[-1],
          f"{_csp.split('frame-ancestors')[-1].strip()!r}")

    # Permissions a frame does not inherit. Asserted per-app because the
    # failure is silent in both directions: no microphone and Trace cannot
    # hear, microphone everywhere and five surfaces hold a capability they
    # have no use for.
    check("LIVE-51 only the cockpit frame is granted a microphone",
          "microphone" in get("/app/cockpit").read().decode()
          and not any("microphone" in get("/app/%s" % i).read().decode()
                      for i in P.SHELLED_APPS if i != "cockpit"))
except Exception as e:
    check("LIVE-45 the shell frames the registry's own URL", False, str(e))

# A framed service that is down would otherwise show the browser's own
# connection-failed page inside the frame, which reads as Fleetdeck being
# broken rather than the service being off.
check("UNIT-SHELL-DOWN a stopped service has a page of its own",
      "__LABEL__" in P.APP_SHELL_DOWN and "not running" in P.APP_SHELL_DOWN)

# One bar, one look, both surfaces. The cashflow injection and the shell share
# it so they cannot drift into two different ways back.
check("UNIT-SHELL-BAR the seam is shared, not duplicated",
      "__SUB__" in P.FLEET_BAR and 'href="/phone"' in P.fleet_bar("x")
      and "&lt;script&gt;" not in P.fleet_bar("x")
      and P.fleet_bar("<script>").count("<script>") == 0,
      "the sub-label must be escaped — it reaches a foreign document")


# ── cashflow ─────────────────────────────────────────────────────────────────
#
# Served from THIS origin on purpose rather than through a tunnel: a different
# port would be a different origin and would drop the installed Android app
# into a Custom Tab, which is the fault the fleet-map button already carries.
# Asserted as a relative path for that reason — the day this becomes an
# absolute URL to another port is the day it stops working the way it was
# built to.
try:
    _board, _phone = get("/board").read().decode(), get("/phone").read().decode()
    check("LIVE-38 cashflow is reachable from both surfaces",
          'id="cashflow"' in _board and 'href="/cashflow"' in _phone
          and P.CASHFLOW_LABEL in _board and P.CASHFLOW_LABEL in _phone)
    check("LIVE-39 cashflow stays on this origin",
          'href="/cashflow"' in _board and 'href="/cashflow"' in _phone
          and "//brainwave" not in _board.split('id="cashflow"')[1][:200],
          "an absolute URL would leave the installed app")

    # Same tab, unlike the fleet map beside it. This is an internal surface and
    # opening it in a new tab would strand Fleetdeck tabs behind it.
    _i = _board.index('id="cashflow"')
    check("LIVE-40 cashflow opens in the same tab",
          'target="_blank"' not in _board[_i - 120:_i + 200],
          _board[_i - 40:_i + 160])

    _cf = get("/cashflow")
    _body = _cf.read().decode()
    check("LIVE-41 cashflow serves the accountant's page", _cf.status == 200
          and "Cash Flow" in _body and len(_body) > 5000,
          f"{_cf.status} {len(_body)}B")

    # No address bar in the installed app means no browser-provided way back,
    # and this page is the agent's artefact with no header of its own.
    check("LIVE-42 the served page carries a way back into Fleetdeck",
          'id="fd-back"' in _body and 'href="/phone"' in _body)

    # Injected at serve time. If this ever lands on disk, the accountant's next
    # regeneration silently drops it — and worse, we would be editing a file
    # this server does not own.
    _disk = open(P.CASHFLOW_PATH, encoding="utf-8").read()
    check("LIVE-43 the accountant's file is never written to",
          "fd-back" not in _disk and "Fleetdeck" not in _disk)

    # The point of a monitoring surface: what is served is what is on disk NOW,
    # not a copy taken when this process booted.
    check("LIVE-44 the page is read per request, not cached at boot",
          _disk[:400] in _body or _body.count("Cash Flow") == _disk.count("Cash Flow"))
except Exception as e:
    check("LIVE-38 cashflow is reachable from both surfaces", False, str(e))

# A missing file is the likely state before the accountant has written one, and
# it must not read as a broken button.
check("UNIT-CASHFLOW the not-yet-written page names the path it wanted",
      "__PATH__" in P.CASHFLOW_MISSING and "finance-ops" in P.CASHFLOW_PATH)

# Financial data. The gate is the same tailnet check as everything else, and
# this asserts the route sits behind it rather than in the pre-auth prologue
# where /healthz lives.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "portal_server.py"), encoding="utf-8").read()
check("UNIT-CASHFLOW-PRIVATE cashflow is behind the tailnet gate",
      _src.index('if path == "/cashflow"')
      > _src.index("if not allowed(self.client_address[0])"))


# ── notes ────────────────────────────────────────────────────────────────────
#
# The store is asserted through notes_normalise() rather than through the HTTP
# routes, because that function IS the boundary — both the phone form and, in
# time, Trace arrive there, and anything true of it is true of both callers.
# Testing the routes instead would prove the phone path and leave the agent
# path, which is the one written for a caller that may be wrong, unproven.

# The shape is the product. A note that cannot say what it is, what it means
# and what to do next is the generic list this surface exists to not be.
seed = P.notes_normalise(P.SEED_NOTES[0])
check("UNIT-NOTE-SHAPE a note carries original, concept, move and status",
      all(seed.get(k) for k in ("title", "original", "concept", "next", "status")),
      f"{sorted(k for k, v in seed.items() if not v)} empty")

check("UNIT-NOTE-SEED the seeded idea is the operator's, intact",
      seed["title"] == "Instant iMessage Agent Installer"
      and "Apple Account" in seed["original"] and "head agent" in seed["original"]
      and seed["status"] == "inbox" and seed["source"] == "voice")

# ── the agent write path ─────────────────────────────────────────────────────
#
# These four are the whole reason Trace can be given this endpoint later. Each
# one is a way a confused or hostile caller could reach past its own note, and
# each is asserted to fail closed. If any of them regress, the endpoint stops
# being safe to hand to an agent — which is a thing that would otherwise be
# discovered by an agent doing it.
hostile = P.notes_normalise({
    "id": seed["id"], "created": 1, "title": "hijack", "status": "../../etc",
    "source": "admin", "path": "/etc/passwd", "cmd": "rm -rf /",
})
check("UNIT-NOTE-ID a client cannot choose its own id",
      hostile["id"] != seed["id"] and hostile["created"] != 1,
      f"{hostile['id']!r}")
check("UNIT-NOTE-STATUS status is a closed set, not a string",
      hostile["status"] == "inbox" and P.notes_normalise({"status": "done"})["status"] == "done",
      f"{hostile['status']!r}")
check("UNIT-NOTE-KEYS unknown keys never reach the file",
      set(hostile) == {"id", "created", "title", "original", "concept", "next",
                       "status", "source"},
      f"{sorted(set(hostile) - {'id','created','title','original','concept','next','status','source'})}")
check("UNIT-NOTE-CAPS every field is truncated server-side",
      len(P.notes_normalise({"title": "x" * 9999})["title"]) == P.NOTE_LIMITS["title"]
      and len(P.notes_normalise({"original": "y" * 99999})["original"])
          == P.NOTE_LIMITS["original"])

# ── editing ──────────────────────────────────────────────────────────────────
#
# Exercised against a private temporary store. An earlier version edited the
# operator's first live note and tried to restore it; a crash or concurrent edit
# could lose the operator's changes.
try:
    with tempfile.TemporaryDirectory() as _notes_dir:
        _real_notes_path = P.NOTES_PATH
        P.NOTES_PATH = os.path.join(_notes_dir, "notes.json")
        try:
            _before = P.notes_all()[0].copy()
            _one = P.notes_update(_before["id"], {"concept": "a corrected concept"})
            check("UNIT-NOTE-EDIT an edit touches only the keys it names",
                  _one["concept"] == "a corrected concept"
                  and _one["title"] == _before["title"]
                  and _one["next"] == _before["next"]
                  and _one["original"] == _before["original"])

            _two = P.notes_update(_before["id"], {
                "id": "n-other", "created": 1, "source": "admin",
                "status": "done", "edited": 0, "cmd": "rm -rf /",
                "title": "a corrected title"})
            check("UNIT-NOTE-EDIT-IDENTITY an edit cannot rewrite identity",
                  _two["id"] == _before["id"]
                  and _two["created"] == _before["created"]
                  and _two["source"] == _before["source"]
                  and _two["status"] == _before["status"]
                  and "cmd" not in _two and _two["title"] == "a corrected title")

            check("UNIT-NOTE-EDIT-CAPS an edit is truncated like a capture",
                  len(P.notes_update(_before["id"],
                                     {"title": "z" * 9999})["title"])
                  == P.NOTE_LIMITS["title"])
            check("UNIT-NOTE-EDIT-EMPTY an edit cannot hollow out a note",
                  P.notes_update(_before["id"], {
                      "title": "", "concept": "", "original": ""}) is None
                  and P.notes_update("ghost", {"title": "x"}) is None)
            check("UNIT-NOTE-EDIT-ISOLATED edits stay in a temporary store",
                  os.path.isfile(P.NOTES_PATH)
                  and P.NOTES_PATH != _real_notes_path)
        finally:
            P.NOTES_PATH = _real_notes_path
except Exception as e:
    check("UNIT-NOTE-EDIT an edit touches only the keys it names", False, str(e))

# The editor has to be reachable and has to be the same four fields the card
# shows. A save button with no `original` field would quietly make the raw text
# uneditable, which is the one thing a bad transcription needs.
try:
    _np = get("/notes").read().decode()
    check("LIVE-32 the editor offers every field the card shows",
          all(('data-f="%s"' % f) in _np
              for f in ("title", "concept", "next", "original"))
          and "data-save=" in _np and "data-cancel=" in _np and "data-edit=" in _np)
except Exception as e:
    check("LIVE-32 the editor offers every field the card shows", False, str(e))

# ── capture ──────────────────────────────────────────────────────────────────
#
# The raw text is the evidence. A distillation that loses what was actually
# said cannot be checked against anything later, which is the failure that
# makes a notes app worth abandoning.
raw = "just a thought about the installer\nand a second line"
check("UNIT-NOTE-RAW an unlabelled capture keeps its text and names itself",
      P.note_from_text(raw)["original"] == raw
      and P.notes_normalise(P.note_from_text(raw))["title"].startswith("just a thought"))

parsed = P.note_from_text(
    "title: Ladder autoposter\nconcept: schedule the posts\n  from one queue\n"
    "next: pick the scheduler\ntrailing prose")
check("UNIT-NOTE-PARSE labelled lines distil, and wrapped ones stay whole",
      parsed["title"] == "Ladder autoposter"
      and parsed["concept"] == "schedule the posts from one queue"
      and parsed["next"] == "pick the scheduler"
      and parsed["original"].endswith("trailing prose"),
      f"{parsed}")

# ── live ─────────────────────────────────────────────────────────────────────
try:
    notes_page = get("/notes").read().decode()
    check("LIVE-29 the notes surface renders with its statuses interpolated",
          "__STATUSES__" not in notes_page and "__MACHINE__" not in notes_page
          and '"inbox"' in notes_page and 'id="list"' in notes_page)

    # It is reached from the front screen or it may as well not exist.
    check("LIVE-30 the phone screen offers a way into notes",
          'href="/notes"' in get("/phone").read().decode())

    # GET would seed a missing store, so the live test checks the page only.
    # Store behavior is covered against a private path above.
except Exception as e:
    check("LIVE-29 the notes surface renders with its statuses interpolated", False, str(e))

# The one boundary that must not move. Notes are deliberately NOT behind
# AGENT_ACTIONS — they touch no process and take no path — so the gate that
# keeps them private is the same tailnet check as everything else. If a notes
# route were ever added before that check in do_GET, this is what would say so.
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "portal_server.py"), encoding="utf-8").read()
gate = src.index("if not allowed(self.client_address[0])")
check("UNIT-NOTE-PRIVATE no notes route is reachable before the tailnet gate",
      src.index('if path == "/notes"') > gate
      and src.index('if path == "/api/notes"') > gate)

print("\n" + ("ALL PASS" if not _fails else f"{len(_fails)} FAILED: "
                                            + ", ".join(_fails)))
sys.exit(1 if _fails else 0)
