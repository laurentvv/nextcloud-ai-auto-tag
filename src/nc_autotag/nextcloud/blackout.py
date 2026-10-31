from datetime import datetime, time, timedelta


def parse_windows(specs: list[str]) -> list[tuple[time, time]]:
    """« HH:MM-HH:MM » -> (début, fin) ; une fenêtre peut passer minuit (23:00-01:00)."""
    out = []
    for spec in specs:
        try:
            start, end = (time.fromisoformat(p.strip()) for p in spec.split("-"))
        except ValueError as e:
            raise ValueError(f"Fenêtre invalide « {spec} » (attendu HH:MM-HH:MM)") from e
        if start == end:
            raise ValueError(f"Fenêtre vide : {spec}")
        out.append((start, end))
    return out


def _inside(t: time, start: time, end: time) -> bool:
    return start <= t < end if start < end else t >= start or t < end


def in_blackout(now: datetime, windows: list[tuple[time, time]]) -> bool:
    return any(_inside(now.time(), s, e) for s, e in windows)


def seconds_until_end(now: datetime, windows: list[tuple[time, time]]) -> float:
    """0 hors fenêtre ; sinon durée jusqu'à la fin de la dernière fenêtre enchaînée."""
    total = 0.0
    current = now
    for _ in range(len(windows) + 1):
        hit = next(((s, e) for s, e in windows if _inside(current.time(), s, e)), None)
        if hit is None:
            break
        end = datetime.combine(current.date(), hit[1])
        if end <= current:
            end += timedelta(days=1)
        total += (end - current).total_seconds()
        current = end
    return total
