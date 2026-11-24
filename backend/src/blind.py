"""Blind validation workflow for new recipes.

The supervisor seals the frozen training references and the witness roster
before any prediction is produced. Witness predictions are locked first and
measured shrinkage is registered afterwards, always checked against each
witness's own frozen curve. Production trials open only when every witness
error stays within the tolerance (two percentage points by default).
"""
import logging

from fastapi import HTTPException
from sqlalchemy import select

from . import ledger
from .db import QUEUE, redis, utcnow
from .models import (
    BatchState, BlindState, BlindValidation, BlindWitness, ChamberRun, DryBatch,
    RecipeLot, RunState, WitnessState,
)

BLIND_PREFIX = 'blind:'


def blocked(db, actor, action, detail, code=None):
    ledger.record(db, actor, f'{action}.blocked', detail, code)
    db.commit()
    raise HTTPException(409, detail)


def active_validation(db, recipe_code):
    return db.scalar(select(BlindValidation)
                     .where(BlindValidation.recipe_code == recipe_code,
                            BlindValidation.state == BlindState.SEALED)
                     .order_by(BlindValidation.id.desc()).limit(1))


def training_candidates(db, recipe):
    return db.scalars(select(DryBatch)
                      .filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                      .order_by(DryBatch.code)).all()


def reference_snapshot(db, recipe, batch):
    run = ledger.latest_run(db, batch.code)
    if run is None or run.state != RunState.FROZEN:
        raise ValueError(f'参考批 {batch.code} 缺少已冻结曲线')
    issue = ledger.curve_issue(run.points)
    if issue:
        raise ValueError(f'参考批 {batch.code} 的曲线不完整：{issue}')
    pairs = ledger.paired_measurements(db, run)
    return {
        'batch': batch.code, 'curve_hash': run.checksum,
        'features': ledger.feature_row(batch, recipe, run),
        'observed': ledger.observed(pairs), 'pairs': pairs,
    }


def sealed_index_for(db, recipe_code):
    previous = db.scalar(select(BlindValidation).filter_by(recipe_code=recipe_code)
                         .order_by(BlindValidation.sealed_index.desc()).limit(1))
    return (previous.sealed_index + 1) if previous else 1


def seal_blind(db, payload, actor):
    recipe = db.get(RecipeLot, payload.recipe_code)
    if recipe is None:
        raise HTTPException(404, '请选用已建档的配方版本')
    # Lock the recipe row so two supervisors cannot seal concurrently.
    db.scalar(select(RecipeLot).where(RecipeLot.code == recipe.code).with_for_update())
    if active_validation(db, recipe.code) is not None:
        blocked(db, actor, 'blind.seal', '该配方已有封存中的盲验，作废后才能重开', recipe.code)
    witnesses = []
    for code in payload.witness_batches:
        if any(item[0].code == code for item in witnesses):
            blocked(db, actor, 'blind.seal', f'见证批 {code} 在名单中重复', code)
        batch = db.get(DryBatch, code)
        if batch is None:
            blocked(db, actor, 'blind.seal', f'见证批 {code} 尚未登记', code)
        if batch.recipe_code != recipe.code:
            blocked(db, actor, 'blind.seal', f'见证批 {code} 不属于配方 {recipe.code}', code)
        if batch.state == BatchState.REFERENCE:
            blocked(db, actor, 'blind.seal', f'见证批 {code} 已认证为参考，不能进入本次盲验', code)
        run = ledger.latest_run(db, code)
        if run is None or run.state != RunState.FROZEN:
            blocked(db, actor, 'blind.seal', f'见证批 {code} 必须先冻结各自的设备曲线', code)
        if ledger.curve_issue(run.points):
            blocked(db, actor, 'blind.seal', f'见证批 {code} 的冻结曲线缺少完整真空段', code)
        if db.scalar(select(BlindWitness.id).join(BlindValidation)
                     .where(BlindWitness.batch_code == code,
                            BlindValidation.state == BlindState.SEALED)):
            blocked(db, actor, 'blind.seal', f'见证批 {code} 已在封存中的盲验名单内', code)
        witnesses.append((batch, run))
    try:
        training = [reference_snapshot(db, recipe, batch) for batch in training_candidates(db, recipe)]
    except ValueError as exc:
        blocked(db, actor, 'blind.seal', str(exc), recipe.code)
    if len(training) < 3:
        blocked(db, actor, 'blind.seal', '至少需要三个同配方的已认证标定参考批次才能封存盲验', recipe.code)
    feature_columns = zip(*(item['features'] for item in training), strict=True)
    if not any(max(column) - min(column) > 1e-9 for column in feature_columns):
        blocked(db, actor, 'blind.seal', '封存参考工况一致，请先认证有工况变化的同配方历史批次', recipe.code)
    roster = {
        'recipe': recipe.code, 'tolerance_pp': 2.0,
        'training_batches': [item['batch'] for item in training],
        'witness_batches': [batch.code for batch, _ in witnesses],
    }
    validation = BlindValidation(
        recipe_code=recipe.code, sealed_index=sealed_index_for(db, recipe.code),
        state=BlindState.SEALED, tolerance_pp=2.0, sealed_by=actor,
        sealed_note=payload.note, roster_hash=ledger.checksum(roster),
        training_snapshot=training,
    )
    db.add(validation)
    db.flush()
    for batch, run in witnesses:
        db.add(BlindWitness(blind_id=validation.id, batch_code=batch.code, run_id=run.id,
                            job_state=WitnessState.IDLE, touched_at=utcnow()))
    ledger.record(db, actor, 'blind.seal',
                  f'{recipe.code} · 第 {validation.sealed_index} 轮 · 参考 {len(training)} 批 / 见证 {len(witnesses)} 批 · {payload.note}',
                  recipe.code)
    db.flush()
    return validation


def witness_snapshot(db, validation, witness, recipe):
    run = db.get(ChamberRun, witness.run_id)
    batch = db.get(DryBatch, witness.batch_code)
    if run is None or run.state != RunState.FROZEN:
        raise ValueError('见证曲线未冻结，不能锁定预测')
    return {
        'blind_id': validation.id, 'witness_id': witness.id, 'batch': batch.code,
        'recipe': recipe.code, 'curve_hash': run.checksum,
        'features': ledger.feature_row(batch, recipe, run),
        'window': [round(recipe.window_low, 5), round(recipe.window_high, 5)],
        'training': validation.training_snapshot,
    }


def lock_prediction(db, witness_id, actor):
    witness = db.get(BlindWitness, witness_id)
    if witness is None:
        raise HTTPException(404, '没有找到该见证批记录')
    validation = db.get(BlindValidation, witness.blind_id)
    db.refresh(validation, with_for_update=True)
    if validation.state != BlindState.SEALED:
        blocked(db, actor, 'blind.predict', '该盲验已结束，晚到的预测不能进入新盲验', witness.batch_code)
    if witness.job_state not in (WitnessState.IDLE, WitnessState.FAILED):
        blocked(db, actor, 'blind.predict', '该见证批的盲验预测已锁定，不能重复提交', witness.batch_code)
    recipe = db.get(RecipeLot, validation.recipe_code)
    try:
        snapshot = witness_snapshot(db, validation, witness, recipe)
    except ValueError as exc:
        blocked(db, actor, 'blind.predict', str(exc), witness.batch_code)
    witness.snapshot = snapshot
    witness.input_hash = ledger.checksum(snapshot)
    witness.error = None
    witness.job_state = WitnessState.QUEUED
    witness.touched_at = utcnow()
    ledger.record(db, actor, 'blind.predict.queue',
                  f'{witness.batch_code} · 预测先行锁定 · {witness.input_hash}', witness.batch_code)
    db.flush()
    return witness


def wake_blind(witness_id):
    try:
        redis.rpush(QUEUE, f'{BLIND_PREFIX}{witness_id}')
    except Exception:
        logging.getLogger(__name__).exception('Redis wake-up delayed; worker will scan blind jobs')


def observed_pct(pairs):
    return round(
        sum(100 * (1 - pair.after_mm / pair.before_mm) for pair in pairs) / len(pairs), 6)


def register_observed(db, witness_id, payload, actor):
    witness = db.get(BlindWitness, witness_id)
    if witness is None:
        raise HTTPException(404, '没有找到该见证批记录')
    validation = db.get(BlindValidation, witness.blind_id)
    db.refresh(validation, with_for_update=True)
    if validation.state != BlindState.SEALED:
        blocked(db, actor, 'blind.observed', '该盲验已结束，晚到的实测不能给新盲验凑数', witness.batch_code)
    if witness.job_state != WitnessState.PREDICTED:
        blocked(db, actor, 'blind.observed', '见证批须先锁定盲验预测，再登记实测', witness.batch_code)
    if witness.observed_pct is not None:
        blocked(db, actor, 'blind.observed', '该见证批实测已登记，盲验记录不可改写', witness.batch_code)
    run = db.get(ChamberRun, witness.run_id)
    if run.state != RunState.FROZEN:
        blocked(db, actor, 'blind.observed', '见证曲线已不是冻结版本，实测须按各自冻结曲线核对', witness.batch_code)
    if run.checksum != witness.snapshot['curve_hash']:
        blocked(db, actor, 'blind.observed',
                '见证曲线指纹与封存时的冻结版本不一致，实测须按各自冻结曲线核对', witness.batch_code)
    pair_rows = [{'specimen': pair.specimen, 'before_mm': pair.before_mm, 'after_mm': pair.after_mm}
                 for pair in payload.pairs]
    measured = observed_pct(payload.pairs)
    error_pp = round(measured - witness.result['prediction_pct'], 6)
    within = abs(error_pp) <= validation.tolerance_pp + 1e-9
    witness.observed_pct = measured
    witness.observed_pairs = pair_rows
    witness.curve_hash_at_observed = run.checksum
    witness.observed_note = payload.note
    witness.observed_by = actor
    witness.observed_at = utcnow()
    witness.error_pp = error_pp
    witness.within_tolerance = within
    witness.job_state = WitnessState.COMPLETED
    witness.touched_at = utcnow()
    ledger.record(db, actor, 'blind.observed',
                  f'{witness.batch_code} · 实测 {measured:.3f}% · 预测 {witness.result["prediction_pct"]:.3f}% · '
                  f'误差 {error_pp:+.3f} pp · {payload.note}', witness.batch_code)
    db.flush()
    if not within:
        void_validation(db, validation, actor,
                        f'见证批 {witness.batch_code} 误差 {error_pp:+.3f} pp 超过 ±{validation.tolerance_pp:g} 个百分点，盲验自动作废')
        return validation, witness
    siblings = db.scalars(select(BlindWitness).filter_by(blind_id=validation.id)).all()
    if all(item.observed_pct is not None for item in siblings) and all(item.within_tolerance for item in siblings):
        open_trial(db, validation, siblings, actor)
    return validation, witness


def open_trial(db, validation, witnesses, actor):
    recipe = db.get(RecipeLot, validation.recipe_code)
    basis = {
        'tolerance_pp': validation.tolerance_pp,
        'roster_hash': validation.roster_hash,
        'witnesses': [
            {'batch': item.batch_code, 'prediction_pct': item.result['prediction_pct'],
             'observed_pct': item.observed_pct, 'error_pp': item.error_pp,
             'curve_hash': item.curve_hash_at_observed}
            for item in witnesses
        ],
    }
    validation.state = BlindState.OPENED
    validation.opened_by = actor
    validation.opened_at = utcnow()
    validation.opened_note = '全部见证批误差在允许范围内，开放生产试算'
    validation.opening_basis = basis
    recipe.trial_opened_at = validation.opened_at
    ledger.record(db, actor, 'blind.opened',
                  f'{recipe.code} · {len(witnesses)} 批见证全部达标（±{validation.tolerance_pp:g} pp），开放生产试算',
                  recipe.code)


def void_validation(db, validation, actor, note):
    db.refresh(validation, with_for_update=True)
    if validation.state != BlindState.SEALED:
        blocked(db, actor, 'blind.void', '仅封存中的盲验可以作废', validation.recipe_code)
    validation.state = BlindState.VOID
    validation.voided_by = actor
    validation.voided_note = note
    validation.voided_at = utcnow()
    for witness in db.scalars(select(BlindWitness).filter_by(blind_id=validation.id)).all():
        if witness.job_state in (WitnessState.QUEUED, WitnessState.RUNNING):
            witness.job_state = WitnessState.FAILED
            witness.error = '盲验已作废，未完成的预测不再结算'
            witness.touched_at = utcnow()
    ledger.record(db, actor, 'blind.void', f'{validation.recipe_code} · {note}', validation.recipe_code)


def detail_payload(db, validation):
    witnesses = db.scalars(select(BlindWitness).filter_by(blind_id=validation.id)
                           .order_by(BlindWitness.id)).all()
    runs = {witness.run_id: db.get(ChamberRun, witness.run_id) for witness in witnesses}
    return {
        'validation': ledger.row(validation),
        'witnesses': [
            {**ledger.row(witness),
             'source_name': runs[witness.run_id].source_name,
             'curve_checksum': runs[witness.run_id].checksum}
            for witness in witnesses
        ],
    }
