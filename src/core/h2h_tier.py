"""H2H tier classification — letra mostrada no alert do Telegram.

Tier baseado em ROI historico do (player, line, opponent), com
sample minimo de 3 alertas. Faixas decididas pelo owner em 2026-05-05,
threshold do D ajustado 2026-05-18, F adicionada 2026-06-15:

  S  ROI >= 50%
  A  30% <= ROI < 50%
  B  15% <= ROI < 30%
  C  2%  <= ROI < 15%
  D  0%  <= ROI < 2%
  F  ROI < 0% com n>=3 (tem dado, e o dado eh ruim) [2026-06-15]
  ?  n < 3 (sample insuficiente — combo novo, sem dado pra avaliar)
  E  state em SHADOW/PERMANENT (alert nao chega no Telegram)

2026-06-15: separamos "sem dado" (?) de "dado ruim" (F). Antes ambos
mostravam ?, gerando confusao no alert do Telegram. Agora o usuario
ve claramente: ? = combo novo (acumular sample), F = ja sabemos que
historicamente perde.

2026-06-15 v2: FALLBACK HISTORICO DE JOGOS. Quando n_alerts < 3 mas
existem >=3 G2 historicos do combo (player perdeu G1 contra opp),
estimar tier baseado no hit rate dos jogos + odds tipica da linha.
So promove pra S/A/B (ROI estimado >= 15%). Resto continua "?".

Backtest 60d (772 alerts suppressed): 423 viraram S/A/B com PL real
+49.80u. Combos com historico de jogos forte mas sem alerts agora
"engatam" na primeira tip ao inves de demorar 3 alerts pra acumular.

Janela: alertas desde CUTOFF_UTC (deploy do regime SHADOW v3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select, text

from src.db.models import Alert, AlertV2, Match


# Mesma data-corte do SHADOW v3 (blocked_lines.py:34)
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)
MIN_SAMPLE = 3

# Fallback historico de jogos (2026-06-15):
# Quando combo nao tem >=3 alerts previos, estima tier a partir de hit rate
# em G2 historicos com odds tipica da linha.
MIN_GAMES_FALLBACK = 3
LINE_THRESHOLDS = {"over15": 1, "over25": 2, "over35": 3, "over45": 4}
TYPICAL_ODDS = {"over15": 1.65, "over25": 1.80, "over35": 2.00, "over45": 2.20}
FALLBACK_PROMOTE_TIERS = ("S", "A", "B")  # so libera se historico forte (ROI >=15%)

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
    tier: str          # "S" | "A" | "B" | "C" | "D" | "F" | "E" | "?"
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
        # Sample insuficiente — combo novo, ainda nao deu pra avaliar.
        return H2HTierResult(tier="?", n=n, pl=pl, roi=roi, state=state)
    if roi < 0:
        # 2026-06-15: tier F (negativo com sample suficiente). Antes era ?,
        # mas isso confundia "sem dado" com "dado ruim". Agora ? eh so
        # n<3, F eh ROI<0 com n>=3. Owner aprovou em 2026-06-15.
        return H2HTierResult(tier="F", n=n, pl=pl, roi=roi, state=state)
    for letter, threshold in TIER_THRESHOLDS:
        if roi >= threshold:
            return H2HTierResult(tier=letter, n=n, pl=pl, roi=roi, state=state)
    return H2HTierResult(tier="?", n=n, pl=pl, roi=roi, state=state)


def _classify_fallback_from_hit_rate(
    line: str, n_jogos: int, hit_rate: float, state: str,
) -> H2HTierResult | None:
    """Estima tier a partir de hit rate de jogos historicos e odds tipica.

    So retorna tier nao-None se promovido pra S/A/B (ROI estimado >= 15%).
    Combos com hit rate ruim seguem como "?" (cautela: dado historico de
    jogos eh ESTIMATIVA, alerts reais sao mais precisos quando existem).
    """
    if line not in TYPICAL_ODDS or n_jogos < MIN_GAMES_FALLBACK:
        return None
    typical = TYPICAL_ODDS[line]
    # ROI estimado = hit_rate * (odds - 1) - (1 - hit_rate)
    roi_estimado = (hit_rate * (typical - 1.0) - (1.0 - hit_rate)) * 100.0

    # Mesma escala do classify, mas direto do ROI estimado
    if roi_estimado >= 50.0:
        tier = "S"
    elif roi_estimado >= 30.0:
        tier = "A"
    elif roi_estimado >= 15.0:
        tier = "B"
    else:
        return None  # nao promove (vira "?" no caller)

    if tier not in FALLBACK_PROMOTE_TIERS:
        return None

    # n=n_jogos (sinaliza que veio de jogos, nao alerts)
    # pl convencional: hit_rate*100 (so pra mostrar algo coerente)
    return H2HTierResult(
        tier=tier, n=n_jogos, pl=hit_rate * n_jogos, roi=roi_estimado, state=state,
    )


async def _fetch_h2h_games_hit_rate(
    match_repo, player: str, opponent: str, line: str,
) -> tuple[int, float]:
    """Retorna (n_jogos, hit_rate) de G2 historicos onde player perdeu G1 vs opp.

    Direcional: so conta G2 onde 'player' foi efetivamente o loser de G1.
    Threshold de gols vem da linha (over15 -> goals>1, over25 -> goals>2, etc).
    """
    threshold = LINE_THRESHOLDS.get(line)
    if threshold is None:
        return 0, 0.0

    stmt = text("""
        SELECT g2.score_home AS g2h, g2.score_away AS g2a,
               g2.player_home AS g2_home, g2.player_away AS g2_away,
               g1.score_home AS g1h, g1.score_away AS g1a,
               g1.player_home AS g1_home, g1.player_away AS g1_away
        FROM matches g2
        JOIN matches g1 ON g2.pair_match_id = g1.id
        WHERE g2.is_return_match = TRUE
          AND g2.score_home IS NOT NULL
          AND g1.score_home IS NOT NULL AND g1.score_away IS NOT NULL
          AND g1.score_home != g1.score_away
          AND (
            (g2.player_home = :player AND g2.player_away = :opponent)
            OR (g2.player_home = :opponent AND g2.player_away = :player)
          )
    """)
    result = await match_repo.execute_query(
        stmt, {"player": player, "opponent": opponent}
    )
    rows = result.fetchall()

    n_jogos = 0
    hits = 0
    for r in rows:
        # Identificar loser do G1
        if r.g1h > r.g1a:
            loser_g1 = r.g1_away
        else:
            loser_g1 = r.g1_home
        if loser_g1 != player:
            continue
        # Pegar gols do player no G2
        goals = r.g2h if r.g2_home == player else r.g2a
        if goals is None:
            continue
        n_jogos += 1
        if goals > threshold:
            hits += 1

    hit_rate = hits / n_jogos if n_jogos > 0 else 0.0
    return n_jogos, hit_rate


async def compute_h2h_tier(
    alert_repo,
    blocked_repo,
    player: str,
    line: str,
    opponent: str,
    match_repo=None,
) -> H2HTierResult:
    """Calcula tier H2H consultando o banco.

    1) State SHADOW: prioritario (tier=E).
    2) Senao busca historico do combo desde CUTOFF_UTC e classifica.
    3) Se n_alerts < 3 mas match_repo fornecido: fallback historico de jogos.
       So promove pra S/A/B (ROI estimado >= 15%). Resto fica "?".

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
        n, pl = 0, 0.0

    # SHADOW prioritario (mesmo com fallback)
    if state in ("SHADOW", "PERMANENT"):
        return classify(n, pl, state)

    # Caminho normal: amostra suficiente de alerts
    if n >= MIN_SAMPLE:
        return classify(n, pl, state)

    # Fallback historico de jogos (2026-06-15)
    if match_repo is not None:
        try:
            n_jogos, hit_rate = await _fetch_h2h_games_hit_rate(
                match_repo, player, opponent, line
            )
            fallback = _classify_fallback_from_hit_rate(
                line, n_jogos, hit_rate, state
            )
            if fallback is not None:
                return fallback
        except Exception:
            pass  # silenciar — fallback eh best-effort

    # Default: tier "?" (sem amostra suficiente em lugar nenhum)
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
    match_repo=None,
) -> H2HTierResult:
    """Versao M2 do tier H2H, usando alerts_v2 e blocked_lines_v2.

    AlertV2 ja tem opponent_player — sem join com Match necessario.
    Comportamento identico ao M1: tier E se SHADOW, tier D suprime.

    2026-06-15: mesmo fallback historico de jogos do M1. Se n_alerts_v2 < 3
    e match_repo fornecido, estima tier de hit rate de G2 historicos. So
    promove pra S/A/B.
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
        n, pl = 0, 0.0

    if state in ("SHADOW", "PERMANENT"):
        return classify(n, pl, state)

    if n >= MIN_SAMPLE:
        return classify(n, pl, state)

    # Fallback historico de jogos (mesma logica do M1)
    if match_repo is not None:
        try:
            n_jogos, hit_rate = await _fetch_h2h_games_hit_rate(
                match_repo, player, opponent, line
            )
            fallback = _classify_fallback_from_hit_rate(
                line, n_jogos, hit_rate, state
            )
            if fallback is not None:
                return fallback
        except Exception:
            pass

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
