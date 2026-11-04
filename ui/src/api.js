// Talking to the engine (HTTPS) and to the NEAR contract (JSON-RPC view calls).
import { DATASET } from "./config.js";

export async function engine(net, path, body) {
  const init = body === undefined ? {} : {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(net.engine + path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    throw new Error(data.detail ? detail : `engine: HTTP ${res.status}`);
  }
  return data;
}

export async function view(net, method, args = {}) {
  const res = await fetch(net.rpc, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: "ui", method: "query",
      params: {
        request_type: "call_function", finality: "final", account_id: net.contract,
        method_name: method, args_base64: btoa(JSON.stringify(args)),
      },
    }),
  });
  const body = await res.json();
  const error = body.error?.data || body.error?.message || body.result?.error;
  if (error) throw new Error(`${method}: ${error}`);
  return JSON.parse(new TextDecoder().decode(new Uint8Array(body.result.result)));
}

// Epsilon and money as the contract stores them: millionths, and USDC's 6 decimals.
export const toMillionths = (eps) => Math.round(eps * 1e6);
export const usdc = (amount) => `$${(Number(amount) / 1e6).toFixed(2)}`;

// Why request_query would fail, or null. Mirrors the contract (and agent/buyer.py).
export function precheck(dataset, balance, epsilon) {
  const price = Number(dataset.price);
  const left = dataset.epsilon_total - dataset.epsilon_spent - dataset.epsilon_pending;
  if (!dataset.active) return "the dataset is closed";
  if (toMillionths(epsilon) > left) return `only ${left / 1e6} ε left in the budget`;
  if (price === 0) return null;
  if (Number(balance.amount) < price) return `your balance is ${usdc(balance.amount)}: deposit USDC first (agent/wallet.py deposit)`;
  if (Number(balance.per_query_max) < price) return `your per-question limit is ${usdc(balance.per_query_max)} (agent/wallet.py limits)`;
  if (Number(balance.spent_today) + price > Number(balance.daily_max)) return "your daily limit is reached";
  return null;
}

// The wallet signs request_query. The request id is the call's return value,
// or, if the wallet doesn't hand back the outcome, the newest matching request.
export async function payForPlan(connector, net, account, plan) {
  const wallet = await connector.wallet();
  const args = { dataset_id: DATASET, spec_hash: plan.spec_hash, epsilon: toMillionths(plan.epsilon) };
  const outcome = await wallet.signAndSendTransaction({
    signerId: account,
    receiverId: net.contract,
    actions: [{
      type: "FunctionCall",
      params: { methodName: "request_query", args, gas: "30000000000000", deposit: "0" },
    }],
  });
  const tx = outcome?.transaction?.hash || outcome?.transaction_outcome?.id || null;
  const value = outcome?.status?.SuccessValue;
  if (value) return { id: Number(JSON.parse(atob(value))), tx };
  const recent = await view(net, "list_recent", { dataset_id: DATASET, limit: 20 });
  const mine = recent.find((r) => r.buyer === account && r.spec_hash === plan.spec_hash && r.status === "pending");
  if (!mine) throw new Error("the payment went through, but its request isn't on-chain yet: wait a moment and look in the feed");
  return { id: Number(mine.id), tx };
}