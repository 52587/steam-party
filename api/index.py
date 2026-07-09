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
from urllib.parse import urlsplit

import httpx


STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "4A8CB88E2B47982AB099C17E4E56420A")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "YOUR_DEEPSEEK_API_KEY")
DEEPSEEK_API_URLS = (
    "https://api.deepseek.com.cn/v1/chat/completions",
    "https://api.deepseek.com/v1/chat/completions",
)
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT = httpx.Timeout(40.0, connect=10.0, read=30.0)
CACHE_TTL_SECONDS = 300
AI_MATCH_DESCS = {"完美匹配", "非常匹配", "比较匹配", "还行"}
AI_TEMPERATURES = (0.7, 0.9, 1.0)

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


def is_deepseek_configured() -> bool:
    key = DEEPSEEK_API_KEY.strip()
    return bool(key) and key not in {"YOUR_DEEPSEEK_API_KEY", "PLACEHOLDER"} and not key.startswith("@")


def deepseek_base_url(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def describe_deepseek_connection_error(exc: httpx.HTTPError) -> str:
    detail = str(exc) or exc.__class__.__name__
    cause = getattr(exc, "__cause__", None)
    if cause:
        detail = f"{detail}; cause={cause}"

    lower_detail = detail.lower()
    if isinstance(exc, httpx.ConnectTimeout):
        reason = "连接超时"
    elif isinstance(exc, httpx.ReadTimeout):
        reason = "读取超时"
    elif isinstance(exc, httpx.TimeoutException):
        reason = "请求超时"
    elif isinstance(exc, httpx.ConnectError) and any(
        marker in lower_detail
        for marker in (
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "dns",
            "getaddrinfo",
        )
    ):
        reason = "DNS 解析失败"
    elif isinstance(exc, httpx.ConnectError):
        reason = "连接失败"
    else:
        reason = "网络错误"

    return f"{reason}: {detail}"


async def test_deepseek_connection(client: httpx.AsyncClient, api_url: str) -> None:
    await client.get(deepseek_base_url(api_url))


def player_top_games(player: dict, limit: int = 20) -> list[dict]:
    games = list(player.get("game_dict", {}).values())
    games.sort(
        key=lambda game: (game.get("hours", 0), game.get("hours_2weeks", 0)),
        reverse=True,
    )
    return games[:limit]


def games_for_preference(game_dict: dict, max_count: int = 25) -> list[dict]:
    games = list(game_dict.values())
    games.sort(
        key=lambda game: (game.get("hours", 0), game.get("hours_2weeks", 0)),
        reverse=True,
    )

    filtered_games = [
        game for game in games[5:]
        if game.get("hours", 0) >= 1
    ]
    if len(filtered_games) < 10:
        return games[:10]

    return filtered_games[:max_count]


def owned_game_names(players: dict[str, dict]) -> set[str]:
    names = set()
    for player in players.values():
        for game in player.get("game_dict", {}).values():
            name = game.get("name", "").strip().lower()
            if name:
                names.add(name)
    return names


def build_ai_prompt(players: dict[str, dict]) -> str:
    player_sections = []
    owned_names = sorted({
        game.get("name", "").strip()
        for player in players.values()
        for game in player.get("game_dict", {}).values()
        if game.get("name", "").strip()
    })

    for idx, player in enumerate(players.values(), start=1):
        preference_games = games_for_preference(player.get("game_dict", {}))
        game_lines = [
            f"  - {game['name']}：{game.get('hours', 0)} 小时"
            for game in preference_games
        ]
        player_sections.append(
            "\n".join([
                f"玩家 {idx}：{player['name']}（steam_id: {player['steam_id']}）",
                f"游戏总数：{player.get('game_count', len(player.get('game_dict', {})))}",
                "用于偏好分析的游戏（已排除游玩时长最高的 5 款，并过滤少于 1 小时的游戏；小库玩家会回退到前 10 款）：",
                *game_lines,
            ])
        )

    owned_list_for_prompt = "、".join(owned_names[:600])
    if len(owned_names) > 600:
        owned_list_for_prompt += f"、……（共 {len(owned_names)} 款，后端会继续按完整库过滤）"

    return f"""你是一个懂 Steam 多人游戏和合作游戏的中文游戏推荐专家。

请基于以下玩家的 Steam 游戏库数据，先分析每位玩家的类型偏好，再给整个小队推荐 5-8 款适合多人联机或合作游玩的 Steam 游戏。

重要限制：
1. 推荐游戏必须适合多人联机、合作或同屏/线上多人游玩。
2. 不要推荐任何玩家已经拥有的游戏，尤其不要推荐下方“已拥有游戏排除名单”中的游戏。
3. 推荐理由要结合不同玩家的偏好，解释为什么适合整个小队。
4. 只返回合法 JSON，不要返回 Markdown，不要返回额外解释。
5. match_score 必须是 0-100 的整数。
6. match_desc 只能是以下四个值之一："完美匹配"、"非常匹配"、"比较匹配"、"还行"。
7. 每位玩家的 preferences 是类型偏好百分比，value 必须是 0-100 的数字，同一玩家所有 value 总和约等于 100。
8. 每位玩家返回 4-6 个最主要的偏好类型，type 用简短中文，例如“合作生存”“策略经营”“动作射击”“开放世界”“剧情探索”。

玩家数据：
{chr(10).join(player_sections)}

已拥有游戏排除名单：
{owned_list_for_prompt}

请严格返回以下 JSON 对象结构：
{{
  "players": [
    {{
      "steam_id": "玩家 steam_id",
      "name": "玩家名称",
      "preferences": [
        {{"type": "合作生存", "value": 35}},
        {{"type": "动作射击", "value": 25}},
        {{"type": "策略经营", "value": 20}},
        {{"type": "剧情探索", "value": 20}}
      ]
    }}
  ],
  "recommendations": [
    {{
      "name": "Game Name",
      "reason": "为什么要推荐这个游戏的详细理由",
      "match_score": 85,
      "match_desc": "非常匹配"
    }}
  ]
}}"""


def extract_ai_response(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    object_start = text.find("{")
    object_end = text.rfind("}")
    array_start = text.find("[")
    array_end = text.rfind("]")

    if (
        array_start != -1
        and array_end != -1
        and array_end > array_start
        and (object_start == -1 or array_start < object_start)
    ):
        start = array_start
        end = array_end
    elif object_start != -1 and object_end != -1 and object_end > object_start:
        start = object_start
        end = object_end
    else:
        raise ApiError("AI 返回内容不是有效的 JSON", 502)

    json_text = text[start:end + 1]
    if start == -1 or end == -1 or end < start:
        raise ApiError("AI 返回内容不是有效的 JSON", 502)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ApiError(f"AI 返回 JSON 解析失败: {exc}", 502) from exc

    if isinstance(parsed, list):
        return {"players": [], "recommendations": parsed}
    if isinstance(parsed, dict):
        recommendations = parsed.get("recommendations", [])
        players = parsed.get(
            "players",
            parsed.get("player_preferences", parsed.get("preferences", [])),
        )
        return {"players": players, "recommendations": recommendations}

    raise ApiError("AI 返回内容不是推荐对象", 502)


def normalize_preference_items(raw_items) -> list[dict]:
    if isinstance(raw_items, dict):
        iterable = [
            {"type": key, "value": value}
            for key, value in raw_items.items()
        ]
    elif isinstance(raw_items, list):
        iterable = raw_items
    else:
        return []

    preferences = []
    for item in iterable:
        if not isinstance(item, dict):
            continue

        type_name = str(
            item.get("type")
            or item.get("genre")
            or item.get("name")
            or item.get("label")
            or ""
        ).strip()
        if not type_name:
            continue

        raw_value = (
            item.get("value")
            if "value" in item
            else item.get("percentage", item.get("percent", item.get("score", 0)))
        )
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(100, value))
        if value <= 0:
            continue

        preferences.append({
            "type": type_name[:24],
            "value": value,
        })

    preferences.sort(key=lambda item: item["value"], reverse=True)
    preferences = preferences[:6]
    total = sum(item["value"] for item in preferences)
    if total <= 0:
        return []

    normalized = []
    running_total = 0
    for index, item in enumerate(preferences):
        if index == len(preferences) - 1:
            value = max(0, 100 - running_total)
        else:
            value = int(round(item["value"] / total * 100))
            running_total += value
        normalized.append({
            "type": item["type"],
            "value": value,
        })

    return [item for item in normalized if item["value"] > 0]


def normalize_ai_preferences(raw_players, players: dict[str, dict]) -> list[dict]:
    if isinstance(raw_players, dict):
        converted_players = []
        for key, value in raw_players.items():
            if isinstance(value, dict):
                player_value = dict(value)
                if key in players:
                    player_value.setdefault("steam_id", key)
                else:
                    player_value.setdefault("name", key)
                converted_players.append(player_value)
        raw_players = converted_players
    elif not isinstance(raw_players, list):
        raw_players = []

    raw_by_id = {}
    raw_by_name = {}
    for raw_player in raw_players:
        if not isinstance(raw_player, dict):
            continue
        steam_id = str(raw_player.get("steam_id", "")).strip()
        name = str(raw_player.get("name", "")).strip().lower()
        if steam_id:
            raw_by_id[steam_id] = raw_player
        if name:
            raw_by_name[name] = raw_player

    normalized_players = []
    for player in players.values():
        raw_player = raw_by_id.get(player["steam_id"]) or raw_by_name.get(player["name"].strip().lower()) or {}
        raw_preferences = (
            raw_player.get("preferences")
            or raw_player.get("type_preferences")
            or raw_player.get("genres")
            or []
        )
        preferences = normalize_preference_items(raw_preferences)
        if not preferences:
            continue

        normalized_players.append({
            "steam_id": player["steam_id"],
            "name": player["name"],
            "avatar": player.get("avatar", ""),
            "game_count": player.get("game_count", len(player.get("game_dict", {}))),
            "preferences": preferences,
        })

    return normalized_players


def normalize_ai_recommendations(raw_items: list, owned_names: set[str]) -> list[dict]:
    recommendations = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        if not name or name.lower() in owned_names:
            continue

        reason = str(item.get("reason", "")).strip()
        if not reason:
            reason = "这款游戏与小队的整体游玩偏好较匹配，适合作为下一款联机游戏尝试。"

        try:
            match_score = int(item.get("match_score", 0))
        except (TypeError, ValueError):
            match_score = 0
        match_score = max(0, min(100, match_score))

        match_desc = str(item.get("match_desc", "")).strip()
        if match_desc not in AI_MATCH_DESCS:
            if match_score >= 90:
                match_desc = "完美匹配"
            elif match_score >= 80:
                match_desc = "非常匹配"
            elif match_score >= 65:
                match_desc = "比较匹配"
            else:
                match_desc = "还行"

        recommendations.append({
            "name": name,
            "reason": reason,
            "match_score": match_score,
            "match_desc": match_desc,
        })

        if len(recommendations) >= 8:
            break

    return recommendations


def query_flag(query: dict[str, list[str]], name: str) -> bool:
    value = query.get(name, [""])[-1].strip().lower()
    return value in {"1", "true", "yes", "on"}


def next_ai_temperature(session: dict) -> float:
    index = session.get("ai_recommend_temperature_index", 0)
    temperature = AI_TEMPERATURES[index % len(AI_TEMPERATURES)]
    session["ai_recommend_temperature_index"] = index + 1
    return temperature


async def generate_ai_recommendations(session: dict, temperature: float = 0.7) -> dict:
    if not is_deepseek_configured():
        raise ApiError("DeepSeek API Key 未配置，请在环境变量 DEEPSEEK_API_KEY 中设置后重试。", 503)

    players = session.get("players", {})
    if len(players) < 2:
        raise ApiError("Need at least 2 players")

    prompt = build_ai_prompt(players)
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "你是一个专业、克制、了解 Steam 多人游戏生态的中文游戏推荐助手，只输出合法 JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": 2600,
    }

    connection_errors = []
    async with httpx.AsyncClient(timeout=DEEPSEEK_TIMEOUT) as client:
        for api_url in DEEPSEEK_API_URLS:
            try:
                await test_deepseek_connection(client, api_url)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                connection_errors.append(
                    f"{deepseek_base_url(api_url)} 连接测试失败 ({describe_deepseek_connection_error(exc)})"
                )
                continue

            try:
                resp = await client.post(
                    api_url,
                    headers={
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:200] if exc.response is not None else ""
                raise ApiError(f"DeepSeek API HTTP 状态错误: {detail or exc}", 502) from exc
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                connection_errors.append(
                    f"{api_url} 请求连接失败 ({describe_deepseek_connection_error(exc)})"
                )
                continue
            except httpx.HTTPError as exc:
                raise ApiError(f"DeepSeek API 请求失败: {exc}", 502) from exc
            except json.JSONDecodeError as exc:
                raise ApiError("DeepSeek API 返回内容不是有效 JSON", 502) from exc
        else:
            detail = "；".join(connection_errors) or "没有可用端点"
            raise ApiError(f"DeepSeek API 连接失败，所有端点均不可达: {detail}", 502)

    choices = data.get("choices") or []
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise ApiError("DeepSeek API 没有返回推荐内容", 502)

    raw_ai_response = extract_ai_response(content)
    preferences = normalize_ai_preferences(raw_ai_response.get("players", []), players)
    recommendations = normalize_ai_recommendations(
        raw_ai_response.get("recommendations", []),
        owned_game_names(players),
    )
    if not recommendations:
        raise ApiError("AI 没有生成可用的未拥有游戏推荐，请重试。", 502)
    return {
        "preferences": preferences,
        "recommendations": recommendations,
    }


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
                "has_ai_recommendations": bool(session.get("ai_recommendations")),
                "has_ai_preferences": bool(session.get("ai_preferences")),
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

        if method == "POST" and path.startswith("/api/session/") and path.endswith("/ai-recommend"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)

            refresh = query_flag(query, "refresh") or form.get("refresh", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            if refresh:
                session.pop("ai_recommendations", None)
                session.pop("ai_preferences", None)

            if "ai_recommendations" not in session:
                temperature = next_ai_temperature(session)
                ai_response = await generate_ai_recommendations(
                    session,
                    temperature=temperature,
                )
                session["ai_preferences"] = ai_response.get("preferences", [])
                session["ai_recommendations"] = ai_response.get("recommendations", [])

            return 200, {
                "status": "ok",
                "players": session.get("ai_preferences", []),
                "recommendations": session["ai_recommendations"],
            }

        if method == "GET" and path.startswith("/api/session/") and path.endswith("/ai-recommendations"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)
            if "ai_recommendations" not in session:
                raise ApiError("No AI recommendations yet", 404)

            return 200, {
                "status": "ok",
                "players": session.get("ai_preferences", []),
                "recommendations": session["ai_recommendations"],
            }

        if method == "GET" and path.startswith("/api/session/") and path.endswith("/ai-preferences"):
            session_id = path.split("/")[3]
            session = SESSIONS.get(session_id)
            if not session:
                raise ApiError("Session not found", 404)
            if "ai_preferences" not in session:
                raise ApiError("No AI preferences yet", 404)

            return 200, {
                "status": "ok",
                "players": session["ai_preferences"],
            }

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

        # Serve index.html for root and /s/ routes (SPA)
        if path == "/" or path.startswith("/s/"):
            await self._serve_static(send, "index.html")
            return

        status, result = await handle_api(method, path, query, body)
        await self._send_json(send, status, headers, result)

    async def _serve_static(self, send, filename: str):
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            filepath = os.path.join(os.path.dirname(script_dir), filename)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            body = content.encode("utf-8")
            response_headers = {
                "content-type": "text/html; charset=utf-8",
                "content-length": str(len(body)),
                "cache-control": "public, max-age=0, must-revalidate",
            }
        except FileNotFoundError:
            body = b"Not Found"
            response_headers = {
                "content-type": "text/plain",
                "content-length": "9",
            }
        await send({
            "type": "http.response.start",
            "status": 200 if body != b"Not Found" else 404,
            "headers": [
                (key.encode("ascii"), value.encode("ascii"))
                for key, value in response_headers.items()
            ],
        })
        await send({"type": "http.response.body", "body": body})

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
