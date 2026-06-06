"""Target agent — generation 1 (accuracy 33.3%)."""
from sia.tools import python_exec, web_search


def solve(question: str) -> str:
    """GPQA solver, generation 1.

    Strategy: direct answer.
    """
    plan = decompose(question)
    draft = reason(question, plan)
    return finalize(draft)
