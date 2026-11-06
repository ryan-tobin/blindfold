// The accuracy benchmark from `make bench` (docs/bench.json): how close noisy answers land to the truth.
import { useState } from "react";
import bench from "../../docs/bench.json";
import { Eps, useWidth } from "./charts.jsx";

const pct = (x) => `${(100 * x).toFixed(x < 0.1 ? 1 : 0)}%`;
const cap = (s) => s[0].toUpperCase() + s.slice(1);

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
      <div className="segmented" role="group" aria-label="Privacy setting">
        {results.map((r) => (
          <button key={r.epsilon} className={r === pick ? "active" : ""} aria-pressed={r === pick}
                  onClick={() => setPick(r)}><Eps v={r.epsilon} /></button>
        ))}
      </div>
      <div className="tiles">
        <div className="tile"><span>Median error at <Eps v={pick.epsilon} /></span><strong>{pct(pick.median_error)}</strong></div>
        <div className="tile"><span>Questions within 10%</span><strong>{pct(pick.questions_within_10pct)}</strong></div>
        <div className="tile"><span>Answers inside their 95% error bar</span><strong>{pct(pick.inside_error_bar)}</strong></div>
      </div>
      <ErrorBars results={results} pick={pick} onPick={setPick} />
      <div className="table-wrap">
        <table className="bench-table">
          <thead>
            <tr><th>ε</th><th>Median</th><th>90% under</th>{measures.map((m) => <th key={m}>{cap(m)}</th>)}<th>Suppressed</th></tr>
          </thead>
          <tbody>
            {results.map((r) => (
              <tr key={r.epsilon} className={r === pick ? "picked" : ""} onClick={() => setPick(r)}>
                <td>{r.epsilon}</td><td>{pct(r.median_error)}</td><td>{pct(r.p90_error)}</td>
                {measures.map((m) => <td key={m}>{pct(r.by_measure[m])}</td>)}
                <td>{pct(r.suppressed)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note">
        Small groups carry the most error, which is the point of the noise; cells under 20 people are shown as
        "&lt;20" and not scored. Sums are the noisiest measure because one patient can move a total by up to 150 days.
      </p>
    </section>
  );
}

// One bar per ε: the median error. Lower is better; more ε buys accuracy and spends more budget.
function ErrorBars({ results, pick, onPick }) {
  const [ref, W] = useWidth(640);
  const H = 280, L = 44, B = 32, T = 24;
  const top = Math.ceil((Math.max(...results.map((r) => r.median_error)) * 100) / 5) * 5 / 100 || 0.05;
  const y = (v) => T + (1 - v / top) * (H - T - B);
  const slot = (W - L) / results.length;
  const w = Math.min(slot * 0.5, 72);
  const step = top > 0.05 ? 0.05 : top / 2;
  const ticks = Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step);
  return (
    <div ref={ref} className="chart-box">
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="chart bench-chart" role="img"
           aria-label="Median relative error at each epsilon">
        {ticks.map((t, i) => (
          <g key={t}>
            <line x1={L} x2={W} y1={y(t)} y2={y(t)} className={i === 0 ? "base" : "gridline"} />
            <text x={L - 8} y={y(t) + 4} className="axis" textAnchor="end">{`${+(t * 100).toFixed(2)}%`}</text>
          </g>
        ))}
        {results.map((r, i) => {
          const cx = L + i * slot + slot / 2, x = cx - w / 2;
          return (
            <g key={r.epsilon} className={r === pick ? "bar picked" : "bar"} onClick={() => onPick(r)}>
              <title>{`ε ${r.epsilon}: median error ${pct(r.median_error)}, ${pct(r.inside_error_bar)} inside the error bar`}</title>
              <rect x={L + i * slot} y={T} width={slot} height={H - T - B} className="hit" />
              <path d={`M${x},${y(0)} V${y(r.median_error) + 4} q0,-4 4,-4 h${w - 8} q4,0 4,4 V${y(0)} Z`} className="bench-bar" />
              <text x={cx} y={y(r.median_error) - 8} className="bench-value" textAnchor="middle">{pct(r.median_error)}</text>
              <text x={cx} y={H - 10} className="axis" textAnchor="middle">ε {r.epsilon}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
