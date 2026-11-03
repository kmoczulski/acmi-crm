' Runs sync_mail.cmd without opening a console window.
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\sync_mail.cmd""", 0, True
