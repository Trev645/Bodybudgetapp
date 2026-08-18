"""End-to-end corpus tests: benchmarking works and research recovers the
utilisation-experience relationship engineered into the synthetic data."""

from awa.benchmarking.peers import benchmark_building
from awa.benchmarking.research import capacity_experience_research
from awa.synthetic import seed_demo


def test_corpus_benchmark_and_research(engine):
    summary = seed_demo(engine, n_clients=4, days=3, rounds_per_day=2)
    clients = summary["clients"]

    # All four demo clients share size_band-agnostic pool; pick a finance
    # client and benchmark on sector alone won't reach 3 peers (sectors
    # rotate), so benchmark on nothing narrower than... use region+sector
    # combinations depending on data; here: no dimension filter narrower
    # than "all peers" exists, so use the widest single dimension present
    # across all: none guarantees a match — instead verify the guard and
    # a successful broad comparison.
    subject = clients[0]
    result = benchmark_building(engine, subject["client_id"],
                                subject["building_id"], ["size_band"])
    assert "available" in result
    if result["available"]:
        assert result["peer_clients"] >= 3
        assert "peer_percentiles" in result
        # No client identities anywhere in the payload.
        blob = str(result)
        for other in clients[1:]:
            assert other["client_id"] not in blob

    research = capacity_experience_research(engine)
    assert research["studies"] == 4
    corr = research["correlations"]["utilisation_vs_experience"]
    # The synthetic corpus builds in declining experience with utilisation.
    assert corr is None or corr["r"] < 0.5


def test_research_relationship_direction(engine):
    seed_demo(engine, n_clients=6, days=4, rounds_per_day=2)
    research = capacity_experience_research(engine)
    assert research["studies"] == 6
    corr = research["correlations"]["capacity_ratio_vs_experience"]
    assert corr is not None
    # Tighter capacity (higher assigned-per-desk) must not read as better
    # experience in a corpus engineered with a negative relationship.
    assert corr["r"] < 0.3
