"""Build the Python distributions and, optionally, a Windows folder bundle."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = project["project"]["version"]
    tree = ast.parse((ROOT / "src/merge1c/__init__.py").read_text(encoding="utf-8"))
    actual = next(ast.literal_eval(item.value) for item in tree.body
                  if isinstance(item, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "__version__" for target in item.targets))
    if actual != declared:
        raise ValueError(f"Versions differ: pyproject.toml={declared}, __version__={actual}")
    return declared


def windows_arch() -> str:
    # Python's build target is accurate even under Windows ARM emulation.
    target = sysconfig.get_platform()
    try:
        return {"win-amd64": "x64", "win-arm64": "arm64", "win32": "x86"}[target]
    except KeyError as exc:
        raise ValueError(f"Unsupported Windows Python target: {target}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", action="store_true", help="Also build the portable Windows folder and ZIP")
    arguments = parser.parse_args()
    if arguments.windows and sys.platform != "win32":
        parser.error("The Windows distribution must be built on Windows")
    required = ["build", "setuptools", "wheel"] + (["PyInstaller"] if arguments.windows else [])
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        parser.error("Missing build tools: " + ", ".join(missing) + ". Run: python -m pip install -r scripts/requirements-build.txt")
    release = version()
    output, work = ROOT / "dist", ROOT / "build"
    work.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    # Publish only completed files. Existing unrelated outputs are never deleted.
    with tempfile.TemporaryDirectory(prefix="release-", dir=work) as temporary:
        stage = Path(temporary)
        run("-m", "build", "--no-isolation", "--outdir", str(stage))
        wheel = stage / f"merge1c-{release}-py3-none-any.whl"
        with zipfile.ZipFile(wheel) as archive:
            for name in ("merge1c.toml", "merge1c.local.example.toml"):
                archive.getinfo("merge1c/templates/" + name)
        artifacts = [wheel, stage / f"merge1c-{release}.tar.gz"]
        if arguments.windows:
            arch = windows_arch()
            folder_name = f"merge1c-{release}-windows-{arch}"
            bundle_root = stage / "windows"
            run("-m", "PyInstaller", "--noconfirm", "--onedir", "--console", "--noupx",
                "--name", "merge1c", "--paths", str(ROOT / "src"),
                "--add-data", str(ROOT / "src/merge1c/templates") + ":merge1c/templates",
                "--distpath", str(bundle_root),
                "--workpath", str(stage / "pyinstaller"), "--specpath", str(stage),
                str(ROOT / "scripts/merge1c_entry.py"))
            bundle = bundle_root / "merge1c"
            # This must succeed using the bundled interpreter and packaged TOML.
            subprocess.run([str(bundle / "merge1c.exe"), "--version"], cwd=stage, check=True)
            renamed = stage / folder_name
            bundle.rename(renamed)
            shutil.copytree(ROOT / "docs", renamed / "docs")
            for name in ("README.md", "CHANGELOG.md"):
                shutil.copyfile(ROOT / name, renamed / name)
            shutil.copyfile(Path(sys.base_prefix) / "LICENSE.txt", renamed / "LICENSE-Python.txt")
            artifacts.append(Path(shutil.make_archive(str(stage / folder_name), "zip", stage, folder_name)))
        for artifact in artifacts:
            shutil.copyfile(artifact, output / artifact.name)
        # Keep the extracted bundle for immediate local use as well as the ZIP.
        if arguments.windows:
            destination = output / folder_name
            if destination.exists():
                # Only replace our exact versioned bundle, never an arbitrary tree.
                if destination.is_symlink() or not (destination / "merge1c.exe").is_file():
                    raise ValueError(f"Refusing to replace an unknown output directory: {destination}")
                if destination.resolve().parent != output.resolve():
                    raise ValueError(f"Output directory escapes dist: {destination}")
                shutil.rmtree(destination)
            shutil.copytree(renamed, destination)
        published = [output / item.name for item in artifacts]
        checksums = []
        for item in published:
            with item.open("rb") as stream:
                checksums.append(hashlib.file_digest(stream, "sha256").hexdigest() + "  " + item.name + "\n")
        (output / "SHA256SUMS.txt").write_text("".join(checksums), encoding="ascii")
        print("\nBuilt:")
        for item in published:
            print(item)
        if arguments.windows:
            print(output / folder_name / "merge1c.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
