"""Monitors games in real-time: detects ended games, registers results, triggers pair matching.

Correções aplicadas (auditoria 2025-03-25):
- BUG 7: update_result agora recebe status como parâmetro explícito via ended_at
- PROBLEMA 8: Commits manuais mantidos mas com rollback defensivo por ciclo
- PROBLEMA 11: _processed_events usa OrderedDict para manter ordem de inserção
- PROBLEMA 12: Prune agora é determinístico (mantém os mais recentes)

Melhorias v2 (2026-03-25):
- MELHORIA 4: Polling adaptativo — intervalo varia de 15s (ativo) a 120s (idle)
  baseado na quantidade de jogos detectados nos últimos ciclos.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger

_BRT = ZoneInfo("America/Sao_Paulo")


class GameWatcher:
    """
    Main loop that polls the API for game updates.
    - Detects ended games and registers results
    - Identifies the losing player
    - Triggers pair matching to find the return game

    MELHORIA 4: Polling adaptativo
    AUDITORIA 2026-03-26: Sessão por operação (não mais shared session)
    """

    # Limites do polling adaptativo
    MIN_INTERVAL: int = 15     # segundos — quando há atividade
    MAX_INTERVAL: int = 120    # segundos — quando está idle
    RAMP_UP_CYCLES: int = 5    # ciclos sem atividade para chegar ao MAX

    # Circuit breaker: para de tentar DB após N falhas consecutivas
    _MAX_DB_FAILURES: int = 5
    _DB_COOLDOWN: int = 60     # segundos de cooldown após circuit breaker abrir

    def __init__(self, api_client, match_repo, player_repo, team_repo, pair_matcher):
        self.api = api_client
        self.matches = match_repo
        self.players = player_repo
        self.teams = team_repo
        self.pair_matcher = pair_matcher
        self._processed_events: OrderedDict[str, bool] = OrderedDict()
        self._running = False
        # Polling adaptativo state
        self._idle_cycles: int = 0
        self._current_interval: int = 30  # começa no padrão
        self._health = None  # HealthMonitor (opcional, injetado após construção)
        # Circuit breaker state
        self._db_consecutive_failures: int = 0

    def _compute_interval(self, found_new: bool) -> int:
        """Compute next poll interval based on activity.

        Returns interval in seconds.
        """
        if found_new:
            self._idle_cycles = 0
            self._current_interval = self.MIN_INTERVAL
        else:
            self._idle_cycles += 1
            if self._idle_cycles >= self.RAMP_UP_CYCLES:
                self._current_interval = self.MAX_INTERVAL
            else:
                # Linear ramp: MIN → MAX over RAMP_UP_CYCLES
                step = (self.MAX_INTERVAL - self.MIN_INTERVAL) / self.RAMP_UP_CYCLES
                self._current_interval = int(self.MIN_INTERVAL + step * self._idle_cycles)

        return self._current_interval

    async def start(self, league_id: str, poll_interval: int = 30) -> None:
        """Start the game watching loop.

        Args:
            league_id: BetsAPI league ID to monitor.
            poll_interval: Initial poll interval (overridden by adaptive logic).
        """
        self._running = True
        self._current_interval = poll_interval
        logger.info(
            f"GameWatcher started for league {league_id}, "
            f"adaptive polling {self.MIN_INTERVAL}s-{self.MAX_INTERVAL}s"
        )

        while self._running:
            found_new = False

            # Circuit breaker: se DB falhou muito, esperar cooldown
            if self._db_consecutive_failures >= self._MAX_DB_FAILURES:
                logger.warning(
                    f"DB circuit breaker OPEN ({self._db_consecutive_failures} failures), "
                    f"cooldown {self._DB_COOLDOWN}s"
                )
                await asyncio.sleep(self._DB_COOLDOWN)
                self._db_consecutive_failures = 0
                # Com session-per-method, não precisa recriar repos
                logger.info("GameWatcher: circuit breaker reset após cooldown")
                continue

            try:
                found_new = await self._poll_cycle(league_id)
                if self._health:
                    self._health.record_api_success()
                self._db_consecutive_failures = 0  # reset on success
            except Exception as e:
                err_str = str(e).lower()
                is_db_error = (
                    "sqlalchemy" in err_str or "asyncpg" in err_str
                    or "operationalerror" in err_str or "connection refused" in err_str
                )

                if is_db_error:
                    self._db_consecutive_failures += 1
                    logger.error(
                        f"GameWatcher DB error ({self._db_consecutive_failures}/{self._MAX_DB_FAILURES}): {e}"
                    )
                    if self._health:
                        self._health.record_db_error()
                else:
                    logger.error(f"GameWatcher poll error: {e}")
                    if self._health:
                        self._health.record_api_failure()

            # Heartbeat para Docker healthcheck
            self._write_heartbeat()

            interval = self._compute_interval(found_new)
            if self._idle_cycles > 0 and self._idle_cycles % 10 == 0:
                logger.debug(
                    f"GameWatcher idle for {self._idle_cycles} cycles, "
                    f"polling every {interval}s"
                )
            await asyncio.sleep(interval)

    @staticmethod
    def _write_heartbeat() -> None:
        """Write heartbeat file for Docker healthcheck."""
        try:
            from pathlib import Path
            Path("data/heartbeat").write_text(str(datetime.now(timezone.utc).isoformat()))
        except Exception as e:
            logger.debug(f"Heartbeat write failed: {e}")

    def stop(self) -> None:
        """Stop the game watching loop."""
        self._running = False
        logger.info("GameWatcher stopped")

    async def _poll_cycle(self, league_id: str) -> bool:
        """Single poll cycle: fetch ended games and process new ones.

        Returns True if any new games were processed.
        """
        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.strftime("%Y%m%d")
        yesterday_utc = (now_utc - timedelta(days=1)).strftime("%Y%m%d")

        ended_today = await self.api.get_ended_events(league_id, day=today_utc, use_v2=True)
        ended_yesterday = await self.api.get_ended_events(league_id, day=yesterday_utc, use_v2=True)
        ended_events = ended_today + ended_yesterday

        new_count = 0
        for event in ended_events:
            if event.id in self._processed_events:
                continue

            try:
                existing = await self.matches.get_by_api_event_id(event.id)
                if existing and existing.status == "ended":
                    self._processed_events[event.id] = True
                    continue

                await self._process_ended_game(event)
                self._processed_events[event.id] = True
                new_count += 1
            except Exception as e:
                # Não travar o ciclo inteiro por causa de 1 evento problemático
                logger.warning(f"Skipping event {event.id} due to error: {e}")
                self._processed_events[event.id] = True

        # Prune determinístico — mantém os mais recentes
        while len(self._processed_events) > 1000:
            self._processed_events.popitem(last=False)

        return new_count > 0

    async def _process_ended_game(self, event) -> None:
        """Process a single ended game: save result and trigger pair matching.

        AUDITORIA: commit isolado por evento para não contaminar outros.
        """
        logger.info(
            f"Game ended: {event.home_name} {event.home_score}-{event.away_score} {event.away_name}"
        )

        # Score check first (sem DB)
        if event.home_score is None or event.away_score is None:
            logger.warning(f"No score for event {event.id}, skipping")
            return

        # Salvar match no DB com commit isolado
        match = await self.matches.get_by_api_event_id(event.id)
        if match is None:
            _st = event.scheduled_time
            if _st and _st.tzinfo is None:
                _st = _st.replace(tzinfo=timezone.utc)
            started_brt = _st.astimezone(_BRT) if _st else None
            started_naive = _st.replace(tzinfo=None) if _st else None
            ended_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            match = await self.matches.create(
                api_event_id=event.id,
                league=event.league_name,
                player_home=event.home_name,
                player_away=event.away_name,
                team_home=event.home_team,
                team_away=event.away_team,
                score_home=event.home_score,
                score_away=event.away_score,
                status="ended",
                started_at=started_naive,
                ended_at=ended_naive,
                day_of_week=started_brt.weekday() if started_brt else None,
                hour_of_day=started_brt.hour if started_brt else None,
            )
        else:
            if match.status != "ended":
                await self.matches.update_result(
                    match_id=match.id,
                    score_home=event.home_score,
                    score_away=event.away_score,
                    ended_at=datetime.now(timezone.utc),
                )

        # Commit imediatamente para isolar este evento
        # Teams e players (best-effort, não trava se falhar)
        try:
            if event.home_team:
                await self.teams.save_match_team(match.id, event.home_name, event.home_team, "home")
            if event.away_team:
                await self.teams.save_match_team(match.id, event.away_name, event.away_team, "away")
            await self.players.get_or_create(event.home_name)
            await self.players.get_or_create(event.away_name)
        except Exception as e:
            logger.debug(f"Teams/players save failed (non-critical): {e}")

        # Atualizar team_stats, matchup_stats e player_team_preferences (best-effort)
        try:
            h_team, a_team = event.home_team, event.away_team
            h_goals, a_goals = event.home_score, event.away_score
            if h_team:
                await self.teams.update_stats(h_team, goals_scored=h_goals, goals_conceded=a_goals)
                await self.teams.update_player_team_preference(event.home_name, h_team, h_goals)
            if a_team:
                await self.teams.update_stats(a_team, goals_scored=a_goals, goals_conceded=h_goals)
                await self.teams.update_player_team_preference(event.away_name, a_team, a_goals)
            if h_team and a_team:
                await self.teams.update_matchup_stats(h_team, a_team, h_goals + a_goals)
        except Exception as e:
            logger.debug(f"Team stats update failed (non-critical): {e}")

        if event.home_score == event.away_score:
            logger.debug(f"Draw game {event.id}, skipping pair matching")
            return

        if event.home_score < event.away_score:
            loser = event.home_name
            winner = event.away_name
            loser_goals_g1 = event.home_score
        else:
            loser = event.away_name
            winner = event.home_name
            loser_goals_g1 = event.away_score

        logger.info(
            f"Loser identified: {loser} com {loser_goals_g1} gols em G1 (will look for return match)"
        )
        if self._health:
            self._health.record_game_processed()

        await self.pair_matcher.find_and_link_return_match(
            game1_match=match,
            loser=loser,
            winner=winner,
            player_home=event.home_name,
            player_away=event.away_name,
            league_id=event.league_id,
            loser_goals_g1=loser_goals_g1,
        )

        # Session-per-method: cada repo method auto-comita
