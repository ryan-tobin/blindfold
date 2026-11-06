// The dataset card (with the privacy meter) and the feed of recent questions.
import { Eps, Meter } from "./charts.jsx";
import { Icon } from "./icons.jsx";
import { usdc } from "./api.js";

const MODES = { tdx: "Intel TDX enclave", simulated: "Simulated enclave", dev: "Dev mode, no enclave" };
const STATUS = { answered: "Answered", refused: "Refused", expired: "Expired", pending: "Pending" };

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
        <dd>{dataset ? (Number(dataset.price) ? `${usdc(dataset.price)} a question, in USDC` : "Free") : "…"}</dd>
        <dt>Owner</dt>
        <dd>{dataset?.owner ?? "…"}</dd>
        <dt>Engine</dt>
        <dd>{info ? `${MODES[info.mode] ?? info.mode}, online` : "Offline (start the VM)"}</dd>
        {net.contract && (
          <>
            <dt>Schema</dt>
            <dd className={schemaOk == null ? "" : schemaOk ? "ok" : "bad"}>
              {schemaOk == null ? "…" : schemaOk ? (
                <><Icon name="pass" />Matches the hash on-chain</>
              ) : (
                <><Icon name="fail" />Differs from the hash on-chain</>
              )}
            </dd>
          </>
        )}
      </dl>
      <details className="schema">
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
  if (r.status === "answered") return <><Eps v={r.epsilon_charged / 1e6} /> spent</>;
  if (r.status === "refused") return <><code>{r.refusal}</code> · refunded</>;
  if (r.status === "expired") return "Refunded";
  return <><Eps v={r.epsilon / 1e6} /> held</>;
}

export function Feed({ rows, local, onVerify }) {
  return (
    <section className="panel feed">
      <div className="panel-head">
        <h2>Recent questions</h2>
        <span className="meta">{local ? "This session" : "Read from the contract"}</span>
      </div>
      {rows.length === 0 && <p className="note">No questions yet</p>}
      <ol>
        {rows.map((r) => (
          <li key={r.id} className={`feed-row ${r.status}`}>
            <span className="feed-id">#{r.id}</span>
            <span className="feed-status"><i className="dot" />{STATUS[r.status] ?? r.status}</span>
            <span className="feed-what">{what(r)}</span>
            <span className="feed-buyer" title={r.buyer}>{r.buyer}</span>
            <span className="feed-time">{ago(r.created_at)}</span>
            {r.status === "answered" || r.status === "refused" ? (
              <button className="small" onClick={() => onVerify({ id: Number(r.id) })}>Verify</button>
            ) : <span />}
          </li>
        ))}
      </ol>
    </section>
  );
}
