# adcp-relay

Control a Sony projector from a Sofabaton X2 remote over the network, with named
picture presets and a small web page for editing them.

The relay is a single Python process. It takes plain HTTP requests, which is
what a Sofabaton Wifi device sends, and turns them into Sony ADCP commands
using [pyadcp](https://github.com/johnmschoonover/sony-adcp).

```
Sofabaton X2 hub ── HTTP GET + X-Token ──▶ relay.py :8080 ── ADCP (TCP 53595) ──▶ Sony projector
                                               ▲
browser ── /ui (preset editor) ────────────────┘
```

## Intended use

- **Reliable power.** Separate power-on and power-off commands replace an IR
  toggle that can get out of sync.
- **Picture presets per Activity.** Pressing a PS5 Activity can switch the
  projector to Game mode with Input lag reduction on and Motionflow off.
  Pressing a Blu-ray Activity can switch it back to Reference or Cinema Film 1.
  A preset is an ordered list of settings, and the relay applies them in order.
- **Editing presets without touching the remote.** The hub only stores a URL
  such as `/preset/game`. What that preset does lives on the relay and can be
  changed from the web page at any time.

It is built for one home theater on a trusted LAN. It is not a multi-user
service, and it is not hardened for exposure to the internet.

Tested with a **Sony VPL-VW295ES** and a **Sofabaton X2**. Other Sony projectors
that speak ADCP should work, but expect differences (see [Known gaps](#known-gaps)).

## Requirements

- A Sony projector with ADCP enabled (Network settings on the projector; default port 53595).
- Python 3.12 or newer on an always-on host on the same network. A small LXC
  container or VM is plenty.
- A Sofabaton X2 (or another hub whose network devices can send HTTP GET
  requests with a custom header).
- The hub must be able to reach the relay's port. They can be on different
  subnets if those subnets are routed to each other.

## Setup

### 1. Prepare the projector

- Enable ADCP and note the port (53595 by default).
- Note whether ADCP authentication is on. If it is, the relay needs the
  projector's administrator password. If you turned authentication off, leave
  the password empty.
- Make sure the projector can be woken over the network. On the VPL-VW295ES,
  with network management off, the network interface sleeps in standby and only
  a Wake-on-LAN packet brings it back. Either turn network management on, or
  turn Remote start on and give the relay the projector's MAC address (see
  `PROJECTOR_MAC` below). Menu names differ between models; check your manual.

### 2. Install the relay

On a Debian or Ubuntu host (run as root):

```sh
useradd --system --home /opt/adcp-relay --shell /usr/sbin/nologin adcp
mkdir -p /opt/adcp-relay
cp relay.py ui.html /opt/adcp-relay/
python3 -m venv /opt/adcp-relay/.venv
/opt/adcp-relay/.venv/bin/pip install aiohttp pyadcp
```

Create `/etc/adcp-relay.env`, readable only by root:

```sh
PROJECTOR_HOST=192.168.1.50          # the projector's IP address
PROJECTOR_PASSWORD=                  # empty if ADCP authentication is off
PROJECTOR_MAC=                       # optional, enables Wake-on-LAN
RELAY_TOKEN=                         # openssl rand -hex 16
RELAY_PORT=8080
PRESETS_FILE=/var/lib/adcp-relay/presets.json
UI_AUTO_LOGIN=192.168.1.0/24         # see "Signing in to the web page"
LOG_LEVEL=INFO
```

```sh
chmod 600 /etc/adcp-relay.env
```

Create `/etc/systemd/system/adcp-relay.service`:

```ini
[Unit]
Description=Sofabaton X2 -> Sony ADCP relay
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=adcp
Group=adcp
EnvironmentFile=/etc/adcp-relay.env
ExecStart=/opt/adcp-relay/.venv/bin/python /opt/adcp-relay/relay.py
Restart=on-failure
RestartSec=5
StateDirectory=adcp-relay
StateDirectoryMode=0750
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
LockPersonality=yes
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

`ProtectSystem=strict` makes the whole filesystem read-only to the service.
`StateDirectory=` gives it one writable directory, `/var/lib/adcp-relay`, which
is where the presets file lives.

Seed the presets file and start the relay:

```sh
install -d -o adcp -g adcp -m 750 /var/lib/adcp-relay
install -o adcp -g adcp -m 640 presets.json /var/lib/adcp-relay/presets.json
systemctl daemon-reload
systemctl enable --now adcp-relay
```

Check that the relay is up and can reach the projector:

```sh
. /etc/adcp-relay.env
curl -s localhost:8080/healthz                                  # {"ok": true}
curl -s -H "X-Token: $RELAY_TOKEN" localhost:8080/status        # {"ok": true, "power": "standby"}
curl -s -H "X-Token: $RELAY_TOKEN" localhost:8080/caps | head   # what this model supports
```

### Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `PROJECTOR_HOST` | required | Projector IP address or hostname. |
| `PROJECTOR_PASSWORD` | `Projector` | ADCP password; set it empty when ADCP authentication is off. |
| `PROJECTOR_MAC` | unset | Sends a Wake-on-LAN packet before power-on. |
| `RELAY_TOKEN` | unset | Shared secret. Every request needs it as an `X-Token` header or `?token=`, except `/healthz` and the web page's sign-in routes. Unset means no authentication at all. |
| `RELAY_PORT` / `RELAY_BIND` | `8080` / `0.0.0.0` | Listen address. |
| `PRESETS_FILE` | unset | JSON file of presets. Without it the built-in `game` and `movie` presets are used, and edits from the web page cannot be saved. |
| `UI_AUTO_LOGIN` | `open` | Which browsers are signed in to the web page without the token: `open` (any address, logged as a warning), `off`, or comma-separated networks such as `192.168.1.0/24`. |
| `LOG_LEVEL` | `INFO` | Python logging level. The access log records paths only, never `?token=`. |

### 3. Add the projector to the Sofabaton

In the Sofabaton app, add a network (Wi-Fi/IP) device that sends HTTP
requests, and give it one URL command per button. Use method **GET** and add a header named
`X-Token` set to your `RELAY_TOKEN`:

| Command | URL |
|---|---|
| Power On | `http://<relay>:8080/power/on` |
| Power Off | `http://<relay>:8080/power/off` |
| Game Mode | `http://<relay>:8080/preset/game?wait=20` |
| Movie Mode | `http://<relay>:8080/preset/movie?wait=20` |

The web page's **Links** button lists these URLs for every saved preset, with
copy buttons.

Then:

- Set the device's power-on and power-off to the Power On and Power Off commands.
- In each Activity, add the preset command after the projector's power-on step.
  `?wait=20` makes the relay retry for up to 20 seconds while the projector is
  still warming up, but a delay of a few seconds after power-on in the Activity
  helps too.

A Wifi device holds at most 10 commands, so presets are the way to get many
settings behind one button.

#### Optional: provision the hub from a script

`provision_projector.py` creates the Wifi device and its URL commands
directly on the hub. `backup_hub.py` takes a full, verified, read-only backup of
the hub first. Both use [sofabaton-x](https://github.com/m3tac0de/home-assistant-sofabaton-x1s),
an unofficial library built by reverse-engineering the hub's protocol.

```sh
python3 -m venv /opt/sofabaton-tools
/opt/sofabaton-tools/bin/pip install "sofabaton-x>=0.2.6,<0.3"

/opt/sofabaton-tools/bin/python backup_hub.py --hub-ip <hub-ip>
/opt/sofabaton-tools/bin/python provision_projector.py --hub-ip <hub-ip> \
    --relay http://<relay>:8080 --token "$RELAY_TOKEN" --name "Sony Projector"            # dry run
/opt/sofabaton-tools/bin/python provision_projector.py ... --apply                         # write
/opt/sofabaton-tools/bin/python provision_projector.py ... --device-id <id> --apply        # add missing commands to an existing device
```

- **Close the Sofabaton app on every phone first.** The hub accepts only one
  controller at a time.
- **Run them on a host the hub can connect back to.** The hub opens a TCP
  connection to the client on port 8200.
- **Back up before every write.** The hub has no undo.

## Using the web page

Open `http://<relay>:8080/ui`.

**Preset buttons.** The presets appear as a row of buttons under the projector
name. A dot marks unsaved changes. **+ Add preset** creates an empty preset, and
**Copy to new** duplicates the current one.

**Editing a preset.** Each row is one setting, with its value in a dropdown or
number field. Drag the six-dot handle to reorder rows; with the handle focused,
↑ and ↓ also work. × removes a row. A setting itself cannot be swapped for a
different one; remove the row and add the new setting instead.

**Add settings…** lists every picture and signal setting this projector
supports, filled in with its current values. Change the ones the preset should
set. Changed rows move to a **To add** section at the top, where you can adjust
them before adding. New rows are placed to suit the ordering rules below.

**Ordering warnings.** A **!** appears next to a row whose position is known to
cause trouble:

- a picture setting above **Picture mode** (most settings are stored per
  picture mode, so it would change the previous mode);
- a picture setting above **Input** (settings are stored per input);
- Motionflow or Reality Creation below **Input lag reduction on** (the projector
  refuses them once it is on);
- the same setting twice (the earlier one is overridden).

**Apply now** sends the preset to the projector. **Validate** applies it and
reads every setting back twice: right after setting it, and again after the last
step. That catches values the projector silently ignored and values a later step
undid.

**Save** writes the preset to `PRESETS_FILE` and keeps the previous version as
`presets.json.bak`. **Delete** asks for confirmation, because remote buttons that
use the preset stop working.

The page reloads itself when the relay is updated, unless there are unsaved
edits, in which case it shows an **Updated — reload** button instead.

### Signing in to the web page

With `RELAY_TOKEN` set, a browser either signs in once with the token or is
signed in automatically when its address matches `UI_AUTO_LOGIN`. Either way it
then holds a login cookie:

- it is derived from the token, not the token itself;
- page scripts cannot read it (`HttpOnly`), and other sites cannot make the
  browser send it (`SameSite=Strict`);
- it is only accepted together with a header the page adds, so a link or form on
  another site cannot use it;
- it lasts a year, and changing `RELAY_TOKEN` cancels every cookie.

The page itself never receives the token. Remote-control requests are
unaffected: anything without a login cookie still needs `X-Token`.

`UI_AUTO_LOGIN` defaults to `open`, which signs in *any* browser that can reach
the port, and the relay logs a warning about it at startup. Set it to your LAN's
network, or to `off`.

## HTTP API

Every control route accepts GET or POST and needs the token.

| Route | Does |
|---|---|
| `/power/on`, `/power/off` | Power. Power-on sends Wake-on-LAN first when `PROJECTOR_MAC` is set. |
| `/preset/<name>[?wait=N]` | Apply a preset. Returns 200 if every step landed, 502 with per-step results otherwise. |
| `/preset/<name>/verify[?wait=N]` | Apply and read every setting back. Always 200, with per-step results. |
| `/input/<terminal>`, `/picmode/<mode>`, `/blank/<on\|off>`, `/key/<code>` | Single actions. |
| `/set/<command>/<value>[?suffix=&wait=]` | Set any ADCP setting, typed by pyadcp's command table. |
| `/get/<command>[?suffix=]` | Read any ADCP setting. |
| `/values?names=a,b:suffix` | Read several picture settings at once (null when greyed out). |
| `/presets` | List presets. `PUT` / `DELETE /presets/<name>` save or remove one; a GET can never change them. |
| `/caps` | What this projector supports. The first call makes about 200 round trips. |
| `/status` | Power state and input; never changes anything. |
| `/healthz` | Relay liveness, no token needed; does not contact the projector. |

`?wait=N` (up to 120 seconds) retries settings the projector refuses while it
starts up or has no signal.

### Presets file

```json
{
  "game": [
    ["picture_mode", "game"],
    ["motionflow", "off"],
    ["real_cre", "off"],
    ["input_lag_red", "on"]
  ],
  "movie": [
    ["input_lag_red", "off"],
    ["picture_mode", "reference"]
  ]
}
```

Each step is `[command, value]` or `[command, value, suffix]`, using pyadcp's
command names; `/caps` lists the options your model accepts. The relay checks
the file at startup and refuses to start on a typo. Presets saved through the
web page or API may only use picture and signal settings. Network, lock and
setup commands are excluded so a preset cannot cut the relay off from the
projector.

## Known gaps

**Scope**

- **One projector per relay.** For a second projector, run a second instance on
  another host or port, with its own token and presets file.
- **Tested on one model.** Only the VPL-VW295ES has been tried. Other models may
  name options differently or support fewer settings. The editor reads options
  from the projector, but the allowed-settings list and the ordering rules are
  written for Sony's home-theater menus.
- **The ordering rules are observations, not specifications.** "Stored per
  picture mode" and "Input lag reduction disables Motionflow and Reality
  Creation" come from how the VW295ES behaves. Use **Validate** to confirm a
  preset on your unit.
- **No factory defaults.** ADCP cannot report them, so Add settings… starts from
  the projector's current values. In standby, picture settings cannot be read at
  all, and those rows start blank.

**Security**

- **Plain HTTP.** The token and login cookie cross the network unencrypted. Keep
  the relay on a trusted LAN, or put it behind a TLS reverse proxy. Behind a
  proxy, `UI_AUTO_LOGIN` sees the proxy's address, not the browser's.
- **`UI_AUTO_LOGIN=open` is the default.** It is convenient, but anyone who can
  reach the port can use the web page.
- **`?token=` in URLs** can end up in other systems' logs. Prefer the `X-Token`
  header.

**Presets and editing**

- **Renaming or deleting a preset breaks remote buttons** that use its URL.
  There is no rename; use Copy to new and then delete the old one.
- **Last save wins.** Two browsers editing the same preset overwrite each other.
  Only one previous version is kept (`.bak`), and there is no undo.
- **Validate can report false failures** if a projector applies a setting a
  moment after acknowledging it. Run it again before trusting a single failure.
- **Validate and Apply change the projector immediately.** There is no
  dry run against the real unit.
- **The copy button falls back to the browser's legacy copy command** on plain
  HTTP. Some browsers may refuse it; the URL is left selected so you can press
  ⌘C or Ctrl+C.

**Sofabaton**

- **No error feedback on the remote.** The hub does not show whether an HTTP
  command failed. Check the relay's journal (`journalctl -u adcp-relay`).
- **The hub must reach the relay.** On a separate IoT network, allow the hub to
  connect to the relay's port.
- **The provisioning scripts use an unofficial, reverse-engineered library.**
  A hub firmware update could break them. Setting the device up by hand in the
  Sofabaton app always works.

**Project**

- **No automated tests in this repository.** Changes were checked against the
  fake projector in pyadcp's test suite and on one real VPL-VW295ES.
- **Wake-on-LAN** uses a broadcast, so the relay host must be on the same layer-2
  network as the projector for it to work.

## Files

| File | Purpose |
|---|---|
| `relay.py` | The relay: HTTP routes, presets, web-page sign-in. |
| `ui.html` | The web page, served at `/ui`. |
| `presets.json` | Example presets (`game`, `movie`). |
| `provision_projector.py` | Optional: create the Sofabaton Wifi device from a script. |
| `backup_hub.py` | Optional: full read-only backup of a Sofabaton hub. |

## Not affiliated

Not affiliated with or endorsed by Sony or Sofabaton. Product names are
trademarks of their owners.
