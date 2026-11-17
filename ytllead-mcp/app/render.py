"""LEAD user JSON → Markdown for the model.

LEAD publishes no schema; this follows the shape of the real records.
Known fields get named sections. Anything else (a new field, or one never
seen populated like ``speakers``) goes through
``_fields``, so new data shows up instead of being dropped.

Left out on purpose: database plumbing (ids, pivots, image paths, colours),
``login_status`` (an undocumented code), the event ``type`` (LEAD labels
BUILD workshops "Seminar") and ``is_active``/``is_ready`` (admin flags).

Where the data is ambiguous the output says so instead of guessing: a
not-attending user, or a step never completed (its entry may be a system
default); whether a tour is confirmed; which of several venues applies;
the tour meeting point, which isn't in the record.

Times: ISO timestamps with ``Z``/an offset are UTC and shown in MYT.
Zone-less ``YYYY-MM-DD HH:MM:SS`` values (the ``is_*_completed`` stamps) are
already MYT and shown as-is. Tour ``start_time``/``end_time`` are local too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

MYT = timezone(timedelta(hours=8))  # Malaysia: fixed UTC+8, no DST; no tzdata needed
NOT_INTERESTED_TITLE = "I am not interested."
PLACEHOLDER_DOB = "1970-01-01"
NOISE = re.compile(
    r"(^id$|_id$|^pivot$|img|thumbnail|file_path|_color$|^nice_name$"
    r"|token|password|secret|^otp)"  # never forward credential-looking fields
)
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _when(value: str, *, date_only: bool = False) -> str:
    """Timestamp → MYT text. Unparseable input is returned as-is."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if dt.tzinfo is None:  # zone-less = MYT, see module docstring
        dt = dt.replace(tzinfo=MYT)
    dt = dt.astimezone(MYT)
    return dt.strftime("%a %-d %b %Y" if date_only else "%a %-d %b %Y, %H:%M MYT")


def _span(start: str, end: str | None) -> str:
    """'Mon 9 Nov 2026, 09:00–11:00 MYT'; the full end is kept if it's another day."""
    if not end:
        return _when(start)
    s, e = _when(start), _when(end)
    s_day, _, s_time = s.rpartition(", ")
    e_day, _, e_time = e.rpartition(", ")
    if s_day == e_day:
        return f"{s_day}, {s_time.removesuffix(' MYT')}–{e_time}"
    return f"{s} – {e}"


def _hhmm(value: str | None) -> str:
    return (value or "")[:5]


def _text(value: Any) -> str:
    text = re.sub(r"[ \t]+\n", "\n", str(value).replace("\r\n", "\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def _yes(value: Any) -> str:
    return "Yes" if value in (1, True, "1") else "No"


def _label(key: str) -> str:
    return key.removeprefix("is_").replace("_", " ").capitalize()


def _scalar(key: str, value: Any) -> str:
    if key.startswith("is_") and value in (0, 1, True, False):
        return _yes(value)
    if isinstance(value, str) and ISO.match(value):
        return _when(value)
    return _text(value)


def _fields(data: Any, skip: set[str] = frozenset(), indent: int = 0) -> list[str]:
    """Every non-empty, non-noise field as nested bullets."""
    pad = "  " * indent
    out: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                sub = _fields(item, indent=indent + 1)
                if sub:
                    out.append(f"{pad}- " + sub[0].lstrip(" -"))
                    out.extend(sub[1:])
            elif item not in (None, ""):
                out.append(f"{pad}- {_text(item)}")
        return out
    for key, value in data.items():
        if key in skip or NOISE.search(key) or value in (None, "", [], {}):
            continue
        if isinstance(value, dict | list):
            sub = _fields(value, indent=indent + 1)
            if sub:
                out.append(f"{pad}- {_label(key)}:")
                out.extend(sub)
        else:
            out.append(f"{pad}- {_label(key)}: {_scalar(key, value)}")
    return out


def _done(value: Any) -> str:
    return f"done ({_when(value)})" if value and ISO.match(str(value)) else "not yet"


def _places(locations: Any) -> list[str]:
    if isinstance(locations, dict):
        locations = [locations]
    return [
        loc["name"] + (f" ({loc['address']})" if loc.get("address") else "")
        for loc in locations or []
        if isinstance(loc, dict) and isinstance(loc.get("name"), str) and loc["name"]
    ]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _norm(text: str) -> str:
    return " ".join(text.split())


def _event(e: dict[str, Any]) -> list[str]:
    out = [f"### {e.get('title') or 'Event'}"]
    if e.get("start_at"):
        out.append(f"- When: {_span(e['start_at'], e.get('end_at'))}")
    places = _places(e.get("locations"))
    raw = e.get("workshop_teams")
    teams = [wt for wt in ([raw] if isinstance(raw, dict) else raw or []) if isinstance(wt, dict)]
    if len(places) == 1:
        out.append(f"- Where: {places[0]}")
    elif places:  # the data doesn't say which venue applies to this user
        out.append("- Where (listed venues, data doesn't say which): " + "; ".join(places))
    elif "workshop_teams" in e and not teams:  # BUILD, no team yet: say so, don't leave a gap
        out.append("- Where: not in the record yet (no workshop team or room assigned)")
    elif not teams:  # with teams, each workshop below carries its own room and venue
        out.append("- Where: not in the record")
    for wt in teams:
        out.extend(_workshop(wt, e))
    speakers = _fields(e.get("speakers") or [], indent=1)
    out.append("- Speakers:" if speakers else "- Speakers: none listed")
    out.extend(speakers)
    skip = {
        "title",
        "type",
        "start_at",
        "end_at",
        "locations",
        "speakers",
        "description",
        "is_active",
        "is_ready",
        "capacity",
        "workshop_teams",
    }
    out.extend(_fields(e, skip))
    if e.get("description"):
        out += ["", _text(e["description"])]
    return out


def _workshop(wt: dict[str, Any], event: dict[str, Any]) -> list[str]:
    """One ``events[].workshop_teams[]`` item as a bullet nested in its event."""
    w, team = _dict(wt.get("workshop")), _dict(wt.get("team"))
    out = [f"- Workshop: {w.get('title') or 'title not in the record'}"]
    room = w.get("sub_title")
    if room:
        out.append(f"  - Room: {room}")
    places = _places(wt.get("location") or wt.get("locations"))
    if places:
        out.append("  - Where: " + "; ".join(places))
    elif room:
        out.append("  - Where: venue not in the record (only the room is listed)")
    else:
        out.append("  - Where: not in the record yet (no room assigned)")
    if w.get("start_at"):
        when = _span(w["start_at"], w.get("end_at"))
        # compare rendered times (one moment spelled two ways isn't a clash); ends only if both
        ends = (w.get("end_at"), event.get("end_at"))
        clash = event.get("start_at") and (
            _when(w["start_at"]) != _when(event["start_at"])
            or (all(ends) and _when(ends[0]) != _when(ends[1]))
        )
        out.append(
            f"  - Workshop time in the record: {when}"
            + (
                " (differs from the event time above; the data doesn't say which applies)"
                if clash
                else ""
            )
        )
    if team.get("name"):
        size = f"up to {team['capacity']} members" if team.get("capacity") else None
        extra = ", ".join(filter(None, (team.get("team_code"), size)))
        out.append(f"  - Your team: {team['name']}" + (f" ({extra})" if extra else ""))
    out.extend(_fields(w, {"title", "sub_title", "start_at", "end_at", "description"}, indent=1))
    out.extend(_fields(team, {"name", "team_code", "capacity"}, indent=1))
    out.extend(_fields(wt, {"workshop", "team", "location", "locations"}, indent=1))
    desc = w.get("description")
    if desc and _text(desc) != _text(room or ""):  # LEAD repeats the room as the description
        out.append(f"  - About: {_text(desc)}")
    return out


def _tour(t: dict[str, Any], selection_done: bool, only_row: bool = True) -> list[str]:
    session = t.get("pre_conference_session") or {}
    tour = session.get("pre_conference_event") or {}
    remark = (t.get("pivot") or {}).get("remark")
    if tour.get("title") == NOT_INTERESTED_TITLE:
        if remark and remark.lower().startswith("auto-placed"):
            if selection_done:  # stamp says submitted, remark says auto-placed: state both
                return [
                    "- Not interested in a pre-conference tour "
                    "(the record notes it was auto-placed from registration)"
                ]
            return ["- No tour selected (the system auto-placed this user as not interested)"]
        note = f" ({remark})" if remark and not remark.lower().startswith("not interested") else ""
        return [f"- Not interested in a pre-conference tour{note}"]
    out = [f"### {tour.get('title') or 'Pre-conference tour'}"]
    if session.get("date"):
        times = "–".join(filter(None, (_hhmm(t.get("start_time")), _hhmm(t.get("end_time")))))
        out.append(
            f"- Date: {_when(session['date'], date_only=True)}"
            + (f", {times} MYT" if times else "")
        )
    if remark:  # "2nd Choice", "Same tour, different day", …: a preference note, not a status
        note = remark.strip()
        lone = (
            only_row
            and re.match(r"^\d+(st|nd|rd|th) choice$", note, re.I)
            and not note.startswith("1")
        )
        out.append(
            f"- Your selection note: {remark}"
            + (" (only this choice appears in the record)" if lone else "")
        )
    if tour.get("warning"):
        out.append("- Notes (from the tour selection page): " + "; ".join(_split(tour["warning"])))
    if tour.get("facility"):
        out.append("- Provided: " + ", ".join(_split(tour["facility"])))
    out.append("- Meeting point: not in the record")
    out.extend(_fields(tour, {"title", "warning", "facility", "description"}))
    out.extend(_fields(session, {"date", "pre_conference_event"}))
    out.extend(
        _fields(t, {"pre_conference_session", "start_time", "end_time", "pivot", "capacity"})
    )
    if tour.get("description"):
        out += ["", _text(tour["description"])]
    return out


def _personality(up: dict[str, Any]) -> list[str]:
    p = up.get("personality") or {}
    if not p:
        return ["- Not determined yet"]
    out = [
        f"**{p.get('name') or 'Unknown'}**" + (f" — {p['sub_title']}" if p.get("sub_title") else "")
    ]
    # LEAD may open description/summary with the sub_title sentence (already in the header).
    sub = _norm(str(p.get("sub_title") or ""))

    def paras(key: str) -> list[str]:
        return [
            para for para in _text(p.get(key) or "").split("\n\n") if para and _norm(para) != sub
        ]

    summary = "\n\n".join(paras("summary"))
    in_summary = _norm(summary)
    # LEAD repeats description paragraphs inside summary; keep only new ones.
    fresh = [para for para in paras("description") if _norm(para) not in in_summary]
    out += [x for para in fresh for x in ("", para)]
    if summary:
        out += ["", summary]
    lists = {
        "strengths": "Strengths",
        "tags": "Tags",
        "famous_people": "Famous people with this personality",
    }
    for key, title in lists.items():
        if p.get(key):
            items = p[key] if isinstance(p[key], list) else [p[key]]
            out += ["", f"{title}:", *(f"- {_text(s)}" for s in items)]
    if p.get("watch_out_for"):
        out += ["", f"Watch out for: {_text(p['watch_out_for'])}"]
    if p.get("recommendations"):
        out += ["", f"Recommendation: {_text(p['recommendations'])}"]
    agenda = p.get("lead_agenda") or {}
    if agenda:
        out += [
            "",
            "Tips for each part of the conference "
            "(advice for this personality type, not a schedule):",
        ]
        for part, tips in agenda.items():
            out.append(f"- {_label(part)}:")
            out.extend(f"  - {_text(tip)}" for tip in (tips if isinstance(tips, list) else [tips]))
    rest = _fields(
        p,
        {
            "name",
            "sub_title",
            "description",
            "summary",
            "watch_out_for",
            "recommendations",
            "lead_agenda",
            *lists,
        },
    )
    rest += _fields(up, {"personality"})
    if rest:
        out += ["", *rest]
    return out


def to_markdown(data: dict[str, Any]) -> str:
    prof = data.get("user_profile") or {}
    company = (prof.get("company") or {}).get("name")
    name = " ".join(filter(None, (prof.get("title"), prof.get("name")))) or "Unknown"
    phone = " ".join(filter(None, (prof.get("phone_country_code"), prof.get("phone_number"))))
    dob = prof.get("dob") if prof.get("dob") not in (None, "", PLACEHOLDER_DOB) else "not provided"
    out = [
        f"# Conference profile: {name}",
        "",
        "This is the signed-in user's own record. All times are Malaysia time (MYT).",
        "",
        "## Personal details",
        f"- User ID: {data.get('id')}",
        f"- Name: {name}",
        f"- Designation: {prof.get('designation') or 'not provided'}",
        f"- Company: {company or 'not provided'}",
        f"- Email: {data.get('email') or 'not provided'}",
        f"- Phone: {phone or 'not provided'}",
        f"- Date of birth: {dob}",
        f"- Vegetarian: {_yes(prof.get('is_vegetarian'))}",
        f"- Role: {(data.get('user_role') or {}).get('name') or 'unknown'}",
    ]
    if data.get("username"):
        out.append(f"- Username: {data['username']}")
    out += _fields(
        data,
        {
            "email",
            "username",
            "user_role",
            "user_profile",
            "events",
            "pre_conference_event",
            "user_personality",
            "workshop_teams",
            "login_status",
            "is_registration_completed",
        },
    )

    not_attending = prof.get("is_attending") in (0, False, "0")
    unregistered = data.get("is_registration_completed") in (0, False, "0")
    attending = _yes(prof.get("is_attending"))
    if prof.get("not_attending_reason"):
        attending += f" (reason: {_text(prof['not_attending_reason'])})"
    elif not_attending:
        attending += " (no reason recorded)"
    elif unregistered:
        attending += " (registration not completed, so this may be a default)"
    out += [
        "",
        "## Attendance and registration",
        f"- Registration completed: {_yes(data.get('is_registration_completed'))}",
        f"- Attending the conference: {attending}",
        f"- Table number: {prof.get('table_no') or 'not assigned yet'}",
        f"- Personality quiz: {_done(prof.get('is_personality_completed'))}",
        f"- Activity selection: {_done(prof.get('is_activity_completed'))}",
        f"- Pre-conference tour selection: {_done(prof.get('is_pre_conference_completed'))}",
    ]
    out += _fields(
        prof,
        {
            "title",
            "name",
            "designation",
            "company",
            "phone_country_code",
            "phone_number",
            "dob",
            "is_vegetarian",
            "is_attending",
            "not_attending_reason",
            "table_no",
            "is_personality_completed",
            "is_activity_completed",
            "is_pre_conference_completed",
        },
    )

    def caveats(step_done: Any, step: str) -> list[str]:
        notes = []
        if not_attending:
            notes.append(
                "- Note: marked as not attending the conference, "
                "so the selection below may no longer apply."
            )
        if not step_done:
            notes.append(
                f"- Note: {step} is not completed, so the entry below may be a system default, "
                "not the user's own choice."
            )
        return notes

    out += ["", "## Event"]
    by_id: dict[Any, dict[str, Any]] = {}
    for i, e in enumerate(data.get("events") or []):
        key = e.get("id") if e.get("id") is not None else f"#{i}"
        prev = by_id.get(key)
        if prev and prev.get("workshop_teams") and not e.get("workshop_teams"):
            e = {**e, "workshop_teams": prev["workshop_teams"]}  # a duplicate must not drop a team
        by_id[key] = e
    events = list(by_id.values())
    if events:
        out += caveats(prof.get("is_activity_completed"), "activity selection")
    out += [line for e in events for line in [*_event(e), ""]] or ["- None registered", ""]

    out += [
        "## Pre-conference tour",
        "The tour recorded for this user. The data does not say whether it is confirmed.",
    ]
    tours = data.get("pre_conference_event") or []
    selection_done = bool(prof.get("is_pre_conference_completed"))
    if tours:
        out += caveats(selection_done, "pre-conference tour selection")
    if not tours and prof.get("is_pre_conference_completed"):
        out += ["- Selection submitted, but no tour on record", ""]
    else:
        out += [
            line for t in tours for line in [*_tour(t, selection_done, len(tours) == 1), ""]
        ] or [
            "- No selection recorded",
            "",
        ]

    legacy = _fields(data.get("workshop_teams") or [])  # pre-2026-11 shape: top-level list
    if legacy:
        caveat = caveats(prof.get("is_activity_completed"), "activity selection")
        out += ["## Workshop team", *caveat, *legacy, ""]

    out += ["## Personality profile", *_personality(data.get("user_personality") or {})]
    return "\n".join(out).rstrip() + "\n"
