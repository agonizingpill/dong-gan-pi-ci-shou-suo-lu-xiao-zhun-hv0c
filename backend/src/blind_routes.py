"""HTTP routes for the shrinkage blind validation workflow."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import blind, ledger
from .auth import identity, require
from .db import session
from .models import BlindValidation, BlindWitness, RecipeLot, Role
from .schemas import BlindObservedIn, BlindSealIn, BlindVoidIn

router = APIRouter(prefix='/blind', tags=['收缩率盲验'])
supervisor = require(Role.SUPERVISOR)
lab_staff = require(Role.OPERATOR, Role.SUPERVISOR)


@router.get('/recipes-overview', dependencies=[Depends(identity)])
def overview(db: Session = Depends(session)):
    recipes = db.scalars(select(RecipeLot).order_by(RecipeLot.code)).all()
    result = []
    for recipe in recipes:
        validations = db.scalars(select(BlindValidation).filter_by(recipe_code=recipe.code)
                                 .order_by(BlindValidation.id.desc())).all()
        result.append({
            'recipe': ledger.row(recipe),
            'trial_opened': recipe.trial_opened_at is not None,
            'validations': [
                {'id': item.id, 'sealed_index': item.sealed_index, 'state': item.state,
                 'sealed_at': item.sealed_at, 'opened_at': item.opened_at, 'voided_at': item.voided_at}
                for item in validations
            ],
        })
    return result


@router.post('/seal', status_code=201)
def seal(payload: BlindSealIn, db: Session = Depends(session), user=Depends(supervisor)):
    validation = blind.seal_blind(db, payload, user.username)
    db.commit()
    return blind.detail_payload(db, validation)


@router.get('/{blind_id}', dependencies=[Depends(identity)])
def detail(blind_id: int, db: Session = Depends(session)):
    validation = db.get(BlindValidation, blind_id)
    if validation is None:
        raise HTTPException(404, '没有找到该盲验记录')
    return blind.detail_payload(db, validation)


@router.post('/witnesses/{witness_id}/predict', status_code=202)
def predict(witness_id: int, db: Session = Depends(session), user=Depends(lab_staff)):
    if db.get(BlindWitness, witness_id) is None:
        raise HTTPException(404, '没有找到该见证批记录')
    witness = blind.lock_prediction(db, witness_id, user.username)
    db.commit()
    blind.wake_blind(witness.id)
    return ledger.row(witness)


@router.post('/witnesses/{witness_id}/observed')
def observed(witness_id: int, payload: BlindObservedIn, db: Session = Depends(session),
             user=Depends(lab_staff)):
    if db.get(BlindWitness, witness_id) is None:
        raise HTTPException(404, '没有找到该见证批记录')
    validation, witness = blind.register_observed(db, witness_id, payload, user.username)
    db.commit()
    return blind.detail_payload(db, validation)


@router.post('/{blind_id}/void')
def void(blind_id: int, payload: BlindVoidIn, db: Session = Depends(session),
         user=Depends(supervisor)):
    validation = db.get(BlindValidation, blind_id)
    if validation is None:
        raise HTTPException(404, '没有找到该盲验记录')
    blind.void_validation(db, validation, user.username, payload.note)
    db.commit()
    return blind.detail_payload(db, validation)
