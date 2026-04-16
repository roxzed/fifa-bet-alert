"""Web dashboard for FIFA eSports Bet Alert — real-time results & analytics."""

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ_LOCAL = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ_LOCAL = timezone(timedelta(hours=-3))

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", "db.aoxwotodixhzfgcbuoem.supabase.co")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "postgres")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "CEKA2uwnKGPGAws6")
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "1800"))

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(application):
    # Startup: initial fetch + background refresh thread
    rows = _fetch_data()
    with _lock:
        _cache["rows"] = rows
        _cache["updated"] = datetime.now(timezone.utc)
    t = threading.Thread(target=_refresh_loop, daemon=True)
    t.start()
    yield


app = FastAPI(title="Lenda_BOT Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# ---------------------------------------------------------------------------
# Filters (mirror of production stats_engine.py)
# ---------------------------------------------------------------------------
BLACKLIST = {
    "Kavviro", "SPACE", "R0ge", "maksdh", "Kot", "Boulevard", "A1ose",
    "Revange", "Kivu17", "V1nn",
}
COND_BL = {
    "volvo": {"block_away_g2": True},
    "Grellz": {"block_home_g2": True},
    "nikkitta": {"block_home_g2": True, "block_lines": {"over25", "over35", "over45"}},
    "Cira": {"block_lines": {"over25", "over35", "over45"}},
    "tohi4": {"block_lines": {"over25", "over35", "over45"}},
}
BAD_HOURS = {0, 1, 9, 14, 16, 18}
BAD_HOUR_MIN_EDGE = 0.15

# Date when filters were deployed to production (local BRT time)
# Before this: simulate filters on all alerts
# After this: alerts already passed production filters
FILTERS_DEPLOY_DATE = datetime(2026, 4, 14)

WEEKDAYS_PT = {
    0: "Segunda", 1: "Terca", 2: "Quarta", 3: "Quinta",
    4: "Sexta", 5: "Sabado", 6: "Domingo",
}

LINE_LABELS = {
    "over15": "Over 1.5", "over25": "Over 2.5",
    "over35": "Over 3.5", "over45": "Over 4.5",
}


def apply_filter(r: dict, sent_local: datetime | None) -> tuple[bool, str]:
    """Return (passed, reason) mirroring production filters.

    Used only for alerts BEFORE FILTERS_DEPLOY_DATE (simulation).
    """
    player = r["losing_player"]
    line = r["best_line"] or "over25"
    is_home = player == r.get("player_home")
    edg = r["edge"] or 0
    lt = r["loss_type"] or "?"
    hour = sent_local.hour if sent_local else 0

    if player in BLACKLIST:
        return False, "Blacklist"
    cond = COND_BL.get(player)
    if cond:
        if cond.get("block_home_g2") and is_home:
            return False, f"BL cond: {player} HOME"
        if cond.get("block_away_g2") and not is_home:
            return False, f"BL cond: {player} AWAY"
        if cond.get("block_lines") and line in cond["block_lines"]:
            return False, f"BL cond: {player} {line}"
    if not is_home and lt == "tight":
        return False, "Tight AWAY"
    if hour in BAD_HOURS and edg < BAD_HOUR_MIN_EDGE:
        return False, f"Horario ruim ({hour:02d}h)"
    return True, ""


def get_hit(r: dict):
    bl = r["best_line"] or "over25"
    return r.get(f"{bl}_hit")


def get_odds(r: dict) -> float:
    bl = r["best_line"] or "over25"
    return r.get(f"{bl}_odds") or 0


def get_profit(r: dict) -> float:
    h = get_hit(r)
    o = get_odds(r)
    if h is None:
        return 0
    return (o - 1.0) if h else -1.0


# ---------------------------------------------------------------------------
# Data cache (refreshed in background)
# ---------------------------------------------------------------------------
_cache: dict = {"rows": [], "updated": None}
_lock = threading.Lock()

QUERY = """
SELECT a.id, a.losing_player, a.best_line, a.sent_at,
       a.over15_odds, a.over25_odds, a.over35_odds, a.over45_odds,
       a.actual_goals, a.over15_hit, a.over25_hit, a.over35_hit, a.over45_hit,
       a.profit_flat, a.edge, a.true_prob, a.star_rating,
       a.game1_score, a.loss_type, a.loser_goals_g1,
       m.player_home, m.player_away, m.team_home, m.team_away,
       m.score_home, m.score_away
FROM alerts a
LEFT JOIN matches m ON a.match_id = m.id
WHERE a.validated_at IS NOT NULL
  AND a.sent_at >= '2026-04-05'
ORDER BY a.sent_at DESC
"""


def _fetch_data() -> list[dict]:
    try:
        conn = psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASS, sslmode="require",
        )
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(QUERY)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[DB ERROR] {e}")
        return []


def _refresh_loop():
    while True:
        rows = _fetch_data()
        if rows:
            with _lock:
                _cache["rows"] = rows
                _cache["updated"] = datetime.now(timezone.utc)
        time.sleep(REFRESH_INTERVAL)


def _get_data() -> list[dict]:
    with _lock:
        return list(_cache["rows"])


def _get_updated() -> str:
    with _lock:
        if _cache["updated"]:
            return _cache["updated"].strftime("%H:%M:%S UTC")
    return "..."


# ---------------------------------------------------------------------------
# Build enriched dataset
# ---------------------------------------------------------------------------
def build_dataset(rows: list[dict]) -> dict:
    """Process raw rows into enriched alerts + aggregated stats.

    Hybrid approach:
    - Before FILTERS_DEPLOY_DATE (14/04): simulate filters on all alerts
    - After FILTERS_DEPLOY_DATE: alerts already passed production filters
    """
    alerts = []

    for r in rows:
        line = r["best_line"] or "over25"
        is_home = r["losing_player"] == r.get("player_home")
        hit = get_hit(r)
        odds = get_odds(r)
        pl = get_profit(r)

        loser_team = r.get("team_home") if is_home else r.get("team_away")
        opp_team = r.get("team_away") if is_home else r.get("team_home")
        opponent = r.get("player_away") if is_home else r.get("player_home")

        # Convert UTC to local timezone (same as Telegram bot)
        sent_utc = r["sent_at"]
        if sent_utc:
            sent_local = sent_utc.replace(tzinfo=timezone.utc).astimezone(TZ_LOCAL)
        else:
            sent_local = None

        # Before filters deploy: simulate filters; after: already filtered
        is_before_deploy = sent_local and sent_local.replace(tzinfo=None) < FILTERS_DEPLOY_DATE
        if is_before_deploy:
            passed, _ = apply_filter(r, sent_local)
            if not passed:
                continue  # skip alerts that would have been blocked

        alert = {
            "id": r["id"],
            "date": sent_local.strftime("%d/%m") if sent_local else "",
            "time": sent_local.strftime("%H:%M") if sent_local else "",
            "datetime": sent_local,
            "player": r["losing_player"],
            "opponent": opponent or "?",
            "team": loser_team or "?",
            "opp_team": opp_team or "?",
            "side": "HOME" if is_home else "AWAY",
            "g1_score": r["game1_score"] or "?",
            "loss_type": r["loss_type"] or "?",
            "line": LINE_LABELS.get(line, line),
            "line_key": line,
            "odds": round(odds, 2) if odds else 0,
            "edge": round((r["edge"] or 0) * 100, 1),
            "goals": r["actual_goals"],
            "star": r["star_rating"] or 0,
            "hit": hit,
            "pl": round(pl, 2),
            "passed": True,
            "hour": sent_local.hour if sent_local else 0,
            "weekday": WEEKDAYS_PT.get(sent_local.weekday(), "?") if sent_local else "?",
        }
        alerts.append(alert)

    # Cumulative P/L
    chrono = sorted(alerts, key=lambda a: a["datetime"])
    cum = 0
    for a in chrono:
        cum += a["pl"]
        a["cum_pl"] = round(cum, 2)

    stats = _aggregate(alerts)
    return {
        "alerts": alerts,
        "passed": alerts,
        "stats": stats,
        "stats_all": stats,
        "updated": _get_updated(),
    }


def _aggregate(alerts: list[dict]) -> dict:
    """Compute aggregated stats from a list of alert dicts."""
    if not alerts:
        return {
            "total": 0, "hits": 0, "wr": 0, "pl": 0, "roi": 0,
            "avg_odds": 0, "tips_per_day": 0,
            "by_day": {}, "by_hour": {}, "by_player": {}, "by_team": {},
            "by_line": {}, "by_side": {}, "by_loss_type": {}, "by_weekday": {},
            "cum_by_day": [],
        }

    total = len(alerts)
    hits = sum(1 for a in alerts if a["hit"])
    pl = sum(a["pl"] for a in alerts)
    days = len(set(a["date"] for a in alerts))

    def group_stats(key):
        groups = defaultdict(list)
        for a in alerts:
            groups[a[key]].append(a)
        result = {}
        for k, items in groups.items():
            n = len(items)
            h = sum(1 for a in items if a["hit"])
            p = sum(a["pl"] for a in items)
            result[k] = {
                "n": n, "hits": h,
                "wr": round(h / n * 100, 1) if n else 0,
                "pl": round(p, 2),
                "roi": round(p / n * 100, 1) if n else 0,
            }
        return result

    # Cumulative P/L by day
    chrono = sorted(alerts, key=lambda a: a["datetime"])
    day_pl = defaultdict(float)
    for a in chrono:
        day_pl[a["date"]] += a["pl"]
    cum = 0
    cum_by_day = []
    for day in sorted(day_pl.keys(), key=lambda d: datetime.strptime(d, "%d/%m")):
        cum += day_pl[day]
        cum_by_day.append({"day": day, "pl": round(day_pl[day], 2), "cum": round(cum, 2)})

    by_player = group_stats("player")
    by_team = group_stats("team")

    # Pre-sort for templates (Jinja2 can't sort dicts of dicts)
    players_ranked = sorted(by_player.items(), key=lambda x: x[1]["pl"], reverse=True)
    teams_ranked = sorted(
        ((k, v) for k, v in by_team.items() if v["n"] >= 3),
        key=lambda x: x[1]["pl"], reverse=True,
    )

    return {
        "total": total,
        "hits": hits,
        "wr": round(hits / total * 100, 1),
        "pl": round(pl, 2),
        "roi": round(pl / total * 100, 1),
        "avg_odds": round(sum(a["odds"] for a in alerts) / total, 2),
        "tips_per_day": round(total / days, 1) if days else 0,
        "days": days,
        "by_day": group_stats("date"),
        "by_hour": group_stats("hour"),
        "by_player": by_player,
        "by_team": by_team,
        "by_line": group_stats("line"),
        "by_side": group_stats("side"),
        "by_loss_type": group_stats("loss_type"),
        "by_weekday": group_stats("weekday"),
        "cum_by_day": cum_by_day,
        "players_ranked": players_ranked,
        "teams_ranked": teams_ranked,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _filter_by_date(rows: list[dict], date_from: str | None, date_to: str | None) -> list[dict]:
    """Filter rows by date range (inclusive)."""
    filtered = rows
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            # Compare using local time (BRT)
            filtered = [
                r for r in filtered
                if r["sent_at"] and r["sent_at"].replace(tzinfo=timezone.utc).astimezone(TZ_LOCAL).replace(tzinfo=None) >= dt_from
            ]
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            filtered = [
                r for r in filtered
                if r["sent_at"] and r["sent_at"].replace(tzinfo=timezone.utc).astimezone(TZ_LOCAL).replace(tzinfo=None) <= dt_to
            ]
        except ValueError:
            pass
    return filtered


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, de: str | None = None, ate: str | None = None):
    rows = _filter_by_date(_get_data(), de, ate)
    data = build_dataset(rows)
    data["filter_from"] = de or ""
    data["filter_to"] = ate or ""
    tpl = templates.env.get_template("dashboard.html")
    return HTMLResponse(tpl.render(**data))


@app.get("/resultados", response_class=HTMLResponse)
async def resultados(request: Request, de: str | None = None, ate: str | None = None):
    rows = _filter_by_date(_get_data(), de, ate)
    data = build_dataset(rows)
    data["filter_from"] = de or ""
    data["filter_to"] = ate or ""
    tpl = templates.env.get_template("resultados.html")
    return HTMLResponse(tpl.render(**data))


@app.get("/api/data")
async def api_data():
    from fastapi.responses import JSONResponse
    import json

    data = build_dataset(_get_data())

    def default_ser(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj)}")

    # Remove non-serializable keys and serialize with custom handler
    for key in ("players_ranked", "teams_ranked"):
        data["stats"].pop(key, None)
        data["stats_all"].pop(key, None)

    return JSONResponse(json.loads(json.dumps(data, default=default_ser)))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
