"""Carrying a message between agents, and refusing to (M7, FLOTTA-54).

The limits are here rather than in a live test on purpose: proving that two
agents *stop* talking to each other means letting them start, and a runaway
costs a wake and a model call per hop. These are the fakes that make the
runaway free to test.
"""

import pytest

from flotta.relay import BUDGET, MAX_DEPTH, WINDOW_S, Chains, RelayRefused, envelope


def _chain_of(chains: Chains, *names: str) -> list:
    """Walk a chain of delegations, each one made while the last is in flight."""
    return [chains.begin(a, b) for a, b in zip(names, names[1:], strict=False)]


def test_a_first_message_starts_a_chain():
    chains = Chains()
    hop = chains.begin("a", "b")
    assert hop.depth == 1
    assert hop.path == ("a", "b")
    assert hop.caller == "a" and hop.peer == "b"


def test_a_message_sent_while_answering_continues_the_same_chain():
    """The thing that makes a chain a chain. B is answering A when it asks C,
    so C's question is one hop further into A's chain rather than a new one —
    which is what the depth limit has to count."""
    chains = Chains()
    first, second = _chain_of(chains, "a", "b", "c")
    assert second.chain == first.chain
    assert second.depth == 2
    assert second.path == ("a", "b", "c")


def test_an_unrelated_message_starts_its_own_chain():
    chains = Chains()
    first = chains.begin("a", "b")
    chains.end(first)
    second = chains.begin("d", "e")
    assert second.chain != first.chain
    assert second.depth == 1


def test_a_reply_that_would_go_back_to_the_asker_is_refused():
    """A → B → A is the cheapest infinite loop there is."""
    chains = Chains()
    chains.begin("a", "b")
    with pytest.raises(RelayRefused, match="already in it"):
        chains.begin("b", "a")


def test_a_longer_ring_is_refused_too():
    """Not just A→B→A: any repeat, because an agent asked the same question
    twice in one chain answers it the same way both times."""
    chains = Chains()
    _chain_of(chains, "a", "b", "c")
    with pytest.raises(RelayRefused, match="already in it"):
        chains.begin("c", "a")


def test_a_question_cannot_be_passed_along_forever():
    chains = Chains()
    names = ["n0"] + [f"n{i}" for i in range(1, MAX_DEPTH + 1)]
    _chain_of(chains, *names)
    with pytest.raises(RelayRefused, match="passed along"):
        chains.begin(names[-1], "one-too-far")


def test_a_wide_fan_out_spends_the_asking_agents_budget():
    """The depth limit alone permits one agent asking everybody, forever: each
    of these is depth 1, none is a loop, and each finishes before the next
    starts. Only a budget catches it."""
    chains = Chains()
    for i in range(BUDGET):
        chains.end(chains.begin("a", f"peer{i}"))
    with pytest.raises(RelayRefused, match=f"{BUDGET} messages"):
        chains.begin("a", "one-more")


def test_asking_the_same_agent_over_and_over_spends_it_too():
    """The runaway that survived the first version of this: A asks B, B
    answers, A asks B again. Nothing is in flight between them, no cycle, no
    depth — and it can go on forever."""
    chains = Chains()
    for _ in range(BUDGET):
        chains.end(chains.begin("a", "b"))
    with pytest.raises(RelayRefused, match=f"{BUDGET} messages"):
        chains.begin("a", "b")


def test_the_budget_is_spent_by_whoever_started_the_question():
    """B asking C on A's behalf spends A's budget, not B's. Otherwise a chain
    one agent deep at a time could fan out indefinitely by handing the
    question along."""
    chains = Chains()
    _chain_of(chains, "a", "b", "c")
    assert len(chains.recent["a"]) == 2
    assert "b" not in chains.recent


def test_the_budget_refills_as_the_window_passes():
    """Otherwise an agent that had a busy morning is mute for the rest of the
    day, and the limit reads as a bug rather than a brake."""
    now = [1000.0]
    chains = Chains(clock=lambda: now[0])
    for i in range(BUDGET):
        chains.end(chains.begin("a", f"peer{i}"))
    with pytest.raises(RelayRefused):
        chains.begin("a", "one-more")
    now[0] += WINDOW_S + 1
    assert chains.begin("a", "later").depth == 1


def test_a_quiet_agent_leaves_nothing_behind():
    """`recent` is keyed by box id and appended to on every delivery, so
    without pruning a long-lived control plane grows forever."""
    now = [1000.0]
    chains = Chains(clock=lambda: now[0])
    chains.end(chains.begin("a", "b"))
    now[0] += WINDOW_S + 1
    chains.end(chains.begin("c", "d"))
    assert "a" not in chains.recent


def test_an_agent_already_answering_is_not_interrupted():
    chains = Chains()
    chains.begin("a", "b")
    with pytest.raises(RelayRefused, match="already answering"):
        chains.begin("c", "b")


def test_an_agent_is_free_again_once_it_has_answered():
    chains = Chains()
    hop = chains.begin("a", "b")
    chains.end(hop)
    assert chains.begin("c", "b").depth == 1


def test_ending_twice_is_harmless():
    """The route ends the hop in a `finally`, which also runs after a failure
    that already ended it."""
    chains = Chains()
    hop = chains.begin("a", "b")
    chains.end(hop)
    chains.end(hop)
    assert chains.executing == {}


def test_a_refused_delivery_does_not_count_against_the_budget():
    """A refusal costs nothing — no wake, no model call — so charging for it
    would let a confused agent exhaust its own budget on things it was never
    allowed to do."""
    chains = Chains()
    chains.begin("a", "b")
    with pytest.raises(RelayRefused):
        chains.begin("b", "a")
    assert len(chains.recent["a"]) == 1


def test_the_envelope_says_who_is_asking_and_that_there_is_no_reply():
    """The door strips the caller's authorization before proxying, so the
    answering agent cannot learn who is talking to it from the connection."""
    text = envelope("eng-a", "what does the door do when a box is asleep?")
    assert "eng-a" in text
    assert "what does the door do when a box is asleep?" in text
    assert "cannot reply again" in text


def test_a_loop_refusal_names_agents_the_way_a_person_does():
    """The first live loop refusal read `b-79c6ca696e62 → b-e0d678cb4684 →
    b-79c6ca696e62`. The model worked it out; the person reading the
    transcript could not."""
    names = {"b-1": "eng-g", "b-2": "eng-r"}
    chains = Chains()
    chains.begin("b-1", "b-2", name=names.get)
    with pytest.raises(RelayRefused) as refused:
        chains.begin("b-2", "b-1", name=names.get)
    assert "eng-g → eng-r → eng-g" in str(refused.value)
    assert "b-1" not in str(refused.value)
