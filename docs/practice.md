# Practice mode (2026-11-18)

For showing people how to use AutoCAM live without keeping anything: the same service, pages and Fusion, on a
**practice Trello board** ([5940 AutoCAM practice](https://trello.com/b/5KYqzX7N/5940-autocam-practice)) with its own
records. The Onshape panel, the shop page (autocam.team5940.org/shop), the machine screen and Slack sign-in all work as
usual, and every page has a purple bar: "Practice mode: nothing you do here is kept".

## Switching (on the shop laptop)

```powershell
powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1 on
powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1 off
powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1        # which is running
```

- **on:** stops the real service (task "AutoCAM service"), archives every card on the practice board (not the System,
  "How to add a part" or "New part" cards), starts practice's records fresh (`autocam practice-reset`) and starts the
  service in practice mode (task "AutoCAM practice", `autocam run --practice`, made the first time; never started by
  itself).
- **off:** stops practice, puts back what it gathered that the real service needs (`autocam practice-done`: names,
  Slack sign-ins, names typed in the panel, the Onshape call count, the tunnel's process), takes practice's jobs that
  Fusion hasn't started off the queue, and starts the real service.
- **A reboot always comes back in the real mode.** While practice is on, the real board isn't watched: nothing on it
  moves, nothing new on it is CAM'd.

## What practice changes (`config.practice_config`)

| | Real | Practice |
|---|---|---|
| Trello | the real board | the practice board (`config/practice.toml`) |
| Records | `state/` | `state-practice/`, made fresh at each `on` (only who's who carried in) |
| Logs | `logs/` | `logs/practice/` |
| Runs | r114, r115... | p001, p002... (the queue is shared with Fusion; "p" job folders never take a real run's name) |
| Onshape | a material picked in the panel is set on the part; webhooks for open sheets | nothing written: the material goes on the card; no webhooks |

Everything else (Fusion, the post, the checks, nesting, the shop's lists, offcut photos, the machine screen) is the
real code. Refused if `practice.toml` names the real board or any of its lists.
