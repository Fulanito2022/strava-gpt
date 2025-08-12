from typing import Iterable
from .models import Activity

def pace_per_km(seconds: float, meters: float) -> str:
    if meters <= 0 or seconds <= 0:
        return "-"
    sec_per_km = seconds / (meters / 1000.0)
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km % 60))
    return f"{m}:{s:02d} min/km"

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
    avg_hr = int(sum(hr_vals)/len(hr_vals)) if hr_vals else None

    # Ritmo medio ponderado por distancia
    avg_p = pace_per_km(movs, dist)

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
        "distance_km": round(dist/1000.0, 2),
        "moving_time_h": round(movs/3600.0, 2),
        "elev_gain_m": elev,
        "avg_pace": avg_p,
        "avg_hr": avg_hr,
        "best_efforts": {k: fmt_time(v) for k, v in best.items()},
    }
