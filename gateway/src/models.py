from datetime import datetime

from pydantic import BaseModel, Field, model_validator


# --- TTS Models ---


class SynthesizeRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str | None = None
    language: str = "en"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    split_sentences: bool = True


class StreamRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str | None = None
    language: str = "en"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    split_sentences: bool = True


class VoiceWeight(BaseModel):
    voice_id: str
    weight: float = Field(..., gt=0.0, le=1.0)


class InterpolateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voices: list[VoiceWeight] = Field(..., min_length=2, max_length=5)
    language: str = "en"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)

    @model_validator(mode="after")
    def validate_weights_sum(self):
        total = sum(v.weight for v in self.voices)
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Voice weights must sum to 1.0 (got {total:.3f})")
        return self


class LanguageInfo(BaseModel):
    code: str
    name: str


class VoiceUploadResponse(BaseModel):
    voice_id: str
    name: str
    references_count: int
    references: list[str]


class VoiceInfo(BaseModel):
    voice_id: str
    name: str
    created_at: datetime
    references_count: int
    references: list[str]


# --- STT Models ---


class TranscriptWord(BaseModel):
    word: str
    start: float
    end: float
    probability: float


class TranscriptSegment(BaseModel):
    id: int
    text: str
    start: float
    end: float
    words: list[TranscriptWord] | None = None


class TranscriptionResult(BaseModel):
    text: str
    language: str
    language_probability: float
    duration: float
    segments: list[TranscriptSegment]


class LanguageDetectionResult(BaseModel):
    detected_language: str
    probability: float
    all_languages: list[LanguageInfo]


class StreamTranscriptEvent(BaseModel):
    segment: TranscriptSegment


# --- Speaker Models ---


class DiarizationSegment(BaseModel):
    speaker: str
    start: float
    end: float


class DiarizationResult(BaseModel):
    num_speakers: int
    segments: list[DiarizationSegment]
    duration: float


class VerificationResult(BaseModel):
    is_same_speaker: bool
    similarity_score: float
    threshold: float


# --- Audio Processing Models ---


class DenoiseResponse(BaseModel):
    original_duration: float
    format: str


class ConvertResponse(BaseModel):
    output_format: str
    sample_rate: int
    duration: float
