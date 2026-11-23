#!/usr/bin/env python3
"""Create a Sofabaton X2 Wifi (URL) device whose commands call the ADCP relay.

Run on a host on the same LAN as the hub (the hub opens TCP back to this host
on port 8200), with the official Sofabaton app fully closed on every phone.

    python provision_projector.py --hub-ip 192.168.1.40 \
        --relay http://192.168.1.60:8080 --token "$RELAY_TOKEN"          # dry run
    python provision_projector.py ... --apply                            # write

Every run first writes a full hub backup (hub_backup_<timestamp>.json).
Edit COMMANDS below to taste; use option names from the relay's /caps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from sofabaton import AsyncXProxy, NetworkCommand, edits

DEVICE_NAME = "Projector"

# (label on the remote, relay path). Max 10 per Wifi device.
COMMANDS: list[tuple[str, str]] = [
    ("Power On", "/power/on"),
    ("Power Off", "/power/off"),
    ("Game Mode", "/preset/game?wait=20"),
    ("Movie Mode", "/preset/movie?wait=20"),
]


def build_commands(relay: str, token: str | None) -> list[tuple[str, NetworkCommand]]:
    headers = [("X-Token", token)] if token else []
    out = []
    for label, path in COMMANDS:
        url = relay.rstrip("/") + path
        out.append((label, NetworkCommand.url(url=url, method="GET", headers=headers)))
    return out


async def main(args: argparse.Namespace) -> int:
    if len(COMMANDS) > 10:
        print("A Wifi device holds at most 10 commands.", file=sys.stderr)
        return 2
    commands = build_commands(args.relay, args.token)  # validates before touching the hub

    proxy = AsyncXProxy(hub_ip=args.hub_ip)
    async with proxy:
        if not await proxy.wait_until_controllable(timeout=30):
            print("Hub not controllable: is the Sofabaton app fully closed everywhere?",
                  file=sys.stderr)
            return 1

        # Load the full catalog first: a backup taken before the device list has
        # arrived silently omits every device (seen as "0 devices, complete=True").
        await proxy.refresh()
        devices = await proxy.devices()
        backup_path = f"hub_backup_{time.strftime('%Y%m%d-%H%M%S')}.json"
        bundle = await proxy.backup()
        with open(backup_path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
        print(f"Backup: {backup_path} ({len(bundle['devices'])} devices, "
              f"{len(bundle['activities'])} activities, complete={bundle['complete']})")
        if not devices or len(bundle["devices"]) != len(devices):
            print(f"Backup holds {len(bundle['devices'])} of {len(devices)} devices; it would "
                  f"not restore your setup. Not continuing. Re-run in a minute.", file=sys.stderr)
            return 1
        for dev in devices:
            print(f"  device {dev.device_id:3}  {dev.device_class:<10} {dev.name}")
        existing: set[str] = set()
        if args.device_id is not None:
            if not any(d.device_id == args.device_id for d in devices):
                print(f"No device {args.device_id} on the hub.", file=sys.stderr)
                return 1
            existing = {c.label for c in await proxy.commands(args.device_id)}
            commands = [(l, c) for l, c in commands if l not in existing]
        elif any(d.name == args.name for d in devices):
            print(f"A device named {args.name!r} already exists; delete it in the app "
                  f"or pass --name.", file=sys.stderr)
            return 1

        if len(existing) + len(commands) > 10:
            print("A Wifi device holds at most 10 commands.", file=sys.stderr)
            return 2
        target = f"device {args.device_id}" if args.device_id is not None else repr(args.name)
        print(f"\nPlanned commands for Wifi {target} (already there: {sorted(existing)}):")
        for label, cmd in commands:
            print(f"  {label:<10} GET {cmd.fields['url']}"
                  + ("   [X-Token]" if args.token else ""))
        if not args.apply:
            print("\nDry run. Re-run with --apply to create it.")
            return 0

        if args.device_id is not None:
            device_id = args.device_id
        else:
            device_id = await proxy.add_device(args.name, "wifi_ip")
            print(f"\nCreated device {device_id}; writing commands...")
        snap = await proxy.refresh(device_id=device_id)
        edited = snap.bundle
        for label, cmd in commands:
            edited, _ = edits.add_command(edited, device_id, cmd, label)
        result = await proxy.sync_device(
            baseline=snap.bundle, edited=edited, device_id=device_id,
            snapshot_id=snap.snapshot_id, strict=True,
        )
        if not result.ok:
            print(f"Sync stopped at {result.failed_at}: {result.message}", file=sys.stderr)
            print(f"Device {device_id} may be half-written; delete it in the app and retry.",
                  file=sys.stderr)
            return 1
        for cmd in await proxy.commands(device_id):
            print(f"  command {cmd.command_id}: {cmd.label}")
        print("\nDone. The remote picks the device up after its sync (can take a few minutes).")
        print("Add it to your Activities in the Sofabaton app.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hub-ip", required=True)
    p.add_argument("--relay", required=True, help="e.g. http://192.168.1.60:8080")
    p.add_argument("--token", default=None, help="the relay's RELAY_TOKEN, sent as X-Token")
    p.add_argument("--name", default=DEVICE_NAME)
    p.add_argument("--device-id", type=int, default=None,
                   help="append missing commands to this existing Wifi device")
    p.add_argument("--apply", action="store_true", help="actually create the device")
    sys.exit(asyncio.run(main(p.parse_args())))
