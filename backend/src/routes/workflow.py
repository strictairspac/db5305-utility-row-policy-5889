from fastapi import APIRouter, Depends, Header
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import (CaseForm, IntakeForm, LoginForm, ReviewForm,
                                  StorageSwitchForm)
from src.services import domain, storage, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/storage')
def storage_status(_=Depends(current_user)):
    with engine.connect() as conn:
        return storage.status(conn)


@router.post('/storage/switch')
def switch_storage(form: StorageSwitchForm, actor=Depends(operator)):
    with engine.begin() as conn:
        return storage.switch_mode(conn, form.mode, actor)


@router.post('/storage/migrate')
def migrate_storage(actor=Depends(operator)):
    with engine.begin() as conn:
        return storage.migrate(conn, actor)


@router.post('/intake', status_code=201)
def intake(form: IntakeForm, actor=Depends(operator),
           idempotency_key: str | None = Header(default=None)):
    # The explicit header wins; the body field keeps the endpoint usable from
    # plain forms and the demo UI.
    with engine.begin() as conn:
        return storage.intake(conn, form.source, idempotency_key or form.receipt_key,
                              form.title, form.payload, actor)


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/cases/{case_id}/archive')
def case_archive(case_id: int, _=Depends(current_user)):
    with engine.connect() as conn:
        result = storage.read_archive(conn, case_id)
        return {**result['case'], 'payload': result['payload'],
                'storage_version': result['storage_version'],
                'storage_mode': result['storage_mode'],
                'order_count': result['order_count'],
                'authoritative_source': result['authoritative_source'],
                'receipts': result['receipts']}


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)
