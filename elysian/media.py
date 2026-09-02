"""Shell-side media classification helpers."""
from pathlib import Path

from . import config


def suffix(path: str) -> str:
    return Path(path).suffix.lower()


def is_audio_path(path: str) -> bool:
    return suffix(path) in config.AUDIO_EXTENSIONS


def is_video_path(path: str) -> bool:
    return suffix(path) in config.VIDEO_EXTENSIONS


def is_media_path(path: str) -> bool:
    return suffix(path) in config.MEDIA_EXTENSIONS


def media_kind_from_path(path: str) -> str:
    if is_video_path(path):
        return "video"
    if is_audio_path(path):
        return "audio"
    return "unknown"
