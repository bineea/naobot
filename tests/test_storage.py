from naobot.models import Action, Routine
from naobot.storage import MemoryStore, RoutineStore


def test_memory_requires_confirmation(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    item = store.suggest("用户喜欢被称呼为老板")
    assert not item.confirmed
    confirmed = store.confirm(item.id)
    assert confirmed.confirmed
    store.delete(item.id)
    assert store.list() == []


def test_routine_rejects_unsafe_action(tmp_path) -> None:
    store = RoutineStore(tmp_path)
    routine = Routine(name="危险动作", trigger="touch_head", actions=[Action(name="flip")])
    try:
        store.suggest(routine)
    except ValueError as exc:
        assert "未知" in str(exc)
    else:
        raise AssertionError("unsafe routine should be rejected")


def test_routine_confirmation(tmp_path) -> None:
    store = RoutineStore(tmp_path)
    routine = store.suggest(
        Routine(name="开心打招呼", trigger="double_touch_head", actions=[Action(name="wave")])
    )
    assert not routine.enabled
    confirmed = store.confirm(routine.id)
    assert confirmed.enabled
