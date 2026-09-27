"""Build a portable Omnigram for the current OS into dist/. CI runs this on each platform; so can you:

    uv run --extra gui --group build python build.py

    Windows  Omnigram-<version>-windows-x64.zip      (folder with Omnigram.exe)
    macOS    Omnigram-<version>-macos-<arch>.dmg     (Omnigram.app)
    Linux    Omnigram-<version>-linux-x64.AppImage

The compiler is Nuitka with its PySide6 plugin — what pyside6-deploy runs underneath. Calling it directly
keeps the build free of pyside6-deploy's machine-specific spec file and its `pip install` step.
Builds are unsigned (see README → Download).
"""
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
BUILD, DIST = ROOT / "build", ROOT / "dist"
ICON = ROOT / "assets" / "logo.png"
ARCH = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
APPIMAGETOOL = "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"


def server_wheel() -> Path:
    """This version's package, bundled so the app installs the same version on a server (remote.find_wheel)."""
    out = BUILD / "wheel"
    shutil.rmtree(out, ignore_errors=True)
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True)
    return next(out.glob("omnigram-*.whl"))


def nuitka(*extra: str):
    wheel = server_wheel()
    subprocess.run([
        sys.executable, "-m", "nuitka", "main.py",
        "--enable-plugin=pyside6", "--include-qt-plugins=platforminputcontexts", "--noinclude-qt-translations",
        "--include-package=phonenumbers",  # loads its per-region data modules by name at runtime
        "--include-package-data=tzdata",  # zoneinfo's timezone database on Windows (warm-up active hours)
        "--include-distribution-metadata=omnigram",  # omnigram.__version__ reads it
        "--include-package-data=omnigram",  # the bundled Lucide icons (omnigram/icons/*.svg + LICENSE)
        f"--include-data-files={wheel}=server/{wheel.name}",  # what Server → Install puts on the server
        "--include-module=omnigram.server",  # imported by name for `serve`, and by remote.py
        f"--output-dir={BUILD}", "--output-filename=Omnigram", "--assume-yes-for-downloads", *extra,
    ], cwd=ROOT, check=True)


def windows() -> Path:
    numeric = VERSION.split("+")[0].split("-")[0]  # Windows version resources accept digits only
    nuitka("--mode=standalone", "--windows-console-mode=disable", f"--windows-icon-from-ico={ICON}",
           "--product-name=Omnigram", "--company-name=h4nz4", "--file-description=Omnigram",
           f"--product-version={numeric}", f"--file-version={numeric}")
    folder = BUILD / "Omnigram"
    shutil.rmtree(folder, ignore_errors=True)
    (BUILD / "main.dist").rename(folder)
    archive = shutil.make_archive(str(DIST / f"Omnigram-{VERSION}-windows-{ARCH}"), "zip", BUILD, "Omnigram")
    return Path(archive)


def macos() -> Path:
    nuitka("--mode=app", f"--macos-app-icon={ICON}", "--macos-app-name=Omnigram", f"--macos-app-version={VERSION}")
    app = BUILD / "Omnigram.app"
    shutil.rmtree(app, ignore_errors=True)
    next(BUILD.glob("*.app")).rename(app)
    dmg = DIST / f"Omnigram-{VERSION}-macos-{ARCH}.dmg"
    subprocess.run(["hdiutil", "create", "-volname", "Omnigram", "-srcfolder", str(app), "-ov", "-format", "UDZO",
                    str(dmg)], check=True)
    return dmg


def linux() -> Path:
    nuitka("--mode=standalone", f"--linux-icon={ICON}")
    appdir = BUILD / "Omnigram.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    (BUILD / "main.dist").rename(appdir)
    (appdir / "AppRun").symlink_to("Omnigram")
    shutil.copy(ICON, appdir / "omnigram.png")
    (appdir / "omnigram.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Omnigram\nComment=Telegram account manager\n"
        "Exec=Omnigram\nIcon=omnigram\nCategories=Network;\nTerminal=false\n", "utf-8")
    tool = BUILD / "appimagetool"
    if not tool.exists():
        urllib.request.urlretrieve(APPIMAGETOOL, tool)
        tool.chmod(0o755)
    image = DIST / f"Omnigram-{VERSION}-linux-{ARCH}.AppImage"
    # extract-and-run: CI runners have no FUSE to mount appimagetool's own AppImage
    subprocess.run([str(tool), str(appdir), str(image)], check=True,
                   env={**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"})
    return image


if __name__ == "__main__":
    DIST.mkdir(exist_ok=True)
    build = {"win32": windows, "darwin": macos}.get(sys.platform, linux)
    print(f"built {build()}")
