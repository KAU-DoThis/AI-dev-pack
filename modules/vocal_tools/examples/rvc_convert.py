from pathlib import Path

from vocal_tools.rvc import RVCConversionOptions, RVCConverter


def main():
    converter = RVCConverter(models_dir="data/models")
    options = RVCConversionOptions(
        pitch_change=0,
        f0_method="rmvpe",
        index_rate=0.5,
    )

    output_path = converter.convert(
        input_audio=Path("output/song/song_Vocals_Main_DeReverb.wav"),
        output_audio=Path("output/song/song_iut.wav"),
        voice_model="iut",
        options=options,
    )
    print(output_path)


if __name__ == "__main__":
    main()
