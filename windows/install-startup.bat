@echo off
rem Tao shortcut trong thu muc Startup -> tu chay khi dang nhap Windows.
cd /d "%~dp0"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\VN Stock.lnk'); $s.TargetPath='%~dp0start.bat'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
echo Da them vao Startup. Go bo: xoa "VN Stock.lnk" trong shell:startup
