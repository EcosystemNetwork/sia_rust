"""Target agent — generation 2 (accuracy 50.0%)."""
from sia.tools import python_exec, web_search


def solve(question: str) -> str:
    """GPQA solver, generation 2.

    Strategy: chain-of-thought + self-check.
    """
    plan = decompose(question)
    draft = reason(question, plan)
    draft = self_check(draft)  # added gen 2
    return finalize(draft)
