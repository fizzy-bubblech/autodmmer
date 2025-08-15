# main.py
import os, json
from typing import List, Dict, Optional
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

# pip install instagrapi python-dotenv redis
from instagrapi import Client

app = FastAPI()

# ---------- Optional Redis (persistent "seen" storage) ----------
try:
    import redis  # optional
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")  # fallback (ephemeral op Render)

# ---------- Auth / login ----------
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
IG_SESSION_ID = os.getenv("IG_SESSION_ID")  # aanbevolen boven password

def login_client() -> Client:
    """Login via session-id of username/password; gooi nette 500 als creds ontbreken."""
    cl = Client()
    try:
        if IG_SESSION_ID:
            cl.login_by_sessionid(IG_SESSION_ID)
            return cl
        if IG_USERNAME and IG_PASSWORD:
            cl.login(IG_USERNAME, IG_PASSWORD)
            return cl
    except Exception as e:
        # Geef duidelijke fout terug als login faalt
        raise HTTPException(status_code=500, detail=f"login_failed: {e}")
    raise HTTPException(status_code=500, detail="Missing IG credentials: set IG_SESSION_ID or IG_USERNAME+IG_PASSWORD")

# ---------- Helpers: persist "seen followers" ----------
def _load_seen_ids() -> set:
    if r:
        return set(map(int, r.smembers(SEEN_KEY) or []))
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def _save_seen_ids(ids: set):
    if r:
        pipe = r.pipeline()
        for i in ids:
            pipe.sadd(SEEN_KEY, int(i))
        pipe.execute()
    else:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(list(ids)), f)

# ---------- Followers polling core ----------
def _get_new_followers(cl: Client) -> List[Dict]:
    me_us_
