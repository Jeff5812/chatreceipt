"""Versions recorded with every extraction run, so results can be traced and compared.

Bump a prompt version whenever its instructions or schema change in a way that could change
answers; bump ENGINE_VERSION when the deterministic rules change.
"""

ENGINE_VERSION = "2026.11.19-1"
TRANSCRIPTION_PROMPT_VERSION = "transcribe-v1"
INTERPRETATION_PROMPT_VERSION = "interpret-v1"
INTERPRETATION_SCHEMA_VERSION = "interpretation-schema-v1"
