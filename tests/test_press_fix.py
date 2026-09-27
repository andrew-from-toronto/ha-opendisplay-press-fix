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
    return t


def ups(tracker, *values):
    """button_ids of every button_up across a run of advertisements, the first being the baseline."""
    out = []
    for value in values:
        out += [e.button_id for e in tracker.update(TAG, adv(value), timestamp=0.0) if e.event_type == "button_up"]
    return out


def test_the_shipped_tracker_loses_a_tap_nobody_saw_held():
    shipped = AdvertisementTracker()
    shipped.update(TAG, adv(byte(0, 3)))
    assert not [e for e in shipped.update(TAG, adv(byte(0, 4))) if e.event_type == "button_up"]


def test_a_tap_seen_only_by_its_counter_is_a_press(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4)) == [0]


def test_a_held_press_is_one_press_not_two(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4, True), byte(0, 4)) == [0]


def test_the_press_that_wraps_the_counter_counts(tracker):
    # Button 0 at 15 -> 0 is an all-zero byte, the same byte a freshly booted tag sends; the reboot
    # flag, not the byte, is what tells them apart.
    assert ups(tracker, byte(0, 15), byte(0, 0)) == [0]


def test_a_bounced_press_that_skips_zero_still_wraps(tracker):
    assert ups(tracker, byte(1, 15), byte(1, 1)) == [1]


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


def test_a_reboot_is_a_new_baseline(tracker):
    tracker.update(TAG, adv(byte(0, 9)))
    assert not tracker.update(TAG, adv(byte(0, 0), reboot=True), timestamp=0.0)
    assert ups(tracker, byte(0, 1)) == [0]


def reboot_run(tracker, *values):
    out = []
    for value in values:
        out += [e.button_id for e in tracker.update(TAG, adv(value, reboot=True), timestamp=0.0)
                if e.event_type == "button_up"]
    return out


def test_presses_after_a_reboot_count_though_the_flag_stays_up(tracker):
    # The firmware clears the flag only on its next BLE connection, so every advertisement until the
    # next screen push carries it.
    tracker.update(TAG, adv(byte(2, 9)))
    assert reboot_run(tracker, 0, 0, byte(0, 1), byte(0, 2)) == [0, 0]


def test_a_reboot_after_a_screen_push_is_seen_again(tracker):
    tracker.update(TAG, adv(byte(2, 9)))
    reboot_run(tracker, 0)
    assert ups(tracker, 0, byte(0, 1)) == [0]
    assert reboot_run(tracker, 0) == []


def test_a_release_missed_before_another_button_still_closes_the_first(tracker):
    assert ups(tracker, byte(0, 3), byte(0, 4, True), byte(2, 7)) == [0, 2]


def test_upstream_is_not_yet_fixed():
    assert not fix._is_fixed_upstream()
