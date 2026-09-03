import json
from pathlib import Path

from agents.evaluation.benchmarks import exact_match, extract_final_answer, load_gaia, load_hle


def test_final_answer_extraction_prefers_marker() -> None:
    text = "Reasoning\nFINAL ANSWER:  Time-Parking 2: Parallel Universe  \n"
    assert extract_final_answer(text) == "Time-Parking 2: Parallel Universe"


def test_numeric_exact_match_normalizes_commas_and_period() -> None:
    assert exact_match("1,024.", "1024")


def test_benchmark_adapters_load_minimal_datasets(tmp_path: Path) -> None:
    gaia_path = tmp_path / "GAIA" / "all.json"
    gaia_path.parent.mkdir()
    gaia_path.write_text(
        json.dumps([{"task_id": "g1", "Question": "Q?", "answer": "A", "problem_type": "text"}]),
        encoding="utf-8",
    )
    hle_path = tmp_path / "HLE" / "all.json"
    hle_path.parent.mkdir()
    hle_path.write_text(
        json.dumps([{"id": "h1", "question": "Q?", "answer": "A", "problem_type": "text"}]),
        encoding="utf-8",
    )
    gaia = load_gaia(gaia_path)
    hle = load_hle(hle_path)
    assert len(gaia) == 1
    assert len(hle) == 1
    assert gaia[0].answer
    assert hle[0].answer


def test_checked_local_benchmarks_expose_supported_and_multimodal_counts() -> None:
    root = Path(__file__).resolve().parents[1]
    gaia_path = root / "data" / "GAIA" / "all.json"
    hle_path = root / "data" / "HLE" / "all_500.json"
    if not gaia_path.exists() or not hle_path.exists():
        return
    gaia = load_gaia(gaia_path)
    hle = load_hle(hle_path)
    assert sum(case.problem_type == "mm" for case in gaia) == 24
    assert sum(case.problem_type == "mm" for case in hle) == 113
    assert sum(case.problem_type != "mm" for case in [*gaia, *hle]) == 528
