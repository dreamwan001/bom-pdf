"""메모 포함 PDF 내보내기 결과 상태 — 작업자(pdf.py)와 UI 가 함께 쓴다. PyMuPDF 를 import하지 않는다."""

EXPORT_OK = "ok"
EXPORT_CHANGED = "changed"  # 원본이 연 뒤에 바뀜(sha256 다름) — 내보내지 않음
EXPORT_REFUSED = "refused"  # 산출물 경로가 원본과 같음
EXPORT_ERROR = "error"
EXPORT_INTERRUPTED = "interrupted"  # 내보내던 중 작업자 급사 (클라이언트가 만든다)

# 1-4 페이지 편집 저장·용량 압축본(작업 프로세스)도 같은 상태를 쓴다
JOB_CANCELLED = "cancelled"  # 사용자가 취소 — 임시 파일은 클라이언트가 지운다
JOB_NOT_SMALLER = "not_smaller"  # 압축본이 원본보다 작지 않음 — 파일을 남기지 않음
TMP_SUFFIX = ".bom-pdf.tmp"  # 산출물 옆 임시 파일(검증 뒤 replace)
