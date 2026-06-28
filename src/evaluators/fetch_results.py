"""
Fetch results from an ALREADY-COMPLETED Foundry evaluation run.

Use this when a cloud evaluation finished successfully in Azure AI Foundry but
you no longer have (or never got) a usable local results file — e.g. the run
completed but the original script printed "No scores returned", or you deleted
evaluation_results.txt.

This does NOT start a new evaluation and spends NO judge-model tokens. It only
reads back the scores Foundry already computed for the given eval/run IDs.

Usage:
    # Fetch the known-good baseline run (defaults below)
    python src/evaluators/fetch_results.py

    # Fetch a specific run
    python src/evaluators/fetch_results.py \
        --eval-id eval_xxxxxxxx --run-id evalrun_xxxxxxxx

    # Inspect the raw shape of the first item (debugging the API response)
    python src/evaluators/fetch_results.py --debug
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT")

# The confirmed-complete baseline run (89 items, 0 errored). These come from the
# evaluation_results.txt that was committed before it was deleted, so they are a
# safe default to fall back on.
DEFAULT_EVAL_ID = "eval_7c4c645fbe2c4e9e83d2c1b3af300106"
DEFAULT_RUN_ID = "evalrun_97c6ed6859324fc0bb01dce5df3ecb75"

RESULTS_FILE = Path("evaluation_results.txt")

# Metrics we expect, in display order, with aligned labels.
METRIC_LABELS = {
    "intent_resolution": "Intent Resolution",
    "relevance":         "Relevance        ",
    "groundedness":      "Groundedness     ",
}

PASS_THRESHOLD = 3  # scores are on a 1-5 scale; >= 3 is a pass


# ---------------------------------------------------------------------------
# Robust score extraction
# ---------------------------------------------------------------------------

def _as_dict(obj):
    """Best-effort convert an SDK model (pydantic / dataclass / plain) to a dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _first_number(d, *keys):
    """Return the first key in `d` whose value is a number, else None."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def extract_scores(output_items):
    """
    Pull per-metric scores out of the run's output items.

    The Evals API returns each scored item with a list of per-criterion results.
    Depending on SDK version that list lives under `results` (most common) or
    `evaluator_outputs`, and each entry may key its number as `score` or `value`.
    We handle all of those so this keeps working across versions — that
    flexibility is exactly what the original script was missing.
    """
    scores = {key: [] for key in METRIC_LABELS}

    for item in output_items:
        data = _as_dict(item)
        results = data.get("results") or data.get("evaluator_outputs") or []
        for r in results:
            r = _as_dict(r)
            name = r.get("name") or r.get("metric") or r.get("evaluator_name")
            if not name:
                continue
            # normalise "builtin.intent_resolution" -> "intent_resolution"
            name = str(name).split(".")[-1]
            if name not in scores:
                continue
            score = _first_number(r, "score", "value")
            if score is not None:
                scores[name].append(score)

    return scores


def status_of(item):
    data = _as_dict(item)
    return data.get("status")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval-id", default=os.environ.get("EVAL_ID", DEFAULT_EVAL_ID),
                        help=f"Evaluation ID (default: {DEFAULT_EVAL_ID})")
    parser.add_argument("--run-id", default=os.environ.get("RUN_ID", DEFAULT_RUN_ID),
                        help=f"Run ID (default: {DEFAULT_RUN_ID})")
    parser.add_argument("--debug", action="store_true",
                        help="Print the raw dict of the first output item and exit.")
    parser.add_argument("--no-write", action="store_true",
                        help="Print results but do not write evaluation_results.txt.")
    args = parser.parse_args()

    if not endpoint:
        print("ERROR: AZURE_AI_PROJECT_ENDPOINT is not set. Add it to your .env file.")
        sys.exit(1)

    project_client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    client = project_client.get_openai_client()

    print(f"Fetching results for:")
    print(f"  Eval ID: {args.eval_id}")
    print(f"  Run ID : {args.run_id}\n")

    # Confirm the run is actually complete before reading items.
    run = client.evals.runs.retrieve(run_id=args.run_id, eval_id=args.eval_id)
    print(f"Run status: {run.status}")
    if run.status != "completed":
        print(f"  This run is '{run.status}', not 'completed'. Scores may be partial or absent.")

    output_items = list(
        client.evals.runs.output_items.list(run_id=args.run_id, eval_id=args.eval_id)
    )

    if args.debug:
        if not output_items:
            print("\nNo output items returned for this run.")
            return
        print("\n--- Raw shape of first output item ---")
        print(json.dumps(_as_dict(output_items[0]), indent=2, default=str))
        return

    errored = [i for i in output_items if status_of(i) == "error"]
    scored = [i for i in output_items if status_of(i) != "error"]
    scores = extract_scores(scored)

    lines = [
        "=" * 80,
        " Trail Guide Agent - Evaluation Results",
        "=" * 80,
        f"\n  Eval ID      : {args.eval_id}",
        f"  Run ID       : {args.run_id}",
        f"  Total items  : {len(output_items)}",
        f"  Errored items: {len(errored)}",
        f"  Scored items : {len(scored)}",
        "\nAverage Scores (1-5 scale, threshold: 3)",
    ]

    pass_lines = ["\nPass Rates (score >= 3)"]
    any_scores = False
    for key, label in METRIC_LABELS.items():
        values = scores[key]
        if values:
            any_scores = True
            avg = sum(values) / len(values)
            rate = sum(1 for v in values if v >= PASS_THRESHOLD) / len(values) * 100
            lines.append(f"  {label}: {avg:.2f} (n={len(values)})")
            pass_lines.append(f"  {label}: {rate:.1f}%")

    if not any_scores:
        lines.append("  No scores parsed — re-run with --debug to inspect the item shape.")
        pass_lines.append("  No scores parsed.")

    lines.extend(pass_lines)
    summary = "\n".join(lines)
    print("\n" + summary)

    if not args.no_write:
        RESULTS_FILE.write_text(summary, encoding="utf-8")
        print(f"\n  Results saved to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
