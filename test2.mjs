import { Readable } from 'stream';
import { ProxyAgent } from 'undici';
import handler from './api/index.js';

async function main() {
  // Patch global fetch to use proxy
  const proxyAgent = new ProxyAgent('http://127.0.0.1:7897');
  const origFetch = globalThis.fetch;
  globalThis.fetch = (url, opts = {}) => {
    opts.dispatcher = proxyAgent;
    return origFetch(url, opts);
  };

  const body = 'steam_id=52587';
  const req = Readable.from([body]);
  req.method = 'POST';
  req.url = '/api/session/create';
  req.headers = { 'content-type': 'application/x-www-form-urlencoded' };

  const res = {
    _status: 200,
    _data: null,
    status(s) { this._status = s; return this; },
    json(d) { this._data = d; console.log('Status:', this._status); console.log('Data:', JSON.stringify(d).substring(0, 500)); },
    setHeader() {},
    end() {}
  };

  try {
    await handler(req, res);
  } catch (e) {
    console.log('Error:', e.message);
    console.log(e.stack?.substring(0, 500));
  }
}

main().catch(e => console.log('Top:', e.message));
