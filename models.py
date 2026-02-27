from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field
import uuid


class Suit(str, Enum):
    HEARTS = "H"
    DIAMONDS = "D"
    CLUBS = "C"
    SPADES = "S"


class Rank(str, Enum):
    ACE = "A"
    TWO = "2"
    THREE = "3"
    FOUR = "4"
    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"
    NINE = "9"
    TEN = "10"
    JACK = "J"
    QUEEN = "Q"
    KING = "K"


RANK_VALUES: dict[Rank, int] = {
    Rank.ACE: 1,
    Rank.TWO: 2,
    Rank.THREE: 3,
    Rank.FOUR: 4,
    Rank.FIVE: 5,
    Rank.SIX: 6,
    Rank.SEVEN: 7,
    Rank.EIGHT: 8,
    Rank.NINE: 9,
    Rank.TEN: 10,
    Rank.JACK: 10,
    Rank.QUEEN: 10,
    Rank.KING: 10,
}


class Card(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    rank: Rank
    suit: Suit
    is_red_king: bool = False

    @property
    def value(self) -> int:
        if self.is_red_king:
            return 0
        return RANK_VALUES[self.rank]

    def dict_public(self) -> dict:
        """Return only public info (no rank/suit revealed)."""
        return {"id": self.id}

    def dict_private(self) -> dict:
        return {
            "id": self.id,
            "rank": self.rank,
            "suit": self.suit,
            "value": self.value,
            "is_red_king": self.is_red_king,
        }


class GameState(str, Enum):
    LOBBY = "lobby"
    PLAYING = "playing"
    LAST_ROUND = "last_round"
    ENDED = "ended"


class PlayerPublic(BaseModel):
    id: str
    name: str
    connected: bool
    called_rikiki: bool
    card_count: int  # how many cards in hand


class ActionLog(BaseModel):
    player_id: str
    action: str
    details: dict = Field(default_factory=dict)
