"""馆际作品借展的领域核心。

只依赖标准库，集中表达五类业务规则：

1. 复合作品结构：实体作品 → 组成区段 → 作者贡献（含合作顺序、题跋）。
2. 借展协议：约束展期、展厅、照度、运输、保险与数字传播用途。
3. 状态交接：出库/到馆/布展/撤展/归还由交接双方签认；重复扫码幂等；
   发现损伤立即冻结后续动作并保全前后图像哈希。
4. 保险理赔：报案锁定事故时有效的协议保险条款、双方签认与图像摘要；
   保险方/出借馆/承借馆/运输方只能按自身角色追加材料，补充材料逐版
   保存、不替换已被引用的证据；资金以追加分录保持金额守恒；理赔关闭
   与作品解冻分别判断，修复复核与保管责任同时满足才恢复交接。
5. 策展展签：发布日期确认后锁定证据快照，后来的学术更正只产生新版，
   不改变旧版展签；任意版本都可从展签定位到实体、贡献区段、当前保管
   责任、授权范围与未解除风险。
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

ROLES = ("出借馆", "承借馆", "运输方", "策展人")

# 理赔参与方：保险方不参与交接，但参与估损、核赔与追偿
CLAIM_ROLES = ("保险方", "出借馆", "承借馆", "运输方")

WORK_KINDS = ("独立作品", "合作画", "历史图录", "长卷")

CONTRIBUTION_KINDS = ("作画", "题跋", "书引首", "鉴藏印", "题签")

# 追加材料类型 → 允许提交的角色。各方只能按自身角色追加，
# 异议任何一方都可提出；估损/核赔结论只归保险方。
ADDITION_KINDS = (
    "估损",          # 保险方
    "核赔结论",      # 保险方（部分认可/拒赔/全额认可）
    "异议",          # 任一理赔参与方
    "修复方案",      # 承借馆
    "责任意见",      # 运输方
)
ADDITION_ROLES: dict[str, tuple[str, ...]] = {
    "估损": ("保险方",),
    "核赔结论": ("保险方",),
    "异议": CLAIM_ROLES,
    "修复方案": ("承借馆",),
    "责任意见": ("运输方",),
}

# 资金分录方向：流入案件余额为正（赔付/追偿收回），流出为负（免赔额扣减）。
# 全部以追加分录表达，不回改历史金额。
FUND_ENTRY_KINDS = ("赔付", "免赔额", "追偿")

# 理赔阶段（只随决定推进，不随补充材料回退）
CLAIM_STAGES = ("待估损", "估损中", "部分认可", "全额认可", "拒赔", "已关闭")

# 交接类型固定，顺序即借展生命周期
HANDOVER_TYPES = ("出库", "到馆", "布展", "撤展", "归还")

# 每次交接后实体所处的保管状态
STATUS_AFTER = {
    "出库": "运输中",
    "到馆": "待布展",
    "布展": "展出中",
    "撤展": "待归还",
    "归还": "已归还",
}

# 各交接类型的法定交出方 / 接收方角色
TRANSFER_PAIRS = {
    "出库": ("出借馆", "运输方"),
    "到馆": ("运输方", "承借馆"),
    "布展": ("承借馆", "承借馆"),
    "撤展": ("承借馆", "运输方"),
    "归还": ("运输方", "出借馆"),
}

LABEL_STATUS = ("草拟", "已发布", "已更正")


class DomainError(ValueError):
    """请求违反领域规则（作为 4xx 返回给调用方）。"""


class ConflictError(DomainError):
    """状态冲突（重复交接、已冻结、已锁定等），语义上是 409。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    _new_id.counter[prefix] = _new_id.counter.get(prefix, 0) + 1
    return f"{prefix}-{_new_id.counter[prefix]:04d}"


_new_id.counter = {}  # type: ignore[attr-defined]


def _transport_org(handover: "Handover") -> str:
    """从一次事故交接中辨认运输方机构（出库/到馆/撤展/归还上都有运输方）。"""
    for party in (handover.from_party, handover.to_party):
        if party.role == "运输方":
            return party.org
    return ""


@dataclass
class Work:
    """作品实体：一件可以被独立出借、运输、投保的物理对象。"""

    title: str
    kind: str
    owner_org: str  # 当前权属机构
    work_id: str = field(default_factory=lambda: _new_id("work"))
    segments: list["Segment"] = field(default_factory=list)
    contributions: list["Contribution"] = field(default_factory=list)

    def require_segment(self, segment_id: str) -> "Segment":
        for segment in self.segments:
            if segment.segment_id == segment_id:
                return segment
        raise DomainError(f"区段 {segment_id} 不属于作品 {self.work_id}")

    def to_ref(self) -> dict[str, str]:
        return {"work_id": self.work_id, "title": self.title, "kind": self.kind}


@dataclass
class Segment:
    """组成区段：长卷/合作画上可独立辨认的物理局部。"""

    label: str
    start_cm: float = 0.0
    end_cm: float = 0.0
    segment_id: str = field(default_factory=lambda: _new_id("seg"))
    note: str = ""


@dataclass
class Contribution:
    """作者贡献：谁、以何种方式、按什么合作顺序、落在哪个区段。"""

    author: str
    kind: str
    order: int
    segment_id: Optional[str] = None
    contribution_id: str = field(default_factory=lambda: _new_id("ctrb"))


@dataclass
class Agreement:
    """借展协议：约束条件随协议保存，授权范围由这里派生。"""

    agreement_id: str
    work_id: str
    lender_org: str
    borrower_org: str
    start_on: str  # 展期起（YYYY-MM-DD）
    end_on: str  # 展期止
    gallery: str
    max_lux: int
    transport: dict[str, Any]
    insurance: dict[str, Any]
    digital_rights: dict[str, Any]
    version: int = 1
    supersedes: Optional[str] = None
    # 协议版本在登记全局事件流中的序号：用于判定“事故发生时哪一版已存在”
    created_seq: int = 0

    def authorization_scope(self) -> dict[str, Any]:
        """从协议条款派生当前授权范围，供展签与风险视图引用。"""
        return {
            "agreement_id": self.agreement_id,
            "version": self.version,
            "exhibition_period": {"start": self.start_on, "end": self.end_on},
            "gallery": self.gallery,
            "max_lux": self.max_lux,
            "transport": self.transport,
            "insurance": self.insurance,
            "digital_rights": self.digital_rights,
        }


@dataclass
class Signature:
    org: str
    role: str
    person: str


@dataclass
class ConditionReport:
    """状态报告：交接时由双方签认，发现损伤时带损伤说明与前后图像哈希。"""

    condition: str  # 良好 / 损伤
    image_hashes: list[str]
    damage_note: str = ""
    before_hashes: list[str] = field(default_factory=list)
    after_hashes: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Handover:
    handover_id: str
    work_id: str
    type: str
    scan_code: str
    from_party: Signature
    to_party: Signature
    report: ConditionReport
    on_date: str
    at_location: str
    linked_segments: list[str] = field(default_factory=list)
    event_seq: int = 0

    @property
    def damaged(self) -> bool:
        return self.report.condition == "损伤" or bool(self.report.damage_note)


@dataclass
class Incident:
    """损伤事件：冻结后所有未完成交接，图像哈希作为证据保全。

    解冻与理赔关闭分别判断：修复经双馆书面复核（restoration_reviewed）
    与当前保管方签认保管责任（custody_confirmed）同时满足才解冻，
    二者均不以理赔是否关闭为前提。
    """

    incident_id: str
    work_id: str
    handover_id: str
    on_date: str
    note: str
    before_hashes: list[str]
    after_hashes: list[str]
    resolved: bool = False
    resolution_note: str = ""
    restoration_reviewed: bool = False
    restoration_review: dict[str, Any] = field(default_factory=dict)
    custody_confirmed: bool = False
    custody_confirmation: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClaimAddition:
    """理赔追加材料：只能追加，形成新版本，永不替换已被引用的证据。"""

    addition_id: str
    seq: int
    kind: str            # ADDITION_KINDS 之一
    org: str
    role: str
    person: str
    summary: str
    on_date: str
    # 材料可带新的图像/文件摘要，但只能新增，不能覆盖报案快照
    image_hashes: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    supersedes_seq: Optional[int] = None  # 本方同类材料的上一版（仅标记，旧版保留）


@dataclass
class FundEntry:
    """资金分录：赔付（可分期）、免赔额、追偿收回，全部追加、守恒。"""

    entry_id: str
    seq: int
    kind: str            # 赔付 / 免赔额 / 追偿
    amount: float        # 非负；方向由 kind 决定
    currency: str
    org: str             # 提交/付款机构
    person: str
    on_date: str
    receipt_no: str = ""
    note: str = ""
    installment_no: int = 1   # 同一笔赔付的分期序号


@dataclass
class Claim:
    """保险理赔：报案时锁定事故时基线，之后只追加，不回写。"""

    claim_id: str
    incident_id: str
    work_id: str
    filed_on: str
    # 报案锁定的事故时基线（即使协议后来改期、对方后来换照片，均不变）
    agreement_snapshot: dict[str, Any]
    handover_snapshot: dict[str, Any]
    image_summary: dict[str, Any]
    # 参与机构按角色固定，其他机构视为未授权
    parties: dict[str, str]  # role -> org
    additions: list[ClaimAddition] = field(default_factory=list)
    fund_entries: list[FundEntry] = field(default_factory=list)
    receipt_nos: set[str] = field(default_factory=set)
    stage: str = "待估损"
    closed: bool = False
    closed_on: Optional[str] = None
    close_note: str = ""
    currency: str = "CNY"
    assessed_amount: Optional[float] = None
    accepted_amount: Optional[float] = None
    deductible_amount: Optional[float] = None
    decision_seq: Optional[int] = None  # 决定引用的核赔结论版本


@dataclass
class LabelVersion:
    """展签的一个不可变版本。发布即锁定证据快照。"""

    version: int
    status: str
    narrative: str
    citations: list[dict[str, str]]
    evidence: dict[str, Any]
    published_on: Optional[str]
    frozen: bool


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class LoanRegistry:
    """保存全部借展记录并强制业务规则。

    对外使用命令式方法（register_work / record_handover / ...），
    查询通过 get_work_view / label_version / risk_view 等只读视图。
    """

    def __init__(self) -> None:
        self.works: dict[str, Work] = {}
        self.agreements: dict[str, Agreement] = {}
        self._agreement_history: dict[str, list[str]] = {}  # work_id → 协议ID（含历史版本）
        self.handovers: list[Handover] = []
        self._scan_codes: set[str] = set()
        self.incidents: list[Incident] = []
        self._frozen_works: set[str] = set()
        self.labels: dict[str, list[LabelVersion]] = {}
        self.claims: dict[str, Claim] = {}
        self._claim_by_incident: dict[str, str] = {}  # incident_id → claim_id
        # 报案、核赔决定与资金记账串行化：并发决定/重复回执不得多记赔款
        self._lock = threading.RLock()
        self._event_seq = 0

    def _next_event_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    # -- 作品结构 ----------------------------------------------------------

    def register_work(
        self,
        title: str,
        kind: str,
        owner_org: str,
        segments: Optional[list[dict[str, Any]]] = None,
        contributions: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if not title or not title.strip():
            raise DomainError("作品名称不能为空")
        if kind not in WORK_KINDS:
            raise DomainError(f"作品类型须为 {WORK_KINDS} 之一")
        if not owner_org or not owner_org.strip():
            raise DomainError("必须登记当前权属机构")

        work = Work(title=title.strip(), kind=kind, owner_org=owner_org.strip())

        segment_ids: list[str] = []
        for raw in segments or []:
            segment_id = raw.get("segment_id") or _new_id("seg")
            if segment_id in segment_ids:
                raise DomainError(f"区段编号 {segment_id} 重复")
            segment = Segment(
                label=raw["label"],
                start_cm=float(raw.get("start_cm", 0.0)),
                end_cm=float(raw.get("end_cm", 0.0)),
                segment_id=segment_id,
                note=raw.get("note", ""),
            )
            if segment.end_cm < segment.start_cm:
                raise DomainError(f"区段 {segment.label} 的终点不能早于起点")
            work.segments.append(segment)
            segment_ids.append(segment.segment_id)

        orders: set[int] = set()
        for raw in contributions or []:
            order = int(raw["order"])
            if order <= 0:
                raise DomainError("合作顺序须从 1 开始")
            if order in orders:
                raise DomainError(f"合作顺序 {order} 重复")
            orders.add(order)
            contribution_kind = raw.get("kind", "作画")
            if contribution_kind not in CONTRIBUTION_KINDS:
                raise DomainError(f"贡献类型须为 {CONTRIBUTION_KINDS} 之一")
            segment_id = raw.get("segment_id")
            if segment_id is not None and segment_id not in segment_ids:
                raise DomainError(f"贡献指向不存在的区段 {segment_id}")
            work.contributions.append(
                Contribution(
                    author=raw["author"],
                    kind=contribution_kind,
                    order=order,
                    segment_id=segment_id,
                )
            )

        self.works[work.work_id] = work
        return self.get_work_view(work.work_id)

    # -- 协议 --------------------------------------------------------------

    def create_agreement(self, payload: dict[str, Any]) -> dict[str, Any]:
        work = self._work(payload["work_id"])
        agreement_id = payload.get("agreement_id") or f"AGR-{work.work_id}-v1"
        self._validate_agreement_id(agreement_id)
        agreement = self._build_agreement(agreement_id, work.work_id, payload, version=1)
        agreement.created_seq = self._next_event_seq()
        self.agreements[agreement_id] = agreement
        self._agreement_history.setdefault(work.work_id, []).append(agreement_id)
        return self._agreement_view(agreement)

    def reschedule_agreement(
        self, current_agreement_id: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        """跨馆改期：协议条款变更生成新版本，旧版保留，已发布展签不受影响。"""
        old = self.agreements.get(current_agreement_id)
        if old is None:
            raise DomainError(f"协议 {current_agreement_id} 不存在")
        work = self._work(old.work_id)
        # 注意：冻结期不禁止改期——事故后仍可能另订新协议；
        # 但理赔报案基线锁定的是事故时版本，改期不改变原保障范围。

        merged: dict[str, Any] = {
            "lender_org": old.lender_org,
            "borrower_org": old.borrower_org,
            "start_on": old.start_on,
            "end_on": old.end_on,
            "gallery": old.gallery,
            "max_lux": old.max_lux,
            "transport": old.transport,
            "insurance": old.insurance,
            "digital_rights": old.digital_rights,
        }
        merged.update(changes)

        new_id = changes.get("agreement_id") or self._next_agreement_id(work.work_id, old.version + 1)
        self._validate_agreement_id(new_id)
        agreement = self._build_agreement(new_id, work.work_id, merged, version=old.version + 1)
        agreement.supersedes = current_agreement_id
        agreement.created_seq = self._next_event_seq()
        self.agreements[new_id] = agreement
        self._agreement_history.setdefault(work.work_id, []).append(new_id)
        return self._agreement_view(new_id)

    def _build_agreement(
        self, agreement_id: str, work_id: str, payload: dict[str, Any], version: int
    ) -> Agreement:
        start_on = self._date(payload["start_on"], "展期开始")
        end_on = self._date(payload["end_on"], "展期结束")
        if end_on < start_on:
            raise DomainError("展期结束日不能早于开始日")
        max_lux = int(payload["max_lux"])
        if max_lux <= 0:
            raise DomainError("照度上限须为正数（勒克斯）")
        agreement = Agreement(
            agreement_id=agreement_id,
            work_id=work_id,
            lender_org=payload["lender_org"],
            borrower_org=payload["borrower_org"],
            start_on=start_on.isoformat(),
            end_on=end_on.isoformat(),
            gallery=payload["gallery"],
            max_lux=max_lux,
            transport=dict(payload.get("transport") or {}),
            insurance=dict(payload.get("insurance") or {}),
            digital_rights=dict(payload.get("digital_rights") or {}),
            version=version,
        )
        return agreement

    def _validate_agreement_id(self, agreement_id: str) -> None:
        if agreement_id in self.agreements:
            raise ConflictError(f"协议编号 {agreement_id} 已存在")

    @staticmethod
    def _next_agreement_id(work_id: str, version: int) -> str:
        return f"AGR-{work_id}-v{version}"

    # -- 状态交接 ----------------------------------------------------------

    def record_handover(self, payload: dict[str, Any]) -> dict[str, Any]:
        work = self._work(payload["work_id"])
        handover_type = payload["type"]
        if handover_type not in HANDOVER_TYPES:
            raise DomainError(f"交接类型须为 {HANDOVER_TYPES} 之一")

        # 冻结与生命周期先校验，未成立的交接不得消费扫码标识。
        if work.work_id in self._frozen_works:
            raise ConflictError(f"作品 {work.work_id} 已因损伤冻结，后续交接全部中止")

        scan_code = str(payload["scan_code"])
        if not scan_code.strip():
            raise DomainError("扫码标识不能为空")
        if scan_code in self._scan_codes:
            previous = next(h.handover_id for h in self.handovers if h.scan_code == scan_code)
            raise ConflictError(
                f"扫码 {scan_code} 已在交接 {previous} 使用，重复扫码不能产生第二次交接"
            )

        expected_from, expected_to = TRANSFER_PAIRS[handover_type]
        from_party = self._signature(payload["from_party"], expected_from)
        to_party = self._signature(payload["to_party"], expected_to)
        self._assert_lifecycle(work.work_id, handover_type)

        linked_segments = list(payload.get("linked_segments") or [])
        for segment_id in linked_segments:
            work.require_segment(segment_id)

        report = self._condition_report(payload.get("report") or {})
        handover = Handover(
            handover_id=_new_id("handover"),
            work_id=work.work_id,
            type=handover_type,
            scan_code=scan_code,
            from_party=from_party,
            to_party=to_party,
            report=report,
            on_date=self._date(payload["on_date"], "交接日期").isoformat(),
            at_location=str(payload.get("at_location", "")),
            linked_segments=linked_segments,
            event_seq=self._next_event_seq(),
        )
        self._scan_codes.add(scan_code)
        self.handovers.append(handover)

        if handover.damaged:
            self._frozen_works.add(work.work_id)
            incident = Incident(
                incident_id=_new_id("incident"),
                work_id=work.work_id,
                handover_id=handover.handover_id,
                on_date=handover.on_date,
                note=report.damage_note,
                before_hashes=list(report.before_hashes),
                after_hashes=list(report.after_hashes),
            )
            self.incidents.append(incident)
            return self._handover_view(handover, frozen=True, incident_id=incident.incident_id)

        return self._handover_view(handover, frozen=False)

    # -- 保险理赔 ----------------------------------------------------------

    def file_claim(
        self,
        incident_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """报案：锁定事故发生时有效的保险条款、双方签认与图像摘要。

        报案基线一经形成永不改变；协议事后改期、对方另引一版照片都只产生
        新材料版本，不替换快照。
        """
        with self._lock:
            incident = self._incident(incident_id)
            if incident_id in self._claim_by_incident:
                raise ConflictError(
                    f"损伤事件 {incident_id} 已报案（{self._claim_by_incident[incident_id]}），"
                    "补充材料应走追加接口"
                )
            handover = next(h for h in self.handovers if h.handover_id == incident.handover_id)
            agreement = self._agreement_effective_on(
                incident.work_id, incident.on_date, before_seq=handover.event_seq
            )
            if agreement is None:
                raise DomainError("事故发生时不存在有效的借展协议/保险条款，不能报案")

            parties = self._claim_parties(incident.work_id, agreement, handover, payload)
            # 报案须由当事一方（或其保险方）提出
            self._assert_party_org(payload, parties, viewer_org)

            currency = str(payload.get("currency", "CNY"))
            claim = Claim(
                claim_id=payload.get("claim_id") or _new_id("claim"),
                incident_id=incident.incident_id,
                work_id=incident.work_id,
                filed_on=self._date(payload.get("filed_on", incident.on_date), "报案日期").isoformat(),
                agreement_snapshot=agreement.authorization_scope() | {
                    "agreement_id": agreement.agreement_id,
                    "supersedes": agreement.supersedes,
                },
                handover_snapshot=self._handover_view(handover),
                image_summary={
                    "before_hashes": list(incident.before_hashes),
                    "after_hashes": list(incident.after_hashes),
                    "handover_image_hashes": list(handover.report.image_hashes),
                    "note": "事故交接时双方签认的图像摘要",
                },
                parties=parties,
                currency=currency,
            )
            if claim.claim_id in self.claims:
                raise ConflictError(f"理赔编号 {claim.claim_id} 已存在")
            self.claims[claim.claim_id] = claim
            self._claim_by_incident[incident_id] = claim.claim_id
            return self._claim_view(claim, viewer_org)

    def add_claim_material(
        self,
        claim_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """按角色追加估损/异议/修复方案/责任意见。

        只能追加形成新版本；可标注 supersede 本方上一版同类材料，
        但被取代的旧版仍保留可查，任何已被决定引用的证据不得替换。
        """
        with self._lock:
            claim = self._claim(claim_id)
            if claim.closed:
                raise ConflictError(f"理赔 {claim_id} 已关闭，新材料只能另案处理")
            kind = payload.get("kind", "")
            if kind not in ADDITION_KINDS:
                raise DomainError(f"追加材料类型须为 {ADDITION_KINDS} 之一")
            org, role, person = self._acting_party(payload, claim)
            if role not in ADDITION_ROLES[kind]:
                raise DomainError(f"{kind} 只能由 {'/'.join(ADDITION_ROLES[kind])} 追加，{role} 无权提交")
            summary = str(payload.get("summary", "") or "").strip()
            if not summary:
                raise DomainError("追加材料必须填写摘要")

            supersedes_seq = payload.get("supersedes_seq")
            if supersedes_seq is not None:
                supersedes_seq = int(supersedes_seq)
                old = next((a for a in claim.additions if a.seq == supersedes_seq), None)
                if old is None:
                    raise DomainError(f"被取代的材料版本 #{supersedes_seq} 不存在")
                if old.org != org or old.kind != kind:
                    raise DomainError("只能取代本方同一类型的上一版材料")

            seq = len(claim.additions) + 1
            addition = ClaimAddition(
                addition_id=_new_id("add"),
                seq=seq,
                kind=kind,
                org=org,
                role=role,
                person=person,
                summary=summary,
                on_date=self._date(payload["on_date"], "材料日期").isoformat(),
                image_hashes=[self._image_hash(h) for h in payload.get("image_hashes", [])],
                payload={
                    k: v for k, v in dict(payload.get("payload") or {}).items()
                    if k not in ("image_hashes",)
                },
                supersedes_seq=supersedes_seq,
            )
            claim.additions.append(addition)
            if kind == "估损":
                self._apply_assessment(claim, addition, payload)
            elif kind == "核赔结论":
                self._apply_decision(claim, addition, payload)
            return self._claim_view(claim, viewer_org)

    def _apply_assessment(self, claim: Claim, addition: ClaimAddition, payload: dict[str, Any]) -> None:
        amount = payload.get("amount")
        if amount is None:
            raise DomainError("估损必须给出金额 amount")
        amount = float(amount)
        if amount < 0:
            raise DomainError("估损金额不能为负")
        claim.assessed_amount = amount
        addition.payload["amount"] = amount
        if claim.stage == "待估损":
            claim.stage = "估损中"

    def _apply_decision(self, claim: Claim, addition: ClaimAddition, payload: dict[str, Any]) -> None:
        """核赔结论是唯一的资金决定；并发/重复决定第二次起一律拒绝。"""
        decision = str(payload.get("decision", "") or "").strip()
        if decision not in ("全额认可", "部分认可", "拒赔"):
            raise DomainError("核赔结论 decision 须为 全额认可 / 部分认可 / 拒赔")
        if claim.decision_seq is not None:
            raise ConflictError(
                f"理赔已有核赔结论（材料 #{claim.decision_seq}），"
                "改变结论只能通过追加异议与复核，不能重复决定"
            )
        accepted = float(payload.get("accepted_amount", 0) or 0)
        deductible = float(payload.get("deductible_amount", 0) or 0)
        if accepted < 0 or deductible < 0:
            raise DomainError("认可金额与免赔额不能为负")
        if decision == "拒赔" and accepted != 0:
            raise DomainError("拒赔结论的认可金额必须为 0")
        if claim.assessed_amount is not None and accepted > claim.assessed_amount + 1e-9:
            raise DomainError("认可金额不能超过估损金额")
        if decision == "全额认可":
            if claim.assessed_amount is None:
                raise DomainError("全额认可前须先有估损")
            if abs(accepted - (claim.assessed_amount - deductible)) > 1e-9:
                raise DomainError("全额认可的 accepted_amount 应等于 估损金额 - 免赔额")
        if decision == "部分认可" and accepted <= 0:
            raise DomainError("部分认可的认可金额必须大于 0（拒赔请用 拒赔）")
        claim.decision_seq = addition.seq
        claim.stage = decision
        claim.accepted_amount = accepted
        claim.deductible_amount = deductible
        addition.payload.update(
            {"decision": decision, "accepted_amount": accepted, "deductible_amount": deductible}
        )
        # 免赔额作为负向资金分录落账，与赔付/追偿一起保持守恒
        if deductible > 0:
            claim.fund_entries.append(FundEntry(
                entry_id=_new_id("fund"), seq=len(claim.fund_entries) + 1,
                kind="免赔额", amount=deductible, currency=claim.currency,
                org=addition.org, person=addition.person, on_date=addition.on_date,
                note=f"核赔结论材料 #{addition.seq} 扣减",
            ))

    def add_fund_entry(
        self,
        claim_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """追加资金分录：赔付（可分期）、追偿收回。回执幂等，金额守恒。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.closed:
                raise ConflictError(f"理赔 {claim_id} 已关闭，不能再记账")
            kind = payload.get("kind", "")
            if kind not in ("赔付", "追偿"):
                raise DomainError("资金接口只接受 赔付 / 追偿；免赔额由核赔结论自动落账")
            org, role, person = self._acting_party(payload, claim)
            if role != "保险方":
                raise DomainError("资金分录只能由保险方登记")
            amount = float(payload.get("amount", 0))
            if amount <= 0:
                raise DomainError("资金分录金额必须大于 0")

            receipt_no = str(payload.get("receipt_no", "") or "").strip()
            if not receipt_no:
                raise DomainError("资金分录必须带回执号 receipt_no")
            if receipt_no in claim.receipt_nos:
                raise ConflictError(f"回执 {receipt_no} 已登记，重复回执不得多记赔款")

            installment_no = int(payload.get("installment_no", 1))
            if kind == "赔付":
                if claim.decision_seq is None:
                    raise ConflictError("尚未作出核赔结论，不能赔付")
                if installment_no < 1:
                    raise DomainError("分期序号从 1 开始")
                # 同一分期不得重复；累计赔付不得超过认可金额（含追偿冲减见下）
                existing_inst = {
                    (e.kind, e.installment_no) for e in claim.fund_entries
                }
                if ("赔付", installment_no) in existing_inst:
                    raise ConflictError(f"第 {installment_no} 期赔付已登记，不能重复入账")
                total_paid = self.fund_totals(claim.claim_id)["paid"]
                if total_paid + amount > claim.accepted_amount + 1e-9:
                    raise DomainError(
                        f"累计赔付 {total_paid + amount} 超过认可金额 {claim.accepted_amount}"
                    )
            if kind == "追偿":
                target = self.fund_totals(claim.claim_id)["paid"] - self.fund_totals(claim.claim_id)["recovered"]
                if amount > target + 1e-9:
                    raise DomainError("追偿收回不能超过尚未冲减的已赔金额")

            entry = FundEntry(
                entry_id=_new_id("fund"),
                seq=len(claim.fund_entries) + 1,
                kind=kind, amount=amount, currency=claim.currency,
                org=org, person=person,
                on_date=self._date(payload["on_date"], "记账日期").isoformat(),
                receipt_no=receipt_no,
                note=str(payload.get("note", "") or ""),
                installment_no=installment_no,
            )
            claim.fund_entries.append(entry)
            claim.receipt_nos.add(receipt_no)
            return self._claim_view(claim, viewer_org)

    def fund_totals(self, claim_id: str) -> dict[str, float]:
        """由全部分录汇总：赔付为正、免赔额为负、追偿收回冲减赔付。"""
        claim = self._claim(claim_id)
        paid = sum(e.amount for e in claim.fund_entries if e.kind == "赔付")
        deductible = sum(e.amount for e in claim.fund_entries if e.kind == "免赔额")
        recovered = sum(e.amount for e in claim.fund_entries if e.kind == "追偿")
        return {
            "assessed": claim.assessed_amount or 0.0,
            "accepted": claim.accepted_amount or 0.0,
            "deductible": deductible,
            "paid": paid,
            "recovered": recovered,
            "outstanding": max((claim.accepted_amount or 0.0) - paid, 0.0),
            "net_paid": paid - recovered,
        }

    def close_claim(
        self,
        claim_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """关闭理赔（账务/责任了结）。关闭不等于解冻，解冻走双闸门。"""
        with self._lock:
            claim = self._claim(claim_id)
            if claim.closed:
                raise ConflictError(f"理赔 {claim_id} 已关闭")
            org, role, _ = self._acting_party(payload, claim)
            if role != "保险方":
                raise DomainError("理赔关闭只能由保险方决定")
            note = str(payload.get("note", "") or "").strip()
            if not note:
                raise DomainError("关闭理赔须填写关闭说明")
            totals = self.fund_totals(claim_id)
            if claim.stage not in ("拒赔",) and totals["outstanding"] > 1e-9:
                raise ConflictError(
                    f"尚有 {totals['outstanding']} 认可赔款未付清，不能关闭；"
                    "分期赔付应先全部到账或追加调整分录"
                )
            claim.closed = True
            claim.closed_on = self._date(payload["on_date"], "关闭日期").isoformat()
            claim.close_note = note
            return self._claim_view(claim, viewer_org)

    # -- 修复复核与保管责任：解冻双闸门 ------------------------------------

    def submit_restoration_review(
        self,
        incident_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """修复完成后的书面复核：出借馆与承借馆双签确认，方可通过修复闸门。"""
        with self._lock:
            incident = self._incident(incident_id)
            if incident.restoration_reviewed:
                raise ConflictError("修复复核已通过，不能重复提交")
            plan_seq = payload.get("plan_seq")
            additions = self._incident_additions(incident_id)
            plans = [a for a in additions if a.kind == "修复方案"]
            if not plans:
                raise ConflictError("缺少承借馆的修复方案，不能复核")
            if plan_seq is not None and not any(a.seq == int(plan_seq) for a in plans):
                raise DomainError(f"复核引用的修复方案 #{plan_seq} 不存在")
            reviewers = payload.get("reviewers")
            if not isinstance(reviewers, list) or len(reviewers) < 2:
                raise DomainError("修复复核须由出借馆与承借馆双方书面签认")
            roles_seen = set()
            for raw in reviewers:
                if not raw.get("person"):
                    raise DomainError("复核签认人不能为空")
                role = raw.get("role", "")
                if role not in ("出借馆", "承借馆"):
                    raise DomainError("修复复核签认角色须为出借馆或承借馆")
                roles_seen.add(role)
            if roles_seen != {"出借馆", "承借馆"}:
                raise DomainError("修复复核必须同时有出借馆与承借馆签认")
            note = str(payload.get("note", "") or "").strip()
            if not note:
                raise DomainError("修复复核须填写复核结论")
            incident.restoration_reviewed = True
            incident.restoration_review = {
                "reviewers": [
                    {"org": r.get("org", ""), "role": r["role"], "person": r["person"]}
                    for r in reviewers
                ],
                "note": note,
                "plan_seq": int(plan_seq) if plan_seq is not None else plans[-1].seq,
                "on_date": self._date(payload["on_date"], "复核日期").isoformat(),
            }
            self._maybe_unfreeze(incident)
            return self.incident_view(incident_id, viewer_org)

    def confirm_custody(
        self,
        incident_id: str,
        payload: dict[str, Any],
        viewer_org: Optional[str] = None,
    ) -> dict[str, Any]:
        """保管责任闸门：当前实际保管方签认作品现状并接管后续保管。"""
        with self._lock:
            incident = self._incident(incident_id)
            if incident.custody_confirmed:
                raise ConflictError("保管责任已签认，不能重复签认")
            custody = self._custody(incident.work_id)
            expected_role = custody["custodian_role"]
            expected_org = custody.get("custodian_org", "")
            role = payload.get("role", expected_role)
            org = str(payload.get("org", expected_org) or "")
            person = payload.get("person", "")
            if not person:
                raise DomainError("保管签认人不能为空")
            if role != expected_role:
                raise DomainError(f"当前保管责任在 {expected_role}，{role} 无权签认")
            note = str(payload.get("note", "") or "").strip()
            if not note:
                raise DomainError("保管签认须填写作品现状与接管说明")
            incident.custody_confirmed = True
            incident.custody_confirmation = {
                "org": org, "role": role, "person": person, "note": note,
                "since_handover": custody.get("since_handover"),
                "on_date": self._date(payload["on_date"], "签认日期").isoformat(),
            }
            self._maybe_unfreeze(incident)
            return self.incident_view(incident_id, viewer_org)

    def _maybe_unfreeze(self, incident: Incident) -> None:
        """理赔关闭与解冻分别判断：修复复核与保管责任同时满足才恢复交接。"""
        if incident.restoration_reviewed and incident.custody_confirmed:
            incident.resolved = True
            incident.resolution_note = "修复复核与保管责任双闸门均已满足，恢复后续交接"
            self._frozen_works.discard(incident.work_id)

    # -- 理赔查询视图 ------------------------------------------------------

    def claim_view(self, claim_id: str, viewer_org: Optional[str] = None) -> dict[str, Any]:
        return self._claim_view(self._claim(claim_id), viewer_org)

    def incident_view(self, incident_id: str, viewer_org: Optional[str] = None) -> dict[str, Any]:
        incident = self._incident(incident_id)
        return self._incident_gate_view(incident, viewer_org)

    def _claim_parties(
        self,
        work_id: str,
        agreement: Agreement,
        handover: Handover,
        payload: dict[str, Any],
    ) -> dict[str, str]:
        insurer = str(payload.get("insurer_org", "") or "").strip()
        if not insurer:
            raise DomainError("报案须登记保险方机构 insurer_org")
        return {
            "保险方": insurer,
            "出借馆": agreement.lender_org,
            "承借馆": agreement.borrower_org,
            "运输方": self._transport_org_for(work_id, handover),
        }

    def _transport_org_for(self, work_id: str, accident: Handover) -> str:
        """事故交接自身无运输方（如布展）时，沿交接链回溯最近的运输方机构。"""
        chain = [h for h in self.handovers if h.work_id == work_id]
        for handover in reversed(chain):
            org = _transport_org(handover)
            if org:
                return org
            if handover is accident:
                break
        return ""

    def _assert_party_org(
        self, payload: dict[str, Any], parties: dict[str, str], viewer_org: Optional[str]
    ) -> None:
        org = str(payload.get("org", viewer_org or "") or "").strip()
        if not org:
            raise DomainError("报案须填写报案机构 org")
        if org not in parties.values():
            raise DomainError(f"机构 {org} 不是本理赔参与方，无权报案")

    def _acting_party(
        self, payload: dict[str, Any], claim: Claim
    ) -> tuple[str, str, str]:
        role = payload.get("role", "")
        org = str(payload.get("org", "") or "").strip()
        person = payload.get("person", "")
        if role not in CLAIM_ROLES:
            raise DomainError(f"角色须为 {CLAIM_ROLES} 之一")
        if not org or not person:
            raise DomainError("追加材料须填写机构 org 与签认人 person")
        if claim.parties.get(role) != org:
            raise DomainError(
                f"{role} 角色在本理赔中只能由 {claim.parties.get(role)} 行使，"
                f"收到的机构为 {org}"
            )
        return org, role, person

    def _agreement_effective_on(self, work_id: str, day: str,
                                before_seq: Optional[int] = None) -> Optional[Agreement]:
        """事故发生时有效的协议版本。

        钉到钉保障在出库起运时即附着，故不按展期日期判断，而按登记的
        全局事件顺序：取事故交接之前（含）已经订立的最新一版协议；
        事故后改期产生的新版本不会被追溯引用。
        """
        history = self._agreement_history.get(work_id, [])
        candidates = [self.agreements[i] for i in history]
        if before_seq is not None:
            candidates = [a for a in candidates if a.created_seq <= before_seq]
        else:
            candidates = [a for a in candidates if a.start_on <= day]
        return candidates[-1] if candidates else None

    def _incident(self, incident_id: str) -> Incident:
        incident = next((i for i in self.incidents if i.incident_id == incident_id), None)
        if incident is None:
            raise DomainError(f"损伤事件 {incident_id} 不存在")
        return incident

    def _claim(self, claim_id: str) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise DomainError(f"理赔 {claim_id} 不存在")
        return claim

    def _incident_additions(self, incident_id: str) -> list[ClaimAddition]:
        claim_id = self._claim_by_incident.get(incident_id)
        return list(self.claims[claim_id].additions) if claim_id else []

    def _is_authorized(self, claim: Claim, viewer_org: Optional[str]) -> bool:
        return bool(viewer_org and viewer_org in claim.parties.values())

    def _addition_view(self, addition: ClaimAddition, authorized: bool) -> dict[str, Any]:
        view = {
            "seq": addition.seq,
            "addition_id": addition.addition_id,
            "kind": addition.kind,
            "org": addition.org,
            "role": addition.role,
            "person": addition.person,
            "on_date": addition.on_date,
            "summary": addition.summary,
            "supersedes_seq": addition.supersedes_seq,
        }
        if authorized:
            view["image_hashes"] = addition.image_hashes
            view["payload"] = addition.payload
        else:
            # 未获授权机构看不到作品敏感材料：只给类型与日期，不给摘要与图像
            view["summary"] = "（未授权，内容隐藏）"
            view["redacted"] = True
        return view

    def _claim_view(self, claim: Claim, viewer_org: Optional[str] = None) -> dict[str, Any]:
        authorized = self._is_authorized(claim, viewer_org)
        incident = self._incident(claim.incident_id)
        baseline = {
            "filed_on": claim.filed_on,
            "agreement_version": claim.agreement_snapshot if authorized else {
                "version": claim.agreement_snapshot.get("version"),
                "redacted": True,
            },
            "signatures": claim.handover_snapshot["signed_by_both"],
            "image_summary": claim.image_summary if authorized else {"redacted": True},
            "locked": True,
        }
        view = {
            "claim_id": claim.claim_id,
            "incident_id": claim.incident_id,
            "work_id": claim.work_id,
            "stage": claim.stage,
            "closed": claim.closed,
            "closed_on": claim.closed_on,
            "currency": claim.currency,
            "parties": claim.parties,
            "baseline": baseline,
            "additions": [self._addition_view(a, authorized) for a in claim.additions],
            "fund_entries": [
                {
                    "seq": e.seq, "kind": e.kind, "amount": e.amount,
                    "currency": e.currency, "on_date": e.on_date,
                    "receipt_no": e.receipt_no, "installment_no": e.installment_no,
                    "org": e.org,
                }
                for e in claim.fund_entries
            ],
            "fund_totals": self.fund_totals(claim.claim_id),
            "decision_seq": claim.decision_seq,
            "restoration_gate": {
                "passed": incident.restoration_reviewed,
                "review": incident.restoration_review if authorized else {},
            },
            "custody_gate": {
                "passed": incident.custody_confirmed,
                "confirmation": incident.custody_confirmation if authorized else {},
            },
            "unfrozen": not (incident.work_id in self._frozen_works),
            "authorized": authorized,
        }
        if not authorized:
            view["parties"] = {"保险方": claim.parties["保险方"]}
        return view

    def _incident_gate_view(self, incident: Incident, viewer_org: Optional[str]) -> dict[str, Any]:
        claim_id = self._claim_by_incident.get(incident.incident_id)
        claim = self.claims.get(claim_id) if claim_id else None
        authorized = bool(claim and self._is_authorized(claim, viewer_org)) or claim is None
        view = {
            "incident_id": incident.incident_id,
            "work_id": incident.work_id,
            "restoration_reviewed": incident.restoration_reviewed,
            "custody_confirmed": incident.custody_confirmed,
            "resolved": incident.resolved,
            "resolution_note": incident.resolution_note,
            "frozen": incident.work_id in self._frozen_works,
        }
        if authorized:
            view["restoration_review"] = incident.restoration_review
            view["custody_confirmation"] = incident.custody_confirmation
        if claim_id:
            view["claim_id"] = claim_id
        return view

    # -- 状态交接（旧） ----------------------------------------------------

    def _assert_lifecycle(self, work_id: str, handover_type: str) -> None:
        completed = [h.type for h in self.handovers if h.work_id == work_id]
        expected = HANDOVER_TYPES[len(completed)]
        if handover_type != expected:
            raise ConflictError(
                f"作品 {work_id} 下一次交接应为 {expected}，不能直接办理 {handover_type}"
            )

    @staticmethod
    def _signature(raw: dict[str, Any], expected_role: str) -> Signature:
        if not raw or not raw.get("person"):
            raise DomainError("交接双方都须指定签认人")
        role = raw.get("role", expected_role)
        if role != expected_role:
            raise DomainError(f"该交接位置须由 {expected_role} 签认，收到的是 {role}")
        return Signature(org=raw.get("org", ""), role=role, person=raw["person"])

    @staticmethod
    def _condition_report(raw: dict[str, Any]) -> ConditionReport:
        condition = raw.get("condition", "良好")
        if condition not in ("良好", "损伤"):
            raise DomainError("状态结论须为 良好 或 损伤")
        hashes = [LoanRegistry._image_hash(h) for h in raw.get("image_hashes", [])]
        damage_note = str(raw.get("damage_note", "") or "").strip()
        if condition == "损伤" and not damage_note:
            raise DomainError("损伤报告必须填写损伤说明")
        return ConditionReport(
            condition=condition,
            image_hashes=hashes,
            damage_note=damage_note,
            before_hashes=[LoanRegistry._image_hash(h) for h in raw.get("before_hashes", [])],
            after_hashes=[LoanRegistry._image_hash(h) for h in raw.get("after_hashes", [])],
            note=str(raw.get("note", "") or ""),
        )

    @staticmethod
    def _image_hash(value: str) -> str:
        """登记图像证据哈希；已是 64 位十六进制（sha256）时原样保全，否则计算。"""
        value = str(value)
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    # -- 策展展签 ----------------------------------------------------------

    def create_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        work = self._work(work_id)
        if work_id in self.labels:
            raise ConflictError(f"作品 {work_id} 的展签已存在，应使用更正接口")
        if not narrative or not narrative.strip():
            raise DomainError("展签叙事不能为空")
        version = LabelVersion(
            version=1,
            status="草拟",
            narrative=narrative.strip(),
            citations=self._normalize_citations(citations),
            evidence={},
            published_on=None,
            frozen=False,
        )
        self.labels[work_id] = [version]
        return self._label_view(work_id, version)

    def publish_label(self, work_id: str, published_on: str) -> dict[str, Any]:
        """发布日期确认：锁定证据快照。快照只含当时的数据，之后不再变化。"""
        versions = self.labels.get(work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        current = versions[-1]
        if current.frozen:
            raise ConflictError(f"展签 v{current.version} 已发布并锁定，不能重复发布")
        day = self._date(published_on, "发布日期")
        current.status = "已发布"
        current.published_on = day.isoformat()
        current.frozen = True
        current.evidence = self._evidence_snapshot(work_id)
        return self._label_view(work_id, current)

    def correct_label(self, work_id: str, narrative: str, citations: list[dict[str, str]]) -> dict[str, Any]:
        """学术更正：另起新版本，旧版展签与其证据快照原样保留。"""
        versions = self.labels.get(work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        current = versions[-1]
        if not current.frozen:
            raise ConflictError("只能更正已发布的展签；草拟版可直接修改")
        new_version = LabelVersion(
            version=current.version + 1,
            status="草拟",
            narrative=narrative.strip(),
            citations=self._normalize_citations(citations),
            evidence={},
            published_on=None,
            frozen=False,
        )
        versions.append(new_version)
        return self._label_view(work_id, new_version)

    def _evidence_snapshot(self, work_id: str) -> dict[str, Any]:
        """发布时点的证据快照：作品、区段、贡献、协议、交接与未解除风险。"""
        work = self.works[work_id]
        agreement = self._current_agreement(work_id)
        handovers = [self._handover_view(h) for h in self.handovers if h.work_id == work_id]
        open_incidents = [
            {
                "incident_id": i.incident_id,
                "handover_id": i.handover_id,
                "on_date": i.on_date,
                "note": i.note,
                "before_hashes": i.before_hashes,
                "after_hashes": i.after_hashes,
            }
            for i in self.incidents
            if i.work_id == work_id and not i.resolved
        ]
        return {
            "snapshot_on": date.today().isoformat(),
            "work": {
                "work_id": work.work_id,
                "title": work.title,
                "kind": work.kind,
                "owner_org": work.owner_org,
            },
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "label": s.label,
                    "start_cm": s.start_cm,
                    "end_cm": s.end_cm,
                }
                for s in work.segments
            ],
            "contributions": [
                {
                    "contribution_id": c.contribution_id,
                    "author": c.author,
                    "kind": c.kind,
                    "order": c.order,
                    "segment_id": c.segment_id,
                }
                for c in sorted(work.contributions, key=lambda c: c.order)
            ],
            "agreement_version": agreement.authorization_scope() if agreement else None,
            "handovers": handovers,
            "custody": self._custody(work_id),
            "open_risks": open_incidents,
        }

    @staticmethod
    def _normalize_citations(citations: list[dict[str, str]]) -> list[dict[str, str]]:
        result = []
        for raw in citations or []:
            if not raw.get("ref"):
                raise DomainError("引用必须指向作品关系（work_id 或 segment_id）")
            result.append({"ref": raw["ref"], "note": raw.get("note", "")})
        return result

    # -- 查询视图 ----------------------------------------------------------

    def get_work_view(self, work_id: str) -> dict[str, Any]:
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        return {
            "work": {
                "work_id": work.work_id,
                "title": work.title,
                "kind": work.kind,
                "owner_org": work.owner_org,
            },
            "segments": [
                {
                    "segment_id": s.segment_id,
                    "label": s.label,
                    "start_cm": s.start_cm,
                    "end_cm": s.end_cm,
                    "note": s.note,
                }
                for s in work.segments
            ],
            "contributions": [
                {
                    "contribution_id": c.contribution_id,
                    "author": c.author,
                    "kind": c.kind,
                    "order": c.order,
                    "segment_id": c.segment_id,
                }
                for c in sorted(work.contributions, key=lambda c: c.order)
            ],
            "current_agreement": agreement.agreement_id if agreement else None,
            "agreement_versions": list(self._agreement_history.get(work_id, [])),
            "custody": self._custody(work_id),
            "frozen": work_id in self._frozen_works,
        }

    def risk_view(self, work_id: str, viewer_org: Optional[str] = None) -> dict[str, Any]:
        """风险追溯：并列呈现开放损伤、理赔阶段、缺少的签认与资金变化。

        未获授权机构看不到作品敏感材料（图像摘要、损伤细节、修复内容）。
        """
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        open_incidents = [i for i in self.incidents if i.work_id == work_id and not i.resolved]
        authorized_parties: set[str] = set()
        claims = [self.claims[cid] for cid in (
            self._claim_by_incident.get(i.incident_id) for i in open_incidents
        ) if cid]
        for claim in claims:
            authorized_parties.update(claim.parties.values())
        authorized = viewer_org is None or viewer_org in authorized_parties or not claims

        incident_blocks = []
        for incident in open_incidents:
            block: dict[str, Any] = {
                "incident_id": incident.incident_id,
                "on_date": incident.on_date,
            }
            if authorized:
                block["note"] = incident.note
                block["before_hashes"] = incident.before_hashes
                block["after_hashes"] = incident.after_hashes
            else:
                block["note"] = "（未授权，损伤细节隐藏）"
                block["redacted"] = True
            claim_id = self._claim_by_incident.get(incident.incident_id)
            block["claim_id"] = claim_id
            if claim_id:
                claim = self.claims[claim_id]
                block["claim_stage"] = claim.stage
                block["claim_closed"] = claim.closed
                block["missing_signatures"] = self._missing_signatures(incident, claim)
                if authorized:
                    block["fund_changes"] = [
                        {"seq": e.seq, "kind": e.kind, "amount": e.amount,
                         "currency": e.currency, "on_date": e.on_date,
                         "installment_no": e.installment_no}
                        for e in claim.fund_entries
                    ]
                    block["fund_totals"] = self.fund_totals(claim_id)
                else:
                    block["fund_changes"] = []
                block["gates"] = {
                    "restoration_reviewed": incident.restoration_reviewed,
                    "custody_confirmed": incident.custody_confirmed,
                }
            else:
                block["claim_stage"] = None
                block["missing_signatures"] = ["报案后才能确定理赔签认缺口"]
            incident_blocks.append(block)

        return {
            "work": work.to_ref(),
            "custody": self._custody(work_id),
            "authorization": agreement.authorization_scope() if agreement else None,
            # 四要素并列，任一未结都在本视图中平铺，不与资金/理赔状态互相遮蔽
            "open_risks": incident_blocks,
            "frozen": work_id in self._frozen_works,
            "authorized": authorized,
        }

    def _missing_signatures(self, incident: Incident, claim: Claim) -> list[str]:
        """列出解冻与理赔链路上尚缺的签认。"""
        missing: list[str] = []
        if not any(a.kind == "修复方案" for a in claim.additions):
            missing.append("承借馆修复方案")
        if not incident.restoration_reviewed:
            missing.append("修复复核：出借馆签认")
            missing.append("修复复核：承借馆签认")
        if not incident.custody_confirmed:
            missing.append(f"保管责任：{self._custody(incident.work_id)['custodian_role']}签认")
        if claim.decision_seq is None:
            missing.append("保险方核赔结论")
        elif not claim.closed:
            totals = self.fund_totals(claim.claim_id)
            if totals["outstanding"] > 1e-9:
                missing.append(f"保险方分期赔付（余 {totals['outstanding']:g}）")
        return missing

    def locate_segment(self, work_id: str, segment_id: str) -> dict[str, Any]:
        """局部状态争议入口：从区段定位实体、贡献、当前保管与风险。"""
        work = self._work(work_id)
        segment = work.require_segment(segment_id)
        linked = [
            {
                "contribution_id": c.contribution_id,
                "author": c.author,
                "kind": c.kind,
                "order": c.order,
            }
            for c in sorted(work.contributions, key=lambda c: c.order)
            if c.segment_id == segment_id
        ]
        segment_handovers = [
            self._handover_view(h)
            for h in self.handovers
            if h.work_id == work_id and (not h.linked_segments or segment_id in h.linked_segments)
        ]
        segment_incidents = [
            {
                "incident_id": i.incident_id,
                "on_date": i.on_date,
                "note": i.note,
                "resolved": i.resolved,
            }
            for i in self.incidents
            if i.work_id == work_id
            and any(
                h.linked_segments and segment_id in h.linked_segments
                for h in self.handovers
                if h.handover_id == i.handover_id
            )
        ]
        return {
            "work": work.to_ref(),
            "segment": {
                "segment_id": segment.segment_id,
                "label": segment.label,
                "start_cm": segment.start_cm,
                "end_cm": segment.end_cm,
            },
            "contributions": linked,
            "custody": self._custody(work_id),
            "segment_handovers": segment_handovers,
            "segment_incidents": segment_incidents,
            "frozen": work_id in self._frozen_works,
        }

    def label_version(self, work_id: str, version: Optional[int] = None) -> dict[str, Any]:
        versions = self.labels.get(self._work(work_id).work_id)
        if not versions:
            raise DomainError(f"作品 {work_id} 尚无展签")
        target = versions[-1] if version is None else next(
            (v for v in versions if v.version == version), None
        )
        if target is None:
            raise DomainError(f"展签 v{version} 不存在")
        return self._label_view(work_id, target)

    def _custody(self, work_id: str) -> dict[str, Any]:
        completed = [h for h in self.handovers if h.work_id == work_id]
        if not completed:
            work = self.works[work_id]
            return {"status": "在库", "custodian_role": "出借馆", "custodian_org": work.owner_org}
        last = completed[-1]
        return {
            "status": STATUS_AFTER[last.type],
            "custodian_role": last.to_party.role,
            "custodian_org": last.to_party.org,
            "since_handover": last.handover_id,
            "since": last.on_date,
        }

    def _current_agreement(self, work_id: str) -> Optional[Agreement]:
        history = self._agreement_history.get(work_id)
        if not history:
            return None
        return self.agreements[history[-1]]

    def _work(self, work_id: str) -> Work:
        work = self.works.get(work_id)
        if work is None:
            raise DomainError(f"作品 {work_id} 不存在")
        return work

    @staticmethod
    def _date(value: str, field_name: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise DomainError(f"{field_name} 须为 YYYY-MM-DD 日期")

    # -- 序列化 ------------------------------------------------------------

    def _agreement_view(self, ref: str | Agreement) -> dict[str, Any]:
        agreement = ref if isinstance(ref, Agreement) else self.agreements[ref]
        return {
            "agreement_id": agreement.agreement_id,
            "work_id": agreement.work_id,
            "version": agreement.version,
            "supersedes": agreement.supersedes,
            "lender_org": agreement.lender_org,
            "borrower_org": agreement.borrower_org,
            "start_on": agreement.start_on,
            "end_on": agreement.end_on,
            "gallery": agreement.gallery,
            "max_lux": agreement.max_lux,
            "transport": agreement.transport,
            "insurance": agreement.insurance,
            "digital_rights": agreement.digital_rights,
        }

    def _handover_view(self, handover: Handover, frozen: bool = False, incident_id: Optional[str] = None) -> dict[str, Any]:
        view = {
            "handover_id": handover.handover_id,
            "work_id": handover.work_id,
            "type": handover.type,
            "scan_code": handover.scan_code,
            "on_date": handover.on_date,
            "at_location": handover.at_location,
            "linked_segments": handover.linked_segments,
            "from_party": {"org": handover.from_party.org, "role": handover.from_party.role, "person": handover.from_party.person},
            "to_party": {"org": handover.to_party.org, "role": handover.to_party.role, "person": handover.to_party.person},
            "signed_by_both": bool(handover.from_party.person and handover.to_party.person),
            "resulting_status": STATUS_AFTER[handover.type],
            "condition": {
                "condition": handover.report.condition,
                "damage_note": handover.report.damage_note,
                "image_hashes": handover.report.image_hashes,
                "before_hashes": handover.report.before_hashes,
                "after_hashes": handover.report.after_hashes,
            },
        }
        if frozen:
            view["frozen"] = True
            view["frozen_reason"] = "发现损伤，后续动作冻结"
        if incident_id:
            view["incident_id"] = incident_id
        return view

    def _label_view(self, work_id: str, version: LabelVersion) -> dict[str, Any]:
        return {
            "work_id": work_id,
            "version": version.version,
            "status": version.status,
            "frozen": version.frozen,
            "published_on": version.published_on,
            "narrative": version.narrative,
            "citations": version.citations,
            "evidence_snapshot": version.evidence,
        }


# ---------------------------------------------------------------------------
# 应用外观：供 HTTP 层调用
# ---------------------------------------------------------------------------


@dataclass
class Route:
    method: str
    pattern: str
    handler: Callable[[LoanRegistry, dict[str, Any], dict[str, str], Optional[str]], dict[str, Any]]


def build_routes() -> list[Route]:
    return [
        Route("POST", r"^/works$", lambda reg, body, _, __: reg.register_work(
            body["title"], body["kind"], body["owner_org"],
            body.get("segments"), body.get("contributions"),
        )),
        Route("GET", r"^/works/(?P<id>[^/]+)$", lambda reg, _b, p, _v: reg.get_work_view(p["id"])),
        Route("GET", r"^/works/(?P<id>[^/]+)/risk$",
              lambda reg, _b, p, v: reg.risk_view(p["id"], v)),
        Route("GET", r"^/works/(?P<id>[^/]+)/segments/(?P<sid>[^/]+)$",
              lambda reg, _b, p, _v: reg.locate_segment(p["id"], p["sid"])),
        Route("POST", r"^/agreements$", lambda reg, body, _, __: reg.create_agreement(body)),
        Route("POST", r"^/agreements/(?P<id>[^/]+)/reschedule$",
              lambda reg, body, p, _v: reg.reschedule_agreement(p["id"], body)),
        Route("POST", r"^/handovers$", lambda reg, body, _, __: reg.record_handover(body)),
        # 理赔报案：锁定事故时基线
        Route("POST", r"^/incidents/(?P<id>[^/]+)/claim$",
              lambda reg, body, p, v: reg.file_claim(p["id"], body, v)),
        Route("GET", r"^/incidents/(?P<id>[^/]+)$",
              lambda reg, _b, p, v: reg.incident_view(p["id"], v)),
        # 四方按角色追加材料（估损/核赔结论/异议/修复方案/责任意见）
        Route("POST", r"^/claims/(?P<id>[^/]+)/additions$",
              lambda reg, body, p, v: reg.add_claim_material(p["id"], body, v)),
        # 资金分录：赔付（分期）/追偿；免赔额随核赔结论自动落账
        Route("POST", r"^/claims/(?P<id>[^/]+)/fund-entries$",
              lambda reg, body, p, v: reg.add_fund_entry(p["id"], body, v)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/close$",
              lambda reg, body, p, v: reg.close_claim(p["id"], body, v)),
        Route("GET", r"^/claims/(?P<id>[^/]+)$",
              lambda reg, _b, p, v: reg.claim_view(p["id"], v)),
        # 解冻双闸门：修复复核（出借馆+承借馆双签）与保管责任（当前保管方）
        Route("POST", r"^/incidents/(?P<id>[^/]+)/restoration-review$",
              lambda reg, body, p, v: reg.submit_restoration_review(p["id"], body, v)),
        Route("POST", r"^/incidents/(?P<id>[^/]+)/custody-confirmation$",
              lambda reg, body, p, v: reg.confirm_custody(p["id"], body, v)),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, body, p, _v: reg.create_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/publish$",
              lambda reg, body, p, _v: reg.publish_label(p["id"], body["published_on"])),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/correct$",
              lambda reg, body, p, _v: reg.correct_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, _b, p, _v: reg.label_version(p["id"], None)),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels/(?P<v>[0-9]+)$",
              lambda reg, _b, p, _v: reg.label_version(p["id"], int(p["v"]))),
    ]
