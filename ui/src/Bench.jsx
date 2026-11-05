// The accuracy benchmark from `make bench` (docs/bench.json): how close noisy answers land to the truth.
import { useState } from "react";
import bench from "../../docs/bench.json";

const pct = (x) => `${(100 * x).toFixed(x < 0.1 ? 1 : 0)}%`;

export function Bench() {
  const results = bench.results;
  const [pick, setPick] = useState(results.find((r) => r.epsilon === 0.2) ?? results[0]);
  const measures = Object.keys(results[0].by_measure);
  return (
    <section className="panel bench">
      <h2>Accuracy over {bench.questions} benchmark questions</h2>
      <p className="note">
        Each question's true answer is computed on the raw tables, then answered {bench.trials} times with the
        enclave's own noise. Error is |noisy − true| / true over every cell a buyer would see. Reproduce it
        with <code>make bench</code>.
      </p>
      <div className="row eps-pick" role="group" aria-label="Privacy setting">
        {results.map((r) => (
          <button key={r.epsilon} className={r === pick ? "primary" : ""} onClick={() => setPick(r)}>ε {r.epsilon}</button>
        ))}
      </div>
      <div className="tiles">
        <div className="tile"><strong>{pct(pick.median_error)}</strong><span>median error at ε {pick.epsilon}</span></div>
        <div className="tile"><strong>{pct(pick.questions_within_10pct)}</strong><span>of questions within 10%</span></div>
        <div className="tile"><strong>{pct(pick.inside_error_bar)}</strong><span>of answers inside their 95% error bar</span></div>
      </div>
      <ErrorBars results={results} pick={pick} onPick={setPick} />
      <table className="bench-table">
        <thead>
          <tr><th>ε</th><th>median</th><th>90% under</th>{measures.map((m) => <th key={m}>{m}</th>)}<th>suppressed</th></tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.epsilon} className={r === pick ? "picked" : ""}>
              <td>{r.epsilon}</td><td>{pct(r.median_error)}</td><td>{pct(r.p90_error)}</td>
              {measures.map((m) => <td key={m}>{pct(r.by_measure[m])}</td>)}
              <td>{pct(r.suppressed)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">
        Small groups carry the most error, which is the point of the noise; cells under 20 people are shown as
        "&lt;20" and not scored. Sums are the noisiest measure because one patient can move a total by up to 150 days.
      </p>
    </section>
  );
}

// One bar per ε: the median error. Lower is better; more ε buys accuracy and spends more budget.
function ErrorBars({ results, pick, onPick }) {
  const W = 560, H = 220, L = 48, B = 34, T = 16;
  const top = Math.ceil((Math.max(...results.map((r) => r.median_error)) * 100) / 5) * 5 / 100 || 0.05;
  const y = (v) => T + (1 - v / top) * (H - T - B);
  const slot = (W - L) / results.length;
  const ticks = [0, top / 2, top];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart bench-chart" role="img"
         aria-label="Median relative error at each epsilon">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={L} x2={W} y1={y(t)} y2={y(t)} className="grid" />
          <text x={L - 8} y={y(t) + 4} className="axis" textAnchor="end">{pct(t)}</text>
        </g>
      ))}
      {results.map((r, i) => {
        const x = L + i * slot + slot * 0.25, w = slot * 0.5;
        return (
          <g key={r.epsilon} className={r === pick ? "bar picked" : "bar"} onClick={() => onPick(r)}>
            <title>{`ε ${r.epsilon}: median error ${pct(r.median_error)}, ${pct(r.inside_error_bar)} inside the error bar`}</title>
            <rect x={L + i * slot} y={T} width={slot} height={H - T - B} className="hit" />
            <path d={`M${x},${y(0)} V${y(r.median_error) + 4} q0,-4 4,-4 h${w - 8} q4,0 4,4 V${y(0)} Z`} className="chart-bar" />
            <text x={x + w / 2} y={y(r.median_error) - 6} className="chart-value" textAnchor="middle">{pct(r.median_error)}</text>
            <text x={x + w / 2} y={H - 12} className="axis" textAnchor="middle">ε {r.epsilon}</text>
          </g>
        );
      })}
    </svg>
  );
}
