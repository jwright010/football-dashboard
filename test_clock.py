from clock import (
    parse_clock,
    total_remaining_seconds,
    period_label,
    is_live,
    describe,
)

cases = []


def check(name, actual, expected):
    ok = actual == expected
    cases.append((name, ok, actual, expected))


# parse_clock
check("clock 10:00", parse_clock("10:00"), 600)
check("clock 0:42", parse_clock("0:42"), 42)
check("clock 15:00", parse_clock("15:00"), 900)
check("clock empty", parse_clock(""), 0)
check("clock junk", parse_clock("--"), 0)
check("clock seconds only", parse_clock("42.0"), 42)

# total_remaining_seconds
check("pre-game", total_remaining_seconds(0, 0, "pre"), 3600)
check("Q1 15:00", total_remaining_seconds(1, 900, "in"), 3600)
check("Q2 10:00", total_remaining_seconds(2, 600, "in"), 2400)
check("halftime", total_remaining_seconds(2, 0, "in"), 1800)
check("Q4 2:00", total_remaining_seconds(4, 120, "in"), 120)
check("final", total_remaining_seconds(4, 0, "post"), 0)
check("OT 8:30", total_remaining_seconds(5, 510, "in"), 510)
check("final OT", total_remaining_seconds(5, 0, "post"), 0)

# period_label
check("label Q2", period_label(2, 600, "in"), "Q2")
check("label half by clock", period_label(2, 0, "in"), "Half")
check("label half by detail", period_label(2, 300, "in", "Halftime"), "Half")
check("label end Q1", period_label(1, 0, "in"), "End Q1")
check("label end Q3", period_label(3, 0, "in"), "End Q3")
check("label OT", period_label(5, 510, "in"), "OT")
check("label OT2", period_label(6, 300, "in"), "OT2")
check("label final", period_label(4, 0, "post"), "Final")
check("label final OT", period_label(5, 0, "post"), "Final/OT")
check("label pre", period_label(0, 0, "pre"), "Pre")

# is_live
check("live in", is_live("in"), True)
check("live pre", is_live("pre"), False)
check("live post", is_live("post"), False)
check("live postponed", is_live("in", "Postponed"), False)
check("live delayed", is_live("in", "Delayed"), False)

# describe
check(
    "describe Q2",
    describe("NE", 21, "NYJ", 7, 2, 600, "in"),
    "NE 21, NYJ 7 - Q2 - 40:00 remaining",
)
check(
    "describe halftime",
    describe("NE", 21, "NYJ", 7, 2, 0, "in", "Halftime"),
    "NE 21, NYJ 7 - Half - 30:00 remaining",
)
check(
    "describe final",
    describe("NE", 24, "NYJ", 17, 4, 0, "post"),
    "NE 24, NYJ 17 - Final",
)
check(
    "describe OT final",
    describe("NE", 27, "NYJ", 24, 5, 0, "post"),
    "NE 27, NYJ 24 - Final/OT",
)
check(
    "describe two-minute",
    describe("KC", 14, "BUF", 17, 4, 118, "in"),
    "KC 14, BUF 17 - Q4 - 1:58 remaining",
)

failed = [c for c in cases if not c[1]]
for name, ok, actual, expected in cases:
    mark = "PASS" if ok else "FAIL"
    line = f"{mark}  {name}"
    if not ok:
        line += f"\n        got:      {actual!r}\n        expected: {expected!r}"
    print(line)

print(f"\n{len(cases) - len(failed)}/{len(cases)} passed")
