import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from vocal_tools.catalog import ModelCatalog
from vocal_tools.rvc import RVCConversionOptions, RVCConverter, RVCLoadedModel


class TestableRVCConverter(RVCConverter):
    def __init__(self, models_dir, catalog=None):
        self.models_dir = Path(models_dir)
        self.catalog = catalog
        self._hubert_path_explicit = False
        self._rmvpe_path_explicit = False
        self.hubert_path = self.models_dir / "hubert_base.pt"
        self.rmvpe_path = self.models_dir / "rmvpe.pt"
        self.config = type("ConfigStub", (), {"rmvpe_path": str(self.rmvpe_path)})()
        self.device = "cpu"
        self.is_half = False
        self._hubert_model = object()
        self._model_cache = {}
        self._active_model = None
        self.calls = []

    def _load_voice_model_paths(self, paths):
        loaded = RVCLoadedModel(
            paths=paths,
            cpt={},
            version="v1",
            net_g=object(),
            tgt_sr=44100,
            vc=object(),
        )
        self._active_model = loaded
        return loaded

    def _run_infer(self, loaded_model, options, *, input_audio, output_audio, index_path):
        output_audio = Path(output_audio)
        output_audio.parent.mkdir(parents=True, exist_ok=True)
        output_audio.write_bytes(b"converted")
        self.calls.append(
            {
                "paths": loaded_model.paths,
                "options": options,
                "input_audio": Path(input_audio),
                "output_audio": output_audio,
                "index_path": index_path,
                "hubert_path": self.hubert_path,
                "rmvpe_path": self.rmvpe_path,
            }
        )
        return output_audio


class RVCResolverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()
        self.input_audio = self.root / "vocal.wav"
        self.input_audio.write_bytes(b"audio")
        self.output_audio = self.root / "out.wav"

    def tearDown(self):
        self._tmp.cleanup()

    def test_existing_voice_model_call_uses_existing_lookup(self):
        model_path, index_path = self._voice_dir("my_voice")
        converter = TestableRVCConverter(self.models_dir)

        result = converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            voice_model="my_voice",
        )

        call = converter.calls[0]
        self.assertEqual(result, self.output_audio)
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["paths"].index_path, index_path)

    def test_model_argument_accepts_direct_pth_file(self):
        model_path, index_path = self._voice_dir("direct")
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model=model_path,
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["paths"].index_path, index_path)

    def test_model_argument_accepts_voice_directory(self):
        model_path, index_path = self._voice_dir("directory_voice")
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model=model_path.parent,
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["paths"].index_path, index_path)

    def test_model_argument_accepts_local_voice_id(self):
        model_path, index_path = self._voice_dir("local_voice")
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model="local_voice",
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["paths"].index_path, index_path)

    def test_model_dirs_are_used_for_voice_search(self):
        shared_dir = self.root / "shared"
        model_path, index_path = self._voice_dir("shared_voice", root=shared_dir / "rvc")
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model="shared_voice",
            model_dirs=[shared_dir],
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["paths"].index_path, index_path)

    def test_pth_neighbor_index_is_auto_selected(self):
        voice_dir = self.models_dir / "sorted_voice"
        voice_dir.mkdir()
        model_path = voice_dir / "voice.pth"
        first_index = voice_dir / "a.index"
        second_index = voice_dir / "b.index"
        model_path.touch()
        second_index.touch()
        first_index.touch()
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model=model_path,
        )

        self.assertEqual(converter.calls[0]["paths"].index_path, first_index)

    def test_explicit_index_overrides_auto_selected_index(self):
        model_path, auto_index = self._voice_dir("indexed")
        explicit_index = self.root / "explicit.index"
        explicit_index.touch()
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model=model_path,
            index=explicit_index,
        )

        call = converter.calls[0]
        self.assertNotEqual(auto_index, explicit_index)
        self.assertEqual(call["paths"].index_path, explicit_index)
        self.assertEqual(call["index_path"], str(explicit_index))

    def test_explicit_index_overrides_manifest_index(self):
        model_path, manifest_index = self._voice_dir("manifest_voice")
        explicit_index = self.root / "explicit_manifest.index"
        explicit_index.touch()
        catalog = self._catalog(
            [
                self._voice_entry(
                    model_id="manifest_voice",
                    filename="manifest_voice/manifest_voice.pth",
                    index_filename="manifest_voice/manifest_voice.index",
                )
            ]
        )
        converter = TestableRVCConverter(self.models_dir, catalog=catalog)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model="manifest_voice",
            index=explicit_index,
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertNotEqual(manifest_index, explicit_index)
        self.assertEqual(call["paths"].index_path, explicit_index)

    def test_missing_path_like_index_raises(self):
        model_path, _ = self._voice_dir("missing_index")
        converter = TestableRVCConverter(self.models_dir)

        with self.assertRaises(FileNotFoundError):
            converter.convert(
                input_audio=self.input_audio,
                output_audio=self.output_audio,
                model=model_path,
                index=self.root / "missing.index",
            )

    def test_model_argument_takes_priority_over_voice_model(self):
        old_model, _ = self._voice_dir("old_voice")
        new_model, _ = self._voice_dir("new_voice")
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            voice_model=old_model,
            model=new_model,
        )

        self.assertEqual(converter.calls[0]["paths"].model_path, new_model)

    def test_models_dir_override_updates_runtime_default_paths(self):
        other_models_dir = self.root / "other_models"
        model_path, _ = self._voice_dir("runtime_voice", root=other_models_dir)
        converter = TestableRVCConverter(self.models_dir)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model="runtime_voice",
            models_dir=other_models_dir,
        )

        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, model_path)
        self.assertEqual(call["hubert_path"], other_models_dir / "hubert_base.pt")
        self.assertEqual(call["rmvpe_path"], other_models_dir / "rmvpe.pt")

    def test_options_object_and_keyword_overrides_merge(self):
        model_path, _ = self._voice_dir("options_voice")
        converter = TestableRVCConverter(self.models_dir)
        options = RVCConversionOptions(
            pitch_change=1,
            f0_method="pm",
            index_rate=0.25,
            protect=0.5,
        )

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model=model_path,
            options=options,
            pitch_change=3,
            f0_method="rmvpe",
            index_rate=0.75,
            hop_length=256,
        )

        used = converter.calls[0]["options"]
        self.assertEqual(used.pitch_change, 3)
        self.assertEqual(used.f0_method, "rmvpe")
        self.assertEqual(used.index_rate, 0.75)
        self.assertEqual(used.protect, 0.5)
        self.assertEqual(used.crepe_hop_length, 256)

    def test_model_id_auto_download_uses_catalog_resolver_result(self):
        source_dir = self.root / "sources"
        source_dir.mkdir()
        source_model = source_dir / "downloaded_voice.pth"
        source_index = source_dir / "downloaded_voice.index"
        source_model.write_bytes(b"downloaded pth")
        source_index.write_bytes(b"downloaded index")
        catalog = self._catalog(
            [
                self._voice_entry(
                    model_id="downloaded_voice",
                    filename="downloaded_voice/downloaded_voice.pth",
                    index_filename="downloaded_voice/downloaded_voice.index",
                    download_url=str(source_model),
                    index_download_url=str(source_index),
                )
            ]
        )
        converter = TestableRVCConverter(self.models_dir, catalog=catalog)

        converter.convert(
            input_audio=self.input_audio,
            output_audio=self.output_audio,
            model="downloaded_voice",
            models_dir=self.models_dir,
            auto_download=True,
        )

        expected_model = self.models_dir / "downloaded_voice" / "downloaded_voice.pth"
        expected_index = self.models_dir / "downloaded_voice" / "downloaded_voice.index"
        call = converter.calls[0]
        self.assertEqual(call["paths"].model_path, expected_model)
        self.assertEqual(call["paths"].index_path, expected_index)
        self.assertEqual(expected_model.read_bytes(), b"downloaded pth")
        self.assertEqual(expected_index.read_bytes(), b"downloaded index")

    def test_cli_rvc_convert_help_lists_model_flags(self):
        completed = subprocess.run(
            [sys.executable, "-m", "vocal_tools.cli", "rvc-convert", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("--model ", completed.stdout)
        self.assertIn("--voice-model", completed.stdout)
        self.assertIn("--index", completed.stdout)

    def _voice_dir(self, name, *, root=None):
        root = Path(root) if root is not None else self.models_dir
        voice_dir = root / name
        voice_dir.mkdir(parents=True)
        model_path = voice_dir / f"{name}.pth"
        index_path = voice_dir / f"{name}.index"
        model_path.touch()
        index_path.touch()
        return model_path, index_path

    def _catalog(self, models):
        manifest_path = self.root / "rvc_manifest.json"
        manifest_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return ModelCatalog(model_dir=self.models_dir, builtin_manifest_path=manifest_path)

    def _voice_entry(
        self,
        *,
        model_id,
        filename,
        index_filename=None,
        download_url=None,
        index_download_url=None,
    ):
        return {
            "id": model_id,
            "name": model_id,
            "arch": "rvc_voice",
            "filename": filename,
            "index_filename": index_filename,
            "tasks": ["voice_conversion"],
            "tags": ["rvc"],
            "download_url": download_url,
            "index_download_url": index_download_url,
            "metadata": {"source": "test", "runnable": True},
        }


if __name__ == "__main__":
    unittest.main()
