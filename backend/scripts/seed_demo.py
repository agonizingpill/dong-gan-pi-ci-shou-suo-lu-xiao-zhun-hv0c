from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from src.auth import password_hash
from src.calculation import estimate
from src.db import engine
from src.ledger import checksum, feature_row, observed, queue_fit
from src.models import (
    BatchState, BlindState, BlindValidation, BlindWitness, ChamberRun, DryBatch,
    RecipeLot, Role, RunState, ShrinkageSample, Stage, User, WitnessState,
)

STAMP = datetime(2026, 11, 21, 0, 0)


def points(minutes, pressure, temp, *, vacuum=True):
    series = [(0, 'freeze', -39.0, 101000.0), (45, 'freeze', -37.0, 98000.0)]
    if vacuum:
        series += [(60, 'vacuum', temp - 4, pressure + 7), (60 + minutes // 3, 'vacuum', temp - 1, pressure + 2),
                   (60 + 2 * minutes // 3, 'vacuum', temp + 1, pressure - 3),
                   (60 + minutes, 'vacuum', temp + 4, pressure - 6)]
    series += [(90 + minutes, 'finish', 9.0, 14.0), (130 + minutes, 'finish', 22.0, 10.0)]
    return [dict(minute=m, phase=phase, shelf_c=t, pressure_pa=p) for m, phase, t, p in series]


def seed(db):
    for role in Role:
        if db.get(User, role.value) is None:
            db.add(User(username=role.value, password_hash=password_hash('Lab-demo-1062'), role=role))
    for values in [
        ('AP-26R3', '青苹果薄片 · AP03', 14.2, 8.0, 9.0, 16.0),
        ('PE-26R1', '香梨薄片 · PE01', 12.8, 7.0, 7.0, 14.0),
        ('ST-26R2', '草莓切片 · ST02', 10.1, 10.0, 12.0, 20.0),
    ]:
        code, product, solids, nominal, low, high = values
        if db.get(RecipeLot, code) is None:
            db.add(RecipeLot(code=code, product=product, solids_pct=solids, nominal_mm=nominal,
                             window_low=low, window_high=high))
    db.flush()
    fixtures = [
        ('FD-1101', 7.1, 170, 23, -27, 10, BatchState.REFERENCE),
        ('FD-1104', 8.0, 240, 31, -20, 14, BatchState.REFERENCE),
        ('FD-1108', 9.1, 340, 45, -12, 20, BatchState.REFERENCE),
        ('FD-1121A', 8.0, 243, 32, -20, 13, BatchState.COLLECTING),
        ('FD-1121B', 8.2, 252, 32, -19, None, BatchState.COLLECTING),
        ('FD-1121C', 9.3, 365, 49, -9, 19, BatchState.COLLECTING),
    ]
    pending_fits = []
    for code, feed, minutes, pressure, temp, shrinkage, state in fixtures:
        if db.get(DryBatch, code) is not None:
            continue
        batch = DryBatch(code=code, recipe_code='AP-26R3', dryer='FDX-02', material_lot=f'APL-{code[3:]}',
                         feed_mm=feed, state=state)
        db.add(batch)
        db.flush()
        if code in ('FD-1121B', 'FD-1121C'):
            db.add(ChamberRun(batch_code=code, source_name=f'{code}_chamber_r0.csv', state=RunState.DRAFT,
                              points=points(minutes, pressure, temp, vacuum=False)))
            db.flush()
        if shrinkage is None:
            continue
        readings = points(minutes, pressure, temp)
        run = ChamberRun(batch_code=code, source_name=f'{code}_chamber_r1.csv', state=RunState.FROZEN,
                         points=readings, checksum=checksum(readings), frozen_at=STAMP)
        db.add(run)
        db.flush()
        for specimen, delta in [('TRAY-A', -0.4), ('TRAY-C', 0.4)]:
            for stage, reading in [(Stage.PRE, feed), (Stage.POST, round(feed * (1 - (shrinkage + delta) / 100), 5))]:
                db.add(ShrinkageSample(run_id=run.id, specimen=specimen, stage=stage, thickness_mm=reading,
                                       technician='operator', recorded_at=STAMP))
        db.flush()
        if code in ('FD-1121A', 'FD-1121C'):
            pending_fits.append(batch)
    # The demo blind validation must open production trials before fits can queue.
    seed_blind(db)
    for batch in pending_fits:
        queue_fit(db, batch, 'operator')
    return len(db.scalars(select(DryBatch.code)).all())


def frozen_witness_batch(db, code, feed, minutes, pressure, temp):
    batch = DryBatch(code=code, recipe_code='AP-26R3', dryer='FDX-02', material_lot=f'APL-{code[3:]}',
                     feed_mm=feed, state=BatchState.COLLECTING)
    db.add(batch)
    db.flush()
    readings = points(minutes, pressure, temp)
    run = ChamberRun(batch_code=code, source_name=f'{code}_chamber_r1.csv', state=RunState.FROZEN,
                     points=readings, checksum=checksum(readings), frozen_at=STAMP)
    db.add(run)
    db.flush()
    return batch, run


def add_witness_samples(db, run, feed, target_shrinkage, offsets=(-0.4, 0.4)):
    for specimen, delta in zip(('TRAY-A', 'TRAY-C'), offsets, strict=True):
        for stage, reading in [
            (Stage.PRE, feed),
            (Stage.POST, round(feed * (1 - (target_shrinkage + delta) / 100), 5)),
        ]:
            db.add(ShrinkageSample(run_id=run.id, specimen=specimen, stage=stage, thickness_mm=reading,
                                   technician='operator', recorded_at=STAMP))
    db.flush()


def seed_blind(db):
    recipe = db.get(RecipeLot, 'AP-26R3')
    if db.scalar(select(BlindValidation.id).filter_by(recipe_code=recipe.code)) is not None:
        return
    witness_specs = [
        ('FD-1131W', 8.4, 210, 28, -23),
        ('FD-1132W', 8.8, 300, 38, -15),
    ]
    created = []
    for code, feed, minutes, pressure, temp in witness_specs:
        if db.get(DryBatch, code) is None:
            batch, run = frozen_witness_batch(db, code, feed, minutes, pressure, temp)
        else:
            batch = db.get(DryBatch, code)
            run = db.scalar(select(ChamberRun).filter_by(batch_code=code).order_by(ChamberRun.id.desc()).limit(1))
        created.append((batch, run, feed))
    references = db.scalars(select(DryBatch).filter_by(recipe_code=recipe.code, state=BatchState.REFERENCE)
                            .order_by(DryBatch.code)).all()
    training = []
    for historical in references:
        archived = db.scalar(select(ChamberRun).filter_by(batch_code=historical.code)
                             .order_by(ChamberRun.id.desc()).limit(1))
        pairs = {}
        for sample in db.scalars(select(ShrinkageSample).filter_by(run_id=archived.id)
                                 .order_by(ShrinkageSample.specimen, ShrinkageSample.id)):
            pairs.setdefault(sample.specimen, {})[sample.stage] = round(sample.thickness_mm, 5)
        training.append({'batch': historical.code, 'curve_hash': archived.checksum,
                         'features': feature_row(historical, recipe, archived),
                         'observed': observed(pairs), 'pairs': pairs})
    roster = {'recipe': recipe.code, 'tolerance_pp': 2.0,
              'training_batches': [item['batch'] for item in training],
              'witness_batches': [batch.code for batch, _, _ in created]}
    validation = BlindValidation(
        recipe_code=recipe.code, sealed_index=1, state=BlindState.OPENED, tolerance_pp=2.0,
        sealed_by='supervisor', sealed_note='投产前收缩率盲验封存（演示）', sealed_at=STAMP,
        roster_hash=checksum(roster), training_snapshot=training,
    )
    db.add(validation)
    db.flush()
    basis_witnesses = []
    # Predictions are locked first; measured specimens are built afterwards around each
    # prediction so the demo blind passes with realistic, non-zero per-batch errors.
    for index, (batch, run, feed) in enumerate(created):
        snapshot = {
            'blind_id': validation.id, 'batch': batch.code, 'recipe': recipe.code,
            'curve_hash': run.checksum, 'features': feature_row(batch, recipe, run),
            'window': [round(recipe.window_low, 5), round(recipe.window_high, 5)], 'training': training,
        }
        result = estimate(snapshot)
        target = result['prediction_pct'] + (0.5 if index == 0 else -0.6)
        add_witness_samples(db, run, feed, target)
        pairs = {}
        for sample in db.scalars(select(ShrinkageSample).filter_by(run_id=run.id)
                                 .order_by(ShrinkageSample.specimen, ShrinkageSample.id)):
            pairs.setdefault(sample.specimen, {})[sample.stage] = round(sample.thickness_mm, 5)
        measured = observed(pairs)
        error_pp = round(measured - result['prediction_pct'], 6)
        pair_rows = [{'specimen': name, 'before_mm': pair[Stage.PRE], 'after_mm': pair[Stage.POST]}
                     for name, pair in pairs.items()]
        db.add(BlindWitness(
            blind_id=validation.id, batch_code=batch.code, run_id=run.id,
            job_state=WitnessState.COMPLETED, input_hash=checksum(snapshot), snapshot=snapshot,
            result=result, attempts=1, predicted_at=STAMP, observed_pct=measured,
            observed_pairs=pair_rows, curve_hash_at_observed=run.checksum,
            observed_note='盲验见证实测登记（演示）', observed_by='operator', observed_at=STAMP,
            error_pp=error_pp, within_tolerance=abs(error_pp) <= 2.0 + 1e-9, touched_at=STAMP,
        ))
        basis_witnesses.append({'batch': batch.code, 'prediction_pct': result['prediction_pct'],
                                'observed_pct': measured, 'error_pp': error_pp,
                                'curve_hash': run.checksum})
        db.flush()
    validation.opened_by = 'supervisor'
    validation.opened_at = STAMP
    validation.opened_note = '全部见证批误差在允许范围内，开放生产试算'
    validation.opening_basis = {'tolerance_pp': 2.0, 'roster_hash': validation.roster_hash,
                                'witnesses': basis_witnesses}
    recipe.trial_opened_at = STAMP


def main():
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK('freeze-demo-seed', 15)")) != 1:
            raise RuntimeError('unable to lock demo seed')
        connection.commit()
        try:
            with OrmSession(bind=connection) as db, db.begin():
                count = seed(db)
            print(f'demo seed committed, batches={count}')
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('freeze-demo-seed')"))


if __name__ == '__main__':
    main()
