"""Grounded technical assessment of a repository that is not a capsule project.

The capsule-less half of Research OS's reasoning. A project with a
``.research/`` capsule reasons about Questions, Hypotheses, Claims and Evidence
through :mod:`research_os.proposal`; a project without one reasons about files,
symbols, deterministic checks and retrieved literature through this package.

Which of the two applies is decided by the controller from a
:class:`~research_os.automation.profile.ProjectProfile`, never by a model and
never by a flag. Nothing here is science, nothing here is promotable, and
nothing here is ever written into a project.
"""

from research_os.assessment.controller import AssessmentController, AssessmentOutcome
from research_os.assessment.models import TechnicalAssessment
from research_os.assessment.store import AssessmentStore

__all__ = [
    "AssessmentController",
    "AssessmentOutcome",
    "AssessmentStore",
    "TechnicalAssessment",
]
