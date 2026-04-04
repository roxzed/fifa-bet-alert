"""Matches game 1 (ida) with game 2 (volta) based on player names and time window.

Correções aplicadas (auditoria 2025-03-25):
- Janela temporal agora é baseada no ended_at do G1 (não mais datetime.now())
- Busca também em eventos recentemente encerrados (ended), não só upcoming/inplay
- Matching de times usa contenção de substring para tolerar variações de nome
- Retry pending usa a mesma lógica corrigida
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger


def _utc(dt: Any) -> datetime | None:
    """Ensure datetime is timezone-aware UTC."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _normalize(name: str) -> str:
    """Normalize a name for fuzzy comparison: lowercase, strip, remove common suffixes."""
    n = name.lower().strip()
    # Remove common suffixes that differ between APIs
    for suffix in (" (esports)", " (esoccer)", " fc", " cf", " sc"):
        n = n.replace(suffix, "")
    return n.strip()


def _teams_match_fuzzy(g1_teams: set[str], event_teams: set[str]) -> bool:
    """Check if two sets of team names match, tolerating minor differences.

    Uses bidirectional substring containment: 'Real Madrid' matches 'Real Madrid CF'.
    """
    if not g1_teams or not event_teams:
        return False

    # Exact match (fast path)
    if g1_teams == event_teams:
        return True

    # Normalized exact match
    g1_norm = {_normalize(t) for t in g1_teams}
    ev_norm = {_normalize(t) for t in event_teams}
    if g1_norm == ev_norm:
        return True

    # Substring containment: each G1 team must be contained in (or contain) some event team
    matched_g1 = 0
    for g1t in g1_norm:
        for evt in ev_norm:
            if g1t in evt or evt in g1t:
                matched_g1 += 1
                break
    return matched_g1 == len(g1_norm)


class PairMatcher:
    """
    Finds the return match for a completed game.
    The return match has the same two players, ~55-60 min after game 1.
    """

    def __init__(self, api_client, match_repo, odds_monitor, session_factory=None):
        self.api = api_client
        self.matches = match_repo
        self.odds_monitor = odds_monitor
        self._session_factory = session_factory
        # Pending pairs: game1_match_id -> {loser, winner, players, league_id, attempts, ...}
        self._pending: dict[int, dict] = {}

    async def find_and_link_return_match(
        self,
        game1_match,
        loser: str,
        winner: str,
        player_home: str,
        player_away: str,
        league_id: str,
        loser_goals_g1: int = 0,
        match_repo=None,
    ) -> bool:
        """
        Try to find the return match in upcoming, inplay, AND recently ended events.
        Uses the G1 ended_at as time anchor instead of datetime.now().
        If not found yet, add to pending queue for retry.
        Returns True if found and linked.

        match_repo: sessão isolada para chamadas do retry_pending (APScheduler),
                    evitando colisão com a sessão do game_watcher.
        """
        _matches = match_repo or self.matches
        players = {player_home.lower().strip(), player_away.lower().strip()}
        # Times do G1 para verificação (97% mantêm os mesmos times)
        g1_teams = set()
        if game1_match.team_home:
            g1_teams.add(game1_match.team_home.lower().strip())
        if game1_match.team_away:
            g1_teams.add(game1_match.team_away.lower().strip())

        # ---- Coletar candidatos de TODAS as fontes ----
        candidates = []

        # 1) Upcoming events
        try:
            upcoming = await self.api.get_upcoming_events(league_id)
            candidates.extend(upcoming)
        except Exception as e:
            logger.error(f"Failed to fetch upcoming events: {e}")

        # 2) Inplay events — o jogo de volta pode já ter começado
        try:
            inplay = await self.api.get_inplay_events(league_id)
            candidates.extend(inplay)
        except Exception as e:
            logger.warning(f"Could not fetch inplay events (continuing without): {e}")

        # 3) Recently ended events — o jogo de volta pode já ter terminado
        #    (API demorou a atualizar, ou processamento atrasou)
        try:
            from datetime import date
            today = date.today().strftime("%Y%m%d")
            ended = await self.api.get_ended_events(league_id, day=today, use_v2=True)
            candidates.extend(ended)
        except Exception as e:
            logger.warning(f"Could not fetch ended events for pairing (continuing without): {e}")

        if not candidates:
            self._add_pending(game1_match, loser, winner, players, league_id, loser_goals_g1)
            return False

        # ---- Janela temporal baseada no ended_at do G1 ----
        # Dados históricos mostram: 94.7% dos jogos de volta ocorrem ~60 min após o início do G1
        # Range real: 45-74 min do início. Usamos ended_at + margem generosa.
        g1_ended = _utc(game1_match.ended_at)
        g1_started = _utc(game1_match.started_at)

        if g1_ended:
            # G1 já terminou: janela de 30 a 80 min após o fim do G1
            # (jogo dura ~8 min, então ended_at ≈ started_at + 8 min)
            anchor = g1_ended
            min_time = anchor + timedelta(minutes=30)
            max_time = anchor + timedelta(minutes=80)
        elif g1_started:
            # Fallback: usar started_at + janela histórica
            anchor = g1_started
            min_time = anchor + timedelta(minutes=45)
            max_time = anchor + timedelta(minutes=80)
        else:
            # Último fallback: usar now() (comportamento antigo)
            anchor = datetime.now(timezone.utc)
            min_time = anchor - timedelta(minutes=5)
            max_time = anchor + timedelta(minutes=80)
            logger.warning(
                f"Game1 {game1_match.id} has no ended_at or started_at, "
                f"falling back to now()-based window"
            )

        # ---- Deduplicate candidates by event ID ----
        seen_ids: set[str] = set()
        unique_candidates = []
        for event in candidates:
            if event.id not in seen_ids:
                seen_ids.add(event.id)
                unique_candidates.append(event)

        # ---- Excluir o próprio G1 dos candidatos ----
        g1_api_id = game1_match.api_event_id
        unique_candidates = [e for e in unique_candidates if e.id != g1_api_id]

        # ---- Scoring dos candidatos ----
        best_match = None
        best_score = -1

        for event in unique_candidates:
            event_players = {event.home_name.lower().strip(), event.away_name.lower().strip()}
            if event_players != players:
                continue

            event_time = _utc(event.scheduled_time)
            if event_time is None or not (min_time <= event_time <= max_time):
                continue

            # OBRIGATORIO: times devem ser os mesmos do G1 (ida e volta = mesmos times)
            # Sem match de times = par errado (outro confronto dos mesmos jogadores)
            if g1_teams and event.home_team and event.away_team:
                event_teams = {event.home_team.lower().strip(), event.away_team.lower().strip()}
                if not _teams_match_fuzzy(g1_teams, event_teams):
                    continue  # times diferentes = NAO eh a volta deste G1
                score = 10
            else:
                # Sem dados de time: aceitar com score baixo (fallback)
                score = 1

            # Bonus: mais perto do tempo esperado (~60 min) = melhor
            if event_time and g1_started:
                delta_min = (event_time - g1_started).total_seconds() / 60
                # Ideal é ~60 min. Penalizar desvios.
                time_penalty = abs(delta_min - 60) / 60  # 0 = perfeito, 1 = 60 min off
                score += max(0, 5 - time_penalty * 5)  # até +5 pontos por proximidade temporal

            if score > best_score:
                best_score = score
                best_match = event

        if best_match is None:
            # Not found yet - add to pending
            self._add_pending(game1_match, loser, winner, players, league_id, loser_goals_g1)
            return False

        event = best_match
        event_time = _utc(event.scheduled_time)

        # Found the return match!
        teams_match = "mesmos times" if best_score >= 10 else "times diferentes/parcial"
        logger.info(
            f"Return match found: {event.home_name} vs {event.away_name} "
            f"at {event_time} ({teams_match}, score={best_score:.1f})"
        )

        # Save return match to DB
        return_match = await _matches.get_by_api_event_id(event.id)
        if return_match is None:
            # Postgres TIMESTAMP WITHOUT TIME ZONE: remover tzinfo
            event_time_naive = event_time.replace(tzinfo=None) if event_time else None
            return_match = await _matches.create(
                api_event_id=event.id,
                league=event.league_name,
                player_home=event.home_name,
                player_away=event.away_name,
                team_home=event.home_team,
                team_away=event.away_team,
                status=event.status if hasattr(event, "status") else "scheduled",
                score_home=event.home_score if hasattr(event, "home_score") else None,
                score_away=event.away_score if hasattr(event, "away_score") else None,
                started_at=event_time_naive,
                is_return_match=True,
                day_of_week=event_time.weekday() if event_time else None,
                hour_of_day=event_time.hour if event_time else None,
            )
        # link_pair already sets is_return_match=True on return_match

        # Link the pair
        time_between = None
        ended_utc = _utc(game1_match.ended_at)
        event_utc = _utc(event_time)
        if ended_utc and event_utc:
            delta = event_utc - ended_utc
            time_between = int(delta.total_seconds() / 60)

        await _matches.link_pair(
            match_id=game1_match.id,
            return_match_id=return_match.id,
            time_between=time_between,
        )

        logger.info(
            f"Pair linked: game1={game1_match.id} -> game2={return_match.id} "
            f"(loser={loser}, {time_between} min apart)"
        )

        # Remove from pending if it was there
        self._pending.pop(game1_match.id, None)

        # Start monitoring odds for the return match
        await self.odds_monitor.start_monitoring(
            return_match=return_match,
            game1_match=game1_match,
            loser=loser,
            winner=winner,
            loser_goals_g1=loser_goals_g1,
        )

        return True

    def _add_pending(self, game1_match, loser, winner, players, league_id, loser_goals_g1: int = 0) -> None:
        """Add to pending queue for retry on next poll cycle."""
        if game1_match.id not in self._pending:
            # PROBLEMA 13 fix: guardar players como tupla ordenada para manter
            # a ordem determinística no retry (set não garante ordem)
            players_tuple = tuple(sorted(players))
            # Snapshot de valores primitivos para evitar MissingGreenlet:
            # session.rollback() expira todos os objetos ORM da sessão compartilhada,
            # tornando lazy-load impossível no contexto do APScheduler.
            match_snapshot = types.SimpleNamespace(
                id=game1_match.id,
                api_event_id=getattr(game1_match, "api_event_id", None),
                team_home=game1_match.team_home,
                team_away=game1_match.team_away,
                ended_at=game1_match.ended_at,
                started_at=game1_match.started_at,
                score_home=game1_match.score_home,
                score_away=game1_match.score_away,
                player_home=game1_match.player_home,
                player_away=game1_match.player_away,
            )
            self._pending[game1_match.id] = {
                "match": match_snapshot,
                "loser": loser,
                "winner": winner,
                "players": players_tuple,
                "league_id": league_id,
                "loser_goals_g1": loser_goals_g1,
                "attempts": 0,
                "added_at": datetime.now(timezone.utc),
            }
            logger.debug(f"Added game1={game1_match.id} to pending pair queue")

    async def retry_pending(self) -> None:
        """Retry finding return matches for pending games. Called periodically by APScheduler.

        OTIMIZAÇÃO: busca candidatos UMA VEZ (3 API calls) e reutiliza para todos
        os pares pendentes, em vez de 3 calls POR PAR.
        Antes: 162 pares × 6 calls = 972 calls/ciclo
        Agora: 6 calls fixos + 0 por par = 6 calls/ciclo
        """
        if not self._pending:
            return

        logger.info(f"Retrying {len(self._pending)} pending pairs")

        # Session-per-method: repos auto-comitam, não precisa de sessão isolada

        # ── OTIMIZAÇÃO: buscar candidatos UMA VEZ para todos os pares ──
        first_info = next(iter(self._pending.values()))
        league_id = first_info["league_id"]

        candidates = []
        try:
            upcoming = await self.api.get_upcoming_events(league_id)
            candidates.extend(upcoming)
        except Exception as e:
            logger.error(f"Failed to fetch upcoming events: {e}")
        try:
            inplay = await self.api.get_inplay_events(league_id)
            candidates.extend(inplay)
        except Exception as e:
            logger.warning(f"Could not fetch inplay events: {e}")
        try:
            from datetime import date
            today = date.today().strftime("%Y%m%d")
            ended = await self.api.get_ended_events(league_id, day=today, use_v2=True)
            candidates.extend(ended)
        except Exception as e:
            logger.warning(f"Could not fetch ended events: {e}")

        # Dedup candidatos
        seen_ids: set[str] = set()
        unique_candidates = []
        for event in candidates:
            if event.id not in seen_ids:
                seen_ids.add(event.id)
                unique_candidates.append(event)

        logger.debug(f"Retry: {len(unique_candidates)} unique candidates fetched")

        # ── Iterar sobre os pares pendentes usando os candidatos já buscados ──
        expired = []
        resolved = []

        for match_id, info in list(self._pending.items()):
            info["attempts"] += 1

            age = datetime.now(timezone.utc) - info["added_at"]
            if age > timedelta(minutes=120):
                logger.warning(
                    f"Giving up on finding return match for game1={match_id} after {age}"
                )
                expired.append(match_id)
                continue

            found = await self._match_from_candidates(
                game1_match=info["match"],
                loser=info["loser"],
                winner=info["winner"],
                players=set(info["players"]),
                loser_goals_g1=info.get("loser_goals_g1", 0),
                candidates=unique_candidates,
                match_repo=self.matches,
            )
            if found:
                resolved.append(match_id)
                logger.info(f"Pending pair resolved for game1={match_id}")

        for match_id in expired + resolved:
            self._pending.pop(match_id, None)

    async def _match_from_candidates(
        self, game1_match, loser: str, winner: str, players: set[str],
        loser_goals_g1: int, candidates: list, match_repo,
    ) -> bool:
        """Try to match a G1 with a G2 from pre-fetched candidates.

        Same matching logic as find_and_link_return_match but without API calls.
        """
        g1_teams = set()
        if game1_match.team_home:
            g1_teams.add(game1_match.team_home.lower().strip())
        if game1_match.team_away:
            g1_teams.add(game1_match.team_away.lower().strip())

        g1_ended = _utc(game1_match.ended_at)
        g1_started = _utc(game1_match.started_at)

        if g1_ended:
            anchor = g1_ended
            min_time = anchor + timedelta(minutes=30)
            max_time = anchor + timedelta(minutes=80)
        elif g1_started:
            anchor = g1_started
            min_time = anchor + timedelta(minutes=45)
            max_time = anchor + timedelta(minutes=80)
        else:
            anchor = datetime.now(timezone.utc)
            min_time = anchor - timedelta(minutes=5)
            max_time = anchor + timedelta(minutes=80)

        g1_api_id = game1_match.api_event_id if hasattr(game1_match, 'api_event_id') else None

        best_match = None
        best_score = -1

        for event in candidates:
            if g1_api_id and event.id == g1_api_id:
                continue

            event_players = {event.home_name.lower().strip(), event.away_name.lower().strip()}
            if event_players != players:
                continue

            event_time = _utc(event.scheduled_time)
            if event_time is None or not (min_time <= event_time <= max_time):
                continue

            # OBRIGATORIO: times devem ser os mesmos do G1
            if g1_teams and event.home_team and event.away_team:
                event_teams = {event.home_team.lower().strip(), event.away_team.lower().strip()}
                if not _teams_match_fuzzy(g1_teams, event_teams):
                    continue  # times diferentes = NAO eh a volta deste G1
                score = 10
            else:
                score = 1

            if event_time and g1_started:
                delta_min = (event_time - g1_started).total_seconds() / 60
                time_penalty = abs(delta_min - 60) / 60
                score += max(0, 5 - time_penalty * 5)

            if score > best_score:
                best_score = score
                best_match = event

        if best_match is None:
            return False

        event = best_match
        event_time = _utc(event.scheduled_time)

        teams_match = "mesmos times" if best_score >= 10 else "times diferentes"
        logger.info(
            f"Return match found: {event.home_name} vs {event.away_name} "
            f"at {event_time} ({teams_match}, score={best_score:.1f})"
        )

        return_match = await match_repo.get_by_api_event_id(event.id)
        if return_match is None:
            event_time_naive = event_time.replace(tzinfo=None) if event_time else None
            return_match = await match_repo.create(
                api_event_id=event.id,
                league=event.league_name,
                player_home=event.home_name,
                player_away=event.away_name,
                team_home=event.home_team,
                team_away=event.away_team,
                status=event.status if hasattr(event, "status") else "scheduled",
                score_home=event.home_score if hasattr(event, "home_score") else None,
                score_away=event.away_score if hasattr(event, "away_score") else None,
                started_at=event_time_naive,
                is_return_match=True,
                day_of_week=event_time.weekday() if event_time else None,
                hour_of_day=event_time.hour if event_time else None,
            )
        # link_pair sets is_return_match=True

        time_between = None
        ended_utc = _utc(game1_match.ended_at)
        event_utc = _utc(event_time)
        if ended_utc and event_utc:
            delta = event_utc - ended_utc
            time_between = int(delta.total_seconds() / 60)

        await match_repo.link_pair(
            match_id=game1_match.id,
            return_match_id=return_match.id,
            time_between=time_between,
        )

        logger.info(
            f"Pair linked: game1={game1_match.id} -> game2={return_match.id} "
            f"(loser={loser}, {time_between} min apart)"
        )

        await self.odds_monitor.start_monitoring(
            return_match=return_match,
            game1_match=game1_match,
            loser=loser,
            winner=winner,
            loser_goals_g1=loser_goals_g1,
        )

        return True

    async def recover_pending_from_db(self, match_repo, league_id: str) -> int:
        """Recupera G1 recentes sem G2 pareado ao reiniciar o sistema.

        Busca matches das ultimas 2h que sao G1 (is_return_match=False),
        nao tem par (pair_match_id IS NULL), e nao foram empate.
        Re-adiciona na fila _pending para o retry_pending encontrar o G2.
        """
        from sqlalchemy import select, and_
        from src.db.models import Match

        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        cutoff_naive = cutoff.replace(tzinfo=None)

        try:
            async with match_repo._session() as session:
                stmt = select(Match).where(and_(
                    Match.is_return_match == False,  # noqa: E712
                    Match.pair_match_id.is_(None),
                    Match.ended_at >= cutoff_naive,
                    Match.score_home.is_not(None),
                    Match.score_away.is_not(None),
                ))
                result = await session.execute(stmt)
                matches = result.scalars().all()

            recovered = 0
            for m in matches:
                # Pular empates
                if m.score_home == m.score_away:
                    continue
                # Pular se ja esta pendente
                if m.id in self._pending:
                    continue

                if m.score_home < m.score_away:
                    loser = m.player_home
                    winner = m.player_away
                    loser_goals_g1 = m.score_home
                else:
                    loser = m.player_away
                    winner = m.player_home
                    loser_goals_g1 = m.score_away

                players = {loser.lower().strip(), winner.lower().strip()}
                self._add_pending(m, loser, winner, players, league_id, loser_goals_g1)
                recovered += 1

            if recovered > 0:
                logger.info(f"Recovered {recovered} pending pairs from DB after restart")
            return recovered
        except Exception as e:
            logger.error(f"Failed to recover pending pairs from DB: {e}")
            return 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)
