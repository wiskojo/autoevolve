import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ranker import score_candidate


def main():
    cases_path = ROOT / "data" / "cases.json"
    cases = json.loads(cases_path.read_text())

    total_error = 0.0
    for item in cases:
        score = score_candidate(item["features"])
        if not 0 <= score <= 1:
            raise SystemExit(f"{item['name']} score must stay within [0, 1]")
        total_error += abs(score - item["target"])

    mean_absolute_error = total_error / len(cases)
    benchmark_score = round(1 - mean_absolute_error, 3)
    print("validation ok")
    print(f"benchmark_score={benchmark_score}")


if __name__ == "__main__":
    main()
