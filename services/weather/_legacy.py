"""Wave-4 transition holdover — kept only to make the git history of commit
c3 reviewable. Scheduled for deletion in commit c4 of this wave.

All real implementations now live in sibling submodules
(``models``, ``client``, ``cache``, ``service``, ``adjustment``,
``merge``, ``singletons``). This file deliberately imports nothing; it
exists solely so that ``git log --follow services/weather/_legacy.py``
tells the decomposition story step-by-step.
"""
