// Opening a Switchboard room in a browser.
//
// The read half of `crypto.py`, and only the read half: a viewer decrypts and
// never seals, so there is no encrypt path here to get wrong. What it must
// match exactly is the other side's derivation, because a mismatch does not
// announce itself — it looks like "this key cannot open this room".
//
// Every constant below is load-bearing and comes from crypto.py, where NUL is
// the separator in every one of these strings:
//
//   subkey   HKDF-SHA256(key, info = "switchboard/v1/payload" NUL <workspace>)
//            and, past epoch 0, <workspace> NUL <epoch> as the info tail
//   payload  AES-256-GCM, 12-byte nonce, AAD "switchboard/v1" NUL ws NUL ctx
//   padding  0x00 marker, 4-byte big-endian length, then filler to a bucket
//   wire     base64url, unpadded
//
// Those separators are built as bytes below rather than written into template
// literals, because a NUL inside a string literal is invisible in every tool
// that will ever show you this file.
//
// `tests/test_web_reader.py` seals with the Python cipher and opens with this
// file in a real browser, which is the only test that can prove the two agree.

const utf8 = new TextEncoder();
const NUL = new Uint8Array([0]);

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

/** base64url or base64, padded or not, or `hex:` — the shapes people paste. */
export function decodeKey(text) {
  const key = String(text ?? "").trim();
  if (key.startsWith("hex:")) {
    const hex = key.slice(4);
    return Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));
  }
  return b64urlToBytes(key);
}

function b64urlToBytes(text) {
  const b64 = String(text).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** The workspace cipher, in the one direction a reader needs. */
export class RoomKey {
  constructor(raw, workspace) {
    this.raw = raw;
    this.workspace = workspace;
    this._subkeys = new Map();
  }

  static from(text, workspace) {
    return new RoomKey(decodeKey(text), workspace);
  }

  async _payloadKey(epoch) {
    if (this._subkeys.has(epoch)) return this._subkeys.get(epoch);
    // HKDF with no salt. An empty salt and 32 zero bytes are the same key to
    // HMAC — both pad out to the block size — which is what makes this match
    // `salt=None` on the Python side rather than merely resemble it.
    const tail = epoch
      ? concat(utf8.encode(this.workspace), NUL, utf8.encode(String(epoch)))
      : utf8.encode(this.workspace);
    const material = await crypto.subtle.importKey(
      "raw", this.raw, "HKDF", false, ["deriveBits"],
    );
    const bits = await crypto.subtle.deriveBits({
      name: "HKDF",
      hash: "SHA-256",
      salt: new Uint8Array(0),
      info: concat(utf8.encode("switchboard/v1/payload"), NUL, tail),
    }, material, 256);
    const key = await crypto.subtle.importKey(
      "raw", bits, "AES-GCM", false, ["decrypt"],
    );
    this._subkeys.set(epoch, key);
    return key;
  }

  /** Open an envelope, or throw.
   *
   * `context` is bound in as additional authenticated data, so a value cannot
   * be moved from one field to another and still open — the same guarantee
   * the Python reader relies on, and the reason this argument is not
   * optional.
   *
   * The epoch comes from the message rather than from a clock: a message
   * written seconds before a boundary must stay readable afterwards, and a
   * reader joining later must be able to open history.
   */
  async open(envelope, context) {
    if (!looksSealed(envelope)) {
      throw new Error(`expected an encrypted value at ${context}`);
    }
    const box = typeof envelope === "string" ? JSON.parse(envelope) : envelope;
    if (box.$swb !== 1) throw new Error(`unsupported envelope version ${box.$swb}`);
    const epoch = Number.isInteger(box.e) ? box.e : 0;
    const aad = concat(utf8.encode("switchboard/v1"), NUL,
                       utf8.encode(this.workspace), NUL, utf8.encode(context));
    const plain = new Uint8Array(await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: b64urlToBytes(box.n), additionalData: aad, tagLength: 128 },
      await this._payloadKey(epoch),
      b64urlToBytes(box.c),
    ));
    return JSON.parse(new TextDecoder().decode(unpad(plain)));
  }
}

/** Padding is detected from the payload itself rather than from a setting, so
 *  a padded and an unpadded writer both open. */
function unpad(bytes) {
  if (!bytes.length || bytes[0] !== 0x00) return bytes;
  const length = new DataView(bytes.buffer, bytes.byteOffset + 1, 4).getUint32(0);
  if (length > bytes.length - 5) {
    throw new Error("padded payload declares a length beyond its own size");
  }
  return bytes.subarray(5, 5 + length);
}

/** Is this an envelope, in either form it travels in — a payload field
 *  carrying a dict, or a text field carrying one serialized? */
export function looksSealed(value) {
  if (value && typeof value === "object" && "$swb" in value) return true;
  if (typeof value === "string" && value.startsWith("{")) {
    try {
      return looksSealed(JSON.parse(value));
    } catch {
      return false;
    }
  }
  return false;
}
