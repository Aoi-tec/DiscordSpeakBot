import io
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from ..settings.models import TTSConfig


def managed_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    voices = (root / "voices").resolve()
    if not path.is_relative_to(voices) or not path.is_file():
        raise ValueError("参照音声はdata-dir/voices内のファイルに限定されます")
    return path


class QwenEngine:
    def __init__(self, config: TTSConfig, store, max_audio_seconds: int):
        self.config, self.store = config, store
        self.max_audio_seconds = max_audio_seconds
        self.model = None
        self.profiles = {}

    def load(self):
        if not self.config.gpu_uuid:
            raise ValueError("tts.gpu_uuidを設定してください")
        if not Path(self.config.model_path).is_dir():
            raise ValueError(
                "tts.model_pathにはダウンロード済みBaseモデルのディレクトリを指定してください"
            )
        # Must be set before importing torch/CUDA. No fallback to the gaming GPU or CPU.
        os.environ["CUDA_VISIBLE_DEVICES"] = self.config.gpu_uuid
        os.environ["HF_HUB_OFFLINE"] = "1"
        import torch
        from qwen_tts import Qwen3TTSModel

        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("指定したCUDA GPUを利用できません")
        self.model = Qwen3TTSModel.from_pretrained(
            self.config.model_path,
            device_map="cuda:0",
            dtype=getattr(torch, self.config.dtype),
            attn_implementation="sdpa",
            local_files_only=True,
        )

    def synthesize(self, job):
        import numpy as np
        import soundfile as sf

        voice = self.store.get("voices").voices.get(job.settings.voice_id)
        if not voice or voice.generation != job.voice_generation:
            raise ValueError("Voiceが変更または削除されています")
        path = managed_path(self.store.root, voice.reference_audio)
        key = (job.settings.voice_id, voice.generation)
        registered = self.store.get("voices").voices
        self.profiles = {
            k: v
            for k, v in self.profiles.items()
            if k[0] in registered and registered[k[0]].generation == k[1]
        }
        if key not in self.profiles:
            # RAM-only cache in MVP; no unsafe pickle loading.
            items = self.model.create_voice_clone_prompt(
                ref_audio=str(path),
                ref_text=voice.reference_text,
                x_vector_only_mode=False,
            )
            self.profiles[key] = [
                replace(
                    item,
                    ref_code=item.ref_code.detach().cpu() if item.ref_code is not None else None,
                    ref_spk_embedding=item.ref_spk_embedding.detach().cpu(),
                )
                for item in items
            ]
        device_profiles = [
            replace(
                item,
                ref_code=item.ref_code.to(self.model.device) if item.ref_code is not None else None,
                ref_spk_embedding=item.ref_spk_embedding.to(self.model.device),
            )
            for item in self.profiles[key]
        ]
        wavs, sample_rate = self.model.generate_voice_clone(
            text=job.text,
            language="Japanese",
            voice_clone_prompt=device_profiles,
            max_new_tokens=self.config.max_new_tokens,
        )
        samples = np.asarray(wavs[0], dtype=np.float32)
        stream = io.BytesIO()
        sf.write(stream, samples, sample_rate, format="WAV", subtype="PCM_16")
        filters = [
            f"atempo={job.settings.speed_percent / 100}",
            f"volume={job.settings.volume_percent / 100}",
            "alimiter=limit=0.98:level=false",
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            [
                self.config.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-af",
                ",".join(filters),
                "-t",
                str(self.max_audio_seconds),
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=stream.getvalue(),
            capture_output=True,
            timeout=30,
            creationflags=flags,
        )
        if result.returncode:
            raise RuntimeError("FFmpeg音声変換に失敗しました")
        pcm = result.stdout
        cap = self.max_audio_seconds * 48000 * 4
        if len(pcm) >= cap:
            audio = np.frombuffer(pcm[:cap], dtype="<i2").copy().reshape(-1, 2)
            fade = min(480, len(audio))
            audio[-fade:] = (audio[-fade:] * np.linspace(1, 0, fade)[:, None]).astype("<i2")
            pcm = audio.tobytes()
        return pcm

    def unload(self):
        self.profiles.clear()
        self.model = None
