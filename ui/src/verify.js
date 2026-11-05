// The receipt checks of verify/verify.py, run in the browser. Three groups:
// signed by the enclave, running the registered code, recorded on NEAR.
// ok: true = pass, null = pass with a warning, false = fail.
// Intel's signature over the quote needs Intel's collateral, so `make verify` checks that one.
import teeCompose from "../../docker-compose.tee.yml?raw";
import mainnetCompose from "../../docker-compose.mainnet.yml?raw";
import { b58decode, canonical, digest, fromHex, hex, rawParse, sha256hex, utf8 } from "./canon.js";
import { engineRaw, view } from "./api.js";

const COMPOSE = { "docker-compose.tee.yml": teeCompose, "docker-compose.mainnet.yml": mainnetCompose };
const BODY = 48; // TDX quote v4: 48-byte header, then the TD report
const RTMR0 = 328, REPORT_DATA = 520;

async function signatureOk(r) {
  const pub = r.signer_pubkey || "", sig = r.sig || "";
  if (!pub.startsWith("ed25519:") || !sig.startsWith("ed25519:")) return false;
  const { sig: _, ...unsigned } = r;
  const key = await crypto.subtle.importKey("raw", b58decode(pub.slice(8)), { name: "Ed25519" }, false, ["verify"]);
  return crypto.subtle.verify({ name: "Ed25519" }, key, b58decode(sig.slice(8)), utf8(canonical(unsigned)));
}

// dstack's runtime event digest: sha384(event_type as u32 LE + ":" + name + ":" + payload)
async function eventDigest(e) {
  const type = new Uint8Array(4);
  new DataView(type.buffer).setUint32(0, e.event_type, true);
  const parts = [type, utf8(`:${e.event}:`), fromHex(e.event_payload)];
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((at, p) => { all.set(p, at); return at + p.length; }, 0);
  return hex(await digest("SHA-384", all));
}

async function replay(digests) {
  let mr = new Uint8Array(48);
  for (const d of digests) {
    const next = new Uint8Array(96);
    next.set(mr);
    next.set(fromHex(d), 48);
    mr = await digest("SHA-384", next);
  }
  return hex(mr);
}

function asText(payload) {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(fromHex(payload));
    if (/^([0-9a-fA-F]{2})*$/.test(text)) return text.toLowerCase();
  } catch { /* raw bytes */ }
  return payload.toLowerCase();
}

export async function enclaveChecks(att, net) {
  const out = [];
  const keys = [b58decode(att.signer_pubkey.slice(8)), fromHex(att.encryption_pubkey), fromHex(att.schema_hash)];
  const joined = new Uint8Array(keys.reduce((n, k) => n + k.length, 0));
  keys.reduce((at, k) => { joined.set(k, at); return at + k.length; }, 0);
  const rd = hex(await digest("SHA-256", joined));
  out.push({ ok: att.report_data === rd, text: "report_data = sha256(signer key + encryption key + schema hash)" });
  if (!att.quote) {
    out.push({ ok: null, text: `enclave mode: ${att.mode} (no quote: not a TEE)` });
    return out;
  }
  const quote = fromHex(att.quote);
  const at = (o, n) => hex(quote.slice(BODY + o, BODY + o + n));
  out.push({ ok: at(REPORT_DATA, 64) === rd.padEnd(128, "0"), text: "the quote carries that report_data" });
  if (att.mode !== "tdx") {
    out.push({ ok: null, text: `enclave mode: ${att.mode} (not a real TEE, the quote proves nothing)` });
    return out;
  }
  out.push({ ok: true, text: "Intel TDX quote (its Intel signature is checked by make verify)" });
  const events = typeof att.event_log === "string" ? JSON.parse(att.event_log) : att.event_log;
  const filled = await Promise.all(events.map(async (e) => (e.imr === 3 && !e.digest ? await eventDigest(e) : e.digest)));
  const rtmr3 = events.filter((e) => e.imr === 3);
  const match = await Promise.all(rtmr3.map(async (e) => !e.digest || e.digest === (await eventDigest(e))));
  out.push({ ok: match.every(Boolean), text: `all ${rtmr3.length} RTMR3 events match their digests` });
  const replays = await Promise.all([0, 1, 2, 3].map((i) => replay(filled.filter((_, j) => events[j].imr === i))));
  out.push({ ok: replays.every((r, i) => r === at(RTMR0 + 48 * i, 48)), text: "replaying the event log gives the quote's RTMR0-3" });
  const logged = Object.fromEntries(rtmr3.map((e) => [e.event, asText(e.event_payload)]));
  const composed = await sha256hex(att.app_compose || "");
  out.push({ ok: logged["compose-hash"] === composed && composed === att.compose_hash,
             text: `compose hash ${composed.slice(0, 16)}… is in the quote and matches app_compose` });
  let manifest = {};
  try { manifest = JSON.parse(att.app_compose); } catch { /* checked above */ }
  out.push({ ok: manifest.docker_compose_file === COMPOSE[net.compose],
             text: `the deployed compose file is ${net.compose} from this repo (it pins the engine image)` });
  return out;
}

function grade(checks) {
  if (checks.some((c) => c.ok === false)) return false;
  return checks.some((c) => c.ok === null) ? null : true;
}

// queryText: the exact /query reply, if this browser asked the question (adds the answer check).
export async function verifyReceipt(net, requestId, queryText = null) {
  const { text } = await engineRaw(net, `/receipt/${requestId}`);
  const raw = rawParse(text).receipt;
  const r = JSON.parse(text).receipt;
  const h = await sha256hex(canonical(Object.fromEntries(Object.entries(raw).filter(([k]) => k !== "sig"))));
  const { data: att } = await engineRaw(net, "/attestation");
  const enclave = net.contract ? await view(net, "get_enclave") : null;

  const signed = [
    { ok: JSON.parse(text).receipt_hash === h, text: `receipt hash ${h.slice(0, 16)}…, recomputed here` },
    { ok: await signatureOk(raw).catch(() => false), text: `ed25519 signature valid for ${r.signer_pubkey.slice(0, 20)}…` },
    { ok: att.signer_pubkey === r.signer_pubkey, text: "the signer is this enclave's attested key" },
  ];
  if (queryText) {
    const answer = rawParse(queryText).answer;
    const ah = answer == null ? null : await sha256hex(canonical(answer));
    signed.push({ ok: ah === r.answer_hash, text: answer == null ? "a refusal: no answer to hash" : "the answer you got is the one the enclave signed" });
  }

  const code = await enclaveChecks(att, net);
  if (enclave) {
    code.push({ ok: enclave.compose_hash === att.compose_hash && att.compose_hash === r.compose_hash,
                text: `the compose hash is the one registered on ${net.contract}, and the receipt names it` });
  }

  let chain = [];
  if (net.contract) {
    const req = await view(net, "get_request", { id: String(requestId) });
    chain = [
      { ok: !!req && req.receipt_hash === h && req.status === r.status,
        text: req?.receipt_hash ? `request #${requestId} settled on-chain as ${req.status}, with this receipt hash`
                                : `request #${requestId} isn't settled on-chain yet` },
      { ok: !!enclave && enclave.signer_pubkey === r.signer_pubkey, text: "the signer is the enclave registered on the contract" },
    ];
    if (r.status === "refused") {
      chain.push({ ok: req?.refusal === r.refusal?.rule && req?.epsilon_charged === 0,
                   text: `refused (${r.refusal?.rule}) at no ε, and the contract refunded the fee` });
    } else {
      chain.push({ ok: req?.epsilon_charged === Math.round(r.epsilon_charged * 1e6),
                   text: `ε ${r.epsilon_charged} charged to the public budget` });
    }
  }

  const groups = [
    { title: "Signed by the enclave", checks: signed },
    { title: "Running the registered code", checks: code },
  ];
  if (net.contract) groups.push({ title: "Recorded on NEAR", checks: chain });
  return { receipt: r, hash: h, groups: groups.map((g) => ({ ...g, ok: grade(g.checks) })) };
}