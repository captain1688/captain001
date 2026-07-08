'use strict';

/**
 * Feishu (Lark) event-subscription verification helpers.
 *
 * Covers the three things Feishu requires when you point an "event
 * subscription" request URL at your server:
 *
 *   1. URL verification  - reply to the `url_verification` challenge.
 *   2. Token check       - confirm the request carries your Verification Token.
 *   3. Signature check   - validate `X-Lark-Signature` for encrypted mode.
 *   4. Decryption        - AES-256-CBC decrypt the `encrypt` payload.
 *
 * Docs: https://open.feishu.cn/document/server-docs/event-subscription-guide/overview
 */

const crypto = require('crypto');

/**
 * Decrypt an encrypted event body.
 *
 * Feishu encrypts with AES-256-CBC. The key is sha256(encryptKey); the
 * base64-decoded payload is [16-byte IV][ciphertext].
 *
 * @param {string} encrypt    base64 string from the `encrypt` field
 * @param {string} encryptKey the Encrypt Key configured in the Feishu console
 * @returns {object} the decrypted JSON object
 */
function decrypt(encrypt, encryptKey) {
  if (!encryptKey) {
    throw new Error('encryptKey is required to decrypt an encrypted payload');
  }
  const key = crypto.createHash('sha256').update(encryptKey).digest();
  const buf = Buffer.from(encrypt, 'base64');
  const iv = buf.subarray(0, 16);
  const data = buf.subarray(16);
  const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
  decipher.setAutoPadding(true);
  const out = Buffer.concat([decipher.update(data), decipher.final()]);
  return JSON.parse(out.toString('utf8'));
}

/**
 * Compute the Feishu request signature.
 * signature = sha256(timestamp + nonce + encryptKey + rawBody)
 *
 * @param {string} timestamp  X-Lark-Request-Timestamp header
 * @param {string} nonce      X-Lark-Request-Nonce header
 * @param {string} encryptKey the Encrypt Key
 * @param {string} rawBody    the raw request body string
 * @returns {string} hex signature
 */
function computeSignature(timestamp, nonce, encryptKey, rawBody) {
  return crypto
    .createHash('sha256')
    .update(timestamp + nonce + encryptKey + rawBody)
    .digest('hex');
}

/**
 * Constant-time compare of the provided signature against the expected one.
 * @returns {boolean}
 */
function verifySignature(timestamp, nonce, encryptKey, rawBody, signature) {
  const expected = computeSignature(timestamp, nonce, encryptKey, rawBody);
  const a = Buffer.from(expected);
  const b = Buffer.from(String(signature || ''));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

/**
 * Handle a raw event-subscription request and return what the server should
 * send back.
 *
 * @param {object}  opts
 * @param {string}  opts.rawBody          raw request body string
 * @param {object} [opts.headers={}]      request headers (lowercased keys)
 * @param {string} [opts.encryptKey]      Encrypt Key (required for encrypted mode)
 * @param {string} [opts.verificationToken] Verification Token to enforce
 * @returns {{status:number, body:object, event?:object, challenge?:string}}
 */
function handleEvent({ rawBody, headers = {}, encryptKey, verificationToken }) {
  let payload = JSON.parse(rawBody);

  // Encrypted mode: verify signature (when we can) then decrypt.
  if (payload.encrypt !== undefined) {
    const ts = headers['x-lark-request-timestamp'];
    const nonce = headers['x-lark-request-nonce'];
    const sig = headers['x-lark-signature'];
    if (sig !== undefined) {
      if (!encryptKey) {
        return { status: 500, body: { error: 'encryptKey not configured' } };
      }
      if (!verifySignature(ts, nonce, encryptKey, rawBody, sig)) {
        return { status: 401, body: { error: 'invalid signature' } };
      }
    }
    payload = decrypt(payload.encrypt, encryptKey);
  }

  // Token check (applies to verification and real events alike).
  if (verificationToken && payload.token && payload.token !== verificationToken) {
    return { status: 401, body: { error: 'invalid verification token' } };
  }

  // URL verification challenge.
  if (payload.type === 'url_verification') {
    return {
      status: 200,
      body: { challenge: payload.challenge },
      challenge: payload.challenge,
    };
  }

  // A real event — the caller can act on `event`.
  return { status: 200, body: { code: 0 }, event: payload };
}

module.exports = {
  decrypt,
  computeSignature,
  verifySignature,
  handleEvent,
};

// ---------------------------------------------------------------------------
// Run directly (`node feishu.js`) to start a verification server.
// ---------------------------------------------------------------------------
if (require.main === module) {
  const http = require('http');
  const PORT = process.env.PORT || 3000;
  const encryptKey = process.env.FEISHU_ENCRYPT_KEY;
  const verificationToken = process.env.FEISHU_VERIFICATION_TOKEN;

  const server = http.createServer((req, res) => {
    if (req.method !== 'POST') {
      res.writeHead(405, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'method not allowed' }));
      return;
    }
    let rawBody = '';
    req.on('data', (c) => (rawBody += c));
    req.on('end', () => {
      try {
        const result = handleEvent({
          rawBody,
          headers: req.headers,
          encryptKey,
          verificationToken,
        });
        if (result.challenge !== undefined) {
          console.log('[feishu] URL verification challenge answered');
        } else if (result.event) {
          console.log('[feishu] event received:', result.event.header?.event_type || result.event.type || 'unknown');
        }
        res.writeHead(result.status, { 'content-type': 'application/json' });
        res.end(JSON.stringify(result.body));
      } catch (err) {
        console.error('[feishu] error handling request:', err.message);
        res.writeHead(400, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: err.message }));
      }
    });
  });

  server.listen(PORT, () => {
    console.log(`[feishu] verification server listening on :${PORT}`);
    if (!encryptKey) console.log('[feishu] FEISHU_ENCRYPT_KEY not set (plain mode only)');
    if (!verificationToken) console.log('[feishu] FEISHU_VERIFICATION_TOKEN not set (token check disabled)');
  });
}
