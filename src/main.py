import os
import random
import time
from datetime import datetime, timezone
import requests
from dateutil import parser
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TARGET_USER = "elonmusk"


def get_tweet_type(tweet: dict) -> str:
    if tweet.get("retweeted_tweet"):
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
        tweet_id = str(tweet.get("id_str") or tweet.get("id"))
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

    response = supabase.table("tweets").upsert(
        records, on_conflict="tweet_id"
    ).execute()

    return len(response.data) if response.data else 0


def run_free_plan_backfill(start_date: str, end_date: str):
    """
    Extracción configurada para los límites del Plan FREE de twitterapi.io.
    """
    url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTER_API_KEY}
    search_query = f"from:{TARGET_USER} since:{start_date} until:{end_date}"

    cursor = None
    page_number = 1
    total_saved = 0

    print(f"🐢 Iniciando extracción [PLAN FREE] para @{TARGET_USER}...")
    print(f"🔍 Query: '{search_query}'\n")

    while True:
        params = {
            "query": search_query,
            "queryType": "Latest"
        }
        if cursor:
            params["cursor"] = cursor

        print(f"📡 Solicitando página {page_number}...")

        data = None
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=20)

                # Si agotaste créditos diarios o el token falló
                if response.status_code in (402, 403):
                    print(f"🛑 [HTTP {response.status_code}] Créditos de plan gratuito agotados por hoy.")
                    return

                # Exceso de peticiones (Rate limit)
                if response.status_code == 429:
                    # Espera larga de 20s, 40s, 60s + tiempo aleatorio para despejar el limite
                    wait_time = (attempt * 20) + random.uniform(1.0, 3.0)
                    print(f"  ⏳ [429 Rate Limit] Plan Free alcanzado. Pausando {wait_time:.1f}s (Intento {attempt}/{max_retries})...")
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                break

            except requests.RequestException as err:
                print(f"  ❌ Error de conexión: {err}")
                if attempt == max_retries:
                    print("  🛑 Reintentos máximos alcanzados para esta página.")
                    break
                time.sleep(10)

        if not data:
            print("🛑 No se obtuvieron datos. Finalizando proceso.")
            break

        raw_tweets = data.get("tweets", [])
        next_cursor = data.get("next_cursor") or data.get("next_cursor")

        if not raw_tweets:
            print("ℹ️ No hay más tweets devueltos en este rango de fechas.")
            break

        # Guardar en Supabase para asegurar los datos inmediatamente
        saved_count = save_tweets_to_supabase(raw_tweets)
        total_saved += saved_count
        print(f"   💾 Guardados {saved_count} tweets (Total en este lote: {total_saved})")

        if not next_cursor or next_cursor == cursor:
            print("🏁 Extracción completada.")
            break

        cursor = next_cursor
        page_number += 1

        # Pausa conservadora entre peticiones para el Plan Free (6-8 segundos)
        delay = random.uniform(6.0, 8.0)
        print(f"  ☕ Esperando {delay:.1f}s antes de la siguiente página...\n")
        time.sleep(delay)

    print(f"\n🎉 [FIN] Total de tweets guardados exitosamente: {total_saved}")


if __name__ == "__main__":
    # Mantener rangos cortos en plan free (ej. 1 mes o 15 días) para no agotar la cuota
    START_DATE = "2026-08-01"
    END_DATE = "2026-08-31"

    run_free_plan_backfill(start_date=START_DATE, end_date=END_DATE)