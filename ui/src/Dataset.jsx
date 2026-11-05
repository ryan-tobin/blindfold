// The dataset card (with the privacy meter) and the feed of recent questions.
import { Meter } from "./charts.jsx";
import { usdc } from "./api.js";

const MODES = { tdx: "Intel TDX enclave", simulated: "simulated enclave", dev: "dev mode, no enclave" };

export function DatasetCard({ schema, dataset, info, schemaOk, net }) {
  return (
    <section className="panel dataset">
      <h2>{schema.title}</h2>
      <p className="note">{schema.source}</p>
      {dataset && (
        <Meter total={dataset.epsilon_total / 1e6} spent={dataset.epsilon_spent / 1e6}
               pending={dataset.epsilon_pending / 1e6} />
      )}
      <dl className="facts">
        <dt>Price</dt>
        <dd>{dataset ? (Number(dataset.price) ? `${usdc(dataset.price)} a question, in USDC` : "free") : "…"}</dd>
        <dt>Owner</dt>
        <dd>{dataset?.owner ?? "…"}</dd>
        <dt>Engine</dt>
        <dd>{info ? `${MODES[info.mode] ?? info.mode}, online` : "offline (start the VM)"}</dd>
        {net.contract && (
          <>
            <dt>Schema</dt>
            <dd>{schemaOk == null ? "…" : schemaOk ? "matches the hash on-chain ✓" : "differs from the hash on-chain ✗"}</dd>
          </>
        )}
      </dl>
      <details>
        <summary>What a question can use</summary>
        <ul className="tables">
          {Object.entries(schema.tables).map(([name, t]) => (
            <li key={name}>
              <strong>{name}</strong> {Object.keys(t.columns).filter((c) => c !== "patient_id").join(", ")}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function ago(ns) {
  const s = Math.max(0, (Date.now() - Number(ns) / 1e6) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function what(r) {
  if (r.status === "answered") return `ε ${r.epsilon_charged / 1e6} spent`;
  if (r.status === "refused") return `${r.refusal} · refunded`;
  if (r.status === "expired") return "expired · refunded";
  return `ε ${r.epsilon / 1e6} held`;
}

export function Feed({ rows, local, onVerify }) {
  return (
    <section className="panel feed">
      <h2>Recent questions <span className="note">{local ? "this session" : "read from the contract"}</span></h2>
      {rows.length === 0 && <p className="note">No questions yet.</p>}
      <ol>
        {rows.map((r) => (
          <li key={r.id} className={`feed-row ${r.status}`}>
            <span className="feed-id">#{r.id}</span>
            <span className="feed-status">{r.status}</span>
            <span>{what(r)}</span>
            <span className="feed-buyer" title={r.buyer}>{r.buyer}</span>
            <span className="feed-time">{ago(r.created_at)}</span>
            {r.status === "answered" || r.status === "refused" ? (
              <button className="feed-verify" onClick={() => onVerify({ id: Number(r.id) })}>verify</button>
            ) : <span />}
          </li>
        ))}
      </ol>
    </section>
  );
}