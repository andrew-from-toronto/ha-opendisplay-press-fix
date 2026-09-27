# OpenDisplay press fix

A small Home Assistant custom integration that stops the core **OpenDisplay** integration losing
button presses.

## The problem

An OpenDisplay tag reports its buttons in its BLE advertisement: one byte holding the id of the button
last pressed, that button's own press counter (0-15, wrapping), and whether it is held. After every
press and release the firmware advertises every 20-30 ms for 3 seconds.

Home Assistant 2026.9 pins `py-opendisplay==7.15.0`, whose `AdvertisementTracker` reports a press only
when an advertisement caught the button **held**. A quick tap that no proxy heard during the few
advertisements sent while a finger was down moves only the counter, which 7.15 reports as
`press_count_changed`. No entity listens for that, so the press is lost. The later 7.17 rewrite
synthesises the press from the counter, but it compares `current > previous` and so still loses the
press that wraps the counter from 15 back to 0.

## What this does

It replaces `AdvertisementTracker.update` so that a press is **a button's counter advancing**, compared
modulo 16 against the last count seen for that button:

- a tap nobody saw held is still a press, because the counter moved;
- the wrap from 15 to 0 counts;
- a release that was never heard is closed when the next button appears;
- a late advertisement relayed by a second proxy, whose counter is behind, is ignored instead of read
  as 15 phantom presses;
- a tag reboot starts a new baseline when its reboot flag first appears. The firmware keeps that flag
  up until the next BLE connection, and presses made meanwhile still count.

It patches the class in place, so it is not a copy of the core integration and needs no upkeep when HA
updates. At startup it checks whether the installed tracker already handles the wrap. If it does, it
changes nothing and logs that it can be removed.

## Install

1. HACS -> Custom repositories -> add this repository as an *Integration*, then download it.
2. Add one line to `configuration.yaml`:

   ```yaml
   opendisplay_press_fix:
   ```

3. Restart Home Assistant. The log shows
   `Patched py-opendisplay's AdvertisementTracker to count presses from the press counter`.

## Tests

```sh
pip install pytest py-opendisplay==7.15.0
pytest
```

The tests run the real `py-opendisplay` tracker with Home Assistant stubbed out.
