import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vocal_tools.catalog import ModelCatalog, ResolvedModel, load_uvr_model_entries, resolve_model
from vocal_tools.core import (
    ModelNotFoundError,
    ModelNotInstalledError,
    ModelPathNotFoundError,
    ModelResolveError,
    ModelVerificationError,
)


class ModelResolverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_direct_onnx_file_resolves(self):
        model_path = self.models_dir / "model.onnx"
        model_path.touch()

        resolved = resolve_model(model_path, arch="mdx_onnx")

        self.assertEqual(resolved.source, "file")
        self.assertEqual(resolved.arch, "mdx_onnx")
        self.assertEqual(resolved.files, {"model": model_path})

    def test_missing_path_like_string_does_not_fallback_to_catalog(self):
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

        missing_path = self.root / "kim_vocal_2"

        with self.assertRaises(ModelPathNotFoundError):
            resolve_model(
                str(missing_path),
                catalog=catalog,
                model_dirs=[self.models_dir],
            )

    def test_filename_searches_model_dirs(self):
        model_path = self.models_dir / "model.onnx"
        model_path.touch()

        resolved = resolve_model(
            "model.onnx",
            arch="mdx_onnx",
            model_dirs=[self.models_dir],
        )

        self.assertEqual(resolved.source, "local")
        self.assertEqual(resolved.files["model"], model_path)

    def test_mdx_catalog_id_resolves_installed_filename(self):
        model_path = self.models_dir / "Kim_Vocal_2.onnx"
        model_path.touch()
        catalog = self._catalog(
            [
                {
                    "id": "kim_vocal_2",
                    "name": "Kim Vocal 2",
                    "arch": "mdx_onnx",
                    "filename": "Kim_Vocal_2.onnx",
                    "tasks": ["vocal_separation"],
                }
            ]
        )

        resolved = resolve_model(
            "kim_vocal_2",
            catalog=catalog,
            model_dirs=[self.models_dir],
        )

        self.assertEqual(resolved.id, "kim_vocal_2")
        self.assertEqual(resolved.source, "catalog")
        self.assertEqual(resolved.task, "vocal_separation")
        self.assertEqual(resolved.files["model"], model_path)

    def test_managed_model_without_filename_resolves_as_external(self):
        catalog = self._catalog(
            [
                {
                    "id": "htdemucs",
                    "name": "HTDemucs",
                    "arch": "demucs",
                    "filename": None,
                    "defaults": {"managed_by": "demucs"},
                }
            ]
        )

        resolved = resolve_model(
            "htdemucs",
            catalog=catalog,
            model_dirs=[self.models_dir],
        )

        self.assertEqual(resolved.id, "htdemucs")
        self.assertEqual(resolved.source, "external")
        self.assertEqual(resolved.files, {})
        self.assertEqual(resolved.metadata["managed_by"], "demucs")

    def test_catalog_model_missing_installed_file_raises_not_installed(self):
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

        with self.assertRaises(ModelNotInstalledError):
            resolve_model(
                "kim_vocal_2",
                catalog=catalog,
                model_dirs=[self.models_dir],
                auto_download=True,
            )

    def test_catalog_model_missing_with_auto_download_false_raises_not_installed(self):
        source_path = self._source_file("source.onnx", b"model-bytes")
        catalog = self._catalog([self._downloadable_entry(download_url=str(source_path))])

        with self.assertRaises(ModelNotInstalledError):
            resolve_model(
                "downloadable",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=False,
            )

        self.assertFalse((self.models_dir / "downloadable.onnx").exists())

    def test_auto_download_copies_local_path_and_resolves_downloaded(self):
        payload = b"local model bytes"
        source_path = self._source_file("source.onnx", payload)
        catalog = self._catalog([self._downloadable_entry(download_url=str(source_path))])

        resolved = resolve_model(
            "downloadable",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        downloaded_path = self.models_dir / "downloadable.onnx"
        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual(resolved.files["model"], downloaded_path)
        self.assertEqual(downloaded_path.read_bytes(), payload)

    def test_auto_download_accepts_file_url(self):
        payload = b"file url model bytes"
        source_path = self._source_file("source-url.onnx", payload)
        catalog = self._catalog([self._downloadable_entry(download_url=source_path.as_uri())])

        resolved = resolve_model(
            "downloadable",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual((self.models_dir / "downloadable.onnx").read_bytes(), payload)

    def test_auto_download_succeeds_with_matching_sha256(self):
        payload = b"hash checked bytes"
        source_path = self._source_file("source-hash.onnx", payload)
        catalog = self._catalog(
            [
                self._downloadable_entry(
                    download_url=str(source_path),
                    sha256=self._sha256(payload),
                )
            ]
        )

        resolved = resolve_model(
            "downloadable",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")

    def test_auto_download_fails_with_mismatched_sha256(self):
        source_path = self._source_file("source-bad-hash.onnx", b"bad hash bytes")
        catalog = self._catalog(
            [
                self._downloadable_entry(
                    download_url=str(source_path),
                    sha256="0" * 64,
                )
            ]
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "downloadable",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_auto_download_succeeds_with_matching_file_size(self):
        payload = b"size checked bytes"
        source_path = self._source_file("source-size.onnx", payload)
        catalog = self._catalog(
            [
                self._downloadable_entry(
                    download_url=str(source_path),
                    file_size=len(payload),
                )
            ]
        )

        resolved = resolve_model(
            "downloadable",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")

    def test_auto_download_fails_with_mismatched_file_size(self):
        source_path = self._source_file("source-bad-size.onnx", b"wrong size bytes")
        catalog = self._catalog(
            [
                self._downloadable_entry(
                    download_url=str(source_path),
                    file_size=999,
                )
            ]
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "downloadable",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_download_dir_takes_priority_over_models_dir(self):
        payload = b"download dir bytes"
        source_path = self._source_file("source-download-dir.onnx", payload)
        download_dir = self.root / "downloads"
        catalog = self._catalog([self._downloadable_entry(download_url=str(source_path))])

        resolved = resolve_model(
            "downloadable",
            catalog=catalog,
            models_dir=self.models_dir,
            download_dir=download_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual(resolved.files["model"], download_dir / "downloadable.onnx")
        self.assertFalse((self.models_dir / "downloadable.onnx").exists())
        self.assertEqual((download_dir / "downloadable.onnx").read_bytes(), payload)

    def test_auto_download_without_download_url_raises_not_installed(self):
        catalog = self._catalog([self._downloadable_entry(download_url=None)])

        with self.assertRaises(ModelNotInstalledError):
            resolve_model(
                "downloadable",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_auto_download_rejects_unsupported_catalog_model(self):
        source_path = self._source_file("roformer.ckpt", b"unsupported bytes")
        catalog = self._catalog(
            [
                {
                    "id": "uvr_roformer_big",
                    "name": "Roformer Big",
                    "arch": "roformer",
                    "filename": "Roformer-Big.ckpt",
                    "download_url": str(source_path),
                    "metadata": {
                        "runnable": False,
                        "unsupported_reason": "engine_not_implemented",
                    },
                }
            ]
        )

        with self.assertRaises(ModelResolveError):
            resolve_model(
                "uvr_roformer_big",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

        self.assertFalse((self.models_dir / "Roformer-Big.ckpt").exists())

    def test_auto_download_keeps_external_model_external(self):
        catalog = self._catalog(
            [
                {
                    "id": "htdemucs",
                    "name": "HTDemucs",
                    "arch": "demucs",
                    "filename": None,
                    "defaults": {"managed_by": "demucs"},
                }
            ]
        )

        resolved = resolve_model(
            "htdemucs",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "external")
        self.assertEqual(resolved.files, {})

    def test_uvr_imported_entry_can_auto_download_from_local_file_url(self):
        release_dir = self.root / "release"
        release_dir.mkdir()
        payload = b"uvr bytes"
        (release_dir / "UVR-Local.onnx").write_bytes(payload)
        source_urls = self._uvr_sources(
            mdx_models=[{"filename": "UVR-Local.onnx"}],
            normal_repo=f"{release_dir.as_uri()}/",
        )
        entries = load_uvr_model_entries(
            include_demucs=False,
            source_urls=source_urls,
        )
        catalog = self._catalog(entries)

        resolved = resolve_model(
            "uvr_local",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual((self.models_dir / "UVR-Local.onnx").read_bytes(), payload)

    def test_direct_rvc_pth_file_resolves_with_index(self):
        model_path = self.models_dir / "voice.pth"
        index_path = self.models_dir / "voice.index"
        model_path.touch()
        index_path.touch()

        resolved = resolve_model(model_path, arch="rvc_voice")

        self.assertEqual(resolved.source, "file")
        self.assertEqual(resolved.files["model"], model_path)
        self.assertEqual(resolved.files["index"], index_path)

    def test_rvc_directory_resolves_first_pth_and_index(self):
        voice_dir = self.models_dir / "voice"
        voice_dir.mkdir()
        first_model = voice_dir / "a.pth"
        second_model = voice_dir / "b.pth"
        first_index = voice_dir / "a.index"
        second_index = voice_dir / "b.index"
        second_model.touch()
        first_model.touch()
        second_index.touch()
        first_index.touch()

        resolved = resolve_model(voice_dir, arch="rvc_voice")

        self.assertEqual(resolved.source, "directory")
        self.assertEqual(resolved.files["model"], first_model)
        self.assertEqual(resolved.files["index"], first_index)

    def test_rvc_voice_id_searches_model_dirs_for_directory(self):
        voice_dir = self.models_dir / "rvc" / "my_voice"
        voice_dir.mkdir(parents=True)
        model_path = voice_dir / "voice.pth"
        index_path = voice_dir / "voice.index"
        model_path.touch()
        index_path.touch()

        resolved = resolve_model(
            "my_voice",
            arch="rvc_voice",
            model_dirs=[self.models_dir],
        )

        self.assertEqual(resolved.source, "local")
        self.assertEqual(resolved.files["model"], model_path)
        self.assertEqual(resolved.files["index"], index_path)

    def test_unknown_catalog_id_raises_catalog_not_found(self):
        catalog = self._catalog([])

        with self.assertRaises(ModelNotFoundError):
            resolve_model(
                "missing_model",
                catalog=catalog,
                model_dirs=[self.models_dir],
            )

    def test_resolver_imports_remain_lightweight(self):
        self.assertEqual(ResolvedModel.__name__, "ResolvedModel")
        self.assertTrue(callable(resolve_model))

    def _catalog(self, models):
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return ModelCatalog(
            model_dir=self.models_dir,
            builtin_manifest_path=manifest_path,
        )

    def _downloadable_entry(
        self,
        *,
        download_url,
        sha256=None,
        file_size=None,
    ):
        return {
            "id": "downloadable",
            "name": "Downloadable",
            "arch": "mdx_onnx",
            "filename": "downloadable.onnx",
            "download_url": download_url,
            "sha256": sha256,
            "file_size": file_size,
            "tasks": ["vocal_separation"],
            "metadata": {"runnable": True},
        }

    def _source_file(self, name, payload):
        source_dir = self.root / "sources"
        source_dir.mkdir(exist_ok=True)
        path = source_dir / name
        path.write_bytes(payload)
        return path

    def _sha256(self, payload):
        return hashlib.sha256(payload).hexdigest()

    def _uvr_sources(self, *, mdx_models, normal_repo):
        mdx_path = self.root / "uvr_mdx.json"
        vr_path = self.root / "uvr_vr.json"
        demucs_path = self.root / "uvr_demucs.json"
        checks_path = self.root / "uvr_checks.json"
        mdx_path.write_text(json.dumps({"models": mdx_models}), encoding="utf-8")
        vr_path.write_text(json.dumps({"models": []}), encoding="utf-8")
        demucs_path.write_text(json.dumps({"models": []}), encoding="utf-8")
        checks_path.write_text(json.dumps({"models": []}), encoding="utf-8")
        return {
            "mdx_model_data": mdx_path.as_uri(),
            "vr_model_data": vr_path.as_uri(),
            "demucs_model_names": demucs_path.as_uri(),
            "download_checks": checks_path.as_uri(),
            "normal_repo": normal_repo,
        }


if __name__ == "__main__":
    unittest.main()
