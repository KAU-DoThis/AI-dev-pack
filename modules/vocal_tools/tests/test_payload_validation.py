import unittest

from vocal_tools import validate_audio_separate_payload


class PayloadValidationSmokeTests(unittest.TestCase):
    def test_valid_audio_separate_payload_passes(self):
        payload = validate_audio_separate_payload(
            {
                "models_dir": "test_models/mdx",
                "input_audio": "input.wav",
                "output_dir": "output/separate",
                "model_id": "kim_vocal_2",
                "options": {"denoise": False},
            }
        )

        self.assertEqual(payload["models_dir"], "test_models/mdx")
        self.assertEqual(payload["model_id"], "kim_vocal_2")
        self.assertEqual(payload["options"], {"denoise": False})

    def test_missing_required_field_raises(self):
        with self.assertRaises(ValueError):
            validate_audio_separate_payload(
                {
                    "models_dir": "test_models/mdx",
                    "input_audio": "input.wav",
                    "output_dir": "output/separate",
                    "options": {},
                }
            )

    def test_options_must_be_dict(self):
        with self.assertRaises(ValueError):
            validate_audio_separate_payload(
                {
                    "models_dir": "test_models/mdx",
                    "input_audio": "input.wav",
                    "output_dir": "output/separate",
                    "model_id": "kim_vocal_2",
                    "options": ["denoise=false"],
                }
            )


if __name__ == "__main__":
    unittest.main()
