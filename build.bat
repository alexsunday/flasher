
call venv\Scripts\activate.bat
python -m PyQt5.uic.pyuic -x main.ui -o dlg.py
python -m PyQt5.pyrcc_main resource.qrc -o resource_rc.py
pyinstaller -F --noupx -w --win-no-prefer-redirects --clean --icon brush.ico flasher.py
