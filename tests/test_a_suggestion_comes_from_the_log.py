"""What an entity became, rather than what its name looks like.

`binary_sensor.buro_linkes_fenster_status` was answered with
`binary_sensor.buro_bambu_lab_h2c_drucker_firmware_status` at 0.74, and the five
suggestions behind it all sat between 0.700 and 0.743 - the order was noise. Two
thirds of every score was handed out for the domain, which only matching
candidates have, and for the first word, which every entity in the room shares.

The rename log records what was actually renamed to what. Where it answers, it
settles the question; where it does not, a guess is offered only if it is close
enough to be worth looking at, and otherwise nothing at all.
"""

import pytest

from reference_checker import ReferenceChecker, Suggestion


class Log:
    """A rename log with the hops written into it."""

    def __init__(self, hops=None, raises=False):
        self.hops = hops or {}
        self.raises = raises

    def search(self, entity_id):
        if self.raises:
            raise OSError("the log is unreadable")
        trail = []
        current = entity_id
        seen = {current}
        while current in self.hops:
            current = self.hops[current]
            trail.append({"new_entity_id": current})
            if current in seen:
                break
            seen.add(current)
        if not trail:
            return {"query": entity_id, "found": False, "renamed": False}
        return {"query": entity_id, "found": True, "renamed": True, "current_entity_id": current, "history": trail}


@pytest.fixture
def checker():
    one = ReferenceChecker("http://nowhere", "token")
    one._existing_entities = {
        "binary_sensor.buro_bambu_lab_h2c_drucker_firmware_status",
        "binary_sensor.buro_heizung_fernmessung_der_aussentemperatur",
        "switch.kammer_it_steckdose_schalter",
        "sensor.balkon_ventil_wasserstatus",
    }
    one._entity_details = {
        entity_id: {"friendly_name": entity_id, "domain": entity_id.split(".")[0]}
        for entity_id in one._existing_entities
    }
    one.rename_log = Log()
    return one


async def test_the_log_settles_it_where_it_can(checker):
    checker.rename_log = Log({"sensor.balkon_ventil_geratestatus": "sensor.balkon_ventil_wasserstatus"})

    found = await checker.get_suggestions("sensor.balkon_ventil_geratestatus")

    assert [one.entity_id for one in found] == ["sensor.balkon_ventil_wasserstatus"]
    assert found[0].score == 1.0
    assert found[0].reasons == ["renamed_here"]


async def test_a_chain_is_followed_to_its_end(checker):
    """An entity can be renamed again, and again."""
    checker.rename_log = Log(
        {
            "sensor.first": "sensor.second",
            "sensor.second": "sensor.third",
            "sensor.third": "sensor.balkon_ventil_wasserstatus",
        }
    )

    found = await checker.get_suggestions("sensor.first")

    assert [one.entity_id for one in found] == ["sensor.balkon_ventil_wasserstatus"]


async def test_a_chain_ending_at_something_gone_is_not_offered(checker):
    """A device swap parks an entity on an interim id and may never come back."""
    checker.rename_log = Log(
        {"binary_sensor.buro_rechtes_fenster_status": "binary_sensor.buro_rechtes_fenster_status_swapout"}
    )

    found = await checker.get_suggestions("binary_sensor.buro_rechtes_fenster_status")

    assert [one.entity_id for one in found] == []


async def test_a_chain_that_returns_to_where_it_started_is_not_offered(checker):
    checker.rename_log = Log({"sensor.round": "sensor.about", "sensor.about": "sensor.round"})

    assert await checker.get_suggestions("sensor.round") == []


async def test_an_unreadable_log_still_allows_a_guess(checker):
    """The log failing is not a reason to answer nothing at all."""
    checker.rename_log = Log(raises=True)

    found = await checker.get_suggestions("switch.kammer_it_steckdose_zustand")

    assert [one.entity_id for one in found] == ["switch.kammer_it_steckdose_schalter"]


async def test_a_printer_is_not_offered_for_a_window(checker):
    """Two words of nine is two entities that share a room."""
    found = await checker.get_suggestions("binary_sensor.buro_linkes_fenster_status")

    assert found == []


async def test_a_rename_that_changed_one_word_is_still_found(checker):
    """Three words of five is the shape of a real rename."""
    found = await checker.get_suggestions("switch.kammer_it_steckdose_zustand")

    assert [one.entity_id for one in found] == ["switch.kammer_it_steckdose_schalter"]
    assert found[0].score == 0.6
    assert found[0].reasons == ["it", "kammer", "steckdose"]


def test_the_domain_is_not_scored(checker):
    """Only same-domain candidates are considered, so scoring it said nothing."""
    score, reasons = checker._calculate_similarity("sensor.one_two", "sensor.one_two")

    assert score == 1.0
    assert "same_domain" not in reasons


def test_sharing_only_a_room_scores_near_nothing(checker):
    score, _reasons = checker._calculate_similarity(
        "binary_sensor.buro_linkes_fenster_status",
        "binary_sensor.buro_bambu_lab_h2c_drucker_firmware_status",
    )

    assert score < 0.3


def test_without_a_log_a_guess_is_all_there_is(checker):
    """A fresh installation has renamed nothing yet."""
    checker.rename_log = None

    assert checker._where_it_went("switch.kammer_it_steckdose_zustand", checker._existing_entities) is None


def test_what_the_log_answers_is_a_suggestion_like_any_other(checker):
    checker.rename_log = Log({"sensor.gone": "sensor.balkon_ventil_wasserstatus"})

    answer = checker._where_it_went("sensor.gone", checker._existing_entities)

    assert isinstance(answer, Suggestion)
    assert answer.to_dict()["entity_id"] == "sensor.balkon_ventil_wasserstatus"


class Recorded:
    """A rename log that keeps what it was told, in order."""

    def __init__(self):
        self.written = []

    def record(self, old_entity_id, new_entity_id, friendly_name=None, timestamp=None):
        self.written.append((old_entity_id, new_entity_id))


def test_a_swap_writes_down_which_entity_took_over():
    """Without it the chain ends at the interim id the old device was parked on."""
    from device_swap import SwapExecutor

    log = Recorded()

    class Registry:
        rename_log = log

    executor = SwapExecutor.__new__(SwapExecutor)
    executor.entity_registry = Registry()

    executor._note_the_succession("binary_sensor.window_old", "binary_sensor.window_new")

    assert log.written == [("binary_sensor.window_old", "binary_sensor.window_new")]


def test_a_swap_that_kept_the_id_writes_nothing():
    from device_swap import SwapExecutor

    log = Recorded()

    class Registry:
        rename_log = log

    executor = SwapExecutor.__new__(SwapExecutor)
    executor.entity_registry = Registry()

    executor._note_the_succession("binary_sensor.same", "binary_sensor.same")

    assert log.written == []


def test_a_swap_without_a_log_carries_on():
    from device_swap import SwapExecutor

    class Registry:
        pass

    executor = SwapExecutor.__new__(SwapExecutor)
    executor.entity_registry = Registry()

    executor._note_the_succession("binary_sensor.one", "binary_sensor.two")


async def test_the_same_window_in_another_room_is_not_offered(checker):
    """Three words of five, and two different windows in two different rooms.

    A name starts with where the thing is, so a candidate that starts somewhere
    else is not the one that was lost.
    """
    checker._existing_entities = {"binary_sensor.kinderzimmer_mittleres_fenster_zustand"}
    checker._entity_details = {
        "binary_sensor.kinderzimmer_mittleres_fenster_zustand": {"friendly_name": "Kinderzimmer"}
    }

    assert await checker.get_suggestions("binary_sensor.buro_mittleres_fenster_zustand") == []


def test_a_different_first_word_scores_nothing(checker):
    score, reasons = checker._calculate_similarity(
        "binary_sensor.buro_mittleres_fenster_zustand",
        "binary_sensor.kinderzimmer_mittleres_fenster_zustand",
    )

    assert score == 0.0
    assert reasons == []
