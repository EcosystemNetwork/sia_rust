"""Target agent — generation 5 (accuracy 91.7%)."""
from sia.tools import python_exec, web_search


def solve(question: str) -> str:
    """GPQA solver, generation 5.

    Strategy: chain-of-thought + self-check + tool-augmented retrieval.
    """
    plan = decompose(question)
    evidence = web_search(plan.query)
    draft = reason(question, plan, evidence)
    draft = self_check(draft)  # added gen 2
    return finalize(draft)
