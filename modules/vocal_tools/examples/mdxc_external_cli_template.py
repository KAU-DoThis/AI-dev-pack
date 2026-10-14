from __future__ import annotations

from vocal_tools import ModelCatalog, Separator


def main() -> None:
    catalog = ModelCatalog(model_dir="models")
    separator = Separator(catalog=catalog)

    result = separator.separate(
        input_audio="input.wav",
        output_dir="output/mdxc",
        model_id="mdxc_roformer_default",
        options={
            "backend": "external_cli",
            "model_config_path": "models/mdxc/config.yaml",
            "checkpoint_path": "models/mdxc/checkpoint.ckpt",
            "command": [
                "python",
                "-m",
                "my_mdxc_runner",
                "--input",
                "{input_audio}",
                "--output-dir",
                "{output_dir}",
                "--config",
                "{model_config_path}",
                "--checkpoint",
                "{checkpoint_path}",
                "--device",
                "{device}",
            ],
        },
    )
    print(result.to_dict())


if __name__ == "__main__":
    main()
