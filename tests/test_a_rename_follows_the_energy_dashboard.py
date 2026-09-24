"""A rename has to follow the energy dashboard.

The dashboard's settings live in a store of their own, reachable over the
WebSocket API alone, and they hold bare entity ids as text. Nothing links them
to the registry, so a rename left them naming an entity that is not there: the
dashboard showed nothing for it and said nothing about why.
"""

import asyncio
from typing import Any, Dict, List

import pytest

from energy_prefs import EnergyPrefs

# A grid source keeps its own lists of flows, and the fields differ per source
# type - which is why the search walks the settings rather than reaching for
# fields it knows by name.
PREFS: Dict[str, Any] = {
    "energy_sources": [
        {
            "type": "grid",
            "flow_from": [
                {
                    "stat_energy_from": "sensor.meter_in",
                    "stat_cost": "sensor.meter_cost",
                    "entity_energy_price": None,
                }
            ],
            "flow_to": [{"stat_energy_to": "sensor.meter_out", "stat_compensation": None}],
        },
        {"type": "water", "stat_energy_from": "sensor.water_total", "stat_cost": None},
        {"type": "solar", "stat_energy_from": "shellyplug:total"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.plug_energy", "name": "Plug"},
        {"stat_consumption": "sensor.water_total", "included_in_stat": "sensor.water_total"},
    ],
}


def _prefs() -> Dict[str, Any]:
    import copy

    return copy.deepcopy(PREFS)


class FakeHomeAssistant:
    """Answers energy/get_prefs and remembers what energy/save_prefs was given."""

    def __init__(self, prefs: Dict[str, Any] | None = None, refuse: bool = False) -> None:
        self.held = prefs if prefs is not None else _prefs()
        self.refuse = refuse
        self.asked: List[str] = []
        self.written: List[Dict[str, Any]] = []

    async def __call__(self, message: Dict[str, Any]) -> Any:
        self.asked.append(message["type"])
        if message["type"] == "energy/get_prefs":
            import copy

            return copy.deepcopy(self.held)
        if message["type"] == "energy/save_prefs":
            if self.refuse:
                raise RuntimeError("invalid schema")
            written = {key: value for key, value in message.items() if key != "type"}
            self.written.append(written)
            self.held = written
            return written
        raise AssertionError(f"unexpected command {message['type']}")


@pytest.fixture
def ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()


@pytest.fixture
def energy(ha: FakeHomeAssistant) -> EnergyPrefs:
    return EnergyPrefs("ws://home-assistant.invalid/api/websocket", "token", command=ha)


# ------------------------------------------------------------------- finding


def test_the_places_that_name_an_entity_are_found(energy):
    """Nested in a grid source's flows, and in a device's consumption alike."""
    places = asyncio.run(energy.referring_to("sensor.water_total"))

    assert places == [
        "energy_sources[1].stat_energy_from",
        "device_consumption[1].stat_consumption",
        "device_consumption[1].included_in_stat",
    ]


def test_an_entity_the_dashboard_does_not_name_is_found_nowhere(energy):
    assert asyncio.run(energy.referring_to("sensor.nothing_here")) == []


def test_a_name_that_is_not_a_statistic_field_is_left_out(energy):
    """The settings also hold prose - a device's name, a currency."""
    assert asyncio.run(energy.referring_to("Plug")) == []


# ------------------------------------------------------------------ renaming


def test_a_rename_reaches_every_place_at_once(energy, ha):
    """There is no partial save: what goes back is the whole object, changed."""
    results = asyncio.run(energy.rename("sensor.water_total", "sensor.garden_water_total"))

    assert results["success"] == [
        "energy_sources[1].stat_energy_from",
        "device_consumption[1].stat_consumption",
        "device_consumption[1].included_in_stat",
    ]
    assert results["failed"] == []
    written = ha.written[-1]
    assert written["energy_sources"][1]["stat_energy_from"] == "sensor.garden_water_total"
    assert written["device_consumption"][1]["stat_consumption"] == "sensor.garden_water_total"
    assert written["device_consumption"][1]["included_in_stat"] == "sensor.garden_water_total"
    # Everything else goes back as it came, or the settings are refused.
    assert written["energy_sources"][0]["flow_from"][0]["stat_energy_from"] == "sensor.meter_in"
    assert written["device_consumption"][0]["name"] == "Plug"


def test_a_dashboard_that_does_not_name_the_entity_is_not_written(energy, ha):
    """A rename of something else is not a reason to write the settings back."""
    results = asyncio.run(energy.rename("sensor.nothing_here", "sensor.still_nothing"))

    assert results == {"success": [], "failed": []}
    assert ha.written == []


def test_the_settings_are_read_again_immediately_before_writing(energy, ha):
    """A settings page left open writes the whole list back as its browser had it.

    Reading again just before the write keeps the window as short as it can be
    made from here; answering out of what was read earlier made it as long as
    the job.
    """
    asyncio.run(energy.prefs())
    ha.held["device_consumption"].append({"stat_consumption": "sensor.water_total", "name": "Added meanwhile"})

    asyncio.run(energy.rename("sensor.water_total", "sensor.garden_water_total"))

    written = ha.written[-1]
    assert len(written["device_consumption"]) == 3
    assert written["device_consumption"][2]["stat_consumption"] == "sensor.garden_water_total"


def test_a_write_that_failed_is_not_kept_as_what_home_assistant_holds(ha):
    """The next entity of the same job would build on a change that never took."""
    refusing = FakeHomeAssistant(refuse=True)
    energy = EnergyPrefs("ws://home-assistant.invalid/api/websocket", "token", command=refusing)

    results = asyncio.run(energy.rename("sensor.water_total", "sensor.garden_water_total"))

    assert results["failed"] and not results["success"]
    # Asked for again rather than answered out of what was refused.
    assert asyncio.run(energy.referring_to("sensor.water_total"))


# ------------------------------------------------------------------- broken


def test_an_entity_that_is_gone_is_reported(energy):
    existing = {
        "sensor.meter_in",
        "sensor.meter_cost",
        "sensor.meter_out",
        "sensor.plug_energy",
    }

    broken = asyncio.run(energy.broken(existing))

    assert [one["missing_entity_id"] for one in broken] == ["sensor.water_total"] * 3
    assert broken[0]["path"] == "energy_sources[1].stat_energy_from"
    assert broken[0]["field"] == "stat_energy_from"


def test_a_statistic_from_outside_the_registry_is_not_missing(energy):
    """ "shellyplug:total" is a statistic, not an entity, and no rename touches it."""
    broken = asyncio.run(energy.broken({"sensor.water_total"}))

    assert "shellyplug:total" not in [one["missing_entity_id"] for one in broken]
