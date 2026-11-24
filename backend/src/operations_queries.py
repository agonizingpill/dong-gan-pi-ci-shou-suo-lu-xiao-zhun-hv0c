"""Business queries over authoritative project records."""

VIEWS = {
    'specimen-repeatability': (
        '试片测量重复性',
        '按冻干曲线和试片阶段对照原测与复测，差值超过 0.5 mm 时交计量主管复核。',
        ['批次', '曲线', '试片', '阶段', '原测 mm', '复测 mm', '绝对差 mm', '判定'],
        """
SELECT b.code, r.id, s.specimen, s.stage,
       AVG(CASE WHEN NOT s.is_retest THEN s.thickness_mm END),
       AVG(CASE WHEN s.is_retest THEN s.thickness_mm END),
       ABS(AVG(CASE WHEN NOT s.is_retest THEN s.thickness_mm END) - AVG(CASE WHEN s.is_retest THEN s.thickness_mm END)),
       CASE WHEN COUNT(CASE WHEN NOT s.is_retest THEN 1 END) = 0 THEN '缺原测'
            WHEN COUNT(CASE WHEN s.is_retest THEN 1 END) = 0 THEN '待复测'
            WHEN ABS(AVG(CASE WHEN NOT s.is_retest THEN s.thickness_mm END) - AVG(CASE WHEN s.is_retest THEN s.thickness_mm END)) > 0.5 THEN '差值超限'
            ELSE '一致' END
FROM dry_batches b JOIN chamber_runs r ON r.batch_code=b.code JOIN shrinkage_samples s ON s.run_id=r.id
GROUP BY b.code,r.id,s.specimen,s.stage ORDER BY r.id DESC,s.specimen,s.stage
""",
    ),
    'blind-witness-errors': (
        '收缩率盲验逐批误差',
        '按封存盲验核对各见证批先锁定的预测与后登记实测，误差绝对值超过 2 个百分点即作废，全部达标才开放生产试算。',
        ['配方', '盲验轮次', '盲验状态', '见证批次', '冻结曲线', '预测 %', '实测 %', '误差 pp', '判定'],
        """
SELECT v.recipe_code, v.sealed_index, v.state, w.batch_code, r.source_name,
       JSON_EXTRACT(w.result, '$.prediction_pct'), w.observed_pct, w.error_pp,
       CASE WHEN w.observed_pct IS NULL THEN '待实测'
            WHEN w.within_tolerance THEN '达标'
            ELSE '超差作废' END
FROM blind_validations v
JOIN blind_witnesses w ON w.blind_id=v.id
JOIN chamber_runs r ON r.id=w.run_id
ORDER BY v.id DESC, w.id
""",
    ),
}
