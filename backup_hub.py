#!/usr/bin/env python3
"""Read-only: take a verified full backup of a Sofabaton hub. Changes nothing.

Run on the hub's LAN with the Sofabaton app fully closed everywhere:
    python backup_hub.py --hub-ip 192.168.1.40
Writes hub_backup_<timestamp>.json and exits non-zero if the backup is incomplete.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from sofabaton import AsyncXProxy


async def main(hub_ip: str) -> int:
    async with AsyncXProxy(hub_ip=hub_ip) as proxy:
        if not await proxy.wait_until_controllable(timeout=30):
            print("Hub not controllable: close the Sofabaton app everywhere.", file=sys.stderr)
            return 1
        print("Reading the whole hub (can take a few minutes)...")
        await proxy.refresh(progress=lambda p: print(f"  read {p}", flush=True))
        devices = await proxy.devices()
        activities = await proxy.activities()
        bundle = await proxy.backup()

        n_dev, n_act = len(bundle["devices"]), len(bundle["activities"])
        path = f"hub_backup_{time.strftime('%Y%m%d-%H%M%S')}.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, indent=2)
        print(f"\nWrote {path}: {n_dev}/{len(devices)} devices, {n_act}/{len(activities)} "
              f"activities, complete={bundle['complete']}, schema v{bundle['schema_version']}")
        for d in devices:
            print(f"  device {d.device_id:3}  {d.device_class:<10} {d.name}")
        for a in activities:
            print(f"  activity {a.activity_id:3}  {a.name}")

        if n_dev != len(devices) or n_act != len(activities) or not bundle["complete"]:
            print("\nINCOMPLETE backup: do not rely on it. Wait a minute and run again.",
                  file=sys.stderr)
            return 1
        print("\nBackup verified.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--hub-ip", required=True)
    sys.exit(asyncio.run(main(p.parse_args().hub_ip)))
