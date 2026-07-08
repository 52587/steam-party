"""
Steam Party API — Vercel serverless (FastAPI).
"""
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "4A8CB88E2B47982AB099C17E4E56420A")

app = FastAPI(title="Steam Party API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SESSIONS: dict[str, dict] = {}
API_CACHE: dict[str, tuple] = {}
CACHE_TTL = 300

# ─── Steam helpers ────────────────────────────────────────

async def steam_call(endpoint: str, params: dict) -> dict:
    sid = params.get("steamid") or params.get("steamids", "")
    cache_key = f"{endpoint}:{sid}:{sorted(params.items())}"
    if cache_key in API_CACHE:
        data, ts = API_CACHE[cache_key]
        if time.time() - ts < CACHE_TTL:
            return data

    url = f"https://api.steampowered.com/{endpoint}/?key={STEAM_API_KEY}"
    for k, v in params.items():
        url += f"&{k}={v}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()

    API_CACHE[cache_key] = (data, time.time())
    if len(API_CACHE) > 500:
        now = time.time()
        for k in list(API_CACHE):
            if now - API_CACHE[k][1] > CACHE_TTL * 2:
                del API_CACHE[k]
    return data


async def resolve_steam_id(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if raw.isdigit() and len(raw) == 17 and raw.startswith("7656119"):
        return raw
    if "/profiles/" in raw:
        sid = raw.split("/profiles/")[-1].split("/")[0]
        if sid.isdigit() and len(sid) == 17:
            return sid
    if "/id/" in raw:
        vanity = raw.split("/id/")[-1].split("/")[0]
    else:
        vanity = raw.replace("https://", "").replace("http://", "")
        vanity = vanity.replace("steamcommunity.com/", "").rstrip("/")
    if not vanity:
        raise ValueError("Could not parse Steam ID")
    resolve = await steam_call("ISteamUser/ResolveVanityURL/v1", {"vanityurl": vanity})
    result = resolve.get("response", {})
    if result.get("success") != 1:
        raise ValueError(f"Could not resolve profile: {vanity}")
    return result["steamid"]


async def fetch_player(steam_id: str) -> dict:
    summary = await steam_call("ISteamUser/GetPlayerSummaries/v2", {"steamids": steam_id})
    players = summary.get("response", {}).get("players", [])
    if not players:
        raise ValueError(f"Steam ID {steam_id} not found")
    profile = players[0]
    lib = await steam_call("IPlayerService/GetOwnedGames/v1", {
        "steamid": steam_id,
        "include_appinfo": "true", "include_played_free_games": "true",
    })
    games = lib.get("response", {}).get("games", [])
    game_dict = {}
    for g in games:
        appid = g["appid"]
        game_dict[appid] = {
            "appid": appid, "name": g.get("name", f"App {appid}"),
            "hours": round(g.get("playtime_forever", 0) / 60, 1),
            "hours_2weeks": round(g.get("playtime_2weeks", 0) / 60, 1),
            "last_played": g.get("rtime_last_played", 0),
            "img_icon": g.get("img_icon_url", ""),
        }
    return {
        "steam_id": steam_id,
        "name": profile.get("personaname", steam_id),
        "avatar": profile.get("avatarfull", ""),
        "game_count": len(game_dict),
        "game_dict": game_dict,
    }


def analyze_group(players: dict) -> dict:
    player_list = list(players.values())
    n = len(player_list)
    if n < 2:
        return {"error": "Need at least 2 players"}

    all_games: dict[int, dict] = {}
    for p in player_list:
        for appid, g in p.get("game_dict", {}).items():
            if appid not in all_games:
                all_games[appid] = {"appid": appid, "name": g["name"], "players": {},
                                     "player_count": 0, "total_hours": 0, "max_hours": 0}
            all_games[appid]["players"][p["steam_id"]] = {
                "name": p["name"], "hours": g["hours"],
                "hours_2weeks": g["hours_2weeks"], "last_played": g["last_played"],
                "avatar": p["avatar"],
            }
            all_games[appid]["player_count"] += 1
            all_games[appid]["total_hours"] += g["hours"]
            all_games[appid]["max_hours"] = max(all_games[appid]["max_hours"], g["hours"])

    common = [g for g in all_games.values() if g["player_count"] == n]
    common.sort(key=lambda g: g["total_hours"], reverse=True)

    near = [g for g in all_games.values() if n // 2 < g["player_count"] < n]
    near.sort(key=lambda g: (g["player_count"], g["total_hours"]), reverse=True)

    recs = []
    for g in near[:20]:
        missing, owning = [], []
        for p in player_list:
            (owning if p["steam_id"] in g["players"] else missing).append(p["name"])
        if missing:
            recs.append({"appid": g["appid"], "name": g["name"],
                         "owned_by": owning, "missing_for": missing,
                         "owned_count": g["player_count"],
                         "total_hours": round(g["total_hours"], 1)})

    pairs = []
    for i in range(len(player_list)):
        for j in range(i + 1, len(player_list)):
            p1, p2 = player_list[i], player_list[j]
            shared = []
            for appid, g1 in p1.get("game_dict", {}).items():
                if appid in p2.get("game_dict", {}):
                    g2 = p2["game_dict"][appid]
                    shared.append({"appid": appid, "name": g1["name"],
                                   "p1_hours": g1["hours"], "p2_hours": g2["hours"],
                                   "overlap": min(g1["hours"], g2["hours"])})
            shared.sort(key=lambda x: x["overlap"], reverse=True)
            pairs.append({"p1": {"name": p1["name"], "steam_id": p1["steam_id"], "avatar": p1["avatar"]},
                          "p2": {"name": p2["name"], "steam_id": p2["steam_id"], "avatar": p2["avatar"]},
                          "shared_count": len(shared), "top_shared": shared[:5]})
    pairs.sort(key=lambda x: x["shared_count"], reverse=True)

    return {"player_count": n, "common_games": common[:30],
            "recommendations": recs, "pair_overlaps": pairs,
            "total_shared_games": len(all_games)}


# ─── Routes ──────────────────────────────────────────────

@app.post("/api/session/create")
async def create_session(steam_id: str = Form(...)):
    sid = await resolve_steam_id(steam_id)
    player = await fetch_player(sid)
    session_id = secrets.token_hex(6)
    SESSIONS[session_id] = {"session_id": session_id, "host_steam_id": sid,
                            "players": {sid: player}, "locked": False,
                            "created": datetime.now(timezone.utc).isoformat()}
    return {"session_id": session_id, "host": player["name"]}


@app.post("/api/session/{session_id}/join")
async def join_session(session_id: str, steam_id: str = Form(...)):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    session = SESSIONS[session_id]
    if session.get("locked"):
        raise HTTPException(400, "Session locked")
    sid = await resolve_steam_id(steam_id)
    if sid in session["players"]:
        return {"status": "already_joined", "name": session["players"][sid]["name"],
                "player_count": len(session["players"])}
    player = await fetch_player(sid)
    session["players"][sid] = player
    return {"status": "joined", "name": player["name"],
            "game_count": player["game_count"], "player_count": len(session["players"])}


@app.get("/api/session/{session_id}/status")
async def session_status(session_id: str):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    s = SESSIONS[session_id]
    players = [{"name": p["name"], "steam_id": p["steam_id"],
                "game_count": p["game_count"], "avatar": p["avatar"]}
               for p in s["players"].values()]
    return {"session_id": session_id, "host_steam_id": s["host_steam_id"],
            "players": players, "player_count": len(players),
            "locked": s.get("locked", False), "has_results": "results" in s}


@app.post("/api/session/{session_id}/analyze")
async def analyze_session(session_id: str):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    session = SESSIONS[session_id]
    if len(session["players"]) < 2:
        raise HTTPException(400, "Need at least 2 players")
    session["locked"] = True
    results = analyze_group(session["players"])
    session["results"] = results
    return {"status": "ok", **results}


@app.get("/api/session/{session_id}/results")
async def get_results(session_id: str):
    if session_id not in SESSIONS:
        raise HTTPException(404, "Session not found")
    if "results" not in SESSIONS[session_id]:
        raise HTTPException(404, "No results yet")
    return SESSIONS[session_id]["results"]
