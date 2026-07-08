// Vercel serverless function — no dependencies needed (Node 18+ has fetch built-in)
const STEAM_KEY = "4A8CB88E2B47982AB099C17E4E56420A";

const sessions = {};
const cache = {};
const CACHE_TTL = 300_000;

function cacheKey(...parts) { return parts.join("::"); }

async function steamCall(endpoint, params) {
  const key = cacheKey(endpoint, JSON.stringify(Object.entries(params).sort()));
  const cached = cache[key];
  if (cached && Date.now() - cached.ts < CACHE_TTL) return cached.data;

  const url = new URL(`https://api.steampowered.com/${endpoint}/`);
  url.searchParams.set("key", STEAM_KEY);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, String(v));

  const resp = await fetch(url, { signal: AbortSignal.timeout(25000) });
  const data = await resp.json();
  cache[key] = { data, ts: Date.now() };
  return data;
}

async function resolveSteamId(raw) {
  raw = raw.trim().replace(/\/+$/, "");
  if (/^\d{17}$/.test(raw) && raw.startsWith("7656119")) return raw;
  const profMatch = raw.match(/\/profiles\/(\d{17})/);
  if (profMatch) return profMatch[1];
  let vanity = raw;
  const idMatch = raw.match(/\/id\/([^\/]+)/);
  if (idMatch) vanity = idMatch[1];
  else vanity = vanity.replace(/https?:\/\//, "").replace("steamcommunity.com/", "");
  if (!vanity) throw new Error("Cannot parse Steam ID");
  const res = await steamCall("ISteamUser/ResolveVanityURL/v1", { vanityurl: vanity });
  const result = res?.response;
  if (result?.success !== 1) throw new Error(`Cannot resolve profile: ${vanity}`);
  return result.steamid;
}

async function fetchPlayer(steamId) {
  const summary = await steamCall("ISteamUser/GetPlayerSummaries/v2", { steamids: steamId });
  const profile = summary?.response?.players?.[0];
  if (!profile) throw new Error(`Steam ID ${steamId} not found`);
  const lib = await steamCall("IPlayerService/GetOwnedGames/v1", {
    steamid: steamId, include_appinfo: "true", include_played_free_games: "true",
  });
  const games = lib?.response?.games || [];
  const gameDict = {};
  for (const g of games) {
    gameDict[g.appid] = {
      appid: g.appid,
      name: g.name || `App ${g.appid}`,
      hours: Math.round((g.playtime_forever || 0) / 6) / 10,
      hours_2weeks: Math.round((g.playtime_2weeks || 0) / 6) / 10,
      last_played: g.rtime_last_played || 0,
      img_icon: g.img_icon_url || "",
    };
  }
  return {
    steam_id: steamId,
    name: profile.personaname || steamId,
    avatar: profile.avatarfull || "",
    game_count: Object.keys(gameDict).length,
    game_dict: gameDict,
  };
}

function analyzeGroup(players) {
  const playerList = Object.values(players);
  const n = playerList.length;
  if (n < 2) return { error: "Need at least 2 players" };

  const allGames = {};
  for (const p of playerList) {
    for (const [appid, g] of Object.entries(p.game_dict || {})) {
      if (!allGames[appid]) {
        allGames[appid] = { appid: Number(appid), name: g.name, players: {}, player_count: 0, total_hours: 0, max_hours: 0 };
      }
      allGames[appid].players[p.steam_id] = {
        name: p.name, hours: g.hours, hours_2weeks: g.hours_2weeks,
        last_played: g.last_played, avatar: p.avatar,
      };
      allGames[appid].player_count++;
      allGames[appid].total_hours += g.hours;
      allGames[appid].max_hours = Math.max(allGames[appid].max_hours, g.hours);
    }
  }

  const common = Object.values(allGames).filter(g => g.player_count === n);
  common.sort((a, b) => b.total_hours - a.total_hours);

  const near = Object.values(allGames).filter(g => g.player_count > n / 2 && g.player_count < n);
  near.sort((a, b) => b.player_count !== a.player_count ? b.player_count - a.player_count : b.total_hours - a.total_hours);

  const recs = near.slice(0, 20).map(g => {
    const owning = [], missing = [];
    for (const p of playerList) {
      (p.steam_id in g.players ? owning : missing).push(p.name);
    }
    return { appid: g.appid, name: g.name, owned_by: owning, missing_for: missing, owned_count: g.player_count, total_hours: Math.round(g.total_hours * 10) / 10 };
  }).filter(r => r.missing_for.length > 0);

  const pairs = [];
  for (let i = 0; i < playerList.length; i++) {
    for (let j = i + 1; j < playerList.length; j++) {
      const p1 = playerList[i], p2 = playerList[j];
      const shared = [];
      for (const [appid, g1] of Object.entries(p1.game_dict || {})) {
        const g2 = (p2.game_dict || {})[appid];
        if (g2) {
          shared.push({ appid: Number(appid), name: g1.name, p1_hours: g1.hours, p2_hours: g2.hours, overlap: Math.min(g1.hours, g2.hours) });
        }
      }
      shared.sort((a, b) => b.overlap - a.overlap);
      pairs.push({
        p1: { name: p1.name, steam_id: p1.steam_id, avatar: p1.avatar },
        p2: { name: p2.name, steam_id: p2.steam_id, avatar: p2.avatar },
        shared_count: shared.length,
        top_shared: shared.slice(0, 5),
      });
    }
  }
  pairs.sort((a, b) => b.shared_count - a.shared_count);

  return { player_count: n, common_games: common.slice(0, 30), recommendations: recs, pair_overlaps: pairs, total_shared_games: Object.keys(allGames).length };
}

// ─── Router ───────────────────────────────────────────

async function route(method, path, body) {
  let form = {};
  if (body) {
    for (const pair of body.split("&")) {
      const [k, v] = pair.split("=");
      if (k) form[decodeURIComponent(k)] = decodeURIComponent(v || "");
    }
  }

  try {
    // POST /api/session/create
    if (method === "POST" && path === "/api/session/create") {
      const steamId = form.steam_id || "";
      if (!steamId) return [400, { error: "Missing steam_id" }];
      const sid = await resolveSteamId(steamId);
      const player = await fetchPlayer(sid);
      const sessionId = [...Array(12)].map(() => Math.random().toString(36)[2]).join("");
      sessions[sessionId] = { session_id: sessionId, host_steam_id: sid, players: { [sid]: player }, locked: false, created: new Date().toISOString() };
      return [200, { session_id: sessionId, host: player.name }];
    }

    // POST /api/session/{id}/join
    const joinMatch = path.match(/^\/api\/session\/([a-z0-9]+)\/join$/);
    if (method === "POST" && joinMatch) {
      const sessionId = joinMatch[1];
      const session = sessions[sessionId];
      if (!session) return [404, { error: "Session not found" }];
      if (session.locked) return [400, { error: "Session locked" }];
      const steamId = form.steam_id || "";
      if (!steamId) return [400, { error: "Missing steam_id" }];
      const sid = await resolveSteamId(steamId);
      if (session.players[sid]) return [200, { status: "already_joined", name: session.players[sid].name, player_count: Object.keys(session.players).length }];
      const player = await fetchPlayer(sid);
      session.players[sid] = player;
      return [200, { status: "joined", name: player.name, game_count: player.game_count, player_count: Object.keys(session.players).length }];
    }

    // GET /api/session/{id}/status
    const statusMatch = path.match(/^\/api\/session\/([a-z0-9]+)\/status$/);
    if (method === "GET" && statusMatch) {
      const sessionId = statusMatch[1];
      const s = sessions[sessionId];
      if (!s) return [404, { error: "Session not found" }];
      const players = Object.values(s.players).map(p => ({ name: p.name, steam_id: p.steam_id, game_count: p.game_count, avatar: p.avatar }));
      return [200, { session_id: sessionId, host_steam_id: s.host_steam_id, players, player_count: players.length, locked: s.locked || false, has_results: !!s.results }];
    }

    // POST /api/session/{id}/analyze
    const analyzeMatch = path.match(/^\/api\/session\/([a-z0-9]+)\/analyze$/);
    if (method === "POST" && analyzeMatch) {
      const sessionId = analyzeMatch[1];
      const s = sessions[sessionId];
      if (!s) return [404, { error: "Session not found" }];
      if (Object.keys(s.players).length < 2) return [400, { error: "Need at least 2 players" }];
      s.locked = true;
      const results = analyzeGroup(s.players);
      s.results = results;
      return [200, { status: "ok", ...results }];
    }

    // GET /api/session/{id}/results
    const resultsMatch = path.match(/^\/api\/session\/([a-z0-9]+)\/results$/);
    if (method === "GET" && resultsMatch) {
      const sessionId = resultsMatch[1];
      const s = sessions[sessionId];
      if (!s) return [404, { error: "Session not found" }];
      if (!s.results) return [404, { error: "No results yet" }];
      return [200, s.results];
    }

    return [404, { error: "Not found" }];
  } catch (e) {
    return [500, { error: e.message }];
  }
}

// ─── Vercel handler ────────────────────────────────────

export default async function handler(req, res) {
  // CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  const path = req.url.split("?")[0];
  const method = req.method;

  // Read body
  let body = "";
  if (method === "POST") {
    body = await new Promise((resolve) => {
      let data = "";
      req.on("data", chunk => data += chunk);
      req.on("end", () => resolve(data));
    });
  }

  const [status, data] = await route(method, path, body);
  res.status(status).json(data);
}
