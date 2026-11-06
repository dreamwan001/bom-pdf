"""PyInstaller 진입점. 패키지 main 을 부르기만 한다."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()  # 작업자 프로세스 분기 — 반드시 첫 줄

    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parent / "src"
    if src.is_dir() and str(src) not in sys.path:  # 소스 실행일 때만
        sys.path.insert(0, str(src))

    from bom_pdf.app import main

    sys.exit(main(sys.argv[1:]))
