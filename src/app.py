import os
import sys
import time
import random
from datetime import datetime
import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Crear cliente de Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TARGET_USER = "elonmusk"
CHECK_INTERVAL_SECONDS = 300  # 5 minutos

def get_last_saved_tweet_id(username: str) -> str | None:
    response = (
        supabase.table("tweets")
        .select("tweet_id")
        .eq("author_username", username)
        .order("tweet_id", desc=True)
        .limit(1)
        .execute()
    )
    
    if response.data and len(response.data) > 0:
        return response.data[0]["tweet_id"]
    return None


def get_tweet_type(tweet: dict) -> str:
    if tweet.get("retweeted_tweet"):
        return "retweet"
    if tweet.get("isReply"):
        # Se previene el error si inReplyToUsername viene vacío (None)
        reply_username = str(tweet.get("inReplyToUsername") or "").lower()
        if reply_username == TARGET_USER.lower():
            return "self-reply"
        return "reply"
    if tweet.get("quoted_tweet"):
        return "quote"
    
    return "original"


def fetch_tweets_from_api(username: str, since_id: str | None = None) -> list:
    """Obtiene los tweets desde TwitterAPI.io con manejo robusto de reintentos."""
    url = "https://api.twitterapi.io/twitter/user/last_tweets"
    headers = {"X-API-Key": TWITTER_API_KEY}

    new_tweets = []
    cursor = None
    since_id_int = int(since_id) if since_id else None

    while True:
        params = {
            "userName": username,
            "includeReplies": True
        }

        if cursor:
            params["cursor"] = cursor

        data = None
        max_retries = 3

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=15)
                
                # Si agotaste créditos diarios o el token falló
                if response.status_code in (402, 403):
                    print(f"🛑 [HTTP {response.status_code}] Créditos agotados o token inválido.")
                    return new_tweets  # Retorna lo recolectado hasta ahora

                # Exceso de peticiones (Rate limit)
                if response.status_code == 429:
                    wait_time = (attempt * 20) + random.uniform(1.0, 3.0)
                    print(f"  ⏳ [429 Rate Limit] Pausando {wait_time:.1f}s (Intento {attempt}/{max_retries})...")
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                break  # Petición exitosa, sale del bucle de reintentos

            except requests.RequestException as err:
                print(f"  ❌ Error de conexión: {err}")
                if attempt == max_retries:
                    print("  🛑 Reintentos máximos alcanzados para esta página.")
                    break
                time.sleep(10)

        # Si tras los reintentos no hay data, se rompe el paginador
        if not data:
            print("⚠️ No se obtuvieron datos en esta iteración. Abortando paginación.")
            break

        raw_tweets = data.get("data", {}).get("tweets", [])
        cursor = data.get("data", {}).get("next_cursor", None)

        if not raw_tweets:
            break

        reached_since_id = False
        for tweet in raw_tweets:
            tweet_id = int(tweet.get("id"))

            # Si llegamos al tweet que ya tenemos, paramos de agregar
            if since_id_int and tweet_id <= since_id_int:
                reached_since_id = True
                break
            else:
                new_tweets.append(tweet)

        if reached_since_id or not cursor:
            break
            
        # Pausa suave si toca pedir la siguiente página
        time.sleep(random.uniform(2.0, 4.0))

    new_tweets.reverse()
    return new_tweets


def save_tweets_to_supabase(tweets: list) -> int:
    """Guarda o actualiza los tweets en Supabase."""
    if not tweets:
        return 0

    records = []
    for tweet in tweets:
        tweet_id = str(tweet.get("id") or tweet.get("id_str"))
        
        record = {
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
        }
        records.append(record)

    response = supabase.table("tweets").upsert(
        records,
        on_conflict="tweet_id"
    ).execute()

    return len(response.data) if response.data else 0


def start_monitoring():
    print(f"🔄 Iniciando ciclo de monitoreo para @{TARGET_USER} (cada {CHECK_INTERVAL_SECONDS} segundos)...\n")
    
    # 2. Ciclo continuo de monitoreo
    while True:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            last_id = get_last_saved_tweet_id(TARGET_USER)
            print(f"[{timestamp}] Último tweet guardado ID: {last_id or 'Ninguno'}")
            
            tweets = fetch_tweets_from_api(TARGET_USER, since_id=last_id)
            
            if tweets:
                saved_count = save_tweets_to_supabase(tweets)
                print(f"[{timestamp}] 🆕 Se guardaron {saved_count} tweet(s) nuevos.")
            else:
                print(f"[{timestamp}] 😴 Sin tweets nuevos.")

        except Exception as err:
            print(f"[{timestamp}] ❌ Error crítico durante el ciclo: {err}")

        # Esperar al siguiente ciclo
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    start_monitoring()