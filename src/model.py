import random
import zlib
from collections import Counter
from datetime import date, datetime, timedelta, timezone

DEFAULT_EXCEEDANCE_THRESHOLDS = [100, 150, 200, 250, 300, 400, 500]


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


def exceedance_probs(totals, thresholds=None):
    thresholds = thresholds if thresholds else DEFAULT_EXCEEDANCE_THRESHOLDS
    n = len(totals) or 1
    return {
        str(t): round(sum(1 for v in totals if v >= t) / n, 6) for t in thresholds
    }


def derive_seed(tracking_id):
    if not tracking_id:
        return None
    return zlib.crc32(str(tracking_id).encode("utf-8"))


def compute_prediction(
    tracking,
    current_count,
    counts_by_date,
    now,
    sims,
    half_life,
    seed=None,
    exceedance_thresholds=None,
):
    title = tracking.get("title") or tracking["id"]
    start = parse_datetime(tracking.get("start_date"))
    end = parse_datetime(tracking.get("end_date"))

    if start is None or end is None:
        raise ValueError("Sin fechas de ventana válidas")
    if now >= end:
        raise ValueError("Mercado ya terminado")

    if seed is None:
        seed = derive_seed(tracking.get("id"))

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

    return {
        "tracking_id": tracking.get("id"),
        "market_title": title,
        "handle": tracking.get("handle"),
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
        "exceedance": exceedance_probs(totals, exceedance_thresholds),
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


def fetch_daily_counts(supabase, handle, cutoff, page_size=1000):
    parsed = []
    offset = 0
    while True:
        response = (
            supabase.table("tweets")
            .select("created_at")
            .eq("author_username", handle)
            .neq("type", "reply")
            .gte("created_at", cutoff.isoformat())
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = response.data or []
        for row in rows:
            try:
                parsed.append(parse_datetime(row["created_at"]))
            except (ValueError, TypeError):
                continue
        if len(rows) < page_size:
            break
        offset += page_size
    return daily_counts(parsed)


def fetch_count_in_window(supabase, handle, start, end):
    query = (
        supabase.table("tweets")
        .select("id", count="exact")
        .eq("author_username", handle)
        .neq("type", "reply")
        .gte("created_at", start.isoformat())
        .lt("created_at", end.isoformat())
    )
    response = query.execute()
    if response.count is not None:
        return response.count
    total = 0
    offset = 0
    while True:
        page = query.range(offset, offset + 999).execute()
        rows = page.data or []
        total += len(rows)
        if len(rows) < 1000:
            break
        offset += 1000
    return total


def fetch_tweet_version(supabase, handle):
    response = (
        supabase.table("tweets")
        .select("created_at", count="exact")
        .eq("author_username", handle)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    max_created_at = parse_datetime(rows[0]["created_at"]) if rows else None
    total = response.count if response.count is not None else len(rows)
    return max_created_at, total


def ok_tracking_ids(supabase):
    try:
        response = (
            supabase.table("predictions")
            .select("tracking_id")
            .eq("status", "ok")
            .execute()
        )
        return {r["tracking_id"] for r in response.data or []}
    except Exception as err:
        print(f"  ⚠️ No se pudo consultar predictions: {err}")
        return set()


def save_prediction(supabase, record, no_db=False):
    if no_db:
        return
    try:
        supabase.table("predictions").insert(record).execute()
    except Exception as err:
        message = str(err)
        if "predictions" in message and "does not exist" in message:
            print("  🔧 Ejecuta supabase/predictions.sql para crear la tabla predictions.")
        raise


def insert_snapshot(supabase, record):
    supabase.table("prediction_snapshots").insert(record).execute()