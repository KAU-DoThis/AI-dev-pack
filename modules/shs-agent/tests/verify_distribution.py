"""Verify built archives and run the offline suite against the wheel in isolation.

Run after `uv build`: python tests/verify_distribution.py
No package installation, external service or third-party test runner is needed.
"""
from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def run_isolated_suite(package: Path, test_root: Path, example_root: Path) -> None:
    script = (
        "import sys, unittest; "
        f"sys.path[:0] = [{str(package)!r}, {str(example_root)!r}, {str(test_root)!r}]; "
        "import shs_agent; "
        f"assert str(shs_agent.__file__).startswith({str(package)!r}), shs_agent.__file__; "
        "import examples.full_workflow_host; "
        f"assert str(examples.full_workflow_host.__file__).startswith({str(example_root)!r}); "
        "from shs_agent import Tracer, TracerSnapshot; "
        f"suite = unittest.defaultTestLoader.discover({str(test_root / 'tests')!r}); "
        "result = unittest.TextTestRunner(verbosity=1).run(suite); "
        "assert not any('.old' in name or '._deprecated' in name for name in sys.modules); "
        "assert not any(name in sys.modules for name in ['torch', 'ollama', 'openai', 'sqlalchemy']); "
        "sys.exit(not result.wasSuccessful())"
    )
    subprocess.run([sys.executable, "-I", "-S", "-c", script], check=True, timeout=180, cwd=test_root)


def run_isolated_examples(package: Path, example_root: Path, output: Path) -> None:
    for name, arguments in (
        ("full_workflow_host.py", ["--quiet", "--output-dir", str(output)]),
        ("replay_task_tree.py", [str(output / "task_tree.json")]),
    ):
        script = (
            "import sys, runpy; "
            f"sys.path[:0] = [{str(package)!r}, {str(example_root / 'examples')!r}]; "
            f"sys.argv = {[name, *arguments]!r}; "
            f"runpy.run_path({str(example_root / 'examples' / name)!r}, run_name='__main__')"
        )
        completed = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                                   capture_output=True, text=True, timeout=30, cwd=example_root)
        if completed.returncode:
            raise AssertionError(completed.stdout + completed.stderr)
        assert ("live_matches_archive" if name == "full_workflow_host.py" else "complete=True") in completed.stdout


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    wheels = list((root / "dist").glob("shs_agent-*.whl"))
    sources = list((root / "dist").glob("shs_agent-*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise RuntimeError("expected one wheel and one sdist in dist/; run uv build first")
    with (tempfile.TemporaryDirectory(prefix="shs-agent-sdist-") as temporary,
          zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sources[0]) as sdist):
        unpacked = Path(temporary)
        wheel_examples = unpacked / "wheel_examples"
        names = wheel.namelist() + sdist.getnames()
        metadata = wheel.read(next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))).decode()
        assert all("extra ==" in line for line in metadata.splitlines() if line.startswith("Requires-Dist:")), \
            "minimal install must not require optional SDKs or SQL storage"
        for archive in ("old", "_deprecated"):
            assert not any(f"/agent/{archive}/" in name or name.endswith(f"/agent/{archive}") for name in names)
        for obsolete in ("context.py", "runtime.py", "session.py", "tools_depricated.py"):
            assert not any(name.endswith("/agent/" + obsolete) for name in names), obsolete
        assert not any(name.endswith("/agent/context/memory.py") for name in names)
        for source in (root / "src" / "shs_agent").rglob("*.py"):
            if {"old", "_deprecated"}.intersection(source.relative_to(root / "src").parts):
                continue
            relative = source.relative_to(root / "src").as_posix()
            assert wheel.read(relative) == source.read_bytes(), f"stale wheel member: {relative}"
            sdist_source = next(name for name in sdist.getnames() if name.endswith("/src/" + relative))
            data = sdist.extractfile(sdist_source).read()
            assert data == source.read_bytes(), relative
            destination = unpacked / "src" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        source_names = sdist.getnames()
        for source in (root / "tests").rglob("*.py"):
            relative = source.relative_to(root).as_posix()
            member = next(name for name in source_names if name.endswith("/" + relative))
            data = sdist.extractfile(member).read()
            assert data == source.read_bytes(), f"stale sdist test: {relative}"
            destination = unpacked / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        # Verify and extract every shipped example/doc from both archives. Tests
        # must import these examples, never silently fall back to the checkout.
        data_prefix = next(name.split("share/shs-agent/")[0] for name in wheel.namelist()
                           if "share/shs-agent/" in name) + "share/shs-agent/"
        for member in wheel.namelist():
            if not member.startswith(data_prefix):
                continue
            relative = member[len(data_prefix):]
            source_relative = "README.md" if relative == "docs/README.md" else relative
            source = root / source_relative
            data = wheel.read(member)
            assert data == source.read_bytes(), f"stale wheel data: {relative}"
            source_member = next(name for name in source_names if name.endswith("/" + source_relative))
            assert sdist.extractfile(source_member).read() == data, relative
            for base in (wheel_examples, unpacked):
                destination = base / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        for relative in ("examples/full_workflow_host.py", "examples/workflow_support.py",
                         "examples/replay_task_tree.py", "examples/ollama_workflow_host.py",
                         "examples/workspace_coding_host.py", "examples/context_recovery_host.py", "docs/dev/HOST_USAGE.md",
                         "docs/dev/reports/phase_8_integration/phase_8_integration.md"):
            assert (wheel_examples / relative).is_file(), relative
        print("wheel/sdist: current source, tests, examples and docs verified; archived agents excluded", flush=True)
        print("isolated wheel suite", flush=True)
        run_isolated_suite(wheels[0], unpacked, wheel_examples)
        print("isolated sdist suite", flush=True)
        run_isolated_suite(unpacked / "src", unpacked, unpacked)
        run_isolated_examples(wheels[0], wheel_examples, unpacked / "wheel_output")
        run_isolated_examples(unpacked / "src", unpacked, unpacked / "sdist_output")
        print("isolated wheel/sdist Fake Host and replay CLI: passed", flush=True)


if __name__ == "__main__":
    main()
