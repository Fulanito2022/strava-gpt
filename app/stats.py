from typing import Iterable
from .models import Activity

def pace_per_km(seconds: float, meters: float) -> str:
    if meters <= 0 or seconds <= 0:
        return "-"
    sec_per_km = seconds / (meters / 1000.0)
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    return f"{m}:{s:02d} min/km"

def _pace_sec_per_km(seconds: float, meters: float) -> float | None:
    if meters <= 0 or seconds <= 0:
        return None
    return seconds / (meters / 1000.0)

def _fmt_mmss(sec: float | None) -> str | None:
    if sec is None:
        return None
    sec = int(round(sec))
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"

def summarize_runs(runs: Iterable[Activity]):
    runs = list(runs)
    if not runs:
        return {
            "sessions": 0,
            "distance_km": 0.0,
            "moving_time_h": 0.0,
            "elev_gain_m": 0,
            "avg_pace": "-",
            "avg_hr": None,
            "best_efforts": {},
        }
    dist = sum(r.distance_m for r in runs)
    movs = sum(r.moving_time_s for r in runs)
    elev = sum(r.total_elevation_gain_m for r in runs)
    hr_vals = [r.average_heartrate for r in runs if r.average_heartrate]
    avg_hr = int(sum(hr_vals) / len(hr_vals)) if hr_vals else None
    avg_p = pace_per_km(movs, dist)

    # Mejores parciales según "best_efforts" de Strava (si están en el detalle)
    best = {"5k": None, "10k": None, "21k": None}
    for r in runs:
        if r.raw and isinstance(r.raw, dict):
            be = r.raw.get("best_efforts") or []
            for effort in be:
                name = (effort.get("name") or "").lower()
                if "5k" in name and (best["5k"] is None or effort["elapsed_time"] < best["5k"]):
                    best["5k"] = effort["elapsed_time"]
                if "10k" in name and (best["10k"] is None or effort["elapsed_time"] < best["10k"]):
                    best["10k"] = effort["elapsed_time"]
                if any(x in name for x in ["half marathon", "21k", "21.1k", "21.1 km"]):
                    if best["21k"] is None or effort["elapsed_time"] < best["21k"]:
                        best["21k"] = effort["elapsed_time"]

    def fmt_time(sec):
        if sec is None:
            return None
        m = int(sec // 60)
        s = int(sec % 60)
        return f"{m}:{s:02d}"

    return {
        "sessions": len(runs),
        "distance_km": round(dist / 1000.0, 2),
        "moving_time_h": round(movs / 3600.0, 2),
        "elev_gain_m": elev,
        "avg_pace": avg_p,
        "avg_hr": avg_hr,
        "best_efforts": {k: fmt_time(v) for k, v in best.items()},
    }

def compare_runs(curr: Iterable[Activity], prev: Iterable[Activity]):
    """Devuelve resumen de ambos periodos + diferencias y una recomendación breve."""
    curr = list(curr); prev = list(prev)

    def totals(rs: list[Activity]):
        dist = sum(r.distance_m for r in rs)
        movs = sum(r.moving_time_s for r in rs)
        elev = sum(r.total_elevation_gain_m for r in rs)
        hr_vals = [r.average_heartrate for r in rs if r.average_heartrate]
        avg_hr = int(sum(hr_vals) / len(hr_vals)) if hr_vals else None
        pace_sec = _pace_sec_per_km(movs, dist)
        return dist, movs, elev, avg_hr, pace_sec

    c_dist, c_movs, c_elev, c_hr, c_pace_sec = totals(curr)
    p_dist, p_movs, p_elev, p_hr, p_pace_sec = totals(prev)

    current = summarize_runs(curr)
    previous = summarize_runs(prev)

    # Deltas
    diff = {
        "sessions": current["sessions"] - previous["sessions"],
        "distance_km": round(current["distance_km"] - previous["distance_km"], 2),
        "moving_time_h": round(current["moving_time_h"] - previous["moving_time_h"], 2),
        "elev_gain_m": current["elev_gain_m"] - previous["elev_gain_m"],
        "avg_pace_change_text": None,
        "avg_hr_change": None,
    }
    # Ritmo: positivo = más rápido
    if c_pace_sec is not None and p_pace_sec is not None:
        sec = p_pace_sec - c_pace_sec
        tag = "más rápido" if sec > 0 else "más lento"
        diff["avg_pace_change_text"] = f"{tag} {_fmt_mmss(abs(sec))} /km"

    if c_hr is not None and p_hr is not None:
        diff["avg_hr_change"] = c_hr - p_hr

    # Recomendación breve
    advice = []
    if p_dist > 0:
        vol_pct = (c_dist - p_dist) / p_dist * 100.0
        if vol_pct > 25:
            advice.append("Has subido el volumen >25%: reduce a incrementos ~10-15% para evitar sobrecarga.")
        elif vol_pct < -20:
            advice.append("Bajaste mucho el volumen: recupera de forma progresiva.")
    elif c_dist > 0 and p_dist == 0:
        advice.append("Reinicio de entrenos: escala la carga +5–10% semanal.")

    if diff["avg_pace_change_text"]:
        faster = "más rápido" in diff["avg_pace_change_text"]
        if faster and (diff["avg_hr_change"] is None or diff["avg_hr_change"] <= 2):
            advice.append("Mejora eficiente: mejor ritmo con FC controlada.")
        if (not faster) and diff["avg_hr_change"] and diff["avg_hr_change"] >= 3:
            advice.append("Ritmo peor y FC más alta: posible fatiga, considera descarga.")

    if not advice:
        advice.append("Mantén progresión estable y revisa técnica/zonas.")

    return {
        "current": current,
        "previous": previous,
        "diff": diff,
        "advice": " ".join(advice),
    }
