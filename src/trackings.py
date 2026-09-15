import os
import re
import time

import requests

from model import buckets_from_labels, parse_datetime

GAMMA_BASE = os.getenv("POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com")


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
        "config": t.get("config") or {},
        "buckets": t.get("buckets") or (t.get("config") or {}).get("buckets"),
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


def event_slug_from_link(market_link):
    if not market_link:
        return None
    parts = str(market_link).rstrip("/").split("/")
    return parts[-1] if parts else None


def find_event_slug_by_title(title, retries=2, timeout=20):
    """Busca el slug del evento en Polymarket por título (fallback sin market_link)."""
    if not title:
        return None
    url = f"{GAMMA_BASE}/public-search"
    params = {"q": title, "limit": 5}
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            events = data.get("events") or []
            for event in events:
                slug = event.get("slug")
                if slug:
                    return slug
            return None
        except requests.RequestException:
            if attempt == retries:
                return None
            time.sleep(3)
    return None


def fetch_event_buckets(slug, retries=3, timeout=20):
    url = f"{GAMMA_BASE}/events/slug/{slug}"
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {response.status_code}"
                print(f"  ⏳ {last_error} en gamma-api; reintentando en {attempt * 5}s...")
                time.sleep(attempt * 5)
                continue
            response.raise_for_status()
            event = response.json()
            labels = []
            for m in event.get("markets") or []:
                label = (m.get("groupItemTitle") or "").strip()
                if not label:
                    match = re.search(
                        r"post\s+(<{0,1}\d[\d,\-+]*)\s+tweets", m.get("question") or "", re.IGNORECASE
                    )
                    if match:
                        label = match.group(1).strip()
                if label:
                    labels.append(label)
            if not labels:
                raise ValueError("El evento no expone markets/buckets")
            return buckets_from_labels(labels)
        except requests.RequestException as err:
            last_error = str(err)
            if attempt == retries:
                break
            time.sleep(attempt * 5)
    raise RuntimeError(f"No se pudieron obtener buckets de {slug}: {last_error}")


def ensure_buckets(supabase, trackings, write=True, sleep_between=0.2):
    """Llena tracking['buckets'] desde la Gamma API para los que faltan."""
    missing = [t for t in trackings if not t.get("buckets")]
    if not missing:
        return trackings
    for t in missing:
        slug = event_slug_from_link(t.get("market_link"))
        if not slug:
            slug = find_event_slug_by_title(t.get("title"))
        if not slug:
            continue
        try:
            buckets = fetch_event_buckets(slug)
            t["buckets"] = buckets
            if write:
                supabase.table("polymarket_trackings").update(
                    {"buckets": buckets}
                ).eq("id", t["id"]).execute()
            first = buckets[0]["label"] if buckets else "?"
            last = buckets[-1]["label"] if buckets else "?"
            print(f"   📦 Buckets ({len(buckets)}): {first} … {last}")
            time.sleep(sleep_between)
        except Exception as err:
            print(f"   ⚠️ Buckets de {slug}: {err}")
    return trackings