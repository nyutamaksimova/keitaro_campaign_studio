import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.models import CampaignEditor


class EditorTracker:
    def __init__(self):
        self.calls = []
        self.streams = [
            {
                "id": 10,
                "campaign_id": 1,
                "name": "Geo",
                "type": "regular",
                "schema": "redirect",
                "state": "active",
                "position": 1,
                "action_payload": "https://google.com",
                "offers": [],
            },
            {
                "id": 11,
                "campaign_id": 1,
                "name": "Offers",
                "type": "default",
                "schema": "landings",
                "state": "active",
                "position": 2,
                "offers": [{"id": 700, "offer_id": 101, "share": 100, "state": "active"}],
            },
        ]
        self.fail_stream = None
        self.timeout_stream = None
        self.fail_read_after_put = False
        self.put_seen = False
        self.campaign_state = "active"

    def __call__(self, request):
        path = request.url.path.removeprefix("/admin_api/v1")
        method = request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if path == "/offers":
            return httpx.Response(
                200, json=[{"id": i, "name": f"Offer {i}"} for i in (101, 102, 103, 104)]
            )
        if path.startswith("/offers/"):
            id = int(path.rsplit("/", 1)[1])
            return httpx.Response(
                200 if id in (101, 102, 103, 104) else 404, json={"id": id, "name": f"Offer {id}"}
            )
        if path == "/campaigns":
            offset, limit = (
                int(request.url.params.get("offset", 0)),
                int(request.url.params.get("limit", 20)),
            )
            rows = [{"id": i, "name": f"Campaign {i}", "state": "active"} for i in range(1, 4)]
            return httpx.Response(200, json=rows[offset : offset + limit])
        if path in ("/campaigns/1/enable", "/campaigns/1/disable"):
            assert method == "POST"
            self.campaign_state = "active" if path.endswith("/enable") else "disabled"
            return httpx.Response(200, json={"id": 1, "state": self.campaign_state})
        if path == "/campaigns/1":
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "name": "Campaign 1",
                    "state": self.campaign_state,
                    "token": "must-not-expose",
                },
            )
        if path == "/campaigns/1/streams":
            return httpx.Response(200, json=deepcopy(self.streams))
        if path.startswith("/streams/"):
            stream_id = int(path.rsplit("/", 1)[1])
            stream = next((s for s in self.streams if s["id"] == stream_id), None)
            if not stream:
                return httpx.Response(404, json={})
            if method == "PUT":
                self.put_seen = True
                assert set(body) == {"offers"}
                if stream_id == self.fail_stream:
                    return httpx.Response(500, json={"error": "failure"})
                stream["offers"] = deepcopy(body["offers"])
                if stream_id == self.timeout_stream:
                    raise httpx.ReadTimeout("lost response", request=request)
            elif self.put_seen and self.fail_read_after_put:
                raise httpx.ReadTimeout("confirmation unavailable", request=request)
            return httpx.Response(200, json=deepcopy(stream))
        return httpx.Response(404, json={})


@pytest.fixture
def editor():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    tracker = EditorTracker()
    settings = Settings(_env_file=None, database_url="sqlite://", keitaro_api_key="test")
    with TestClient(create_app(settings, engine, httpx.MockTransport(tracker))) as client:
        yield client, tracker
    engine.dispose()


def load(client):
    response = client.get("/api/editor/campaigns/1")
    assert response.status_code == 200, response.text
    return response.json()


def command(client, state, action, **kwargs):
    return client.post(
        "/api/editor/campaigns/1/actions",
        json={
            "revision": state["revision"],
            "action": action,
            **kwargs,
        },
    )


def stream(state, id=11):
    return next(s for s in state["draft"]["streams"] if s["id"] == id)


def test_campaign_pages_and_paginated_api(editor):
    client, _ = editor
    assert client.get("/campaigns").status_code == 200
    assert client.get("/campaigns/1").status_code == 200
    response = client.get("/api/campaigns?offset=0&limit=2").json()
    assert [c["id"] for c in response["items"]] == [1, 2]
    assert response["has_more"]
    assert client.get("/api/campaigns?q=3").json()["items"][0]["id"] == 3


def test_draft_persists_without_writes_and_uses_offer_id_not_association_id(editor):
    client, tracker = editor
    state = load(client)
    assert stream(state)["offers"][0]["offer_id"] == 101
    assert "must-not-expose" not in json.dumps(state)
    response = command(client, state, "add", stream_id=11, offer_id=102)
    assert response.status_code == 200, response.text
    state = load(client)
    assert [o["share"] for o in stream(state)["offers"]] == [50, 50]
    assert state["dirty"]
    assert not [c for c in tracker.calls if c[0] != "GET"]


def test_archive_restore_pin_and_discard(editor):
    client, _ = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    state = command(client, state, "pin", stream_id=11, offer_id=101, locked=True, share=70).json()
    assert [o["share"] for o in stream(state)["offers"]] == [70, 30]
    state = command(client, state, "remove", stream_id=11, offer_id=102).json()
    # Removing the last free row with an incomplete fixed total requires fixing the pin first.
    assert "detail" in state
    state = load(client)
    state = command(client, state, "pin", stream_id=11, offer_id=101, locked=False).json()
    state = command(client, state, "remove", stream_id=11, offer_id=101).json()
    assert state["archive"][0]["offer_id"] == 101
    state = command(client, state, "restore", stream_id=11, offer_id=101).json()
    assert not state["archive"]
    state = command(client, state, "discard").json()
    assert [o["offer_id"] for o in stream(state)["offers"]] == [101]
    assert not state["dirty"]


def test_revision_and_readonly_stream_protection(editor):
    client, _ = editor
    old = load(client)
    assert command(client, old, "add", stream_id=11, offer_id=102).status_code == 200
    assert command(client, old, "discard").status_code == 409
    assert command(client, load(client), "add", stream_id=10, offer_id=102).status_code == 422
    assert command(client, load(client), "add", stream_id=999, offer_id=102).status_code == 404


def test_refresh_archives_remote_removal_and_can_restore(editor):
    client, tracker = editor
    state = load(client)
    tracker.streams[1]["offers"] = []
    state = command(client, state, "refresh").json()
    assert not stream(state)["offers"]
    assert state["archive"][0]["reason"] == "remote_removed"
    state = command(client, state, "discard").json()
    assert state["archive"][0]["offer_id"] == 101
    state = command(client, state, "restore", stream_id=11, offer_id=101).json()
    assert state["dirty"]
    assert not [c for c in tracker.calls if c[0] == "PUT"]


def test_publish_sends_only_changed_offers_then_becomes_clean(editor):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    response = command(client, state, "publish")
    assert response.status_code == 200, response.text
    state = response.json()
    assert not state["dirty"]
    puts = [c for c in tracker.calls if c[0] == "PUT"]
    assert len(puts) == 1
    assert puts[0] == (
        "PUT",
        "/streams/11",
        {
            "offers": [
                {"offer_id": 101, "share": 50, "state": "active"},
                {"offer_id": 102, "share": 50, "state": "active"},
            ]
        },
    )
    assert command(client, state, "publish").status_code == 200
    assert len([c for c in tracker.calls if c[0] == "PUT"]) == 1


def test_publish_rejects_external_changes_then_refresh_merges(editor):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    tracker.streams[1]["offers"][0]["share"] = 70
    response = command(client, state, "publish")
    assert response.status_code == 409
    assert not [c for c in tracker.calls if c[0] == "PUT"]
    state = command(client, load(client), "refresh").json()
    assert state["conflicts"]
    state = command(client, state, "refresh").json()
    assert state["conflicts"]
    for conflict in list(state["conflicts"]):
        state = command(client, state, "resolve", conflict_id=conflict["id"], choice="local").json()
    assert not state["conflicts"]


def test_successful_publish_keeps_pins_on_later_discard(editor):
    client, _ = editor
    state = command(
        client, load(client), "pin", stream_id=11, offer_id=101, share=100, locked=True
    ).json()
    state = command(client, state, "publish").json()
    state = command(client, state, "pin", stream_id=11, offer_id=101, locked=False).json()
    state = command(client, state, "discard").json()
    assert stream(state)["offers"][0]["locked"]


def test_partial_publish_retains_confirmed_progress_and_remaining_draft(editor):
    client, tracker = editor
    tracker.streams.append({**deepcopy(tracker.streams[1]), "id": 12, "name": "Second"})
    state = load(client)
    state = command(client, state, "add", stream_id=11, offer_id=102).json()
    state = command(client, state, "add", stream_id=12, offer_id=103).json()
    tracker.fail_stream = 12
    response = command(client, state, "publish")
    assert response.status_code == 502
    state = load(client)
    assert state["status"] == "partial"
    assert [o["offer_id"] for o in stream({"draft": state["base"]})["offers"]] == [101, 102]
    assert state["dirty"]
    assert [o["offer_id"] for o in stream(state, 12)["offers"]] == [101, 103]


def test_timeout_confirmed_by_readback_does_not_repeat_put(editor):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    tracker.timeout_stream = 11
    response = command(client, state, "publish")
    assert response.status_code == 200, response.text
    assert not response.json()["dirty"]
    assert len([c for c in tracker.calls if c[0] == "PUT"]) == 1


@pytest.mark.parametrize("put_timeout", [False, True])
def test_failed_confirmation_preserves_uncertainty_until_refresh(editor, put_timeout):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    tracker.fail_read_after_put = True
    tracker.timeout_stream = 11 if put_timeout else None
    assert command(client, state, "publish").status_code == 502
    state = load(client)
    assert state["publish_result"]["uncertain"]
    state = command(client, state, "pin", stream_id=11, offer_id=101, share=50, locked=True).json()
    assert command(client, state, "publish").status_code == 409
    assert len([c for c in tracker.calls if c[0] == "PUT"]) == 1


@pytest.mark.parametrize("deleted", [True, False])
def test_offer_conflict_then_removed_or_readonly_stream_remains_resolvable(editor, deleted):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    tracker.streams[1]["offers"][0]["share"] = 70
    state = command(client, state, "refresh").json()
    assert state["conflicts"]
    if deleted:
        tracker.streams.pop()
    else:
        tracker.streams[1].update(schema="redirect", offers=[])
    state = command(client, state, "refresh").json()
    stream_conflict = next(c for c in state["conflicts"] if c["kind"] == "stream")
    response = command(client, state, "resolve", conflict_id=stream_conflict["id"], choice="remote")
    assert response.status_code == 200, response.text
    assert not response.json()["conflicts"]
    assert {o["offer_id"] for o in response.json()["archive"]} == {101}


def test_draft_only_offer_is_removed_without_archiving_or_pending_changes(editor):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    state = command(client, state, "remove", stream_id=11, offer_id=102).json()
    assert not state["archive"]
    assert not state["dirty"]
    state = command(client, state, "publish").json()
    assert not state["dirty"]
    assert not [c for c in tracker.calls if c[0] == "PUT"]
    state = command(client, state, "discard").json()
    assert not state["archive"]


def test_offer_added_then_published_is_archived_on_removal(editor):
    client, _ = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    state = command(client, state, "publish").json()
    state = command(client, state, "remove", stream_id=11, offer_id=102).json()
    assert [o["offer_id"] for o in state["archive"]] == [102]
    assert state["dirty"]


def test_restored_saved_offer_returns_to_archive_if_removed_again(editor):
    client, tracker = editor
    state = load(client)
    tracker.streams[1]["offers"] = []
    state = command(client, state, "refresh").json()
    state = command(client, state, "restore", stream_id=11, offer_id=101).json()
    state = command(client, state, "remove", stream_id=11, offer_id=101).json()
    assert [o["offer_id"] for o in state["archive"]] == [101]


def test_legacy_unsaved_archive_does_not_reappear_after_publication(editor):
    client, _ = editor
    load(client)
    with client.app.state.sessions() as session:
        saved = session.query(CampaignEditor).one()
        saved.archive = [
            {
                "stream_id": 11,
                "offer_id": 102,
                "name": "Draft only",
                "state": "active",
                "share": 50,
                "locked": False,
                "committed": False,
                "reason": "local_removed",
            }
        ]
        session.commit()
    state = load(client)
    assert not state["archive"]
    response = command(client, state, "publish")
    assert response.status_code == 200
    assert not response.json()["archive"]


def test_campaign_disable_is_draft_until_publish_and_does_not_change_streams(editor):
    client, tracker = editor
    response = command(client, load(client), "disable")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["draft"]["campaign"]["state"] == "disabled"
    assert state["dirty"]
    assert not [c for c in tracker.calls if c[0] != "GET"]
    state = command(client, state, "publish").json()
    assert tracker.campaign_state == "disabled"
    assert not state["dirty"]
    assert [c[:2] for c in tracker.calls if c[0] != "GET"] == [("POST", "/campaigns/1/disable")]
    state = command(client, state, "enable").json()
    state = command(client, state, "publish").json()
    assert tracker.campaign_state == "active"
    assert not state["dirty"]


def test_campaign_state_survives_refresh_and_can_be_discarded(editor):
    client, tracker = editor
    state = command(client, load(client), "disable").json()
    state = command(client, state, "refresh").json()
    assert state["draft"]["campaign"]["state"] == "disabled"
    state = command(client, state, "discard").json()
    assert state["draft"]["campaign"]["state"] == "active"
    assert not state["dirty"]
    assert tracker.campaign_state == "active"


def test_publish_checks_external_campaign_status_before_any_stream_write(editor):
    client, tracker = editor
    state = command(client, load(client), "add", stream_id=11, offer_id=102).json()
    tracker.campaign_state = "disabled"
    assert command(client, state, "publish").status_code == 409
    assert not [c for c in tracker.calls if c[0] != "GET"]
