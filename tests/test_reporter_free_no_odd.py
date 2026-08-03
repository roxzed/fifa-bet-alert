"""Reporter FREE (send_daily_report_free) no modo sem odd.

FIX 1 (Important, review final feat/modo-sem-odd): no modo sem odd os
AlertFree green tem entry_odd=None (nao ha odd). O metodo antigo fazia
aritmetica (`a.entry_odd - 1.0`) e formatacao (`{a.entry_odd:.2f}`) que
estouravam TypeError com None. Correcao: quando entry_odd is None, nao
calcula P/L em unidades nem formata odd — mostra so a contagem GREEN/RED.
Com odd presente (flag bet365_live_odds_enabled=True), o relatorio deve
ficar identico ao de antes (reversibilidade).
"""

from unittest.mock import AsyncMock, MagicMock

from src.core.reporter import Reporter


def _make_reporter(alert_free_repo) -> Reporter:
    notifier = MagicMock()
    notifier.send_free_raw = AsyncMock(return_value=1)
    return Reporter(
        alert_repo=MagicMock(),
        player_repo=MagicMock(),
        method_stats_repo=MagicMock(),
        notifier=notifier,
        alert_free_repo=alert_free_repo,
    )


def _free_alert(status: str, entry_odd, losing_player="Sena", line="over15") -> MagicMock:
    a = MagicMock()
    a.status = status
    a.entry_odd = entry_odd
    a.losing_player = losing_player
    a.line = line
    return a


async def test_send_daily_report_free_noop_sem_repo():
    reporter = _make_reporter(alert_free_repo=None)

    await reporter.send_daily_report_free()

    # sem repo configurado, notifier nem existe pra assert direto; so
    # confirmamos que nao explode (NO-OP silencioso).


async def test_send_daily_report_free_noop_sem_validadas():
    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=[])
    reporter = _make_reporter(alert_free_repo=repo)

    await reporter.send_daily_report_free()

    reporter.notifier.send_free_raw.assert_not_awaited()


async def test_send_daily_report_free_sem_odd_nao_crasha(monkeypatch):
    """Modo sem odd: entry_odd None em green e red — nao pode dar TypeError,
    mensagem deve trazer as contagens GREEN/RED e nao conter odd/None."""
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=[
        _free_alert("green", None, losing_player="Sena", line="over15"),
        _free_alert("red", None, losing_player="Wboy", line="over25"),
    ])
    reporter = _make_reporter(alert_free_repo=repo)

    # nao deve levantar TypeError
    await reporter.send_daily_report_free()

    reporter.notifier.send_free_raw.assert_awaited_once()
    text = reporter.notifier.send_free_raw.await_args.args[0]

    assert "1 GREEN" in text
    assert "1 RED" in text
    assert "None" not in text
    assert "@" not in text  # sem odd por tip
    assert "Saldo" not in text  # sem odd -> sem P/L em unidades


async def test_send_daily_report_free_com_odd_mantem_legado(monkeypatch):
    """Reversibilidade: flag True (modo com odd) mantem P/L e "@ odd" por tip,
    identico ao comportamento anterior a este fix."""
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)

    repo = MagicMock()
    repo.get_validated_since = AsyncMock(return_value=[
        _free_alert("green", 1.80, losing_player="Sena", line="over15"),
        _free_alert("red", 1.90, losing_player="Wboy", line="over25"),
    ])
    reporter = _make_reporter(alert_free_repo=repo)

    await reporter.send_daily_report_free()

    reporter.notifier.send_free_raw.assert_awaited_once()
    text = reporter.notifier.send_free_raw.await_args.args[0]

    assert "1 GREEN" in text
    assert "1 RED" in text
    assert "@ 1.80" in text
    assert "Saldo: -0.20u" in text  # (1.80 - 1.0) - 1.0 = -0.20
    assert "None" not in text
