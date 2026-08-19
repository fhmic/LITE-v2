' Start LITE.vbs
'
' Double-click this to launch LITE with NO console/PowerShell window --
' only the LITE app window itself appears, exactly like a normal
' double-click app. Closing LITE's window closes LITE; there's no
' separate console keeping it alive or printing to it.
'
' How this differs from the first version of this script: that one hid
' python.exe's console via a window-style flag, which on this machine
' ended up hiding the whole app, not just the console. This version uses
' pythonw.exe instead -- a separate executable with no console subsystem
' at all, so there is nothing to hide or accidentally couple to the GUI
' window's visibility. main.py already redirects stdout/stderr to
' logs\console.log when no console is present (added specifically for
' this), so nothing crashes and you can still check that file if
' something goes wrong.
'
' Auto-detects a virtual environment next to main.py (checks .venv, venv,
' env, in that order) and uses ITS pythonw.exe specifically, since a
' silent launch does not activate a venv the way an open terminal does.
' Falls back to whatever "pythonw" resolves to on PATH if none is found.

Set fso    = CreateObject("Scripting.FileSystemObject")
Set shell  = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

venvNames = Array(".venv", "venv", "env")
pythonwExe = ""

For Each name In venvNames
    candidate = scriptDir & "\" & name & "\Scripts\pythonw.exe"
    If fso.FileExists(candidate) Then
        pythonwExe = candidate
        Exit For
    End If
Next

If pythonwExe = "" Then
    pythonwExe = "pythonw.exe"   ' fall back to PATH
End If

shell.CurrentDirectory = scriptDir

' Window-style argument is irrelevant here since pythonw.exe has no
' console to show in the first place -- kept at 0 for consistency, it
' has no effect either way. 2nd arg False = don't wait for LITE to exit
' before this script ends.
shell.Run """" & pythonwExe & """ main.py", 0, False
