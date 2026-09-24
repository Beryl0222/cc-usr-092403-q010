"""领域契约：把借展业务规则固定为可执行的验收条件。

覆盖：
- 十六位作者长卷的区段、贡献与合作顺序；
- 协议条款（展期/展厅/照度/运输/保险/数字传播）；
- 五种交接的双方签认与生命周期顺序；
- 重复扫码幂等；
- 损伤即冻结、前后图像哈希保全；
- 理赔报案锁定事故时有效条款/签认/图像摘要；四方按角色追加、版本不替换；
- 资金分录守恒（免赔额、分期赔付、追偿），重复回执与并发决定不多记；
- 理赔关闭与作品解冻分别判断，修复复核+保管责任双闸门才恢复交接；
- 风险追溯并列呈现开放损伤、理赔阶段、缺少的签认与资金变化；
- 未获授权机构看不到作品敏感材料；
- 展签发布即锁定证据快照，学术更正只另起新版；
- 跨馆改期与局部状态争议下，从展签/区段定位实体、保管、授权与风险。
"""

import threading
import unittest

from domain import (
    CONTRIBUTION_KINDS,
    ConflictError,
    DomainError,
    LoanRegistry,
)

INSURER_ORG = "安保保险"
TRANSPORTER_ORG = "长风运输"
LENDER_ORG = "甲馆"
BORROWER_ORG = "乙馆"


def make_long_scroll(registry: LoanRegistry) -> dict:
    """一件含十六位作者、四个区段的长卷。"""
    segment_ids = ["SEG-A", "SEG-B", "SEG-C", "SEG-D"]
    segments = [
        {"segment_id": "SEG-A", "label": "引首", "start_cm": 0, "end_cm": 80, "note": "书引首与题签"},
        {"segment_id": "SEG-B", "label": "画心甲", "start_cm": 80, "end_cm": 360},
        {"segment_id": "SEG-C", "label": "画心乙", "start_cm": 360, "end_cm": 640},
        {"segment_id": "SEG-D", "label": "尾纸题跋", "start_cm": 640, "end_cm": 900},
    ]
    authors = [
        ("赵某", "作画"), ("钱某", "作画"), ("孙某", "作画"), ("李某", "作画"),
        ("周某", "作画"), ("吴某", "作画"), ("郑某", "作画"), ("王某", "作画"),
        ("冯某", "作画"), ("陈某", "作画"), ("褚某", "作画"), ("卫某", "作画"),
        ("蒋某", "题跋"), ("沈某", "题跋"), ("韩某", "书引首"), ("杨某", "题签"),
    ]
    seg_for_order = [
        "SEG-C", "SEG-B", "SEG-B", "SEG-B",
        "SEG-B", "SEG-C", "SEG-C", "SEG-C",
        "SEG-C", "SEG-C", "SEG-B", "SEG-B",
        "SEG-D", "SEG-D", "SEG-A", "SEG-A",
    ]
    contributions = [
        {"author": a, "kind": k, "order": i + 1, "segment_id": seg_for_order[i]}
        for i, (a, k) in enumerate(authors)
    ]
    return registry.register_work(
        title="百年会面图卷", kind="长卷", owner_org="甲馆",
        segments=segments, contributions=contributions,
    )


def agreement_payload(work_id: str, **overrides) -> dict:
    payload = {
        "work_id": work_id,
        "lender_org": "甲馆",
        "borrower_org": "乙馆",
        "start_on": "2026-10-01",
        "end_on": "2026-12-31",
        "gallery": "三号厅",
        "max_lux": 50,
        "transport": {"mode": "专车恒温", "escort": "随馆馆员", "temp_c": (18, 22)},
        "insurance": {"insured_value": "议定价值", "coverage": "钉到钉", "policy": "POL-001"},
        "digital_rights": {"web": True, "social_media": False, "print_catalog": True, "term": "展期内"},
    }
    payload.update(overrides)
    return payload


def handover_payload(work_id: str, htype: str, scan: str, on_date: str,
                     from_person="出库员甲", to_person="接收员乙",
                     location="甲馆库房", report=None, linked_segments=None) -> dict:
    pairs = {
        "出库": ("出借馆", "运输方", "甲馆", "长风运输"),
        "到馆": ("运输方", "承借馆", "长风运输", "乙馆"),
        "布展": ("承借馆", "承借馆", "乙馆", "乙馆"),
        "撤展": ("承借馆", "运输方", "乙馆", "长风运输"),
        "归还": ("运输方", "出借馆", "长风运输", "甲馆"),
    }
    fr, tr, forg, torg = pairs[htype]
    return {
        "work_id": work_id,
        "type": htype,
        "scan_code": scan,
        "on_date": on_date,
        "at_location": location,
        "from_party": {"org": forg, "role": fr, "person": from_person},
        "to_party": {"org": torg, "role": tr, "person": to_person},
        "report": report or {"condition": "良好", "image_hashes": ["出库全景照"]},
        "linked_segments": linked_segments or [],
    }


def damage_arrival(registry: LoanRegistry, work_id: str,
                   note="画心左下角发现新增折痕") -> str:
    """出库正常、到馆发现损伤，返回 incident_id。"""
    registry.record_handover(
        handover_payload(work_id, "出库", "SCAN-1", "2026-09-25"))
    registry.record_handover(handover_payload(
        work_id, "到馆", "SCAN-2", "2026-09-27",
        report={"condition": "损伤", "damage_note": note,
                "image_hashes": ["到馆检视照"],
                "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
    ))
    return registry.risk_view(work_id)["open_risks"][0]["incident_id"]


def file_claim(registry: LoanRegistry, incident_id: str, **overrides) -> dict:
    payload = {
        "org": LENDER_ORG,
        "insurer_org": INSURER_ORG,
        "filed_on": "2026-09-28",
    }
    payload.update(overrides)
    return registry.file_claim(incident_id, payload, viewer_org=payload["org"])


def setup_claim(registry: LoanRegistry, work_id: str, incident_id: str) -> str:
    return file_claim(registry, incident_id)["claim_id"]


def insurer_payload(on_date: str, note: str = "", **extra) -> dict:
    payload = {"org": INSURER_ORG, "role": "保险方", "person": "理赔员丁", "on_date": on_date}
    if note:
        payload["note"] = note
    payload.update(extra)
    return payload


def assessment_material(on_date: str, amount: float) -> dict:
    payload = insurer_payload(on_date, summary="现场查勘估损", amount=amount)
    payload["kind"] = "估损"
    return payload


def decision_material(on_date: str, decision: str, accepted: float, deductible: float = 0.0) -> dict:
    payload = insurer_payload(
        on_date, summary=f"{decision}通知",
        decision=decision, accepted_amount=accepted, deductible_amount=deductible,
    )
    payload["kind"] = "核赔结论"
    return payload


def run_assessment_and_partial_decision(registry: LoanRegistry, claim_id: str,
                                        assessed: float = 100000.0,
                                        accepted: float = 90000.0,
                                        deductible: float = 10000.0) -> None:
    registry.add_claim_material(claim_id, assessment_material("2026-10-01", assessed))
    registry.add_claim_material(
        claim_id, decision_material("2026-10-03", "部分认可", accepted, deductible))


def pay_installments(registry: LoanRegistry, claim_id: str, receipts: list[tuple[str, float]]) -> None:
    base = sum(1 for e in registry._claim(claim_id).fund_entries if e.kind == "赔付")
    for idx, (receipt, amount) in enumerate(receipts, start=1):
        registry.add_fund_entry(claim_id, {
            "kind": "赔付", "amount": amount, "receipt_no": receipt,
            "installment_no": base + idx, "on_date": f"2026-10-{base + idx + 4:02d}",
            "org": INSURER_ORG, "role": "保险方", "person": "出纳",
        })


def restoration_plan_material(on_date: str) -> dict:
    return {
        "kind": "修复方案", "org": BORROWER_ORG, "role": "承借馆",
        "person": "修复师丙", "on_date": on_date,
        "summary": "托裱加固折痕，最小干预，可逆材料",
    }


def restoration_review_payload(on_date: str) -> dict:
    return {
        "on_date": on_date,
        "note": "双方馆员与修复师书面复核，修复达到继续交接条件",
        "reviewers": [
            {"org": LENDER_ORG, "role": "出借馆", "person": "馆员甲"},
            {"org": BORROWER_ORG, "role": "承借馆", "person": "馆员乙"},
        ],
    }


class CompositeWorkTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.view = make_long_scroll(self.registry)

    def test_scroll_has_sixteen_authors_in_order_with_segments(self):
        self.assertEqual(len(self.view["contributions"]), 16)
        self.assertEqual([c["order"] for c in self.view["contributions"]], list(range(1, 17)))
        self.assertEqual(len({c["contribution_id"] for c in self.view["contributions"]}), 16)
        self.assertEqual(self.view["segments"][3]["label"], "尾纸题跋")
        by_order = {c["order"]: c for c in self.view["contributions"]}
        self.assertEqual(by_order[13]["kind"], "题跋")
        self.assertEqual(by_order[16]["kind"], "题签")
        # 每位作者都定位到具体区段，而不是只挂在整件作品上。
        self.assertTrue(all(c["segment_id"] for c in self.view["contributions"]))

    def test_duplicate_collaboration_order_rejected(self):
        with self.assertRaises(DomainError):
            self.registry.register_work(
                title="合作山水", kind="合作画", owner_org="甲馆",
                contributions=[
                    {"author": "甲", "kind": "作画", "order": 1},
                    {"author": "乙", "kind": "作画", "order": 1},
                ],
            )

    def test_contribution_must_reference_known_segment(self):
        with self.assertRaises(DomainError):
            self.registry.register_work(
                title="册页", kind="独立作品", owner_org="甲馆",
                segments=[{"label": "扉页"}],
                contributions=[{"author": "甲", "kind": "题跋", "order": 1, "segment_id": "seg-ghost"}],
            )

    def test_independent_work_and_catalog_have_their_own_kinds(self):
        painting = self.registry.register_work("独钓图", "独立作品", "丙馆")
        catalog = self.registry.register_work("百年前展场图录", "历史图录", "丁馆")
        self.assertEqual(painting["work"]["kind"], "独立作品")
        self.assertEqual(catalog["work"]["kind"], "历史图录")


class AgreementTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.work = self.registry.register_work("松鹤图", "合作画", "甲馆")
        self.work_id = self.work["work"]["work_id"]

    def test_agreement_constraints_are_stored_and_exposed(self):
        view = self.registry.create_agreement(agreement_payload(self.work_id))
        self.assertEqual(view["gallery"], "三号厅")
        self.assertEqual(view["max_lux"], 50)
        self.assertFalse(view["digital_rights"]["social_media"])
        self.assertEqual(view["insurance"]["coverage"], "钉到钉")
        risk = self.registry.risk_view(self.work_id)
        self.assertEqual(risk["authorization"]["exhibition_period"],
                         {"start": "2026-10-01", "end": "2026-12-31"})

    def test_invalid_period_and_lux_rejected(self):
        with self.assertRaises(DomainError):
            self.registry.create_agreement(
                agreement_payload(self.work_id, start_on="2027-01-01", end_on="2026-12-31"))
        with self.assertRaises(DomainError):
            self.registry.create_agreement(agreement_payload(self.work_id, max_lux=0))

    def test_cross_museum_reschedule_creates_new_version_and_keeps_old(self):
        old = self.registry.create_agreement(agreement_payload(self.work_id))
        new = self.registry.reschedule_agreement(
            old["agreement_id"],
            {"start_on": "2026-11-15", "end_on": "2027-02-15", "gallery": "五号厅"},
        )
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["supersedes"], old["agreement_id"])
        self.assertEqual(new["gallery"], "五号厅")
        # 旧版条款原样可查。
        self.assertEqual(self.registry.agreements[old["agreement_id"]].gallery, "三号厅")
        work_view = self.registry.get_work_view(self.work_id)
        self.assertEqual(work_view["current_agreement"], new["agreement_id"])
        self.assertEqual(len(work_view["agreement_versions"]), 2)


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("溪山行旅", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))

    def test_full_chain_requires_both_signatures_and_moves_custody(self):
        chain = [
            ("出库", "SCAN-1", "2026-09-25", "甲馆库房"),
            ("到馆", "SCAN-2", "2026-09-27", "乙馆收货区"),
            ("布展", "SCAN-3", "2026-09-30", "乙馆三号厅"),
            ("撤展", "SCAN-4", "2027-01-05", "乙馆三号厅"),
            ("归还", "SCAN-5", "2027-01-07", "甲馆库房"),
        ]
        for htype, scan, day, location in chain:
            view = self.registry.record_handover(
                handover_payload(self.work_id, htype, scan, day, location=location))
            self.assertTrue(view["signed_by_both"])
        custody = self.registry.get_work_view(self.work_id)["custody"]
        self.assertEqual(custody["status"], "已归还")
        self.assertEqual(custody["custodian_role"], "出借馆")

    def test_missing_signature_rejected(self):
        payload = handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25")
        payload["to_party"]["person"] = ""
        with self.assertRaises(DomainError):
            self.registry.record_handover(payload)

    def test_wrong_party_role_rejected(self):
        payload = handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25")
        payload["to_party"]["role"] = "承借馆"
        with self.assertRaises(DomainError):
            self.registry.record_handover(payload)

    def test_skip_step_rejected(self):
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-X", "2026-09-30"))

    def test_duplicate_scan_cannot_create_second_handover(self):
        first = self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-DUP", "2026-09-25"))
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "到馆", "SCAN-DUP", "2026-09-27"))
        # 第一次交接仍然有效，生命周期停在出库之后。
        self.assertEqual(self.registry.get_work_view(self.work_id)["custody"]["status"], "运输中")
        # 被拒绝的重复扫码没有消耗下一步——合法的到馆仍可办理。
        self.registry.record_handover(
            handover_payload(self.work_id, "到馆", "SCAN-OK", "2026-09-27"))
        # 第一次交接记录未被重复扫码覆盖或复制。
        self.assertTrue(first["handover_id"].startswith("handover-"))
        self.assertEqual(
            [h.handover_id for h in self.registry.handovers if h.scan_code == "SCAN-DUP"],
            [first["handover_id"]],
        )

    def test_rejected_scan_before_freeze_is_not_consumed(self):
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        # 错序办理“归还”应失败，且该扫码之后仍可用于它真正对应的步骤。
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "归还", "SCAN-RETRY", "2026-09-26"))
        self.registry.record_handover(
            handover_payload(self.work_id, "到馆", "SCAN-2", "2026-09-27"))
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        self.registry.record_handover(
            handover_payload(self.work_id, "撤展", "SCAN-4", "2027-01-05"))
        done = self.registry.record_handover(
            handover_payload(self.work_id, "归还", "SCAN-RETRY", "2027-01-07"))
        self.assertEqual(done["type"], "归还")


class DamageAndFreezeTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", "甲馆")
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))

    def test_damage_freezes_all_following_handovers_and_preserves_hashes(self):
        before = "a" * 64
        after = "b" * 64
        damaged = self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={
                "condition": "损伤",
                "damage_note": "画心左下角发现新增折痕",
                "image_hashes": ["到馆检视照"],
                "before_hashes": [before],
                "after_hashes": [after],
            },
        ))
        self.assertTrue(damaged["frozen"])
        self.assertEqual(damaged["condition"]["before_hashes"], [before])
        self.assertEqual(damaged["condition"]["after_hashes"], [after])
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])

        # 冻结后任何后续交接都不得成立，换一个新扫码也不行。
        with self.assertRaises(ConflictError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-09-30"))
        risk = self.registry.risk_view(self.work_id)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["before_hashes"], [before])

    def test_damage_report_requires_note(self):
        with self.assertRaises(DomainError):
            self.registry.record_handover(handover_payload(
                self.work_id, "到馆", "SCAN-2", "2026-09-27",
                report={"condition": "损伤", "image_hashes": ["x"]},
            ))

    def test_claim_close_and_unfreeze_are_judged_separately(self):
        """修复完成不等于自动解冻，理赔关闭也不解冻；必须双闸门同时满足。"""
        self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "边缘轻微磨损",
                    "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
        ))
        incident_id = self.registry.risk_view(self.work_id)["open_risks"][0]["incident_id"]
        claim_id = setup_claim(self.registry, self.work_id, incident_id)
        run_assessment_and_partial_decision(self.registry, claim_id)
        pay_installments(self.registry, claim_id, [("RCPT-1", 60000)])

        # 只过修复闸门：仍冻结。
        self.registry.add_claim_material(claim_id, restoration_plan_material("2026-10-02"))
        self.registry.submit_restoration_review(incident_id, restoration_review_payload("2026-10-10"))
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])
        gate = self.registry.incident_view(incident_id)
        self.assertTrue(gate["restoration_reviewed"])
        self.assertFalse(gate["custody_confirmed"])

        # 承借馆想在修复完成后直接解除冻结：不允许，必须先由当前保管方签认。
        with self.assertRaises(DomainError):
            self.registry.record_handover(
                handover_payload(self.work_id, "布展", "SCAN-3", "2026-10-11"))

        # 理赔关闭本身也不解冻。
        pay_installments(self.registry, claim_id, [("RCPT-2", 30000)])
        self.registry.close_claim(claim_id, insurer_payload("2026-10-12", note="赔款付清，关闭"))
        self.assertTrue(self.registry.claim_view(claim_id)["closed"])
        self.assertTrue(self.registry.get_work_view(self.work_id)["frozen"])

        # 第二道闸门（当前保管方为承借馆）签认后才解冻。
        self.registry.confirm_custody(incident_id, {
            "org": "乙馆", "role": "承借馆", "person": "保管员乙",
            "on_date": "2026-10-12", "note": "确认修复后现状，接管后续保管",
        })
        self.assertFalse(self.registry.get_work_view(self.work_id)["frozen"])
        self.registry.record_handover(
            handover_payload(self.work_id, "布展", "SCAN-3", "2026-10-13"))

    def test_restoration_review_requires_both_lender_and_borrower(self):
        self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "磨损",
                    "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
        ))
        incident_id = self.registry.risk_view(self.work_id)["open_risks"][0]["incident_id"]
        self.registry.add_claim_material(
            setup_claim(self.registry, self.work_id, incident_id),
            restoration_plan_material("2026-10-02"),
        )
        # 只有承借馆一方签字：拒绝。
        with self.assertRaises(DomainError):
            self.registry.submit_restoration_review(incident_id, {
                "on_date": "2026-10-10", "note": "复核通过",
                "reviewers": [{"org": "乙馆", "role": "承借馆", "person": "乙"}],
            })
        # 运输方不能替代出借馆签字。
        with self.assertRaises(DomainError):
            self.registry.submit_restoration_review(incident_id, {
                "on_date": "2026-10-10", "note": "复核通过",
                "reviewers": [
                    {"org": "长风运输", "role": "运输方", "person": "司机"},
                    {"org": "乙馆", "role": "承借馆", "person": "乙"},
                ],
            })

    def test_custody_confirmation_must_come_from_current_custodian(self):
        self.registry.record_handover(handover_payload(
            self.work_id, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "磨损",
                    "before_hashes": ["c" * 64], "after_hashes": ["d" * 64]},
        ))
        incident_id = self.registry.risk_view(self.work_id)["open_risks"][0]["incident_id"]
        setup_claim(self.registry, self.work_id, incident_id)
        # 事故停在到馆之后，保管方是承借馆；运输方来签认保管责任应被拒绝。
        with self.assertRaises(DomainError):
            self.registry.confirm_custody(incident_id, {
                "org": "长风运输", "role": "运输方", "person": "司机",
                "on_date": "2026-10-12", "note": "我们接管",
            })


class ClaimBaselineTest(unittest.TestCase):
    """报案必须锁定事故时有效的保险条款、双方签认与图像摘要。"""

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", LENDER_ORG)
        self.work_id = work["work"]["work_id"]
        self.agreement = self.registry.create_agreement(agreement_payload(self.work_id))
        self.incident_id = damage_arrival(self.registry, self.work_id)

    def test_claim_locks_insurance_terms_signatures_and_image_summary(self):
        claim = file_claim(self.registry, self.incident_id)
        baseline = claim["baseline"]
        self.assertTrue(baseline["locked"])
        # 事故时有效的是 v1 协议（钉到钉），且双方已签认
        self.assertEqual(baseline["agreement_version"]["version"], 1)
        self.assertEqual(baseline["agreement_version"]["insurance"]["coverage"], "钉到钉")
        self.assertTrue(baseline["signatures"])
        self.assertEqual(baseline["image_summary"]["before_hashes"], ["a" * 64])
        self.assertEqual(baseline["image_summary"]["after_hashes"], ["b" * 64])
        handover_hashes = baseline["image_summary"]["handover_image_hashes"]
        self.assertIn(LoanRegistry._image_hash("到馆检视照"), handover_hashes)
        # 四方按角色固定
        self.assertEqual(claim["parties"]["保险方"], INSURER_ORG)
        self.assertEqual(claim["parties"]["出借馆"], LENDER_ORG)
        self.assertEqual(claim["parties"]["承借馆"], BORROWER_ORG)
        self.assertEqual(claim["parties"]["运输方"], TRANSPORTER_ORG)

    def test_duplicate_report_rejected(self):
        file_claim(self.registry, self.incident_id)
        with self.assertRaises(ConflictError):
            file_claim(self.registry, self.incident_id)

    def test_non_party_cannot_file_claim(self):
        with self.assertRaises(DomainError):
            self.registry.file_claim(self.incident_id, {
                "org": "无关拍卖行", "insurer_org": INSURER_ORG, "filed_on": "2026-09-28",
            }, viewer_org="无关拍卖行")

    def test_later_reschedule_does_not_change_locked_coverage(self):
        """出借馆事后拿出新协议、协议后来改期，都不改变原保障范围。"""
        claim = file_claim(self.registry, self.incident_id)
        self.registry.reschedule_agreement(
            self.agreement["agreement_id"],
            {"insurance": {"insured_value": "议定价值", "coverage": "馆内责任险", "policy": "POL-002"}},
        )
        view = self.registry.claim_view(claim["claim_id"], LENDER_ORG)
        self.assertEqual(view["baseline"]["agreement_version"]["insurance"]["coverage"], "钉到钉")
        # 当前授权确实已是新版，但报案基线不动
        risk = self.registry.risk_view(self.work_id, LENDER_ORG)
        self.assertEqual(risk["authorization"]["insurance"]["coverage"], "馆内责任险")

    def test_claim_without_effective_agreement_rejected(self):
        registry = LoanRegistry()
        work = registry.register_work("无协议作品", "独立作品", LENDER_ORG)
        wid = work["work"]["work_id"]
        # 无协议直接出库/到馆出损伤，不能报案
        registry.record_handover(
            handover_payload(wid, "出库", "SCAN-1", "2026-09-25"))
        registry.record_handover(handover_payload(
            wid, "到馆", "SCAN-2", "2026-09-27",
            report={"condition": "损伤", "damage_note": "折痕",
                    "before_hashes": ["a" * 64], "after_hashes": ["b" * 64]},
        ))
        incident_id = registry.risk_view(wid)["open_risks"][0]["incident_id"]
        with self.assertRaises(DomainError):
            registry.file_claim(incident_id, {
                "org": LENDER_ORG, "insurer_org": INSURER_ORG, "filed_on": "2026-09-28",
            })


class ClaimAdditionsTest(unittest.TestCase):
    """四方只能按自身角色追加；补充材料形成新版本，不替换已引用证据。"""

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", LENDER_ORG)
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.incident_id = damage_arrival(self.registry, self.work_id)
        self.claim_id = setup_claim(self.registry, self.work_id, self.incident_id)

    def test_each_role_can_only_add_its_own_kind(self):
        # 承借馆不能替保险方估损
        with self.assertRaises(DomainError):
            self.registry.add_claim_material(self.claim_id, {
                "kind": "估损", "org": BORROWER_ORG, "role": "承借馆",
                "person": "乙", "on_date": "2026-10-01",
                "summary": "我们自己估 5 万", "amount": 50000,
            })
        # 运输方不能提交修复方案
        with self.assertRaises(DomainError):
            self.registry.add_claim_material(self.claim_id, {
                "kind": "修复方案", "org": TRANSPORTER_ORG, "role": "运输方",
                "person": "司机", "on_date": "2026-10-01", "summary": "运回去擦擦",
            })
        # 角色与报案登记机构不符也不行（别的运输公司不能冒充）
        with self.assertRaises(DomainError):
            self.registry.add_claim_material(self.claim_id, {
                "kind": "责任意见", "org": "别的运输公司", "role": "运输方",
                "person": "陌生人", "on_date": "2026-10-01", "summary": "与我无关",
            })

    def test_dispute_restoration_plan_and_liability_append_as_versions(self):
        # 运输方引用另一版交接照片提出责任意见
        self.registry.add_claim_material(self.claim_id, {
            "kind": "责任意见", "org": TRANSPORTER_ORG, "role": "运输方",
            "person": "车长老路", "on_date": "2026-09-29",
            "summary": "引用到馆卸货另一版照片，认为外包装到馆前完好",
            "image_hashes": ["运输方交接照片"],
        })
        # 出借馆对损伤提出异议
        self.registry.add_claim_material(self.claim_id, {
            "kind": "异议", "org": LENDER_ORG, "role": "出借馆",
            "person": "馆员甲", "on_date": "2026-09-30",
            "summary": "不认可运输方照片版本，损伤发生在运输环节",
        })
        # 承借馆给修复方案，随后修订：旧版保留
        plan = self.registry.add_claim_material(
            self.claim_id, restoration_plan_material("2026-10-02"))
        revised = self.registry.add_claim_material(self.claim_id, {
            **restoration_plan_material("2026-10-04"),
            "summary": "按纤维检测改为仅局部加固（修订版）",
            "supersedes_seq": plan["additions"][-1]["seq"],
        })
        claim = self.registry.claim_view(self.claim_id, BORROWER_ORG)
        kinds = [a["kind"] for a in claim["additions"]]
        self.assertEqual(kinds, ["责任意见", "异议", "修复方案", "修复方案"])
        self.assertEqual(revised["additions"][-1]["supersedes_seq"], 3)
        # 被取代的第 3 版仍然原样可查
        self.assertEqual(claim["additions"][2]["summary"], "托裱加固折痕，最小干预，可逆材料")
        self.assertNotIn("redacted", claim["additions"][2])

    def test_no_additions_after_close(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        pay_installments(self.registry, self.claim_id, [("R-1", 90000)])
        self.registry.close_claim(self.claim_id, insurer_payload("2026-10-12", note="结清关闭"))
        with self.assertRaises(ConflictError):
            self.registry.add_claim_material(
                self.claim_id, restoration_plan_material("2026-10-13"))


class ClaimFundsTest(unittest.TestCase):
    """部分认可、免赔额、分期赔付与追偿还需金额守恒。"""

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", LENDER_ORG)
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.incident_id = damage_arrival(self.registry, self.work_id)
        self.claim_id = setup_claim(self.registry, self.work_id, self.incident_id)

    def test_deductible_installments_and_recovery_keep_amounts_consistent(self):
        run_assessment_and_partial_decision(
            self.registry, self.claim_id,
            assessed=100000, accepted=90000, deductible=10000)
        claim = self.registry.claim_view(self.claim_id, INSURER_ORG)
        # 免赔额在决定作出时即作为负向分录落账
        self.assertEqual([e["kind"] for e in claim["fund_entries"]], ["免赔额"])
        self.assertEqual(claim["fund_totals"]["deductible"], 10000)

        # 分期赔付 6 万 + 3 万
        pay_installments(self.registry, self.claim_id, [("RCPT-1", 60000), ("RCPT-2", 30000)])
        totals = self.registry.fund_totals(self.claim_id)
        self.assertEqual(totals["paid"], 90000)
        self.assertEqual(totals["outstanding"], 0)

        # 之后向运输方追偿收回 4 万，净额相应减少
        self.registry.add_fund_entry(self.claim_id, {
            "kind": "追偿", "amount": 40000, "receipt_no": "REC-1",
            "on_date": "2026-11-02",
            "org": INSURER_ORG, "role": "保险方", "person": "追偿专员",
        })
        totals = self.registry.fund_totals(self.claim_id)
        self.assertEqual(totals["recovered"], 40000)
        self.assertEqual(totals["net_paid"], 50000)
        # 追偿不能超过已赔未冲减部分
        with self.assertRaises(DomainError):
            self.registry.add_fund_entry(self.claim_id, {
                "kind": "追偿", "amount": 50001, "receipt_no": "REC-2",
                "on_date": "2026-11-03",
                "org": INSURER_ORG, "role": "保险方", "person": "追偿专员",
            })

    def test_duplicate_receipt_does_not_double_count(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        pay_installments(self.registry, self.claim_id, [("RCPT-DUP", 45000)])
        # 同一回执号再来一次（哪怕标成第 2 期）：拒绝，不产生第二条分录
        with self.assertRaises(ConflictError):
            self.registry.add_fund_entry(self.claim_id, {
                "kind": "赔付", "amount": 45000, "receipt_no": "RCPT-DUP",
                "installment_no": 2, "on_date": "2026-10-08",
                "org": INSURER_ORG, "role": "保险方", "person": "出纳",
            })
        # 同一分期重复入账也拒绝
        with self.assertRaises(ConflictError):
            self.registry.add_fund_entry(self.claim_id, {
                "kind": "赔付", "amount": 45000, "receipt_no": "RCPT-OTHER",
                "installment_no": 1, "on_date": "2026-10-08",
                "org": INSURER_ORG, "role": "保险方", "person": "出纳",
            })
        totals = self.registry.fund_totals(self.claim_id)
        self.assertEqual(totals["paid"], 45000)

    def test_payment_cannot_exceed_accepted_amount(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        with self.assertRaises(DomainError):
            pay_installments(self.registry, self.claim_id, [("RCPT-X", 90001)])

    def test_concurrent_decisions_only_one_is_recorded(self):
        """两个核赔决定并发到达：只有一个生效，不重复扣免赔额。"""
        self.registry.add_claim_material(
            self.claim_id, assessment_material("2026-10-01", 100000))
        decision = decision_material("2026-10-03", "部分认可", 90000, 10000)
        outcomes = []

        def decide():
            try:
                self.registry.add_claim_material(self.claim_id, dict(decision))
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=decide) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        totals = self.registry.fund_totals(self.claim_id)
        self.assertEqual(totals["deductible"], 10000)
        self.assertEqual(self.registry.claim_view(self.claim_id)["stage"], "部分认可")

    def test_cannot_pay_before_decision(self):
        with self.assertRaises(ConflictError):
            pay_installments(self.registry, self.claim_id, [("RCPT-0", 1000)])

    def test_rejected_decision_allows_close_without_payment(self):
        self.registry.add_claim_material(
            self.claim_id, assessment_material("2026-10-01", 100000))
        self.registry.add_claim_material(
            self.claim_id, decision_material("2026-10-03", "拒赔", 0))
        self.assertEqual(self.registry.fund_totals(self.claim_id)["outstanding"], 0)
        closed = self.registry.close_claim(
            self.claim_id, insurer_payload("2026-10-05", note="拒赔关闭"))
        self.assertTrue(closed["closed"])

    def test_close_blocked_while_installments_outstanding(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        pay_installments(self.registry, self.claim_id, [("RCPT-1", 60000)])
        with self.assertRaises(ConflictError):
            self.registry.close_claim(
                self.claim_id, insurer_payload("2026-10-12", note="想提前关闭"))

    def test_full_approval_amount_must_match_assessed_minus_deductible(self):
        self.registry.add_claim_material(
            self.claim_id, assessment_material("2026-10-01", 100000))
        with self.assertRaises(DomainError):
            self.registry.add_claim_material(
                self.claim_id, decision_material("2026-10-03", "全额认可", 80000, 10000))


class RiskTraceAndRedactionTest(unittest.TestCase):
    """风险追溯并列四要素；未授权机构脱敏。"""

    def setUp(self):
        self.registry = LoanRegistry()
        work = self.registry.register_work("秋林群鹿", "独立作品", LENDER_ORG)
        self.work_id = work["work"]["work_id"]
        self.registry.create_agreement(agreement_payload(self.work_id))
        self.incident_id = damage_arrival(self.registry, self.work_id)
        self.claim_id = setup_claim(self.registry, self.work_id, self.incident_id)

    def test_risk_view_presents_all_four_facets_side_by_side(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        pay_installments(self.registry, self.claim_id, [("RCPT-1", 60000)])
        risk = self.registry.risk_view(self.work_id, LENDER_ORG)
        block = risk["open_risks"][0]
        # 1) 开放损伤
        self.assertIn("折痕", block["note"])
        self.assertEqual(block["before_hashes"], ["a" * 64])
        # 2) 理赔阶段
        self.assertEqual(block["claim_stage"], "部分认可")
        self.assertFalse(block["claim_closed"])
        # 3) 缺少的签认
        missing = block["missing_signatures"]
        self.assertIn("承借馆修复方案", missing)
        self.assertTrue(any(m.startswith("修复复核") for m in missing))
        self.assertTrue(any(m.startswith("保管责任") for m in missing))
        self.assertTrue(any("分期赔付" in m for m in missing))
        # 4) 资金变化
        kinds = [(e["kind"], e["amount"]) for e in block["fund_changes"]]
        self.assertIn(("免赔额", 10000), kinds)
        self.assertIn(("赔付", 60000), kinds)
        self.assertEqual(block["fund_totals"]["outstanding"], 30000)
        # 闸门状态并列
        self.assertFalse(block["gates"]["restoration_reviewed"])
        self.assertFalse(block["gates"]["custody_confirmed"])

    def test_unauthorized_org_cannot_see_sensitive_material(self):
        run_assessment_and_partial_decision(self.registry, self.claim_id)
        self.registry.add_claim_material(
            self.claim_id, restoration_plan_material("2026-10-02"))
        # 未授权机构看理赔：基线图像/条款、材料摘要全部脱敏
        view = self.registry.claim_view(self.claim_id, "某小报")
        self.assertFalse(view["authorized"])
        self.assertTrue(view["baseline"]["image_summary"]["redacted"])
        self.assertTrue(view["baseline"]["agreement_version"]["redacted"])
        self.assertTrue(all(a.get("redacted") for a in view["additions"]))
        # 看不到其他三方
        self.assertEqual(set(view["parties"].keys()), {"保险方"})
        # 风险视图同样脱敏
        risk = self.registry.risk_view(self.work_id, "某小报")
        block = risk["open_risks"][0]
        self.assertTrue(block["redacted"])
        self.assertNotIn("before_hashes", block)
        self.assertEqual(block["fund_changes"], [])
        # 但阶段与缺口这种非敏感状态仍可见，便于协作方知道卡在哪
        self.assertEqual(block["claim_stage"], "部分认可")
        self.assertTrue(block["missing_signatures"])

    def test_parties_see_full_material(self):
        for org in (INSURER_ORG, LENDER_ORG, BORROWER_ORG, TRANSPORTER_ORG):
            view = self.registry.claim_view(self.claim_id, org)
            self.assertTrue(view["authorized"], org)
            self.assertEqual(view["baseline"]["image_summary"]["before_hashes"], ["a" * 64])
            self.assertEqual(set(view["parties"].keys()),
                             {"保险方", "出借馆", "承借馆", "运输方"})


class LabelSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.scroll = make_long_scroll(self.registry)
        self.work_id = self.scroll["work"]["work_id"]
        self.segment_ids = [s["segment_id"] for s in self.scroll["segments"]]
        agreement = self.registry.create_agreement(agreement_payload(self.work_id))
        self.agreement_id = agreement["agreement_id"]

    def test_published_label_locks_evidence_and_corrections_do_not_alter_old_version(self):
        citations = [{"ref": self.segment_ids[1], "note": "画心甲的合笔位置"}]
        self.registry.create_label(self.work_id, "百年会面：两位画家的合作见证", citations)
        published = self.registry.publish_label(self.work_id, "2026-10-01")
        self.assertTrue(published["frozen"])
        snapshot = published["evidence_snapshot"]
        self.assertEqual(len(snapshot["contributions"]), 16)
        self.assertEqual(snapshot["agreement_version"]["version"], 1)
        self.assertEqual(snapshot["custody"]["status"], "在库")

        # 发布后发生学术更正与跨馆改期。
        self.registry.reschedule_agreement(
            self.agreement_id, {"gallery": "七号厅", "start_on": "2026-11-01"})
        corrected = self.registry.correct_label(
            self.work_id, "百年会面：据新发现信札修订合笔顺序",
            [{"ref": self.segment_ids[2], "note": "画心乙主笔改订"}],
        )
        self.assertEqual(corrected["version"], 2)
        self.assertFalse(corrected["frozen"])

        # 旧版展签与证据快照保持发布时的内容。
        old = self.registry.label_version(self.work_id, 1)
        self.assertEqual(old["status"], "已发布")
        self.assertEqual(old["evidence_snapshot"]["agreement_version"]["gallery"], "三号厅")
        self.assertEqual(old["narrative"], "百年会面：两位画家的合作见证")
        self.assertEqual(len(old["evidence_snapshot"]["contributions"]), 16)

        # 新版发布后才锁定当时的新证据。
        new_published = self.registry.publish_label(self.work_id, "2026-11-01")
        self.assertEqual(new_published["evidence_snapshot"]["agreement_version"]["gallery"], "七号厅")
        # 最新版默认查询，旧版仍可按版本号取回。
        self.assertEqual(self.registry.label_version(self.work_id)["version"], 2)

    def test_cannot_publish_same_version_twice(self):
        self.registry.create_label(self.work_id, "展签草稿", [])
        self.registry.publish_label(self.work_id, "2026-10-01")
        with self.assertRaises(ConflictError):
            self.registry.publish_label(self.work_id, "2026-10-02")

    def test_damage_appears_as_open_risk_in_snapshot(self):
        self.registry.create_label(self.work_id, "有局部争议的长卷", [])
        # 出库即发现画心乙局部问题。
        self.registry.record_handover(handover_payload(
            self.work_id, "出库", "SCAN-1", "2026-09-25",
            linked_segments=[self.segment_ids[2]],
            report={"condition": "损伤", "damage_note": "画心乙疑似新霉点",
                    "before_hashes": ["e" * 64], "after_hashes": ["f" * 64]},
        ))
        published = self.registry.publish_label(self.work_id, "2026-09-26")
        risks = published["evidence_snapshot"]["open_risks"]
        self.assertEqual(len(risks), 1)
        self.assertIn("霉点", risks[0]["note"])
        self.assertTrue(published["evidence_snapshot"]["custody"])


class SegmentDisputeTest(unittest.TestCase):
    def setUp(self):
        self.registry = LoanRegistry()
        self.scroll = make_long_scroll(self.registry)
        self.work_id = self.scroll["work"]["work_id"]
        self.segments = {s["label"]: s["segment_id"] for s in self.scroll["segments"]}
        self.registry.create_agreement(agreement_payload(self.work_id))

    def test_locate_segment_reaches_work_contributions_custody_and_risk(self):
        seg_b = self.segments["画心乙"]
        self.registry.record_handover(handover_payload(
            self.work_id, "出库", "SCAN-1", "2026-09-25",
            linked_segments=[seg_b],
            report={"condition": "损伤", "damage_note": "画心乙局部水渍成因存疑",
                    "before_hashes": ["1" * 64], "after_hashes": ["2" * 64]},
        ))
        located = self.registry.locate_segment(self.work_id, seg_b)
        # 从争议区段可回到实体。
        self.assertEqual(located["work"]["work_id"], self.work_id)
        # 可定位到该区段上的作者贡献（画心乙上有多位作画者）。
        self.assertTrue(located["contributions"])
        self.assertTrue({c["author"] for c in located["contributions"]} & {"赵某", "吴某"})
        self.assertTrue(all(c["kind"] == "作画" for c in located["contributions"]))
        self.assertEqual(
            [c["order"] for c in located["contributions"]],
            sorted(c["order"] for c in located["contributions"]),
        )
        # 当前保管责任明确（出库后由运输方承担）。
        self.assertEqual(located["custody"]["custodian_role"], "运输方")
        # 风险未解除且处于冻结。
        self.assertTrue(located["frozen"])
        self.assertEqual(len(located["segment_incidents"]), 1)
        self.assertFalse(located["segment_incidents"][0]["resolved"])

    def test_risk_view_carries_authorization_scope_during_dispute(self):
        self.registry.record_handover(
            handover_payload(self.work_id, "出库", "SCAN-1", "2026-09-25"))
        risk = self.registry.risk_view(self.work_id)
        self.assertEqual(risk["authorization"]["digital_rights"]["term"], "展期内")
        self.assertEqual(risk["custody"]["custodian_role"], "运输方")
        self.registry.reschedule_agreement(
            risk["authorization"]["agreement_id"], {"max_lux": 30})
        risk2 = self.registry.risk_view(self.work_id)
        self.assertEqual(risk2["authorization"]["max_lux"], 30)
        self.assertEqual(risk2["authorization"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
