from __future__ import annotations

from vocal_tools import ModelCatalog, Separator


def main() -> None:
    catalog = ModelCatalog(model_dir="models")
    separator = Separator(catalog=catalog)

    result = separator.separate(
        input_audio="input.wav",
        output_dir="output/vr_arch",
        model_id="vr_arch_vocal_default",
        options={
            "backend": "external_cli",
            "command": [
                "python",
                "-m",
                "my_vr_runner",
                "--input",
                "{input_audio}",
                "--output-dir",
                "{output_dir}",
                "--device",
                "{device}",
            ],
        },
    )
    print(result.to_dict())


if __name__ == "__main__":
    main()
