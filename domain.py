"""馆际作品借展的领域核心。

只依赖标准库，集中表达四类业务规则：

1. 复合作品结构：实体作品 → 组成区段 → 作者贡献（含合作顺序、题跋）。
2. 借展协议：约束展期、展厅、照度、运输、保险与数字传播用途。
3. 状态交接：出库/到馆/布展/撤展/归还由交接双方签认；重复扫码幂等；
   发现损伤立即冻结后续动作并保全前后图像哈希。
4. 策展展签：发布日期确认后锁定证据快照，后来的学术更正只产生新版，
   不改变旧版展签；任意版本都可从展签定位到实体、贡献区段、当前保管
   责任、授权范围与未解除风险。
5. 损伤理赔：报案锁定事故发生时有效的保险条款、交接双方签认与图像摘要；
   四方按角色追加材料且各成新版本；资金分录保持金额守恒、回执幂等；
   理赔关闭与作品解冻分别判断，未获授权的机构看不到作品敏感材料。
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

ROLES = ("出借馆", "承借馆", "运输方", "策展人")

WORK_KINDS = ("独立作品", "合作画", "历史图录", "长卷")

CONTRIBUTION_KINDS = ("作画", "题跋", "书引首", "鉴藏印", "题签")

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

# 理赔参与角色：在借展角色之外引入保险方
CLAIM_ROLES = ("保险方", "出借馆", "承借馆", "运输方")

# 补充材料类型；除异议向所有当事方开放外，每种材料都有固定提交角色
SUBMISSION_KINDS = ("估损", "异议", "修复方案", "责任意见")
SUBMISSION_KIND_ROLES = {
    "估损": ("保险方",),
    "修复方案": ("承借馆",),
    "责任意见": ("出借馆", "运输方"),
    "异议": CLAIM_ROLES,
}

# 资金分录类型：认可与免赔由理赔决定一并登记，赔付与追偿逐笔追加
ENTRY_KINDS = ("认可", "免赔", "赔付", "追偿")

# 理赔阶段（由案件状态推导，不直接赋值）
CLAIM_STAGES = ("已报案", "定损中", "已决定", "赔付中", "已结清", "已关闭")


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

    @property
    def damaged(self) -> bool:
        return self.report.condition == "损伤" or bool(self.report.damage_note)


@dataclass
class Incident:
    """损伤事件：冻结后所有未完成交接，图像哈希作为证据保全。"""

    incident_id: str
    work_id: str
    handover_id: str
    on_date: str
    note: str
    before_hashes: list[str]
    after_hashes: list[str]
    resolved: bool = False
    resolution_note: str = ""
    agreement_id: Optional[str] = None  # 事故发生时有效的协议版本
    claim_id: Optional[str] = None  # 该事件已报案的理赔案


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


@dataclass
class ClaimSubmission:
    """补充材料的一个不可变版本：估损/异议/修复方案/责任意见。"""

    version: int
    kind: str
    by: Signature
    summary: str
    amount_cents: Optional[int]
    image_hashes: list[str]
    submitted_on: str


@dataclass
class FundEntry:
    """资金分录：认可/免赔/赔付/追偿，逐笔追加，保持金额守恒。"""

    seq: int
    kind: str
    amount_cents: int
    receipt_id: str
    by_org: str
    note: str
    recorded_on: str


@dataclass
class ClaimDecision:
    """理赔决定：认可金额（可部分认可）、免赔额与被引用的材料版本。"""

    by: Signature
    approved_cents: int
    deductible_cents: int
    cited_versions: list[int]
    decided_on: str
    note: str


@dataclass
class Claim:
    """理赔案：报案时锁定基准证据，之后只追加，不替换。"""

    claim_id: str
    work_id: str
    incident_id: str
    insurer_org: str
    filed_by: Signature
    filed_on: str
    baseline: dict[str, Any]
    submissions: list[ClaimSubmission] = field(default_factory=list)
    decision: Optional[ClaimDecision] = None
    ledger: list[FundEntry] = field(default_factory=list)
    restoration_reviewed: bool = False
    restoration_review_note: str = ""
    custody_acknowledged: bool = False
    custody_ack_by: str = ""
    closed: bool = False
    closed_on: Optional[str] = None


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class LoanRegistry:
    """保存全部借展记录并强制业务规则。

    对外使用命令式方法（register_work / record_handover / file_claim / ...），
    查询通过 get_work_view / label_version / risk_view / claim_view 等只读视图。
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
        if work.work_id in self._frozen_works:
            raise ConflictError(f"作品 {work.work_id} 已因损伤冻结，须先解除风险")

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
        )
        self._scan_codes.add(scan_code)
        self.handovers.append(handover)

        if handover.damaged:
            self._frozen_works.add(work.work_id)
            agreement = self._current_agreement(work.work_id)
            incident = Incident(
                incident_id=_new_id("incident"),
                work_id=work.work_id,
                handover_id=handover.handover_id,
                on_date=handover.on_date,
                note=report.damage_note,
                before_hashes=list(report.before_hashes),
                after_hashes=list(report.after_hashes),
                agreement_id=agreement.agreement_id if agreement else None,
            )
            self.incidents.append(incident)
            return self._handover_view(handover, frozen=True, incident_id=incident.incident_id)

        return self._handover_view(handover, frozen=False)

    def resolve_incident(self, incident_id: str, resolution_note: str) -> dict[str, Any]:
        """损伤经双方确认解除后解冻；解除前风险一直挂账。

        已报案的损伤须同时满足出借馆修复复核与保管责任确认才能解冻，
        与理赔是否关闭分别判断。
        """
        incident = next((i for i in self.incidents if i.incident_id == incident_id), None)
        if incident is None:
            raise DomainError(f"损伤事件 {incident_id} 不存在")
        if not resolution_note or not resolution_note.strip():
            raise DomainError("解除损伤须填写处理与复核结论")
        claim = next((c for c in self.claims.values() if c.incident_id == incident.incident_id), None)
        if claim is not None:
            missing = []
            if not claim.restoration_reviewed:
                missing.append("出借馆修复复核")
            if not claim.custody_acknowledged:
                missing.append("保管责任确认")
            if missing:
                raise ConflictError(
                    f"理赔 {claim.claim_id} 尚未满足解冻条件：{'、'.join(missing)}"
                )
        incident.resolved = True
        incident.resolution_note = resolution_note.strip()
        self._frozen_works.discard(incident.work_id)
        return {
            "incident_id": incident.incident_id,
            "work_id": incident.work_id,
            "resolved": True,
            "resolution_note": incident.resolution_note,
        }

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

    # -- 损伤理赔 ----------------------------------------------------------

    def file_claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        """报案：锁定事故发生时有效的保险条款、交接双方签认与图像摘要。

        锁定的基准取自损伤事件登记时的协议版本；此后跨馆改期只产生新的
        协议版本，不改变本案的保障范围。
        """
        work = self._work(payload["work_id"])
        incident = next(
            (i for i in self.incidents if i.incident_id == payload.get("incident_id")), None
        )
        if incident is None:
            raise DomainError(f"损伤事件 {payload.get('incident_id')} 不存在")
        if incident.work_id != work.work_id:
            raise DomainError("损伤事件不属于该作品")
        if incident.resolved:
            raise ConflictError("损伤已复核解除，不能再就该事件报案")
        if incident.claim_id:
            raise ConflictError(
                f"损伤事件 {incident.incident_id} 已报案（{incident.claim_id}），不能重复报案"
            )
        if not incident.agreement_id or incident.agreement_id not in self.agreements:
            raise DomainError("事故发生时无有效借展协议，无法锁定保险条款")
        agreement = self.agreements[incident.agreement_id]
        if not agreement.insurance:
            raise DomainError("事故时有效的协议未约定保险条款，不能报案")

        insurer_org = str(payload.get("insurer_org", "") or "").strip()
        if not insurer_org:
            raise DomainError("报案须指明保险方机构")

        filed_by = self._claim_signature(payload.get("filed_by") or {})
        if filed_by.role not in ("出借馆", "承借馆"):
            raise DomainError("报案人须为出借馆或承借馆")
        expected_org = agreement.lender_org if filed_by.role == "出借馆" else agreement.borrower_org
        if filed_by.org != expected_org:
            raise DomainError(f"{filed_by.role}报案机构须为 {expected_org}，收到 {filed_by.org}")

        filed_on = self._date(payload["filed_on"], "报案日期")
        if filed_on.isoformat() < incident.on_date:
            raise DomainError("报案日期不能早于事故日期")

        handover = next(h for h in self.handovers if h.handover_id == incident.handover_id)
        baseline = {
            "agreement_id": agreement.agreement_id,
            "agreement_version": agreement.version,
            "insurance": copy.deepcopy(agreement.insurance),
            "handover_id": handover.handover_id,
            "incident_on": incident.on_date,
            "signatures": {
                "from_party": {
                    "org": handover.from_party.org,
                    "role": handover.from_party.role,
                    "person": handover.from_party.person,
                },
                "to_party": {
                    "org": handover.to_party.org,
                    "role": handover.to_party.role,
                    "person": handover.to_party.person,
                },
            },
            "image_digests": {
                "before_hashes": list(incident.before_hashes),
                "after_hashes": list(incident.after_hashes),
                "handover_hashes": list(handover.report.image_hashes),
            },
        }
        claim = Claim(
            claim_id=_new_id("claim"),
            work_id=work.work_id,
            incident_id=incident.incident_id,
            insurer_org=insurer_org,
            filed_by=filed_by,
            filed_on=filed_on.isoformat(),
            baseline=baseline,
        )
        self.claims[claim.claim_id] = claim
        incident.claim_id = claim.claim_id
        return self.claim_view(claim.claim_id, viewer_org=filed_by.org)

    def add_submission(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """按角色追加补充材料；每次追加都形成新版本，旧版本永不替换。"""
        claim = self._claim(claim_id)
        if claim.closed:
            raise ConflictError("理赔案已关闭，不能再追加材料")
        kind = payload.get("kind", "")
        if kind not in SUBMISSION_KINDS:
            raise DomainError(f"补充材料类型须为 {SUBMISSION_KINDS} 之一")
        by = self._assert_claim_party(claim, payload.get("by") or {}, SUBMISSION_KIND_ROLES[kind])
        summary = str(payload.get("summary", "") or "").strip()
        if not summary:
            raise DomainError("补充材料须填写摘要")
        amount_cents: Optional[int] = None
        if kind == "估损":
            amount_cents = self._cents(payload.get("amount"), "估损金额")
            if amount_cents <= 0:
                raise DomainError("估损金额须为正数")
        submitted_on = str(payload.get("submitted_on") or date.today().isoformat())
        self._date(submitted_on, "提交日期")
        submission = ClaimSubmission(
            version=len(claim.submissions) + 1,
            kind=kind,
            by=by,
            summary=summary,
            amount_cents=amount_cents,
            image_hashes=[self._image_hash(h) for h in payload.get("image_hashes", [])],
            submitted_on=submitted_on,
        )
        claim.submissions.append(submission)
        view = self._submission_view(claim, submission)
        view["claim_id"] = claim.claim_id
        view["stage"] = self._claim_stage(claim)
        return view

    def decide_claim(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """保险方理赔决定：部分认可与免赔额入账，引用材料版本；决定只生效一次。"""
        claim = self._claim(claim_id)
        if claim.closed:
            raise ConflictError("理赔案已关闭")
        if claim.decision is not None:
            raise ConflictError("理赔决定已登记，并发或重复决定不得再次生效")
        by = self._assert_claim_party(claim, payload.get("by") or {}, ("保险方",))
        approved = self._cents(payload.get("approved_amount"), "认可金额")
        deductible = self._cents(payload.get("deductible", 0), "免赔额")
        if approved < 0 or deductible < 0:
            raise DomainError("认可金额与免赔额不能为负")
        if deductible > approved:
            raise DomainError("免赔额不能超过认可金额")
        known = {s.version for s in claim.submissions}
        cited = [int(v) for v in payload.get("cited_versions", [])]
        for version in cited:
            if version not in known:
                raise DomainError(f"材料版本 v{version} 不存在，不能被决定引用")
        decided_on = str(payload.get("decided_on") or date.today().isoformat())
        self._date(decided_on, "决定日期")
        claim.decision = ClaimDecision(
            by=by,
            approved_cents=approved,
            deductible_cents=deductible,
            cited_versions=cited,
            decided_on=decided_on,
            note=str(payload.get("note", "") or ""),
        )
        # 认可与免赔作为首两笔资金分录入账，之后的赔付/追偿在此基础上守恒。
        self._append_entry(claim, "认可", approved, f"{claim.claim_id}-决定-认可",
                           by.org, "理赔决定认可金额", decided_on)
        if deductible:
            self._append_entry(claim, "免赔", deductible, f"{claim.claim_id}-决定-免赔",
                               by.org, "理赔决定免赔额", decided_on)
        return self.claim_view(claim.claim_id, viewer_org=by.org)

    def append_entry(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """追加赔付/追偿分录：回执唯一（幂等），并保持金额守恒。"""
        claim = self._claim(claim_id)
        if claim.closed:
            raise ConflictError("理赔案已关闭，不能再登记资金分录")
        if claim.decision is None:
            raise ConflictError("须先完成理赔决定，才能登记赔付或追偿")
        kind = payload.get("kind", "")
        if kind not in ("赔付", "追偿"):
            raise DomainError("资金分录类型须为 赔付 或 追偿（认可与免赔由理赔决定登记）")
        by = self._assert_claim_party(claim, payload.get("by") or {}, ("保险方",))
        amount = self._cents(payload.get("amount"), "分录金额")
        if amount <= 0:
            raise DomainError("分录金额须为正数")
        receipt_id = str(payload.get("receipt_id", "") or "").strip()
        if not receipt_id:
            raise DomainError("资金分录须附回执编号")
        recorded_on = str(payload.get("recorded_on") or date.today().isoformat())
        self._date(recorded_on, "登记日期")
        entry = self._append_entry(
            claim, kind, amount, receipt_id, by.org,
            str(payload.get("note", "") or ""), recorded_on,
        )
        view = self._entry_view(entry)
        view["claim_id"] = claim.claim_id
        view["stage"] = self._claim_stage(claim)
        view["funds"] = self._funds_view(claim)
        return view

    def review_restoration(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """出借馆修复复核：解冻的两个前提之一；通过后结论不得更改。"""
        claim = self._claim(claim_id)
        by = self._assert_claim_party(claim, payload.get("by") or {}, ("出借馆",))
        if not any(s.kind == "修复方案" for s in claim.submissions):
            raise DomainError("承借馆尚未提交修复方案，不能复核")
        if claim.restoration_reviewed:
            raise ConflictError("修复复核已通过，结论不得更改")
        conclusion = payload.get("conclusion", "")
        if conclusion not in ("通过", "不通过"):
            raise DomainError("复核结论须为 通过 或 不通过")
        claim.restoration_review_note = str(payload.get("note", "") or "").strip()
        if conclusion == "通过":
            claim.restoration_reviewed = True
        return {
            "claim_id": claim.claim_id,
            "conclusion": conclusion,
            "restoration_reviewed": claim.restoration_reviewed,
            "reviewed_by": by.org,
        }

    def acknowledge_custody(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """当前保管方确认保管责任：解冻的另一个前提。"""
        claim = self._claim(claim_id)
        by = payload.get("by") or {}
        org = str(by.get("org", "") or "").strip()
        person = str(by.get("person", "") or "").strip()
        if not person:
            raise DomainError("须指定签认人")
        custodian = self._custody(claim.work_id)["custodian_org"]
        if org != custodian:
            raise DomainError(f"当前保管方为 {custodian}，保管责任须由其确认，收到 {org or '空'}")
        if claim.custody_acknowledged:
            raise ConflictError("保管责任已确认，不能重复签认")
        claim.custody_acknowledged = True
        claim.custody_ack_by = org
        return {
            "claim_id": claim.claim_id,
            "custody_acknowledged": True,
            "acknowledged_by": org,
        }

    def close_claim(self, claim_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """关闭理赔：须赔付结清；与作品解冻分别判断，互不为前提。"""
        claim = self._claim(claim_id)
        self._assert_claim_party(claim, payload.get("by") or {}, ("保险方",))
        if claim.closed:
            raise ConflictError("理赔案已关闭")
        if claim.decision is None:
            raise ConflictError("尚未完成理赔决定，不能关闭")
        if self._fund_totals(claim)["outstanding"] > 0:
            raise ConflictError("赔付未结清，不能关闭理赔")
        claim.closed = True
        claim.closed_on = str(payload.get("closed_on") or date.today().isoformat())
        self._date(claim.closed_on, "关闭日期")
        return {
            "claim_id": claim.claim_id,
            "closed": True,
            "closed_on": claim.closed_on,
            "stage": "已关闭",
        }

    def claim_view(self, claim_id: str, viewer_org: Optional[str] = None) -> dict[str, Any]:
        """理赔案视图；未获授权的机构只能看到阶段等公开信息，敏感材料隐去。"""
        claim = self._claim(claim_id)
        if viewer_org is None or viewer_org not in self._claim_orgs(claim):
            return {
                "claim_id": claim.claim_id,
                "work_id": claim.work_id,
                "stage": self._claim_stage(claim),
                "filed_on": claim.filed_on,
                "closed": claim.closed,
                "sensitive_redacted": True,
                "message": "未获授权的机构看不到作品敏感材料",
            }
        return {
            "claim_id": claim.claim_id,
            "work_id": claim.work_id,
            "incident_id": claim.incident_id,
            "stage": self._claim_stage(claim),
            "filed_on": claim.filed_on,
            "filed_by": {
                "org": claim.filed_by.org,
                "role": claim.filed_by.role,
                "person": claim.filed_by.person,
            },
            "insurer_org": claim.insurer_org,
            "baseline": copy.deepcopy(claim.baseline),
            "submissions": [self._submission_view(claim, s) for s in claim.submissions],
            "decision": self._decision_view(claim.decision) if claim.decision else None,
            "ledger": [self._entry_view(e) for e in claim.ledger],
            "funds": self._funds_view(claim),
            "restoration_review": {
                "reviewed": claim.restoration_reviewed,
                "note": claim.restoration_review_note,
            },
            "custody_acknowledgement": {
                "acknowledged": claim.custody_acknowledged,
                "by_org": claim.custody_ack_by,
            },
            "missing_signoffs": self._missing_signoffs(claim),
            "closed": claim.closed,
            "closed_on": claim.closed_on,
        }

    def _claim(self, claim_id: str) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise DomainError(f"理赔案 {claim_id} 不存在")
        return claim

    @staticmethod
    def _claim_signature(raw: dict[str, Any]) -> Signature:
        if not raw or not raw.get("person"):
            raise DomainError("须指定签认人")
        role = raw.get("role", "")
        if role not in CLAIM_ROLES:
            raise DomainError(f"理赔角色须为 {CLAIM_ROLES} 之一")
        org = str(raw.get("org", "") or "").strip()
        if not org:
            raise DomainError("须指明机构")
        return Signature(org=org, role=role, person=raw["person"])

    def _claim_role_orgs(self, claim: Claim, role: str) -> set[str]:
        """角色对应的法定机构：材料、决定与分录须由这些机构提交。"""
        agreement = self.agreements[claim.baseline["agreement_id"]]
        if role == "保险方":
            return {claim.insurer_org}
        if role == "出借馆":
            return {agreement.lender_org}
        if role == "承借馆":
            return {agreement.borrower_org}
        if role == "运输方":
            return {
                party.org
                for h in self.handovers
                if h.work_id == claim.work_id
                for party in (h.from_party, h.to_party)
                if party.role == "运输方" and party.org
            }
        return set()

    def _assert_claim_party(
        self, claim: Claim, raw: dict[str, Any], allowed_roles: tuple[str, ...]
    ) -> Signature:
        by = self._claim_signature(raw)
        if by.role not in allowed_roles:
            raise DomainError(f"该动作须由 {'、'.join(allowed_roles)} 办理，收到的是 {by.role}")
        orgs = self._claim_role_orgs(claim, by.role)
        if by.org not in orgs:
            raise DomainError(f"{by.role}身份须由 {'、'.join(sorted(orgs))} 出面，收到 {by.org}")
        return by

    def _claim_orgs(self, claim: Claim) -> set[str]:
        """有权查看理赔敏感材料的机构：出借馆、承借馆、保险方与运输方。"""
        orgs = self._claim_role_orgs(claim, "出借馆")
        orgs |= self._claim_role_orgs(claim, "承借馆")
        orgs |= self._claim_role_orgs(claim, "运输方")
        orgs.add(claim.insurer_org)
        orgs.add(claim.filed_by.org)
        return orgs

    def _append_entry(
        self,
        claim: Claim,
        kind: str,
        amount_cents: int,
        receipt_id: str,
        by_org: str,
        note: str,
        recorded_on: str,
    ) -> FundEntry:
        if any(e.receipt_id == receipt_id for e in claim.ledger):
            raise ConflictError(f"回执 {receipt_id} 已登记，重复回执不得多记赔款")
        totals = self._fund_totals(claim)
        if kind == "赔付" and totals["paid"] + amount_cents > totals["recognized"] - totals["deductible"]:
            raise DomainError("赔付累计超过认可金额减去免赔额，破坏金额守恒")
        if kind == "追偿" and totals["recovered"] + amount_cents > totals["paid"]:
            raise DomainError("追偿累计超过已赔付金额，破坏金额守恒")
        entry = FundEntry(
            seq=len(claim.ledger) + 1,
            kind=kind,
            amount_cents=amount_cents,
            receipt_id=receipt_id,
            by_org=by_org,
            note=note,
            recorded_on=recorded_on,
        )
        claim.ledger.append(entry)
        return entry

    def _fund_totals(self, claim: Claim) -> dict[str, int]:
        """金额守恒：认可 = 免赔 + 已付 + 待付；追偿不超过已付。"""
        recognized = sum(e.amount_cents for e in claim.ledger if e.kind == "认可")
        deductible = sum(e.amount_cents for e in claim.ledger if e.kind == "免赔")
        paid = sum(e.amount_cents for e in claim.ledger if e.kind == "赔付")
        recovered = sum(e.amount_cents for e in claim.ledger if e.kind == "追偿")
        return {
            "recognized": recognized,
            "deductible": deductible,
            "paid": paid,
            "recovered": recovered,
            "outstanding": recognized - deductible - paid,
        }

    def _funds_view(self, claim: Claim) -> dict[str, float]:
        return {key: cents / 100 for key, cents in self._fund_totals(claim).items()}

    def _claim_stage(self, claim: Claim) -> str:
        if claim.closed:
            return "已关闭"
        if claim.decision is None:
            has_estimate = any(s.kind == "估损" for s in claim.submissions)
            return "定损中" if has_estimate else "已报案"
        totals = self._fund_totals(claim)
        if totals["paid"] <= 0:
            return "已决定"
        if totals["outstanding"] > 0:
            return "赔付中"
        return "已结清"

    def _missing_signoffs(self, claim: Claim) -> list[str]:
        """风险追溯用：还缺哪些签认（估损、决定、修复复核、保管责任确认）。"""
        missing = []
        if not any(s.kind == "估损" for s in claim.submissions):
            missing.append("保险方估损")
        if claim.decision is None:
            missing.append("保险方理赔决定")
        if not claim.restoration_reviewed:
            missing.append("出借馆修复复核")
        if not claim.custody_acknowledged:
            custodian = self._custody(claim.work_id)["custodian_org"]
            missing.append(f"{custodian}保管责任确认")
        return missing

    def _claim_summary_view(self, claim: Claim) -> dict[str, Any]:
        return {
            "claim_id": claim.claim_id,
            "incident_id": claim.incident_id,
            "stage": self._claim_stage(claim),
            "filed_on": claim.filed_on,
            "closed": claim.closed,
            "restoration_reviewed": claim.restoration_reviewed,
            "custody_acknowledged": claim.custody_acknowledged,
            "missing_signoffs": self._missing_signoffs(claim),
            "funds": self._funds_view(claim),
        }

    def _submission_view(self, claim: Claim, submission: ClaimSubmission) -> dict[str, Any]:
        cited = claim.decision.cited_versions if claim.decision else []
        return {
            "version": submission.version,
            "kind": submission.kind,
            "by": {
                "org": submission.by.org,
                "role": submission.by.role,
                "person": submission.by.person,
            },
            "summary": submission.summary,
            "amount": submission.amount_cents / 100 if submission.amount_cents is not None else None,
            "image_hashes": list(submission.image_hashes),
            "submitted_on": submission.submitted_on,
            "cited_by_decision": submission.version in cited,
        }

    @staticmethod
    def _decision_view(decision: ClaimDecision) -> dict[str, Any]:
        return {
            "decided_by": {
                "org": decision.by.org,
                "role": decision.by.role,
                "person": decision.by.person,
            },
            "approved_amount": decision.approved_cents / 100,
            "deductible": decision.deductible_cents / 100,
            "cited_versions": list(decision.cited_versions),
            "decided_on": decision.decided_on,
            "note": decision.note,
        }

    @staticmethod
    def _entry_view(entry: FundEntry) -> dict[str, Any]:
        return {
            "seq": entry.seq,
            "kind": entry.kind,
            "amount": entry.amount_cents / 100,
            "receipt_id": entry.receipt_id,
            "by_org": entry.by_org,
            "note": entry.note,
            "recorded_on": entry.recorded_on,
        }

    @staticmethod
    def _cents(value: Any, field_name: str = "金额") -> int:
        """金额一律换算为分保存，避免浮点误差破坏守恒。"""
        try:
            return int(round(float(str(value)) * 100))
        except (TypeError, ValueError):
            raise DomainError(f"{field_name}须为数字")

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
        """风险追溯：开放损伤、理赔阶段、缺少的签认与资金变化并列呈现。

        viewer_org 未获授权时，隐去图像摘要等作品敏感材料。
        """
        work = self._work(work_id)
        agreement = self._current_agreement(work_id)
        authorized = viewer_org is None or viewer_org in self._work_orgs(work_id)
        open_risks = []
        for incident in self.incidents:
            if incident.work_id != work_id or incident.resolved:
                continue
            item: dict[str, Any] = {
                "incident_id": incident.incident_id,
                "on_date": incident.on_date,
                "note": incident.note,
            }
            if authorized:
                item["before_hashes"] = incident.before_hashes
                item["after_hashes"] = incident.after_hashes
            else:
                item["sensitive_redacted"] = True
            open_risks.append(item)
        return {
            "work": work.to_ref(),
            "custody": self._custody(work_id),
            "authorization": agreement.authorization_scope() if agreement else None,
            "open_risks": open_risks,
            "claims": [
                self._claim_summary_view(claim)
                for claim in self.claims.values()
                if claim.work_id == work_id
            ],
            "frozen": work_id in self._frozen_works,
        }

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

    def _work_orgs(self, work_id: str) -> set[str]:
        """有权查看作品敏感材料的机构：权属、协议双方、运输方与保险方。"""
        orgs = {self.works[work_id].owner_org}
        agreement = self._current_agreement(work_id)
        if agreement:
            orgs.update([agreement.lender_org, agreement.borrower_org])
        for handover in self.handovers:
            if handover.work_id == work_id:
                for party in (handover.from_party, handover.to_party):
                    if party.org:
                        orgs.add(party.org)
        for claim in self.claims.values():
            if claim.work_id == work_id:
                orgs.add(claim.insurer_org)
                orgs.add(claim.filed_by.org)
        return orgs

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
    handler: Callable[[LoanRegistry, dict[str, Any], dict[str, str]], dict[str, Any]]


def build_routes() -> list[Route]:
    return [
        Route("POST", r"^/works$", lambda reg, body, _: reg.register_work(
            body["title"], body["kind"], body["owner_org"],
            body.get("segments"), body.get("contributions"),
        )),
        Route("GET", r"^/works/(?P<id>[^/]+)$", lambda reg, _b, p: reg.get_work_view(p["id"])),
        Route("GET", r"^/works/(?P<id>[^/]+)/risk$",
              lambda reg, _b, p: reg.risk_view(p["id"], p.get("viewer_org"))),
        Route("GET", r"^/works/(?P<id>[^/]+)/segments/(?P<sid>[^/]+)$",
              lambda reg, _b, p: reg.locate_segment(p["id"], p["sid"])),
        Route("POST", r"^/agreements$", lambda reg, body, _: reg.create_agreement(body)),
        Route("POST", r"^/agreements/(?P<id>[^/]+)/reschedule$",
              lambda reg, body, p: reg.reschedule_agreement(p["id"], body)),
        Route("POST", r"^/handovers$", lambda reg, body, _: reg.record_handover(body)),
        Route("POST", r"^/incidents/(?P<id>[^/]+)/resolve$",
              lambda reg, body, p: reg.resolve_incident(p["id"], body.get("resolution_note", ""))),
        Route("POST", r"^/claims$", lambda reg, body, _: reg.file_claim(body)),
        Route("GET", r"^/claims/(?P<id>[^/]+)$",
              lambda reg, _b, p: reg.claim_view(p["id"], p.get("viewer_org"))),
        Route("POST", r"^/claims/(?P<id>[^/]+)/submissions$",
              lambda reg, body, p: reg.add_submission(p["id"], body)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/decide$",
              lambda reg, body, p: reg.decide_claim(p["id"], body)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/entries$",
              lambda reg, body, p: reg.append_entry(p["id"], body)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/restoration-review$",
              lambda reg, body, p: reg.review_restoration(p["id"], body)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/custody-ack$",
              lambda reg, body, p: reg.acknowledge_custody(p["id"], body)),
        Route("POST", r"^/claims/(?P<id>[^/]+)/close$",
              lambda reg, body, p: reg.close_claim(p["id"], body)),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, body, p: reg.create_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/publish$",
              lambda reg, body, p: reg.publish_label(p["id"], body["published_on"])),
        Route("POST", r"^/works/(?P<id>[^/]+)/labels/correct$",
              lambda reg, body, p: reg.correct_label(p["id"], body["narrative"], body.get("citations", []))),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels$",
              lambda reg, _b, p: reg.label_version(p["id"], None)),
        Route("GET", r"^/works/(?P<id>[^/]+)/labels/(?P<v>[0-9]+)$",
              lambda reg, _b, p: reg.label_version(p["id"], int(p["v"]))),
    ]
