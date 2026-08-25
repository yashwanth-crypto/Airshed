' Start the archive supervisor with no console window at all.
'
' Why this exists: the loop died three times in two days (2026-08-24 22:35 UTC,
' 2026-08-25 12:06 and 12:30), each time without a single line in the log --
' the signature of a process killed rather than one that failed. A minimised
' console is still a window, and a window is a thing that gets closed while
' tidying the taskbar. Archived forecast runs cannot be backfilled, so the
' cheapest fix is to remove the window.
'
' Put a shortcut to THIS file in the Startup folder (Win+R -> shell:startup),
' not to run_archive.bat.
'
' To stop it: scripts\stop_archive.bat. To watch it: the log.
'     Get-Content C:\SIH\data\archive.log -Tail 5
' To check whether it is alive at all, ask for a yes-or-no answer:
'     .venv\Scripts\python.exe scripts\daily_archive.py --health

Set shell = CreateObject("WScript.Shell")
' 0 = hidden window, False = do not wait for it to finish.
shell.Run """C:\SIH\scripts\run_archive.bat""", 0, False
