// Ask: plain English in, the plan shown before paying, then pay, answer and receipt.
import { useState } from "react";
import questions from "../../datasets/riverside/planner_questions.json";
import { AnswerChart } from "./charts.jsx";
import { engine, engineRaw, payForPlan, precheck, usdc } from "./api.js";
import { canonical, sha256hex } from "./canon.js";

const DEMO = questions.groups.filter((g) => g.expected !== "refused").map((g) => g.question);
const ATTACK = questions.groups.find((g) => g.id === "attack_one_patient");

// Attack mode's plan, made ahead of time: the spec the planner writes for the attack
// question, with the planner's default ε for a histogram. The live planner can take
// 90 s on it. A spec that breaks the rules is hashed raw, as the engine does.
async function attackPlan(dataset) {
  const spec = { ...ATTACK.stub_spec, epsilon: 0.5 };
  return {
    premade: true,
    spec,
    spec_hash: await sha256hex(canonical(spec)),
    epsilon: spec.epsilon,
    summary: "Histogram of condition_group · sex female · age_band 65-79 · zip3 606 · admitted 2026-03-03 · ε 0.5",
    will_refuse: { rule: "too_fine", reason: "filter on 'admissions.admit_date': date ranges must cover at least 30 days" },
    price: dataset && Number(dataset.price) ? { amount: dataset.price } : null,
  };
}

export function Ask({ net, connector, account, dataset, balance, onAnswered, onVerify }) {
  const [question, setQuestion] = useState(DEMO[0]);
  const [attack, setAttack] = useState(false);
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

  async function toggleAttack(on) {
    setAttack(on);
    setResult(null);
    setError("");
    setQuestion(on ? ATTACK.question : DEMO[0]);
    setPlan(on ? await attackPlan(dataset) : null);
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
    const signed = plan.plan_id ? { plan_id: plan.plan_id } : {}; // puts the model's signature in the receipt
    let body = { buyer: "browser.local", spec: plan.spec, ...signed };
    let paid = null;
    if (!local) {
      paid = await payForPlan(connector, net, account, plan);
      setStep(`Paid: request #${paid.id}. The enclave is answering and settling on-chain…`);
      body = { request_id: paid.id, spec: plan.spec, ...signed };
    }
    const { data: out, text } = await engineRaw(net, "/query", body);
    setResult({ ...out, pay_tx: paid?.tx, raw: text });
    onAnswered(out);
  });

  let blocker = null;
  if (plan?.will_refuse && !(attack && plan.spec)) blocker = `The engine will refuse this (${plan.will_refuse.rule}): ${plan.will_refuse.reason}`;
  else if (plan && !local && !account) blocker = "Connect a wallet to pay.";
  else if (plan && !local && dataset && balance) blocker = precheck(dataset, balance, plan.epsilon);

  return (
    <section className="panel ask">
      <div className="ask-head">
        <h2>{attack ? "Attack mode: try to find one patient" : "Ask a question"}</h2>
        <label className="toggle">
          <input type="checkbox" checked={attack} disabled={busy} onChange={(e) => toggleAttack(e.target.checked)} />
          Attack mode
        </label>
      </div>
      <div className="chips" hidden={attack}>
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
            {plan.premade ? "planned ahead of time, so the demo doesn't wait on the model"
              : plan.model ? `planned by ${plan.model} in an attested TEE, signature checked` : "planned by the offline stub"}
          </p>
          {attack && plan.will_refuse && (
            <p className="refused">
              The engine will refuse this ({plan.will_refuse.rule}): {plan.will_refuse.reason}.
              {plan.spec && (local ? " Send it anyway to get a signed refusal."
                : " Pay anyway: the enclave signs the refusal, settles it on-chain, and the contract refunds you.")}
            </p>
          )}
        </div>
      )}
      {plan && !result && (
        <div className="row">
          <button className="primary" onClick={payAndAsk} disabled={busy || !!blocker}>
            {attack ? (local ? "Send the attack" : `Pay ${plan.price ? usdc(plan.price.amount) : ""} and try it`)
              : local ? "Ask" : `Pay ${plan.price ? usdc(plan.price.amount) : ""} and ask`}
          </button>
          {blocker && <span className="note">{blocker}</span>}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {result && <Answer result={result} net={net} onVerify={onVerify} />}
    </section>
  );
}

function Answer({ result, net, onVerify }) {
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
        <button onClick={() => onVerify({ id: result.request_id, queryText: result.raw })}>Verify this receipt</button>
      </p>
    </div>
  );
}