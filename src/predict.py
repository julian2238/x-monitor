import argparse
import os
import random
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

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


def parse_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dst_start(year):
    d = date(year, 3, 1)
    while d.weekday() != 6:
        d = d.replace(day=d.day + 1)
    return d.replace(day=d.day + 7)


def _dst_end(year):
    d = date(year, 11, 1)
    while d.weekday() != 6:
        d = d.replace(day=d.day + 1)
    return d


def eastern_offset(local_date):
    if _dst_start(local_date.year) <= local_date < _dst_end(local_date.year):
        return timedelta(hours=-4)
    return timedelta(hours=-5)


def local_to_utc(local_dt):
    return (local_dt - eastern_offset(local_dt.date())).replace(tzinfo=timezone.utc)


def eastern_tz(utc_dt):
    offset = eastern_offset(utc_dt.date())
    local_date = (utc_dt + offset).date()
    return timezone(eastern_offset(local_date))


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


def load_tweets(supabase, username, cutoff, page_size=1000):
    parsed = []
    offset = 0
    while True:
        response = (
            supabase.table("tweets")
            .select("created_at,type")
            .eq("author_username", username)
            .gte("created_at", cutoff.isoformat())
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = response.data or []
        for row in rows:
            if row.get("type") == "reply":
                continue
            try:
                parsed.append(parse_datetime(row["created_at"]))
            except (ValueError, TypeError):
                continue
        if len(rows) < page_size:
            break
        offset += page_size
    return parsed


def daily_counts(created_ats):
    return Counter(dt.astimezone(eastern_tz(dt)).date() for dt in created_ats)


def count_in_window(created_ats, start, end):
    return sum(1 for dt in created_ats if start <= dt < end)


def build_pools(counts_by_date, now, half_life_days):
    by_weekday = {i: [] for i in range(7)}
    global_counts = []
    global_weights = []
    for date_, count in counts_by_date.items():
        day_start_utc = local_to_utc(datetime(date_.year, date_.month, date_.day))
        age_days = (now - day_start_utc).total_seconds() / 86400.0
        if half_life_days and half_life_days > 0:
            weight = 0.5 ** (age_days / half_life_days)
        else:
            weight = 1.0
        by_weekday[date_.weekday()].append((count, weight))
        global_counts.append(count)
        global_weights.append(weight)
    pools = {}
    for i in range(7):
        if by_weekday[i]:
            pools[i] = ([c for c, _ in by_weekday[i]], [w for _, w in by_weekday[i]])
        else:
            pools[i] = (global_counts, global_weights)
    return pools


def remaining_fractional_days(now, end):
    days = []
    et_day = now.astimezone(eastern_tz(now)).date()
    guard = 0
    while guard < 400:
        day_start_utc = local_to_utc(datetime(et_day.year, et_day.month, et_day.day))
        if day_start_utc >= end:
            break
        next_day_utc = local_to_utc(
            datetime(et_day.year, et_day.month, et_day.day) + timedelta(days=1)
        )
        overlap_start = max(now, day_start_utc)
        overlap_end = min(end, next_day_utc)
        if overlap_start < overlap_end:
            fraction = (overlap_end - overlap_start).total_seconds() / 86400.0
            days.append((et_day.weekday(), fraction))
        et_day += timedelta(days=1)
        guard += 1
    return days


def monte_carlo(current_count, remaining, pools, sims, seed=None):
    if seed is not None:
        random.seed(seed)
    totals = [current_count] * sims
    for weekday, fraction in remaining:
        counts, weights = pools[weekday]
        draws = random.choices(counts, weights=weights, k=sims)
        for i, draw in enumerate(draws):
            totals[i] += round(draw * fraction)
    return totals


def build_bins():
    bins = [{"label": "<20", "min": 0, "max": 19}]
    for lo in range(20, 500, 20):
        bins.append({"label": f"{lo}-{lo + 19}", "min": lo, "max": lo + 19})
    bins.append({"label": "500+", "min": 500, "max": None})
    return bins


def bucket_probabilities(totals, bins):
    counts = [0] * len(bins)
    for total in totals:
        for idx, b in enumerate(bins):
            lo, hi = b["min"], b["max"]
            if (lo is None or total >= lo) and (hi is None or total <= hi):
                counts[idx] += 1
                break
    denom = sum(counts) or 1
    return [c / denom for c in counts], counts


def percentiles(totals):
    ordered = sorted(totals)
    n = len(ordered)
    return {
        "p5": ordered[min(n - 1, int(n * 0.05))],
        "p50": ordered[min(n - 1, int(n * 0.50))],
        "p95": ordered[min(n - 1, int(n * 0.95))],
    }


def has_ok_prediction(supabase, tracking_id):
    try:
        response = (
            supabase.table("predictions")
            .select("id")
            .eq("tracking_id", tracking_id)
            .eq("status", "ok")
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception as err:
        print(f"  ⚠️ No se pudo consultar predictions: {err}")
        return False


def save_prediction(supabase, record, no_db):
    if no_db:
        return
    try:
        supabase.table("predictions").insert(record).execute()
    except Exception as err:
        message = str(err)
        if "predictions" in message and "does not exist" in message:
            print("  🔧 Ejecuta supabase/predictions.sql para crear la tabla predictions.")
        raise


def run_prediction_for_tracking(
    supabase, tracking, handle, sims, half_life, now, seed, no_db
):
    title = tracking.get("title") or tracking["id"]
    start = parse_datetime(tracking.get("start_date"))
    end = parse_datetime(tracking.get("end_date"))

    print(f"\n📊 {title}")
    if start is None or end is None:
        print("   ⏭️ Sin fechas de ventana válidas.")
        return "skipped"
    print(f"   Ventana: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")

    if now >= end:
        print("   ⏭️ Mercado ya terminado.")
        return "skipped"

    cutoff = now - timedelta(days=400)
    tweets = load_tweets(supabase, handle, cutoff)
    current_count = count_in_window(tweets, start, now)
    counts_by_date = daily_counts(tweets)

    if not counts_by_date:
        raise ValueError("No hay tweets históricos en Supabase para modelar")

    pools = build_pools(counts_by_date, now, half_life)
    remaining = remaining_fractional_days(max(now, start), end)
    if remaining:
        totals = monte_carlo(current_count, remaining, pools, sims, seed=seed)
    else:
        totals = [current_count] * sims

    bins = build_bins()
    probs, counts_arr = bucket_probabilities(totals, bins)
    pct = percentiles(totals)

    best_idx = max(range(len(bins)), key=lambda i: probs[i])
    best = bins[best_idx]

    avg = sum(counts_by_date.values()) / len(counts_by_date)
    var = sum((c - avg) ** 2 for c in counts_by_date.values()) / len(counts_by_date)

    print(f"   Conteo actual: {current_count} | Días restantes: {len(remaining)} | Sims: {sims}")
    print(f"   Total mediana: {pct['p50']} (P5 {pct['p5']} – P95 {pct['p95']})")
    print(f"   Más probable: {best['label']} = {probs[best_idx] * 100:.1f}%")
    print("   Intervalo     Probabilidad")
    for b, p in zip(bins, probs):
        if p > 0:
            print(f"   {b['label']:<12} {p * 100:6.1f}%")

    record = {
        "tracking_id": tracking["id"],
        "market_title": title,
        "handle": handle,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "generated_at": now.isoformat(),
        "current_count": current_count,
        "sims": sims,
        "recency_half_life_days": half_life,
        "model": "weighted-empirical-weekday",
        "predicted_p5_total": pct["p5"],
        "predicted_median_total": pct["p50"],
        "predicted_p95_total": pct["p95"],
        "most_likely_outcome": best["label"],
        "most_likely_probability": round(probs[best_idx], 6),
        "probabilities": {b["label"]: round(p, 6) for b, p in zip(bins, probs)},
        "meta": {
            "count_types": "all-except-reply",
            "daily_avg": round(avg, 2),
            "daily_std": round(var ** 0.5, 2),
            "obs_days": len(counts_by_date),
            "seed": seed,
        },
        "status": "ok",
        "error_message": None,
    }
    save_prediction(supabase, record, no_db)
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

    summary = Counter()
    for tracking in trackings:
        tid = tracking.get("id")
        if not tid:
            continue
        if not args.force and has_ok_prediction(supabase, tid):
            print(f"⏭️ {tracking.get('title') or tid} ya tiene análisis; --force para recomputar.")
            summary["skipped"] += 1
            continue
        try:
            status = run_prediction_for_tracking(
                supabase, tracking, args.handle, args.sims,
                args.recency_half_life, now, args.seed, args.no_db,
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