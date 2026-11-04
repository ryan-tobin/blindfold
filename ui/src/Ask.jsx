// Ask: plain English in, the plan shown before paying, then pay, answer and receipt.
import { useState } from "react";
import questions from "../../datasets/riverside/planner_questions.json";
import { AnswerChart } from "./charts.jsx";
import { engine, payForPlan, precheck, usdc } from "./api.js";

const DEMO = questions.groups.filter((g) => g.expected !== "refused").map((g) => g.question);

export function Ask({ net, connector, account, dataset, balance, onAnswered }) {
  const [question, setQuestion] = useState(DEMO[0]);
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);
  const [step, setStep] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const local = !net.contract;

  function pick(q) {
    setQuestion(q);
    setPlan(null);
    setResult(null);
  }

  async function run(label, fn) {
    setBusy(true);
    setError("");
    setStep(label);
    try {
      await fn();
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
      setStep("");
    }
  }

  const planIt = () => run("Planning inside the enclave…", async () => {
    setPlan(null);
    setResult(null);
    setPlan(await engine(net, "/plan", { question }));
  });

  const payAndAsk = () => run(local ? "Asking…" : "Waiting for your wallet…", async () => {
    let body = { buyer: "browser.local", spec: plan.spec };
    let paid = null;
    if (!local) {
      paid = await payForPlan(connector, net, account, plan);
      setStep(`Paid: request #${paid.id}. The enclave is answering and settling on-chain…`);
      body = { request_id: paid.id, spec: plan.spec };
    }
    const out = await engine(net, "/query", body);
    setResult({ ...out, pay_tx: paid?.tx });
    onAnswered(out);
  });

  let blocker = null;
  if (plan?.will_refuse) blocker = `The engine will refuse this (${plan.will_refuse.rule}): ${plan.will_refuse.reason}`;
  else if (plan && !local && !account) blocker = "Connect a wallet to pay.";
  else if (plan && !local && dataset && balance) blocker = precheck(dataset, balance, plan.epsilon);

  return (
    <section className="panel ask">
      <h2>Ask a question</h2>
      <div className="chips">
        {DEMO.map((q) => (
          <button key={q} className="chip" onClick={() => pick(q)} disabled={busy}>{q}</button>
        ))}
      </div>
      <textarea value={question} rows={3} maxLength={500}
                onChange={(e) => { setQuestion(e.target.value); setPlan(null); setResult(null); }} />
      <div className="row">
        <button onClick={planIt} disabled={busy || question.trim().length < 3}>Plan it (free)</button>
        {step && <span className="step">{step}</span>}
      </div>

      {plan && (
        <div className="plan">
          <p className="summary">{plan.summary || "No QuerySpec"}</p>
          <p className="note">
            ε {plan.epsilon ?? "–"} · {plan.price ? usdc(plan.price.amount) : "free (local mode)"} ·{" "}
            {plan.model ? `planned by ${plan.model} in an attested TEE, signature checked` : "planned by the offline stub"}
          </p>
        </div>
      )}
      {plan && !result && (
        <div className="row">
          <button className="primary" onClick={payAndAsk} disabled={busy || !!blocker}>
            {local ? "Ask" : `Pay ${plan.price ? usdc(plan.price.amount) : ""} and ask`}
          </button>
          {blocker && <span className="note">{blocker}</span>}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {result && <Answer result={result} net={net} />}
    </section>
  );
}

function Answer({ result, net }) {
  const r = result.receipt;
  const link = (tx, text) => net.explorer && tx && (
    <a href={`${net.explorer}/txns/${tx}`} target="_blank" rel="noreferrer">{text}</a>
  );
  return (
    <div className="answer">
      {result.status === "answered" ? (
        <AnswerChart answer={result.answer} />
      ) : (
        <p className="refused">
          Refused ({r.refusal.rule}): {r.refusal.reason}.{net.contract && " The fee was refunded on-chain."}
        </p>
      )}
      <p className="note">
        {result.answer && `${Math.round(result.answer.level * 100)}% error bars for the noise · `}
        ε {r.epsilon_charged} spent, {r.epsilon_remaining} left
        {result.replay_of != null && ` · the same answer as #${result.replay_of}, so no ε spent`}
      </p>
      <p className="receipt">
        <span className="receipt-chip" title={result.receipt_hash}>
          receipt {result.receipt_hash.slice(0, 12)}… signed in the enclave
        </span>
        {link(result.pay_tx, "payment")}
        {link(result.settle_tx, "settlement")}
      </p>
    </div>
  );
}