// The data owner's side: the listing as the contract has it, what it earned,
// and the owner's controls (price, pause, withdraw). A new dataset can be listed here too.
import { useEffect, useState } from "react";
import schemaText from "../../datasets/riverside/schema.json?raw";
import { callContract, toMillionths, usdc, view } from "./api.js";
import { sha256hex } from "./canon.js";

export function Owner({ net, connector, account }) {
  const [datasets, setDatasets] = useState(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!net.contract) return;
    view(net, "list_datasets").then(setDatasets, (e) => setError(e.message));
  }, [net, tick]);

  if (!net.contract) {
    return <section className="panel"><h2>Owner</h2><p className="note">Local mode has no contract, so there is no listing. Pick testnet or mainnet.</p></section>;
  }
  return (
    <>
      {error && <p className="error">{error}</p>}
      {datasets?.map((d) => (
        <Listing key={d.id} d={d} net={net} connector={connector} account={account} onChange={() => setTick((t) => t + 1)} />
      ))}
      <NewListing net={net} connector={connector} account={account} onChange={() => setTick((t) => t + 1)} />
    </>
  );
}

function Listing({ d, net, connector, account, onChange }) {
  const [recent, setRecent] = useState([]);
  const [earned, setEarned] = useState(null);
  const [price, setPrice] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const mine = account && account === d.owner;

  useEffect(() => {
    view(net, "list_recent", { dataset_id: d.id, limit: 100 }).then(setRecent, () => setRecent([]));
    view(net, "get_balance", { account_id: d.owner }).then((b) => setEarned(b.amount), () => setEarned(null));
  }, [d, net]);

  const count = (s) => recent.filter((r) => r.status === s).length;
  const paid = recent.filter((r) => r.status === "answered").reduce((n, r) => n + Number(r.escrow), 0);

  async function call(label, method, args, deposit, gas) {
    setBusy(label);
    setError("");
    try {
      await callContract(connector, net, account, method, args, deposit, gas);
      onChange();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="panel owner">
      <h2>{d.id} <span className={`badge ${d.active ? "open" : "closed"}`}>{d.active ? "open" : "paused"}</span></h2>
      <dl className="facts">
        <dt>Owner</dt><dd>{d.owner}{mine && " (you)"}</dd>
        <dt>Price</dt><dd>{Number(d.price) ? `${usdc(d.price)} a question` : "free"}</dd>
        <dt>Budget</dt><dd>{(d.epsilon_total - d.epsilon_spent) / 1e6} of {d.epsilon_total / 1e6} ε left, {d.epsilon_pending / 1e6} held</dd>
        <dt>Questions</dt><dd>{count("answered")} answered, {count("refused")} refused, {count("expired")} expired, {count("pending")} pending (last 100)</dd>
        <dt>Earned</dt><dd>{usdc(paid)} from answers in the last 100 · {earned == null ? "…" : usdc(earned)} ready to withdraw</dd>
        <dt>Schema</dt><dd><code>{d.schema_hash}</code></dd>
        <dt>Bundle</dt><dd><code>{d.bundle_hash || "none yet"}</code></dd>
      </dl>
      {mine ? (
        <div className="owner-actions">
          <form className="row" onSubmit={(e) => { e.preventDefault(); call("price", "update_dataset", { id: d.id, price: String(toMillionths(Number(price))) }); }}>
            <label>New price $<input value={price} size={6} inputMode="decimal" onChange={(e) => setPrice(e.target.value)} /></label>
            <button disabled={!!busy || !(Number(price) >= 0) || price === ""}>Set price</button>
          </form>
          <div className="row">
            <button disabled={!!busy} onClick={() => call("active", "update_dataset", { id: d.id, active: !d.active })}>
              {d.active ? "Pause new questions" : "Reopen"}
            </button>
            <button disabled={!!busy || !Number(earned)} onClick={() => call("withdraw", "withdraw", { amount: String(earned) }, "1", "50000000000000")}>
              Withdraw {earned ? usdc(earned) : ""}
            </button>
            {busy && <span className="step">Waiting for your wallet…</span>}
          </div>
          <p className="note">The privacy budget can't be raised: once it's spent, the dataset stays closed.</p>
        </div>
      ) : (
        <p className="note">{account ? "You aren't this dataset's owner." : "Connect the owner's wallet to change the price, pause, or withdraw."}</p>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}

function NewListing({ net, connector, account, onChange }) {
  const [form, setForm] = useState({ id: "", schema_hash: "", bundle_hash: "", price: "0.25", epsilon: "10" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value.trim() });

  useEffect(() => { sha256hex(schemaText).then((h) => setForm((f) => ({ ...f, schema_hash: f.schema_hash || h }))); }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await callContract(connector, net, account, "register_dataset", {
        id: form.id, schema_hash: form.schema_hash, bundle_hash: form.bundle_hash,
        price: String(toMillionths(Number(form.price))), epsilon_total: toMillionths(Number(form.epsilon)),
      });
      onChange();
    } catch (err) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="panel">
      <summary><strong>List a new dataset</strong></summary>
      <p className="note">
        Lists a dataset under your account. The schema hash defaults to this repo's Riverside schema.json.
        Leave the bundle hash empty for now: <code>make bundle</code> encrypts your tables to the enclave's key
        and gives you the hash to set.
      </p>
      <form className="new-listing" onSubmit={submit}>
        <label>Id <input value={form.id} onChange={set("id")} placeholder="riverside-2" /></label>
        <label>Schema hash <input value={form.schema_hash} onChange={set("schema_hash")} /></label>
        <label>Bundle hash <input value={form.bundle_hash} onChange={set("bundle_hash")} placeholder="optional" /></label>
        <label>Price in USDC <input value={form.price} onChange={set("price")} inputMode="decimal" /></label>
        <label>Privacy budget ε <input value={form.epsilon} onChange={set("epsilon")} inputMode="decimal" /></label>
        <button className="primary" disabled={busy || !account || !/^[a-z0-9_-]{1,32}$/.test(form.id)}>
          {account ? "List it" : "Connect a wallet first"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </details>
  );
}
