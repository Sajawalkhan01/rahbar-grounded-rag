"""The query pipeline end to end, with a fake retriever and a scripted provider (section 3.2):
cache, follow-ups, abstention, validation, retry, fallback and citation checks."""

from __future__ import annotations

import fakeredis
import pytest
from app.logjson import request_id_var
from app.providers.llm.errors import ProviderError
from app.schemas import Answer, Citation
from app.services.answer import ABSTAIN_TEXT, answer_question
from app.services.cache import SemanticCache
from app.services.followup import is_followup
from app.services.turns import TurnStore
from app.services.validate import FALLBACK_TEXT

from tests.fakes import (
    FakeRetriever,
    ScriptedProvider,
    good_answer,
    passage,
    settings,
)


@pytest.fixture()
def redis_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


def _cache(r: fakeredis.FakeRedis, threshold: float = 0.9) -> SemanticCache:
    return SemanticCache(r, threshold, 3600)


Q = "What is the attendance requirement for the final exam?"


def test_grounded_answer_has_validated_citations() -> None:
    retriever, provider = FakeRetriever(), ScriptedProvider(good_answer())
    r = answer_question(Q, retriever, provider, settings())
    assert r.answered_from_context and r.citations == [Citation(source="doc1.pdf", section="1. Section 1", page=1)]
    assert [x.chunk_id for x in r.retrieved] == ["c1", "c2"]
    assert not r.cached


def test_nothing_relevant_abstains_without_calling_the_model() -> None:
    retriever = FakeRetriever([passage(1, score=0.2)])
    provider = ScriptedProvider(good_answer())
    r = answer_question("How do I get a hostel room?", retriever, provider, settings(relevance_floor=0.5))
    assert not r.answered_from_context and r.answer == ABSTAIN_TEXT and r.citations == []
    assert provider.calls == 0                      # the model never had the chance to invent one


def test_empty_retrieval_abstains() -> None:
    r = answer_question(Q, FakeRetriever([]), ScriptedProvider(good_answer()), settings())
    assert not r.answered_from_context


def test_model_can_decline_and_its_citations_are_discarded() -> None:
    declined = Answer(reasoning="r", answer="The documents do not say.",
                      citations=[Citation(source="doc1.pdf", section="1. Section 1", page=1)],
                      answered_from_context=False, confidence="low")
    r = answer_question(Q, FakeRetriever(), ScriptedProvider(declined), settings())
    assert not r.answered_from_context and r.citations == []


def test_fabricated_citation_is_removed_and_counted() -> None:
    fake = Citation(source="made-up.pdf", section="9.9 Invented", page=1)
    real = Citation(source="doc1.pdf", section="1. Section 1", page=1)
    answer = good_answer().model_copy(update={"citations": [fake, real]})
    r = answer_question(Q, FakeRetriever(), ScriptedProvider(answer), settings())
    assert r.citations == [real]
    retrieved = {(p.source, p.section) for p in r.retrieved}
    assert all((c.source, c.section) in retrieved for c in r.citations)


def test_citation_with_the_wrong_page_is_not_valid() -> None:
    wrong_page = good_answer().model_copy(update={"citations": [
        Citation(source="doc1.pdf", section="1. Section 1", page=99)]})
    provider = ScriptedProvider(wrong_page, wrong_page)
    r = answer_question(Q, FakeRetriever(), provider, settings())
    assert r.answer == FALLBACK_TEXT and r.citations == []     # no valid citation, twice -> fallback


def test_only_fabricated_citations_trigger_one_retry_then_the_fallback() -> None:
    bad = good_answer().model_copy(update={"citations": [Citation(source="x", section="y", page=1)]})
    provider = ScriptedProvider(bad, bad, bad)
    r = answer_question(Q, FakeRetriever(), provider, settings())
    assert provider.calls == 2                                  # one retry, not more
    assert r.answer == FALLBACK_TEXT and not r.answered_from_context


def test_invalid_model_output_is_retried_once_and_then_succeeds() -> None:
    provider = ScriptedProvider(ProviderError("not JSON"), good_answer())
    r = answer_question(Q, FakeRetriever(), provider, settings())
    assert provider.calls == 2 and r.answered_from_context


def test_provider_failure_gives_a_safe_fallback_not_an_exception() -> None:
    provider = ScriptedProvider(ProviderError("down"), ProviderError("still down"))
    r = answer_question(Q, FakeRetriever(), provider, settings())
    assert r.answer == FALLBACK_TEXT and r.confidence == "low" and not r.answered_from_context


# ---------------------------------------------------------------------------------- cache
def test_second_similar_question_is_served_from_the_cache(redis_client: fakeredis.FakeRedis) -> None:
    cache, retriever = _cache(redis_client), FakeRetriever()
    provider = ScriptedProvider(good_answer())
    first = answer_question(Q, retriever, provider, settings(), cache=cache)
    second = answer_question(Q, retriever, provider, settings(), cache=cache)
    assert not first.cached and second.cached
    assert second.answer == first.answer and provider.calls == 1
    assert len(retriever.calls) == 1                            # a hit skips retrieval too


def test_a_different_question_is_a_miss(redis_client: fakeredis.FakeRedis) -> None:
    cache, retriever = _cache(redis_client), FakeRetriever()
    provider = ScriptedProvider(good_answer())
    answer_question(Q, retriever, provider, settings(), cache=cache)
    other = answer_question("When are tuition fees refunded after dropping a course?",
                            retriever, provider, settings(), cache=cache)
    assert not other.cached and provider.calls == 2


def test_threshold_comes_from_settings(redis_client: fakeredis.FakeRedis) -> None:
    retriever = FakeRetriever()
    emb = retriever.embed_query(Q)
    assert emb is not None
    cache = _cache(redis_client, threshold=0.99)
    cache.store("idx-1", Q, emb, {"x": 1})
    near = retriever.embed_query("What is the attendance requirement for final exams?")
    assert near is not None
    nearest = cache.nearest("idx-1", near)
    assert nearest is not None and 0.5 < nearest.similarity < 1.0
    assert cache.lookup("idx-1", near) is None                  # below 0.99
    cache.threshold = nearest.similarity - 0.01
    assert cache.lookup("idx-1", near) is not None


def test_reindexing_invalidates_the_cache(redis_client: fakeredis.FakeRedis) -> None:
    cache, retriever = _cache(redis_client), FakeRetriever(index="idx-1")
    provider = ScriptedProvider(good_answer())
    answer_question(Q, retriever, provider, settings(), cache=cache)
    retriever.index = "idx-2"                                   # the indexer wrote a new index_id
    after = answer_question(Q, retriever, provider, settings(), cache=cache)
    assert not after.cached and provider.calls == 2


def test_abstentions_are_never_cached(redis_client: fakeredis.FakeRedis) -> None:
    cache, retriever = _cache(redis_client), FakeRetriever([passage(1, score=0.1)])
    provider = ScriptedProvider(good_answer())
    answer_question(Q, retriever, provider, settings(), cache=cache)
    again = answer_question(Q, retriever, provider, settings(), cache=cache)
    assert not again.cached and len(retriever.calls) == 2


def test_model_declines_are_never_cached(redis_client: fakeredis.FakeRedis) -> None:
    declined = Answer(reasoning="r", answer="Not covered.", citations=[],
                      answered_from_context=False, confidence="low")
    cache, provider = _cache(redis_client), ScriptedProvider(declined)
    answer_question(Q, FakeRetriever(), provider, settings(), cache=cache)
    assert not answer_question(Q, FakeRetriever(), provider, settings(), cache=cache).cached
    assert provider.calls == 2


def test_failed_validation_is_never_cached(redis_client: fakeredis.FakeRedis) -> None:
    cache = _cache(redis_client)
    provider = ScriptedProvider(ProviderError("a"), ProviderError("b"), good_answer())
    first = answer_question(Q, FakeRetriever(), provider, settings(), cache=cache)
    assert first.answer == FALLBACK_TEXT
    second = answer_question(Q, FakeRetriever(), provider, settings(), cache=cache)
    assert not second.cached and second.answered_from_context   # a fresh answer, not the fallback


def test_followups_skip_the_cache_in_both_directions(redis_client: fakeredis.FakeRedis) -> None:
    cache, turns = _cache(redis_client), TurnStore(redis_client, 3600)
    retriever, provider = FakeRetriever(), ScriptedProvider(good_answer())
    sid = "session-001"
    answer_question(Q, retriever, provider, settings(), cache=cache, turns=turns, session_id=sid)
    follow = answer_question("And what about labs?", retriever, provider, settings(),
                             cache=cache, turns=turns, session_id=sid)
    assert not follow.cached and provider.calls == 2
    assert Q in retriever.calls[-1]                              # retrieval saw the earlier question
    # The follow-up was not cached either: the same words from a NEW session are a miss.
    fresh = answer_question("And what about labs?", retriever, provider, settings(),
                            cache=cache, turns=turns, session_id="session-002")
    assert not fresh.cached


def test_cache_errors_degrade_to_a_miss() -> None:
    class Broken:
        def pipeline(self) -> None:
            raise ConnectionError("redis down")

        def lrange(self, *_a: object) -> None:
            raise ConnectionError("redis down")

    cache = SemanticCache(Broken(), 0.9, 60)
    r = answer_question(Q, FakeRetriever(), ScriptedProvider(good_answer()), settings(), cache=cache)
    assert r.answered_from_context and not r.cached


def test_request_id_is_on_the_response() -> None:
    token = request_id_var.set("rid-42")
    try:
        r = answer_question(Q, FakeRetriever(), ScriptedProvider(good_answer()), settings())
    finally:
        request_id_var.reset(token)
    assert r.request_id == "rid-42"


# ------------------------------------------------------------------------------- follow-ups
@pytest.mark.parametrize(
    "question, previous, expected",
    [
        ("And what about labs?", True, True),
        ("What about it?", True, True),
        ("Why is that?", True, True),
        ("What is the attendance requirement for the final exam?", True, False),
        ("And what about labs?", False, False),          # nothing to follow
        ("How much is the late registration fee per course?", True, False),
    ],
)
def test_followup_detection(question: str, previous: bool, expected: bool) -> None:
    assert is_followup(question, previous) is expected


def test_turns_are_stored_per_session(redis_client: fakeredis.FakeRedis) -> None:
    turns = TurnStore(redis_client, 60)
    assert turns.last_question("abc12345") is None
    turns.add("abc12345", "first?", True, False)
    turns.add("abc12345", "second?", True, False)
    assert turns.last_question("abc12345") == "second?"
    assert turns.last_question("other-session") is None
    assert turns.last_question(None) is None


def test_a_question_naming_an_exact_term_is_relevant_to_a_passage_that_contains_it() -> None:
    """Embeddings blur clause numbers, so a low score must not make clause 1.21 unanswerable."""
    low = passage(1, score=0.3, text="1.21 Students are required to maintain 100% attendance.")
    provider = ScriptedProvider(good_answer(low))
    r = answer_question("What does clause 1.21 require?", FakeRetriever([low]), provider, settings())
    assert r.answered_from_context and provider.calls == 1


def test_a_low_score_without_an_exact_term_still_abstains() -> None:
    low = passage(1, score=0.3, text="1.21 Students are required to maintain 100% attendance.")
    provider = ScriptedProvider(good_answer(low))
    r = answer_question("What is the hostel curfew?", FakeRetriever([low]), provider, settings())
    assert not r.answered_from_context and provider.calls == 0


def test_a_question_whose_phrase_appears_in_a_passage_is_relevant_despite_a_low_score() -> None:
    """A fee table scores low against an embedding, but it contains the words "Late Registration Fee"."""
    low = passage(1, score=0.3, text="Miscellaneous Fees: Late Registration Fee/Freeze Fee PKR 5,000 per course.")
    provider = ScriptedProvider(good_answer(low))
    r = answer_question("What is the Late Registration Fee in the fee structure?",
                        FakeRetriever([low]), provider, settings())
    assert r.answered_from_context and provider.calls == 1


def test_a_shared_word_or_two_is_not_enough_to_rescue_a_low_score() -> None:
    low = passage(1, score=0.3, text="Miscellaneous Fees: Late Registration Fee PKR 5,000 per course.")
    provider = ScriptedProvider(good_answer(low))
    r = answer_question("What is the monthly hostel fee?", FakeRetriever([low]), provider, settings())
    assert not r.answered_from_context and provider.calls == 0
