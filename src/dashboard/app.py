"""Dashboard web minimo — FastAPI + JSON endpoints para monitoramento.

Roda em porta separada (8080). Mostra:
- Status do sistema
- P&L tracking
- Alertas recentes
- Performance por jogador/time/hora

Uso: python -m src.dashboard.app
"""

from __future__ import annotations

from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="FIFA Bet Alert Dashboard")


def _get_session_factory():
    from src.db.database import async_session_factory
    return async_session_factory


@app.get("/")
async def index():
    """Dashboard principal — HTML simples."""
    return HTMLResponse("""
    <html><head><title>FIFA Bet Alert</title>
    <style>
        body { font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }
        h1 { color: #00d4ff; } h2 { color: #0f9b58; border-bottom: 1px solid #333; }
        .card { background: #16213e; padding: 15px; margin: 10px 0; border-radius: 8px; }
        .green { color: #0f9b58; } .red { color: #e74c3c; }
        table { border-collapse: collapse; width: 100%; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #00d4ff; }
        a { color: #00d4ff; }
    </style>
    </head><body>
    <h1>FIFA Bet Alert Dashboard</h1>
    <div class="card">
        <h2>Endpoints</h2>
        <ul>
            <li><a href="/api/pnl">/api/pnl</a> — P&L ultimos 30 dias</li>
            <li><a href="/api/pnl?days=7">/api/pnl?days=7</a> — P&L ultimos 7 dias</li>
            <li><a href="/api/alerts">/api/alerts</a> — Alertas recentes</li>
            <li><a href="/api/players">/api/players</a> — Performance por jogador</li>
            <li><a href="/api/health">/api/health</a> — Status do sistema</li>
        </ul>
    </div>
    </body></html>
    """)


@app.get("/api/health")
async def health():
    """Status do sistema."""
    return {"status": "running", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/pnl")
async def pnl(days: int = 30):
    """P&L dos ultimos N dias."""
    try:
        factory = _get_session_factory()
        from src.db.repositories import AlertRepository
        session = factory()
        repo = AlertRepository(session)
        result = await repo.get_pnl_summary(days)
        await session.close()
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/alerts")
async def recent_alerts(limit: int = 20):
    """Alertas recentes."""
    try:
        factory = _get_session_factory()
        from src.db.models import Alert
        from sqlalchemy import select
        session = factory()
        stmt = (
            select(Alert)
            .order_by(Alert.sent_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        alerts = result.scalars().all()
        data = []
        for a in alerts:
            data.append({
                "id": a.id,
                "player": a.losing_player,
                "best_line": a.best_line,
                "odds": a.over25_odds,
                "edge": round(a.edge, 4) if a.edge else None,
                "stars": a.star_rating,
                "actual_goals": a.actual_goals,
                "hit": bool(a.over25_hit) if a.over25_hit is not None else None,
                "profit": round(getattr(a, "profit_flat", None) or 0, 2),
                "sent_at": a.sent_at.isoformat() if a.sent_at else None,
                "validated": a.validated_at is not None,
            })
        await session.close()
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/players")
async def player_performance(min_alerts: int = 3, days: int = 30):
    """Performance por jogador."""
    try:
        factory = _get_session_factory()
        from src.db.repositories import AlertRepository
        session = factory()
        repo = AlertRepository(session)
        pnl = await repo.get_pnl_summary(days)
        await session.close()

        players = []
        for name, stats in pnl.get("by_player", {}).items():
            if stats["total"] < min_alerts:
                continue
            rate = stats["wins"] / stats["total"] if stats["total"] else 0
            roi = stats["profit"] / stats["total"] if stats["total"] else 0
            players.append({
                "player": name,
                "alerts": stats["total"],
                "wins": stats["wins"],
                "hit_rate": round(rate, 3),
                "profit": round(stats["profit"], 2),
                "roi": round(roi, 3),
            })
        players.sort(key=lambda x: x["profit"], reverse=True)
        return players
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
