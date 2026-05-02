"""Monitors odds for return matches via bet365 API — player-specific goals markets.

Correções aplicadas (auditoria 2025-03-25):
- _fetch_loser_odds agora usa fuzzy matching para encontrar o evento na Bet365
- Tolera diferenças de nomenclatura entre BetsAPI e Bet365 (sufixos, espaços, capitalização)
- Fuzzy matching também aplicado ao filtrar odds do perdedor

Melhorias v2 (2026-03-25):
- MELHORIA 4: Polling adaptativo no _monitor_loop:
  * Longe do kickoff (>10 min) → poll a cada 60s
  * Perto do kickoff (3-10 min) → poll a cada 15s
  * Após kickoff (0 a -5.5 min) → poll a cada 10s (máxima urgência)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from loguru import logger


def _normalize_player(name: str) -> str:
    """Normalize a player name for fuzzy comparison."""
    n = name.lower().strip()
    for suffix in (" (esports)", " (esoccer)", " esports", " esoccer"):
        n = n.replace(suffix, "")
    return n.strip()


def _fuzzy_match_players(
    target_players: set[str], candidate_players: set[str], threshold: float = 0.80
) -> bool:
    """Check if two sets of player names match using fuzzy comparison."""
    if target_players == candidate_players:
        return True
    if len(target_players) != len(candidate_players):
        return False

    target_list = sorted(target_players)
    candidate_list = sorted(candidate_players)

    if len(target_list) == 2 and len(candidate_list) == 2:
        s1 = _name_similarity(target_list[0], candidate_list[0]) + _name_similarity(target_list[1], candidate_list[1])
        s2 = _name_similarity(target_list[0], candidate_list[1]) + _name_similarity(target_list[1], candidate_list[0])
        best = max(s1, s2) / 2
        return best >= threshold

    used = set()
    total_sim = 0.0
    for t in target_list:
        best_sim = 0.0
        best_idx = -1
        for i, c in enumerate(candidate_list):
            if i in used:
                continue
            sim = _name_similarity(t, c)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_idx >= 0:
            used.add(best_idx)
            total_sim += best_sim

    avg = total_sim / len(target_list) if target_list else 0
    return avg >= threshold


def _name_similarity(a: str, b: str) -> float:
    """Compute similarity between two normalized names."""
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def _adaptive_poll_interval(minutes_to_kickoff: float | None) -> int:
    """Compute adaptive poll interval based on proximity to kickoff.

    Returns interval in seconds.
    """
    if minutes_to_kickoff is None:
        return 15  # unknown → be aggressive

    if minutes_to_kickoff > 10:
        return 60   # longe → economizar API
    elif minutes_to_kickoff > 3:
        return 15   # perto → ficar atento
    else:
        # Historico:
        # - 10s (original)
        # - 4s (2026-04-23) — cortar latencia 10-13s -> 4-5s
        # - 2s (2026-04-28) — owner aprovou em 2026-04-26, aplicado hoje.
        #   Margem ~50% sobre limite 3600 req/h da Bet365.
        # Rollback: mudar para 4 ou 10.
        return 2


class OddsMonitor:
    """
    For each identified return match, polls bet365 for player goals odds.
    When a line (1.5/2.5/3.5/4.5) has edge, triggers the Alert Engine.

    AUDITORIA 2026-03-26: Max 30 tasks simultâneas + cleanup automático de zombies.
    """

    _MAX_MONITOR_SECONDS: int = 45 * 60  # 45 min máximo por partida
    _MAX_CONCURRENT_TASKS: int = 30      # máximo de tasks simultâneas
    _WATCH_LEAD_SECONDS: int = 90        # enviar watch T-90s antes do kickoff
    _WATCH_AUTO_DELETE_SECONDS: int = 600  # apagar watch 10 min apos envio

    def __init__(self, api_client, odds_repo, alert_engine, match_repo=None, poll_interval: int = 15,
                 alert_engine_v2=None) -> None:
        self.api = api_client
        self.odds_repo = odds_repo
        self.alert_engine = alert_engine
        self.alert_engine_v2 = alert_engine_v2  # Method 2 (opcional)
        self.match_repo = match_repo  # PROBLEMA 9 fix: acesso direto ao repo
        self.poll_interval = poll_interval  # default, overridden by adaptive logic
        self._tasks: dict[int, asyncio.Task] = {}
        self._task_meta: dict[int, dict] = {}  # match_id → {game1_match, loser, ...}
        self._task_started: dict[int, float] = {}  # match_id → monotonic start time
        self._alert_v2_sent: dict[int, bool] = {}  # match_id → True se M2 alerta ja foi enviado
        self._watch_tasks: dict[int, asyncio.Task] = {}  # match_id → watch task

    async def start_monitoring(
        self,
        return_match,
        game1_match,
        loser: str,
        winner: str,
        loser_goals_g1: int = 0,
    ) -> None:
        """Begin monitoring odds for a return match."""
        match_id = return_match.id
        if match_id in self._tasks:
            # Verificar se o novo G1 tem mesmos times que o return match (melhor par)
            existing = self._task_meta.get(match_id, {})
            old_g1 = existing.get("game1_match")
            if old_g1 and return_match.team_home and game1_match.team_home:
                ret_teams = {(return_match.team_home or "").lower(), (return_match.team_away or "").lower()}
                new_teams = {(game1_match.team_home or "").lower(), (game1_match.team_away or "").lower()}
                old_teams = {(getattr(old_g1, "team_home", "") or "").lower(), (getattr(old_g1, "team_away", "") or "").lower()}
                new_match = ret_teams == new_teams
                old_match = ret_teams == old_teams
                if new_match and not old_match:
                    logger.info(
                        f"Replacing G1 for return match {match_id}: "
                        f"old g1={old_g1.id} (wrong teams) -> new g1={game1_match.id} (correct teams)"
                    )
                    # Cancelar task antiga e reiniciar com G1 correto
                    task = self._tasks.pop(match_id, None)
                    self._task_meta.pop(match_id, None)
                    if task and not task.done():
                        task.cancel()
                else:
                    logger.debug(f"Already monitoring return match {match_id}")
                    return
            else:
                logger.debug(f"Already monitoring return match {match_id}")
                return

        # Cleanup zombie tasks (done/cancelled mas ainda no dict)
        self._cleanup_dead_tasks()

        # Cap de tasks simultâneas
        if len(self._tasks) >= self._MAX_CONCURRENT_TASKS:
            logger.warning(
                f"OddsMonitor at capacity ({len(self._tasks)} tasks), "
                f"skipping match {match_id}"
            )
            return

        # Pre-filtro: consultar historico REAL de todas as linhas do jogador
        o15_rate = 0.76
        o25_rate = 0.52
        o35_rate = 0.28
        o45_rate = 0.13
        try:
            # PROBLEMA 9 fix: usar match_repo diretamente em vez de cadeia de objetos
            repo = self.match_repo or self.alert_engine.stats.matches
            matches = await repo.get_return_matches_by_player(loser)

            if len(matches) >= 10:
                o15_c = o25_c = o35_c = o45_c = 0
                for m in matches:
                    goals = m.score_home if m.player_home == loser else m.score_away
                    if goals is None:
                        continue
                    if goals > 1: o15_c += 1
                    if goals > 2: o25_c += 1
                    if goals > 3: o35_c += 1
                    if goals > 4: o45_c += 1
                n = len(matches)
                o15_rate = o15_c / n
                o25_rate = o25_c / n
                o35_rate = o35_c / n
                o45_rate = o45_c / n

                best_rate = max(o15_rate, o25_rate, o35_rate, o45_rate)
                realistic_max = best_rate * 1.15
                if realistic_max < 0.68:
                    if self.alert_engine_v2:
                        # M2 usa taxas H2H específicas — não pode pré-filtrar por taxa global
                        logger.info(
                            f"M1 pre-filter skip for {loser} (match {match_id}, "
                            f"max={realistic_max:.0%}), mas M2 ainda monitora"
                        )
                    else:
                        logger.info(
                            f"Skip monitoring {loser} (match {match_id}): "
                            f"O1.5={o15_rate:.0%} O2.5={o25_rate:.0%} O3.5={o35_rate:.0%} "
                            f"max={realistic_max:.0%} < 68%"
                        )
                        return
        except Exception as e:
            logger.debug(f"Pre-filter check failed for {loser}: {e}")

        task = asyncio.create_task(
            self._monitor_loop(return_match, game1_match, loser, winner, loser_goals_g1),
            name=f"odds_monitor_{match_id}",
        )

        def _on_task_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(
                    f"OddsMonitor task for match {match_id} ({loser}) died: {exc}"
                )

        task.add_done_callback(_on_task_done)
        self._tasks[match_id] = task
        self._task_meta[match_id] = {"game1_match": game1_match, "loser": loser}
        self._task_started[match_id] = time.monotonic()
        logger.info(f"Started odds monitoring for return match {match_id} ({loser} as loser, g1_goals={loser_goals_g1})")

        # Log de monitoramento (sem enviar mensagem no Telegram)
        logger.info(
            f"Monitoring {loser} (g1_goals={loser_goals_g1}) for return match {match_id}"
        )

        # Agendar watch (pre-alerta) — fire and forget
        if match_id not in self._watch_tasks:
            wtask = asyncio.create_task(
                self._watch_loop(return_match, game1_match, loser, winner, loser_goals_g1),
                name=f"watch_{match_id}",
            )
            self._watch_tasks[match_id] = wtask

    async def _monitor_loop(
        self, return_match, game1_match, loser: str, winner: str, loser_goals_g1: int = 0
    ) -> None:
        """Poll bet365 for player goals odds until game ends or alert sent.

        MELHORIA 4: Usa _adaptive_poll_interval() para variar a frequência
        de polling baseado na proximidade ao kickoff.
        """
        match_id = return_match.id
        alert_sent = False
        # 2026-04-26: track se ja criamos um suppressed alert pra esse match,
        # pra nao duplicar entradas de shadow tracking a cada poll.
        suppressed_recorded = False
        _monitor_started = time.monotonic()

        _consecutive_errors = 0
        try:
            while True:
                # Timeout de segurança: máximo 45 min de monitoramento
                if time.monotonic() - _monitor_started > self._MAX_MONITOR_SECONDS:
                    logger.info(f"Match {match_id}: timeout de 45min atingido, encerrando monitor")
                    break

                now = datetime.now(timezone.utc).replace(tzinfo=None)
                kickoff = return_match.started_at

                # Para apenas quando game_watcher marca o jogo como ended.
                # Tempo virtual de 8min vira ~13-14min reais (pausas de gol/falta/replay).
                # Salvaguarda final: _MAX_MONITOR_SECONDS=45min acima.
                if kickoff and (now - kickoff).total_seconds() > 60:
                    repo = self.match_repo or self.alert_engine.stats.matches
                    try:
                        current = await repo.get_by_id(match_id)
                        if current and (current.status == "ended" or current.ended_at is not None):
                            logger.info(f"Match {match_id}: game ended, stopping monitor")
                            break
                    except Exception as e:
                        logger.debug(f"Match {match_id}: ended-check failed: {e}")

                # Calculate minutes to/from kickoff
                minutes_left = None
                if kickoff:
                    minutes_left = (kickoff - now).total_seconds() / 60

                # MELHORIA 4: Intervalo adaptativo
                interval = _adaptive_poll_interval(minutes_left)

                # Esperar até 3 min antes do kickoff sem fazer requests
                # Pre-warm: aquecer cache de stats 4 min antes do kickoff
                if minutes_left is not None and minutes_left > 3:
                    if 3 < minutes_left <= 4.5 and not getattr(self, f'_warmed_{match_id}', False):
                        try:
                            await self.alert_engine.stats.pre_warm_cache(loser, winner)
                            setattr(self, f'_warmed_{match_id}', True)
                            logger.info(f"Match {match_id}: cache pre-aquecido para {loser}")
                        except Exception as e:
                            logger.debug(f"Pre-warm failed for {loser}: {e}")
                    wait = max(10, (minutes_left - 3) * 60)
                    logger.debug(f"Match {match_id}: {minutes_left:.0f}min pro kickoff, dormindo {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue

                try:
                    # Timing instrumentation (2026-04-29 — diagnose latencia)
                    _t0 = time.monotonic()
                    # Buscar odds bet365 do jogador (com fuzzy matching)
                    loser_odds, bet365_url, matched_ev = await self._fetch_loser_odds(return_match, loser)
                    _t_fetch = time.monotonic()

                    if loser_odds is None and matched_ev is None:
                        await asyncio.sleep(interval)
                        continue
                    if loser_odds is None:
                        loser_odds = []

                    # Extract lines: 1.5, 2.5, 3.5, 4.5
                    over15_odds = None
                    over25_odds = None
                    over35_odds = None
                    over45_odds = None

                    for po in loser_odds:
                        if po.line == 1.5:
                            over15_odds = po.over_odds
                        elif po.line == 2.5:
                            over25_odds = po.over_odds
                        elif po.line == 3.5:
                            over35_odds = po.over_odds
                        elif po.line == 4.5:
                            over45_odds = po.over_odds

                    ml_odds = None  # ML removido: nunca gerou alerta e custava 2-3s de API

                    best_odds = over25_odds or over35_odds or over15_odds or over45_odds
                    if not best_odds:
                        await asyncio.sleep(interval)
                        continue

                    # Log what we found
                    lines_str = " | ".join(
                        f"O{l}@{o:.2f}" for l, o in
                        [(1.5, over15_odds), (2.5, over25_odds), (3.5, over35_odds), (4.5, over45_odds)]
                        if o
                    )
                    logger.info(f"Bet365 odds for {loser} (match {match_id}): {lines_str}")

                    # Evaluate and alert PRIMEIRO — DB saves depois (reduz delay)
                    # Sem gate temporal: alerta dispara sempre que filtros estatisticos passarem
                    if not alert_sent:
                        # Get opening odds (necessário para eval)
                        over25_opening = None
                        history = []
                        _t_hist_start = time.monotonic()
                        try:
                            history = await self.odds_repo.get_history(match_id, loser, "over_2.5")
                            if history:
                                over25_opening = history[0].odds_value
                        except Exception:
                            pass
                        _t_hist = time.monotonic()

                        sent, was_suppressed = await self.alert_engine.evaluate_and_alert(
                            return_match=return_match,
                            game1_match=game1_match,
                            loser=loser,
                            winner=winner,
                            over25_odds=over25_odds or 0.0,
                            over35_odds=over35_odds,
                            over45_odds=over45_odds,
                            over15_odds=over15_odds,
                            ml_odds=ml_odds,
                            over25_opening=over25_opening,
                            minutes_to_kickoff=int(minutes_left) if minutes_left is not None else 0,
                            odds_history=history,
                            loser_goals_g1=loser_goals_g1,
                            bet365_url=bet365_url or "",
                            suppressed_already_recorded=suppressed_recorded,
                        )
                        _t_eval = time.monotonic()
                        # Timing log: so quando algo aconteceu (alert ou suppressed)
                        # ou quando o pipeline demorou >2s — pra nao inundar logs.
                        _total = _t_eval - _t0
                        if sent or was_suppressed or _total > 2.0:
                            logger.bind(category="latency").info(
                                f"[LAT] match={match_id} loser={loser} "
                                f"fetch={_t_fetch - _t0:.2f}s "
                                f"hist={_t_hist - _t_hist_start:.2f}s "
                                f"eval+alert={_t_eval - _t_hist:.2f}s "
                                f"total={_total:.2f}s "
                                f"sent={sent} supp={was_suppressed}"
                            )
                        if sent:
                            alert_sent = True
                            logger.info(f"Alert sent for return match {match_id}")
                        elif was_suppressed:
                            # 2026-04-26 fix: alerta suprimido (auto-block) NAO trava
                            # o monitor — ele continua avaliando proximas polls. Mas
                            # marcamos suppressed_recorded pra alert_engine nao criar
                            # novo DB row em cada poll subsequente (shadow tracking
                            # ja foi computado na 1a vez).
                            if not suppressed_recorded:
                                suppressed_recorded = True
                                logger.info(
                                    f"Match {match_id}: alerta suprimido registrado, "
                                    f"monitor continua avaliando outras linhas"
                                )

                    # Method 2: avaliar independentemente (uma vez por partida)
                    # Sem gate temporal: alerta dispara sempre que filtros estatisticos passarem
                    if self.alert_engine_v2 and not self._alert_v2_sent.get(match_id):
                        try:
                            sent_v2 = await self.alert_engine_v2.evaluate_and_alert(
                                return_match=return_match,
                                game1_match=game1_match,
                                loser=loser,
                                winner=winner,
                                over25_odds=over25_odds or 0.0,
                                over35_odds=over35_odds,
                                over45_odds=over45_odds,
                                over15_odds=over15_odds,
                                minutes_to_kickoff=int(minutes_left) if minutes_left is not None else 0,
                                loser_goals_g1=loser_goals_g1,
                                bet365_url=bet365_url or "",
                            )
                            if sent_v2:
                                self._alert_v2_sent[match_id] = True
                                logger.info(f"M2 alert sent for return match {match_id}")
                        except Exception as e_v2:
                            logger.warning(f"M2 alert engine error for match {match_id}: {e_v2}")

                    # DB saves DEPOIS da avaliação (não bloqueia o alerta)
                    for label, odds_val in [("over_1.5", over15_odds), ("over_2.5", over25_odds),
                                             ("over_3.5", over35_odds), ("over_4.5", over45_odds)]:
                        if odds_val:
                            try:
                                await self.odds_repo.save_snapshot(
                                    match_id=match_id, player=loser,
                                    market=label, odds_value=odds_val,
                                )
                            except Exception as snap_err:
                                logger.debug(f"Odds snapshot save failed: {snap_err}")

                    _consecutive_errors = 0

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _consecutive_errors += 1
                    logger.warning(
                        f"Odds monitor cycle error for {match_id} ({_consecutive_errors}/5): {e}"
                    )
                    if _consecutive_errors >= 5:
                        logger.error(f"Match {match_id}: 5 consecutive errors, stopping monitor")
                        break

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.debug(f"Odds monitor for {match_id} cancelled")
        finally:
            self._tasks.pop(match_id, None)
            self._task_started.pop(match_id, None)
            self._alert_v2_sent.pop(match_id, None)

    async def _watch_loop(
        self, return_match, game1_match, loser: str, winner: str, loser_goals_g1: int
    ) -> None:
        """Sleep ate T-90s, prediz candidato e envia watch silencioso ao grupo.

        Watch eh um pre-aviso para o cliente abrir a bet365 e ficar atento.
        Auto-deleta apos 10 min para nao poluir o grupo.
        """
        match_id = return_match.id
        kickoff = return_match.started_at
        if kickoff is None:
            logger.debug(f"Watch {match_id}: sem kickoff, abortando")
            return

        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            seconds_until_send = (kickoff - now).total_seconds() - self._WATCH_LEAD_SECONDS

            # Se ja passou T-90s, ainda envia se kickoff nao chegou
            if seconds_until_send > 0:
                await asyncio.sleep(seconds_until_send)

            # Verificar se kickoff ja aconteceu (atrasamos demais)
            now2 = datetime.now(timezone.utc).replace(tzinfo=None)
            if (kickoff - now2).total_seconds() < 0:
                logger.info(f"Watch {match_id}: kickoff ja passou, abortando")
                return

            # Predizer candidato
            stats = self.alert_engine.stats
            loser_was_home_g1 = (
                (game1_match.player_home == loser)
                if game1_match.player_home else None
            )
            candidate = await stats.predict_watch_candidate(
                return_match=return_match,
                game1_match=game1_match,
                losing_player=loser,
                opponent_player=winner,
                loser_goals_g1=loser_goals_g1,
                loser_was_home_g1=loser_was_home_g1,
            )
            if candidate is None:
                logger.debug(f"Watch {match_id}: sem candidato, skip")
                return

            # 2026-04-28 fix + 2026-04-29 v3 H2H granular: skip watch se
            # (target_player, line, opponent) esta em SHADOW/PERMANENT.
            # is_suppressed agora exige opponent (state machine por matchup).
            blocked_repo = getattr(self.alert_engine, "blocked", None)
            candidate_line = candidate.get("line")
            if blocked_repo is not None and candidate_line:
                try:
                    is_supp = await blocked_repo.is_suppressed(
                        loser, candidate_line, winner
                    )
                except Exception as e:
                    logger.warning(
                        f"Watch {match_id}: is_suppressed falhou ({e}), prosseguindo"
                    )
                    is_supp = False
                if is_supp:
                    h2h_wl = getattr(self.alert_engine.stats, "H2H_WHITELIST", set())
                    if (loser, winner, candidate_line) in h2h_wl:
                        is_supp = False
                if is_supp:
                    logger.info(
                        f"Watch {match_id}: skip — "
                        f"{loser}/{candidate_line}/vs.{winner} "
                        f"esta suprimida (auto-block); pre-alerta nao seria honrado"
                    )
                    return

            # Montar payload e enviar
            from zoneinfo import ZoneInfo
            kickoff_brt = (
                kickoff.replace(tzinfo=timezone.utc)
                .astimezone(ZoneInfo("America/Sao_Paulo"))
            )
            watch_data = {
                "kickoff_str": kickoff_brt.strftime("%H:%M"),
                "player_home": return_match.player_home,
                "player_away": return_match.player_away,
                "line_label": candidate["line_label"],
                "target_player": candidate["target_player"],
                "target_odds": candidate["target_odds"],
                "lines": candidate.get("lines") or [],
            }
            notifier = self.alert_engine.notifier
            await notifier.send_watch(
                watch_data,
                auto_delete_seconds=self._WATCH_AUTO_DELETE_SECONDS,
            )

        except asyncio.CancelledError:
            logger.debug(f"Watch task {match_id} cancelled")
            raise
        except Exception as e:
            logger.warning(f"Watch loop error for match {match_id}: {e}")
        finally:
            self._watch_tasks.pop(match_id, None)

    async def _fetch_loser_odds(self, return_match, loser: str):
        """Find bet365 FI for the match and return player goals odds for the loser.

        Uses fuzzy matching to tolerate name differences between BetsAPI and Bet365.
        Returns (loser_odds, bet365_url, matched_event) or (None, None, None).
        """
        try:
            inplay = await self.api.bet365_get_inplay_esoccer()

            target_players = {
                _normalize_player(return_match.player_home),
                _normalize_player(return_match.player_away),
            }

            matched_ev = None
            best_similarity = 0.0

            for ev in inplay:
                ev_players = {
                    _normalize_player(ev.home_player),
                    _normalize_player(ev.away_player),
                }

                if ev_players == target_players:
                    matched_ev = ev
                    break

                if _fuzzy_match_players(target_players, ev_players, threshold=0.80):
                    t_list = sorted(target_players)
                    e_list = sorted(ev_players)
                    sim = (_name_similarity(t_list[0], e_list[0]) + _name_similarity(t_list[1], e_list[1])) / 2
                    sim2 = (_name_similarity(t_list[0], e_list[1]) + _name_similarity(t_list[1], e_list[0])) / 2
                    score = max(sim, sim2)

                    if score > best_similarity:
                        best_similarity = score
                        matched_ev = ev

            if matched_ev is None:
                logger.debug(f"Match {return_match.id} not found in bet365 inplay yet")
                return None, None, None

            if best_similarity > 0 and best_similarity < 1.0:
                logger.info(
                    f"Fuzzy matched bet365 event for match {return_match.id}: "
                    f"{matched_ev.home_player} vs {matched_ev.away_player} "
                    f"(similarity={best_similarity:.2f})"
                )

            all_odds = await self.api.bet365_get_player_goals_odds(matched_ev.fi)

            loser_norm = _normalize_player(loser)
            loser_odds = []
            for o in all_odds:
                o_norm = _normalize_player(o.player_name)
                if o_norm == loser_norm:
                    loser_odds.append(o)
                elif _name_similarity(o_norm, loser_norm) >= 0.85:
                    logger.debug(
                        f"Fuzzy matched loser odds: '{o.player_name}' ≈ '{loser}' "
                        f"(sim={_name_similarity(o_norm, loser_norm):.2f})"
                    )
                    loser_odds.append(o)

            if not loser_odds:
                logger.debug(f"No goals market for {loser} in bet365 event {matched_ev.fi}")
                # Mesmo sem goals market, retornar matched_ev para ML odds
                safe_url = getattr(matched_ev, 'bet365_url', None) or ""
                return None, safe_url, matched_ev

            # PROBLEMA 10 fix: tratamento defensivo de bet365_url
            safe_url = getattr(matched_ev, 'bet365_url', None) or ""
            return loser_odds, safe_url, matched_ev

        except Exception as e:
            logger.warning(f"Failed to fetch bet365 odds for {loser}: {e}")
            return None, None, None

    def _cleanup_dead_tasks(self) -> None:
        """Remove tasks that are done, cancelled, or timed out."""
        now = time.monotonic()
        dead = []
        for mid, task in self._tasks.items():
            if task.done() or task.cancelled():
                dead.append(mid)
            elif mid in self._task_started and now - self._task_started[mid] > self._MAX_MONITOR_SECONDS + 60:
                # Zombie: exceeded max time + 1 min grace
                task.cancel()
                dead.append(mid)
                logger.warning(f"Killed zombie odds monitor for match {mid}")
        for mid in dead:
            self._tasks.pop(mid, None)
            self._task_meta.pop(mid, None)
            self._task_started.pop(mid, None)

    def stop_monitoring(self, match_id: int) -> None:
        """Cancel monitoring for a specific match."""
        task = self._tasks.pop(match_id, None)
        if task:
            task.cancel()
            logger.debug(f"Stopped monitoring return match {match_id}")
        wtask = self._watch_tasks.pop(match_id, None)
        if wtask and not wtask.done():
            wtask.cancel()

    def stop_all(self) -> None:
        """Cancel all monitoring tasks."""
        for task in self._tasks.values():
            task.cancel()
        for wtask in self._watch_tasks.values():
            if not wtask.done():
                wtask.cancel()
        self._tasks.clear()
        self._task_started.clear()
        self._watch_tasks.clear()
        logger.info("All odds monitors stopped")

    @property
    def active_count(self) -> int:
        return len(self._tasks)
