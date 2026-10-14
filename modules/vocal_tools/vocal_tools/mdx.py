from __future__ import annotations

import gc
import hashlib
import json
import os
import queue
import threading
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")
stem_naming = {'Vocals': 'Instrumental', 'Other': 'Instruments', 'Instrumental': 'Vocals', 'Drums': 'Drumless', 'Bass': 'Bassless'}


def _import_librosa():
    try:
        import librosa
    except ImportError as exc:
        raise RuntimeError("MDX separation requires librosa. Install the MDX/audio runtime dependencies first.") from exc
    return librosa


def _import_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("MDX separation requires numpy. Install the MDX/audio runtime dependencies first.") from exc
    return np


def _import_onnxruntime():
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError("MDX ONNX separation requires onnxruntime or onnxruntime-gpu.") from exc
    return ort


def _import_soundfile():
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError("MDX separation requires soundfile. Install the audio runtime dependencies first.") from exc
    return sf


def _import_torch():
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("MDX separation requires PyTorch. Install the MDX/audio runtime dependencies first.") from exc
    return torch


def _cuda_available() -> bool:
    return bool(_import_torch().cuda.is_available())


def _available_ort_providers() -> list[str]:
    return list(_import_onnxruntime().get_available_providers())

DEFAULT_VOCAL_MODEL = "Kim_Vocal_2.onnx"
DEFAULT_KARAOKE_MODEL = "UVR_MDXNET_KARA_2.onnx"
DEFAULT_DEREVERB_MODEL = "Reverb_HQ_By_FoxJoy.onnx"


@dataclass(frozen=True)
class MDXModelNames:
    vocal: str = DEFAULT_VOCAL_MODEL
    karaoke: str = DEFAULT_KARAOKE_MODEL
    dereverb: str = DEFAULT_DEREVERB_MODEL


@dataclass(frozen=True)
class MDXSeparationResult:
    input: str
    vocals: Optional[str]
    instrumental: Optional[str]
    main_vocals: Optional[str] = None
    backup_vocals: Optional[str] = None
    main_vocals_dereverb: Optional[str] = None

    def as_dict(self):
        return asdict(self)


def load_mdx_model_params(models_dir):
    model_data_path = Path(models_dir) / "model_data.json"
    if not model_data_path.exists():
        raise FileNotFoundError(f"MDX model_data.json not found: {model_data_path}")
    with model_data_path.open("r", encoding="utf-8") as infile:
        return json.load(infile)


def ensure_stereo_44k(input_path, work_dir):
    librosa = _import_librosa()
    np = _import_numpy()
    sf = _import_soundfile()
    input_path = Path(input_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    wave, sr = librosa.load(str(input_path), sr=44100, mono=False)
    was_mono = isinstance(wave, np.ndarray) and wave.ndim == 1
    if isinstance(wave, np.ndarray) and wave.ndim == 1:
        wave = np.vstack([wave, wave])

    if sr != 44100 or input_path.suffix.lower() != ".wav" or was_mono:
        out_path = work_dir / f"{input_path.stem}_44k_stereo.wav"
        sf.write(str(out_path), wave.T, 44100)
        return out_path

    return input_path


def _select_processor(processor):
    if processor is None:
        torch = _import_torch()
        return 0 if torch.cuda.is_available() else -1
    return processor


def _device_from_processor(processor):
    torch = _import_torch()
    if processor >= 0 and torch.cuda.is_available():
        return torch.device(f"cuda:{processor}")
    return torch.device("cpu")


def _ort_providers(processor):
    available = _available_ort_providers()
    if processor >= 0 and "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


class MDXModel:
    def __init__(self, device, dim_f, dim_t, n_fft, hop=1024, stem_name=None, compensation=1.000):
        torch = _import_torch()
        self.dim_f = dim_f
        self.dim_t = dim_t
        self.dim_c = 4
        self.n_fft = n_fft
        self.hop = hop
        self.stem_name = stem_name
        self.compensation = compensation

        self.n_bins = self.n_fft // 2 + 1
        self.chunk_size = hop * (self.dim_t - 1)
        self.window = torch.hann_window(window_length=self.n_fft, periodic=True).to(device)

        out_c = self.dim_c

        self.freq_pad = torch.zeros([1, out_c, self.n_bins - self.dim_f, self.dim_t]).to(device)

    def stft(self, x):
        torch = _import_torch()
        x = x.reshape([-1, self.chunk_size])
        x = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True, return_complex=True)
        x = torch.view_as_real(x)
        x = x.permute([0, 3, 1, 2])
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, 4, self.n_bins, self.dim_t])
        return x[:, :, :self.dim_f]

    def istft(self, x, freq_pad=None):
        torch = _import_torch()
        freq_pad = self.freq_pad.repeat([x.shape[0], 1, 1, 1]) if freq_pad is None else freq_pad
        x = torch.cat([x, freq_pad], -2)
        # c = 4*2 if self.target_name=='*' else 2
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, 2, self.n_bins, self.dim_t])
        x = x.permute([0, 2, 3, 1])
        x = x.contiguous()
        x = torch.view_as_complex(x)
        x = torch.istft(x, n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True)
        return x.reshape([-1, 2, self.chunk_size])


class MDX:
    DEFAULT_SR = 44100
    # Unit: seconds
    DEFAULT_CHUNK_SIZE = 0 * DEFAULT_SR
    DEFAULT_MARGIN_SIZE = 1 * DEFAULT_SR

    DEFAULT_PROCESSOR = 0

    def __init__(self, model_path: str, params: MDXModel, processor=DEFAULT_PROCESSOR):
        ort = _import_onnxruntime()
        torch = _import_torch()
        processor = _select_processor(processor)

        self.device = _device_from_processor(processor)
        self.provider = _ort_providers(processor)

        self.model = params

        # Load the ONNX model using ONNX Runtime
        self.ort = ort.InferenceSession(model_path, providers=self.provider)
        # Preload the model for faster performance
        self.ort.run(None, {'input': torch.rand(1, 4, params.dim_f, params.dim_t).numpy()})
        self.process = lambda spec: self.ort.run(None, {'input': spec.cpu().numpy()})[0]

        self.prog = None

    @staticmethod
    def get_hash(model_path):
        try:
            with open(model_path, 'rb') as f:
                f.seek(- 10000 * 1024, 2)
                model_hash = hashlib.md5(f.read()).hexdigest()
        except:
            model_hash = hashlib.md5(open(model_path, 'rb').read()).hexdigest()

        return model_hash

    @staticmethod
    def segment(wave, combine=True, chunk_size=DEFAULT_CHUNK_SIZE, margin_size=DEFAULT_MARGIN_SIZE):
        np = _import_numpy()
        """
        Segment or join segmented wave array

        Args:
            wave: (np.array) Wave array to be segmented or joined
            combine: (bool) If True, combines segmented wave array. If False, segments wave array.
            chunk_size: (int) Size of each segment (in samples)
            margin_size: (int) Size of margin between segments (in samples)

        Returns:
            numpy array: Segmented or joined wave array
        """

        if combine:
            processed_wave = None  # Initializing as None instead of [] for later numpy array concatenation
            for segment_count, segment in enumerate(wave):
                start = 0 if segment_count == 0 else margin_size
                end = None if segment_count == len(wave) - 1 else -margin_size
                if margin_size == 0:
                    end = None
                if processed_wave is None:  # Create array for first segment
                    processed_wave = segment[:, start:end]
                else:  # Concatenate to existing array for subsequent segments
                    processed_wave = np.concatenate((processed_wave, segment[:, start:end]), axis=-1)

        else:
            processed_wave = []
            sample_count = wave.shape[-1]

            if chunk_size <= 0 or chunk_size > sample_count:
                chunk_size = sample_count

            if margin_size > chunk_size:
                margin_size = chunk_size

            for segment_count, skip in enumerate(range(0, sample_count, chunk_size)):

                margin = 0 if segment_count == 0 else margin_size
                end = min(skip + chunk_size + margin_size, sample_count)
                start = skip - margin

                cut = wave[:, start:end].copy()
                processed_wave.append(cut)

                if end == sample_count:
                    break

        return processed_wave

    def pad_wave(self, wave):
        np = _import_numpy()
        torch = _import_torch()
        """
        Pad the wave array to match the required chunk size

        Args:
            wave: (np.array) Wave array to be padded

        Returns:
            tuple: (padded_wave, pad, trim)
                - padded_wave: Padded wave array
                - pad: Number of samples that were padded
                - trim: Number of samples that were trimmed
        """
        n_sample = wave.shape[1]
        trim = self.model.n_fft // 2
        gen_size = self.model.chunk_size - 2 * trim
        pad = gen_size - n_sample % gen_size

        # Padded wave
        wave_p = np.concatenate((np.zeros((2, trim)), wave, np.zeros((2, pad)), np.zeros((2, trim))), 1)

        mix_waves = []
        for i in range(0, n_sample + pad, gen_size):
            waves = np.array(wave_p[:, i:i + self.model.chunk_size])
            mix_waves.append(waves)

        mix_waves = torch.tensor(mix_waves, dtype=torch.float32).to(self.device)

        return mix_waves, pad, trim

    def _process_wave(self, mix_waves, trim, pad, q: queue.Queue, _id: int):
        torch = _import_torch()
        np = _import_numpy()
        """
        Process each wave segment in a multi-threaded environment

        Args:
            mix_waves: (torch.Tensor) Wave segments to be processed
            trim: (int) Number of samples trimmed during padding
            pad: (int) Number of samples padded during padding
            q: (queue.Queue) Queue to hold the processed wave segments
            _id: (int) Identifier of the processed wave segment

        Returns:
            numpy array: Processed wave segment
        """
        mix_waves = mix_waves.split(1)
        with torch.no_grad():
            pw = []
            for mix_wave in mix_waves:
                self.prog.update()
                spec = self.model.stft(mix_wave)
                processed_spec = torch.tensor(self.process(spec))
                processed_wav = self.model.istft(processed_spec.to(self.device))
                processed_wav = processed_wav[:, :, trim:-trim].transpose(0, 1).reshape(2, -1).cpu().numpy()
                pw.append(processed_wav)
        processed_signal = np.concatenate(pw, axis=-1)[:, :-pad]
        q.put({_id: processed_signal})
        return processed_signal

    def process_wave(self, wave: np.array, mt_threads=1):
        from tqdm import tqdm

        """
        Process the wave array in a multi-threaded environment

        Args:
            wave: (np.array) Wave array to be processed
            mt_threads: (int) Number of threads to be used for processing

        Returns:
            numpy array: Processed wave array
        """
        self.prog = tqdm(total=0)
        chunk = wave.shape[-1] // mt_threads
        waves = self.segment(wave, False, chunk)

        # Create a queue to hold the processed wave segments
        q = queue.Queue()
        threads = []
        for c, batch in enumerate(waves):
            mix_waves, pad, trim = self.pad_wave(batch)
            self.prog.total = len(mix_waves) * mt_threads
            thread = threading.Thread(target=self._process_wave, args=(mix_waves, trim, pad, q, c))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        self.prog.close()

        processed_batches = []
        while not q.empty():
            processed_batches.append(q.get())
        processed_batches = [list(wave.values())[0] for wave in
                             sorted(processed_batches, key=lambda d: list(d.keys())[0])]
        assert len(processed_batches) == len(waves), 'Incomplete processed batches, please reduce batch size!'
        return self.segment(processed_batches, True, chunk)


def run_mdx(model_params, output_dir, model_path, filename, exclude_main=False, exclude_inversion=False, suffix=None, invert_suffix=None, denoise=False, keep_orig=True, m_threads=2, processor=None):
    librosa = _import_librosa()
    np = _import_numpy()
    sf = _import_soundfile()
    torch = _import_torch()
    output_dir = str(output_dir)
    model_path = str(model_path)
    filename = str(filename)
    os.makedirs(output_dir, exist_ok=True)

    processor = _select_processor(processor)
    device = _device_from_processor(processor)

    if device.type == "cuda":
        device_properties = torch.cuda.get_device_properties(device)
        vram_gb = device_properties.total_memory / 1024**3
        m_threads = 1 if vram_gb < 8 else max(1, m_threads)
    else:
        m_threads = max(1, m_threads)

    model_hash = MDX.get_hash(model_path)
    mp = model_params.get(model_hash)
    if mp is None:
        raise KeyError(f"No MDX parameters found for model hash {model_hash}: {model_path}")

    model = MDXModel(
        device,
        dim_f=mp["mdx_dim_f_set"],
        dim_t=2 ** mp["mdx_dim_t_set"],
        n_fft=mp["mdx_n_fft_scale_set"],
        stem_name=mp["primary_stem"],
        compensation=mp["compensate"]
    )

    mdx_sess = MDX(model_path, model, processor=processor)
    wave, sr = librosa.load(filename, mono=False, sr=44100)
    if isinstance(wave, np.ndarray) and wave.ndim == 1:
        wave = np.vstack([wave, wave])
    # normalizing input wave gives better output
    peak = max(np.max(wave), abs(np.min(wave)))
    if peak > 0:
        wave /= peak
    if denoise:
        wave_processed = -(mdx_sess.process_wave(-wave, m_threads)) + (mdx_sess.process_wave(wave, m_threads))
        wave_processed *= 0.5
    else:
        wave_processed = mdx_sess.process_wave(wave, m_threads)
    # return to previous peak
    wave_processed *= peak
    stem_name = model.stem_name if suffix is None else suffix

    main_filepath = None
    if not exclude_main:
        main_filepath = os.path.join(output_dir, f"{os.path.basename(os.path.splitext(filename)[0])}_{stem_name}.wav")
        sf.write(main_filepath, wave_processed.T, sr)

    invert_filepath = None
    if not exclude_inversion:
        diff_stem_name = stem_naming.get(stem_name) if invert_suffix is None else invert_suffix
        stem_name = f"{stem_name}_diff" if diff_stem_name is None else diff_stem_name
        invert_filepath = os.path.join(output_dir, f"{os.path.basename(os.path.splitext(filename)[0])}_{stem_name}.wav")
        sf.write(invert_filepath, (-wave_processed.T * model.compensation) + wave.T, sr)

    if not keep_orig:
        os.remove(filename)

    del mdx_sess, wave_processed, wave
    gc.collect()
    return main_filepath, invert_filepath


class MDXSeparator:
    def __init__(self, models_dir, model_names=None, model_params=None, processor=None):
        self.models_dir = Path(models_dir)
        self.model_names = model_names or MDXModelNames()
        self.model_params = model_params if model_params is not None else load_mdx_model_params(self.models_dir)
        self.processor = processor

    def model_path(self, filename):
        path = self.models_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"MDX model not found: {path}")
        return path

    def separate(
        self,
        input_audio,
        output_dir,
        denoise=False,
        keep_input=True,
        split_main_backup=False,
        dereverb=False,
        m_threads=2,
    ):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        input_audio = Path(input_audio)
        if not input_audio.exists():
            raise FileNotFoundError(f"Input audio not found: {input_audio}")

        safe_input = ensure_stereo_44k(input_audio, output_dir)
        vocals_path, instrumental_path = run_mdx(
            self.model_params,
            output_dir,
            self.model_path(self.model_names.vocal),
            safe_input,
            denoise=denoise,
            keep_orig=keep_input,
            m_threads=m_threads,
            processor=self.processor,
        )

        main_vocals_path = None
        backup_vocals_path = None
        main_vocals_dereverb_path = None

        if split_main_backup:
            backup_vocals_path, main_vocals_path = run_mdx(
                self.model_params,
                output_dir,
                self.model_path(self.model_names.karaoke),
                vocals_path,
                suffix="Backup",
                invert_suffix="Main",
                denoise=denoise,
                keep_orig=True,
                m_threads=m_threads,
                processor=self.processor,
            )

        if dereverb:
            if main_vocals_path is None:
                main_vocals_path = vocals_path

            _, main_vocals_dereverb_path = run_mdx(
                self.model_params,
                output_dir,
                self.model_path(self.model_names.dereverb),
                main_vocals_path,
                invert_suffix="DeReverb",
                exclude_main=True,
                denoise=denoise,
                keep_orig=True,
                m_threads=m_threads,
                processor=self.processor,
            )

        return MDXSeparationResult(
            input=str(input_audio),
            vocals=str(vocals_path) if vocals_path else None,
            instrumental=str(instrumental_path) if instrumental_path else None,
            main_vocals=str(main_vocals_path) if main_vocals_path else None,
            backup_vocals=str(backup_vocals_path) if backup_vocals_path else None,
            main_vocals_dereverb=str(main_vocals_dereverb_path) if main_vocals_dereverb_path else None,
        )
