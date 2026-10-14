import subprocess
import sys
import unittest


class ImportSmokeTests(unittest.TestCase):
    def test_import_vocal_tools(self):
        import vocal_tools

        self.assertIsNotNone(vocal_tools)

    def test_public_api_lazy_imports(self):
        from vocal_tools import (
            ModelCatalog,
            RVCConversionOptions,
            RVCVoiceCatalog,
            run_cover_pipeline,
        )

        self.assertEqual(ModelCatalog.__name__, "ModelCatalog")
        self.assertEqual(RVCVoiceCatalog.__name__, "RVCVoiceCatalog")
        self.assertEqual(RVCConversionOptions.__name__, "RVCConversionOptions")
        self.assertTrue(callable(run_cover_pipeline))

    def test_top_level_import_does_not_load_heavy_runtime_modules(self):
        code = (
            "import sys; "
            "before=set(sys.modules); "
            "import vocal_tools; "
            "added=set(sys.modules)-before; "
            "heavy={'torch','librosa','onnxruntime','fairseq','numpy','scipy','soundfile'}; "
            "bad=sorted(heavy & added); "
            "print(bad); "
            "raise SystemExit(1 if bad else 0)"
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
