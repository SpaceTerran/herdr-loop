"""herdr-loop plumbing: the few lines a model manager does not hold itself.

Everything here is deterministic and file-based. Judgement (which item is
well-authored, which engine builds, what a failure means) stays with the
manager; these modules only refuse what must never happen.
"""
