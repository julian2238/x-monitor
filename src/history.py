import os
import random
import threading
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TARGET_USER = "elonmusk"

# These defaults are conservative until the account's exact QPS limit is known.
MONTH_WORKERS = int(os.getenv("HISTORY_MONTH_WORKERS", "3"))
MIN_REQUEST_INTERVAL = float(os.getenv("HISTORY_MIN_REQUEST_INTERVAL", "5"))
REQUEST_JITTER_MIN = float(os.getenv("HISTORY_REQUEST_JITTER_MIN", "0.5"))
REQUEST_JITTER_MAX = float(os.getenv("HISTORY_REQUEST_JITTER_MAX", "2"))

request_lock = threading.Lock()
database_lock = threading.Lock()
last_request_started = 0.0


def get_tweet_type(tweet: dict) -> str:
    if tweet.get("retweeted_tweet") or tweet.get("isRetweet"):
        return "retweet"
    if tweet.get("isReply"):
        reply_username = str(tweet.get("inReplyToUsername") or "").lower()
        if reply_username == TARGET_USER.lower():
            return "self-reply"
        return "reply"
    if tweet.get("quoted_tweet"):
        return "quote"
    return "original"


def save_tweets_to_supabase(tweets: list) -> int:
    if not tweets:
        return 0

    records = []
    for tweet in tweets:
        tweet_id = str(tweet.get("id"))
        records.append({
            "tweet_id": tweet_id,
            "author_username": TARGET_USER,
            "text": tweet.get("text"),
            "type": get_tweet_type(tweet),
            "created_at": tweet.get("createdAt"),
            "like_count": tweet.get("likeCount", 0),
            "retweet_count": tweet.get("retweetCount", 0),
            "reply_count": tweet.get("replyCount", 0),
            "quote_count": tweet.get("quoteCount", 0),
            "view_count": tweet.get("viewCount"),
            "raw_data": tweet
        })

    with database_lock:
        response = supabase.table("tweets").upsert(
            records, on_conflict="tweet_id"
        ).execute()

    return len(response.data) if response.data else 0


def wait_for_request_slot() -> None:
    """Serializa el inicio de peticiones y añade jitter entre trabajadores."""
    global last_request_started

    with request_lock:
        now = time.monotonic()
        elapsed = now - last_request_started
        wait_time = max(0, MIN_REQUEST_INTERVAL - elapsed)
        wait_time += random.uniform(REQUEST_JITTER_MIN, REQUEST_JITTER_MAX)
        if wait_time:
            time.sleep(wait_time)
        last_request_started = time.monotonic()


def log_failed_window(
    start_timestamp: int,
    end_timestamp: int,
    error_message: str,
) -> None:
    """Registra una ventana que deberá revisarse o reintentarse después."""
    try:
        supabase.table("history_failed_windows").insert({
            "target_username": TARGET_USER,
            "since_ts": start_timestamp,
            "until_ts": end_timestamp,
            "error_message": error_message[:1000],
        }).execute()
    except Exception as err:
        # Un fallo del log no debe ocultar el fallo original de la API.
        print(f"    ⚠️ No se pudo guardar el log de la ventana fallida: {err}")


def parse_twitter_date(date_str: str) -> int:
    """
    Convierte el string de fecha de Twitter a Unix Timestamp de forma segura.
    """
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
    except ValueError:
        dt = datetime.strptime(date_str, '%a %b %d %H:%M:%S %z %Y')
    
    return int(dt.timestamp())


def run_time_sliced_backfill(start_timestamp: int, end_timestamp: int) -> int:
    """
    Extrae tuits deslizando el until_time basado en el tuit más antiguo recibido.
    """
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"x-api-key": TWITTER_API_KEY}
    
    current_until = end_timestamp
    total_saved_in_window = 0
    iteration = 1
    max_iterations = 500
    empty_attempts = 0

    while current_until > start_timestamp and iteration <= max_iterations:
        search_query = f"from:{TARGET_USER} since_time:{start_timestamp} until_time:{current_until}"
        
        params = {
            "query": search_query,
            "queryType": "Latest"
        }

        print(f"  🔍 Iteración {iteration} | Query: '{search_query}'")
        
        data = None
        last_error = None
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                wait_for_request_slot()
                response = requests.get(url, headers=headers, params=params, timeout=20)

                if response.status_code in (402, 403):
                    last_error = (
                        f"HTTP {response.status_code}: "
                        "créditos agotados o error de autenticación"
                    )
                    print(f"🛑 [{last_error}]")
                    log_failed_window(start_timestamp, current_until, last_error)
                    return total_saved_in_window

                if response.status_code == 429:
                    last_error = "HTTP 429: rate limit"
                    wait_time = (attempt * 20) + random.uniform(1.0, 3.0)
                    print(f"    ⏳ [429 Rate Limit] Pausando {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                break

            except (requests.RequestException, ValueError) as err:
                last_error = str(err)
                print(f"    ❌ Error de conexión (Intento {attempt}/{max_retries}): {err}")
                if attempt == max_retries:
                    break
                time.sleep(10)

        if not data:
            log_failed_window(
                start_timestamp,
                current_until,
                last_error or "No se recibió respuesta de la API",
            )
            break

        raw_tweets = data.get("tweets", [])

        if not raw_tweets:
            empty_attempts += 1
            if empty_attempts < 3:
                wait_time = 5 * empty_attempts
                print(
                    f"  ⚠️ Respuesta vacía; reintento "
                    f"{empty_attempts}/3 en {wait_time}s..."
                )
                time.sleep(wait_time)
                continue

            print("  ℹ️ La API devolvió vacío después de 3 intentos.")
            break

        # Una respuesta con datos confirma que esta ventana sigue avanzando.
        empty_attempts = 0

        # Guardar en base de datos
        try:
            saved_count = save_tweets_to_supabase(raw_tweets)
        except Exception as err:
            error_message = f"Error guardando tweets en Supabase: {err}"
            log_failed_window(start_timestamp, current_until, error_message)
            print(f"  ❌ {error_message}")
            break
        total_saved_in_window += saved_count
        
        # CORRECCIÓN BUG A: Encontrar explícitamente el timestamp MÁS ANTIGUO de la lista
        try:
            oldest_ts = min(parse_twitter_date(t["createdAt"]) for t in raw_tweets)
        except (KeyError, TypeError, ValueError) as err:
            error_message = f"Fecha createdAt inválida: {err}"
            log_failed_window(start_timestamp, current_until, error_message)
            print(f"  ❌ {error_message}")
            break
        
        print(f"  💾 Guardados/Actualizados: {saved_count} (Total ventana: {total_saved_in_window}). Pivot timestamp bajó a: {datetime.fromtimestamp(oldest_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

        # CORRECCIÓN BUG C: Si la llamada trajo menos de 20 tuits, el rango está agotado
        if len(raw_tweets) < 20:
            print("  ✅ Rango completado (menos de 20 tuits retornados).")
            break

        # Desplazamiento temporal
        if oldest_ts < current_until:
            current_until = oldest_ts - 1
        else:
            # No avanzar aquí podría repetir indefinidamente la misma respuesta.
            error_message = (
                "La ventana no avanzó: el tweet más antiguo no es anterior "
                "a until_time"
            )
            log_failed_window(start_timestamp, current_until, error_message)
            print(f"  ⚠️ {error_message}")
            break

        iteration += 1
        time.sleep(random.uniform(6.0, 8.0))

    if current_until > start_timestamp and iteration > max_iterations:
        log_failed_window(
            start_timestamp,
            current_until,
            f"Se alcanzó el máximo de {max_iterations} iteraciones",
        )

    return total_saved_in_window


def next_month(date: datetime) -> datetime:
    """Devuelve el primer día del mes siguiente."""
    if date.month == 12:
        return date.replace(
            year=date.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return date.replace(
        month=date.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def generate_month_windows(
    start_date: datetime,
    end_date: datetime,
) -> list[tuple[datetime, datetime]]:
    """Divide un rango [start_date, end_date) en ventanas mensuales."""
    if start_date.tzinfo is None or end_date.tzinfo is None:
        raise ValueError("Las fechas deben incluir zona horaria")
    if start_date >= end_date:
        raise ValueError("start_date debe ser anterior a end_date")

    windows = []
    cursor = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < end_date:
        month_end = min(next_month(cursor), end_date)
        window_start = max(cursor, start_date)
        if window_start < month_end:
            windows.append((window_start, month_end))
        cursor = next_month(cursor)
    return windows


def process_month(month_start: datetime, month_end: datetime) -> int:
    """Procesa un mes día por día; cada mes corre en su propio trabajador."""
    total_month_saved = 0
    current_date = month_start
    total_days = (month_end.date() - month_start.date()).days
    day_number = 0

    print(
        f"\n📆 [MES INICIO] {month_start.strftime('%Y-%m-%d')} → "
        f"{month_end.strftime('%Y-%m-%d')}"
    )

    while current_date < month_end:
        next_date = min(current_date + timedelta(days=1), month_end)
        day_number += 1
        print(
            f"📅 [{month_start.strftime('%Y-%m')}] "
            f"Día {day_number}/{total_days}: "
            f"{current_date.strftime('%Y-%m-%d')}"
        )

        start_ts = int(current_date.timestamp())
        end_ts = int(next_date.timestamp())

        try:
            saved_today = run_time_sliced_backfill(start_ts, end_ts)
        except Exception as err:
            error_message = f"Error inesperado procesando el día: {err}"
            log_failed_window(start_ts, end_ts, error_message)
            print(f"❌ {error_message}")
            saved_today = 0

        total_month_saved += saved_today
        print(
            f"✅ Día finalizado. Guardados/actualizados: {saved_today} | "
            f"Acumulado del mes: {total_month_saved}"
        )
        current_date = next_date

    print(
        f"🏁 [MES FIN] {month_start.strftime('%Y-%m')} | "
        f"Total: {total_month_saved}"
    )
    return total_month_saved


def run_historical_extraction(start_date: datetime, end_date: datetime) -> None:
    """Procesa los meses del rango en paralelo y sus días secuencialmente."""
    month_windows = generate_month_windows(start_date, end_date)
    workers = min(MONTH_WORKERS, len(month_windows))
    total_saved = 0

    print(
        f"🐢 Iniciando extracción para @{TARGET_USER}: "
        f"{start_date.isoformat()} → {end_date.isoformat()}"
    )
    print(f"📊 Meses: {len(month_windows)} | Trabajadores: {workers}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process_month, month_start, month_end): month_start
            for month_start, month_end in month_windows
        }

        for future in as_completed(futures):
            month_start = futures[future]
            try:
                total_saved += future.result()
            except Exception as err:
                print(
                    f"❌ Falló el trabajador del mes "
                    f"{month_start.strftime('%Y-%m')}: {err}"
                )

    print(f"\n🎉 [FIN EXTRACCIÓN] Total guardado/actualizado: {total_saved}")


if __name__ == "__main__":
    start_date = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(2026, 9, 8, 0, 0, 0, tzinfo=timezone.utc)
    run_historical_extraction(start_date, end_date)
