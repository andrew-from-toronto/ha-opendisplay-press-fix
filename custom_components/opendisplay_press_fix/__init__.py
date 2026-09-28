"""Stop Home Assistant's OpenDisplay integration losing short button presses.

The Minnie panel's buttons ride in the tag's BLE advertisement, one byte shared by all four buttons: the
id of the button last pressed (bits 0-2), that button's own press counter, which increments on every
press and wraps at 16 (bits 3-6), and its level (bit 7). After every edge the firmware advertises every
20-30 ms for 3 s, so one press puts ~100 advertisements carrying the new counter on the air.

The core ``opendisplay`` integration hands each advertisement to py-opendisplay's ``AdvertisementTracker``,
and the version HA 2026.9 pins (7.15.0) reports a press only when an advertisement happened to catch the
button *held* - the handful sent while a finger is down. A tap that none of those reached a proxy for
moves only the counter, which 7.15 reports as ``press_count_changed``, an event no entity listens for,
and the press is gone. Upstream's later rewrite (7.17) synthesises the press from the counter but
compares ``curr > prev``, and so still loses the press that wraps it 15 -> 0.

This replaces ``AdvertisementTracker.update`` on the class, so the integration's existing tracker picks
it up with no reload. A press is a button's counter advancing, compared modulo 16 against the last
count seen *for that button* - never against whatever button the shared byte showed last, which is
what lets a late advertisement relayed by a second proxy be recognised as old news rather than a
phantom press. A jump of more than ``MAX_PRESSES`` is taken to be exactly that: the past arriving late.

Delete this, and its line in configuration.yaml, once HA ships a py-opendisplay that survives the
wrap; ``_is_fixed_upstream`` makes it stand down by itself and log that it can go.
"""

from __future__ import annotations

import logging
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "opendisplay_press_fix"

# The most presses of one button believed to fit between two advertisements Home Assistant hears. Any
# larger jump round the 16-count ring is an older advertisement arriving after a newer one.
MAX_PRESSES = 7

# How long after tracking starts (Home Assistant starting, the integration reloading, the tag rebooting)
# advertisements only set the baseline. Home Assistant hands the coordinator the last advertisement it
# cached before a restart, and the first live one follows within a second; measured 2026-09-27, a tag
# that had rebooted in between turned that pair into a phantom press 260 ms after start-up.
SETTLE_SECONDS = 10.0


def fixed_update(self, address, advertisement, timestamp=None):
    """Process one advertisement and return the button transitions it implies."""
    from opendisplay.models.advertisement import ButtonChangeEvent

    # Last count seen per (address, byte, button). Kept beside upstream's own state rather than in it,
    # so the tracker's other methods (reset) still see the shape they expect.
    counts = self.__dict__.setdefault("_press_fix_counts", {})

    if advertisement.format_version != "v1":
        self._last_by_address.pop(address, None)
        return []

    current = self._watched(advertisement.button_events)
    previous = self._last_by_address.get(address)

    # A rebooted tag starts every counter from zero: a new baseline, never a press. The firmware holds
    # the flag up until its next BLE connection - the next screen push, which may be hours away - so
    # only the advertisement that first carries it is the reboot; presses after it count from zero.
    flagged = self.__dict__.setdefault("_press_fix_flagged", {})
    rebooted = advertisement.reboot_flag and not flagged.get(address, False)
    flagged[address] = bool(advertisement.reboot_flag)

    now = timestamp if timestamp is not None else time.time()
    settling = self.__dict__.setdefault("_press_fix_settle_until", {})

    if previous is None or len(previous) != len(current) or rebooted:
        for key in [k for k in counts if k[0] == address]:
            del counts[key]
        settling[address] = now + SETTLE_SECONDS

    if now < settling.get(address, 0.0):
        for curr in current:
            counts[(address, curr.byte_index, curr.button_id)] = curr.press_count
        self._last_by_address[address] = current
        return []

    events = []

    for i, (prev, curr) in enumerate(zip(previous, current, strict=False)):
        if prev.raw == curr.raw:
            continue

        def emit(event_type, button_id, pressed):
            events.append(ButtonChangeEvent(
                address=address, byte_index=curr.byte_index, event_type=event_type, button_id=button_id,
                pressed=pressed, press_count=curr.press_count, previous_press_count=prev.press_count,
                raw=curr.raw, previous_raw=prev.raw, timestamp=now))

        key = (address, curr.byte_index, curr.button_id)

        # A count of zero, not held, is a button nothing has pressed since the tag booted. The firmware's
        # reboot flag cannot be relied on to say so: Home Assistant connects to the tag while setting the
        # integration up, and that connection clears it before the first advertisement is read. The one
        # real press this misses - a wrap onto exactly zero that no advertisement caught held - is the
        # price of never inventing a meal.
        if curr.press_count == 0 and not curr.pressed and not prev.pressed:
            counts[key] = 0
            continue

        known = counts.get(key)
        # Unknown only for a button first seen since Home Assistant started, and then only because
        # the shared byte moved to it - which only a press of it does.
        presses = 1 if known is None else (curr.press_count - known) & 0x0F

        if presses > MAX_PRESSES:
            current[i] = prev
            continue
        counts[key] = curr.press_count

        if prev.button_id != curr.button_id:
            emit("button_slot_changed", curr.button_id, curr.pressed)
            # The previous button's release was never heard, though its press was: close it.
            if prev.pressed:
                emit("button_up", prev.button_id, False)
            if presses:
                emit("button_down", curr.button_id, True)
                if not curr.pressed:
                    emit("button_up", curr.button_id, False)
        elif prev.pressed and not curr.pressed:
            emit("button_up", curr.button_id, False)
            # The held press was counted when it went down, so any advance is a further whole press.
            if presses:
                emit("button_down", curr.button_id, True)
                emit("button_up", curr.button_id, False)
        elif curr.pressed and not prev.pressed:
            emit("button_down", curr.button_id, True)
        elif presses and prev.pressed:
            # Held, released unheard, and held again.
            emit("button_up", curr.button_id, False)
            emit("button_down", curr.button_id, True)
        elif presses:
            emit("button_down", curr.button_id, True)
            emit("button_up", curr.button_id, False)

        if presses:
            emit("press_count_changed", curr.button_id, curr.pressed)

    self._last_by_address[address] = current
    return events


def _is_fixed_upstream() -> bool:
    """Whether the installed tracker already counts a press that wraps the counter."""
    from opendisplay.models.advertisement import AdvertisementData, AdvertisementTracker

    def adv(button_byte):
        return AdvertisementData(battery_mv=4000, temperature_c=20.0, loop_counter=0, format_version="v1",
                                 reboot_flag=False, dynamic_data=bytes([button_byte]) + bytes(10))

    tracker = AdvertisementTracker()
    tracker.update("probe", adv(15 << 3))
    events = tracker.update("probe", adv(1 << 3), timestamp=0.0)
    return any(e.event_type == "button_up" for e in events)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Patch the tracker once, at startup."""
    from opendisplay.models.advertisement import AdvertisementTracker

    if _is_fixed_upstream():
        _LOGGER.warning(
            "py-opendisplay now counts a wrapped button press itself, so %s changed nothing. "
            "Remove it from custom_components and configuration.yaml", DOMAIN)
        return True

    AdvertisementTracker.update = fixed_update
    _LOGGER.info("Patched py-opendisplay's AdvertisementTracker to count presses from the press counter")
    return True
