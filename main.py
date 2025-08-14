import os, json
from typing import List, Dict
from fastapi import FastAPI, Body
from pydantic import BaseModel
from instagrapi import Client
import redis

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
IG_SESSION_ID = os.getenv("IG_SESSION_ID")  # alternatief voor password
REDIS_URL = os.getenv("REDIS_URL")  # bv. upstash: rediss://:pass@host:port
SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")

r = None
if REDIS_URL:
    r = redis.from_url(REDIS_URL, decode_responses=True)

app = FastAPI()

def login_client() -> Client:
    cl = Client()
    if IG_SESSION_ID:
        cl.login_by_sessionid(IG_SESSION_ID)
    else:
        cl.login(IG_USERNAME, IG_PASSWORD)
    return cl

def load_seen() -> set:
    if r:
        return set(map(int, r.smembers(SEEN_KEY) or []))
    # fallback in-memory (niet persistent op serverless)
    return set()

def save_seen(ids: set):
    if r:
        pipe = r.pipeline()
        for i in ids:
            pipe.sadd(SEEN_KEY, int(i))
        pipe.execute()

@app.get("/poll_followers")
def poll_followers():
    cl = login_client()
    my_id = cl.user_id_from_username(IG_USERNAME or cl.account_info().username)
    followers = cl.user_followers_v1(my_id)  # dict {pk(str): UserShort}
    current_ids = set(map(int, followers.keys()))
    seen = load_seen()
    new_ids = current_ids - seen

    new_followers: List[Dict] = []
    for pk in new_ids:
        u = followers[str(pk)]
        new_followers.append({"pk": int(pk), "username": u.username})

    # markeer ALLE huidige als gezien (of alleen new_ids—jouw keuze)
    save_seen(current_ids)

    return {"new_followers": new_followers, "new_count": len(new_followers)}

class SendBody(BaseModel):
    pk: int
    message: str

@app.post("/send_dm")
def send_dm(payload: SendBody):
    cl = login_client()
    cl.direct_send(payload.message, [int(payload.pk)])
    return {"ok": True, "sent_to": int(payload.pk)}
