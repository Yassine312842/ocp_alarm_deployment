"""
Central configuration: the (simplified) MAP granulation line, its tags,
alarm limits, and the fault scenarios the synthetic generator injects.

This is the ONE place that encodes plant knowledge for the demo. When you
switch to real data, tags/limits come from the historian's tag database and
this file mostly describes the alarm-limit config only.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tag:
    name: str
    unit: str
    setpoint: float          # normal operating value
    noise: float             # 1-sigma measurement noise
    lo: float | None = None  # LO alarm limit
    lolo: float | None = None
    hi: float | None = None  # HI alarm limit
    hihi: float | None = None  # HIHI -> treated as trip-level


# --- Simplified monoammonium phosphate (MAP) granulation line -------------
TAGS: list[Tag] = [
    Tag("REACTOR_TEMP",      "degC", 118.0, 0.6, lo=108, hi=126, hihi=132),
    Tag("REACTOR_PRESS",     "bar",    1.4, 0.03, hi=1.7, hihi=1.9),
    Tag("NH3_FLOW",          "m3/h",  42.0, 0.8,  lo=34, hi=50),
    Tag("PHOS_ACID_FLOW",    "m3/h",  60.0, 1.0,  lo=50, hi=70),
    Tag("SLURRY_DENSITY",    "kg/m3", 1520.0, 6.0, lo=1470, hi=1570),
    Tag("SCRUBBER_PH",       "pH",     6.2, 0.05, lolo=5.2, lo=5.6),
    Tag("DRYER_TEMP",        "degC",  190.0, 1.5, lo=170, hi=210, hihi=225),
    Tag("PRODUCT_MOISTURE",  "%",      1.2, 0.08, hi=1.8, hihi=2.2),
    Tag("GRANULATOR_AMPS",   "A",     310.0, 4.0, lo=260, hi=360, hihi=390),
    Tag("RECYCLE_RATIO",     "-",      4.5, 0.1,  lo=3.8, hi=5.4),
]
TAGS_BY_NAME = {t.name: t for t in TAGS}

# EEMUA-191 / ISA-18.2 alarm priorities (drives the "priority" on each event)
PRIORITY_BY_LEVEL = {"LO": "LOW", "HI": "LOW",
                     "LOLO": "HIGH", "HIHI": "CRITICAL"}


@dataclass(frozen=True)
class Scenario:
    """A recurring fault with a KNOWN root cause and a cascading alarm chain.

    `chain` = ordered (tag, level, delay_min) steps. The generator drives the
    named tags past their limits in order, producing a repeatable alarm
    sequence that the analysis/root-cause layers are meant to rediscover.
    """
    key: str
    root_cause: str
    chain: list[tuple[str, str, float]] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    # Scrubber pump degradation -> pH drops -> back-pressure -> reactor trip
    Scenario("SCRUBBER_DEGRADATION", root_cause="Scrubber recirc pump fouling",
             chain=[("SCRUBBER_PH", "LO", 0.0),
                    ("SCRUBBER_PH", "LOLO", 6.0),
                    ("REACTOR_PRESS", "HI", 9.0),
                    ("REACTOR_PRESS", "HIHI", 14.0)]),
    # Feed imbalance -> reactor overheats -> dryer runs hot
    Scenario("FEED_IMBALANCE", root_cause="NH3/acid ratio controller drift",
             chain=[("NH3_FLOW", "HI", 0.0),
                    ("REACTOR_TEMP", "HI", 5.0),
                    ("REACTOR_TEMP", "HIHI", 11.0),
                    ("DRYER_TEMP", "HI", 16.0)]),
    # Granulator overload -> recycle climbs -> moisture out of spec
    Scenario("GRANULATOR_OVERLOAD", root_cause="Wet product recycle buildup",
             chain=[("RECYCLE_RATIO", "HI", 0.0),
                    ("GRANULATOR_AMPS", "HI", 7.0),
                    ("PRODUCT_MOISTURE", "HI", 12.0),
                    ("GRANULATOR_AMPS", "HIHI", 18.0)]),
]

# A "bad actor" / chattering instrument: this tag's flow transmitter is faulty
# and toggles its LO alarm rapidly. Layer-3 chattering detection should catch it.
CHATTERING_TAG = "PHOS_ACID_FLOW"
