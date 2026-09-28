"""Checks for opendisplay_press_fix against the real py-opendisplay tracker.

Run with py-opendisplay importable (the version HA pins; 7.15.0 on HA 2026.9):
    pip install py-opendisplay==7.15.0 pytest && pytest

Home Assistant itself is not needed: the two names the component imports from it are stubbed.
"""

import sys
import types

for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.typing"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.helpers.typing"].ConfigType = dict

sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__file__), "..", "custom_components"))

import pytest
from opendisplay.models.advertisement import AdvertisementData, AdvertisementTracker

import opendisplay_press_fix as fix

TAG = "D1:CB:43:E5:45:E0"


def byte(button, count, pressed=False):
    return button | (count & 0x0F) << 3 | (0x80 if pressed else 0)


def adv(value, reboot=False):
    return AdvertisementData(battery_mv=4000, temperature_c=20.0, loop_counter=0, format_version="v1",
                             reboot_flag=reboot, dynamic_data=bytes([value]) + bytes(10))


@pytest.fixture
def tracker():
    t = AdvertisementTracker()
    t.update = types.MethodType(fix.fixed_update, t)
    t.clock = 0.0
    return t


# Well past the settle window, so each advertisement in a run is a live one after the baseline.
STEP = 30.0


def run(tracker, *values, reboot=False, step=STEP):
    """button_ids of every button_up across a run of advertisements, each `step` seconds apart."""
    out = []
    for value in values:
        events = tracker.update(TAG, adv(value, reboot=reboot), timestamp=tracker.clock)
        tracker.clock += step
        out += [e.button_id for e in events if e.event_type == "button_up"]
    return out


def ups(tracker, *values):
    return run(tracker, *values)


def test_the_shipped_tracker_loses_a_tap_nobody_saw_held():
    shipped = AdvertisementTracker()
    shipped.update(TAG, adv(byte(0, 3)))
    assert not [e for e in shipped.update(TAG, adv(byte(0, 4))) if e.event_type == "button_up"]


def test_a_tap_seen_only_by_its_counter_is_a_press(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4)) == [0]


def test_a_held_press_is_one_press_not_two(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4, True), byte(0, 4)) == [0]


def test_the_press_that_wraps_the_counter_counts(tracker):
    assert ups(tracker, byte(1, 15), byte(1, 1)) == [1]


def test_a_wrap_onto_zero_caught_held_still_counts(tracker):
    assert ups(tracker, byte(0, 15), byte(0, 0, True), byte(0, 0)) == [0]


def test_a_zero_count_is_never_a_press(tracker):
    # The one real press given up: a wrap onto exactly zero that nothing caught held. It reads the same
    # as a tag that has rebooted, and a missed meal is cheaper than an invented one.
    assert ups(tracker, byte(0, 15), byte(0, 0)) == []
    assert ups(tracker, byte(0, 1)) == [0]


def test_a_repeat_of_the_same_advertisement_is_nothing(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4), byte(0, 4), byte(0, 4)) == [0]


def test_another_button_is_a_press_of_that_button(tracker):
    assert ups(tracker, byte(0, 3), byte(2, 7)) == [2]


def test_a_late_advertisement_from_another_proxy_is_not_a_press(tracker):
    # Andrew FED, then Michelle FED, then a second proxy relays the older Andrew FED byte.
    assert ups(tracker, byte(0, 3), byte(0, 4), byte(2, 7), byte(0, 4)) == [0, 2]
    # And the counter going backwards on one button is the same thing.
    assert ups(tracker, byte(0, 5), byte(0, 4)) == [0]


def test_after_a_late_advertisement_the_next_real_press_still_counts(tracker):
    assert ups(tracker, byte(0, 5), byte(0, 4), byte(0, 6)) == [0]


def test_the_2026_09_27_phantom_is_not_a_press(tracker):
    # Home Assistant restarted; the tag had rebooted meanwhile and HA's own setup connection had already
    # cleared its reboot flag. The coordinator got the advertisement cached from before the restart
    # (Andrew FED, count 14), then 260 ms later the live one, all zeros.
    assert run(tracker, byte(0, 14), 0, step=0.26) == []


def test_a_cached_advertisement_for_another_button_is_not_a_press_either(tracker):
    assert run(tracker, byte(1, 5), byte(0, 14), step=0.26) == []


def test_after_settling_a_real_press_counts(tracker):
    assert run(tracker, byte(0, 14), 0, step=0.26) == []
    tracker.clock += fix.SETTLE_SECONDS
    assert ups(tracker, byte(2, 1)) == [2]


def test_a_reboot_is_a_new_baseline(tracker):
    ups(tracker, byte(0, 9))
    assert run(tracker, byte(0, 0), reboot=True) == []
    assert ups(tracker, byte(0, 1)) == [0]


def test_presses_after_a_reboot_count_though_the_flag_stays_up(tracker):
    # The firmware clears the flag only on its next BLE connection, so every advertisement until the
    # next screen push carries it.
    ups(tracker, byte(2, 9))
    assert run(tracker, 0, 0, byte(0, 1), byte(0, 2), reboot=True) == [0, 0]


def test_a_reboot_after_a_screen_push_is_seen_again(tracker):
    ups(tracker, byte(2, 9))
    run(tracker, 0, reboot=True)
    assert ups(tracker, 0, byte(0, 1)) == [0]
    assert run(tracker, 0, reboot=True) == []


def test_a_release_missed_before_another_button_still_closes_the_first(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4, True), byte(2, 7)) == [0, 2]


def test_upstream_is_not_yet_fixed():
    assert not fix._is_fixed_upstream()
