# Feishu (Lark) Verification

A zero-dependency (Node built-ins only) server for verifying a Feishu / Lark
**event subscription** request URL. It handles everything the Feishu console
requires when you point an event subscription at your endpoint:

1. **URL verification** — replies to the `url_verification` challenge.
2. **Verification Token** check.
3. **Signature** check (`X-Lark-Signature`) for encrypted mode.
4. **Decryption** — AES-256-CBC decrypt of the `encrypt` payload.

## Run the server

```bash
# Plain mode (only the Verification Token, no encryption)
FEISHU_VERIFICATION_TOKEN=your_token node feishu.js

# Encrypted mode (Encrypt Key set in the Feishu console)
FEISHU_VERIFICATION_TOKEN=your_token \
FEISHU_ENCRYPT_KEY=your_encrypt_key \
PORT=3000 node feishu.js
```

Then set the **Request URL** in the Feishu Open Platform console
(开发者后台 → 事件订阅) to your public endpoint, e.g. `https://your.host/`.
Feishu POSTs a challenge and the server echoes it back — verification passes.

| Env var                     | Required | Purpose                                            |
| --------------------------- | -------- | -------------------------------------------------- |
| `FEISHU_VERIFICATION_TOKEN` | no       | If set, requests with a mismatched token are 401'd |
| `FEISHU_ENCRYPT_KEY`        | no       | Required only if you enabled encryption            |
| `PORT`                      | no       | Listen port (default `3000`)                       |

## Use as a library

```js
const { handleEvent } = require('./feishu');

const result = handleEvent({
  rawBody,                 // raw request body string
  headers: req.headers,    // for signature verification
  encryptKey: process.env.FEISHU_ENCRYPT_KEY,
  verificationToken: process.env.FEISHU_VERIFICATION_TOKEN,
});
// result.status  -> HTTP status to return
// result.body    -> JSON to send back (echoes the challenge on verification)
// result.event   -> the decoded event, for real (non-verification) callbacks
```

## Test

```bash
node feishu.test.js
```

The tests encrypt and sign payloads exactly the way the Feishu server does,
then run them through the handler to prove URL verification, token checks,
signature validation, and decryption all work end to end.

## Reference

<https://open.feishu.cn/document/server-docs/event-subscription-guide/overview>
