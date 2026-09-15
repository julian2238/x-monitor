import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from model import (
    compute_prediction,
    fetch_count_in_window,
    fetch_daily_counts,
    fetch_tweet_version,
    insert_snapshot,
    ok_tracking_ids,
    parse_datetime,
    save_prediction,
)
from trackings import (
    ensure_buckets,
    fetch_trackings,
    normalize_tracking,
    sync_trackings,
)

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

XTRACKER_BASE = os.getenv("POLYMARKET_XTRACKER_BASE", "https://xtracker.polymarket.com")
TARGET_USER = os.getenv("TARGET_USER", "elonmusk")
DEFAULT_SIMS = int(os.getenv("PREDICTION_SIMS", "50000"))
DEFAULT_HALFLIFE = float(os.getenv("PREDICTION_RECENCY_HALFLIFE_DAYS", "30"))
EXCEEDANCE_THRESHOLDS = [
    int(x)
    for x in os.getenv(
        "PREDICTION_EXCEEDANCE_THRESHOLDS", "100,150,200,250,300,400,500"
    ).split(",")
    if x.strip()
]


def load_active_trackings(supabase, handle):
    response = (
        supabase.table("polymarket_trackings")
        .select("*")
        .eq("is_active", True)
        .execute()
    )
    return [normalize_tracking(t, handle) for t in response.data or []]


def build_markets(supabase, handle, now, trackings):
    """Filtra mercados con ventana válida y precalcula su conteo actual."""
    markets = []
    for tracking in trackings:
        tid = tracking.get("id")
        if not tid:
            continue
        start = parse_datetime(tracking.get("start_date"))
        end = parse_datetime(tracking.get("end_date"))
        if start is None or end is None:
            continue
        if now >= end:
            continue
        current_count = fetch_count_in_window(supabase, handle, start, now)
        markets.append({
            "tracking": tracking,
            "start": start,
            "end": end,
            "current_count": current_count,
        })
    return markets


def read_version(supabase, handle):
    response = (
        supabase.table("prediction_runs")
        .select("*")
        .eq("handle", handle)
        .limit(1)
        .execute()
    )
    data = response.data or []
    return data[0] if data else None


def update_version(supabase, handle, max_created_at, total_count, now):
    supabase.table("prediction_runs").upsert({
        "handle": handle,
        "last_max_created_at": (
            max_created_at.isoformat() if max_created_at else None
        ),
        "last_total_count": total_count,
        "last_run_at": now.isoformat(),
    }, on_conflict="handle").execute()


def main():
    now = datetime.now(timezone.utc)
    handle = TARGET_USER

    supabase = create_client(
        os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    )

    # Paso 1: cargar trackings desde xtracker
    try:
        print(f"🔄 Sincronizando mercados activos de @{handle}...")
        raw = fetch_trackings(XTRACKER_BASE, handle)
        synced = sync_trackings(supabase, raw, handle, now)
        print(f"   ✅ {len(raw)} mercados activos ({synced} sincronizados).")
    except Exception as err:
        print(f"   ⚠️ Sync falló ({err}); usando mercados guardados.")

    trackings = load_active_trackings(supabase, handle)
    markets = build_markets(supabase, handle, now, trackings)
    if not markets:
        print("😴 Sin mercados activos con ventana válida.")
        return
    ensure_buckets(
        supabase, [m["tracking"] for m in markets], write=True
    )
    print(f"   📌 {len(markets)} mercados a vigilar.")

    # Paso 2: predicción inicial para mercados sin análisis
    already = ok_tracking_ids(supabase)
    new_markets = [m for m in markets if m["tracking"]["id"] not in already]

    # Paso 3: detectar si el contador cambió (diff) antes de decidir
    max_created_at, total_count = fetch_tweet_version(supabase, handle)
    prev = read_version(supabase, handle)
    changed = prev is None or (
        parse_datetime(prev.get("last_max_created_at")) != max_created_at
    ) or (prev.get("last_total_count") != total_count)

    if not new_markets and not changed:
        print("🔇 Sin mercados nuevos ni cambios en los tweets; no se reanaliza.")
        update_version(supabase, handle, max_created_at, total_count, now)
        return

    cutoff = now - timedelta(days=400)
    counts_by_date = fetch_daily_counts(supabase, handle, cutoff)
    if not counts_by_date:
        print("❌ No hay tweets históricos en Supabase para modelar.")
        return

    for m in new_markets:
        tracking = m["tracking"]
        title = tracking.get("title") or tracking["id"]
        try:
            record = compute_prediction(
                tracking,
                m["current_count"],
                counts_by_date,
                now,
                DEFAULT_SIMS,
                DEFAULT_HALFLIFE,
                seed=None,
                exceedance_thresholds=EXCEEDANCE_THRESHOLDS,
            )
            rec = dict(record)
            rec.pop("exceedance", None)
            save_prediction(supabase, rec, no_db=False)
            print(f"   ✨ Predicción inicial creada: {title}")
        except Exception as err:
            print(f"   ❌ Falló predicción inicial de {title}: {err}")

    if not changed:
        print("🔇 Contador sin cambios; no se generan snapshots.")
        update_version(supabase, handle, max_created_at, total_count, now)
        return

    snapshot_count = 0
    for m in markets:
        tracking = m["tracking"]
        title = tracking.get("title") or tracking["id"]
        try:
            record = compute_prediction(
                tracking,
                m["current_count"],
                counts_by_date,
                now,
                DEFAULT_SIMS,
                DEFAULT_HALFLIFE,
                seed=None,
                exceedance_thresholds=EXCEEDANCE_THRESHOLDS,
            )
            insert_snapshot(supabase, record)
            snapshot_count += 1
        except Exception as err:
            print(f"   ❌ Falló snapshot de {title}: {err}")

    update_version(supabase, handle, max_created_at, total_count, now)
    print(f"✅ Cambio detectado; {snapshot_count} snapshots guardados.")


if __name__ == "__main__":
    main()