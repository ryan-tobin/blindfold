// Hand-drawn SVG: the privacy meter and the answer's bar chart with error bars.

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
        <text x="100" y="100" className="meter-number">{Number(left.toFixed(2))}</text>
        <text x="100" y="126" className="meter-unit">of {total} ε left</text>
      </svg>
      <figcaption>
        Privacy budget. Every answer spends some of it, in public. At 0 the dataset closes for good.
        {pending > 0 && ` ${Number(pending.toFixed(2))} ε is held by questions being answered.`}
      </figcaption>
    </figure>
  );
}

export function AnswerChart({ answer }) {
  const cells = answer.cells;
  const rate = answer.measure === "rate";
  const top = Math.max(...cells.map((c) => (c.value ?? 0) + (c.plus_minus ?? 0)), rate ? 0.01 : 1);
  const max = rate ? Math.min(top * 1.1, 1) : top * 1.1;
  const W = 560, L = 140, R = 150, ROW = 36;
  const x = (v) => L + (Math.min(Math.max(v, 0), max) / max) * (W - L - R);
  return (
    <svg viewBox={`0 0 ${W} ${cells.length * ROW + 8}`} className="chart" role="img">
      {cells.map((c, i) => {
        const y = i * ROW + 4;
        const label = Object.values(c.cell).join(" · ") || "all";
        return (
          <g key={i}>
            <text x={L - 10} y={y + 19} className="chart-label">{label}</text>
            {!c.suppressed && (
              <>
                <rect x={L} y={y + 6} width={x(c.value) - L} height={20} rx="2" className="chart-bar" />
                <line x1={x(c.value - c.plus_minus)} x2={x(c.value + c.plus_minus)} y1={y + 16} y2={y + 16}
                      className="chart-err" />
                <line x1={x(c.value - c.plus_minus)} x2={x(c.value - c.plus_minus)} y1={y + 10} y2={y + 22}
                      className="chart-err" />
                <line x1={x(c.value + c.plus_minus)} x2={x(c.value + c.plus_minus)} y1={y + 10} y2={y + 22}
                      className="chart-err" />
              </>
            )}
            <text x={W - R + 12} y={y + 19} className="chart-value">{c.display}</text>
          </g>
        );
      })}
    </svg>
  );
}
