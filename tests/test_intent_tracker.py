from naobot.intent_tracker import IntentTracker


def test_track_returns_true_for_new_id() -> None:
    tracker = IntentTracker()
    assert tracker.track("i1", deadline_ms=4000, ts_ms=100) is True
    assert tracker.status("i1") == "pending"


def test_track_returns_false_for_duplicate() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)
    assert tracker.track("i1", deadline_ms=4000, ts_ms=200) is False
    assert tracker.status("i1") == "pending"  # 重复不重置状态


def test_observe_ack_completed_removes() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)
    tracker.observe_ack("i1", "completed")
    assert tracker.status("i1") is None


def test_observe_ack_failed_removes() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)
    tracker.observe_ack("i1", "failed")
    assert tracker.status("i1") is None


def test_observe_ack_accepted_keeps_tracked() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)
    tracker.observe_ack("i1", "accepted")
    assert tracker.status("i1") == "accepted"


def test_observe_error_removes() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)
    tracker.observe_error("i1")
    assert tracker.status("i1") is None


def test_reclaim_returns_expired_intents() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)  # deadline 4100
    tracker.track("i2", deadline_ms=4000, ts_ms=100)  # deadline 4100
    tracker.observe_ack("i1", "accepted")  # 仍 pending，未终态

    expired = tracker.reclaim(current_ms=4101)

    assert set(expired) == {"i1", "i2"}
    assert tracker.status("i1") is None
    assert tracker.status("i2") is None


def test_reclaim_keeps_unexpired_intents() -> None:
    tracker = IntentTracker()
    tracker.track("i1", deadline_ms=4000, ts_ms=100)  # deadline 4100

    assert tracker.reclaim(current_ms=4000) == []
    assert tracker.status("i1") == "pending"


def test_lru_eviction_drops_oldest() -> None:
    tracker = IntentTracker(capacity=2)
    tracker.track("i1", ts_ms=100)
    tracker.track("i2", ts_ms=100)
    tracker.track("i3", ts_ms=100)  # 淘汰 i1

    assert tracker.status("i1") is None
    assert tracker.status("i2") == "pending"
    assert tracker.status("i3") == "pending"


def test_track_none_id_does_not_track() -> None:
    tracker = IntentTracker()
    assert tracker.track(None) is True
    assert len(tracker) == 0
