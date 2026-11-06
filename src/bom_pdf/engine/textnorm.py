"""검색어·쪽 글자 정규화 — PyMuPDF 없이 UI·작업자 양쪽에서 쓴다.

- 대소문자 무시: 글자 하나씩 lower (길이가 바뀌는 글자는 그대로 두어 원래 글자 위치와 1:1 유지)
- 기본: 공백·줄바꿈 연속은 공백 하나
- ignore_space: 공백·줄바꿈을 모두 지운다 (스캔 OCR 의 끼어든 줄바꿈·띄어쓰기 대응)
"""
from __future__ import annotations


def fold(c: str) -> str:
    low = c.lower()
    return low if len(low) == len(c) else c


def normalize_query(query: str, ignore_space: bool) -> str:
    q = "".join(fold(c) for c in query)
    if ignore_space:
        return "".join(ch for ch in q if not ch.isspace())
    return " ".join(q.split())
