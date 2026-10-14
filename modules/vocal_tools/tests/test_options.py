import unittest

from vocal_tools.core import parse_key_value_options


class OptionParserSmokeTests(unittest.TestCase):
    def test_parse_key_value_options(self):
        options = parse_key_value_options(
            ["denoise=false", "overlap=0.25", "shifts=1", "x=null"]
        )

        self.assertEqual(
            options,
            {
                "denoise": False,
                "overlap": 0.25,
                "shifts": 1,
                "x": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
