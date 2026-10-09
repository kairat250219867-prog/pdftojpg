@echo off
rem Run PDF editor; installs dependencies on first launch
python -c "import PySide6, pymupdf" 2>nul || python -m pip install -r "%~dp0requirements.txt"
start "" pythonw "%~dp0pdf_editor.py" %*
