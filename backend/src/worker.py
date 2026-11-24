import json
import logging
import time
from datetime import timedelta
from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy import or_, select

from . import blind
from .calculation import estimate
from .db import QUEUE, Session, redis, utcnow
from .ledger import checksum, inside, record
from .models import (
    BatchState, BlindState, BlindValidation, BlindWitness, CalibrationRelease,
    ChamberRun, DryBatch, ReleaseState, WitnessState,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
HEARTBEAT = Path('/tmp/worker-heartbeat')
BLIND_PREFIX = blind.BLIND_PREFIX


def claim(wakeup_id):
    with Session.begin() as db:
        eligible = or_(CalibrationRelease.state == ReleaseState.QUEUED,
                       (CalibrationRelease.state == ReleaseState.RUNNING) &
                       (CalibrationRelease.touched_at < utcnow() - timedelta(minutes=5)))
        query = select(CalibrationRelease).where(eligible)
        if wakeup_id is not None:
            query = query.where(CalibrationRelease.id == wakeup_id)
        release = db.scalar(query.order_by(CalibrationRelease.id).limit(1).with_for_update(skip_locked=True))
        if release is None:
            return None
        release.state = ReleaseState.RUNNING
        release.attempts += 1
        release.touched_at = utcnow()
        return release.id, release.attempts, release.snapshot, release.input_hash


def settle(job, result, error):
    release_id, attempt, snapshot, digest = job
    with Session.begin() as db:
        batch = db.scalar(select(DryBatch).where(DryBatch.code == snapshot['batch']).with_for_update())
        entry = db.get(CalibrationRelease, release_id, with_for_update=True)
        if entry.attempts != attempt or entry.state != ReleaseState.RUNNING:
            return
        entry.touched_at = utcnow()
        if error is not None:
            entry.state = ReleaseState.FAILED
            entry.error = error[:240]
            record(db, 'worker', 'fit.failed', entry.error, batch.code)
            return
        evidence = Path(f'/evidence/fit-{release_id}.json')
        temp = evidence.with_suffix(f'.{attempt}.tmp')
        try:
            payload = {'input_hash': digest, 'snapshot': snapshot, 'result': result}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(evidence)
        finally:
            temp.unlink(missing_ok=True)
        entry.result = result
        entry.prediction_pct = result['prediction_pct']
        within_window = inside(entry.prediction_pct, snapshot['window'])
        entry.state = ReleaseState.AWAITING if within_window else ReleaseState.RETEST
        batch.state = BatchState.REVIEW
        run = db.get(ChamberRun, entry.run_id)
        record(db, 'worker', 'fit.completed' if within_window else 'retest.opened',
               f'{run.source_name} · {entry.prediction_pct:.3f}% · 工艺窗 {snapshot["window"]}', batch.code)
        log.info('calibration %s %s', release_id, entry.state.value)


def process(job):
    _, _, snapshot, digest = job
    try:
        if checksum(snapshot) != digest:
            raise ValueError('试算输入快照校验失败')
        result = estimate(snapshot)
    except Exception as exc:
        log.exception('calibration fit failed')
        settle(job, None, str(exc))
    else:
        try:
            settle(job, result, None)
        except OSError:
            log.exception('calibration evidence could not be written')
            settle(job, None, '证据卷暂不可写，可按原快照重试')


def claim_blind(wakeup_id):
    with Session.begin() as db:
        eligible = or_(BlindWitness.job_state == WitnessState.QUEUED,
                       (BlindWitness.job_state == WitnessState.RUNNING) &
                       (BlindWitness.touched_at < utcnow() - timedelta(minutes=5)))
        query = select(BlindWitness).where(eligible)
        if wakeup_id is not None:
            query = query.where(BlindWitness.id == wakeup_id)
        witness = db.scalar(query.order_by(BlindWitness.id).limit(1).with_for_update(skip_locked=True))
        if witness is None:
            return None
        validation = db.get(BlindValidation, witness.blind_id)
        if validation.state != BlindState.SEALED:
            witness.job_state = WitnessState.FAILED
            witness.error = '盲验已作废或结束，未完成的预测不再结算'
            witness.touched_at = utcnow()
            return None
        witness.job_state = WitnessState.RUNNING
        witness.attempts += 1
        witness.touched_at = utcnow()
        return witness.id, witness.attempts, witness.snapshot, witness.input_hash


def settle_blind(job, result, error):
    witness_id, attempt, snapshot, digest = job
    with Session.begin() as db:
        witness = db.get(BlindWitness, witness_id, with_for_update=True)
        if witness.attempts != attempt or witness.job_state != WitnessState.RUNNING:
            return
        validation = db.get(BlindValidation, witness.blind_id)
        witness.touched_at = utcnow()
        if validation.state != BlindState.SEALED:
            witness.job_state = WitnessState.FAILED
            witness.error = '盲验已作废或结束，未完成的预测不再结算'
            return
        if error is not None:
            witness.job_state = WitnessState.FAILED
            witness.error = error[:240]
            record(db, 'worker', 'blind.fit.failed', witness.error, witness.batch_code)
            return
        evidence = Path(f'/evidence/blind-{witness_id}.json')
        temp = evidence.with_suffix(f'.{attempt}.tmp')
        try:
            payload = {'input_hash': digest, 'snapshot': snapshot, 'result': result}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            temp.replace(evidence)
        finally:
            temp.unlink(missing_ok=True)
        witness.result = result
        witness.predicted_at = utcnow()
        witness.job_state = WitnessState.PREDICTED
        witness.error = None
        run = db.get(ChamberRun, witness.run_id)
        record(db, 'worker', 'blind.predicted',
               f'{run.source_name} · 预测已锁定 {result["prediction_pct"]:.3f}%（实测登记前封存）',
               witness.batch_code)
        log.info('blind witness %s predicted', witness_id)


def process_blind(job):
    _, _, snapshot, digest = job
    try:
        if checksum(snapshot) != digest:
            raise ValueError('盲验输入快照校验失败')
        result = estimate(snapshot)
    except Exception as exc:
        log.exception('blind fit failed')
        settle_blind(job, None, str(exc))
    else:
        try:
            settle_blind(job, result, None)
        except OSError:
            log.exception('blind evidence could not be written')
            settle_blind(job, None, '证据卷暂不可写，可重新锁定预测')


def main():
    while True:
        try:
            try:
                entry = redis.blpop(QUEUE, timeout=2)
            except RedisError:
                log.warning('Redis unavailable; checking committed queues')
                time.sleep(2)
                entry = None
            job = None
            if entry:
                raw = entry[1].decode() if isinstance(entry[1], bytes) else entry[1]
                if raw.startswith(BLIND_PREFIX):
                    job = ('blind', claim_blind(int(raw[len(BLIND_PREFIX):])))
                else:
                    job = ('fit', claim(int(raw)))
            if job is None or job[1] is None:
                blind_job = claim_blind(None)
                job = ('blind', blind_job) if blind_job else ('fit', claim(None))
            if job and job[1] is not None:
                process_blind(job[1]) if job[0] == 'blind' else process(job[1])
            HEARTBEAT.touch()
        except Exception:
            log.exception('worker iteration failed')
            time.sleep(3)


if __name__ == '__main__':
    main()
