// The verify screen: anyone can check a receipt here, with no account.
import { useEffect, useState } from "react";
import { verifyReceipt } from "./verify.js";

const MARK = { true: "✓", null: "!", false: "✗" };
const CLASS = { true: "pass", null: "warn", false: "fail" };

export function Verify({ net, target }) {
  const [id, setId] = useState(target?.id ?? "");
  const [out, setOut] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function check(requestId, queryText) {
    setBusy(true);
    setError("");
    setOut(null);
    try {
      setOut(await verifyReceipt(net, requestId, queryText));
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (target?.id != null) {
      setId(String(target.id));
      check(target.id, target.queryText);
    }
  }, [target]);

  return (
    <section className="panel verify">
      <h2>Verify a receipt</h2>
      <p className="note">
        Your browser recomputes the receipt's hash, checks the enclave's signature, replays the enclave's
        measurements, and reads the record on NEAR. Nothing here trusts the engine's word.
      </p>
      <form className="row" onSubmit={(e) => { e.preventDefault(); check(Number(id)); }}>
        <label htmlFor="rid">Request</label>
        <input id="rid" value={id} inputMode="numeric" size={6} onChange={(e) => setId(e.target.value.replace(/\D/g, ""))} />
        <button className="primary" disabled={busy || id === ""}>{busy ? "Checking…" : "Verify"}</button>
      </form>
      {error && <p className="error">{error}</p>}
      {out && (
        <>
          <p className="note">
            Request #{out.receipt.request_id}, {out.receipt.status}, receipt <code>{out.hash.slice(0, 16)}…</code>
          </p>
          <div className="checks">
            {out.groups.map((g) => (
              <div key={g.title} className={`check-group ${CLASS[g.ok]}`}>
                <h3><span className="mark">{MARK[g.ok]}</span>{g.title}</h3>
                <ul>
                  {g.checks.map((c) => (
                    <li key={c.text} className={CLASS[c.ok]}><span className="mark">{MARK[c.ok]}</span>{c.text}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
          <p className="note">
            For Intel's signature on the quote and NEAR AI's signature on the plan, run{" "}
            <code>make verify REQUEST={out.receipt.request_id}{net.contract ? ` CONTRACT_ID=${net.contract}` : ""}</code>.
          </p>
        </>
      )}
    </section>
  );
}
