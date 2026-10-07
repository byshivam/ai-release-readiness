"""A single self-contained HTML dashboard (no external scripts, works offline)."""

from __future__ import annotations

from html import escape

from readiness.assess import Assessment

DECISION_CLASS = {"GO": "go", "CONDITIONAL GO": "cond", "NO-GO": "nogo"}
STATUS_CLASS = {"Mitigated": "ok", "Open": "bad", "Evidence gap": "warn", "Accepted": "info"}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e4e3de;
--ok:#1f7a4d;--ok-bg:#e3f3ea;--bad:#b42318;--bad-bg:#fde7e5;--warn:#a15c07;--warn-bg:#fdf0dc;
--info:#2457a6;--info-bg:#e5edfa;--h1:#f1efe9;--h2:#f6e7c8;--h3:#f4c9b8;--h4:#eba59a}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--muted:#a3a29b;
--line:#33332f;--ok:#5fd19a;--ok-bg:#163526;--bad:#ff8a7e;--bad-bg:#40191a;--warn:#f2b45c;--warn-bg:#3a2a12;--info:#8fb5f5;--info-bg:#1a2740;
--h1:#252522;--h2:#3a3220;--h3:#45291f;--h4:#55211d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
.muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:16px}
.banner{display:flex;flex-wrap:wrap;align-items:center;gap:12px 20px}
.pill{font-weight:700;padding:6px 14px;border-radius:999px;font-size:15px}
.go{background:var(--ok-bg);color:var(--ok)}.cond{background:var(--warn-bg);color:var(--warn)}.nogo{background:var(--bad-bg);color:var(--bad)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:14px}
.kpi{border:1px solid var(--line);border-radius:10px;padding:10px 12px}.kpi b{display:block;font-size:24px}
.grid2{display:grid;grid-template-columns:1fr;gap:16px}@media(min-width:820px){.grid2{grid-template-columns:minmax(0,360px) minmax(0,1fr)}}
.heat{display:grid;grid-template-columns:22px repeat(5,minmax(0,1fr));gap:4px;font-size:12px}
.cell{min-height:56px;min-width:0;border-radius:6px;display:flex;flex-wrap:wrap;align-content:center;justify-content:center;gap:2px;padding:2px}
.ax{display:flex;align-items:center;justify-content:center;color:var(--muted)}
.tag{font-size:11px;font-weight:700;padding:1px 5px;border-radius:5px;border:1px solid currentColor}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}.info{color:var(--info)}
.s-ok{background:var(--ok-bg);color:var(--ok)}.s-bad{background:var(--bad-bg);color:var(--bad)}
.s-warn{background:var(--warn-bg);color:var(--warn)}.s-info{background:var(--info-bg);color:var(--info)}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;white-space:nowrap}
.table{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:600}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.metric{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.metric .v{font-size:20px;font-weight:700}svg.spark{width:100%;height:36px;display:block;margin-top:4px}
ul.reasons{margin:8px 0 0;padding-left:18px}
a{color:var(--info)}
"""


def _sparkline(points: list[float], threshold: float | None) -> str:
    if not points:
        return '<svg class="spark" viewBox="0 0 100 36"></svg>'
    lo = min(points + ([threshold] if threshold is not None else []))
    hi = max(points + ([threshold] if threshold is not None else []))
    span = (hi - lo) or 1.0
    lo, span = lo - span * 0.1, span * 1.2

    def y(v: float) -> float:
        return 32 - (v - lo) / span * 28

    xs = [4 + i * (92 / max(1, len(points) - 1)) for i in range(len(points))]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y(v):.1f}" for i, (x, v) in enumerate(zip(xs, points)))
    gate = f'<line x1="0" x2="100" y1="{y(threshold):.1f}" y2="{y(threshold):.1f}" stroke="currentColor" stroke-dasharray="3 3" opacity=".45"/>' if threshold is not None else ""
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y(v):.1f}" r="2.2" fill="currentColor"/>' for x, v in zip(xs, points))
    return f'<svg class="spark" viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden="true">{gate}<path d="{path}" fill="none" stroke="currentColor" stroke-width="1.6"/>{dots}</svg>'


def _heat_fill(score: int) -> str:
    return "var(--h4)" if score >= 15 else "var(--h3)" if score >= 10 else "var(--h2)" if score >= 5 else "var(--h1)"


def render(system: dict, register: dict, nist: dict, a: Assessment) -> str:
    ev = a.evidence
    counts = {s: sum(1 for r in a.risks if r.status == s) for s in STATUS_CLASS}

    # Heatmap: likelihood rows (5 at top) x impact columns.
    placed: dict[tuple[int, int], list] = {}
    for r in a.risks:
        placed.setdefault((int(r.risk["likelihood"]), int(r.risk["impact"])), []).append(r)
    heat = []
    for lk in range(5, 0, -1):
        heat.append(f'<div class="ax">{lk}</div>')
        for im in range(1, 6):
            tags = "".join(f'<span class="tag {STATUS_CLASS[r.status]}" title="{escape(r.risk["title"])}">{r.id}</span>' for r in placed.get((lk, im), []))
            heat.append(f'<div class="cell" style="background:{_heat_fill(lk * im)}">{tags}</div>')
    heat.append('<div></div>' + "".join(f'<div class="ax">{i}</div>' for i in range(1, 6)))

    # Metric trends.
    specs = {}
    for risk in register["risks"]:
        for e in risk.get("evidence", []):
            if e.get("type", "metric") == "metric":
                specs[e["metric"]] = e
    cards = []
    for name, spec in sorted(specs.items()):
        m = ev.metric(name)
        series = [v for _, v in ev.series(name)]
        value = "—" if m.value is None else f"{m.value:.2f}"
        cls = "muted"
        if m.value is not None:
            ok = m.value >= spec["threshold"] if spec["op"] in (">=", ">") else m.value <= spec["threshold"]
            cls = "ok" if ok and m.status == "current" else "warn" if ok else "bad"
        cards.append(
            f'<div class="metric"><div class="muted">{escape(name)} · gate {spec["op"]} {spec["threshold"]:.2f}</div>'
            f'<div class="v {cls}">{value}</div><div class="{cls}">{_sparkline(series, spec["threshold"])}</div>'
            f'<div class="muted">{len(series)} run(s) · {m.status}</div></div>'
        )

    rows = "".join(
        f"<tr><td><b>{r.id}</b></td><td>{escape(r.risk['title'])}<div class='muted'>{escape(r.risk['owner'])} · {', '.join(r.risk['nist'])}</div></td>"
        f"<td>{r.inherent_rating}</td><td><span class='badge s-{STATUS_CLASS[r.status]}'>{r.status}</span></td><td>{r.residual_rating}</td></tr>"
        for r in a.risks
    )
    nist_rows = "".join(
        f"<tr><td><b>{fn}</b></td><td>{len(subs)}</td><td>{escape(', '.join(subs))}</td></tr>" for fn, subs in nist.items()
    )
    reasons = "".join(f"<li>{escape(x)}</li>" for x in a.reasons)
    sign = system.get("sign_off") or {}
    sign_txt = (f"Signed off by {escape(sign['approver'])} on {escape(sign.get('date') or '—')}"
                if sign.get("approver") else "Awaiting human sign-off — this is a recommendation")
    latest = ev.latest_run

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Release Readiness</title><style>{CSS}</style></head>
<body><main>
<h1>{escape(system['name'])}</h1>
<div class="muted">AI release readiness · {escape(system['approved_configuration']['generator_model'])} · prompt {escape(system['approved_configuration']['prompt_version'])} · updated {ev.as_of:%Y-%m-%d %H:%M} UTC</div>

<section class="card">
  <div class="banner"><span class="pill {DECISION_CLASS[a.decision]}">{a.decision}</span><span class="muted">{sign_txt}</span></div>
  <ul class="reasons">{reasons}</ul>
  <div class="kpis">
    <div class="kpi"><b>{len(a.risks)}</b><span class="muted">Risks tracked</span></div>
    <div class="kpi"><b class="ok">{counts['Mitigated']}</b><span class="muted">Mitigated</span></div>
    <div class="kpi"><b class="bad">{counts['Open']}</b><span class="muted">Open</span></div>
    <div class="kpi"><b class="warn">{counts['Evidence gap']}</b><span class="muted">Evidence gap</span></div>
    <div class="kpi"><b class="info">{counts['Accepted']}</b><span class="muted">Accepted</span></div>
    <div class="kpi"><b>{len(ev.eligible_runs)}</b><span class="muted">Eval runs (approved config)</span></div>
  </div>
</section>

<div class="grid2">
  <section class="card"><h2>Inherent risk heatmap</h2>
    <div class="heat">{''.join(heat)}</div>
    <div class="muted" style="margin-top:8px">Rows: likelihood · columns: impact · tag colour: current status</div>
  </section>
  <section class="card"><h2>Risk register</h2><div class="table"><table>
    <thead><tr><th>ID</th><th>Risk</th><th>Inherent</th><th>Status</th><th>Residual</th></tr></thead><tbody>{rows}</tbody></table></div>
  </section>
</div>

<section class="card"><h2>Evaluation metrics</h2>
  <div class="metrics">{''.join(cards)}</div>
  <div class="muted" style="margin-top:8px">Dashed line: release gate. Latest run: {f"{latest.run_at:%Y-%m-%d %H:%M} UTC" if latest else "none"}.</div>
</section>

<section class="card"><h2>NIST AI RMF coverage</h2><div class="table"><table>
  <thead><tr><th>Function</th><th>Subcategories</th><th>Covered</th></tr></thead><tbody>{nist_rows}</tbody></table></div>
  <div class="muted" style="margin-top:8px">Full mapping in reports/NIST_AI_RMF.md</div>
</section>
</main></body></html>
"""
