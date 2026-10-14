import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vocal_tools.catalog import ModelCatalog, resolve_model
from vocal_tools.core import (
    ModelNotInstalledError,
    ModelResolveError,
    ModelVerificationError,
)


class RVCVoiceManifestResolverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_manifest_id_resolves_local_pth_and_index(self):
        model_path = self._write_model_file("voices/test_voice.pth", b"pth")
        index_path = self._write_model_file("voices/test_voice.index", b"index")
        catalog = self._catalog(
            [self._entry(filename="voices/test_voice.pth", index_filename="voices/test_voice.index")]
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            task="voice_conversion",
            catalog=catalog,
            models_dir=self.models_dir,
        )

        self.assertEqual(resolved.source, "catalog")
        self.assertEqual(resolved.files["model"], model_path)
        self.assertEqual(resolved.files["index"], index_path)

    def test_manifest_index_is_optional_when_not_required(self):
        model_path = self._write_model_file("voices/test_voice.pth", b"pth")
        catalog = self._catalog(
            [self._entry(filename="voices/test_voice.pth", index_filename="voices/test_voice.index")]
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            task="voice_conversion",
            catalog=catalog,
            models_dir=self.models_dir,
        )

        self.assertEqual(resolved.files["model"], model_path)
        self.assertNotIn("index", resolved.files)

    def test_missing_pth_without_auto_download_raises_not_installed(self):
        source_path = self._source_file("source.pth", b"pth")
        catalog = self._catalog([self._entry(download_url=str(source_path))])

        with self.assertRaises(ModelNotInstalledError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                task="voice_conversion",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=False,
            )

    def test_auto_download_from_local_path_downloads_pth(self):
        payload = b"local path pth"
        source_path = self._source_file("local.pth", payload)
        catalog = self._catalog([self._entry(index_filename=None, download_url=str(source_path))])

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            task="voice_conversion",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual(resolved.files["model"].read_bytes(), payload)
        self.assertEqual(resolved.files["model"], self.models_dir / "test_voice" / "test_voice.pth")

    def test_auto_download_from_file_url_downloads_pth(self):
        payload = b"file url pth"
        source_path = self._source_file("file-url.pth", payload)
        catalog = self._catalog([self._entry(index_filename=None, download_url=source_path.as_uri())])

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            task="voice_conversion",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual(resolved.files["model"].read_bytes(), payload)

    def test_index_download_url_downloads_index_too(self):
        pth_payload = b"pth bytes"
        index_payload = b"index bytes"
        source_pth = self._source_file("voice.pth", pth_payload)
        source_index = self._source_file("voice.index", index_payload)
        catalog = self._catalog(
            [
                self._entry(
                    download_url=str(source_pth),
                    index_download_url=str(source_index),
                )
            ]
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            task="voice_conversion",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.source, "downloaded")
        self.assertEqual(resolved.files["model"].read_bytes(), pth_payload)
        self.assertEqual(resolved.files["index"].read_bytes(), index_payload)

    def test_pth_sha256_success(self):
        payload = b"verified pth"
        source_path = self._source_file("verified.pth", payload)
        catalog = self._catalog(
            [
                self._entry(
                    index_filename=None,
                    download_url=str(source_path),
                    sha256=self._sha256(payload),
                )
            ]
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            catalog=catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )

        self.assertEqual(resolved.files["model"].read_bytes(), payload)

    def test_pth_sha256_failure(self):
        source_path = self._source_file("bad-sha.pth", b"bad pth")
        catalog = self._catalog(
            [self._entry(index_filename=None, download_url=str(source_path), sha256="0" * 64)]
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_index_sha256_success_and_failure(self):
        pth_payload = b"pth"
        index_payload = b"verified index"
        source_pth = self._source_file("index-sha.pth", pth_payload)
        source_index = self._source_file("index-sha.index", index_payload)
        success_catalog = self._catalog(
            [
                self._entry(
                    download_url=str(source_pth),
                    index_download_url=str(source_index),
                    index_sha256=self._sha256(index_payload),
                )
            ],
            manifest_name="success_manifest.json",
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            catalog=success_catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )
        self.assertEqual(resolved.files["index"].read_bytes(), index_payload)

        self._reset_models_dir()
        failure_catalog = self._catalog(
            [
                self._entry(
                    download_url=str(source_pth),
                    index_download_url=str(source_index),
                    index_sha256="0" * 64,
                )
            ],
            manifest_name="failure_manifest.json",
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=failure_catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_pth_file_size_success_and_failure(self):
        payload = b"size checked pth"
        source_path = self._source_file("size.pth", payload)
        success_catalog = self._catalog(
            [
                self._entry(
                    index_filename=None,
                    download_url=str(source_path),
                    file_size=len(payload),
                )
            ],
            manifest_name="size_success.json",
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            catalog=success_catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )
        self.assertEqual(resolved.files["model"].read_bytes(), payload)

        self._reset_models_dir()
        failure_catalog = self._catalog(
            [self._entry(index_filename=None, download_url=str(source_path), file_size=len(payload) + 1)],
            manifest_name="size_failure.json",
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=failure_catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_index_file_size_success_and_failure(self):
        pth_payload = b"pth"
        index_payload = b"size checked index"
        source_pth = self._source_file("index-size.pth", pth_payload)
        source_index = self._source_file("index-size.index", index_payload)
        success_catalog = self._catalog(
            [
                self._entry(
                    download_url=str(source_pth),
                    index_download_url=str(source_index),
                    index_file_size=len(index_payload),
                )
            ],
            manifest_name="index_size_success.json",
        )

        resolved = resolve_model(
            "test_voice",
            arch="rvc_voice",
            catalog=success_catalog,
            models_dir=self.models_dir,
            auto_download=True,
        )
        self.assertEqual(resolved.files["index"].read_bytes(), index_payload)

        self._reset_models_dir()
        failure_catalog = self._catalog(
            [
                self._entry(
                    download_url=str(source_pth),
                    index_download_url=str(source_index),
                    index_file_size=len(index_payload) + 1,
                )
            ],
            manifest_name="index_size_failure.json",
        )

        with self.assertRaises(ModelVerificationError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=failure_catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_unsafe_filename_is_rejected(self):
        source_path = self._source_file("unsafe.pth", b"pth")
        catalog = self._catalog(
            [self._entry(filename="../unsafe.pth", index_filename=None, download_url=str(source_path))]
        )

        with self.assertRaises(ModelResolveError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_absolute_filename_is_rejected(self):
        source_path = self._source_file("absolute.pth", b"pth")
        catalog = self._catalog(
            [
                self._entry(
                    filename=str(self.root / "absolute.pth"),
                    index_filename=None,
                    download_url=str(source_path),
                )
            ]
        )

        with self.assertRaises(ModelResolveError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def test_unsafe_index_filename_is_rejected(self):
        self._write_model_file("voices/test_voice.pth", b"pth")
        catalog = self._catalog([self._entry(filename="voices/test_voice.pth", index_filename="../unsafe.index")])

        with self.assertRaises(ModelResolveError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
            )

    def test_runnable_false_blocks_download(self):
        source_path = self._source_file("blocked.pth", b"pth")
        catalog = self._catalog(
            [
                self._entry(
                    index_filename=None,
                    download_url=str(source_path),
                    metadata={"source": "test", "runnable": False, "unsupported_reason": "engine_not_implemented"},
                )
            ]
        )

        with self.assertRaises(ModelResolveError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

        self.assertFalse((self.models_dir / "test_voice" / "test_voice.pth").exists())

    def test_index_required_without_download_url_raises_when_missing(self):
        self._write_model_file("voices/test_voice.pth", b"pth")
        catalog = self._catalog(
            [
                self._entry(
                    filename="voices/test_voice.pth",
                    index_filename="voices/test_voice.index",
                    metadata={"source": "test", "runnable": True, "index_required": True},
                )
            ]
        )

        with self.assertRaises(ModelNotInstalledError):
            resolve_model(
                "test_voice",
                arch="rvc_voice",
                catalog=catalog,
                models_dir=self.models_dir,
                auto_download=True,
            )

    def _catalog(self, models, *, manifest_name="manifest.json"):
        manifest_path = self.root / manifest_name
        manifest_path.write_text(json.dumps({"models": models}), encoding="utf-8")
        return ModelCatalog(model_dir=self.models_dir, builtin_manifest_path=manifest_path)

    def _entry(self, **overrides):
        entry = {
            "id": "test_voice",
            "name": "Test Voice",
            "arch": "rvc_voice",
            "filename": "test_voice/test_voice.pth",
            "index_filename": "test_voice/test_voice.index",
            "tasks": ["voice_conversion"],
            "tags": ["rvc"],
            "description": "Test RVC voice.",
            "download_url": None,
            "index_download_url": None,
            "sha256": None,
            "index_sha256": None,
            "file_size": None,
            "index_file_size": None,
            "defaults": {},
            "options": [],
            "ui": {"category": "RVC voice"},
            "metadata": {"source": "test", "runnable": True},
        }
        entry.update(overrides)
        return entry

    def _write_model_file(self, relative_path, payload):
        path = self.models_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _source_file(self, name, payload):
        source_dir = self.root / "sources"
        source_dir.mkdir(exist_ok=True)
        path = source_dir / name
        path.write_bytes(payload)
        return path

    def _reset_models_dir(self):
        for path in sorted(self.models_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()

    def _sha256(self, payload):
        return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    unittest.main()
