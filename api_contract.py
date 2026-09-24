"""HTTP 层契约：验证路由分发与 400/409 状态码映射。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from service import Handler


def post_json(base_url, path, payload, viewer_org=None):
    headers = {"Content-Type": "application/json"}
    if viewer_org:
        headers["X-Viewer-Org"] = quote(viewer_org)
    request = Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    return urlopen(request, timeout=2)


def get_json(base_url, path, viewer_org=None):
    headers = {}
    if viewer_org:
        headers["X-Viewer-Org"] = quote(viewer_org)
    request = Request(f"{base_url}{path}", headers=headers, method="GET")
    return urlopen(request, timeout=2)


def read_error(error):
    return error.code, json.loads(error.read().decode("utf-8"))


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类使用独立端口；进程内 STATE 由各用例使用不同作品号隔离。
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_register_and_fetch_work(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 烟霭图", "kind": "独立作品", "owner_org": "戊馆",
        }) as response:
            body = json.load(response)
        self.assertEqual(response.status, 200)
        work_id = body["work"]["work_id"]
        with urlopen(f"{self.base_url}/works/{work_id}", timeout=2) as response:
            self.assertEqual(json.load(response)["work"]["owner_org"], "戊馆")

    def test_invalid_kind_is_400(self):
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, "/works", {"title": "x", "kind": "瓷器", "owner_org": "戊馆"})
        code, body = read_error(error.exception)
        self.assertEqual(code, 400)
        self.assertIn("作品类型", body["error"])

    def test_malformed_json_is_400(self):
        request = Request(
            f"{self.base_url}/works", data=b"{not-json",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 400)

    def test_duplicate_scan_is_409_and_chain_continues(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 溪山图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, fr, tr, forg, torg):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": forg, "role": fr, "person": "甲"},
                "to_party": {"org": torg, "role": tr, "person": "乙"},
                "report": {"condition": "良好", "image_hashes": ["h"]},
            })

        with handover("出库", "NET-1", "出借馆", "运输方", "甲馆", "运输") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            handover("到馆", "NET-1", "运输方", "承借馆", "运输", "乙馆")
        code, body = read_error(error.exception)
        self.assertEqual(code, 409)
        self.assertIn("重复扫码", body["error"])
        # 新扫码办理到馆成功，证明被拒绝的重复扫码没有破坏生命周期。
        with handover("到馆", "NET-2", "运输方", "承借馆", "运输", "乙馆") as response:
            self.assertEqual(json.load(response)["resulting_status"], "待布展")

    def test_damage_freezes_next_handover_and_risk_reports_it(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 秋林图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]

        def handover(htype, scan, report):
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-22",
                "from_party": {"org": "甲馆", "role": "出借馆" if htype == "出库" else "运输方", "person": "甲"},
                "to_party": {"org": "运输", "role": "运输方" if htype == "出库" else "承借馆", "person": "乙"},
                "report": report,
            })

        handover("出库", "D-1", {"condition": "良好", "image_hashes": ["ok"]}).close()
        with handover("到馆", "D-2", {
            "condition": "损伤", "damage_note": "新增折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            self.assertTrue(json.load(response)["frozen"])
        with self.assertRaises(HTTPError) as error:
            handover("布展", "D-3", {"condition": "良好", "image_hashes": ["ok"]})
        self.assertEqual(error.exception.code, 409)
        with urlopen(f"{self.base_url}/works/{work_id}/risk", timeout=2) as response:
            risk = json.load(response)
        self.assertTrue(risk["frozen"])
        self.assertEqual(len(risk["open_risks"]), 1)
        self.assertEqual(risk["open_risks"][0]["before_hashes"], ["a" * 64])


class ClaimFlowApiTest(unittest.TestCase):
    """HTTP 层：报案锁定基线、角色追加、资金守恒、双闸门解冻、脱敏头。"""

    INSURER = "安保保险"
    TRANSPORTER = "长风运输"

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _open_damaged_case(self):
        with post_json(self.base_url, "/works", {
            "title": "HTTP 理赔图", "kind": "独立作品", "owner_org": "甲馆",
        }) as response:
            work_id = json.load(response)["work"]["work_id"]
        post_json(self.base_url, "/agreements", {
            "work_id": work_id,
            "lender_org": "甲馆", "borrower_org": "乙馆",
            "start_on": "2026-10-01", "end_on": "2026-12-31",
            "gallery": "三号厅", "max_lux": 50,
            "transport": {"mode": "专车恒温"},
            "insurance": {"coverage": "钉到钉", "policy": "POL-001"},
            "digital_rights": {"web": True},
        }).close()

        def handover(htype, scan, report):
            pairs = {
                "出库": ("出借馆", "运输方", "甲馆", self.TRANSPORTER),
                "到馆": ("运输方", "承借馆", self.TRANSPORTER, "乙馆"),
            }
            fr, tr, forg, torg = pairs[htype]
            return post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": htype, "scan_code": scan,
                "on_date": "2026-09-27",
                "from_party": {"org": forg, "role": fr, "person": "甲"},
                "to_party": {"org": torg, "role": tr, "person": "乙"},
                "report": report,
            })

        handover("出库", "C-1", {"condition": "良好", "image_hashes": ["ok"]}).close()
        with handover("到馆", "C-2", {
            "condition": "损伤", "damage_note": "新增折痕",
            "before_hashes": ["a" * 64], "after_hashes": ["b" * 64],
        }) as response:
            incident_id = json.load(response)["incident_id"]
        return work_id, incident_id

    def _material(self, base_url, claim_id, payload, viewer):
        with post_json(base_url, f"/claims/{claim_id}/additions", payload, viewer) as r:
            return json.load(r)

    def test_full_claim_flow_locks_baseline_and_requires_both_gates(self):
        work_id, incident_id = self._open_damaged_case()

        # 报案：出借馆报案并声明保险方
        with post_json(self.base_url, f"/incidents/{incident_id}/claim", {
            "org": "甲馆", "insurer_org": self.INSURER, "filed_on": "2026-09-28",
        }, "甲馆") as response:
            claim = json.load(response)
        claim_id = claim["claim_id"]
        self.assertTrue(claim["baseline"]["locked"])
        self.assertEqual(claim["baseline"]["agreement_version"]["insurance"]["coverage"], "钉到钉")
        self.assertTrue(claim["baseline"]["signatures"])

        # 未授权机构：图像摘要与条款被脱敏
        with get_json(self.base_url, f"/claims/{claim_id}", "某小报") as response:
            redacted = json.load(response)
        self.assertFalse(redacted["authorized"])
        self.assertTrue(redacted["baseline"]["image_summary"]["redacted"])

        # 角色越权追加：承借馆替保险方估损 → 400
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, f"/claims/{claim_id}/additions", {
                "kind": "估损", "org": "乙馆", "role": "承借馆", "person": "乙",
                "on_date": "2026-10-01", "summary": "自估", "amount": 1000,
            }, "乙馆")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        # 保险方估损 → 部分认可（免赔 1 万）
        self._material(self.base_url, claim_id, {
            "kind": "估损", "org": self.INSURER, "role": "保险方", "person": "理赔员",
            "on_date": "2026-10-01", "summary": "估损 10 万", "amount": 100000,
        }, self.INSURER)
        self._material(self.base_url, claim_id, {
            "kind": "核赔结论", "org": self.INSURER, "role": "保险方", "person": "理赔员",
            "on_date": "2026-10-03", "summary": "部分认可",
            "decision": "部分认可", "accepted_amount": 90000, "deductible_amount": 10000,
        }, self.INSURER)

        # 重复回执 → 409，不多记
        for receipt in ("RCPT-1", "RCPT-2"):
            post_json(self.base_url, f"/claims/{claim_id}/fund-entries", {
                "kind": "赔付", "amount": 45000, "receipt_no": receipt,
                "installment_no": 1 if receipt == "RCPT-1" else 2,
                "on_date": "2026-10-08",
                "org": self.INSURER, "role": "保险方", "person": "出纳",
            }, self.INSURER).close()
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, f"/claims/{claim_id}/fund-entries", {
                "kind": "赔付", "amount": 45000, "receipt_no": "RCPT-1",
                "installment_no": 3, "on_date": "2026-10-09",
                "org": self.INSURER, "role": "保险方", "person": "出纳",
            }, self.INSURER)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 承借馆修复方案 + 双馆复核（修复闸门）
        self._material(self.base_url, claim_id, {
            "kind": "修复方案", "org": "乙馆", "role": "承借馆", "person": "修复师",
            "on_date": "2026-10-02", "summary": "局部托裱",
        }, "乙馆")
        post_json(self.base_url, f"/incidents/{incident_id}/restoration-review", {
            "on_date": "2026-10-10", "note": "双馆书面复核通过",
            "reviewers": [
                {"org": "甲馆", "role": "出借馆", "person": "甲"},
                {"org": "乙馆", "role": "承借馆", "person": "乙"},
            ],
        }, "乙馆").close()

        # 仅修复闸门通过：仍冻结，不能布展
        with self.assertRaises(HTTPError) as error:
            post_json(self.base_url, "/handovers", {
                "work_id": work_id, "type": "布展", "scan_code": "C-3",
                "on_date": "2026-10-11",
                "from_party": {"org": "乙馆", "role": "承借馆", "person": "甲"},
                "to_party": {"org": "乙馆", "role": "承借馆", "person": "乙"},
                "report": {"condition": "良好", "image_hashes": ["ok"]},
            })
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

        # 理赔可先关闭；关闭本身不解冻
        post_json(self.base_url, f"/claims/{claim_id}/close", {
            "org": self.INSURER, "role": "保险方", "person": "理赔员",
            "on_date": "2026-10-12", "note": "赔款付清关闭",
        }, self.INSURER).close()

        # 第二道闸门：当前保管方（承借馆）签认后解冻
        post_json(self.base_url, f"/incidents/{incident_id}/custody-confirmation", {
            "org": "乙馆", "role": "承借馆", "person": "保管员",
            "on_date": "2026-10-12", "note": "接管修复后作品",
        }, "乙馆").close()
        with get_json(self.base_url, f"/works/{work_id}/risk", "甲馆") as response:
            risk = json.load(response)
        self.assertFalse(risk["frozen"])
        self.assertEqual(risk["open_risks"], [])

        # 解冻后交接链恢复
        with post_json(self.base_url, "/handovers", {
            "work_id": work_id, "type": "布展", "scan_code": "C-3",
            "on_date": "2026-10-13",
            "from_party": {"org": "乙馆", "role": "承借馆", "person": "甲"},
            "to_party": {"org": "乙馆", "role": "承借馆", "person": "乙"},
            "report": {"condition": "良好", "image_hashes": ["ok"]},
        }) as response:
            self.assertEqual(json.load(response)["resulting_status"], "展出中")


if __name__ == "__main__":
    unittest.main()
