"""Pure distribution and three-way merge rules for campaign drafts."""

from copy import deepcopy
from datetime import datetime, timezone


def offer_value(offer):
    if offer is None:
        return None
    return {key: offer[key] for key in ("offer_id", "share", "state")}


def canonical_stream(stream):
    if stream is None or stream.get("missing"):
        return None
    return {
        "schema": stream["schema"],
        "type": stream["type"],
        "state": stream["state"],
        "offers": sorted((offer_value(o) for o in stream["offers"]), key=lambda o: o["offer_id"]),
    }


def snapshot_value(snapshot):
    return {s["id"]: canonical_stream(s) for s in snapshot["streams"] if not s.get("missing")}


def validate_distribution(offers):
    active = [o for o in offers if o["state"] == "active"]
    if not active:
        return None
    if any(type(o["share"]) is not int or not 0 <= o["share"] <= 100 for o in active):
        return "Доли должны быть целыми числами от 0 до 100."
    if sum(o["share"] for o in active if o.get("locked")) > 100:
        return "Сумма закреплённых долей превышает 100%."
    if sum(o["share"] for o in active) != 100:
        return "Сумма долей активных офферов должна быть 100%. Распределите свободные доли."
    return None


def distribute(offers):
    result = deepcopy(offers)
    active = [o for o in result if o["state"] == "active"]
    fixed = sum(o["share"] for o in active if o.get("locked"))
    if fixed > 100 or fixed < 0:
        raise ValueError("Сумма закреплённых долей превышает 100%.")
    free = [o for o in active if not o.get("locked")]
    if active and not free and fixed != 100:
        raise ValueError("Если все доли закреплены, их сумма должна быть 100%.")
    if free:
        share, remainder = divmod(100 - fixed, len(free))
        for index, item in enumerate(free):
            item["share"] = share + (index < remainder)
    return result


def pins(snapshot):
    return {
        str(s["id"]): {str(o["offer_id"]): o["share"] for o in s["offers"] if o.get("locked")}
        for s in snapshot["streams"]
        if not s.get("missing") and any(o.get("locked") for o in s["offers"])
    }


def with_pins(snapshot, base_pins):
    result = deepcopy(snapshot)
    for stream in result["streams"]:
        fixed = base_pins.get(str(stream["id"]), {})
        for offer in stream["offers"]:
            offer["locked"] = str(offer["offer_id"]) in fixed
    return result


def with_history(snapshot, base, archive):
    result = deepcopy(snapshot)
    known = {(s["id"], o["offer_id"]) for s in base["streams"] for o in s["offers"]}
    known.update(
        (o["stream_id"], o["offer_id"])
        for o in archive
        if o.get("persisted", o.get("committed", False))
    )
    for stream in result["streams"]:
        for offer in stream["offers"]:
            offer["persisted"] = bool(
                offer.get("persisted") or (stream["id"], offer["offer_id"]) in known
            )
    return result


def archive_offer(archive, stream_id, offer, reason, committed=False):
    if offer.get("persisted") is False:
        return
    previous = next(
        (o for o in archive if o["stream_id"] == stream_id and o["offer_id"] == offer["offer_id"]),
        None,
    )
    if previous:
        if committed:
            previous.update(deepcopy(offer), reason=reason, committed=True)
        return
    archive.append(
        {
            **deepcopy(offer),
            "persisted": True,
            "stream_id": stream_id,
            "reason": reason,
            "committed": committed,
            "removed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _row_changed(local, base):
    return offer_value(local) != offer_value(base) or bool(local and local.get("locked")) != bool(
        base and base.get("locked")
    )


def merge_state(base, draft, remote, archive, conflicts, base_pins):
    base = with_pins(base, base_pins)
    draft = with_history(draft, base, archive)
    remote = with_history(remote, remote, [])
    result = {"campaign": deepcopy(remote["campaign"]), "streams": []}
    archive = deepcopy(archive)
    detected = {}
    changes = []
    maps = [{s["id"]: s for s in state["streams"]} for state in (base, draft, remote)]
    before, local, after = maps

    def conflict(sid, oid, kind, b, local_stream, r):
        key = f"{sid}:{oid}" if oid is not None else f"{sid}:stream"
        detected[key] = {
            "id": key,
            "stream_id": sid,
            "offer_id": oid,
            "kind": kind,
            "base": deepcopy(b),
            "local": deepcopy(local_stream),
            "remote": deepcopy(r),
        }

    original_state = base["campaign"]["state"]
    local_state = draft["campaign"]["state"]
    remote_state = remote["campaign"]["state"]
    if local_state != original_state:
        result["campaign"]["state"] = local_state
        if remote_state != original_state and remote_state != local_state:
            detected["campaign"] = {
                "id": "campaign",
                "kind": "campaign",
                "stream_id": None,
                "offer_id": None,
                "base": deepcopy(base["campaign"]),
                "local": deepcopy(draft["campaign"]),
                "remote": deepcopy(remote["campaign"]),
            }
    if remote_state != original_state:
        changes.append({"stream_id": None, "text": "Состояние кампании изменено в Keitaro."})

    for sid in dict.fromkeys([*after, *local, *before]):
        b, local_stream, r = before.get(sid), local.get(sid), after.get(sid)
        if r is None:
            if b is None and local_stream and local_stream.get("missing"):
                result["streams"].append(deepcopy(local_stream))
                continue
            old_offers = {o["offer_id"]: o for s in (b, local_stream) if s for o in s["offers"]}
            for offer in old_offers.values():
                archive_offer(archive, sid, offer, "stream_removed", True)
            if local_stream and (
                canonical_stream(local_stream) != canonical_stream(b)
                or pins({"streams": [local_stream]}) != pins({"streams": [b] if b else []})
            ):
                conflict(sid, None, "stream", b, local_stream, None)
            if local_stream or b:
                result["streams"].append(
                    {**deepcopy(local_stream or b), "missing": True, "offers": []}
                )
            if b:
                changes.append({"stream_id": sid, "text": f"Поток #{sid} удалён в Keitaro."})
            continue
        if b is None and (local_stream is None or local_stream.get("missing")):
            result["streams"].append(deepcopy(r))
            changes.append({"stream_id": sid, "text": f"Новый поток #{sid} из Keitaro."})
            continue
        if b and (r["schema"], r["type"]) != (b["schema"], b["type"]):
            for offer in (local_stream or b)["offers"]:
                if not any(o["offer_id"] == offer["offer_id"] for o in r["offers"]):
                    archive_offer(archive, sid, offer, "remote_removed", True)
            if local_stream and (
                canonical_stream(local_stream) != canonical_stream(b)
                or pins({"streams": [local_stream]}) != pins({"streams": [b]})
            ):
                conflict(sid, None, "stream", b, local_stream, r)
            result["streams"].append(deepcopy(r))
            changes.append({"stream_id": sid, "text": f"Схема потока #{sid} изменена в Keitaro."})
            continue

        stream = deepcopy(r)
        stream["offers"] = []
        bm = {o["offer_id"]: o for o in (b or {}).get("offers", [])}
        lm = {o["offer_id"]: o for o in (local_stream or {}).get("offers", [])}
        rm = {o["offer_id"]: o for o in r["offers"]}
        for oid in dict.fromkeys([*lm, *rm, *bm]):
            bo, lo, ro = bm.get(oid), lm.get(oid), rm.get(oid)
            chosen = deepcopy(lo)
            if bo and ro is None:
                archive_offer(archive, sid, lo or bo, "remote_removed", True)
                if lo and _row_changed(lo, bo):
                    conflict(sid, oid, "offer", bo, lo, None)
                chosen = None
            elif bo and lo is None:
                if ro and offer_value(ro) != offer_value(bo):
                    conflict(sid, oid, "offer", bo, None, ro)
            elif lo and ro and bo:
                row_conflict = False
                chosen["name"] = ro["name"]
                for field in ("share", "state"):
                    if lo[field] == bo[field]:
                        chosen[field] = ro[field]
                    elif ro[field] != bo[field] and ro[field] != lo[field]:
                        row_conflict = True
                if lo.get("locked") and ro["share"] != bo["share"]:
                    row_conflict = True
                    chosen["share"] = lo["share"]
                if row_conflict:
                    conflict(sid, oid, "offer", bo, lo, ro)
            elif lo and ro:
                if offer_value(lo) != offer_value(ro):
                    conflict(sid, oid, "offer", None, lo, ro)
            elif ro:
                chosen = deepcopy(ro)
            if chosen:
                stream["offers"].append(chosen)
            if offer_value(ro) != offer_value(bo):
                action = "удалён" if ro is None else "добавлен" if bo is None else "изменён"
                changes.append(
                    {"stream_id": sid, "offer_id": oid, "text": f"Оффер #{oid} {action} в Keitaro."}
                )
        result["streams"].append(stream)

    # Never let a second refresh silently resolve an earlier conflict.
    for previous in conflicts:
        entry = deepcopy(previous)
        if entry["kind"] == "campaign":
            entry["remote"] = deepcopy(remote["campaign"])
            detected[entry["id"]] = entry
            continue
        if entry["kind"] == "offer" and entry["local"]:
            entry["local"].setdefault("persisted", entry["base"] is not None)
        stream = after.get(entry["stream_id"])
        if entry["kind"] == "offer" and (stream is None or stream["schema"] != "landings"):
            sid = entry["stream_id"]
            if entry["local"]:
                archive_offer(
                    archive,
                    sid,
                    entry["local"],
                    "stream_removed" if stream is None else "remote_removed",
                    True,
                )
            if f"{sid}:stream" not in detected:
                conflict(sid, None, "stream", before.get(sid), local.get(sid), stream)
            continue
        if entry["kind"] == "stream":
            entry["remote"] = deepcopy(stream)
        else:
            entry["remote"] = next(
                (
                    deepcopy(o)
                    for o in (stream or {}).get("offers", [])
                    if o["offer_id"] == entry["offer_id"]
                ),
                None,
            )
        detected[entry["id"]] = entry
    return result, archive, list(detected.values()), changes
