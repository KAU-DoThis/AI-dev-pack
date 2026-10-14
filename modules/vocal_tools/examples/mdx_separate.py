from pathlib import Path

from vocal_tools.mdx import MDXSeparator


def main():
    separator = MDXSeparator(models_dir="data/models/mdx")
    result = separator.separate(
        input_audio=Path("input/song.mp3"),
        output_dir=Path("output/song"),
        denoise=True,
        split_main_backup=True,
        dereverb=True,
    )

    for name, path in result.as_dict().items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
