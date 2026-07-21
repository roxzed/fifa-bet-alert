"""Testes do Reporter — relatório diário FREE (send_daily_report_free).

Leves: só mocks, sem DB. Copy pública — nunca revela o método (sem
"volta", "g1", "g2", "perdedor").
"""

from unittest.mock import AsyncMock, MagicMock

from src.core.reporter import Reporter


def _al(player, line, status, entry_odd, goals):
    a = MagicMock()
    a.losing_player = player
    a.line = line
    a.status = status
    a.entry_odd = entry_odd
    a.actual_goals = goals
    a.hit = (status == "green")
    return a


def _reporter(alerts):
    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=alerts)
    notifier = MagicMock()
    notifier.send_free_raw = AsyncMock(return_value=1)
    return Reporter(
        alert_repo=MagicMock(),
        player_repo=MagicMock(),
        method_stats_repo=MagicMock(),
        notifier=notifier,
        alert_free_repo=repo,
    ), notifier


async def test_relatorio_free_agrega_green_red_e_ignora_void():
    alerts = [
        _al("Sena", "over15", "green", 1.75, 3),
        _al("Bosko", "over25", "red", 1.90, 2),
        _al("X", "over15", "green", 1.80, 4),
        _al("Y", "over15", "void", None, 3),
    ]
    r, n = _reporter(alerts)
    await r.send_daily_report_free()
    n.send_free_raw.assert_awaited_once()
    t = n.send_free_raw.await_args.args[0]
    assert "2" in t and "GREEN" in t.upper()  # 2 greens
    # nao revela metodo
    for f in ["volta", "g1", "g2", "perdedor", "edge", "ev"]:
        assert f not in t.lower()


async def test_free_report_noop_sem_repo():
    r = Reporter(
        alert_repo=MagicMock(),
        player_repo=MagicMock(),
        method_stats_repo=MagicMock(),
        notifier=MagicMock(),
        alert_free_repo=None,
    )
    await r.send_daily_report_free()  # nao lanca, nao envia


async def test_free_report_noop_sem_alertas_validados():
    """Sem greens/reds nas ultimas 24h (so void ou nada) -> nao envia."""
    alerts = [_al("Y", "over15", "void", None, 3)]
    r, n = _reporter(alerts)
    await r.send_daily_report_free()
    n.send_free_raw.assert_not_awaited()
