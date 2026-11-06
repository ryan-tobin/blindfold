// Hand-drawn SVG: the privacy meter and the answer's bar chart with error bars.
import { useLayoutEffect, useRef, useState } from "react";

// An ε amount, set the same way everywhere: one mono token.
export function Eps({ v }) {
  return <span className="eps">ε {v}</span>;
}

// The width a chart actually has, so it's drawn at real pixel size and its text never shrinks.
export function useWidth(fallback) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => { if (e.contentRect.width) setWidth(Math.round(e.contentRect.width)); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function Meter({ total, spent, pending = 0 }) {
  const r = 80;
  const c = 2 * Math.PI * r;
  const left = Math.max(total - spent, 0);
  const frac = total ? left / total : 0;
  const held = total ? Math.min(pending, left) / total : 0;
  return (
    <figure className="meter">
      <svg viewBox="0 0 200 200" role="img" aria-label={`${left} of ${total} epsilon left`}>
        <circle cx="100" cy="100" r={r} className="meter-track" />
        <circle cx="100" cy="100" r={r} className="meter-left" strokeDasharray={c}
                strokeDashoffset={c * (1 - frac)} transform="rotate(-90 100 100)" />
        {held > 0 && (
          <circle cx="100" cy="100" r={r} className="meter-held" strokeDasharray={`${c * held} ${c}`}
                  transform={`rotate(${-90 + 360 * (frac - held)} 100 100)`} />
        )}
        <text x="100" y="102" className="meter-number">{Number(left.toFixed(2))}</text>
        <text x="100" y="128" className="meter-unit">of {total} ε left</text>
      </svg>
      <figcaption>
        Privacy budget. Every answer spends some of it, in public. At 0 the dataset closes for good.
        {pending > 0 && ` ${Number(pending.toFixed(2))} ε is held by questions being answered.`}
      </figcaption>
    </figure>
  );
}

// One bar per cell with its error bar. Narrow screens put each label above its bar.
export function AnswerChart({ answer }) {
  const [ref, W] = useWidth(560);
  const cells = answer.cells;
  const rate = answer.measure === "rate";
  const top = Math.max(...cells.map((c) => (c.value ?? 0) + (c.plus_minus ?? 0)), rate ? 0.01 : 1);
  const max = rate ? Math.min(top * 1.1, 1) : top * 1.1;
  const narrow = W < 420;
  const L = narrow ? 0 : Math.min(160, Math.round(W * 0.28)), R = 112;
  const ROW = narrow ? 48 : 36, BAR = narrow ? 22 : 6;
  const x = (v) => L + (Math.min(Math.max(v, 0), max) / max) * (W - L - R);
  return (
    <div ref={ref} className="chart-box">
      <svg width={W} height={cells.length * ROW + 4} viewBox={`0 0 ${W} ${cells.length * ROW + 4}`}
           className="chart" role="img" aria-label="Noisy answer with error bars">
        {cells.map((c, i) => {
          const y = i * ROW + 2;
          const label = Object.values(c.cell).join(" · ") || "all";
          const mid = y + BAR + 10;
          return (
            <g key={i}>
              <text x={narrow ? 0 : L - 12} y={narrow ? y + 14 : mid + 5} textAnchor={narrow ? "start" : "end"}
                    className="chart-label">{label}</text>
              <line x1={L} x2={L} y1={mid - 12} y2={mid + 12} className="chart-base" />
              {!c.suppressed && (
                <>
                  <rect x={L} y={mid - 10} width={Math.max(x(c.value) - L, 0)} height={20} rx="3" className="chart-bar" />
                  <line x1={x(c.value)} x2={x(c.value)} y1={mid - 10} y2={mid + 10} className="chart-mark" />
                  <path d={`M${x(c.value - c.plus_minus)},${mid - 5} v10 M${x(c.value - c.plus_minus)},${mid}
                            H${x(c.value + c.plus_minus)} M${x(c.value + c.plus_minus)},${mid - 5} v10`}
                        className="chart-err" />
                </>
              )}
              <text x={W} y={mid + 5} textAnchor="end" className={c.suppressed ? "chart-value muted" : "chart-value"}>
                {c.display}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
