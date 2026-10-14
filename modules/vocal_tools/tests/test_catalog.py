import unittest

from vocal_tools import ModelCatalog


class CatalogSmokeTests(unittest.TestCase):
    def test_builtin_catalog_loads_expected_models(self):
        catalog = ModelCatalog(model_dir="test_models")
        cards = catalog.list_model_cards()
        ids = {card["id"] for card in cards}

        self.assertGreaterEqual(len(cards), 3)
        self.assertIn("kim_vocal_2", ids)
        self.assertIn("htdemucs", ids)
        self.assertIn("htdemucs_6s", ids)


if __name__ == "__main__":
    unittest.main()
