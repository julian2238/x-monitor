import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

from model import (
    compute_prediction,
    fetch_count_in_window,
    fetch_daily_counts,
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


def run_prediction_for_tracking(
    supabase, tracking, handle, sims, half_life, now, seed, no_db,
    counts_by_date, exceedance_thresholds,
):
    title = tracking.get("title") or tracking["id"]

    print(f"\n📊 {title}")

    start = parse_datetime(tracking.get("start_date"))
    end = parse_datetime(tracking.get("end_date"))
    if start is None or end is None:
        print("   ⏭️ Sin fechas de ventana válidas.")
        return "skipped"
    print(
        f"   Ventana: {start.strftime('%Y-%m-%d %H:%M')} → "
        f"{end.strftime('%Y-%m-%d %H:%M')} UTC"
    )
    if now >= end:
        print("   ⏭️ Mercado ya terminado.")
        return "skipped"

    current_count = fetch_count_in_window(supabase, handle, start, now)

    try:
        record = compute_prediction(
            tracking,
            current_count,
            counts_by_date,
            now,
            sims,
            half_life,
            seed=seed,
            exceedance_thresholds=exceedance_thresholds,
        )
    except ValueError as err:
        print(f"   ⏭️ {err}")
        return "skipped"

    print(
        f"   Conteo actual: {record['current_count']} | Sims: {record['sims']} | "
        f"Half-life: {record['recency_half_life_days']}d"
    )
    print(
        f"   Total mediana: {record['predicted_median_total']} "
        f"(P5 {record['predicted_p5_total']} – P95 {record['predicted_p95_total']})"
    )
    print(
        f"   Más probable: {record['most_likely_outcome']} = "
        f"{record['most_likely_probability'] * 100:.1f}%"
    )
    print("   Intervalo     Probabilidad")
    for label, prob in record["probabilities"].items():
        if prob > 0:
            print(f"   {label:<12} {prob * 100:6.1f}%")

    exceeds = record.get("exceedance") or {}
    if exceeds:
        print("   P(total ≥ N)")
        for threshold, prob in exceeds.items():
            if prob > 0:
                print(f"   ≥ {threshold:<4} {prob * 100:6.1f}%")

    rec = dict(record)
    rec.pop("exceedance", None)
    save_prediction(supabase, rec, no_db)
    return "ok"


def main():
    parser = argparse.ArgumentParser(
        description="Sincroniza mercados de tweets de Polymarket y genera predicción Monte Carlo"
    )
    parser.add_argument("--handle", default=TARGET_USER)
    parser.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    parser.add_argument("--recency-half-life", type=float, default=DEFAULT_HALFLIFE)
    parser.add_argument("--only", help="Procesar solo este tracking_id")
    parser.add_argument("--force", action="store_true", help="Recomputar aunque ya exista análisis")
    parser.add_argument("--require-sync", action="store_true", help="Abortar si el sync falla")
    parser.add_argument("--no-db", action="store_true", help="No escribir en Supabase")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    supabase = create_client(
        os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    )

    raw_trackings = None
    try:
        print(f"🔄 Obteniendo mercados activos de xtracker para @{args.handle}...")
        raw_trackings = fetch_trackings(XTRACKER_BASE, args.handle)
        print(f"   ✅ {len(raw_trackings)} mercados activos.")
        if not args.no_db:
            synced = sync_trackings(supabase, raw_trackings, args.handle, now)
            print(f"   💾 Sincronizados/actualizados: {synced}")
    except Exception as err:
        print(f"   ⚠️ Error de sync: {err}")
        if args.require_sync:
            print("   🛑 Abortando por --require-sync.")
            sys.exit(1)
        print("   ↪ Usando mercados previamente guardados en Supabase.")
        try:
            response = (
                supabase.table("polymarket_trackings")
                .select("*")
                .eq("is_active", True)
                .execute()
            )
            raw_trackings = response.data or []
        except Exception as err2:
            print(f"   ❌ No se pudieron leer mercados guardados: {err2}")
            sys.exit(1)

    if not raw_trackings:
        print("😴 Sin mercados activos.")
        return

    trackings = [normalize_tracking(t, args.handle) for t in raw_trackings]

    if args.only:
        trackings = [t for t in trackings if t.get("id") == args.only]
        if not trackings:
            print(f"❌ No se encontró el tracking {args.only}.")
            sys.exit(1)

    trackings = ensure_buckets(supabase, trackings, write=not args.no_db)

    cutoff = now - timedelta(days=400)
    counts_by_date = fetch_daily_counts(supabase, args.handle, cutoff)
    if not counts_by_date:
        print("❌ No hay tweets históricos en Supabase para modelar")
        sys.exit(1)

    already = ok_tracking_ids(supabase) if not args.no_db else set()

    summary = Counter()
    for tracking in trackings:
        tid = tracking.get("id")
        if not tid:
            continue
        if not args.force and tid in already:
            print(f"⏭️ {tracking.get('title') or tid} ya tiene análisis; --force para recomputar.")
            summary["skipped"] += 1
            continue
        try:
            status = run_prediction_for_tracking(
                supabase, tracking, args.handle, args.sims,
                args.recency_half_life, now, args.seed, args.no_db,
                counts_by_date, EXCEEDANCE_THRESHOLDS,
            )
            summary[status] += 1
        except Exception as err:
            summary["failed"] += 1
            print(f"   ❌ Error: {err}")
            if not args.no_db:
                try:
                    failed = {
                        "tracking_id": tid,
                        "market_title": tracking.get("title"),
                        "handle": args.handle,
                        "window_start": tracking.get("start_date"),
                        "window_end": tracking.get("end_date"),
                        "generated_at": now.isoformat(),
                        "current_count": None,
                        "sims": args.sims,
                        "recency_half_life_days": args.recency_half_life,
                        "model": "weighted-empirical-weekday",
                        "most_likely_outcome": None,
                        "probabilities": {},
                        "meta": {"seed": args.seed},
                        "status": "failed",
                        "error_message": str(err)[:1000],
                    }
                    save_prediction(supabase, failed, args.no_db)
                except Exception as save_err:
                    print(f"   ⚠️ No se pudo guardar el fallo: {save_err}")

    print(f"\n📋 Resumen: {dict(summary)}")


if __name__ == "__main__":
    main()