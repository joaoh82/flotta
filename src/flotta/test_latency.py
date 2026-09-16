"""Reading where a turn's time goes (FLOTTA-61, part 3)."""

from datetime import UTC, datetime

from flotta.latency import (
    ModelCall,
    Spread,
    TurnTiming,
    calls_between,
    parse_call,
    percentile,
    report,
)

# Verbatim from eng-r's agent.log, 2026-09-14.
SPIKE = (
    "2026-09-14 09:35:54,917 INFO [20260913_205158_59d343] agent.conversation_loop: "
    "API call #5: model=z-ai/glm-5.2 provider=openrouter in=14497 out=38 total=14535 "
    "latency=29.6s cache=10332/14497 (71%) id=gen-1789378525-N3xxZqQMSmgWDzWk7w8Q "
    "upstream=StreamLake"
)
QUICK = (
    "2026-09-14 09:36:14,432 INFO [20260913_205158_59d343] agent.conversation_loop: "
    "API call #7: model=z-ai/glm-5.2 provider=openrouter in=14991 out=50 total=15041 "
    "latency=2.2s cache=13751/14991 (92%) id=gen-1789378572-TRCN8Es1uIqqdh2XqicU "
    "upstream=StreamLake"
)
NOISE = (
    "2026-09-14 09:35:55,256 WARNING [20260913_205158_59d343] agent.tool_executor: "
    "Tool terminal returned error (0.32s): {...}"
)


def test_a_real_log_line_is_read_field_by_field():
    call = parse_call(SPIKE)
    assert call is not None
    assert call.latency_s == 29.6
    assert call.upstream == "StreamLake"
    assert call.model == "z-ai/glm-5.2"
    assert (call.tokens_in, call.tokens_out) == (14497, 38)
    assert call.cached_tokens == 10332
    assert round(call.cache_pct) == 71
    assert call.number == 5
    expected = datetime(2026, 9, 14, 9, 35, 54, 917000, tzinfo=UTC).timestamp()
    assert abs(call.at - expected) < 0.001


def test_lines_that_are_not_model_calls_are_skipped():
    assert parse_call(NOISE) is None
    assert parse_call("") is None
    assert parse_call("garbage") is None


def test_a_call_with_no_upstream_falls_back_to_the_provider():
    """A provider called directly has no router, so no `upstream`."""
    line = SPIKE.replace(" upstream=StreamLake", "")
    assert parse_call(line).upstream == "openrouter"


def test_a_call_with_no_cache_figure_has_no_cache_percentage():
    line = SPIKE.replace(" cache=10332/14497 (71%)", "")
    call = parse_call(line)
    assert call.cached_tokens is None
    assert call.cache_pct is None


def test_only_calls_inside_the_window_count():
    spike, quick = parse_call(SPIKE), parse_call(QUICK)
    window = calls_between([QUICK, NOISE, SPIKE], spike.at - 1, spike.at + 1)
    assert window == [spike]
    both = calls_between([QUICK, SPIKE], spike.at, quick.at)
    assert [c.number for c in both] == [5, 7], "calls are ordered by time"


def test_percentiles_are_nearest_rank_and_honest_about_no_data():
    assert percentile([], 95) is None
    assert percentile([3.0], 95) == 3.0
    values = [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 29.6]
    assert percentile(values, 50) == 2.4
    # The whole point: one spike in ten is the p95.
    assert percentile(values, 95) == 29.6


def _call(latency, upstream="DeepInfra", cached=13000, tokens_in=14000, at=0.0):
    return ModelCall(
        at=at,
        session="s",
        number=1,
        model="m",
        upstream=upstream,
        tokens_in=tokens_in,
        tokens_out=40,
        latency_s=latency,
        cached_tokens=cached,
    )


def test_the_report_separates_upstreams_and_cache_temperatures():
    calls = [
        _call(2.1, "DeepInfra", at=1),
        _call(2.3, "DeepInfra", at=2),
        _call(29.6, "StreamLake", cached=5000, at=3),
        _call(2.2, "DeepInfra", at=4),
    ]
    turns = [TurnTiming("hey", 0.4, 2.6, 3.0, 0)]
    text = report(turns, calls, title="eng-g")

    assert "| upstream DeepInfra | 3 |" in text
    assert "| upstream StreamLake | 1 | 29.6s | 29.6s | 29.6s |" in text
    assert "| cache < 50% | 1 | 29.6s |" in text
    assert "Upstream changed between consecutive calls 2 time(s)." in text
    # The slowest call leads its table.
    slowest = text.split("**Slowest calls**")[1]
    assert slowest.index("29.6s") < slowest.index("2.3s")


def test_a_turn_that_never_showed_text_is_not_counted_as_instant():
    turns = [TurnTiming("run it", None, None, 12.0, 1)]
    text = report(turns, [], title="t")
    assert "| answer starts | 0 | – | – | – |" in text
    assert "| turn done | 1 | 12.0s | 12.0s | 12.0s |" in text


def test_spread_rows_render_missing_values_as_dashes():
    assert Spread.of([]).row("x") == "| x | 0 | – | – | – |"
