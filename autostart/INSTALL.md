# Auto-start the Brain on every login

Two minutes, then the scheduler runs 24/7 every time you turn on your laptop.

## Option A: Task Scheduler (recommended)

Auto-relaunches on crash, auto-starts at login, restart-survives.

1. Press `Win + R`, type `taskschd.msc`, hit Enter
2. Right sidebar → **Import Task...**
3. Pick `C:\Users\chris\clipfarmer\autostart\Clipfarmer_Scheduler.xml`
4. Review the dialog (no changes needed) → click **OK**
5. To test now: find "Clipfarmer Scheduler" in the task list → right-click → **Run**
6. Confirm it's working: open Telegram — within 60 seconds you should be able to type "status" to the bot and get a reply

The task will now fire on every login. It runs hidden — no console window, no taskbar entry. It also auto-restarts up to 3 times if the scheduler crashes.

## Option B: Startup folder (simpler, less robust)

Auto-starts at login but won't auto-restart on crash.

1. Press `Win + R`, type `shell:startup`, hit Enter
2. A folder opens — copy `start_clipfarmer.bat` into it
3. Done — next login it fires

## To stop the scheduler

- **Task Scheduler version:** open `taskschd.msc` → find Clipfarmer Scheduler → right-click → **End**, then **Disable** if you want it to stay off
- **Startup folder version:** Task Manager → Background Processes → find a `pythonw.exe` process whose command line includes `clipfarmer` → End task. Delete the .bat from the Startup folder to stop it relaunching at login.

## To see what it's doing

`logs/scheduler-autostart.log` — everything stdout/stderr from the background process.
`logs/scheduler.log` — the scheduler's structured logs (rotated).

## Why this is the "free smoothest" 24/7

- Costs: $0
- Works while the laptop is on + connected to internet
- Survives reboots and crashes
- No console window to accidentally close

Limitation: when the laptop sleeps/shuts down, the scheduler pauses. To go truly 24/7 laptop-free, deploy to a cloud VM (Hetzner box, Oracle Always Free, etc.) — but that's overkill until volume justifies it.
