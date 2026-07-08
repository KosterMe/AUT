"""Speech to text.

`transcribe` is the entry point; which engine runs behind it is a
configuration choice, not a call-site choice.
"""
from app.adapters.asr.service import transcribe, transcribe_segment

__all__ = ["transcribe", "transcribe_segment"]
