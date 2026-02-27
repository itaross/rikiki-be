import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from game import GameRoom, build_deck
from models import Rank, Suit, Card


def test_deck_generation():
    deck = build_deck(seed=42)
    assert len(deck) == 104
    red_kings = [c for c in deck if c.is_red_king]
    assert len(red_kings) == 4  # 2x K♥ + 2x K♦
    for rk in red_kings:
        assert rk.value == 0


def test_deck_shuffle_is_seeded():
    d1 = build_deck(seed=1)
    d2 = build_deck(seed=1)
    assert [c.rank for c in d1] == [c.rank for c in d2]


def test_discard_matching():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    original_hand_size = len([c for c in room.players[0].hand if c])
    test_card = Card(rank=Rank.FIVE, suit=Suit.HEARTS)
    matching = Card(rank=Rank.FIVE, suit=Suit.CLUBS)
    room.players[0].hand[0] = test_card
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': matching}
    room.turn_index = 0
    result = room.attempt_discard('p1', 0)
    assert result['success'] is True
    # hand should be smaller (slot becomes None)
    assert room.players[0].hand[0] is None


def test_discard_fail_card_goes_to_hand():
    """On failed discard attempt, drawn card is added to player hand (penalty)."""
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    hand_size_before = len(room.players[0].hand)
    test_card = Card(rank=Rank.FIVE, suit=Suit.HEARTS)
    non_matching = Card(rank=Rank.SEVEN, suit=Suit.CLUBS)
    room.players[0].hand[0] = test_card
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': non_matching}
    room.turn_index = 0
    result = room.attempt_discard('p1', 0)
    assert result['success'] is False
    # Hand grows by 1 (penalty)
    assert len(room.players[0].hand) == hand_size_before + 1
    assert room.players[0].hand[-1].rank == Rank.SEVEN


def test_keep_card_discards_drawn():
    """keep_card (KEEP_ADDS_TO_HAND=False): drawn card goes to discard pile, hand unchanged."""
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    hand_size_before = len(room.players[0].hand)
    discard_size_before = len(room.discard_pile)
    drawn = Card(rank=Rank.THREE, suit=Suit.DIAMONDS)
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': drawn}
    room.turn_index = 0
    result = room.keep_card('p1')
    assert result['kept']['rank'] == Rank.THREE
    # Hand stays the same size (card discarded, not kept)
    assert len(room.players[0].hand) == hand_size_before
    assert len(room.discard_pile) == discard_size_before + 1


def test_jack_effect():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    jack = Card(rank=Rank.JACK, suit=Suit.CLUBS)
    target_card = Card(rank=Rank.NINE, suit=Suit.DIAMONDS)
    hand_size_before = len(room.players[0].hand)
    room.players[1].hand[0] = target_card
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': jack}
    room.turn_index = 0
    result = room.use_jack('p1', 'p2', 0)
    assert result['peeked']['rank'] == Rank.NINE
    # Jack is discarded, hand size unchanged
    assert len(room.players[0].hand) == hand_size_before


def test_queen_swap():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    card_a = Card(rank=Rank.THREE, suit=Suit.HEARTS)
    card_b = Card(rank=Rank.KING, suit=Suit.CLUBS)
    room.players[0].hand[0] = card_a
    room.players[1].hand[0] = card_b
    queen = Card(rank=Rank.QUEEN, suit=Suit.SPADES)
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': queen}
    room.turn_index = 0
    room.use_queen('p1', 'p1', 0, 'p2', 0)
    assert room.players[0].hand[0].rank == Rank.KING
    assert room.players[1].hand[0].rank == Rank.THREE


def test_scoring():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    room.players[0].hand = [
        Card(rank=Rank.ACE, suit=Suit.CLUBS),
        Card(rank=Rank.FIVE, suit=Suit.HEARTS),
        Card(rank=Rank.KING, suit=Suit.HEARTS, is_red_king=True),
        Card(rank=Rank.TEN, suit=Suit.SPADES),
    ]
    assert room.players[0].score() == 16


def test_rikiki_auto_lose():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    room.players[0].hand = [
        Card(rank=Rank.TEN, suit=Suit.CLUBS),
        Card(rank=Rank.TEN, suit=Suit.HEARTS),
    ]
    assert room.players[0].score() == 20
    room.players[0].called_rikiki = True
    room.rikiki_called_by = 'p1'
    result = room.end_game()
    assert result['caller_auto_lose'] is True
    assert result['winner_id'] == 'p2'


def test_king_peek_and_swap():
    room = GameRoom('TEST', seed=0)
    room.add_player('p1', 'Alice')
    room.add_player('p2', 'Bob')
    room.start_game()
    card_own = Card(rank=Rank.TWO, suit=Suit.CLUBS)
    card_other = Card(rank=Rank.EIGHT, suit=Suit.SPADES)
    room.players[0].hand[0] = card_own
    room.players[1].hand[0] = card_other
    king = Card(rank=Rank.KING, suit=Suit.SPADES)
    room.pending_special = {'type': 'drawn', 'player_id': 'p1', 'card': king}
    room.turn_index = 0
    peek_result = room.use_king_peek('p1', 'p1', 0)
    assert peek_result['peeked']['rank'] == Rank.TWO
    swap_result = room.use_king_swap('p1', 'p2', 0)
    assert swap_result['swapped'] is True
    assert room.players[0].hand[0].rank == Rank.EIGHT
    assert room.players[1].hand[0].rank == Rank.TWO
