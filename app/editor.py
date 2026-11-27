"""Persistent editor commands. Only publish() writes to the tracker."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.editor_logic import (
    archive_offer,
    canonical_stream,
    distribute,
    merge_state,
    offer_value,
    pins,
    snapshot_value,
    validate_distribution,
    with_history,
    with_pins,
)
from app.keitaro import KeitaroError
from app.models import CampaignEditor


def scope(tracker):
    settings = tracker.settings
    return sha256(
        (settings.keitaro_base_url + "\0" + settings.keitaro_api_key.get_secret_value()).encode()
    ).hexdigest()


def positive_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) <= 2147483647:
        raise KeitaroError("Keitaro вернул некорректный ID.")
    return int(value)


def normalize_stream(raw, campaign_id, names):
    if not isinstance(raw, dict):
        raise KeitaroError("Некорректный формат потока Keitaro.")
    if positive_id(raw.get("campaign_id")) != campaign_id:
        raise KeitaroError("Поток Keitaro не принадлежит выбранной кампании.")
    offers = raw.get("offers", [])
    if not isinstance(offers, list):
        raise KeitaroError("Некорректный список офферов потока.")
    rows = []
    for row in offers:
        if not isinstance(row, dict):
            raise KeitaroError("Некорректный оффер потока.")
        oid = positive_id(row.get("offer_id"))
        share = row.get("share", 0)
        if type(share) is not int or not 0 <= share <= 100:
            raise KeitaroError("Keitaro вернул нецелую или некорректную долю оффера.")
        state = row.get("state", "active")
        if state not in ("active", "disabled"):
            raise KeitaroError("Keitaro вернул неизвестное состояние оффера.")
        rows.append(
            {
                "offer_id": oid,
                "name": names.get(oid, f"Оффер #{oid}"),
                "share": share,
                "state": state,
                "locked": False,
                "persisted": True,
            }
        )
    if len({o["offer_id"] for o in rows}) != len(rows):
        raise KeitaroError("В потоке Keitaro обнаружены повторяющиеся офферы.")
    return {
        "id": positive_id(raw.get("id")),
        "campaign_id": campaign_id,
        "name": str(raw.get("name", "")),
        "schema": str(raw.get("schema", "")),
        "type": str(raw.get("type", "")),
        "state": str(raw.get("state", "active")),
        "position": raw.get("position", 0),
        "action_type": str(raw.get("action_type") or ""),
        "action_payload": str(raw.get("action_payload") or ""),
        "offers": rows,
    }


def read_remote(tracker, campaign_id):
    try:
        campaign = tracker.request("GET", f"/campaigns/{campaign_id}")
        streams = tracker.request("GET", f"/campaigns/{campaign_id}/streams")
    except KeitaroError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, "Кампания не найдена в Keitaro.") from exc
        raise
    if not isinstance(campaign, dict) or positive_id(campaign.get("id")) != campaign_id:
        raise KeitaroError("Некорректный ответ Keitaro при чтении кампании.")
    if not isinstance(streams, list):
        raise KeitaroError("Некорректный список потоков Keitaro.")
    catalog = tracker.catalog("/offers")
    names = {positive_id(o.get("id")): str(o.get("name", "")) for o in catalog}
    normalized = [normalize_stream(s, campaign_id, names) for s in streams]
    if len({s["id"] for s in normalized}) != len(normalized):
        raise KeitaroError("Keitaro вернул повторяющиеся потоки.")
    return {
        "campaign": {
            "id": campaign_id,
            "name": str(campaign.get("name", "")),
            "state": str(campaign.get("state", "active")),
        },
        "streams": sorted(normalized, key=lambda s: (s["position"], s["id"])),
    }


def find_stream(snapshot, stream_id):
    return next((s for s in snapshot["streams"] if s["id"] == stream_id), None)


def editable(stream):
    return stream and stream["schema"] == "landings" and not stream.get("missing")


def saved_pins(editor):
    return pins(with_pins(editor.base, editor.base_pins))


def historical_archive(editor):
    known = {(s["id"], o["offer_id"]) for s in editor.base["streams"] for o in s["offers"]}
    return [
        deepcopy(o)
        for o in editor.archive
        if o.get("persisted", o.get("committed") or (o["stream_id"], o["offer_id"]) in known)
    ]


def view(editor):
    draft = deepcopy(editor.draft)
    active = {(s["id"], o["offer_id"]) for s in draft["streams"] for o in s["offers"]}
    archive = [
        o for o in historical_archive(editor) if (o["stream_id"], o["offer_id"]) not in active
    ]
    for sid in dict.fromkeys(o["stream_id"] for o in archive):
        if not find_stream(draft, sid):
            draft["streams"].append(
                {
                    "id": sid,
                    "name": f"Удалённый поток #{sid}",
                    "missing": True,
                    "schema": "",
                    "type": "",
                    "state": "deleted",
                    "offers": [],
                }
            )
    errors = [
        {"stream_id": s["id"], "message": validate_distribution(s["offers"])}
        for s in draft["streams"]
        if editable(s) and validate_distribution(s["offers"])
    ]
    dirty = snapshot_value(editor.base) != snapshot_value(editor.draft) or saved_pins(
        editor
    ) != pins(editor.draft)
    dirty = dirty or any(not item.get("committed") for item in archive)
    dirty = dirty or editor.draft["campaign"]["state"] != editor.base["campaign"]["state"]
    return {
        "campaign_id": editor.campaign_id,
        "revision": editor.revision,
        "base": editor.base,
        "base_pins": saved_pins(editor),
        "draft": draft,
        "archive": archive,
        "conflicts": editor.conflicts,
        "changes": editor.changes,
        "errors": errors,
        "dirty": dirty,
        "status": editor.status,
        "error": editor.error,
        "publish_result": editor.publish_result,
        "updated_at": editor.updated_at,
    }


def save(session, editor):
    editor.updated_at = datetime.now(timezone.utc)
    session.commit()


def get_editor(session, tracker, campaign_id, lock=False):
    query = (
        select(CampaignEditor)
        .where(CampaignEditor.scope == scope(tracker), CampaignEditor.campaign_id == campaign_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update()
    editor = session.scalar(query)
    if editor:
        return editor
    remote = read_remote(tracker, campaign_id)
    editor = CampaignEditor(
        scope=scope(tracker),
        campaign_id=campaign_id,
        base=remote,
        draft=deepcopy(remote),
        archive=[],
        conflicts=[],
        changes=[],
        base_pins={},
        status="draft",
        publish_result={},
    )
    session.add(editor)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
    return session.scalar(query.with_for_update() if lock else query)


def check_revision(editor, revision):
    if editor.revision != revision:
        raise HTTPException(409, "Черновик изменён в другой вкладке. Перезагрузите редактор.")


def apply_command(session, tracker, editor, command):
    check_revision(editor, command.revision)
    action = command.action
    if action == "publish":
        return publish(session, tracker, editor)
    archive = historical_archive(editor)
    draft = with_history(editor.draft, editor.base, archive)
    conflicts = deepcopy(editor.conflicts)
    if action in ("enable", "disable"):
        if draft["campaign"]["state"] not in ("active", "disabled"):
            raise HTTPException(422, "Состояние этой кампании нельзя изменить редактором.")
        draft["campaign"]["state"] = "active" if action == "enable" else "disabled"
    elif action == "refresh":
        remote = read_remote(tracker, editor.campaign_id)
        draft, archive, conflicts, changes = merge_state(
            editor.base, draft, remote, archive, conflicts, editor.base_pins
        )
        editor.base = remote
        editor.base_pins = pins(with_pins(remote, editor.base_pins))
        editor.changes = changes
    elif action == "discard":
        draft = with_pins(editor.base, editor.base_pins)
        archive = [o for o in archive if o.get("committed")]
        conflicts = []
        editor.changes = []
    elif action == "resolve":
        entry = next((c for c in conflicts if c["id"] == command.conflict_id), None)
        if entry is None or command.choice not in ("local", "remote"):
            raise HTTPException(422, "Выберите существующий конфликт и вариант решения.")
        resolve(draft, archive, entry, command.choice)
        if entry["kind"] == "stream" and command.choice == "remote":
            for previous in conflicts:
                if (
                    previous["stream_id"] == entry["stream_id"]
                    and previous["kind"] == "offer"
                    and previous["local"]
                ):
                    archive_offer(
                        archive,
                        entry["stream_id"],
                        previous["local"],
                        "stream_removed" if entry["remote"] is None else "remote_removed",
                        True,
                    )
            conflicts = [c for c in conflicts if c["stream_id"] != entry["stream_id"]]
        else:
            conflicts = [c for c in conflicts if c["id"] != entry["id"]]
    else:
        stream = find_stream(draft, command.stream_id)
        if stream is None:
            raise HTTPException(404, "Поток не найден в выбранной кампании.")
        if not editable(stream):
            raise HTTPException(422, "Этот поток доступен только для просмотра.")
        if any(c["stream_id"] == stream["id"] for c in conflicts):
            raise HTTPException(409, "Сначала разрешите конфликты этого потока.")
        mutate_stream(tracker, stream, archive, command)
    editor.draft, editor.archive, editor.conflicts = draft, archive, conflicts
    editor.status = "draft"
    if action in ("refresh", "discard") or not editor.publish_result.get("uncertain"):
        editor.error, editor.publish_result = None, {}
    save(session, editor)
    return view(editor)


def mutate_stream(tracker, stream, archive, command):
    oid, action = command.offer_id, command.action
    row = next((o for o in stream["offers"] if o["offer_id"] == oid), None)
    if action in ("add", "restore"):
        if oid is None or row:
            raise HTTPException(422, "Укажите ID; оффер не должен повторяться внутри потока.")
        if action == "restore" and not any(
            o["stream_id"] == stream["id"] and o["offer_id"] == oid for o in archive
        ):
            raise HTTPException(404, "Оффер не найден в архиве этого потока.")
        try:
            offer = tracker.offer(oid)
        except KeitaroError as exc:
            if exc.status_code == 404:
                raise HTTPException(422, "Оффер с таким ID не найден в Keitaro.") from exc
            raise
        stream["offers"].append(
            {
                "offer_id": oid,
                "name": offer["name"],
                "share": 0,
                "state": "active",
                "locked": False,
                "persisted": any(
                    o["stream_id"] == stream["id"]
                    and o["offer_id"] == oid
                    and o.get("persisted", o.get("committed", False))
                    for o in archive
                ),
            }
        )
    elif action == "remove":
        if row is None:
            raise HTTPException(404, "Оффер не найден в потоке.")
        archive_offer(archive, stream["id"], row, "local_removed")
        stream["offers"] = [o for o in stream["offers"] if o["offer_id"] != oid]
    elif action == "pin":
        if row is None or command.locked is None or row["state"] != "active":
            raise HTTPException(422, "Фиксация доступна для активного оффера.")
        if command.locked and command.share is None:
            raise HTTPException(422, "Укажите закрепляемую долю.")
        row["locked"] = command.locked
        if command.locked:
            row["share"] = command.share
    elif action != "equalize":
        raise HTTPException(422, "Неизвестное действие редактора.")
    try:
        stream["offers"] = distribute(stream["offers"])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def resolve(draft, archive, entry, choice):
    if entry["kind"] == "campaign":
        selected_state = entry[choice]["state"]
        if selected_state not in ("active", "disabled"):
            raise HTTPException(422, "Недопустимое состояние кампании.")
        draft["campaign"]["state"] = selected_state
        return
    stream = find_stream(draft, entry["stream_id"])
    selected = deepcopy(entry[choice])
    if entry["kind"] == "stream":
        if choice == "local":
            if not editable(entry["remote"]):
                raise HTTPException(
                    422, "Удалённый или redirect-поток нельзя восстановить редактором."
                )
            selected = {**deepcopy(entry["remote"]), "offers": selected["offers"]}
        if selected is None:
            if stream:
                stream.update(missing=True, offers=[])
        else:
            draft["streams"] = [s for s in draft["streams"] if s["id"] != entry["stream_id"]]
            draft["streams"].append(selected)
        return
    if not editable(stream):
        raise HTTPException(422, "Поток удалён или сменил схему; примите внешнее состояние потока.")
    old = next((o for o in stream["offers"] if o["offer_id"] == entry["offer_id"]), None)
    stream["offers"] = [o for o in stream["offers"] if o["offer_id"] != entry["offer_id"]]
    if selected:
        selected["locked"] = selected.get("locked", False) if choice == "local" else False
        stream["offers"].append(selected)
    elif old:
        archive_offer(archive, stream["id"], old, "local_removed")


def _replace_stream(snapshot, stream):
    result = deepcopy(snapshot)
    result["streams"] = [stream if s["id"] == stream["id"] else s for s in result["streams"]]
    return result


def publish(session, tracker, editor):
    current_view = view(editor)
    if editor.conflicts or editor.status == "uncertain" or editor.publish_result.get("uncertain"):
        raise HTTPException(409, "Обновите данные и разрешите конфликты перед отправкой.")
    if current_view["errors"]:
        raise HTTPException(422, current_view["errors"][0]["message"])
    remote = read_remote(tracker, editor.campaign_id)
    if (
        snapshot_value(remote) != snapshot_value(editor.base)
        or remote["campaign"]["state"] != editor.base["campaign"]["state"]
    ):
        raise HTTPException(409, "Кампания изменилась в Keitaro. Нажмите «Обновить из Keitaro».")
    pending = [
        s
        for s in editor.draft["streams"]
        if editable(s)
        and canonical_stream(s) != canonical_stream(find_stream(editor.base, s["id"]))
    ]
    if pending:
        catalog = tracker.request("GET", "/offers")  # Fresh validation before any PUT.
        if not isinstance(catalog, list) or not all(isinstance(o, dict) for o in catalog):
            raise KeitaroError("Некорректный каталог офферов Keitaro.")
        available = {positive_id(o.get("id")) for o in catalog}
        required = {o["offer_id"] for s in pending for o in s["offers"]}
        missing = required - available
        if missing:
            raise HTTPException(422, f"Офферы отсутствуют в Keitaro: {sorted(missing)}.")
    confirmed = []
    expected_revision = editor.revision
    for target in pending:
        editor = get_editor(session, tracker, editor.campaign_id, lock=True)
        check_revision(editor, expected_revision)
        sid = target["id"]
        names = {o["offer_id"]: o["name"] for o in target["offers"]}
        put_dispatched = False
        try:
            actual = normalize_stream(
                tracker.request("GET", f"/streams/{sid}"), editor.campaign_id, names
            )
            if canonical_stream(actual) != canonical_stream(find_stream(editor.base, sid)):
                raise HTTPException(409, f"Поток #{sid} изменён извне. Обновите данные.")
            put_error = None
            try:
                put_dispatched = True
                tracker.request(
                    "PUT",
                    f"/streams/{sid}",
                    json={
                        "offers": [offer_value(o) for o in target["offers"]],
                    },
                )
            except KeitaroError as exc:
                if not exc.uncertain:
                    put_dispatched = False
                    raise
                put_error = exc
            actual = normalize_stream(
                tracker.request("GET", f"/streams/{sid}"), editor.campaign_id, names
            )
            if canonical_stream(actual) != canonical_stream(target):
                if put_error:
                    raise put_error
                raise KeitaroError("Keitaro не подтвердил ожидаемый состав потока.", uncertain=True)
            put_dispatched = False  # Matching readback is the only successful confirmation.
            fixed = {str(o["offer_id"]): o["share"] for o in target["offers"] if o.get("locked")}
            editor.base = _replace_stream(editor.base, actual)
            editor.draft = _replace_stream(
                editor.draft, with_pins({"streams": [actual]}, {str(sid): fixed})["streams"][0]
            )
            editor.base_pins = {**editor.base_pins, str(sid): fixed}
            editor.archive = [
                {**o, "committed": True} if o["stream_id"] == sid else o
                for o in historical_archive(editor)
            ]
            confirmed.append(sid)
            editor.publish_result = {
                "confirmed": list(confirmed),
                "remaining": [s["id"] for s in pending if s["id"] not in confirmed],
            }
            editor.status, editor.error = "partial", None
            save(session, editor)
            expected_revision = editor.revision
        except (KeitaroError, HTTPException) as exc:
            uncertain = put_dispatched or bool(getattr(exc, "uncertain", False))
            editor.status = "partial" if confirmed else "uncertain" if uncertain else "error"
            editor.error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            editor.publish_result = {
                "confirmed": confirmed,
                "uncertain": uncertain,
                "remaining": [s["id"] for s in pending if s["id"] not in confirmed],
            }
            save(session, editor)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, editor.error) from exc
        except SQLAlchemyError as exc:
            session.rollback()
            raise HTTPException(
                503,
                f"Ошибка записи после отправки потока #{sid}. "
                "Не повторяйте отправку; обновите данные из Keitaro.",
            ) from exc
    editor = get_editor(session, tracker, editor.campaign_id, lock=True)
    check_revision(editor, expected_revision)
    if editor.draft["campaign"]["state"] != editor.base["campaign"]["state"]:
        publish_campaign_state(session, tracker, editor, confirmed)
    editor.base_pins = pins(editor.draft)
    editor.archive = [{**item, "committed": True} for item in historical_archive(editor)]
    editor.status, editor.error, editor.changes = "saved", None, []
    editor.publish_result = {"confirmed": confirmed, "remaining": []}
    save(session, editor)
    return view(editor)


def publish_campaign_state(session, tracker, editor, confirmed):
    desired = editor.draft["campaign"]["state"]
    if desired not in ("active", "disabled"):
        raise HTTPException(422, "Недопустимое состояние кампании.")
    dispatched = False
    try:
        actual = tracker.request("GET", f"/campaigns/{editor.campaign_id}")
        if not isinstance(actual, dict) or str(actual.get("id")) != str(editor.campaign_id):
            raise KeitaroError("Некорректный ответ при проверке состояния кампании.")
        if actual.get("state") != editor.base["campaign"]["state"]:
            raise HTTPException(
                409, "Состояние кампании изменено извне. Обновите данные из Keitaro."
            )
        action = "enable" if desired == "active" else "disable"
        dispatched = True
        try:
            tracker.request("POST", f"/campaigns/{editor.campaign_id}/{action}")
        except KeitaroError as exc:
            if not exc.uncertain:
                dispatched = False
                raise
        actual = tracker.request("GET", f"/campaigns/{editor.campaign_id}")
        if (
            not isinstance(actual, dict)
            or str(actual.get("id")) != str(editor.campaign_id)
            or actual.get("state") != desired
        ):
            raise KeitaroError(
                "Состояние кампании не подтверждено. Обновите данные.", uncertain=True
            )
        editor.base = {**editor.base, "campaign": {**editor.base["campaign"], "state": desired}}
    except (KeitaroError, HTTPException) as exc:
        uncertain = dispatched or bool(getattr(exc, "uncertain", False))
        editor.status = "partial" if confirmed else "uncertain" if uncertain else "error"
        editor.error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        editor.publish_result = {
            "confirmed": confirmed,
            "remaining": [],
            "campaign_pending": True,
            "uncertain": uncertain,
        }
        save(session, editor)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(502, editor.error) from exc
