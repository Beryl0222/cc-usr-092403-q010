"""损伤理赔契约：把理赔责任与作品安全分离的规则固定为可执行的验收条件。

覆盖：
- 报案锁定事故发生时有效的保险条款、交接双方签认与图像摘要；
- 保险方/出借馆/承借馆/运输方按角色追加估损、异议、修复方案、责任意见，
  补充材料各成新版本，被决定引用的版本不得替换；
- 部分认可、免赔额、分期赔付与追偿以资金分录保持金额守恒，
  重复回执与并发决定不得多记赔款；
- 协议后来改期不改变原保障范围；
- 理赔关闭与作品解冻分别判断：解冻须修复复核与保管责任同时满足；
- 风险追溯并列呈现开放损伤、理赔阶段、缺少的签认与资金变化；
- 未获授权的机构看不到作品敏感材料。
"""

import unittest

from domain import ConflictError, DomainError, LoanRegistry
from domain_contract import agreement_payload, handover_payload

INSURER = "安诚保险"


def make_claim_env():
    """作品 + 协议 + 出库 + 到馆损伤（作品冻结中）的理赔现场。"""
    registry = LoanRegistry()
    work = registry.register_work("溪山暮雪", "独立作品", "甲馆")
    work_id = work["work"]["work_id"]
    registry.create_agreement(agreement_payload(work_id))
    registry.record_handover(handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
    damaged = registry.record_handover(handover_payload(
        work_id, "到馆", "SCAN-2", "2026-09-27",
        report={
            "condition": "损伤",
            "damage_note": "画心左下角新增折痕",
            "image_hashes": ["到馆检视照"],
            "before_hashes": ["a" * 64],
            "after_hashes": ["b" * 64],
        },
    ))
    return registry, work_id, damaged["incident_id"]


def claim_payload(work_id, incident_id, **overrides):
    payload = {
        "work_id": work_id,
        "incident_id": incident_id,
        "insurer_org": INSURER,
        "filed_by": {"org": "甲馆", "role": "出借馆", "person": "馆员甲"},
        "filed_on": "2026-09-28",
    }
    payload.update(overrides)
    return payload


def file_claim(registry, work_id, incident_id, **overrides):
    return registry.file_claim(claim_payload(work_id, incident_id, **overrides))


def insurer(person="理赔员"):
    return {"org": INSURER, "role": "保险方", "person": person}


def add_estimate(registry, claim_id, amount=1000000):
    return registry.add_submission(claim_id, {
        "kind": "估损", "summary": "修复费用与贬值合计", "amount": amount,
        "image_hashes": ["估损明细照"], "by": insurer("估损师"),
    })


def add_plan(registry, claim_id):
    return registry.add_submission(claim_id, {
        "kind": "修复方案", "summary": "局部揭裱修补折痕",
        "by": {"org": "乙馆", "role": "承借馆", "person": "修复师"},
    })


def decide(registry, claim_id, approved=800000, deductible=50000, cited=(1,)):
    return registry.decide_claim(claim_id, {
        "approved_amount": approved, "deductible": deductible,
        "cited_versions": list(cited), "decided_on": "2026-10-05",
        "by": insurer(),
    })


def pay(registry, claim_id, amount, receipt):
    return registry.append_entry(claim_id, {
        "kind": "赔付", "amount": amount, "receipt_id": receipt, "by": insurer(),
    })


def satisfy_unfreeze(registry, claim_id):
    """修复方案 → 出借馆复核通过 → 当前保管方确认保管责任。"""
    add_plan(registry, claim_id)
    registry.review_restoration(claim_id, {
        "conclusion": "通过", "note": "修复达标",
        "by": {"org": "甲馆", "role": "出借馆", "person": "复核员"},
    })
    registry.acknowledge_custody(claim_id, {"by": {"org": "乙馆", "person": "保管员"}})


class ClaimFilingTest(unittest.TestCase):
    def test_filing_locks_incident_time_terms_signatures_and_digests(self):
        registry, work_id, incident_id = make_claim_env()
        view = file_claim(registry, work_id, incident_id)
        baseline = view["baseline"]
        # 事故发生时有效的保险条款（协议 v1）
        self.assertEqual(baseline["agreement_version"], 1)
        self.assertEqual(baseline["insurance"]["coverage"], "钉到钉")
        self.assertEqual(baseline["insurance"]["policy"], "POL-001")
        # 损伤交接的双方签认
        self.assertEqual(baseline["signatures"]["from_party"]["role"], "运输方")
        self.assertEqual(baseline["signatures"]["from_party"]["org"], "长风运输")
        self.assertEqual(baseline["signatures"]["to_party"]["role"], "承借馆")
        self.assertEqual(baseline["signatures"]["to_party"]["org"], "乙馆")
        # 图像摘要：损伤前后哈希 + 交接检视照哈希
        digests = baseline["image_digests"]
        self.assertEqual(digests["before_hashes"], ["a" * 64])
        self.assertEqual(digests["after_hashes"], ["b" * 64])
        self.assertEqual(len(digests["handover_hashes"]), 1)
        self.assertEqual(view["stage"], "已报案")

    def test_duplicate_filing_for_same_incident_rejected(self):
        registry, work_id, incident_id = make_claim_env()
        file_claim(registry, work_id, incident_id)
        with self.assertRaises(ConflictError):
            file_claim(registry, work_id, incident_id)

    def test_filing_requires_open_incident(self):
        registry, work_id, incident_id = make_claim_env()
        registry.resolve_incident(incident_id, "双方确认轻微痕迹，无需理赔")
        with self.assertRaises(ConflictError):
            file_claim(registry, work_id, incident_id)

    def test_filing_validates_filer_role_org_and_date(self):
        registry, work_id, incident_id = make_claim_env()
        # 运输方不能报案
        with self.assertRaises(DomainError):
            file_claim(registry, work_id, incident_id,
                       filed_by={"org": "长风运输", "role": "运输方", "person": "调度"})
        # 机构与角色不符
        with self.assertRaises(DomainError):
            file_claim(registry, work_id, incident_id,
                       filed_by={"org": "乙馆", "role": "出借馆", "person": "馆员乙"})
        # 报案日期不能早于事故日期
        with self.assertRaises(DomainError):
            file_claim(registry, work_id, incident_id, filed_on="2026-09-26")
        # 承借馆可以报案
        view = file_claim(registry, work_id, incident_id,
                          filed_by={"org": "乙馆", "role": "承借馆", "person": "馆员乙"})
        self.assertEqual(view["filed_by"]["org"], "乙馆")

    def test_filing_without_insurance_terms_rejected(self):
        registry = LoanRegistry()
        work = registry.register_work("无保险作品", "独立作品", "甲馆")
        work_id = work["work"]["work_id"]
        registry.create_agreement(agreement_payload(work_id, insurance={}))
        registry.record_handover(handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
        damaged = registry.record_handover(handover_payload(
            work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "磕碰",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        with self.assertRaises(DomainError):
            file_claim(registry, work_id, damaged["incident_id"])


class SubmissionTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.work_id, self.incident_id = make_claim_env()
        self.claim_id = file_claim(self.registry, self.work_id, self.incident_id)["claim_id"]

    def test_each_role_appends_its_own_kind(self):
        estimate = add_estimate(self.registry, self.claim_id)
        self.assertEqual(estimate["version"], 1)
        plan = add_plan(self.registry, self.claim_id)
        self.assertEqual(plan["version"], 2)
        opinion = self.registry.add_submission(self.claim_id, {
            "kind": "责任意见", "summary": "运输途中温湿度记录正常",
            "by": {"org": "长风运输", "role": "运输方", "person": "调度"},
        })
        self.assertEqual(opinion["version"], 3)
        objection = self.registry.add_submission(self.claim_id, {
            "kind": "异议", "summary": "对损伤成因有异议", "by": insurer("查勘员"),
        })
        self.assertEqual(objection["version"], 4)

    def test_wrong_role_or_org_rejected(self):
        with self.assertRaises(DomainError):
            self.registry.add_submission(self.claim_id, {
                "kind": "估损", "summary": "越权估损", "amount": 1,
                "by": {"org": "乙馆", "role": "承借馆", "person": "馆员乙"},
            })
        with self.assertRaises(DomainError):
            self.registry.add_submission(self.claim_id, {
                "kind": "修复方案", "summary": "越权方案",
                "by": {"org": "长风运输", "role": "运输方", "person": "调度"},
            })
        with self.assertRaises(DomainError):
            self.registry.add_submission(self.claim_id, {
                "kind": "估损", "summary": "假冒保险方", "amount": 1,
                "by": {"org": "假冒保险", "role": "保险方", "person": "骗子"},
            })
        # 被拒绝的追加不产生版本
        view = self.registry.claim_view(self.claim_id, viewer_org=INSURER)
        self.assertEqual(view["submissions"], [])

    def test_versions_never_replace_cited_evidence(self):
        add_estimate(self.registry, self.claim_id, amount=1000000)
        self.registry.add_submission(self.claim_id, {
            "kind": "异议", "summary": "出借馆对估损口径有异议",
            "by": {"org": "甲馆", "role": "出借馆", "person": "馆员甲"},
        })
        decide(self.registry, self.claim_id, cited=(1,))
        # 决定之后继续追加：形成新版本，而不是替换被引用的 v1
        add_estimate(self.registry, self.claim_id, amount=900000)
        view = self.registry.claim_view(self.claim_id, viewer_org=INSURER)
        self.assertEqual([s["version"] for s in view["submissions"]], [1, 2, 3])
        v1 = view["submissions"][0]
        self.assertEqual(v1["amount"], 1000000)
        self.assertEqual(v1["summary"], "修复费用与贬值合计")
        self.assertTrue(v1["cited_by_decision"])
        self.assertFalse(view["submissions"][2]["cited_by_decision"])
        self.assertEqual(view["decision"]["cited_versions"], [1])


class DecisionAndLedgerTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.work_id, self.incident_id = make_claim_env()
        self.claim_id = file_claim(self.registry, self.work_id, self.incident_id)["claim_id"]
        add_estimate(self.registry, self.claim_id, amount=1000000)

    def test_partial_approval_deductible_installments_and_recovery_conserve_amounts(self):
        decided = decide(self.registry, self.claim_id, approved=800000, deductible=50000)
        funds = decided["funds"]
        self.assertEqual(funds["recognized"], 800000)
        self.assertEqual(funds["deductible"], 50000)
        self.assertEqual(funds["outstanding"], 750000)
        # 分期赔付：累计不得超过 认可 - 免赔
        pay(self.registry, self.claim_id, 300000, "PAY-1")
        with self.assertRaises(DomainError):
            pay(self.registry, self.claim_id, 500001, "PAY-2")
        view = pay(self.registry, self.claim_id, 450000, "PAY-2")
        self.assertEqual(view["funds"]["outstanding"], 0)
        self.assertEqual(view["stage"], "已结清")
        # 追偿：累计不得超过已赔付
        self.registry.append_entry(self.claim_id, {
            "kind": "追偿", "amount": 200000, "receipt_id": "REC-1", "by": insurer(),
        })
        with self.assertRaises(DomainError):
            self.registry.append_entry(self.claim_id, {
                "kind": "追偿", "amount": 600000, "receipt_id": "REC-2", "by": insurer(),
            })
        funds = self.registry.claim_view(self.claim_id, viewer_org=INSURER)["funds"]
        # 金额守恒：认可 = 免赔 + 已付 + 待付
        self.assertEqual(
            funds["recognized"],
            funds["deductible"] + funds["paid"] + funds["outstanding"],
        )
        self.assertEqual(funds["recovered"], 200000)

    def test_duplicate_receipt_and_concurrent_decision_never_double_count(self):
        decide(self.registry, self.claim_id)
        pay(self.registry, self.claim_id, 300000, "PAY-1")
        with self.assertRaises(ConflictError):
            pay(self.registry, self.claim_id, 300000, "PAY-1")
        with self.assertRaises(ConflictError):
            decide(self.registry, self.claim_id)
        view = self.registry.claim_view(self.claim_id, viewer_org=INSURER)
        self.assertEqual(view["funds"]["paid"], 300000)  # 没有多记赔款
        self.assertEqual(view["funds"]["recognized"], 800000)
        self.assertEqual(len([e for e in view["ledger"] if e["kind"] == "赔付"]), 1)

    def test_decision_validation(self):
        with self.assertRaises(DomainError):  # 免赔额超过认可金额
            decide(self.registry, self.claim_id, approved=50000, deductible=60000)
        with self.assertRaises(DomainError):  # 引用不存在的材料版本
            decide(self.registry, self.claim_id, cited=(9,))
        with self.assertRaises(DomainError):  # 非保险方不能决定
            self.registry.decide_claim(self.claim_id, {
                "approved_amount": 1, "deductible": 0,
                "by": {"org": "甲馆", "role": "出借馆", "person": "馆员甲"},
            })
        with self.assertRaises(ConflictError):  # 决定前不能登记赔付
            pay(self.registry, self.claim_id, 1, "PAY-X")

    def test_close_requires_settlement_and_is_separate_from_unfreeze(self):
        add_plan(self.registry, self.claim_id)
        decide(self.registry, self.claim_id)
        with self.assertRaises(ConflictError):  # 未结清不能关闭
            self.registry.close_claim(self.claim_id, {"by": insurer()})
        pay(self.registry, self.claim_id, 750000, "PAY-1")
        closed = self.registry.close_claim(
            self.claim_id, {"by": insurer(), "closed_on": "2026-10-20"})
        self.assertEqual(closed["stage"], "已关闭")
        # 理赔关闭与作品解冻分别判断：理赔已关闭，作品仍冻结
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])
        # 满足修复复核与保管责任后，交接链照样恢复，理赔保持已关闭
        self.registry.review_restoration(self.claim_id, {
            "conclusion": "通过", "by": {"org": "甲馆", "role": "出借馆", "person": "复核员"},
        })
        self.registry.acknowledge_custody(self.claim_id, {"by": {"org": "乙馆", "person": "保管员"}})
        self.registry.resolve_incident(self.incident_id, "修复复核与保管责任均已确认")
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        view = self.registry.claim_view(self.claim_id, viewer_org=INSURER)
        self.assertEqual(view["stage"], "已关闭")


class UnfreezeTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.work_id, self.incident_id = make_claim_env()
        self.claim_id = file_claim(self.registry, self.work_id, self.incident_id)["claim_id"]

    def test_unfreeze_requires_restoration_review_and_custody_ack(self):
        # 承借馆提交修复方案并完成修复后，想直接解除冻结 → 拒绝
        add_plan(self.registry, self.claim_id)
        with self.assertRaises(ConflictError) as ctx:
            self.registry.resolve_incident(self.incident_id, "修复完成")
        self.assertIn("修复复核", str(ctx.exception))
        # 出借馆复核通过后，仍缺保管责任确认
        self.registry.review_restoration(self.claim_id, {
            "conclusion": "通过", "by": {"org": "甲馆", "role": "出借馆", "person": "复核员"},
        })
        with self.assertRaises(ConflictError) as ctx:
            self.registry.resolve_incident(self.incident_id, "修复完成")
        self.assertIn("保管责任", str(ctx.exception))
        # 非当前保管方不能确认
        with self.assertRaises(DomainError):
            self.registry.acknowledge_custody(
                self.claim_id, {"by": {"org": "甲馆", "person": "馆员甲"}})
        # 当前保管方（到馆后为承借馆乙馆）确认
        self.registry.acknowledge_custody(
            self.claim_id, {"by": {"org": "乙馆", "person": "保管员"}})
        resolved = self.registry.resolve_incident(
            self.incident_id, "修复复核与保管责任均已确认")
        self.assertTrue(resolved["resolved"])
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        # 交接链恢复
        view = self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-10-10"))
        self.assertEqual(view["resulting_status"], "展出中")

    def test_restoration_review_requires_plan_and_lender(self):
        with self.assertRaises(DomainError):  # 尚无修复方案
            self.registry.review_restoration(self.claim_id, {
                "conclusion": "通过",
                "by": {"org": "甲馆", "role": "出借馆", "person": "复核员"},
            })
        add_plan(self.registry, self.claim_id)
        with self.assertRaises(DomainError):  # 复核须由出借馆作出
            self.registry.review_restoration(self.claim_id, {
                "conclusion": "通过",
                "by": {"org": "乙馆", "role": "承借馆", "person": "修复师"},
            })


class TraceAndAccessTest(unittest.TestCase):
    def setUp(self):
        self.registry, self.work_id, self.incident_id = make_claim_env()
        self.claim_id = file_claim(self.registry, self.work_id, self.incident_id)["claim_id"]

    def test_risk_view_lists_damage_stage_missing_signoffs_and_funds(self):
        add_estimate(self.registry, self.claim_id)
        decide(self.registry, self.claim_id)
        pay(self.registry, self.claim_id, 300000, "PAY-1")
        risk = self.registry.risk_view(self.work_id)
        # 开放损伤
        self.assertEqual(len(risk["open_risks"]), 1)
        # 理赔阶段
        claim = risk["claims"][0]
        self.assertEqual(claim["claim_id"], self.claim_id)
        self.assertEqual(claim["stage"], "赔付中")
        # 缺少的签认
        self.assertIn("出借馆修复复核", claim["missing_signoffs"])
        self.assertTrue(any("保管责任确认" in m for m in claim["missing_signoffs"]))
        self.assertNotIn("保险方估损", claim["missing_signoffs"])
        self.assertNotIn("保险方理赔决定", claim["missing_signoffs"])
        # 资金变化
        self.assertEqual(claim["funds"]["recognized"], 800000)
        self.assertEqual(claim["funds"]["paid"], 300000)
        self.assertEqual(claim["funds"]["outstanding"], 450000)

    def test_stage_progression(self):
        def stage():
            return self.registry.claim_view(self.claim_id, viewer_org=INSURER)["stage"]

        self.assertEqual(stage(), "已报案")
        add_estimate(self.registry, self.claim_id)
        self.assertEqual(stage(), "定损中")
        decide(self.registry, self.claim_id)
        self.assertEqual(stage(), "已决定")
        pay(self.registry, self.claim_id, 100000, "PAY-1")
        self.assertEqual(stage(), "赔付中")
        pay(self.registry, self.claim_id, 650000, "PAY-2")
        self.assertEqual(stage(), "已结清")
        self.registry.close_claim(self.claim_id, {"by": insurer()})
        self.assertEqual(stage(), "已关闭")

    def test_unauthorized_org_cannot_see_sensitive_material(self):
        add_estimate(self.registry, self.claim_id)
        decide(self.registry, self.claim_id)
        # 无关机构：看不到基准证据、材料与资金分录
        outsider = self.registry.claim_view(self.claim_id, viewer_org="丙馆")
        self.assertTrue(outsider["sensitive_redacted"])
        for key in ("baseline", "submissions", "ledger", "decision"):
            self.assertNotIn(key, outsider)
        # 不提供机构同样看不到
        self.assertTrue(self.registry.claim_view(self.claim_id)["sensitive_redacted"])
        # 当事各方可见：保险方、出借馆、承借馆、运输方
        for org in (INSURER, "甲馆", "乙馆", "长风运输"):
            full = self.registry.claim_view(self.claim_id, viewer_org=org)
            self.assertNotIn("sensitive_redacted", full)
            self.assertEqual(full["baseline"]["image_digests"]["before_hashes"], ["a" * 64])
        # 风险视图同样按机构隐去图像摘要
        risk = self.registry.risk_view(self.work_id, viewer_org="丙馆")
        self.assertNotIn("before_hashes", risk["open_risks"][0])
        self.assertTrue(risk["open_risks"][0]["sensitive_redacted"])
        risk = self.registry.risk_view(self.work_id, viewer_org="乙馆")
        self.assertEqual(risk["open_risks"][0]["before_hashes"], ["a" * 64])

    def test_reschedule_after_incident_does_not_change_claim_coverage(self):
        add_estimate(self.registry, self.claim_id)
        satisfy_unfreeze(self.registry, self.claim_id)
        self.registry.resolve_incident(self.incident_id, "复核通过，责任明确")
        old_agreement = self.registry.get_work_view(self.work_id)["current_agreement"]
        self.registry.reschedule_agreement(old_agreement, {
            "start_on": "2026-11-01", "end_on": "2027-01-31",
            "insurance": {"insured_value": "议定价值", "coverage": "一切险", "policy": "POL-002"},
        })
        # 理赔基准仍是事故时的条款
        baseline = self.registry.claim_view(self.claim_id, viewer_org=INSURER)["baseline"]
        self.assertEqual(baseline["insurance"]["coverage"], "钉到钉")
        self.assertEqual(baseline["insurance"]["policy"], "POL-001")
        self.assertEqual(baseline["agreement_version"], 1)
        # 当前授权范围已是新版
        risk = self.registry.risk_view(self.work_id)
        self.assertEqual(risk["authorization"]["insurance"]["coverage"], "一切险")
        self.assertEqual(risk["authorization"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
