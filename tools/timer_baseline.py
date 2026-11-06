"""대조군: 앱 없이 빈 QWidget + 16ms 타이머만 돌려 이 PC 자체의 틱 지연(OS 잡음)을 잰다.

사용: .venv/Scripts/python.exe tools/timer_baseline.py [초=180]
measure_scroll.py 결과의 50ms 초과 틱이 앱 때문인지 가르는 기준으로 쓴다.
"""
import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0
    app = QApplication([sys.argv[0]])
    w = QWidget()
    w.resize(900, 1000)
    w.show()
    ticks: list[float] = []

    def tick() -> None:
        ticks.append(time.perf_counter())
        w.update()
        if ticks[-1] - ticks[0] > seconds:
            app.quit()

    t = QTimer()
    t.setTimerType(Qt.TimerType.PreciseTimer)
    t.timeout.connect(tick)
    t.start(16)
    app.exec()
    g = sorted(b - a for a, b in zip(ticks, ticks[1:]))
    print(json.dumps({"baseline_s": seconds, "ticks": len(ticks), "p99_ms": round(g[int(len(g) * .99)] * 1000, 1),
                      "max_ms": round(g[-1] * 1000, 1), "over_50ms": sum(x > .05 for x in g),
                      "over_33ms": sum(x > .033 for x in g)}))


if __name__ == "__main__":
    main()
