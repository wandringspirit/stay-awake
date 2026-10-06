# Stay Awake

Keeps your Windows laptop from sleeping, either right now or on a schedule. Nothing to install: it only needs Python (3.11+).

**Open it:** double-click `stay_awake.pyw`.

## Using it

- **Keep awake right now**: 30 min / 1 hour / 2 hours / 4 hours / Until a time... / Indefinitely.
  These share one "quick" slot (clicking another replaces the last one) and disappear once they expire.
- **+ New schedule...**: create an *arm*:
  - **Time window**: e.g. 09:00 to 18:00. Repeat *Once (on a date)*, *Every day*, *Weekdays*, *Weekends*, or *Custom days*.
    If the end is earlier than the start (22:00 to 02:00), it runs past midnight.
  - **Until a specific date and time**: starts now.
  - **Indefinitely**: until you disarm it.
- **The list**: select one or more arms and click **Arm**, **Disarm**, **Edit...** or **Delete**.
  You can also double-click a row to edit it, press Space to arm/disarm, press Delete to remove it, or right-click for a menu.
- **Stop now** (in the green banner): disarms whatever is keeping the laptop awake at that moment.

## How it works

The window only edits a list saved in `%APPDATA%\StayAwake\arms.json`. A small background agent (`stay_awake.pyw --agent`, no window)
watches that list and asks Windows to stay awake (`SetThreadExecutionState`) while an armed entry is active.
It releases the request as soon as nothing is active. This works the same way as video players or PowerToys Awake, and it doesn't change any of your power settings.

- Closing the window is fine. The agent keeps running until you click **Stop agent** or sign out.
- Tick **Start agent when I sign in to Windows** so schedules survive a restart. This adds a `StayAwakeAgent` entry under
  `HKCU\...\CurrentVersion\Run`. Untick it to remove the entry.
- **Open log** shows when the agent started and stopped keeping the PC awake (`%APPDATA%\StayAwake\agent.log`).

## Limits

- Closing the lid, pressing the power button, or choosing Sleep from the Start menu still sleeps the laptop. Those follow Windows'
  own settings. To keep it running with the lid shut, set *Control Panel > Power Options > Choose what closing the lid does* to *Do nothing*.
- This laptop uses Modern Standby, so leave **Keep screen on** ticked for the most reliable results.
- If you move `stay_awake.pyw`, open it once from the new location and the sign-in entry updates itself.
