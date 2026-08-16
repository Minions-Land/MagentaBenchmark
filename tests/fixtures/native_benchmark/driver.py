import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args()

    public = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    answer = output_dir / "answer.json"
    answer.write_text(
        json.dumps(
            {
                "attempt_id": args.attempt_id,
                "case_id": args.case_id,
                "score": public["score"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    trace = output_dir / "trace.jsonl"
    trace.write_text(
        json.dumps({"event": "fixture_complete", "case_id": args.case_id}) + "\n",
        encoding="utf-8",
    )
    result = {
        "schema_version": "magentabench.native-result.v1",
        "case_id": args.case_id,
        "verifier": "native-fixture.v1",
        "metrics": {
            "native_score": public["score"],
            "secondary_exact": 1.0,
            "secondary_error": 0.25,
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "model_calls": 0,
            "tool_calls": 0,
        },
        "artifacts": ["answer.json"],
        "trace": "trace.jsonl",
        "model_activation": None,
    }
    (output_dir / "result.json").write_text(
        json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
