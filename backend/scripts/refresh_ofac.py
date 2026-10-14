"""Download the public OFAC list files into ONBOARDIQ_OFAC_DATA_DIR and (optionally) tell a running API to reload.

    python -m scripts.refresh_ofac                    # download only
    python -m scripts.refresh_ofac --notify http://localhost:8080 --api-key ...   # + POST /admin/screening/refresh

Source: https://sanctionslist.ofac.treas.gov/ (SDN and Consolidated lists, CSV). Files are written atomically and the
previous set is kept as *.bak so a partial download never breaks screening. Run daily (Container Apps job / cron).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import httpx

from app.config import get_settings

BASE = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/"
FILES = {"sdn.csv": "SDN.CSV", "alt.csv": "ALT.CSV", "cons_prim.csv": "CONS_PRIM.CSV", "cons_alt.csv": "CONS_ALT.CSV"}
LEGACY = "https://www.treasury.gov/ofac/downloads/"  # older mirror paths: sdn.csv, alt.csv, consolidated/cons_prim.csv ...


def download(dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        for local, remote in FILES.items():
            urls = [BASE + remote, LEGACY + (f"consolidated/{local}" if local.startswith("cons") else local)]
            data = None
            for u in urls:
                try:
                    r = c.get(u)
                    if r.status_code == 200 and len(r.content) > 1000:
                        data = r.content
                        break
                except httpx.HTTPError:
                    continue
            if data is None:
                print(f"!! could not download {local} from {urls}", file=sys.stderr)
                continue
            target = dest / local
            if target.exists():
                shutil.copy2(target, target.with_suffix(".csv.bak"))
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
            total += 1
            print(f"{local}: {len(data)/1024:.0f} KB")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=None)
    ap.add_argument("--notify", default=None, help="API base URL to POST /admin/screening/refresh")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--bearer", default=None)
    a = ap.parse_args()
    dest = Path(a.dir) if a.dir else Path(get_settings().ofac_data_dir)
    n = download(dest)
    if n == 0:
        return 1
    if a.notify:
        headers = {"X-API-Key": a.api_key} if a.api_key else {"Authorization": f"Bearer {a.bearer}"} if a.bearer else {}
        r = httpx.post(a.notify.rstrip("/") + "/admin/screening/refresh", headers=headers, timeout=120)
        print("refresh:", r.status_code, r.text[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
