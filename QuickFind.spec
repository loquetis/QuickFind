# PyInstaller spec: one-file, windowed QuickFind.exe with the web UI bundled.
# Build:  pyinstaller --noconfirm --clean QuickFind.spec
a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    datas=[('web', 'web')],
    hiddenimports=['server', 'db', 'indexer', 'watcher', 'winshell', 'storage', 'appserver', 'sqlite3'],
    excludes=['tkinter', 'unittest', 'pydoc', 'test', 'numpy'],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='QuickFind',
    console=False,
    icon='quickfind.ico',
    upx=False,
    version='version_info.txt',
)
