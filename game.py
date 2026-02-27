from __future__ import annotations
import random
from typing import Optional
from models import Card, Rank, Suit, GameState, ActionLog

GRID_SIZE = 4  # 2x2 = 4 cards per player
CALLER_AUTO_LOSE_SCORE = 7


def build_deck(seed: Optional[int] = None) -> list[Card]:
    """Build a double 52-card deck (104 cards)."""
    rng = random.Random(seed)
    deck: list[Card] = []
    for _ in range(2):
        for suit in Suit:
            for rank in Rank:
                is_red_king = rank == Rank.KING and suit in (Suit.HEARTS, Suit.DIAMONDS)
                deck.append(Card(rank=rank, suit=suit, is_red_king=is_red_king))
    rng.shuffle(deck)
    return deck


class Player:
    def __init__(self, player_id: str, name: str):
        self.id = player_id
        self.name = name
        self.hand: list[Optional[Card]] = [None] * GRID_SIZE  # 4 slots
        self.connected: bool = True
        self.called_rikiki: bool = False
        self.done_last_turn: bool = False

    def score(self) -> int:
        total = 0
        for card in self.hand:
            if card is not None:
                total += card.value
        return total

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "connected": self.connected,
            "called_rikiki": self.called_rikiki,
            "card_count": sum(1 for c in self.hand if c is not None),
            "hand_public": [{"id": c.id} if c else None for c in self.hand],
        }

    def to_private(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "hand": [c.dict_private() if c else None for c in self.hand],
        }


class GameRoom:
    def __init__(self, room_code: str, seed: Optional[int] = None):
        self.room_code = room_code
        self.seed = seed
        self.players: list[Player] = []
        self.deck: list[Card] = []
        self.discard_pile: list[Card] = []
        self.state: GameState = GameState.LOBBY
        self.turn_index: int = 0
        self.rikiki_called_by: Optional[str] = None
        self.last_round_remaining: list[str] = []  # player ids still to play last round
        self.action_log: list[ActionLog] = []
        self.pending_special: Optional[dict] = None  # for J/Q/K multi-step actions
        self.reveal_until: float = 0.0  # server epoch until which bottom-2 cards are shown
        self.reveal_until: float = 0.0  # unix timestamp until which bottom cards are revealed

    @property
    def current_player(self) -> Optional[Player]:
        if not self.players:
            return None
        return self.players[self.turn_index % len(self.players)]

    def get_player(self, player_id: str) -> Optional[Player]:
        for p in self.players:
            if p.id == player_id:
                return p
        return None

    def add_player(self, player_id: str, name: str) -> Player:
        if self.state != GameState.LOBBY:
            raise ValueError("Game already started")
        if len(self.players) >= 8:
            raise ValueError("Room full")
        existing = self.get_player(player_id)
        if existing:
            existing.connected = True
            return existing
        player = Player(player_id, name)
        self.players.append(player)
        return player

    def start_game(self) -> None:
        if len(self.players) < 2:
            raise ValueError("Need at least 2 players")
        self.deck = build_deck(self.seed)
        self.discard_pile = []
        self.state = GameState.PLAYING
        self.turn_index = 0
        self.rikiki_called_by = None
        self.pending_special = None
        import time
        # All clients receive reveal_until in public_state so the countdown
        # is synchronised server-side regardless of network latency.
        REVEAL_SECONDS = 5  # configurable
        self.reveal_until = time.time() + REVEAL_SECONDS
        # Deal 4 cards to each player
        for player in self.players:
            player.hand = []
            player.called_rikiki = False
            player.done_last_turn = False
            for _ in range(GRID_SIZE):
                if self.deck:
                    player.hand.append(self.deck.pop())
                else:
                    player.hand.append(None)
        self._log(self.players[0].id, "game_started", {"seed": self.seed})

    def draw_card(self, player_id: str) -> dict:
        """Player draws a card. Returns action result."""
        player = self._validate_turn(player_id)
        if not self.deck:
            # Reshuffle discard
            if not self.discard_pile:
                raise ValueError("No cards left")
            self.deck = self.discard_pile[:]
            random.shuffle(self.deck)
            self.discard_pile = []

        card = self.deck.pop()
        self._log(player_id, "draw_card", {"card_id": card.id})
        
        # Store drawn card in pending
        self.pending_special = {
            "type": "drawn",
            "player_id": player_id,
            "card": card,
        }

        result = {
            "card": card.dict_private(),
            "is_special": card.rank in (Rank.JACK, Rank.QUEEN, Rank.KING),
            "rank": card.rank,
        }
        return result

    def attempt_discard(self, player_id: str, position: int) -> dict:
        """Try to discard drawn card with card at position."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")
        
        drawn_card: Card = self.pending_special["card"]
        
        if position < 0 or position >= len(player.hand):
            raise ValueError("Invalid position")
        
        hand_card = player.hand[position]
        if hand_card is None:
            raise ValueError("No card at position")

        if drawn_card.rank == hand_card.rank:
            # Successful discard: both cards go to discard pile, slot becomes None
            self.discard_pile.extend([drawn_card, hand_card])
            player.hand[position] = None
            self.pending_special = None
            self._log(player_id, "discard_success", {"position": position})
            self._advance_turn()
            return {"success": True, "discarded": [drawn_card.dict_private(), hand_card.dict_private()]}
        else:
            # Failed attempt: player keeps the drawn card pending and must choose
            # whether to discard it or replace one owned card.
            self._log(player_id, "discard_fail", {"position": position})
            return {
                "success": False,
                "drawn": drawn_card.dict_private(),
                "next_action_required": "keep_or_replace",
            }

    def replace_card(self, player_id: str, position: int) -> dict:
        """Replace one owned card with the pending drawn card."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")

        if position < 0 or position >= len(player.hand):
            raise ValueError("Invalid position")

        current_card = player.hand[position]
        if current_card is None:
            raise ValueError("No card at position")

        drawn_card: Card = self.pending_special["card"]
        player.hand[position] = drawn_card
        self.discard_pile.append(current_card)
        self.pending_special = None
        self._log(player_id, "replace_card", {"position": position})
        self._advance_turn()
        return {
            "replaced": True,
            "position": position,
            "new_card": drawn_card.dict_private(),
        }

    def keep_card(self, player_id: str) -> dict:
        """Discard the drawn card without attempting a swap.

        Assumption (configurable): if the player does not attempt to discard a pair,
        the drawn card goes to the discard pile and the hand stays the same size.
        Set KEEP_ADDS_TO_HAND = True to revert to original behaviour (card goes in hand).
        """
        KEEP_ADDS_TO_HAND = False  # change to True to allow hand to grow
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")

        drawn_card: Card = self.pending_special["card"]
        if KEEP_ADDS_TO_HAND:
            player.hand.append(drawn_card)
        else:
            self.discard_pile.append(drawn_card)
        self.pending_special = None
        self._log(player_id, "keep_card", {"added_to_hand": KEEP_ADDS_TO_HAND})
        self._advance_turn()
        return {"kept": drawn_card.dict_private(), "added_to_hand": KEEP_ADDS_TO_HAND}

    def use_jack(self, player_id: str, target_player_id: str, position: int) -> dict:
        """Jack: peek at any card."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")
        drawn_card: Card = self.pending_special["card"]
        if drawn_card.rank != Rank.JACK:
            raise ValueError("Drawn card is not a Jack")
        
        target = self.get_player(target_player_id)
        if not target:
            raise ValueError("Target player not found")
        if position < 0 or position >= len(target.hand):
            raise ValueError("Invalid position")
        
        peeked_card = target.hand[position]
        self.discard_pile.append(drawn_card)
        # Jack is simply discarded — no change to player's hand
        self.pending_special = None
        self._log(player_id, "use_jack", {"target": target_player_id, "position": position})
        self._advance_turn()
        
        return {
            "peeked": peeked_card.dict_private() if peeked_card else None,
            "target_player_id": target_player_id,
            "position": position,
        }

    def use_queen(self, player_id: str, 
                  player_a_id: str, pos_a: int,
                  player_b_id: str, pos_b: int) -> dict:
        """Queen: swap two cards from different players (blind)."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")
        drawn_card: Card = self.pending_special["card"]
        if drawn_card.rank != Rank.QUEEN:
            raise ValueError("Drawn card is not a Queen")
        if player_a_id == player_b_id:
            raise ValueError("Must swap between different players")
        
        pa = self.get_player(player_a_id)
        pb = self.get_player(player_b_id)
        if not pa or not pb:
            raise ValueError("Player not found")
        if pos_a < 0 or pos_a >= len(pa.hand):
            raise ValueError("Invalid position A")
        if pos_b < 0 or pos_b >= len(pb.hand):
            raise ValueError("Invalid position B")
        
        pa.hand[pos_a], pb.hand[pos_b] = pb.hand[pos_b], pa.hand[pos_a]
        self.discard_pile.append(drawn_card)
        self.pending_special = None
        self._log(player_id, "use_queen", {
            "player_a": player_a_id, "pos_a": pos_a,
            "player_b": player_b_id, "pos_b": pos_b,
        })
        self._advance_turn()
        return {"swapped": True}

    def use_king_peek(self, player_id: str, target_player_id: str, position: int) -> dict:
        """King phase 1: peek a card (must be own card or any). Store for mandatory swap."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "drawn":
            raise ValueError("No card drawn yet")
        drawn_card: Card = self.pending_special["card"]
        if drawn_card.rank != Rank.KING:
            raise ValueError("Drawn card is not a King")
        
        target = self.get_player(target_player_id)
        if not target:
            raise ValueError("Target player not found")
        if position < 0 or position >= len(target.hand):
            raise ValueError("Invalid position")
        
        peeked = target.hand[position]
        # Store king state for mandatory swap
        self.pending_special = {
            "type": "king_peek",
            "player_id": player_id,
            "card": drawn_card,
            "peeked_player_id": target_player_id,
            "peeked_position": position,
            "peeked_card": peeked,
        }
        self._log(player_id, "king_peek", {"target": target_player_id, "position": position})
        return {
            "peeked": peeked.dict_private() if peeked else None,
            "target_player_id": target_player_id,
            "position": position,
        }

    def use_king_swap(self, player_id: str, other_player_id: str, other_position: int) -> dict:
        """King phase 2: mandatory swap of peeked card with other player's card."""
        player = self._validate_turn(player_id)
        if not self.pending_special or self.pending_special.get("type") != "king_peek":
            raise ValueError("Must peek first with King")
        ps = self.pending_special
        if ps["player_id"] != player_id:
            raise ValueError("Not your king action")
        
        peeked_player = self.get_player(ps["peeked_player_id"])
        other_player = self.get_player(other_player_id)
        if not other_player:
            raise ValueError("Other player not found")
        if other_player_id == ps["peeked_player_id"]:
            raise ValueError("Must swap with a different player")
        if other_position < 0 or other_position >= len(other_player.hand):
            raise ValueError("Invalid position")

        drawn_card: Card = ps["card"]
        peeked_pos: int = ps["peeked_position"]
        
        # Swap peeked card with other player's card
        peeked_player.hand[peeked_pos], other_player.hand[other_position] = \
            other_player.hand[other_position], peeked_player.hand[peeked_pos]
        
        self.discard_pile.append(drawn_card)
        self.pending_special = None
        self._log(player_id, "king_swap", {
            "from_player": ps["peeked_player_id"], "from_pos": peeked_pos,
            "to_player": other_player_id, "to_pos": other_position,
        })
        self._advance_turn()
        return {"swapped": True}

    def call_rikiki(self, player_id: str) -> dict:
        player = self._validate_turn(player_id)
        if self.state != GameState.PLAYING:
            raise ValueError("Cannot call Rikiki now")
        
        self.rikiki_called_by = player_id
        player.called_rikiki = True
        self.state = GameState.LAST_ROUND
        # All other players get one more turn
        idx = self.players.index(player)
        n = len(self.players)
        self.last_round_remaining = [
            self.players[(idx + i + 1) % n].id for i in range(n - 1)
        ]
        player.done_last_turn = True
        self._log(player_id, "call_rikiki", {})
        # Advance to next player for last round
        self._advance_turn(force=True)
        return {"called": True, "last_round_for": self.last_round_remaining}

    def end_game(self) -> dict:
        """Calculate final scores and determine winner."""
        self.state = GameState.ENDED
        scores = []
        caller = self.get_player(self.rikiki_called_by) if self.rikiki_called_by else None
        
        for p in self.players:
            scores.append({
                "player_id": p.id,
                "name": p.name,
                "score": p.score(),
                "hand": [c.dict_private() if c else None for c in p.hand],
                "called_rikiki": p.called_rikiki,
            })

        caller_score = caller.score() if caller else None
        caller_auto_lose = caller_score is not None and caller_score > CALLER_AUTO_LOSE_SCORE

        if caller_auto_lose:
            winner_id = min(
                (p for p in self.players if not p.called_rikiki),
                key=lambda p: p.score(),
                default=None,
            )
        else:
            winner_id = min(self.players, key=lambda p: p.score(), default=None)

        result = {
            "scores": scores,
            "winner_id": winner_id.id if winner_id else None,
            "caller_id": self.rikiki_called_by,
            "caller_auto_lose": caller_auto_lose,
        }
        self._log("system", "game_ended", result)
        return result

    def _validate_turn(self, player_id: str) -> Player:
        player = self.get_player(player_id)
        if not player:
            raise ValueError("Player not found")
        if self.state not in (GameState.PLAYING, GameState.LAST_ROUND):
            raise ValueError("Game not in playing state")
        if self.current_player and self.current_player.id != player_id:
            raise ValueError("Not your turn")
        return player

    def _advance_turn(self, force: bool = False) -> None:
        if self.state == GameState.LAST_ROUND:
            current_id = self.current_player.id if self.current_player else None
            if current_id in self.last_round_remaining:
                self.last_round_remaining.remove(current_id)
            if not self.last_round_remaining:
                self.end_game()
                return
        
        n = len(self.players)
        for _ in range(n):
            self.turn_index = (self.turn_index + 1) % n
            next_p = self.players[self.turn_index]
            if next_p.connected:
                break

    def _log(self, player_id: str, action: str, details: dict) -> None:
        self.action_log.append(ActionLog(player_id=player_id, action=action, details=details))

    def public_state(self) -> dict:
        import time
        return {
            "room_code": self.room_code,
            "state": self.state,
            "turn_index": self.turn_index,
            "current_player_id": self.current_player.id if self.current_player else None,
            "rikiki_called_by": self.rikiki_called_by,
            "deck_count": len(self.deck),
            "discard_top": self.discard_pile[-1].dict_private() if self.discard_pile else None,
            "players": [p.to_public() for p in self.players],
            "pending_action": self.pending_special.get("type") if self.pending_special else None,
            "reveal_until": self.reveal_until,  # clients show bottom 2 cards until this timestamp
        }

    def private_state(self, player_id: str) -> dict:
        player = self.get_player(player_id)
        if not player:
            return {}
        return player.to_private()
