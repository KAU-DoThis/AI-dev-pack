import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from vocal_tools import EdgeTTS, Qwen3TTS, TTS, TTSResult, TTSError, TTSDependencyError
from vocal_tools.core import InvalidOptionError, ProgressEvent
from vocal_tools.tasks import run_tts_payload, run_tts_payload_async, validate_tts_payload


def write_wav(path, data=None, rate=24000, **kwargs):
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack('<hhhh', 0, 1000, -1000, 0))


class FakeEdge:
    calls = []
    failure = False

    def __init__(self, text, voice, **kwargs):
        self.calls.append((text, voice, kwargs))

    async def save(self, path):
        Path(path).write_bytes(b'ID3test-audio')
        if self.failure:
            raise ConnectionError('service unavailable')


class FakeWave(list):
    ndim = 1
    size = 4


class FakeQwen:
    def __init__(self):
        self.model = SimpleNamespace(tts_model_type='base')
        self.prompts = []
        self.generations = []
        self.failure = False

    def create_voice_clone_prompt(self, **kwargs):
        self.prompts.append(kwargs)
        return ['prompt', len(self.prompts)]

    def generate_voice_clone(self, **kwargs):
        self.generations.append(kwargs)
        if self.failure:
            raise RuntimeError('inference failed')
        return [[0.0, 0.1, -0.1, 0.0]], 24000


class TTSIntegrationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.reference = self.root/'reference.wav'
        write_wav(self.reference)
        FakeEdge.calls = []
        FakeEdge.failure = False
        self.fake_qwen = FakeQwen()
        self.loads = []
        self.downloads = []

        def load(source, **kwargs):
            self.loads.append((source, kwargs))
            return self.fake_qwen

        def download(**kwargs):
            self.downloads.append(kwargs)
            return str(self.root/'snapshot')

        async def voices(**kwargs):
            return [{'ShortName':'ko-KR-SunHiNeural','Locale':'ko-KR','Gender':'Female'},
                    {'ShortName':'en-US-AriaNeural','Locale':'en-US','Gender':'Female'}]

        self.modules = {
            'edge_tts': SimpleNamespace(Communicate=FakeEdge, list_voices=voices),
            'qwen_tts': SimpleNamespace(Qwen3TTSModel=SimpleNamespace(from_pretrained=load)),
            'huggingface_hub': SimpleNamespace(snapshot_download=download),
            'torch': SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: True),
                                     float32='fp32',float16='fp16',bfloat16='bf16'),
            'numpy': SimpleNamespace(asarray=lambda v: FakeWave(v),
                                     isfinite=lambda v: SimpleNamespace(all=lambda: True)),
            'soundfile': SimpleNamespace(write=write_wav),
        }
        self.module_patch = patch.dict(sys.modules, self.modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def qwen_call(self, engine, output='out.wav', **kwargs):
        return engine.synthesize('안녕하세요', self.root/output, reference_audio=self.reference, **kwargs)

    def test_edge_sync_output_voice_options_and_events(self):
        events = []
        result = EdgeTTS().synthesize('안녕하세요', self.root/'edge.mp3',
            options={'rate':'+10%'}, progress_callback=events.append)
        self.assertIsInstance(result, TTSResult)
        self.assertEqual(result.output_path.read_bytes(), b'ID3test-audio')
        self.assertEqual(FakeEdge.calls[0][1], 'ko-KR-SunHiNeural')
        self.assertEqual(FakeEdge.calls[0][2]['rate'], '+10%')
        self.assertTrue(all(isinstance(e, ProgressEvent) for e in events))
        self.assertEqual([e.stage for e in events], ['start','complete'])
        json.dumps(result.to_dict())

    def test_edge_async_inside_event_loop(self):
        async def call():
            with self.assertRaisesRegex(TTSError, 'synthesize_async'):
                EdgeTTS().synthesize('test', self.root/'wrong.mp3')
            return await EdgeTTS().synthesize_async('test', self.root/'async.mp3')
        self.assertTrue(asyncio.run(call()).output_path.exists())

    def test_edge_empty_text_and_unknown_options_fail_before_network(self):
        for text, options in [('',None), ('text',{'bad':1}), ('text',{'rate':'fast'})]:
            with self.assertRaises(InvalidOptionError):
                EdgeTTS().synthesize(text,self.root/'bad.mp3',options=options)
        self.assertEqual(FakeEdge.calls, [])

    def test_edge_extension_and_missing_ffmpeg_are_explicit(self):
        with self.assertRaises(InvalidOptionError):
            EdgeTTS().synthesize('test',self.root/'bad.ogg')
        with patch('vocal_tools.tts.edge.shutil.which', return_value=None):
            with self.assertRaisesRegex(TTSDependencyError,'ffmpeg'):
                EdgeTTS().synthesize('test',self.root/'bad.wav')
        self.assertEqual(FakeEdge.calls, [])

    def test_edge_wav_decodes_instead_of_renaming_mp3(self):
        with patch('vocal_tools.tts.edge.shutil.which', return_value='ffmpeg'), patch(
            'vocal_tools.tts.edge._decode_wav', side_effect=lambda exe,source,target:write_wav(target)) as decode:
            result = EdgeTTS().synthesize('test',self.root/'out.wav')
        decode.assert_called_once()
        self.assertEqual(result.output_path.read_bytes()[:4], b'RIFF')
        self.assertEqual(result.sample_rate,24000)
        self.assertGreater(result.duration_sec,0)

    def test_edge_failure_preserves_existing_file_and_removes_partial(self):
        target=self.root/'out.mp3'
        target.write_bytes(b'old')
        FakeEdge.failure=True
        with self.assertRaisesRegex(TTSError,'service unavailable'):
            EdgeTTS().synthesize('test',target)
        self.assertEqual(target.read_bytes(),b'old')
        self.assertEqual(list(self.root.glob('.vocal-tts-*')),[])

    def test_edge_voice_listing_can_filter_locale(self):
        voices = EdgeTTS().list_voices(locale='ko-KR')
        self.assertEqual([v['id'] for v in voices],['ko-KR-SunHiNeural'])

    def test_missing_optional_dependencies_are_actionable(self):
        with patch.dict(sys.modules,{'edge_tts':None}):
            with self.assertRaisesRegex(TTSDependencyError,r'vocal-tools\[edge-tts\]'):
                EdgeTTS().synthesize('test',self.root/'out.mp3')
        with patch.dict(sys.modules,{'qwen_tts':None}):
            with self.assertRaisesRegex(TTSDependencyError,r'vocal-tools\[qwen-tts\]'):
                self.qwen_call(Qwen3TTS())

    def test_qwen_lazily_loads_and_reuses_model_and_prompt(self):
        engine=Qwen3TTS(cache_dir=self.root/'cache')
        self.assertEqual(self.loads,[])
        first=self.qwen_call(engine,reference_text='원본 문장',language='ko',options={'max_new_tokens':128})
        second=self.qwen_call(engine,'again.wav',reference_text='원본 문장',language='ko')
        self.assertEqual(len(self.loads),1)
        self.assertEqual(len(self.fake_qwen.prompts),1)
        self.assertFalse(first.metadata['reference_cached'])
        self.assertTrue(second.metadata['reference_cached'])
        self.assertFalse(first.metadata['x_vector_only_mode'])
        self.assertEqual(self.fake_qwen.generations[0]['language'],'Korean')
        self.assertEqual(self.fake_qwen.generations[0]['max_new_tokens'],128)
        self.assertEqual(self.loads[0][1]['dtype'],'bf16')
        self.assertEqual(self.downloads[0]['cache_dir'],str(self.root/'cache'))
        self.assertEqual(second.output_path.read_bytes()[:4],b'RIFF')
        json.dumps(second.to_dict())

    def test_qwen_reference_without_transcript_selects_embedding_mode(self):
        result=self.qwen_call(Qwen3TTS())
        self.assertTrue(result.metadata['x_vector_only_mode'])
        self.assertTrue(self.fake_qwen.prompts[0]['x_vector_only_mode'])

    def test_qwen_requires_transcript_when_embedding_mode_disabled(self):
        with self.assertRaisesRegex(InvalidOptionError,'reference_text'):
            self.qwen_call(Qwen3TTS(),x_vector_only_mode=False)
        self.assertEqual(self.loads,[])

    def test_qwen_missing_reference_fails_before_loading(self):
        with self.assertRaisesRegex(FileNotFoundError,'Reference audio'):
            Qwen3TTS().synthesize('test',self.root/'out.wav',reference_audio=self.root/'missing.wav')
        self.assertEqual(self.loads,[])

    def test_qwen_cannot_overwrite_reference(self):
        with self.assertRaises(InvalidOptionError):
            Qwen3TTS().synthesize('test',self.reference,reference_audio=self.reference)

    def test_qwen_local_model_bypasses_download_and_cpu_uses_fp32(self):
        local=self.root/'model'
        local.mkdir()
        self.qwen_call(Qwen3TTS(model=local,device='cpu'))
        self.assertEqual(self.downloads,[])
        self.assertEqual(self.loads[0],(str(local.resolve()),{'device_map':'cpu','dtype':'fp32','attn_implementation':'sdpa'}))

    def test_qwen_offline_flag_reaches_snapshot_resolver(self):
        self.qwen_call(Qwen3TTS(local_files_only=True))
        self.assertTrue(self.downloads[0]['local_files_only'])

    def test_qwen_changed_reference_and_transcript_invalidate_prompt(self):
        engine=Qwen3TTS()
        self.qwen_call(engine,reference_text='one')
        self.qwen_call(engine,'two.wav',reference_text='two')
        self.reference.write_bytes(self.reference.read_bytes()+b'changed')
        self.qwen_call(engine,'three.wav',reference_text='two')
        self.assertEqual(len(self.fake_qwen.prompts),3)

    def test_qwen_cache_bound_and_close(self):
        engine=Qwen3TTS(reference_cache_size=1)
        self.qwen_call(engine,reference_text='one')
        self.qwen_call(engine,'two.wav',reference_text='two')
        self.qwen_call(engine,'three.wav',reference_text='one')
        self.assertEqual(len(self.fake_qwen.prompts),3)
        self.assertEqual(len(engine._references),1)
        engine.close()
        self.assertIsNone(engine._model)
        self.assertEqual(len(engine._references),0)

    def test_qwen_generation_failure_preserves_existing_output(self):
        target=self.root/'out.wav'
        target.write_bytes(b'old')
        self.fake_qwen.failure=True
        with self.assertRaisesRegex(TTSError,'inference failed'):
            self.qwen_call(Qwen3TTS())
        self.assertEqual(target.read_bytes(),b'old')

    def test_qwen_reserved_options_are_rejected(self):
        with self.assertRaises(InvalidOptionError):
            self.qwen_call(Qwen3TTS(),options={'text':'override'})

    def test_qwen_wrong_model_family_is_rejected(self):
        with self.assertRaisesRegex(InvalidOptionError,'Base'):
            Qwen3TTS(model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice')
        self.fake_qwen.model.tts_model_type='custom_voice'
        with self.assertRaisesRegex(TTSError,'Base'):
            self.qwen_call(Qwen3TTS())

    def test_qwen_async_uses_same_result_and_event_contract(self):
        events=[]
        result=asyncio.run(Qwen3TTS().synthesize_async('test',self.root/'async.wav',
            reference_audio=self.reference,progress_callback=events.append))
        self.assertIsInstance(result,TTSResult)
        self.assertTrue(all(isinstance(e,ProgressEvent) for e in events))
        self.assertEqual([e.stage for e in events],['start','load','reference','synthesis','complete'])

    def test_payload_and_direct_api_share_field_names(self):
        result=run_tts_payload({'engine':'edge','text':'test','output_path':self.root/'payload.mp3',
            'voice':'en-US-AriaNeural','options':{'pitch':'+2Hz'}})
        self.assertEqual(result['engine'],'edge')
        self.assertEqual(result['voice'],'en-US-AriaNeural')
        json.dumps(result)

    def test_async_payload_and_engine_validation(self):
        result=asyncio.run(run_tts_payload_async({'text':'test','output_path':self.root/'async.mp3'}))
        self.assertEqual(result['engine'],'edge')
        with self.assertRaises(InvalidOptionError):
            TTS('missing')
        with self.assertRaises(InvalidOptionError):
            validate_tts_payload({'engine':'qwen3','text':'test','output_path':'out.wav'})

    def test_payload_can_reuse_qwen_instance_without_closing_it(self):
        tts=TTS('qwen3')
        payload={'engine':'qwen3','text':'test','output_path':self.root/'payload.wav','reference_audio':self.reference}
        run_tts_payload(payload,synthesizer=tts)
        result=run_tts_payload(payload,synthesizer=tts)
        self.assertEqual(len(self.loads),1)
        self.assertTrue(result['metadata']['reference_cached'])
        with self.assertRaises(InvalidOptionError):
            run_tts_payload({**payload,'config':{'device':'cpu'}},synthesizer=tts)

    def test_payload_rejects_engine_mismatch_and_unknown_fields(self):
        for payload in [
            {'text':'test','output_path':'out.mp3','reference_audio':'ref.wav'},
            {'text':'test','output_path':'out.mp3','config':{'model':'bad'}},
        ]:
            with self.assertRaises(InvalidOptionError):
                run_tts_payload(payload)
        with self.assertRaises(InvalidOptionError):
            run_tts_payload({'text':'test','output_path':'out.mp3'},synthesizer=TTS('qwen3'))

    def test_cli_uses_the_payload_api(self):
        from vocal_tools.cli import main
        with patch('vocal_tools.cli.run_tts_payload',return_value={'output_path':'out.wav'}) as call, redirect_stdout(io.StringIO()):
            code=main(['tts','--engine','qwen3','--text','hello','--output','out.wav',
                '--reference-audio','ref.wav','--language','ko','--local-files-only','--option','max_new_tokens=128','--json'])
        self.assertEqual(code,0)
        payload=call.call_args.args[0]
        self.assertEqual(payload['reference_audio'],'ref.wav')
        self.assertEqual(payload['options']['max_new_tokens'],128)
        self.assertTrue(payload['config']['local_files_only'])

    def test_cli_json_survives_upstream_status_prints(self):
        from vocal_tools.cli import main
        def synthesize(payload):
            print('Loading upstream model')
            return {'output_path':'out.wav'}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('vocal_tools.cli.run_tts_payload',side_effect=synthesize), redirect_stdout(stdout), redirect_stderr(stderr):
            code=main(['tts','--text','test','--output','out.wav','--json'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(stdout.getvalue()),{'output_path':'out.wav'})
        self.assertIn('Loading upstream model',stderr.getvalue())

    def test_invalid_path_and_payload_types_fail_without_runtime_loading(self):
        with self.assertRaises(InvalidOptionError):
            EdgeTTS().synthesize('test',None)
        with self.assertRaises(InvalidOptionError):
            Qwen3TTS().synthesize('test',self.root/'out.wav',reference_audio=None)
        with self.assertRaises(InvalidOptionError):
            validate_tts_payload({'engine':[],'text':'test','output_path':'out.wav'})
        self.assertEqual(self.loads,[])

    def test_public_tts_imports_do_not_load_optional_runtimes(self):
        code='import sys; from vocal_tools import TTS, EdgeTTS, Qwen3TTS, run_tts_payload; bad=set(sys.modules)&{"torch","edge_tts","qwen_tts","numpy","soundfile","transformers"}; print(sorted(bad)); raise SystemExit(bool(bad))'
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
