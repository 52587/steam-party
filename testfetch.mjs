import { Readable } from 'stream';
import handler from './api/index.js';

async function main() {
  // Test raw fetch first
  const url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key=4A8CB88E2B47982AB099C17E4E56420A&steamids=76561198195124690";
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(25000) });
    const data = await resp.json();
    console.log('Direct fetch OK:', data?.response?.players?.[0]?.personaname);
  } catch (e) {
    console.log('Direct fetch error:', e.constructor.name, e.message);
  }
}

main().catch(e => console.log('Top:', e.message));
