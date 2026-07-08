'use strict';

/**
 * Self-contained tests for the Feishu verification helpers.
 * Run with: node feishu.test.js
 *
 * We encrypt/sign payloads exactly the way Feishu does, then feed them
 * through the handler to prove verification works end to end.
 */

const assert = require('assert');
const crypto = require('crypto');
const { handleEvent, computeSignature, decrypt } = require('./feishu');

const ENCRYPT_KEY = 'test-encrypt-key';
const VERIFICATION_TOKEN = 'test-verification-token';

// Encrypt a JSON object the way the Feishu server does.
function encryptPayload(obj, encryptKey) {
  const key = crypto.createHash('sha256').update(encryptKey).digest();
  const iv = crypto.randomBytes(16);
  const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
  const data = Buffer.concat([
    cipher.update(JSON.stringify(obj), 'utf8'),
    cipher.final(),
  ]);
  return Buffer.concat([iv, data]).toString('base64');
}

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log('  ok -', name);
}

// 1. Plain url_verification challenge.
test('plain url_verification returns challenge', () => {
  const body = JSON.stringify({
    type: 'url_verification',
    challenge: 'abc123',
    token: VERIFICATION_TOKEN,
  });
  const res = handleEvent({ rawBody: body, verificationToken: VERIFICATION_TOKEN });
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.body.challenge, 'abc123');
});

// 2. Wrong token is rejected.
test('wrong verification token is rejected', () => {
  const body = JSON.stringify({
    type: 'url_verification',
    challenge: 'abc123',
    token: 'WRONG',
  });
  const res = handleEvent({ rawBody: body, verificationToken: VERIFICATION_TOKEN });
  assert.strictEqual(res.status, 401);
});

// 3. Encrypted url_verification with a valid signature.
test('encrypted url_verification with valid signature', () => {
  const inner = { type: 'url_verification', challenge: 'zzz999', token: VERIFICATION_TOKEN };
  const encrypt = encryptPayload(inner, ENCRYPT_KEY);
  const rawBody = JSON.stringify({ encrypt });
  const timestamp = '1600000000';
  const nonce = 'nonce-1';
  const signature = computeSignature(timestamp, nonce, ENCRYPT_KEY, rawBody);
  const res = handleEvent({
    rawBody,
    headers: {
      'x-lark-request-timestamp': timestamp,
      'x-lark-request-nonce': nonce,
      'x-lark-signature': signature,
    },
    encryptKey: ENCRYPT_KEY,
    verificationToken: VERIFICATION_TOKEN,
  });
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.body.challenge, 'zzz999');
});

// 4. Bad signature is rejected.
test('encrypted request with bad signature is rejected', () => {
  const inner = { type: 'url_verification', challenge: 'x', token: VERIFICATION_TOKEN };
  const rawBody = JSON.stringify({ encrypt: encryptPayload(inner, ENCRYPT_KEY) });
  const res = handleEvent({
    rawBody,
    headers: {
      'x-lark-request-timestamp': '1600000000',
      'x-lark-request-nonce': 'nonce-1',
      'x-lark-signature': 'deadbeef',
    },
    encryptKey: ENCRYPT_KEY,
    verificationToken: VERIFICATION_TOKEN,
  });
  assert.strictEqual(res.status, 401);
});

// 5. decrypt() round-trips.
test('decrypt round-trips an encrypted payload', () => {
  const obj = { hello: 'world', n: 42 };
  const decrypted = decrypt(encryptPayload(obj, ENCRYPT_KEY), ENCRYPT_KEY);
  assert.deepStrictEqual(decrypted, obj);
});

// 6. A real event is passed through.
test('real event is returned to the caller', () => {
  const body = JSON.stringify({
    schema: '2.0',
    header: { event_type: 'im.message.receive_v1', token: VERIFICATION_TOKEN },
    event: { message: { content: 'hi' } },
  });
  const res = handleEvent({ rawBody: body, verificationToken: VERIFICATION_TOKEN });
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.event.header.event_type, 'im.message.receive_v1');
});

console.log(`\n${passed} tests passed`);
