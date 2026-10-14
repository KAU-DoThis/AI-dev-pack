"""Run explicitly after installing vocal-tools with the required TTS extra."""
import argparse
import json

from vocal_tools import TTS


def main():
    parser = argparse.ArgumentParser(description='Generate speech with Edge or Qwen3-TTS.')
    parser.add_argument('--engine', choices=['edge', 'qwen3'], default='edge')
    parser.add_argument('--text', default='안녕하세요. 음성 합성 테스트입니다.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--reference-audio')
    parser.add_argument('--reference-text')
    args = parser.parse_args()
    kwargs = {}
    if args.engine == 'qwen3':
        if not args.reference_audio:
            parser.error('--reference-audio is required for qwen3')
        kwargs = {'reference_audio': args.reference_audio,
                  'reference_text': args.reference_text, 'language': 'Korean'}
    with TTS(args.engine) as tts:
        result = tts.synthesize(args.text, args.output, **kwargs)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
