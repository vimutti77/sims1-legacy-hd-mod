@echo off
:: Double-click this, or add it to Steam as a non-Steam game.
:: Extra options are passed through, e.g.:  Sims4K.bat -Width 1280 -Height 720
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File "%~dp0SimsLauncher.ps1" %*
