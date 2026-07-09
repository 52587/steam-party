import { Readable } from 'stream';
import handler from './api/index.js';

async function main() {
  const body = 'steam_id=52587';
  const req = Readable.from([body]);
  req.method = 'POST';
  req.url = '/api/session/create';
  req.headers = { 'content-type': 'application/x-www-form-urlencoded' };

  const chunks = [];
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

main().catch(e => console.log('Top-level error:', e.message));
