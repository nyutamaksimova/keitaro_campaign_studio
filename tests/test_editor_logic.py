from copy import deepcopy

import pytest

from app.editor_logic import distribute, merge_state, validate_distribution


def offer(id, share=100, locked=False, state="active"):
    return {"offer_id": id, "name": f"Offer {id}", "share": share, "state": state, "locked": locked}


def snapshot(offers):
    return {
        "campaign": {"id": 1, "name": "Test", "state": "active"},
        "streams": [
            {
                "id": 10,
                "campaign_id": 1,
                "name": "Offers",
                "type": "default",
                "schema": "landings",
                "state": "active",
                "position": 1,
                "offers": offers,
            }
        ],
    }


def test_equal_integer_distribution_and_input_not_mutated():
    values = [offer(1), offer(2), offer(3)]
    assert [o["share"] for o in distribute(values)] == [34, 33, 33]
    assert values[0]["share"] == 100


def test_pins_disabled_and_empty_distribution():
    values = [offer(1, 50, True), offer(2), offer(3), offer(4, 72, state="disabled")]
    assert [o["share"] for o in distribute(values)] == [50, 25, 25, 72]
    assert distribute([]) == []
    assert [o["share"] for o in distribute([offer(1, 100, True), offer(2)])] == [100, 0]


def test_invalid_pins_are_reported_and_never_silently_changed():
    with pytest.raises(ValueError):
        distribute([offer(1, 70, True), offer(2, 40, True)])
    with pytest.raises(ValueError):
        distribute([offer(1, 80, True)])
    assert validate_distribution([offer(1, 80)])
    assert validate_distribution([offer(1, 100)]) is None


def test_remote_removal_becomes_archive_and_repeat_refresh_does_not_duplicate():
    base = snapshot([offer(1, 50), offer(2, 50)])
    remote = snapshot([offer(1)])
    merged, archive, conflicts, changes = merge_state(base, deepcopy(base), remote, [], [], {})
    assert [o["offer_id"] for o in merged["streams"][0]["offers"]] == [1]
    assert archive[0]["offer_id"] == 2
    assert archive[0]["reason"] == "remote_removed"
    assert not conflicts
    assert changes
    _, again, conflicts, _ = merge_state(remote, merged, remote, archive, [], {})
    assert len(again) == 1
    assert not conflicts


def test_merge_keeps_local_addition_and_imports_remote_addition():
    base = snapshot([offer(1)])
    local = snapshot([offer(1, 50), offer(2, 50)])
    remote = snapshot([offer(1), offer(3, 0)])
    merged, _, conflicts, _ = merge_state(base, local, remote, [], [], {})
    assert {o["offer_id"] for o in merged["streams"][0]["offers"]} == {1, 2, 3}
    assert not conflicts


def test_conflict_survives_second_refresh():
    base = snapshot([offer(1, 50), offer(2, 50)])
    local = snapshot([offer(1, 60, True), offer(2, 40)])
    remote = snapshot([offer(1, 30), offer(2, 70)])
    merged, archive, conflicts, _ = merge_state(base, local, remote, [], [], {})
    assert any(c["offer_id"] == 1 for c in conflicts)
    _, _, persisted, _ = merge_state(remote, merged, remote, archive, conflicts, {})
    assert {c["id"] for c in persisted} == {c["id"] for c in conflicts}
    assert next(c for c in persisted if c["offer_id"] == 1)["local"]["share"] == 60


def test_deleted_locally_modified_offer_is_archived_with_conflict():
    base = snapshot([offer(1)])
    local = snapshot([offer(1, 80, True), offer(2, 20)])
    remote = snapshot([])
    merged, archive, conflicts, _ = merge_state(base, local, remote, [], [], {})
    assert [o["offer_id"] for o in merged["streams"][0]["offers"]] == [2]
    assert any(o["offer_id"] == 1 for o in archive)
    assert any(c["offer_id"] == 1 and c["remote"] is None for c in conflicts)


def test_deleted_stream_is_not_recreated_and_archives_all_offers():
    base = snapshot([offer(1)])
    remote = {**deepcopy(base), "streams": []}
    local = snapshot([offer(1, 50), offer(2, 50)])
    merged, archive, conflicts, _ = merge_state(base, local, remote, [], [], {})
    assert merged["streams"][0]["missing"]
    assert {o["offer_id"] for o in archive} == {1}
    assert conflicts[0]["kind"] == "stream"
