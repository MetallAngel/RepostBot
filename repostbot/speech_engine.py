"""Silero's heavy imports and inference live only in this disposable CPU process."""
import contextlib
import json
import os
from pathlib import Path
import sys
import time
from .speech_voices import tts_arguments


def lower_priority():
    if hasattr(os, 'nice'):
        try:
            os.nice(15)
        except OSError:
            pass
    elif sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)


def main():
    lower_priority()
    root = Path(sys.argv[1])
    # Native libraries must see these limits before torch is imported.
    for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[key] = '1'
    os.environ['CUDA_VISIBLE_DEVICES'] = ''
    protocol = sys.stdout
    def emit(value):
        protocol.write(json.dumps(value) + '\n')
        protocol.flush()
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            import lameenc
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
            model = torch.package.PackageImporter(str(root / 'v5_3_ru.pt')).load_pickle('tts_models', 'model')
            model.to(torch.device('cpu'))
        emit({'ready': True})
    except Exception as exc:
        emit({'error': type(exc).__name__})
        return 1
    for line in sys.stdin:
        job = {}
        try:
            job = json.loads(line)
            started = time.monotonic()
            with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
                audio = model.apply_tts(**tts_arguments(job['text'], job))
                maximum = int(job['max_seconds']) * 24000
                clipped = audio.numel() > maximum
                audio = audio[:maximum]
                # Fade the last 50 ms when a long recording is cut.
                if clipped and audio.numel() >= 1200:
                    audio[-1200:] *= torch.linspace(1, 0, 1200)
                pcm = (audio.clamp(-1, 1) * 32767).to(torch.int16).numpy().tobytes()
                encoder = lameenc.Encoder()
                encoder.silence()
                encoder.set_bit_rate(64)
                encoder.set_in_sample_rate(24000)
                encoder.set_channels(1)
                encoder.set_quality(7)
                encoded = encoder.encode(pcm) + encoder.flush()
            path = Path(job['output'])
            # The parent supplies paths in this one directory only.
            if path.parent.resolve() != (root / 'audio').resolve() or path.suffix != '.mp3':
                raise ValueError('output')
            temporary = path.with_suffix('.part')
            temporary.write_bytes(encoded)
            temporary.replace(path)
            emit({'id': job['id'], 'duration': round(audio.numel() / 24000, 2), 'clipped': clipped,
                  'synthesis_ms': round((time.monotonic() - started) * 1000)})
        except Exception as exc:
            emit({'id': job.get('id'), 'error': type(exc).__name__})
    return 0


if __name__ == '__main__':
    sys.exit(main())
