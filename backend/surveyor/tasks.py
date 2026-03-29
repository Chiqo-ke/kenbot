from __future__ import annotations

import asyncio
import logging

from celery import shared_task
from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Primary survey task
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    name="surveyor.tasks.survey_service",
    max_retries=2,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    acks_late=True,
)
def survey_service(
    self,
    service_id: str,
    service_name: str,
    start_url: str,
) -> str:
    """
    Trigger a full portal exploration for *service_id*.

    Invokes the LangGraph Surveyor workflow synchronously inside the Celery
    worker process. Returns the final status string.
    """
    from surveyor.agent import SurveyState, build_surveyor_graph
    from surveyor.models import SurveyJob

    logger.info(
        "survey_service: starting job for service_id=%s url=%s",
        service_id,
        start_url,
    )

    # Create a tracking record so admin views can monitor progress.
    job, _ = SurveyJob.objects.update_or_create(
        service_id=service_id,
        celery_task_id=self.request.id,
        defaults={
            "service_name": service_name,
            "start_url": start_url,
            "status": SurveyJob.Status.RUNNING,
        },
    )

    initial_state: SurveyState = {
        "service_id": service_id,
        "service_name": service_name,
        "start_url": start_url,
        "raw_exploration": None,
        "service_map": None,
        "validation_issues": [],
        "status": "exploring",
        "healing_target": None,
        "attempt": 0,
    }

    graph = build_surveyor_graph()

    try:
        # LangGraph async graphs must be awaited; run inside a new event loop.
        final_state = asyncio.run(graph.ainvoke(initial_state))
        terminal_status: str = final_state.get("status", "failed")

        issues = final_state.get("validation_issues", [])
        job.status = (
            SurveyJob.Status.COMPLETE
            if terminal_status == "complete"
            else SurveyJob.Status.FAILED
        )
        job.validation_issues = issues
        job.save(update_fields=["status", "validation_issues"])

        logger.info(
            "survey_service: job=%s finished with status=%s issues=%s",
            self.request.id,
            terminal_status,
            issues,
        )
        return terminal_status

    except Exception as exc:
        job.status = SurveyJob.Status.FAILED
        job.validation_issues = [str(exc)]
        job.save(update_fields=["status", "validation_issues"])
        logger.exception(
            "survey_service: job=%s failed: %s", self.request.id, exc
        )
        raise


# ---------------------------------------------------------------------------
# Healing task  (triggered by Pilot when a step selector breaks)
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    name="surveyor.tasks.heal_step",
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
)
def heal_step(
    self,
    service_id: str,
    step_id: str,
    failed_selector: str,
) -> str:
    """
    Re-run the Surveyor for a specific step whose selector has broken.

    Looks up the existing ServiceMap so the agent knows which URL and context
    to target, then invokes a focused re-exploration graph run.
    """
    from maps.repository import MapRepository
    from surveyor.agent import SurveyState, build_surveyor_graph
    from surveyor.models import SurveyJob

    logger.info(
        "heal_step: service_id=%s step_id=%s failed_selector=%r",
        service_id,
        step_id,
        failed_selector,
    )

    repo = MapRepository()
    existing_map = repo.get_map(service_id)
    if existing_map is None:
        logger.error(
            "heal_step: no existing map found for service_id=%s", service_id
        )
        return "failed"

    # Find the target step to get its URL.
    target_step = next(
        (s for s in existing_map.workflow if s.step_id == step_id), None
    )
    start_url = (
        target_step.url_match if target_step else existing_map.workflow[0].url_match
    )

    job, _ = SurveyJob.objects.update_or_create(
        service_id=f"{service_id}__heal__{step_id}",
        celery_task_id=self.request.id,
        defaults={
            "service_name": existing_map.service_name,
            "start_url": start_url,
            "status": SurveyJob.Status.RUNNING,
        },
    )

    initial_state: SurveyState = {
        "service_id": service_id,
        "service_name": existing_map.service_name,
        "start_url": start_url,
        "raw_exploration": None,
        "service_map": None,
        "validation_issues": [],
        "status": "exploring",
        "healing_target": step_id,
        "attempt": 0,
    }

    graph = build_surveyor_graph()

    try:
        final_state = asyncio.run(graph.ainvoke(initial_state))
        terminal_status: str = final_state.get("status", "failed")

        issues = final_state.get("validation_issues", [])
        job.status = (
            SurveyJob.Status.COMPLETE
            if terminal_status == "complete"
            else SurveyJob.Status.FAILED
        )
        job.validation_issues = issues
        job.save(update_fields=["status", "validation_issues"])

        logger.info(
            "heal_step: job=%s finished status=%s", self.request.id, terminal_status
        )
        return terminal_status

    except Exception as exc:
        job.status = SurveyJob.Status.FAILED
        job.validation_issues = [str(exc)]
        job.save(update_fields=["status", "validation_issues"])
        logger.exception(
            "heal_step: job=%s failed: %s", self.request.id, exc
        )
        raise
