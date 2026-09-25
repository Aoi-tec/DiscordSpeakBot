import io
import logging
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from ..settings.models import TTSConfig

log = logging.getLogger(__name__)


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
        from faster_qwen3_tts import FasterQwen3TTS

        if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
            raise RuntimeError("指定したCUDA GPUを利用できません")
        self.model = FasterQwen3TTS.from_pretrained(
            self.config.model_path,
            device="cuda:0",
            dtype=getattr(torch, self.config.dtype),
            attn_implementation="sdpa",
            max_seq_len=1024,
            local_files_only=True,
        )
        self.model.warmup()
        voice_id = self.store.get("system").defaults.voice_id
        voice = self.store.get("voices").voices.get(voice_id) if voice_id else None
        if voice:
            try:
                self._cached_profile(voice_id, voice)
            except Exception:
                log.exception("既定Voiceの事前準備に失敗しました: %s", voice_id)

    def _cached_profile(self, voice_id, voice):
        key = (voice_id, voice.generation)
        if key not in self.profiles:
            path = managed_path(self.store.root, voice.reference_audio)
            items = self.model.model.create_voice_clone_prompt(
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
        return self.profiles[key]

    def synthesize(self, job):
        import numpy as np
        import soundfile as sf

        voice = self.store.get("voices").voices.get(job.settings.voice_id)
        if not voice or voice.generation != job.voice_generation:
            raise ValueError("Voiceが変更または削除されています")
        key = (job.settings.voice_id, voice.generation)
        started = time.perf_counter()
        registered = self.store.get("voices").voices
        self.profiles = {
            k: v
            for k, v in self.profiles.items()
            if k[0] in registered and registered[k[0]].generation == k[1]
        }
        # RAM-only cache in MVP; no unsafe pickle loading.
        self._cached_profile(job.settings.voice_id, voice)
        device_profiles = [
            replace(
                item,
                ref_code=item.ref_code.to(self.model.device) if item.ref_code is not None else None,
                ref_spk_embedding=item.ref_spk_embedding.to(self.model.device),
            )
            for item in self.profiles[key]
        ]
        profile_done = time.perf_counter()
        wavs, sample_rate = self.model.generate_voice_clone(
            text=job.text,
            language="Japanese",
            voice_clone_prompt=device_profiles,
            max_new_tokens=self.config.max_new_tokens,
        )
        inference_done = time.perf_counter()
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
        postprocess_done = time.perf_counter()
        self.last_timing = {
            "profile_ms": round((profile_done - started) * 1000, 1),
            "inference_ms": round((inference_done - profile_done) * 1000, 1),
            "postprocess_ms": round((postprocess_done - inference_done) * 1000, 1),
            "audio_seconds": round(len(pcm) / (48000 * 2 * 2), 2),
        }
        return pcm

    def unload(self):
        self.profiles.clear()
        self.model = None
