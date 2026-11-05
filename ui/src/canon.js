// Recomputing the engine's hashes in the browser: canonical JSON and SHA-256.
// The engine hashes json.dumps(sort_keys, no spaces). JSON.parse would turn
// Python's 1.0 into 1 and change the hash, so rawParse keeps every number
// exactly as the engine wrote it.

class Num {
  constructor(text) { this.text = text; }
}

export function rawParse(text) {
  let i = 0;
  const ws = () => { while (/\s/.test(text[i])) i++; };
  function value() {
    ws();
    const c = text[i];
    if (c === "{") {
      const out = {};
      i++; ws();
      if (text[i] === "}") { i++; return out; }
      for (;;) {
        ws();
        const key = string();
        ws(); i++; // the colon
        out[key] = value();
        ws();
        if (text[i++] === "}") return out;
      }
    }
    if (c === "[") {
      const out = [];
      i++; ws();
      if (text[i] === "]") { i++; return out; }
      for (;;) {
        out.push(value());
        ws();
        if (text[i++] === "]") return out;
      }
    }
    if (c === '"') return string();
    for (const [word, v] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(word, i)) { i += word.length; return v; }
    }
    const m = /^-?\d+(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i));
    if (!m) throw new Error(`bad JSON at ${i}`);
    i += m[0].length;
    return new Num(m[0]);
  }
  function string() {
    const start = i;
    i++;
    while (text[i] !== '"') i += text[i] === "\\" ? 2 : 1;
    i++;
    return JSON.parse(text.slice(start, i));
  }
  return value();
}

// Python's json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
export function canonical(v) {
  if (v instanceof Num) return v.text;
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  const keys = Object.keys(v).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
}

export const hex = (bytes) => [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
export const fromHex = (h) => new Uint8Array((h.match(/../g) || []).map((b) => parseInt(b, 16)));
export const utf8 = (s) => new TextEncoder().encode(s);

export async function digest(algo, bytes) {
  return new Uint8Array(await crypto.subtle.digest(algo, bytes));
}

export async function sha256hex(text) {
  return hex(await digest("SHA-256", utf8(text)));
}

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function b58decode(text) {
  let n = 0n;
  for (const ch of text) {
    const d = B58.indexOf(ch);
    if (d < 0) throw new Error("bad base58");
    n = n * 58n + BigInt(d);
  }
  const bytes = [];
  while (n > 0n) { bytes.unshift(Number(n % 256n)); n /= 256n; }
  for (const ch of text) { if (ch !== "1") break; bytes.unshift(0); }
  return new Uint8Array(bytes);
}