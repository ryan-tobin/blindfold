// Blindfold's browser UI: dataset page, privacy meter, ask flow and query feed.
import { useCallback, useEffect, useState } from "react";
import schemaText from "../../datasets/riverside/schema.json?raw";
import { DATASET, NETWORKS, currentNetwork } from "./config.js";
import { engine, toMillionths, usdc, view } from "./api.js";
import { connectedAccount, makeConnector } from "./wallet.js";
import { DatasetCard, Feed } from "./Dataset.jsx";
import { Ask } from "./Ask.jsx";
import { Verify } from "./Verify.jsx";
import { Owner } from "./Owner.jsx";
import { Bench } from "./Bench.jsx";

const NET_NAME = currentNetwork();
const NET = NETWORKS[NET_NAME];
const connector = NET.contract ? makeConnector(NET_NAME, NET) : null;
const schema = JSON.parse(schemaText);
const TABS = { ask: "Ask", verify: "Verify", owner: "Owner", bench: "Accuracy"};
const START_TAB = new URLSearchParams(location.search).get("tab");

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export default function App() {
  const [account, setAccount] = useState(null);
  const [dataset, setDataset] = useState(null);
  const [balance, setBalance] = useState(null);
  const [feed, setFeed] = useState([]);
  const [info, setInfo] = useState(null);
  const [schemaOk, setSchemaOk] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState(TABS[START_TAB] ? START_TAB : "ask");
  const [verifyTarget, setVerifyTarget] = useState(null);

  function go(name) {
    setTab(name);
    const q = new URLSearchParams(location.search);
    q.set("tab", name);
    history.replaceState(null, "", `?${q}`);
  }

  function verify(target) {
    setVerifyTarget({ ...target });
    go("verify");
  }

  const refresh = useCallback(async () => {
    engine(NET, "/info").then(setInfo, () => setInfo(null));
    try {
      if (NET.contract) {
        const [d, recent] = await Promise.all([
          view(NET, "get_dataset", { id: DATASET }),
          view(NET, "list_recent", { dataset_id: DATASET, limit: 15 }),
        ]);
        setDataset(d);
        setFeed(recent);
        setBalance(account ? await view(NET, "get_balance", { account_id: account }) : null);
      } else {
        const b = await engine(NET, "/budget");
        setDataset({ owner: "local mode", price: "0", active: !b.closed, epsilon_pending: 0,
                     epsilon_total: toMillionths(b.epsilon_total), epsilon_spent: toMillionths(b.epsilon_spent) });
      }
      setError("");
    } catch (e) {
      setError(`Couldn't read the ${NET.contract ? "contract" : "engine"}: ${e.message}`);
    }
  }, [account]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 8000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!connector) return;
    connectedAccount(connector).then(setAccount);
    connector.on("wallet:signIn", (t) => setAccount(t.accounts[0]?.accountId ?? null));
    connector.on("wallet:signOut", () => setAccount(null));
  }, []);

  useEffect(() => {
    if (dataset?.schema_hash) sha256(schemaText).then((h) => setSchemaOk(h === dataset.schema_hash));
  }, [dataset?.schema_hash]);

  function onAnswered(out) {
    if (!NET.contract) {
      const row = { id: out.request_id, buyer: "you", status: out.status, refusal: out.refusal?.rule,
                    epsilon_charged: toMillionths(out.receipt.epsilon_charged), created_at: Date.now() * 1e6 };
      setFeed((rows) => [row, ...rows]);
    }
    refresh();
  }

  return (
    <>
      <header>
        <div className="brand">
          <strong>Blindfold</strong>
          <span>answers, not data</span>
        </div>
        <div className="row">
          <select value={NET_NAME} onChange={(e) => { location.search = `?net=${e.target.value}&tab=${tab}`; }}>
            {Object.keys(NETWORKS).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          {connector && (account ? (
            <button onClick={() => connector.disconnect()} title="Sign out">{account}</button>
          ) : (
            <button className="primary" onClick={() => connector.connect().catch((e) => setError(e.message))}>
              Connect wallet
            </button>
          ))}
        </div>
      </header>
      {balance && (
        <p className="balance">
          Your balance {usdc(balance.amount)} · up to {usdc(balance.per_query_max)} a question,{" "}
          {usdc(balance.daily_max)} a day ({usdc(balance.spent_today)} spent today)
        </p>
      )}
      <nav className="tabs">
        {Object.entries(TABS).map(([name, label]) => (
          <button key={name} className={tab === name ? "active" : ""} onClick={() => go(name)}>{label}</button>
        ))}
      </nav>
      <main>
        {error && <p className="error">{error}</p>}
        <div className="grid" hidden={tab !== "ask"}>
          <DatasetCard schema={schema} dataset={dataset} info={info} schemaOk={schemaOk} net={NET} />
          <Ask net={NET} connector={connector} account={account} dataset={dataset} balance={balance}
               onAnswered={onAnswered} onVerify={verify} />
        </div>
        {tab === "verify" && <Verify net={NET} target={verifyTarget} />}
        {tab === "owner" && <Owner net={NET} connector={connector} account={account} />}
        {tab === "bench" && <Bench />}
        {(tab === "ask" || tab === "verify") && <Feed rows={feed} local={!NET.contract} onVerify={verify} />}
      </main>
      <footer>
        {NET.contract ? (
          <>Contract <a href={`${NET.explorer}/address/${NET.contract}`} target="_blank" rel="noreferrer">
            {NET.contract}</a> · engine {NET.engine.replace("https://", "").slice(0, 16)}…</>
        ) : (
          <>Local mode: engine at {NET.engine}, nothing is paid</>
        )}
      </footer>
    </>
  );
}