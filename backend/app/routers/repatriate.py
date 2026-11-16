from fastapi import APIRouter

from app.services import repatriate as svc

router = APIRouter(prefix="/repatriate", tags=["归位任务"])


@router.get("/scan")
def scan():
    return svc.scan()


@router.post("/run")
def run():
    return svc.run()


@router.get("/runs")
def runs():
    return svc.list_runs()


@router.get("/runs/{run_id}")
def run_detail(run_id: int):
    return svc.get_run(run_id)


@router.get("/escrow")
def escrow():
    return svc.list_escrow()


@router.get("/quarantine")
def quarantine():
    return svc.list_quarantine()


@router.get("/snapshot")
def snapshot():
    return svc.snapshot()
