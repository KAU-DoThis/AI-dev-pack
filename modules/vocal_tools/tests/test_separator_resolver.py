import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vocal_tools.catalog import ModelCatalog
from vocal_tools.core import ModelNotInstalledError, OptionSpec, SeparationResult
from vocal_tools.engines.base import SeparationEngine
from vocal_tools.separator import Separator


class StubSeparationEngine(SeparationEngine):
    arch = "mdx_onnx"

    def __init__(self):
        self.requests = []

    def get_option_schema(self, model):
        return [
            OptionSpec(name="denoise", type="bool", default=False),
            OptionSpec(name="m_threads", type="int", default=2, min=1),
        ]

    def separate(self, request, progress_callback=None):
        self.requests.append(request)
        return SeparationResult(
            input_audio=request.input_audio,
            output_dir=request.output_dir,
            model_id=request.model.id,
            model_name=request.model.name,
            arch=request.model.arch,
            stems={},
            options_used=dict(request.options),
            metadata={"model_path": str(request.model_path) if request.model_path else None},
        )


class SeparatorResolverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()
        self.input_audio = self.root / "song.wav"
        self.input_audio.write_bytes(b"audio")
        self.output_dir = self.root / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_model_id_call_still_uses_catalog_model(self):
        model_path = self.models_dir / "Kim_Vocal_2.onnx"
        model_path.write_bytes(b"model")
        catalog = self._catalog(
            [
                {
                    "id": "kim_vocal_2",
                    "name": "Kim Vocal 2",
                    "arch": "mdx_onnx",
                    "filename": "Kim_Vocal_2.onnx",
                    "defaults": {"denoise": True},
                }
            ]
        )
        engine = StubSeparationEngine()
        separator = Separator(catalog=catalog, engines={"mdx_onnx": engine})

        result = separator.separate(
            input_audio=self.input_audio,
            output_dir=self.output_dir,
            model_id="kim_vocal_2",
            options={"m_threads": 4},
        )

        request = engine.requests[0]
        self.assertEqual(result.model_id, "kim_vocal_2")
        self.assertEqual(request.model_path, model_path)
        self.assertEqual(request.options["denoise"], True)
        self.assertEqual(request.options["m_threads"], 4)

    def test_model_argument_catalog_id_takes_resolver_path(self):
        model_path = self.models_dir / "Kim_Vocal_2.onnx"
        model_path.write_bytes(b"model")
        catalog = self._catalog(
            [
                {
                    "id": "kim_vocal_2",
                    "name": "Kim Vocal 2",
                    "arch": "mdx_onnx",
                    "filename": "Kim_Vocal_2.onnx",
                }
            ]
        )
        engine = StubSeparationEngine()
        separator = Separator(catalog=catalog, engines={"mdx_onnx": engine})

        separator.separate(
            input_audio=self.input_audio,
            output_dir=self.output_dir,
            model="kim_vocal_2",
            model_id="different_model",
        )

        request = engine.requests[0]
        self.assertEqual(request.model.id, "kim_vocal_2")
        self.assertEqual(request.model_path, model_path)

    def test_model_argument_direct_onnx_file_uses_minimal_model_spec(self):
        direct_path = self.root / "custom.onnx"
        direct_path.write_bytes(b"custom")
        catalog = self._catalog([])
        engine = StubSeparationEngine()
        separator = Separator(catalog=catalog, engines={"mdx_onnx": engine})

        result = separator.separate(
            input_audio=self.input_audio,
            output_dir=self.output_dir,
            model=direct_path,
        )

        request = engine.requests[0]
        self.assertEqual(request.model_path, direct_path)
        self.assertEqual(request.model.arch, "mdx_onnx")
        self.assertEqual(request.model.defaults, {})
        self.assertEqual(result.model_id, "direct_custom")

    def test_auto_download_downloads_before_engine_request(self):
        source_path = self.root / "source.onnx"
        source_path.write_bytes(b"downloaded")
        catalog = self._catalog(
            [
                {
                    "id": "downloadable",
                    "name": "Downloadable",
                    "arch": "mdx_onnx",
                    "filename": "downloaded.onnx",
                    "download_url": source_path.as_uri(),
                }
            ]
        )
        engine = StubSeparationEngine()
        separator = Separator(catalog=catalog, engines={"mdx_onnx": engine})

        separator.separate(
            input_audio=self.input_audio,
            output_dir=self.output_dir,
            model="downloadable",
            models_dir=self.models_dir,
            auto_download=True,
        )

        downloaded_path = self.models_dir / "downloaded.onnx"
        request = engine.requests[0]
        self.assertEqual(request.model_path, downloaded_path)
        self.assertEqual(downloaded_path.read_bytes(), b"downloaded")

    def test_auto_download_false_raises_when_catalog_file_missing(self):
        source_path = self.root / "source.onnx"
        source_path.write_bytes(b"downloaded")
        catalog = self._catalog(
            [
                {
                    "id": "downloadable",
                    "name": "Downloadable",
                    "arch": "mdx_onnx",
                    "filename": "downloaded.onnx",
                    "download_url": source_path.as_uri(),
                }
            ]
        )
        engine = StubSeparationEngine()
        separator = Separator(catalog=catalog, engines={"mdx_onnx": engine})

        with self.assertRaises(ModelNotInstalledError):
            separator.separate(
                input_audio=self.input_audio,
                output_dir=self.output_dir,
                model="downloadable",
                models_dir=self.models_dir,
                auto_download=False,
            )

        self.assertEqual(engine.requests, [])

    def test_cli_separate_help_lists_model_flags(self):
        completed = subprocess.run(
            [sys.executable, "-m", "vocal_tools.cli", "separate", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("--model ", completed.stdout)
        self.assertIn("--model-id", completed.stdout)
        self.assertIn("--no-auto-download", completed.stdout)

    def test_cli_no_auto_download_flag_parses_false(self):
        from vocal_tools.cli import build_parser

        args = build_parser().parse_args(
            [
                "separate",
                "--input",
                str(self.input_audio),
                "--output-dir",
                str(self.output_dir),
                "--model",
                "missing_model",
                "--no-auto-download",
            ]
        )

        self.assertFalse(args.auto_download)
        self.assertEqual(args.model, "missing_model")
        self.assertEqual(args.model_id, "kim_vocal_2")

    def _catalog(self, models):
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return ModelCatalog(
            model_dir=self.models_dir,
            builtin_manifest_path=manifest_path,
        )


if __name__ == "__main__":
    unittest.main()
