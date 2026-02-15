"""Voice reference sample manager.

Copied from coqui-tts-server/src/voice_manager.py with tts_engine import removed.
The gateway doesn't run XTTS v2 — Chatterbox handles its own model state.
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
import wave
from datetime import datetime, timezone

import aiofiles

from .models import VoiceInfo, VoiceUploadResponse

logger = logging.getLogger(__name__)


def validate_voice_id(voice_id: str, library_dir: str) -> str:
    """Validate voice_id is safe and resolve to its directory path.

    Prevents path traversal attacks by rejecting dangerous characters
    and verifying the resolved path stays within library_dir.
    """
    if not voice_id or ".." in voice_id or "/" in voice_id or "\\" in voice_id or "\x00" in voice_id:
        raise ValueError(f"Invalid voice_id: {voice_id!r}")

    voice_dir = os.path.join(library_dir, voice_id)
    resolved = os.path.abspath(voice_dir)

    if not resolved.startswith(os.path.abspath(library_dir) + os.sep):
        raise ValueError(f"Invalid voice_id: {voice_id!r}")

    return resolved


class VoiceManager:
    """Manages voice reference samples on the filesystem.

    Voice layout (multi-reference):
        /data/voices/{voice_id}/references/ref_001.wav, ref_002.wav, ...
        /data/voices/{voice_id}/metadata.json

    Legacy layout (auto-migrated):
        /data/voices/{voice_id}/reference.wav
    """

    def __init__(self, library_dir: str) -> None:
        self.library_dir = library_dir
        os.makedirs(self.library_dir, exist_ok=True)

    @staticmethod
    def _migrate_legacy_voice(voice_dir: str) -> bool:
        """Auto-migrate legacy single-reference layout to multi-reference."""
        legacy_path = os.path.join(voice_dir, "reference.wav")
        refs_dir = os.path.join(voice_dir, "references")

        if os.path.exists(legacy_path) and not os.path.exists(refs_dir):
            os.makedirs(refs_dir, exist_ok=True)
            new_path = os.path.join(refs_dir, "ref_001.wav")
            shutil.move(legacy_path, new_path)
            logger.info("Migrated legacy voice: %s", voice_dir)
            return True
        return False

    def _get_reference_files(self, voice_dir: str) -> list[str]:
        """Get sorted list of reference WAV filenames in a voice directory."""
        refs_dir = os.path.join(voice_dir, "references")
        if not os.path.exists(refs_dir):
            return []
        return sorted(f for f in os.listdir(refs_dir) if f.lower().endswith(".wav"))

    def get_reference_paths(self, voice_id: str) -> list[str]:
        """Get absolute paths to all reference WAVs for a voice."""
        try:
            voice_dir = validate_voice_id(voice_id, self.library_dir)
        except ValueError:
            return []

        if not os.path.exists(voice_dir):
            return []

        self._migrate_legacy_voice(voice_dir)

        refs_dir = os.path.join(voice_dir, "references")
        files = self._get_reference_files(voice_dir)
        return [os.path.join(refs_dir, f) for f in files]

    async def list_voices(self) -> list[VoiceInfo]:
        """List all voices in the library."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_voices_sync)

    def _list_voices_sync(self) -> list[VoiceInfo]:
        """Synchronous voice listing."""
        voices = []
        if not os.path.exists(self.library_dir):
            return voices

        for voice_id in sorted(os.listdir(self.library_dir)):
            voice_dir = os.path.join(self.library_dir, voice_id)
            if not os.path.isdir(voice_dir):
                continue

            meta_path = os.path.join(voice_dir, "metadata.json")
            if not os.path.exists(meta_path):
                continue

            self._migrate_legacy_voice(voice_dir)

            with open(meta_path) as f:
                meta = json.load(f)

            ref_files = self._get_reference_files(voice_dir)

            voices.append(
                VoiceInfo(
                    voice_id=meta["voice_id"],
                    name=meta["name"],
                    created_at=meta["created_at"],
                    references_count=len(ref_files),
                    references=ref_files,
                )
            )

        return voices

    def get_voice(self, voice_id: str) -> VoiceInfo | None:
        """Get a single voice by ID."""
        try:
            voice_dir = validate_voice_id(voice_id, self.library_dir)
        except ValueError:
            return None

        meta_path = os.path.join(voice_dir, "metadata.json")
        if not os.path.exists(meta_path):
            return None

        self._migrate_legacy_voice(voice_dir)

        with open(meta_path) as f:
            meta = json.load(f)

        ref_files = self._get_reference_files(voice_dir)

        return VoiceInfo(
            voice_id=meta["voice_id"],
            name=meta["name"],
            created_at=meta["created_at"],
            references_count=len(ref_files),
            references=ref_files,
        )

    async def upload_voice(
        self, name: str, audio_data_list: list[bytes]
    ) -> VoiceUploadResponse:
        """Save voice reference samples and return metadata."""
        voice_id = str(uuid.uuid4())
        voice_dir = os.path.join(self.library_dir, voice_id)
        refs_dir = os.path.join(voice_dir, "references")
        os.makedirs(refs_dir, exist_ok=True)

        try:
            ref_files = []
            total_duration = 0.0

            for idx, audio_data in enumerate(audio_data_list, start=1):
                filename = f"ref_{idx:03d}.wav"
                wav_path = os.path.join(refs_dir, filename)

                async with aiofiles.open(wav_path, "wb") as f:
                    await f.write(audio_data)

                loop = asyncio.get_event_loop()
                duration = await loop.run_in_executor(
                    None, self._get_wav_duration, wav_path
                )
                total_duration += duration
                ref_files.append(filename)

            meta = {
                "voice_id": voice_id,
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": total_duration,
            }
            meta_path = os.path.join(voice_dir, "metadata.json")
            async with aiofiles.open(meta_path, "w") as f:
                await f.write(json.dumps(meta, indent=2))
        except Exception:
            shutil.rmtree(voice_dir, ignore_errors=True)
            raise

        logger.info(
            "Uploaded voice '%s' (%s), %d refs, %.1fs total",
            name, voice_id, len(ref_files), total_duration,
        )

        return VoiceUploadResponse(
            voice_id=voice_id,
            name=name,
            references_count=len(ref_files),
            references=ref_files,
        )

    async def add_references(
        self, voice_id: str, audio_data_list: list[bytes]
    ) -> VoiceUploadResponse:
        """Add additional reference WAVs to an existing voice."""
        try:
            voice_dir = validate_voice_id(voice_id, self.library_dir)
        except ValueError as e:
            raise FileNotFoundError(f"Voice not found: {voice_id}") from e

        if not os.path.exists(voice_dir):
            raise FileNotFoundError(f"Voice not found: {voice_id}")

        self._migrate_legacy_voice(voice_dir)

        refs_dir = os.path.join(voice_dir, "references")
        os.makedirs(refs_dir, exist_ok=True)

        existing = self._get_reference_files(voice_dir)
        next_idx = len(existing) + 1

        for idx, audio_data in enumerate(audio_data_list, start=next_idx):
            filename = f"ref_{idx:03d}.wav"
            wav_path = os.path.join(refs_dir, filename)
            async with aiofiles.open(wav_path, "wb") as f:
                await f.write(audio_data)

        # No tts_engine cache invalidation needed — Chatterbox manages its own state

        meta_path = os.path.join(voice_dir, "metadata.json")
        with open(meta_path) as f:
            meta = json.load(f)

        all_refs = self._get_reference_files(voice_dir)

        logger.info(
            "Added %d refs to voice '%s' (%s), total %d",
            len(audio_data_list), meta["name"], voice_id, len(all_refs),
        )

        return VoiceUploadResponse(
            voice_id=voice_id,
            name=meta["name"],
            references_count=len(all_refs),
            references=all_refs,
        )

    def delete_voice(self, voice_id: str) -> bool:
        """Delete a voice and its files."""
        try:
            voice_dir = validate_voice_id(voice_id, self.library_dir)
        except ValueError:
            return False

        if not os.path.exists(voice_dir):
            return False

        # No tts_engine cache invalidation needed — Chatterbox manages its own state

        shutil.rmtree(voice_dir)
        logger.info("Deleted voice: %s", voice_id)
        return True

    @staticmethod
    def _get_wav_duration(wav_path: str) -> float:
        """Get duration of a WAV file in seconds."""
        try:
            with wave.open(wav_path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return frames / rate if rate > 0 else 0.0
        except wave.Error:
            return 0.0
