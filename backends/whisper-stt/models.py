from pydantic import BaseModel


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


class LanguageInfo(BaseModel):
    code: str
    name: str


class LanguageDetectionResult(BaseModel):
    detected_language: str
    probability: float
    all_languages: list[LanguageInfo]
