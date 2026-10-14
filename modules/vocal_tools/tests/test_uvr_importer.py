import json
import tempfile
import unittest
from pathlib import Path

from vocal_tools.catalog import ModelCatalog, load_uvr_model_entries


class UVRImporterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.models_dir = self.root / "models"
        self.models_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_mdx_onnx_normalizes_to_manifest_entry(self):
        sources = self._sources(
            mdx_models=[
                {
                    "name": "UVR-MDX-NET-Voc_FT.onnx",
                    "filename": "UVR-MDX-NET-Voc_FT.onnx",
                }
            ]
        )

        entries = load_uvr_model_entries(
            include_demucs=False,
            source_urls=sources,
        )

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "uvr_mdx_net_voc_ft")
        self.assertEqual(entry["name"], "UVR-MDX-NET-Voc FT")
        self.assertEqual(entry["arch"], "mdx_onnx")
        self.assertEqual(entry["filename"], "UVR-MDX-NET-Voc_FT.onnx")
        self.assertEqual(entry["metadata"]["runnable"], True)
        self.assertEqual(entry["metadata"]["unsupported_reason"], None)

    def test_download_url_uses_repo_base_and_filename(self):
        sources = self._sources(
            mdx_models=[
                {
                    "name": "UVR-MDX-NET Voc FT.onnx",
                    "filename": "UVR-MDX-NET Voc FT.onnx",
                }
            ],
            normal_repo="https://example.test/release",
        )

        entry = load_uvr_model_entries(
            include_demucs=False,
            source_urls=sources,
        )[0]

        self.assertEqual(
            entry["download_url"],
            "https://example.test/release/UVR-MDX-NET%20Voc%20FT.onnx",
        )

    def test_unsupported_models_are_excluded_by_default(self):
        sources = self._sources(
            mdx_models=[
                {"filename": "UVR-MDX-NET-Voc_FT.onnx"},
                {"filename": "Roformer-Big.ckpt"},
            ]
        )

        entries = load_uvr_model_entries(
            include_demucs=False,
            source_urls=sources,
        )

        self.assertEqual([entry["id"] for entry in entries], ["uvr_mdx_net_voc_ft"])

    def test_include_unsupported_keeps_reason_metadata(self):
        sources = self._sources(
            mdx_models=[
                {"filename": "UVR-MDX-NET-Voc_FT.onnx"},
                {"filename": "Roformer-Big.ckpt"},
            ]
        )

        entries = load_uvr_model_entries(
            include_unsupported=True,
            include_demucs=False,
            source_urls=sources,
        )
        by_id = {entry["id"]: entry for entry in entries}

        unsupported = by_id["uvr_roformer_big"]
        self.assertEqual(unsupported["arch"], "roformer")
        self.assertEqual(unsupported["metadata"]["runnable"], False)
        self.assertEqual(unsupported["metadata"]["unsupported_reason"], "engine_not_implemented")
        self.assertEqual(unsupported["metadata"]["detected_arch"], "roformer")

    def test_vr_source_requires_include_vr_and_unsupported_flag(self):
        sources = self._sources(
            mdx_models=[],
            vr_models=[{"filename": "UVR-VR-DeEcho.pth"}],
        )

        without_unsupported = load_uvr_model_entries(
            include_vr=True,
            include_demucs=False,
            source_urls=sources,
        )
        with_unsupported = load_uvr_model_entries(
            include_unsupported=True,
            include_vr=True,
            include_demucs=False,
            source_urls=sources,
        )

        self.assertEqual(without_unsupported, [])
        self.assertEqual(with_unsupported[0]["arch"], "vr_arch")
        self.assertEqual(with_unsupported[0]["metadata"]["unsupported_reason"], "engine_not_implemented")

    def test_demucs_normalizes_as_package_managed_runnable(self):
        sources = self._sources(
            mdx_models=[],
            demucs_models=["htdemucs"],
        )

        entry = load_uvr_model_entries(
            include_mdx=False,
            include_demucs=True,
            source_urls=sources,
        )[0]

        self.assertEqual(entry["id"], "uvr_htdemucs")
        self.assertEqual(entry["arch"], "demucs")
        self.assertIsNone(entry["filename"])
        self.assertEqual(entry["defaults"]["managed_by"], "demucs")
        self.assertEqual(entry["metadata"]["runnable"], True)

    def test_model_catalog_with_uvr_filters_cards_by_runnable_and_source(self):
        sources = self._sources(
            mdx_models=[
                {"filename": "UVR-MDX-NET-Voc_FT.onnx"},
                {"filename": "Roformer-Big.ckpt"},
            ]
        )

        catalog = ModelCatalog.with_uvr_remote(
            model_dir=self.models_dir,
            include_unsupported=True,
            include_demucs=False,
            source_urls=sources,
        )

        default_ids = {card["id"] for card in catalog.list_model_cards(source="uvr")}
        all_cards = catalog.list_model_cards(include_unsupported=True, source="uvr")
        all_ids = {card["id"] for card in all_cards}
        source_names = {source["source"] for source in catalog.list_sources()}

        self.assertIn("uvr", source_names)
        self.assertEqual(default_ids, {"uvr_mdx_net_voc_ft"})
        self.assertEqual(all_ids, {"uvr_mdx_net_voc_ft", "uvr_roformer_big"})

    def test_builtin_unsupported_cards_are_hidden_by_default(self):
        catalog = ModelCatalog(model_dir=self.models_dir)

        default_arches = {card["arch"] for card in catalog.list_model_cards()}
        all_arches = {card["arch"] for card in catalog.list_model_cards(include_unsupported=True)}

        self.assertNotIn("vr_arch", default_arches)
        self.assertNotIn("mdxc", default_arches)
        self.assertIn("vr_arch", all_arches)
        self.assertIn("mdxc", all_arches)

    def _sources(
        self,
        *,
        mdx_models,
        vr_models=None,
        demucs_models=None,
        normal_repo="https://example.test/models/",
    ):
        mdx_path = self._write_json("mdx.json", {"models": mdx_models})
        vr_path = self._write_json("vr.json", {"models": vr_models or []})
        demucs_path = self._write_json("demucs.json", {"models": demucs_models or []})
        checks_path = self._write_json("checks.json", {"models": []})
        return {
            "mdx_model_data": mdx_path.as_uri(),
            "vr_model_data": vr_path.as_uri(),
            "demucs_model_names": demucs_path.as_uri(),
            "download_checks": checks_path.as_uri(),
            "normal_repo": normal_repo,
        }

    def _write_json(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
