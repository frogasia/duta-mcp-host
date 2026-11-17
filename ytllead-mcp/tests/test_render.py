"""to_markdown on an invented record in LEAD's observed shape, plus fields
never seen populated in the sample (workshop team, speaker, table, a new
top-level field) to prove the fallback renders them instead of dropping."""

from __future__ import annotations

from ytllead_mcp.render import to_markdown


def tour(title: str, remark: str, **extra) -> dict:
    return {
        "id": 16,
        "start_time": "09:00:00",
        "end_time": "14:30:00",
        "pivot": {"user_id": 7, "remark": remark},
        "pre_conference_session": {
            "id": 13,
            "date": "2026-11-23T16:00:00.000000Z",  # = 24 Nov MYT
            "location_id": 1,
            "pre_conference_event": {"id": 10, "title": title, "description": None, **extra},
        },
    }


RECORD = {
    "id": 7,
    "user_role_id": 2,
    "email": "test@example.test",
    "login_status": 1,
    "username": None,
    "badge_colour_code": "B7",  # unknown future field
    "user_role": {"id": 2, "name": "User"},
    "user_profile": {
        "id": 7,
        "title": "Ms",
        "name": "Test Person",
        "designation": None,
        "company": None,
        "phone_country_code": "+60",
        "phone_number": "120000000",
        "dob": None,
        "is_vegetarian": 1,
        "is_attending": 1,
        "table_no": "12",
        "is_personality_completed": "2026-10-23 07:18:57",
        "is_activity_completed": None,
        "is_pre_conference_completed": "2026-10-23 07:22:24",
    },
    "events": [
        {
            "id": 1,
            "title": "BUILD (Workshops)",
            "type": {"id": 1, "name": "Seminar", "type": "seminar"},
            "start_at": "2026-11-09T01:00:00.000000Z",
            "end_at": "2026-11-09T03:00:00.000000Z",
            "description": "Line one.  \r\n\r\nLine two.",
            "img_file_path": "x.png",
            "pivot": {"user_id": 7, "event_id": 1},
            "speakers": [{"id": 3, "name": "Dr Speaker", "topic": "AI"}],
            "locations": [{"id": 1, "name": "Menara YTL", "address": None}],
        }
    ],
    "pre_conference_event": [
        tour(
            "YTL Museum Tour",
            "1st Choice",
            warning="Full Day",
            facility="Lunch Provided;Transport Provided",
        )
    ],
    "workshop_teams": [{"id": 5, "name": "Team Falcon", "table": "A3"}],
    "user_personality": {
        "personality": {
            "name": "Explorer",
            "sub_title": "You open paths.",
            "strengths": ["Curious"],
            "lead_agenda": {"arrive": ["Start somewhere new."]},
            "primary_color": "#2A9D8F",
        }
    },
}


def test_known_sections_and_myt_times() -> None:
    md = to_markdown(RECORD)
    assert "# Conference profile: Ms Test Person" in md
    assert "- Company: not provided" in md
    assert "- Phone: +60 120000000" in md
    assert "- Vegetarian: Yes" in md
    assert "- Table number: 12" in md
    # zone-less completion stamps are already MYT: shown as-is, not +8h
    assert "- Personality quiz: done (Fri 23 Oct 2026, 07:18 MYT)" in md
    assert "- Activity selection: not yet" in md
    assert "- When: Mon 9 Nov 2026, 09:00–11:00 MYT" in md
    assert "- Date: Tue 24 Nov 2026, 09:00–14:30 MYT" in md
    assert "- Provided: Lunch Provided, Transport Provided" in md
    assert "Line one.\n\nLine two." in md
    assert "- Arrive:\n  - Start somewhere new." in md


def test_unseen_fields_are_rendered_not_dropped() -> None:
    md = to_markdown(RECORD)
    assert "Dr Speaker" in md and "AI" in md
    assert "- Name: Team Falcon" in md and "- Table: A3" in md
    assert "- Badge colour code: B7" in md


def test_database_plumbing_is_left_out() -> None:
    md = to_markdown(RECORD)
    for noise in ("x.png", "#2A9D8F", "Event id", "User id", "Pivot"):
        assert noise not in md


def test_not_interested_and_empty_record() -> None:
    record = {
        **RECORD,
        "pre_conference_event": [
            tour("I am not interested.", "Not interested in pre-conference tours")
        ],
    }
    assert "- Not interested in a pre-conference tour\n" in to_markdown(record)
    auto = tour("I am not interested.", "Auto-placed from Registration (no selection)")
    md = to_markdown({**RECORD, "pre_conference_event": [auto]})
    # RECORD has a tour-selection stamp, so state both facts rather than "no tour selected"
    assert "(the record notes it was auto-placed from registration)" in md
    unstamped = {**RECORD["user_profile"], "is_pre_conference_completed": None}
    md = to_markdown({**RECORD, "user_profile": unstamped, "pre_conference_event": [auto]})
    assert "- No tour selected (the system auto-placed this user as not interested)" in md
    bare = to_markdown({"id": 1})
    assert "- No selection recorded" in bare and "## Workshop team" not in bare


def test_fixes_from_the_200_record_review() -> None:
    record = {
        **RECORD,
        "login_status": 1,
        "user_profile": {
            **RECORD["user_profile"],
            "is_attending": 0,
            "dob": "1970-01-01",
        },
        "pre_conference_event": [
            tour("Kanthan Tour", "2nd Choice", warning="Full Day;Requires walking")
        ],
        "user_personality": {
            "personality": {
                "name": "Explorer",
                "description": "Unique opener.\r\n\r\nShared paragraph.",
                "summary": "Shared paragraph.\r\n\r\nMore detail.",
            }
        },
    }
    md = to_markdown(record)
    assert "- Attending the conference: No (no reason recorded)" in md
    assert (
        md.count("marked as not attending the conference") == 3
    )  # event, tour, legacy workshop team
    assert "- Date of birth: not provided" in md
    assert "Login status" not in md
    assert "- Your selection note: 2nd Choice" in md
    assert "- Notes (from the tour selection page): Full Day; Requires walking" in md
    assert "does not say whether it is confirmed" in md
    assert md.count("Shared paragraph.") == 1 and "Unique opener." in md


def test_tour_selection_completed_but_no_tour() -> None:
    md = to_markdown({**RECORD, "pre_conference_event": []})
    assert "- Selection submitted, but no tour on record" in md


def test_fixes_from_the_400_record_review() -> None:
    event = RECORD["events"][0]
    two_venues = {**event, "locations": [{"name": "Hall A"}, {"name": "Hall B", "address": "KL"}]}
    md = to_markdown(
        {
            **RECORD,
            "is_registration_completed": 0,
            "events": [two_venues, two_venues],  # LEAD sometimes returns the same event twice
        }
    )
    assert md.count("### BUILD (Workshops)") == 1
    assert "Type:" not in md and "Active:" not in md and "Ready:" not in md
    assert "- Where (listed venues, data doesn't say which): Hall A; Hall B (KL)" in md
    assert "- Registration completed: No" in md.split("## Attendance and registration")[1]
    # caveats follow each step's own stamp: activity not done, tour selection done
    assert "- Note: activity selection is not completed" in md
    assert "pre-conference tour selection is not completed" not in md
    assert (
        "- Attending the conference: Yes (registration not completed, so this may be a default)"
        in md
    )
    assert "- Meeting point: not in the record" in md


def test_fixes_from_the_550_record_review() -> None:
    # registration incomplete but activity chosen -> no "system default" note on the event
    prof = {**RECORD["user_profile"], "is_activity_completed": "2026-10-24 10:00:00"}
    md = to_markdown({**RECORD, "is_registration_completed": 0, "user_profile": prof})
    assert "activity selection is not completed" not in md
    # a lone non-first choice says the other choices aren't in the record
    md = to_markdown({**RECORD, "pre_conference_event": [tour("Museum", "3rd Choice")]})
    assert "- Your selection note: 3rd Choice (only this choice appears in the record)" in md
    md = to_markdown({**RECORD, "pre_conference_event": [tour("Museum", "1st Choice")]})
    assert "- Your selection note: 1st Choice\n" in md


def test_null_event_ids_are_not_collapsed_and_null_name() -> None:
    events = [
        {**RECORD["events"][0], "id": None, "title": "BUILD"},
        {**RECORD["events"][0], "id": None, "title": "KEYNOTE"},
    ]
    md = to_markdown(
        {**RECORD, "events": events, "user_personality": {"personality": {"name": None}}}
    )
    assert "### BUILD" in md and "### KEYNOTE" in md
    assert "**Unknown**" in md and "None" not in md.split("## Personality profile")[1]


def test_round2_hardening() -> None:
    two = [tour("Museum", "2nd Choice"), tour("Starhill", "3rd Choice")]
    md = to_markdown({**RECORD, "pre_conference_event": two})
    assert "only this choice appears" not in md  # two rows: the note would be false
    p = {"name": "X", "strengths": "Curious; Bold"}  # a string, not a list
    md = to_markdown({**RECORD, "user_personality": {"personality": p}})
    assert "- Curious; Bold" in md and "- C\n" not in md
    md = to_markdown({**RECORD, "remember_token": "s3cr3t", "api_password": "pw"})
    assert "s3cr3t" not in md and "pw" not in md.split("## Personal details")[1].split("##")[0]


def test_workshop_teams_nested_under_event() -> None:
    """2026-11 API shape: workshop_teams moved from top level into each event."""
    event = {
        "id": 1,
        "title": "BUILD (Workshops)",
        "start_at": "2026-11-26T03:30:00.000000Z",
        "end_at": "2026-11-26T08:00:00.000000Z",
        "capacity": 1000,
        "speakers": [],
        "workshop_teams": [
            {
                "workshop": {
                    "id": 1,
                    "title": "Problem 9: Test Day",
                    "sub_title": "Room 9",
                    "description": "Room 9",
                    "start_at": "2026-11-26T00:00:00.000000Z",
                    "end_at": "2026-11-26T10:00:00.000000Z",
                },
                "location": [{"id": 6, "name": "Hall X", "address": "Test Hotel"}],
                "team": {"id": 1, "name": "07", "team_code": "team-7", "capacity": 4},
            }
        ],
    }
    md = to_markdown({"id": 7, "user_profile": {"name": "T"}, "events": [event]})
    assert "## Workshop team" not in md  # nested in the event now
    ev = md.split("## Event")[1].split("## Pre-conference")[0]
    assert "Capacity" not in ev and "see the workshop" not in ev
    assert "- Workshop: Problem 9: Test Day\n  - Room: Room 9\n  - Where: Hall X (Test Hotel)" in ev
    assert "  - Your team: 07 (team-7, up to 4 members)" in ev
    assert "08:00–18:00 MYT (differs from the event time above" in ev
    assert ev.count("Room 9") == 1  # description repeating the room is dropped


def test_where_is_never_silent_and_sub_title_not_repeated() -> None:
    build = {"id": 1, "title": "BUILD", "workshop_teams": []}
    think = {"id": 2, "title": "THINK"}
    md = to_markdown({"id": 7, "events": [build, think]})
    assert "- Where: not in the record yet (no workshop team or room assigned)" in md
    assert "- Where: not in the record\n" in md
    p = {
        "name": "X",
        "sub_title": "Same line.",
        "description": "Same line.\n\nMore.",
        "summary": "Same line.\n\nMore.",
    }
    md = to_markdown({"id": 7, "user_personality": {"personality": p}})
    assert md.count("Same line.") == 1 and "More." in md


def test_workshop_team_edge_cases() -> None:
    """Caveats, silent where, odd shapes, dedupe, legacy, time spelling."""
    team = {"workshop": {"title": "P9", "start_at": "2026-11-26T03:30:00.000000Z"}}
    event = {
        "id": 1,
        "title": "BUILD",
        "start_at": "2026-11-26T03:30:00Z",
        "workshop_teams": [team, "junk"],
    }
    record = {
        "id": 7,
        "user_profile": {"is_attending": 0},
        "events": [event, {"id": 1, "title": "BUILD", "workshop_teams": []}],
        "workshop_teams": [{"name": "Legacy Team"}],
    }
    md = to_markdown(record)
    ev = md.split("## Event")[1].split("## Pre-conference")[0]
    assert ev.count("- Workshop: P9") == 1  # duplicate event didn't drop it; "junk" skipped
    assert "  - Where: not in the record yet (no room assigned)" in ev
    assert "marked as not attending" in ev and "activity selection is not completed" in ev
    assert "Legacy Team" in md.split("## Workshop team")[1]  # old top-level shape still shown
    loc = {"workshop": {"title": "P1", "sub_title": "Room 1", "description": "Room 1 \r\n"}}
    loc["location"] = {"name": "Hall X"}  # a single dict, not a list
    md = to_markdown({"id": 7, "events": [{"id": 2, "workshop_teams": [loc]}]})
    assert "- Where: Hall X" in md and md.count("Room 1") == 1


def test_workshop_time_clash_and_odd_nested_shapes() -> None:
    def ev(workshop: dict, **e) -> str:
        event = {
            "id": 1,
            "title": "BUILD",
            "start_at": "2026-11-26T03:30:00Z",
            "workshop_teams": {"workshop": workshop},
            **e,
        }
        md = to_markdown({"id": 7, "events": [event]})
        return md.split("## Event")[1].split("## Pre-conference")[0]

    same = ev(
        {"title": "P", "start_at": "2026-11-26T03:30:00.000000Z"}, end_at="2026-11-26T05:00:00Z"
    )
    assert "- Workshop: P" in same  # a lone dict instead of a list still renders
    assert "differs from the event time" not in same  # same start, different spelling, no end
    late = ev({"title": "P", "start_at": "2026-11-26T04:00:00Z"})
    assert "differs from the event time" in late
    odd = to_markdown(
        {
            "id": 7,
            "events": [
                {
                    "id": 1,
                    "workshop_teams": [{"workshop": "P1", "team": [], "location": [{"name": 6}]}],
                }
            ],
        }
    )
    assert "- Workshop: title not in the record" in odd  # no crash on non-dict parts
    room_only = ev({"title": "P", "sub_title": "Room 2"})
    assert "  - Where: venue not in the record (only the room is listed)" in room_only
    legacy = to_markdown(
        {"id": 7, "user_profile": {"is_attending": 0}, "workshop_teams": [{"name": "T"}]}
    )
    assert "marked as not attending" in legacy.split("## Workshop team")[1]
