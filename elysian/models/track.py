"""The Track record."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Track:
    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    length: float = 0.0
    scanned: bool = field(default=False, compare=False)

    def __post_init__(self):
        if not self.title:
            self.title = Path(self.path).stem

    @property
    def display(self) -> str:
        return f"{self.title} - {self.artist}" if self.artist else self.title

    @property
    def duration_text(self) -> str:
        return format_time(self.length)


def format_time(seconds: float) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
