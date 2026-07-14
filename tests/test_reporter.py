"""Testes do Reporter — relatório diário M3 (send_daily_report_v3).

Leves: só mocks, sem DB. O Reporter recebe repos/notifier via construtor
(kwargs, mesma assinatura real de src/core/reporter.py).
"""

from unittest.mock import AsyncMock, MagicMock

from src.core.reporter import Reporter


def _make_reporter(alert_v3_repo=None) -> Reporter:
    notifier = MagicMock()
    notifier.send_message_v3_raw = AsyncMock(return_value=99)
    return Reporter(
        alert_repo=MagicMock(),
        player_repo=MagicMock(),
        method_stats_repo=MagicMock(),
        notifier=notifier,
        alert_v3_repo=alert_v3_repo,
    )


def _alert(hit: bool, profit_flat: float) -> MagicMock:
    a = MagicMock()
    a.hit = hit
    a.profit_flat = profit_flat
    return a


async def test_send_daily_report_v3_noop_sem_repo():
    reporter = _make_reporter(alert_v3_repo=None)

    await reporter.send_daily_report_v3()

    reporter.notifier.send_message_v3_raw.assert_not_awaited()


async def test_send_daily_report_v3_noop_sem_alertas_validados():
    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=[])
    reporter = _make_reporter(alert_v3_repo=repo)

    await reporter.send_daily_report_v3()

    repo.get_validated_since.assert_awaited_once()
    reporter.notifier.send_message_v3_raw.assert_not_awaited()


async def test_send_daily_report_v3_agrega_greens_reds_e_profit():
    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=[
        _alert(hit=True, profit_flat=0.85),
        _alert(hit=True, profit_flat=0.62),
        _alert(hit=False, profit_flat=-1.0),
    ])
    reporter = _make_reporter(alert_v3_repo=repo)

    await reporter.send_daily_report_v3()

    reporter.notifier.send_message_v3_raw.assert_awaited_once()
    text = reporter.notifier.send_message_v3_raw.await_args.args[0]
    assert "✅ 2" in text
    assert "❌ 1" in text
    assert "🟢" in text  # profit >= 0
    assert "+0.47" in text  # 0.85 + 0.62 - 1.0
    assert "3 apostas" in text
