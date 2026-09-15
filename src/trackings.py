import time

import requests

from model import parse_datetime


def normalize_tracking(t, handle):
    return {
        "id": t.get("id"),
        "user_id": t.get("userId") or t.get("user_id"),
        "handle": handle,
        "title": t.get("title"),
        "start_date": t.get("startDate") or t.get("start_date"),
        "end_date": t.get("endDate") or t.get("end_date"),
        "target": t.get("target"),
        "market_link": t.get("marketLink") or t.get("market_link"),
        "is_active": t.get("isActive", t.get("is_active", True)),
    }


def fetch_trackings(base, handle, retries=3, timeout=20):
    url = f"{base}/api/users/{handle}/trackings"
    params = {"platform": "x", "activeOnly": True}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}"
                wait = attempt * 10
                print(f"  ⏳ {last_error} en xtracker; reintentando en {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            if not data.get("success"):
                raise ValueError(data.get("error") or "Respuesta sin success")
            return data.get("data", [])
        except requests.RequestException as err:
            last_error = str(err)
            if attempt == retries:
                break
            time.sleep(attempt * 10)
    raise RuntimeError(f"No se pudieron obtener trackings: {last_error}")


def sync_trackings(supabase, trackings, handle, now):
    records = []
    for t in trackings:
        records.append({
            "id": t.get("id"),
            "user_id": t.get("userId"),
            "handle": handle,
            "platform": "x",
            "title": t.get("title"),
            "start_date": parse_datetime(t.get("startDate")).isoformat(),
            "end_date": parse_datetime(t.get("endDate")).isoformat(),
            "target": t.get("target"),
            "market_link": t.get("marketLink"),
            "is_active": t.get("isActive", True),
            "metrics": t.get("metrics") or {},
            "config": t.get("config") or {},
            "created_at_xtracker": (
                parse_datetime(t.get("createdAt")).isoformat()
                if t.get("createdAt") else None
            ),
            "updated_at_xtracker": (
                parse_datetime(t.get("updatedAt")).isoformat()
                if t.get("updatedAt") else None
            ),
            "synced_at": now.isoformat(),
        })
    if records:
        supabase.table("polymarket_trackings").upsert(
            records, on_conflict="id"
        ).execute()
    return len(records)