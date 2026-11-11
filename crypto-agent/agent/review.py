"""Revisión nocturna: qué se hizo, qué se falló, cómo de fiable es el REFLEX y qué
cambios de política merecería la pena probar.

Solo PROPONE. Una candidata se guarda como versión nueva y no se activa hasta que tú lo
ordenas con `python -m agent policy promote N --confirm`; siempre se puede volver atrás.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from .calibration import EDGES, Calibrator
from .config import Settings
from .policy import PolicyStore

MARGIN = 0.03        # ventaja mínima sobre el punto de equilibrio para fiarse de un tramo
MIN_SAMPLES = 50


def _fmt(x, pattern="{:.3f}") -> str:
    return "—" if x is None else pattern.format(x)


def analyse(cal: Calibrator, threshold: float) -> dict:
    """Lee la calibración y decide si hay algo que proponer. Función pura."""
    breakeven = cal.breakeven_p + MARGIN   # equilibrio ya descontados los costes
    rows = []
    for lo, row in zip(EDGES, cal.table()):
        cons = cal.p_conservative(lo, MIN_SAMPLES)
        rows.append({**row, "lo": lo, "p_prudente": cons, "fiable": row["n"] >= MIN_SAMPLES and cons > breakeven})
    good = [r["lo"] for r in rows if r["fiable"]]
    skill = None
    if cal.count >= 100:
        skill = cal.brier_calibrated < cal.brier_base_rate
    notes: list[str] = []
    new_threshold = None
    if cal.count < 100:
        notes.append(f"Solo hay {cal.count} señales resueltas: demasiado pronto para sacar conclusiones (mínimo 100).")
    elif not good:
        notes.append("Ningún tramo de confianza demuestra ventaja tras costes. El agente seguirá sin arriesgar "
                     "(tamaño de Kelly = 0). No se propone ningún cambio: bajar el listón sería apostar sin evidencia.")
    else:
        lowest = min(g for g in good if g >= 0.6) if any(g >= 0.6 for g in good) else None
        if lowest is not None and abs(lowest - 0.01 - threshold) > 1e-9:
            new_threshold = round(lowest - 0.01, 2)
            verb = "bajar" if new_threshold < threshold else "subir"
            notes.append(f"El tramo {lowest:.1f}+ acierta por encima del equilibrio con margen. Se propone {verb} "
                         f"la confianza mínima de {threshold:.2f} a {new_threshold:.2f}.")
        else:
            notes.append("La política actual ya coincide con los tramos que demuestran ventaja.")
    if skill is False:
        notes.append("La confianza del REFLEX no predice mejor que suponer siempre la tasa media "
                     f"(Brier {cal.brier_calibrated:.4f} frente a {cal.brier_base_rate:.4f}): hoy no aporta información.")
    return {"rows": rows, "breakeven": breakeven, "skill": skill, "notes": notes, "new_threshold": new_threshold}


def nightly_review(store, settings: Settings, cal: Calibrator, policies: PolicyStore, now_ms: int,
                   policy_version: int, write_report: bool = True) -> dict:
    since = now_ms - 86_400_000
    fills = [f for f in store.fills_since(since) if f["status"] != "rejected"]
    rejected = [f for f in store.fills_since(since) if f["status"] == "rejected"]
    decisions = store.decisions_since(since)
    misses = [d for d in decisions if d["action"] == "miss"]
    actions: dict[str, int] = {}
    for d in decisions:
        actions[d["action"]] = actions.get(d["action"], 0) + 1
    lat = sorted(d["latency_ms"] for d in decisions if d["action"] != "miss")
    pnl = sum(f["pnl"] or 0.0 for f in fills if f["side"] == "SELL")
    miss_rate = len(misses) / len(decisions) if decisions else 0.0

    analysis = analyse(cal, settings.policy.min_direction_confidence)
    notes = list(analysis["notes"])
    if miss_rate > 0.05:
        notes.append(f"Se perdió el {miss_rate * 100:.0f} % de las velas por fallos o retrasos del REFLEX. "
                     "Revisa la conexión, la clave o sube `reflex.timeout_s`.")
    candidate = None
    if analysis["new_threshold"] is not None:
        proposed = settings.policy.model_copy(update={"min_direction_confidence": analysis["new_threshold"]})
        candidate = policies.propose(proposed, notes[0], {
            "signals": cal.count, "brier_calibrated": cal.brier_calibrated, "brier_base_rate": cal.brier_base_rate,
            "table": [{k: r[k] for k in ("tramo", "n", "acierto", "p_prudente")} for r in analysis["rows"]]})
        notes.append(f"Candidata guardada como versión {candidate}. NO está activa. Para probarla en simulación: "
                     f"`python -m agent policy promote {candidate} --confirm`.")

    day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    result = {
        "day": day, "policy_version": policy_version, "fills": len(fills), "rejected_orders": len(rejected),
        "realized_pnl": pnl, "decisions": len(decisions), "actions": actions, "misses": len(misses),
        "miss_rate": miss_rate, "miss_reasons": sorted({(m["error"] or "")[:80] for m in misses})[:5],
        "latency_ms_p50": lat[len(lat) // 2] if lat else None,
        "latency_ms_p95": lat[int(len(lat) * 0.95)] if lat else None,
        "signals": cal.count, "base_rate": cal.base_rate, "brier_raw": cal.brier_raw,
        "brier_calibrated": cal.brier_calibrated, "brier_base_rate": cal.brier_base_rate,
        "breakeven": cal.breakeven_p, "skill": analysis["skill"], "calibration": analysis["rows"], "notes": notes,
        "candidate_version": candidate,
    }
    if write_report:
        path = os.path.join(settings.data_dir, "reports", f"review-{day}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(render(result))
        result["report_path"] = path
    store.event(now_ms, "review", " | ".join(notes))
    return result


def render(r: dict) -> str:
    lines = [
        f"# Revisión del {r['day']} (política v{r['policy_version']})", "",
        "## Últimas 24 horas",
        f"- Órdenes ejecutadas: {r['fills']} · rechazadas: {r['rejected_orders']} · resultado cerrado: {r['realized_pnl']:+.2f} $",
        f"- Decisiones: {r['decisions']} · por tipo: {r['actions']}",
        f"- Velas perdidas: {r['misses']} ({r['miss_rate'] * 100:.1f} %)" + (f" · motivos: {r['miss_reasons']}" if r["misses"] else ""),
        f"- Latencia del REFLEX: mediana {_fmt(r['latency_ms_p50'], '{:.0f}')} ms · p95 {_fmt(r['latency_ms_p95'], '{:.0f}')} ms", "",
        "## Calibración (todas las señales resueltas)",
        f"- Señales: {r['signals']} · acierto medio: {_fmt(r['base_rate'], '{:.1%}')} · acierto necesario para no perder "
        f"tras costes: {_fmt(r['breakeven'], '{:.1%}')}",
        f"- Brier de la confianza declarada: {_fmt(r['brier_raw'], '{:.4f}')} · calibrada: {_fmt(r['brier_calibrated'], '{:.4f}')} "
        f"· de suponer siempre la media: {_fmt(r['brier_base_rate'], '{:.4f}')} (menor es mejor)", "",
        "| Confianza | Señales | Acierto real | Probabilidad prudente | ¿Fiable? |", "|---|---|---|---|---|",
    ]
    for row in r["calibration"]:
        lines.append(f"| {row['tramo']} | {row['n']} | {_fmt(row['acierto'], '{:.1%}')} | {row['p_prudente']:.1%} | {'sí' if row['fiable'] else 'no'} |")
    lines += ["", "## Conclusiones y propuestas"] + [f"- {n}" for n in r["notes"]] + [""]
    return "\n".join(lines)
