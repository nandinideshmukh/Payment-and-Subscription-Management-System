import logging
from fastapi import APIRouter, Request, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scheduler", tags=["Scheduler"])


@router.get("/status")
async def get_scheduler_status(request: Request):
    """Get scheduler status"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler:
        return {
            "success": False,
            "message": "Scheduler not initialized",
            "running": False
        }
    
    return {
        "success": True,
        "running": scheduler.is_running,
        "enabled": scheduler.enabled
    }


@router.get("/jobs")
async def get_scheduler_jobs(request: Request):
    """Get all scheduled jobs"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler or not scheduler.is_running:
        return {
            "success": False,
            "message": "Scheduler not running",
            "jobs": []
        }
    
    jobs = scheduler.get_jobs()
    job_list = []
    
    for job in jobs:
        job_list.append({
            "id": job.id,
            "name": job.name,
            "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger)
        })
    
    return {
        "success": True,
        "jobs": job_list,
        "count": len(job_list)
    }


@router.post("/trigger/renewals")
async def trigger_renewals(request: Request):
    """Manually trigger renewal processing"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    
    try:
        result = scheduler.trigger_renewals()
        return {
            "success": True,
            "message": "Renewal processing triggered",
            "result": result
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Manual trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trigger/expiry")
async def trigger_expiry(request: Request):
    """Manually trigger expiry check"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    
    try:
        result = scheduler.trigger_expiry_check()
        return {
            "success": True,
            "message": "Expiry check triggered",
            "result": result
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Manual trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trigger/reminders")
async def trigger_reminders(request: Request):
    """Manually trigger expiry reminders"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    
    try:
        result = scheduler.trigger_reminders()
        return {
            "success": True,
            "message": "Reminders triggered",
            "result": result
        }
    except Exception as e:
        logger.error(f"[SCHEDULER] Manual trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/pause")
async def pause_scheduler(request: Request):
    """Pause the scheduler"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler or not scheduler.is_running:
        raise HTTPException(status_code=500, detail="Scheduler not running")
    
    scheduler.scheduler.pause()
    return {
        "success": True,
        "message": "Scheduler paused"
    }


@router.post("/resume")
async def resume_scheduler(request: Request):
    """Resume the scheduler"""
    scheduler = getattr(request.app.state, "scheduler", None)
    
    if not scheduler:
        raise HTTPException(status_code=500, detail="Scheduler not initialized")
    
    scheduler.scheduler.resume()
    return {
        "success": True,
        "message": "Scheduler resumed"
    }