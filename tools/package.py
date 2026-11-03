"""Create a source release using an explicit list, never local logs/configuration."""

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "codex-context-bridge-0.1.0.zip"
FILES = ["README.md", "LICENSE", "config.example.json", "start.cmd", "stop.cmd",
         "scripts/launch.ps1", "scripts/python-runtime.json", "tools/package.py", "docs/settings.png"]
FILES += [str(path.relative_to(ROOT)) for path in (ROOT / "bridge").rglob("*")
          if path.is_file() and "__pycache__" not in path.parts and path.suffix in {".py", ".html", ".css", ".js"}]
OUTPUT.parent.mkdir(exist_ok=True)
with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as archive:
    for name in sorted(FILES):
        archive.write(ROOT / name, "codex-context-bridge/" + name.replace("\\", "/"))
print(OUTPUT)
