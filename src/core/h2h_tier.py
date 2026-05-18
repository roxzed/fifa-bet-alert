"""H2H tier classification — letra mostrada no alert do Telegram.

Tier baseado em ROI historico do (player, line, opponent), com
sample minimo de 3 alertas. Faixas decididas pelo owner em 2026-05-05,
threshold do D ajustado 2026-05-18 (backtest mostrou que 2-5% eram
quase todos GREEN):

  S  ROI >= 50%
  A  30% <= ROI < 50%
  B  15% <= ROI < 30%
  C  2%  <= ROI < 15%
  D  0%  <= ROI < 2%
  ?  n < 3 OU ROI < 0% e nao em SHADOW (sem letra util a mostrar)
  E  state em SHADOW/PERMANENT (alert nao chega no Telegram)

Janela: alertas desde CUTOFF_UTC (deploy do regime SHADOW v3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select

from src.db.models import Alert, AlertV2, Match


# Mesma data-corte do SHADOW v3 (blocked_lines.py:34)
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)
MIN_SAMPLE = 3

# Faixas de ROI (%). Ordem: maior threshold primeiro.
# C boundary baixou de 5.0 -> 2.0 em 2026-05-18: backtest mostrou que
# alertas D classificados como 2-5% deram ROI +132% (todos GREEN), eram
# sobre-filtrados pela regra antiga.
TIER_THRESHOLDS: list[tuple[str, float]] = [
    ("S", 50.0),
    ("A", 30.0),
    ("B", 15.0),
    ("C", 2.0),
    ("D", 0.0),
]


@dataclass
class H2HTierResult:
    tier: str          # "S" | "A" | "B" | "C" | "D" | "E" | "?"
    n: int
    pl: float
    roi: float
    state: str         # "ACTIVE" | "SHADOW" | "PERMANENT"


def classify(n: int, pl: float, state: str) -> H2HTierResult:
    """Aplica regras do tier dado sample, PL e state SHADOW."""
    roi = (pl / n * 100) if n else 0.0
    if state in ("SHADOW", "PERMANENT"):
        return H2HTierResult(tier="E", n=n, pl=pl, roi=roi, state=state)
    if n < MIN_SAMPLE:
        return H2HTierResult(tier="?", n=n, pl=pl, roi=roi, state=state)
    if roi < 0:
        # Negativo mas ainda ACTIVE (raro — prestes a virar E pelo SHADOW).
        # Owner: nao mostrar letra para negativo.
        return H2HTierResult(tier="?", n=n, pl=pl, roi=roi, state=state)
    for letter, threshold in TIER_THRESHOLDS:
        if roi >= threshold:
            return H2HTierResult(tier=letter, n=n, pl=pl, roi=roi, state=state)
    return H2HTierResult(tier="?", n=n, pl=pl, roi=roi, state=state)


async def compute_h2h_tier(
    alert_repo,
    blocked_repo,
    player: str,
    line: str,
    opponent: str,
) -> H2HTierResult:
    """Calcula tier H2H consultando o banco.

    1) State SHADOW: prioritario (tier=E).
    2) Senao busca historico do combo desde CUTOFF_UTC e classifica.

    Falhas em qualquer etapa: retorna tier=? (best-effort, nao quebra alerta).
    """
    state = "ACTIVE"
    if blocked_repo is not None:
        try:
            entry = await blocked_repo.get(player, line, opponent)
            if entry:
                state = entry.state
        except Exception:
            state = "ACTIVE"

    try:
        n, pl = await _fetch_h2h_pl(alert_repo, player, line, opponent)
    except Exception:
        return H2HTierResult(tier="?", n=0, pl=0.0, roi=0.0, state=state)

    return classify(n, pl, state)


async def _fetch_h2h_pl(
    alert_repo,
    player: str,
    line: str,
    opponent: str,
) -> tuple[int, float]:
    """Soma profit_flat e conta alertas M1 pro combo (player, line, opp).

    Considera tanto envs quanto sups (todos com profit_flat populado).
    Opponent eh o OUTRO jogador no Match.
    """
    stmt = (
        select(
            func.coalesce(func.sum(Alert.profit_flat), 0.0).label("pl"),
            func.count(Alert.id).label("n"),
        )
        .join(Match, Alert.match_id == Match.id)
        .where(and_(
            Alert.losing_player == player,
            Alert.best_line == line,
            Alert.profit_flat.is_not(None),
            Alert.sent_at >= CUTOFF_UTC,
            (
                ((Match.player_home == player) & (Match.player_away == opponent))
                | ((Match.player_away == player) & (Match.player_home == opponent))
            ),
        ))
    )
    result = await alert_repo.execute_query(stmt)
    row = result.one()
    return int(row.n or 0), float(row.pl or 0.0)


async def compute_h2h_tier_v2(
    alert_v2_repo,
    blocked_v2_repo,
    player: str,
    line: str,
    opponent: str,
) -> H2HTierResult:
    """Versao M2 do tier H2H, usando alerts_v2 e blocked_lines_v2.

    AlertV2 ja tem opponent_player — sem join com Match necessario.
    Comportamento identico ao M1: tier E se SHADOW, tier D suprime.
    """
    state = "ACTIVE"
    if blocked_v2_repo is not None:
        try:
            entry = await blocked_v2_repo.get(player, line, opponent)
            if entry:
                state = entry.state
        except Exception:
            state = "ACTIVE"

    try:
        n, pl = await _fetch_h2h_pl_v2(alert_v2_repo, player, line, opponent)
    except Exception:
        return H2HTierResult(tier="?", n=0, pl=0.0, roi=0.0, state=state)

    return classify(n, pl, state)


async def _fetch_h2h_pl_v2(
    alert_v2_repo,
    player: str,
    line: str,
    opponent: str,
) -> tuple[int, float]:
    """Soma profit_flat e conta alertas M2 pro combo (player, line, opp).

    AlertV2 tem opponent_player direto — sem join com Match.
    """
    stmt = (
        select(
            func.coalesce(func.sum(AlertV2.profit_flat), 0.0).label("pl"),
            func.count(AlertV2.id).label("n"),
        )
        .where(and_(
            AlertV2.losing_player == player,
            AlertV2.best_line == line,
            AlertV2.opponent_player == opponent,
            AlertV2.profit_flat.is_not(None),
            AlertV2.sent_at >= CUTOFF_UTC,
        ))
    )
    result = await alert_v2_repo.execute_query(stmt)
    row = result.one()
    return int(row.n or 0), float(row.pl or 0.0)
