"""`python -m bom_pdf [파일...]` 진입점.

렌더 작업자 프로세스(D3)가 이 모듈을 다시 불러오므로 맨 위에서 무거운 import를 하지 않는다.
"""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # 포장본(PyInstaller)에서 작업자 프로세스 분기 — 반드시 첫 줄

    import sys

    from bom_pdf.app import main

    sys.exit(main(sys.argv[1:]))
