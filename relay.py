"""HTTP relay: Sofabaton X2 virtual HTTP device -> Sony projector over ADCP (pyadcp).

Every control endpoint accepts GET or POST so the Sofabaton app can use either.

    /power/on  /power/off
    /input/<terminal>        e.g. /input/hdmi1
    /picmode/<mode>          e.g. /picmode/reference
    /key/<code>              e.g. /key/menu
    /blank/<on|off>
    /set/<command>/<value>   any ADCP setting, e.g. /set/hdr/auto  [?suffix=..][&wait=60]
    /get/<command>           read any ADCP setting
    /values?names=a,b:sfx    current values of preset-able settings, null where greyed out
    /preset/<name>           apply a named group of settings, e.g. /preset/game  [&wait=20]
    /preset/<name>/verify    apply, reading every setting back after it is set and again at the end
    /presets                 list the configured presets
    PUT/DELETE /presets/<name>  save (JSON {"steps": [...]}) or remove a preset in PRESETS_FILE
    /ui                      browser page for status and preset editing (no token to load it)
    /ui/login /ui/logout /ui/session   browser sign-in: the token once, then an HttpOnly cookie
    /caps                    what this unit supports (options and ranges)
    /status                  JSON, never changes projector state
    /healthz                 relay liveness only (does not touch the projector)

Config (environment):
    PROJECTOR_HOST       required
    PROJECTOR_PASSWORD   ADCP admin password (default "Projector")
    PRESETS_FILE         JSON file of named presets (see DEFAULT_PRESETS); needed to save edits
    PROJECTOR_MAC        optional; enables Wake-on-LAN when the unit is unreachable
    RELAY_TOKEN          optional; if set, requests need ?token=... or X-Token header
    UI_AUTO_LOGIN        browsers signed in to /ui without the token: "open" (default, every
                         address; logged as a warning), "off", or networks like 192.168.1.0/24,10.0.0.5
    RELAY_PORT           default 8080
    RELAY_BIND           default 0.0.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import web
from pyadcp import (
    AdcpConnectionError,
    AdcpError,
    AdcpTimeoutError,
    AuthenticationError,
    CommandError,
    CommandFormError,
    CommandInactiveError,
    CommandKind,
    PowerStatus,
    Projector,
    UnknownCommandError,
    get_command,
)

log = logging.getLogger("adcp-relay")

PROJECTOR: web.AppKey[Projector] = web.AppKey("projector", Projector)
MAC: web.AppKey[str | None] = web.AppKey("mac", str)
TOKEN: web.AppKey[str | None] = web.AppKey("token", str)
type Networks = tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
AUTO_LOGIN: web.AppKey[Networks] = web.AppKey("auto_login", tuple)
OPEN_NETWORKS: Networks = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))
# A step is {"command": ..., "value": ...} plus "suffix" for commands that take one.
type Step = dict[str, str]
type Presets = dict[str, list[Step]]
PRESETS: web.AppKey[Presets] = web.AppKey("presets", dict)
PRESETS_PATH: web.AppKey[str | None] = web.AppKey("presets_path", str)
PRESETS_LOCK: web.AppKey[asyncio.Lock] = web.AppKey("presets_lock", asyncio.Lock)
CAPS: web.AppKey[dict[str, dict[str, object]]] = web.AppKey("caps", dict)

UI_FILE = Path(__file__).with_name("ui.html")
SESSION_COOKIE = "relay_session"
UI_HEADER = "X-Relay-UI"
OPEN_PATHS = frozenset({"/healthz", "/ui", "/ui/login", "/ui/logout", "/ui/session", "/ui/version"})
BOOT_ID = os.urandom(4).hex()

# What a preset saved through the API may set: picture and signal settings only.
# Network, lock, IR-receiver and similar setup commands are left out so a preset
# cannot cut the relay off from the projector.
PRESET_COMMANDS = frozenset({
    "input", "blank", "picture_mode", "contrast", "brightness", "color", "hue", "sharpness",
    "color_temp", "coltemp_gain_r", "coltemp_gain_g", "coltemp_gain_b", "coltemp_bias_r",
    "coltemp_bias_g", "coltemp_bias_b", "color_space", "col_space_x", "col_space_y",
    "gamma_correction", "film_mode", "real_cre", "real_cre_db", "real_cre_reso",
    "real_cre_noise", "motionflow", "nr", "mnr", "smooth_grd", "contrast_enh",
    "col_correction", "col_corr_hue", "col_corr_color", "col_corr_brt", "clear_white",
    "xvcolor", "input_lag_red", "lamp_control", "hdr", "aspect", "pic_pos_sel", "2d3d_sel",
    "3d_format", "3d_depth", "sim3d_effect", "hdmi_signal_format", "dynamic_range",
})

# Each preset is an ordered list of steps, applied in order. Order matters: input lag
# reduction greys out Motionflow and Reality Creation on Sony units, so those are set
# first. Override with PRESETS_FILE (same JSON shape). Option names are this unit's;
# confirm them against /caps.
DEFAULT_PRESETS: Presets = {
    "game": [
        {"command": "picture_mode", "value": "game"},
        {"command": "motionflow", "value": "off"},
        {"command": "real_cre", "value": "off"},
        {"command": "nr", "value": "off"},
        {"command": "mnr", "value": "off"},
        {"command": "input_lag_red", "value": "on"},
        {"command": "hdr", "value": "auto"},
    ],
    "movie": [
        {"command": "input_lag_red", "value": "off"},
        {"command": "picture_mode", "value": "reference"},
        {"command": "hdr", "value": "auto"},
    ],
}
STEP_KEYS = frozenset({"command", "value", "suffix"})

# Only simple identifiers may reach the projector; never forward raw text.
_IDENT = re.compile(r"^[a-z0-9_]{1,32}$")
_INT = re.compile(r"^-?\d{1,6}$")
_MAC = re.compile(r"^([0-9a-f]{2}[:-]?){5}[0-9a-f]{2}$", re.I)


def wake_on_lan(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Send one WoL magic packet (stdlib only; a single UDP datagram)."""
    raw = bytes.fromhex(re.sub(r"[:-]", "", mac))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(b"\xff" * 6 + raw * 16, (broadcast, port))


def _json(status: int, **body: object) -> web.Response:
    return web.json_response(body, status=status)


@web.middleware
async def guard(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Token check, then map library errors to HTTP statuses."""
    response = await _guarded(request, handler)
    # Own access log: path only, so ?token= never reaches the journal.
    log.info("%s %s %s %s", request.remote, request.method, request.path, response.status)
    return response


async def _guarded(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    if request.path not in OPEN_PATHS and not _authorized(request):
        return _json(401, ok=False, error="bad token")
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return exc
    except CommandInactiveError as err:  # e.g. picture setting with no signal / in standby
        return _json(409, ok=False, error="inactive", detail=str(err))
    except (UnknownCommandError, CommandFormError, CommandError) as err:
        return _json(400, ok=False, error="rejected", detail=str(err))
    except AuthenticationError as err:
        log.error("ADCP authentication failed: %s", err)
        return _json(502, ok=False, error="projector auth failed")
    except (AdcpConnectionError, AdcpTimeoutError, OSError) as err:
        return _json(503, ok=False, error="projector unreachable", detail=str(err))
    except AdcpError as err:
        return _json(502, ok=False, error="adcp error", detail=str(err))


def _session_value(token: str) -> str:
    """Cookie value derived from the token: never the token itself, and rotating
    RELAY_TOKEN signs every browser out."""
    return hmac.new(token.encode(), b"adcp-relay-ui-session-v1", hashlib.sha256).hexdigest()


def _has_session(request: web.Request) -> bool:
    token = request.app[TOKEN]
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(token and cookie) and hmac.compare_digest(cookie, _session_value(token))


def _authorized(request: web.Request) -> bool:
    expected = request.app[TOKEN]
    if not expected:
        return True
    given = request.query.get("token") or request.headers.get("X-Token", "")
    if given:
        return hmac.compare_digest(given, expected)
    # The cookie only counts with the page's own header: a cross-site form or link
    # cannot add one, and SameSite=Strict keeps the cookie off cross-site requests anyway.
    return request.headers.get(UI_HEADER) == "1" and _has_session(request)


def parse_auto_login(value: str | None) -> Networks:
    value = (value or "open").strip().lower()
    if value == "open":
        return OPEN_NETWORKS
    if value == "off":
        return ()
    return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in value.split(",") if part.strip())


def _auto_login_allowed(request: web.Request) -> bool:
    # request.remote is the TCP peer; forwarded-for headers are deliberately ignored.
    try:
        addr = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in request.app[AUTO_LOGIN])


def _set_session(response: web.Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, _session_value(token), max_age=365 * 24 * 3600,
        httponly=True, samesite="Strict", path="/",
    )


async def ui_session(request: web.Request) -> web.Response:
    token = request.app[TOKEN]
    if not token:
        return _json(200, ok=True, token_required=False, signed_in=True, auto=False)
    if _has_session(request):
        return _json(200, ok=True, token_required=True, signed_in=True, auto=_auto_login_allowed(request))
    if _auto_login_allowed(request):
        log.info("ui auto sign-in for %s", request.remote)
        response = _json(200, ok=True, token_required=True, signed_in=True, auto=True)
        _set_session(response, token)
        return response
    return _json(200, ok=True, token_required=True, signed_in=False, auto=False)


async def ui_login(request: web.Request) -> web.Response:
    token = request.app[TOKEN]
    try:
        body = await request.json()
    except ValueError:
        body = None
    given = body.get("token") if isinstance(body, dict) else None
    if not token or not isinstance(given, str) or not hmac.compare_digest(given, token):
        return _json(401, ok=False, error="bad token")
    response = _json(200, ok=True, signed_in=True)
    _set_session(response, token)
    return response


async def ui_logout(_: web.Request) -> web.Response:
    response = _json(200, ok=True, signed_in=False)
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


def _ident(value: str) -> str:
    value = value.lower()
    if not _IDENT.match(value):
        raise web.HTTPBadRequest(text="invalid value")
    return value


async def power_on(request: web.Request) -> web.Response:
    projector = request.app[PROJECTOR]
    mac = request.app[MAC]
    if mac:
        # A VW295ES with "Network management" off leaves the network in standby, and
        # only a magic packet (with "Remote start" On) brings it back. Sending it first
        # is harmless when the unit is reachable, and avoids waiting out a connect
        # timeout before falling back to it.
        await asyncio.to_thread(wake_on_lan, mac)
    try:
        await asyncio.wait_for(projector.power_on(), timeout=6 if mac else None)
    except (AdcpConnectionError, AdcpTimeoutError, OSError, TimeoutError):
        if not mac:
            raise
        return _json(202, ok=True, action="wake-on-lan sent; projector booting")
    return _json(200, ok=True, action="power on")


async def power_off(request: web.Request) -> web.Response:
    await request.app[PROJECTOR].power_off()
    return _json(200, ok=True, action="power off")


async def set_input(request: web.Request) -> web.Response:
    terminal = _ident(request.match_info["terminal"])
    await request.app[PROJECTOR].set_input(terminal)
    return _json(200, ok=True, input=terminal)


async def set_picmode(request: web.Request) -> web.Response:
    mode = _ident(request.match_info["mode"])
    await request.app[PROJECTOR].set_picture_mode(mode)
    return _json(200, ok=True, picture_mode=mode)


async def send_key(request: web.Request) -> web.Response:
    code = _ident(request.match_info["code"])
    await request.app[PROJECTOR].send_key(code)
    return _json(200, ok=True, key=code)


async def set_blank(request: web.Request) -> web.Response:
    state = request.match_info["state"].lower()
    if state not in ("on", "off"):
        raise web.HTTPBadRequest(text="use on or off")
    await request.app[PROJECTOR].set_blank(state == "on")
    return _json(200, ok=True, blank=state)


def _wait_deadline(request: web.Request) -> float:
    try:
        wait = float(request.query.get("wait", "0"))
    except ValueError:
        wait = math.nan
    if not math.isfinite(wait):
        raise web.HTTPBadRequest(text="wait must be a number of seconds")
    return asyncio.get_running_loop().time() + min(max(wait, 0.0), 120.0)


async def _until(deadline: float, call: Callable[[], Awaitable[object]]) -> object:
    """Run ``call``, retrying every 2 s while it answers err_inactive, until ``deadline``."""
    while True:
        try:
            return await call()
        except CommandInactiveError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(2)


async def _apply_setting(
    projector: Projector, deadline: float, name: str, raw: str, suffix: str | None
) -> object:
    """Set one ADCP setting, typed by the command table. Returns the value sent."""
    name = _ident(name)
    raw = str(raw).lower()
    if suffix is not None:
        suffix = _ident(suffix)
    kind = get_command(name).kind
    if kind is CommandKind.NUMBER:
        if not _INT.match(raw):
            raise web.HTTPBadRequest(text=f"{name} takes an integer")
        number = int(raw)
        await _until(deadline, lambda: projector.set_number(name, number, suffix))
        return number
    if kind is CommandKind.SELECT:
        value = _ident(raw)
        await _until(deadline, lambda: projector.set_select(name, value, suffix))
        return value
    raise web.HTTPBadRequest(text=f"{name} is {kind.value}, not settable here")


async def _retry_inactive(request: web.Request, call: Callable[[], Awaitable[object]]) -> object:
    """Run ``call``; with ?wait=N retry for up to N seconds while it answers err_inactive.

    Picture settings are inactive in standby and while there is no input signal,
    so an Activity that powers on and then sets a mode needs to wait for the unit.
    """
    try:
        wait = float(request.query.get("wait", "0"))
    except ValueError:
        wait = math.nan
    if not math.isfinite(wait):
        raise web.HTTPBadRequest(text="wait must be a number of seconds")
    wait = min(max(wait, 0.0), 120.0)
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        try:
            return await call()
        except CommandInactiveError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(2)


async def generic_set(request: web.Request) -> web.Response:
    """/set/<command>/<value>[?suffix=hdmi1][&wait=20] for any ADCP setting.

    Examples: /set/picture_mode/reference  /set/hdr/auto  /set/contrast/55
    The projector validates the name and value; nothing is forwarded as raw text.
    """
    suffix = request.query.get("suffix")
    value = await _apply_setting(
        request.app[PROJECTOR],
        _wait_deadline(request),
        request.match_info["command"],
        request.match_info["value"],
        suffix,
    )
    return _json(200, ok=True, command=request.match_info["command"], value=value, suffix=suffix)


async def _read_back(projector: Projector, command: str, suffix: str | None) -> dict[str, object]:
    """Current value of a setting, or why it could not be read."""
    try:
        return {"value": await projector.send(get_command(command).query(suffix))}
    except CommandInactiveError:
        return {"value": None, "error": "inactive (greyed out on the projector)"}
    except (AdcpConnectionError, AdcpTimeoutError, AuthenticationError, OSError):
        raise
    except AdcpError as err:
        return {"value": None, "error": str(err)}


def _same(reported: object, sent: object) -> bool:
    return str(reported).strip().lower() == str(sent).strip().lower()


async def _run_preset(request: web.Request, verify: bool) -> web.Response:
    """Apply each step in order, all sharing one ?wait= window.

    A step that fails is reported and the rest still run, so one setting the unit
    greys out does not leave the others unapplied. With ``verify`` every setting is
    read back right after it is set ("now") and once more after the last step
    ("final"), which catches a later step undoing an earlier one.
    """
    name = _ident(request.match_info["name"])
    steps = request.app[PRESETS].get(name)
    if steps is None:
        raise web.HTTPNotFound(text=f"no preset {name!r}")
    projector = request.app[PROJECTOR]
    deadline = _wait_deadline(request)
    results: list[dict[str, object]] = []
    for step in steps:
        command, value, suffix = step["command"], step["value"], step.get("suffix")
        try:
            sent = await _apply_setting(projector, deadline, command, value, suffix)
            entry: dict[str, object] = {"command": command, "value": sent, "suffix": suffix, "ok": True}
            if verify:
                now = await _read_back(projector, command, suffix)
                entry["now"] = {**now, "confirmed": "error" not in now and _same(now["value"], sent)}
            results.append(entry)
        except (AdcpConnectionError, AdcpTimeoutError, AuthenticationError, OSError):
            raise  # projector gone: the remaining steps would fail the same way
        except (AdcpError, UnknownCommandError, CommandFormError, web.HTTPBadRequest) as err:
            detail = err.text if isinstance(err, web.HTTPException) else str(err)
            log.warning("preset %s: %s %s failed: %s", name, command, value, detail)
            results.append(
                {"command": command, "value": value, "suffix": suffix, "ok": False, "error": detail}
            )
    if verify:
        last = {(r["command"], r["suffix"]): i for i, r in enumerate(results)}
        for i, entry in enumerate(results):
            if not entry["ok"]:
                continue
            if last[(entry["command"], entry["suffix"])] != i:
                entry["final"] = {"overridden": True}
                continue
            final = await _read_back(projector, str(entry["command"]), entry["suffix"])  # type: ignore[arg-type]
            entry["final"] = {**final, "confirmed": "error" not in final and _same(final["value"], entry["value"])}
        ok = all(
            r["ok"] and r["now"]["confirmed"] and (r["final"].get("overridden") or r["final"]["confirmed"])  # type: ignore[index,union-attr]
            for r in results
        )
        return _json(200, ok=ok, preset=name, verified=True, steps=results)
    ok = all(r["ok"] for r in results)
    return _json(200 if ok else 502, ok=ok, preset=name, steps=results)


async def apply_preset(request: web.Request) -> web.Response:
    """/preset/<name>[?wait=20]: 200 if every step landed."""
    return await _run_preset(request, verify=False)


async def verify_preset(request: web.Request) -> web.Response:
    """/preset/<name>/verify[?wait=20]: apply and read every setting back; always 200."""
    return await _run_preset(request, verify=True)


async def list_presets(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "presets": request.app[PRESETS],
        "editable": request.app[PRESETS_PATH] is not None,
    })


def _check_step(step: Step, caps: dict[str, dict[str, object]]) -> None:
    """Reject a step the API may not save; checked against /caps once it has been read."""
    name, value, suffix = step["command"], step["value"].lower(), step.get("suffix")
    if name not in PRESET_COMMANDS:
        raise web.HTTPBadRequest(text=f"{name} is not a picture or signal setting")
    command = get_command(name)
    if (suffix is None) != (not command.suffixes) or (suffix and suffix not in command.suffixes):
        raise web.HTTPBadRequest(text=f"{name}: suffix must be one of {list(command.suffixes)}")
    cap = caps.get(name)
    if cap is not None and suffix is not None:
        cap = {**cap, **(cap["by_suffix"].get(suffix) or {})}  # type: ignore[union-attr]
    if command.kind is CommandKind.SELECT:
        options = cap and cap.get("options")
        if not _IDENT.match(value) or (options and value not in options):
            raise web.HTTPBadRequest(text=f"{name}: {value!r} is not an option")
    elif command.kind is CommandKind.NUMBER:
        bounds = cap and cap.get("range")
        if not _INT.match(value) or (
            bounds and not bounds["min"] <= int(value) <= bounds["max"]  # type: ignore[index]
        ):
            raise web.HTTPBadRequest(text=f"{name}: {value!r} is out of range")
    else:
        raise web.HTTPBadRequest(text=f"{name} is {command.kind.value}, not settable")


def _write_presets(path: str, presets: Presets) -> None:
    """Replace the file atomically, keeping the previous version as <path>.bak."""
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(presets, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    if target.exists():
        target.with_name(target.name + ".bak").write_bytes(target.read_bytes())
    os.replace(tmp, target)


async def _store_presets(app: web.Application, presets: Presets) -> None:
    path = app[PRESETS_PATH]
    if path is None:
        raise web.HTTPConflict(text="PRESETS_FILE is not set; edits would not survive a restart")
    try:
        await asyncio.to_thread(_write_presets, path, presets)
    except OSError as err:
        log.error("saving presets to %s failed: %s", path, err)
        raise web.HTTPInternalServerError(text=f"could not save presets: {err.strerror}") from None
    app[PRESETS] = presets


async def save_preset(request: web.Request) -> web.Response:
    """PUT /presets/<name> with {"steps": [[command, value(, suffix)], ...]}."""
    name = _ident(request.match_info["name"])
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="body must be JSON") from None
    steps = body.get("steps") if isinstance(body, dict) else None
    try:
        checked = _check_presets({name: steps})[name]
    except ValueError as err:
        raise web.HTTPBadRequest(text=str(err)) from None
    for step in checked:
        _check_step(step, request.app[CAPS])
    async with request.app[PRESETS_LOCK]:
        await _store_presets(request.app, {**request.app[PRESETS], name: checked})
    return _json(200, ok=True, preset=name, steps=checked)


async def delete_preset(request: web.Request) -> web.Response:
    name = _ident(request.match_info["name"])
    async with request.app[PRESETS_LOCK]:
        presets = dict(request.app[PRESETS])
        if presets.pop(name, None) is None:
            raise web.HTTPNotFound(text=f"no preset {name!r}")
        await _store_presets(request.app, presets)
    return _json(200, ok=True, deleted=name)


async def ui_version(_: web.Request) -> web.Response:
    """Changes when ui.html is edited or the relay restarts, so open pages can reload."""
    digest = hashlib.sha256(await asyncio.to_thread(UI_FILE.read_bytes)).hexdigest()[:12]
    return _json(200, ok=True, version=f"{BOOT_ID}-{digest}")


async def ui(_: web.Request) -> web.Response:
    return web.FileResponse(UI_FILE, headers={"Cache-Control": "no-cache"})


async def generic_get(request: web.Request) -> web.Response:
    """/get/<command>[?suffix=...] returns the current value."""
    projector = request.app[PROJECTOR]
    name = _ident(request.match_info["command"])
    suffix = request.query.get("suffix")
    if suffix is not None:
        suffix = _ident(suffix)
    line = get_command(name).query(suffix)
    value = await _retry_inactive(request, lambda: projector.send(line))
    return _json(200, ok=True, command=name, value=value)


async def read_values(request: web.Request) -> web.Response:
    """/values?names=picture_mode,col_corr_hue:r,...: current values in one round trip for the UI.

    Only picture and signal settings; a greyed-out one (standby, no signal) reads as null.
    """
    projector = request.app[PROJECTOR]
    values: dict[str, object] = {}
    errors: dict[str, str] = {}
    for key in filter(None, request.query.get("names", "").split(",")[:200]):
        command, _, suffix = key.partition(":")
        command = _ident(command)
        if command not in PRESET_COMMANDS:
            raise web.HTTPBadRequest(text=f"{command} is not a picture or signal setting")
        result = await _read_back(projector, command, _ident(suffix) if suffix else None)
        values[key] = result["value"]
        if "error" in result:
            errors[key] = str(result["error"])
    return _json(200, ok=True, values=values, errors=errors)


async def capabilities(request: web.Request) -> web.Response:
    """/caps: what this unit supports (options and ranges); ~200 round trips on first call."""
    caps = (await request.app[PROJECTOR].capabilities()).to_dict()
    request.app[CAPS] = caps["commands"]
    preset_commands = sorted(n for n in caps["commands"] if n in PRESET_COMMANDS)
    return web.json_response({"ok": True, **caps, "preset_commands": preset_commands})


async def status(request: web.Request) -> web.Response:
    projector = request.app[PROJECTOR]
    power: PowerStatus = await projector.power_status()
    body: dict[str, object] = {"ok": True, "power": str(power)}
    if power is PowerStatus.ON:
        try:
            body["input"] = await projector.get_input()
        except CommandInactiveError:
            body["input"] = None
    return web.json_response(body)


async def healthz(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _close(app: web.Application) -> None:
    await app[PROJECTOR].close()


def _check_presets(presets: object) -> Presets:
    """Fail at startup, not at press time, on a malformed preset file."""
    if not isinstance(presets, dict):
        raise ValueError("presets must be a JSON object of name -> steps")
    checked: Presets = {}
    for name, steps in presets.items():
        if not _IDENT.match(str(name)) or not isinstance(steps, list) or not steps:
            raise ValueError(f"preset {name!r}: needs a simple name and a non-empty step list")
        checked[str(name)] = []
        for step in steps:
            if (
                not isinstance(step, dict)
                or not {"command", "value"} <= step.keys() <= STEP_KEYS
                or not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in step.values())
            ):
                raise ValueError(
                    f"preset {name!r}: step {step!r}: want {{\"command\": ..., \"value\": ...}}"
                    f" with an optional \"suffix\" and nothing else"
                )
            get_command(str(step["command"]))  # UnknownCommandError for a typo
            checked[str(name)].append({k: str(step[k]) for k in ("command", "value", "suffix") if k in step})
    return checked


def build_app(
    host: str,
    password: str,
    mac: str | None = None,
    token: str | None = None,
    port: int = 53595,
    presets: Presets | None = None,
    presets_path: str | None = None,
    auto_login: Networks = (),
) -> web.Application:
    if mac and not _MAC.match(mac):
        raise ValueError(f"invalid PROJECTOR_MAC: {mac!r}")
    app = web.Application(middlewares=[guard])
    # One long-lived Projector: pyadcp serialises commands and reconnects itself.
    app[PROJECTOR] = Projector(host, port, password)
    app[MAC] = mac
    app[TOKEN] = token
    app[AUTO_LOGIN] = auto_login
    if token and auto_login == OPEN_NETWORKS:
        log.warning("UI_AUTO_LOGIN is open: any browser that reaches /ui is signed in without the "
                    "token. Set UI_AUTO_LOGIN to your networks (e.g. 192.168.1.0/24) or off.")
    elif token and auto_login:
        log.info("UI auto sign-in for %s", ", ".join(map(str, auto_login)))
    app[PRESETS] = _check_presets(presets if presets is not None else DEFAULT_PRESETS)
    app[PRESETS_PATH] = presets_path
    app[PRESETS_LOCK] = asyncio.Lock()
    app[CAPS] = {}
    routes: list[tuple[str, Callable[[web.Request], Awaitable[web.Response]]]] = [
        ("/power/on", power_on),
        ("/power/off", power_off),
        ("/input/{terminal}", set_input),
        ("/picmode/{mode}", set_picmode),
        ("/key/{code}", send_key),
        ("/blank/{state}", set_blank),
        ("/set/{command}/{value}", generic_set),
        ("/get/{command}", generic_get),
        ("/values", read_values),
        ("/preset/{name}", apply_preset),
        ("/preset/{name}/verify", verify_preset),
        ("/presets", list_presets),
        ("/caps", capabilities),
        ("/status", status),
        ("/healthz", healthz),
    ]
    for path, handler in routes:
        app.router.add_route("GET", path, handler)
        app.router.add_route("POST", path, handler)
    # Edits are PUT/DELETE only, so a stray GET (or a remote button) never changes them.
    app.router.add_route("PUT", "/presets/{name}", save_preset)
    app.router.add_route("DELETE", "/presets/{name}", delete_preset)
    app.router.add_route("GET", "/ui", ui)
    app.router.add_route("GET", "/ui/session", ui_session)
    app.router.add_route("GET", "/ui/version", ui_version)
    app.router.add_route("POST", "/ui/login", ui_login)
    app.router.add_route("POST", "/ui/logout", ui_logout)
    app.on_cleanup.append(_close)
    return app


def _load_presets(path: str | None) -> Presets | None:
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        loaded: Presets = json.load(fh)
    return loaded


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    app = build_app(
        os.environ["PROJECTOR_HOST"],
        os.environ.get("PROJECTOR_PASSWORD", "Projector"),
        os.environ.get("PROJECTOR_MAC") or None,
        os.environ.get("RELAY_TOKEN") or None,
        presets=_load_presets(os.environ.get("PRESETS_FILE")),
        presets_path=os.environ.get("PRESETS_FILE") or None,
        auto_login=parse_auto_login(os.environ.get("UI_AUTO_LOGIN")),
    )
    web.run_app(
        app,
        host=os.environ.get("RELAY_BIND", "0.0.0.0"),
        port=int(os.environ.get("RELAY_PORT", "8080")),
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
