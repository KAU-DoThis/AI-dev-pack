import os

import yt_tools as yt
import ffmpeg_tools as ft

"""url = "https://www.youtube.com/watch?v=8ykMyNHAdKk"
t_dir = yt.download_video(url, 'temp')


original = os.listdir(t_dir)[0]
print(original)


file_path = os.path.join(t_dir, original)
ft.split_audio_video(file_path)
"""

### ----------------------- mdx

from vocal_tools.mdx import run_mdx
import json
import librosa
import shlex
import subprocess
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DIR = ''

mdxnet_models_dir = os.path.join(BASE_DIR, 'data', 'models', 'mdx')
rvc_models_dir = os.path.join(BASE_DIR, 'data', 'models')
output_dir = os.path.join(BASE_DIR, 'temp')

def convert_to_stereo(audio_path):
    wave, sr = librosa.load(audio_path, mono=False, sr=44100)

    # check if mono
    if type(wave[0]) != np.ndarray:
        stereo_path = f'{os.path.splitext(audio_path)[0]}_stereo.wav'
        command = shlex.split(f'ffmpeg -y -loglevel error -i "{audio_path}" -ac 2 -f wav "{stereo_path}"')
        subprocess.run(command)
        return stereo_path
    else:
        return audio_path

def get_audio_paths(song_dir):
    orig_song_path = None
    instrumentals_path = None
    main_vocals_dereverb_path = None
    backup_vocals_path = None

    for file in os.listdir(song_dir):
        if file.endswith('_Instrumental.wav'):
            instrumentals_path = os.path.join(song_dir, file)
            orig_song_path = instrumentals_path.replace('_Instrumental', '')

        elif file.endswith('_Vocals_Main_DeReverb.wav'):
            main_vocals_dereverb_path = os.path.join(song_dir, file)

        elif file.endswith('_Vocals_Backup.wav'):
            backup_vocals_path = os.path.join(song_dir, file)

    return orig_song_path, instrumentals_path, main_vocals_dereverb_path, backup_vocals_path


def preprocess_song(song_input, mdx_model_params, song_id):
    keep_orig = False
    orig_song_path = song_input

    song_output_dir = os.path.join(output_dir, song_id)
    orig_song_path = convert_to_stereo(orig_song_path)

    #display_progress('[~] Separating Vocals from Instrumental...', 0.1, is_webui, progress)
    #vocals_path, instrumentals_path = run_mdx(mdx_model_params, song_output_dir, os.path.join(mdxnet_models_dir, 'UVR-MDX-NET-Voc_FT.onnx'), orig_song_path, denoise=True, keep_orig=keep_orig)
    vocals_path, instrumentals_path = run_mdx(mdx_model_params, song_output_dir, os.path.join(mdxnet_models_dir, 'Kim_Vocal_2.onnx'), orig_song_path, denoise=True, keep_orig=keep_orig)

    #display_progress('[~] Separating Main Vocals from Backup Vocals...', 0.2, is_webui, progress)
    backup_vocals_path, main_vocals_path = run_mdx(mdx_model_params, song_output_dir, os.path.join(mdxnet_models_dir, 'UVR_MDXNET_KARA_2.onnx'), vocals_path, suffix='Backup', invert_suffix='Main', denoise=True)

    #display_progress('[~] Applying DeReverb to Vocals...', 0.3, is_webui, progress)
    _, main_vocals_dereverb_path = run_mdx(mdx_model_params, song_output_dir, os.path.join(mdxnet_models_dir, 'Reverb_HQ_By_FoxJoy.onnx'), main_vocals_path, invert_suffix='DeReverb', exclude_main=True, denoise=True)

    return orig_song_path, vocals_path, instrumentals_path, main_vocals_path, backup_vocals_path, main_vocals_dereverb_path

def test_mdx():
    song_file = "forest.mp3"
    keep_files = False
    with open(os.path.join(mdxnet_models_dir, 'model_data.json')) as infile:
        mdx_model_params = json.load(infile)

    base_name, ext = os.path.splitext(song_file)
    song_dir = os.path.join(output_dir, base_name)
    
    if not os.path.exists(song_dir):
        os.makedirs(song_dir)
        orig_song_path, vocals_path, instrumentals_path, main_vocals_path, backup_vocals_path, main_vocals_dereverb_path = preprocess_song(song_file, mdx_model_params, base_name)

    else:
        vocals_path, main_vocals_path = None, None
        paths = get_audio_paths(song_dir)

        # if any of the audio files aren't available or keep intermediate files, rerun preprocess
        if any(path is None for path in paths) or keep_files:
            orig_song_path, vocals_path, instrumentals_path, main_vocals_path, backup_vocals_path, main_vocals_dereverb_path = preprocess_song(song_file, mdx_model_params, base_name)
        else:
            orig_song_path, instrumentals_path, main_vocals_dereverb_path, backup_vocals_path = paths

### ------------------------------------------------------

from vocal_tools.rvc import Config, load_hubert, get_vc, rvc_infer
import gc

def get_rvc_model(voice_model):
    rvc_model_filename, rvc_index_filename = None, None
    model_dir = os.path.join(rvc_models_dir, voice_model)
    for file in os.listdir(model_dir):
        ext = os.path.splitext(file)[1]
        if ext == '.pth':
            rvc_model_filename = file
        if ext == '.index':
            rvc_index_filename = file

    if rvc_model_filename is None:
        error_msg = f'No model file exists in {model_dir}.'

    return os.path.join(model_dir, rvc_model_filename), os.path.join(model_dir, rvc_index_filename) if rvc_index_filename else ''


def voice_change(voice_model,
                  vocals_path,
                    output_path,
                      pitch_change,
                        f0_method,
                          index_rate,
                            filter_radius,
                              rms_mix_rate,
                                protect,
                                  crepe_hop_length):
    rvc_model_path, rvc_index_path = get_rvc_model(voice_model)
    device = 'cuda:0'
    config = Config(device, True)
    hubert_model = load_hubert(device, config.is_half, os.path.join(rvc_models_dir, 'hubert_base.pt'))
    cpt, version, net_g, tgt_sr, vc = get_vc(device, config.is_half, config, rvc_model_path)

    # convert main vocals
    rvc_infer(rvc_index_path, index_rate, vocals_path, output_path, pitch_change, f0_method, cpt, version, net_g, filter_radius, tgt_sr, rms_mix_rate, protect, crepe_hop_length, vc, hubert_model)
    del hubert_model, cpt
    gc.collect()


from pedalboard import Pedalboard, Reverb, Compressor, HighpassFilter
from pedalboard.io import AudioFile

def add_audio_effects(audio_path, reverb_rm_size, reverb_wet, reverb_dry, reverb_damping):
    output_path = f'{os.path.splitext(audio_path)[0]}_mixed.wav'

    # Initialize audio effects plugins
    board = Pedalboard(
        [
            HighpassFilter(),
            Compressor(ratio=4, threshold_db=-15),
            Reverb(room_size=reverb_rm_size, dry_level=reverb_dry, wet_level=reverb_wet, damping=reverb_damping)
         ]
    )

    with AudioFile(audio_path) as f:
        with AudioFile(output_path, 'w', f.samplerate, f.num_channels) as o:
            # Read one second of audio at a time, until the file is empty:
            while f.tell() < f.frames:
                chunk = f.read(int(f.samplerate))
                effected = board(chunk, f.samplerate, reset=False)
                o.write(effected)

    return output_path

def test_voice():
    voice_model = "iut"
    main_vocals_dereverb_path = 'temp/forest/forest_Vocals_Main_DeReverb.wav'
    pitch_change = 0
    f0_method='rmvpe'
    index_rate=0.5
    protect=0.33
    filter_radius=3
    rms_mix_rate=0.25
    crepe_hop_length=128
    output_format='mp3'

    song_file = "forest.mp3"
    base_name, ext = os.path.splitext(song_file)
    song_dir = os.path.join(output_dir, base_name)
    orig_song_path = base_name
    
    ai_vocals_path = os.path.join(song_dir, f'{os.path.splitext(os.path.basename(orig_song_path))[0]}_{voice_model}_p{pitch_change}_i{index_rate}_fr{filter_radius}_rms{rms_mix_rate}_pro{protect}_{f0_method}{"" if f0_method != "mangio-crepe" else f"_{crepe_hop_length}"}.wav')
    
    ai_cover_path = os.path.join(song_dir, f'{os.path.splitext(os.path.basename(orig_song_path))[0]} ({voice_model} Ver).{output_format}')

    voice_change(voice_model, main_vocals_dereverb_path, ai_vocals_path, pitch_change, f0_method, index_rate, filter_radius, rms_mix_rate, protect, crepe_hop_length)


from pydub import AudioSegment

def combine_audio(audio_paths, output_path, main_gain, backup_gain, inst_gain, output_format):
    main_vocal_audio = AudioSegment.from_wav(audio_paths[0]) - 4 + main_gain
    backup_vocal_audio = AudioSegment.from_wav(audio_paths[1]) - 6 + backup_gain
    instrumental_audio = AudioSegment.from_wav(audio_paths[2]) - 7 + inst_gain
    main_vocal_audio.overlay(backup_vocal_audio).overlay(instrumental_audio).export(output_path, format=output_format)


"""def test_combine():
    combine_audio([ai_vocals_mixed_path, backup_vocals_path, instrumentals_path], ai_cover_path, main_gain, backup_gain, inst_gain, output_format)
    return ai_cover_path"""



if __name__ == "__main__":
    #test_mdx()
    test_voice()