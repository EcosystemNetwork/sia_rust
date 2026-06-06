"""Target agent — generation 4 (accuracy 75.0%)."""
from sia.tools import python_exec, web_search


def solve(question: str) -> str:
    """GPQA solver, generation 4.

    Strategy: chain-of-thought + self-check + tool-augmented retrieval.
    """
    plan = decompose(question)
    evidence = web_search(plan.query)
    draft = reason(question, plan, evidence)
    draft = self_check(draft)  # added gen 2
    return finalize(draft)
