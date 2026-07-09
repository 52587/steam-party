"""
Steam Party API for Vercel's Python runtime.

Sessions are kept in memory, which matches the original app behavior. Vercel can
cold-start or run multiple instances, so sessions are intentionally ephemeral.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx


STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "4A8CB88E2B47982AB099C17E4E56420A")
CACHE_TTL_SECONDS = 300

SESSIONS: dict[str, dict] = {}
API_CACHE: dict[str, tuple[dict, float]] = {}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cache_key(endpoint: str, params: dict[str, str]) -> str:
    return f"{endpoint}:{sorted(params.items())}"


async def steam_call(endpoint: str, params: dict[str, str]) -> dict:
    key = cache_key(endpoint, params)
    cached = API_CACHE.get(key)
    if cached:
        data, ts = cached
        if time.time() - ts < CACHE_TTL_SECONDS:
            return data

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.steampowered.com/{endpoint}/",
            params={"key": STEAM_API_KEY, **params},
        )
        resp.raise_for_status()
        data = resp.json()

    API_CACHE[key] = (data, time.time())
    if len(API_CACHE) > 500:
        now = time.time()
        stale_keys = [
            k for k, (_, ts) in API_CACHE.items()
            if now - ts > CACHE_TTL_SECONDS * 2
        ]
        for stale_key in stale_keys:
            del API_CACHE[stale_key]

    return data


async def resolve_steam_id(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if raw.isdigit() and len(raw) == 17 and raw.startswith("7656119"):
        return raw

    if "/profiles/" in raw:
        steam_id = raw.split("/profiles/", 1)[-1].split("/", 1)[0]
        if steam_id.isdigit() and len(steam_id) == 17:
            return steam_id

    if "/id/" in raw:
        vanity = raw.split("/id/", 1)[-1].split("/", 1)[0]
    else:
        vanity = raw.replace("https://", "").replace("http://", "")
        vanity = vanity.replace("steamcommunity.com/", "").rstrip("/")

    if not vanity:
        raise ApiError("Cannot parse Steam ID")

    resolved = await steam_call("ISteamUser/ResolveVanityURL/v1", {"vanityurl": vanity})
    result = resolved.get("response", {})
    if result.get("success") != 1:
        raise ApiError(f"Could not resolve profile: {vanity}", 404)
    return result["steamid"]


async def fetch_player(steam_id: str) -> dict:
    summary = await steam_call("ISteamUser/GetPlayerSummaries/v2", {"steamids": steam_id})
    players = summary.get("response", {}).get("players", [])
    if not players:
        raise ApiError(f"Steam ID {steam_id} not found", 404)

    profile = players[0]
    library = await steam_call(
        "IPlayerService/GetOwnedGames/v1",
        {
            "steamid": steam_id,
            "include_appinfo": "true",
            "include_played_free_games": "true",
        },
    )

    game_dict = {}
    for game in library.get("response", {}).get("games", []):
        appid = game["appid"]
        game_dict[appid] = {
            "appid": appid,
            "name": game.get("name", f"App {appid}"),
            "hours": round(game.get("playtime_forever", 0) / 60, 1),
            "hours_2weeks": round(game.get("playtime_2weeks", 0) / 60, 1),
            "last_played": game.get("rtime_last_played", 0),
            "img_icon": game.get("img_icon_url", ""),
        }

    return {
        "steam_id": steam_id,
        "name": profile.get("personaname", steam_id),
        "avatar": profile.get("avatarfull", ""),
        "game_count": len(game_dict),
        "game_dict": game_dict,
    }


def analyze_group(players: dict[str, dict]) -> dict:
    player_list = list(players.values())
    count = len(player_list)
    if count < 2:
        raise ApiError("Need at least 2 players")

    all_games: dict[int, dict] = {}
    for player in player_list:
        for appid, game in player.get("game_dict", {}).items():
            if appid not in all_games:
                all_games[appid] = {
                    "appid": appid,
                    "name": game["name"],
                    "players": {},
                    "player_count": 0,
                    "total_hours": 0,
                    "max_hours": 0,
                }

            all_games[appid]["players"][player["steam_id"]] = {
                "name": player["name"],
                "hours": game["hours"],
                "hours_2weeks": game["hours_2weeks"],
                "last_played": game["last_played"],
                "avatar": player["avatar"],
            }
            all_games[appid]["player_count"] += 1
            all_games[appid]["total_hours"] += game["hours"]
            all_games[appid]["max_hours"] = max(
                all_games[appid]["max_hours"],
                game["hours"],
            )

    common_games = [
        game for game in all_games.values()
        if game["player_count"] == count
    ]
    common_games.sort(key=lambda game: game["total_hours"], reverse=True)

    near_common_games = [
        game for game in all_games.values()
        if game["player_count"] > count / 2 and game["player_count"] < count
    ]
    near_common_games.sort(
        key=lambda game: (game["player_count"], game["total_hours"]),
        reverse=True,
    )

    recommendations = []
    for game in near_common_games[:20]:
        owning = []
        missing = []
        for player in player_list:
            target = owning if player["steam_id"] in game["players"] else missing
            target.append(player["name"])

        if missing:
            recommendations.append({
                "appid": game["appid"],
                "name": game["name"],
                "owned_by": owning,
                "missing_for": missing,
                "owned_count": game["player_count"],
                "total_hours": round(game["total_hours"], 1),
            })

    pair_overlaps = []
    for i, player_a in enumerate(player_list):
        for player_b in player_list[i + 1:]:
            shared = []
            player_b_games = player_b.get("game_dict", {})
            for appid, game_a in player_a.get("game_dict", {}).items():
                game_b = player_b_games.get(appid)
                if game_b:
                    shared.append({
                        "appid": appid,
                        "name": game_a["name"],
                        "p1_hours": game_a["hours"],
                        "p2_hours": game_b["hours"],
                        "overlap": min(game_a["hours"], game_b["hours"]),
                    })

            shared.sort(key=lambda game: game["overlap"], reverse=True)
            pair_overlaps.append({
                "p1": {
                    "name": player_a["name"],
                    "steam_id": player_a["steam_id"],
                    "avatar": player_a["avatar"],
                },
                "p2": {
                    "name": player_b["name"],
                    "steam_id": player_b["steam_id"],
                    "avatar": player_b["avatar"],
                },
                "shared_count": len(shared),
                "top_shared": shared[:5],
            })

    pair_overlaps.sort(key=lambda pair: pair["shared_count"], reverse=True)

    return {
        "player_count": count,
        "common_games": common_games[:30],
        "recommendations": recommendations,
        "pair_overlaps": pair_overlaps,
        "total_shared_games": len(all_games),
    }


def parse_form_body(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


async def handle_api(method: str, path: str, query: dict[str, list[str]], body: bytes) -> tuple[int, dict]:
    form = parse_form_body(body) if body else {}

    try:
        if method == "POST" and path == "/api/session/create":
            steam_id = form.get("steam_id") or query.get("steam_id", [""])[0]
            if not steam_id:
                raise ApiError("Missing steam_id")

            resolved_id = await resolve_steam_id(steam_id)
            player = await fetch_player(resolved_id)
            session_id = secrets.token_hex(6)
            SESSIONS[session_id] = {
                "session_id": session_id,
                "host_steam_id": resolved_id,
                "players": {resolved_id: player},
                "locked": False,
                "created": datetime.now(timezone.utc).isoformat(),
            }
            return 200, {"session_id": session_id, "host": player["name"]}

        if method == "POST" and path.startswith("/api/session/") and path.endswith("/join"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)
            if session.get("locked"):
                raise ApiError("Session is locked")

            steam_id = form.get("steam_id", "")
            if not steam_id:
                raise ApiError("Missing steam_id")

            resolved_id = await resolve_steam_id(steam_id)
            if resolved_id in session["players"]:
                return 200, {
                    "status": "already_joined",
                    "name": session["players"][resolved_id]["name"],
                    "player_count": len(session["players"]),
                }

            player = await fetch_player(resolved_id)
            session["players"][resolved_id] = player
            return 200, {
                "status": "joined",
                "name": player["name"],
                "game_count": player["game_count"],
                "player_count": len(session["players"]),
            }

        if method == "GET" and path.startswith("/api/session/") and path.endswith("/status"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)

            players = [
                {
                    "name": player["name"],
                    "steam_id": player["steam_id"],
                    "game_count": player["game_count"],
                    "avatar": player["avatar"],
                }
                for player in session["players"].values()
            ]
            return 200, {
                "session_id": session_id,
                "host_steam_id": session["host_steam_id"],
                "players": players,
                "player_count": len(players),
                "locked": session.get("locked", False),
                "has_results": "results" in session,
            }

        if method == "POST" and path.startswith("/api/session/") and path.endswith("/analyze"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)
            if len(session["players"]) < 2:
                raise ApiError("Need at least 2 players")

            session["locked"] = True
            results = analyze_group(session["players"])
            session["results"] = results
            return 200, {"status": "ok", **results}

        if method == "GET" and path.startswith("/api/session/") and path.endswith("/results"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)
            if "results" not in session:
                raise ApiError("No results yet", 404)

            return 200, session["results"]

        raise ApiError(f"Unknown endpoint: {method} {path}", 404)

    except ApiError as exc:
        return exc.status, {"error": str(exc)}
    except httpx.HTTPError:
        return 502, {"error": "Steam API request failed"}
    except Exception as exc:
        return 500, {"error": f"Internal error: {exc}"}


class App:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return

        method = scope["method"]
        path = scope.get("path", "/")
        query_string = scope.get("query_string", b"").decode("utf-8", "replace")
        query = parse_qs(query_string, keep_blank_values=True)

        headers = {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "Content-Type",
            "cache-control": "no-cache",
        }

        if method == "OPTIONS":
            await self._send_json(send, 204, headers, {})
            return

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        status, result = await handle_api(method, path, query, body)
        await self._send_json(send, status, headers, result)

    async def _send_json(self, send, status: int, headers: dict[str, str], data: dict):
        response_body = b"" if status == 204 else json.dumps(data, ensure_ascii=False).encode("utf-8")
        response_headers = {
            **headers,
            "content-length": str(len(response_body)),
        }
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (key.encode("ascii"), value.encode("ascii"))
                for key, value in response_headers.items()
            ],
        })
        await send({"type": "http.response.body", "body": response_body})


app = App()
