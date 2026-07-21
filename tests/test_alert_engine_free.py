"""Tests para AlertEngineFree — orquestra pre-alerta do modelo FREE."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.alert_engine_free import AlertEngineFree
from src.core.stats_engine_v3 import EvaluationV3, LineEvalV3


def _line(line, rate):
    return LineEvalV3(
        line=line,
        threshold=0,
        hits=int(rate * 20),
        n=20,
        rate=rate,
        recent_hits=6,
        recent_n=7,
        qualified=True,
    )


def _engine(ev, exists=False):
    stats = MagicMock()
    stats.evaluate = AsyncMock(return_value=ev)
    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=exists)
    created = MagicMock()
    created.id = 1
    repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()
    notifier = MagicMock()
    notifier.send_watch_free = AsyncMock(return_value=88)
    return AlertEngineFree(stats, repo, notifier), repo, notifier


def _m():
    g2 = MagicMock()
    g2.id = 5
    g2.player_home = "Sena"
    g2.player_away = "Bosko"
    g1 = MagicMock()
    g1.player_home = "Bosko"
    g1.player_away = "Sena"
    g1.score_home = 3
    g1.score_away = 1
    return g2, g1


@pytest.mark.asyncio
async def test_escolhe_linha_de_maior_taxa():
    """Deve escolher a linha com maior taxa e enviar alerta."""
    ev = EvaluationV3(
        should_alert=True, lines=[_line("over25", 0.70), _line("over15", 0.85)], n_h2h=20
    )
    eng, repo, notifier = _engine(ev)
    g2, g1 = _m()
    line = await eng.prealert(g2, g1, "Sena", "Bosko", "19:43")
    assert line == "over15"  # maior rate
    assert repo.create.await_args.kwargs["line"] == "over15"
    notifier.send_watch_free.assert_awaited_once()


@pytest.mark.asyncio
async def test_nao_alerta_sem_should_alert():
    """Não deve alertar se should_alert=False."""
    ev = EvaluationV3(should_alert=False, reason="x")
    eng, repo, notifier = _engine(ev)
    g2, g1 = _m()
    assert await eng.prealert(g2, g1, "Sena", "Bosko", "19:43") is None
    notifier.send_watch_free.assert_not_awaited()


@pytest.mark.asyncio
async def test_dedup_por_match():
    """Deve retornar None se alert já existe para este match."""
    ev = EvaluationV3(should_alert=True, lines=[_line("over15", 0.85)], n_h2h=20)
    eng, repo, notifier = _engine(ev, exists=True)
    g2, g1 = _m()
    assert await eng.prealert(g2, g1, "Sena", "Bosko", "19:43") is None
    repo.create.assert_not_awaited()
