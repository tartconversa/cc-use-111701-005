import json
import tempfile
import unittest
from pathlib import Path

from app import checkpoint as cp
from app import portal_ledger as ledger
from app.collector import CollectionAborted, PortalCollector
from app.portal_driver import (
    CONDITION_DETAIL_READY, ImageUnavailable, Selection, SelectionMismatch,
    SessionExpired, SimulatedPortal, WaitTimeout,
)


def make_dataset(stores=('s-7',), days=('2026-11-15',), per_day=5,
                 duplicate_image=False):
    region = 'east-1'
    dataset = {}
    for store in stores:
        for day in days:
            rows = []
            for idx in range(per_day):
                body = bytes([0x10 + idx]) * 16
                if duplicate_image and idx == 4:
                    body = bytes([0x10]) * 16  # 与 a-0 同字节
                rows.append({
                    'audit_id': f'{store}-{day}-a{idx}',
                    'media_id': f'{store}-m{idx}',
                    'image_url': f'https://portal.test/media/{store}-m{idx}.jpg',
                    'image': body,
                })
            dataset.setdefault(region, {}).setdefault(store, {})[day] = rows
    return region, dataset


def make_portal(*args, page_size=2, unavailable=(), **kwargs):
    region, dataset = make_dataset(*args, **kwargs)
    portal = SimulatedPortal(dataset, page_size=page_size, detail_load_polls=3)
    portal.unavailable_media = set(unavailable)
    return region, portal


def sel(region='east-1', store='s-7', day='2026-11-15'):
    return Selection(region, store, day)


class PortalCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ckpt = str(Path(self.tmp.name) / 'checkpoint.json')
        self.addCleanup(self.tmp.cleanup)

    def test_full_run_links_chain_and_receipt_verifies(self):
        region, portal = make_portal(unavailable=('s-7-m3',))
        result = PortalCollector(portal).run(sel(region), self.ckpt)

        self.assertFalse(result.resumed)
        receipt = result.receipt
        self.assertEqual(receipt['status'], 'complete')
        # 5 条：4 张交付、1 个明确缺口；3 个列表页（page_size=2）。
        self.assertEqual(receipt['progress']['pages_visited'], 3)
        self.assertEqual(receipt['progress']['delivered_count'], 4)
        self.assertEqual(receipt['progress']['gap_count'], 1)
        self.assertEqual(receipt['gaps'][0]['media_id'], 's-7-m3')

        # 每条交付都含 列表→详情→原图 三段响应引用且身份绑定。
        delivered = [i for i in receipt['items'] if i['image_response']]
        self.assertEqual(len(delivered), 4)
        for item in delivered:
            self.assertTrue(item['identity_binding']['bound'])
            self.assertEqual(item['detail_response']['status'], 200)
            self.assertEqual(item['image_response']['status'], 200)
            self.assertEqual(len(item['image_response']['body_sha256']), 64)

        verify = ledger.verify_receipt(receipt)
        self.assertTrue(verify['ok'], verify['problems'])
        self.assertTrue(receipt['integrity']['all_images_distinct'])

    def test_lazy_detail_uses_condition_wait_not_fixed_sleep(self):
        region, portal = make_portal(page_size=5)
        PortalCollector(portal).run(sel(region), self.ckpt)
        # 弹层每次都经过 wait_until 条件轮询；轮询次数等于加载步数。
        self.assertIn('wait_until', portal.calls)
        self.assertGreaterEqual(portal.calls.count('wait_until'), 5)

    def test_wait_timeout_when_detail_never_ready(self):
        region, portal = make_portal(page_size=5)
        # 加载步数极大且预算极小 -> 条件永不满足，必须超时而不是误判就绪。
        portal._detail_load_polls = 1_000_000
        collector = PortalCollector(portal, detail_timeout=0.0,
                                    detail_poll=0.0)
        with self.assertRaises(WaitTimeout):
            collector.run(sel(region), self.ckpt)

    def test_session_expiry_stops_at_explainable_position_and_resumes(self):
        region, portal = make_portal(page_size=2)

        # 第 2 张原图交付后让会话过期（仅一次）。
        original_fetch = portal.fetch_original

        def expire_after_two(detail):
            response = original_fetch(detail)
            if len(portal.served_media) >= 2:
                portal.fetch_original = original_fetch
                portal.expire_session()
            return response

        portal.fetch_original = expire_after_two
        with self.assertRaises(CollectionAborted) as ctx:
            PortalCollector(portal).run(sel(region), self.ckpt)
        stopped = ctx.exception.checkpoint
        self.assertEqual(stopped.status, cp.STATUS_STOPPED)
        self.assertEqual(len(stopped.delivered_media), 2)
        # 停在可解释位置：页号、句柄、当前条目都已落盘。
        on_disk = cp.load(self.ckpt)
        self.assertIsNotNone(on_disk.cursor)
        self.assertEqual(on_disk.status, cp.STATUS_STOPPED)
        self.assertIsNotNone(on_disk.stop_reason)

        # 过期后任何领取动作都被拒绝。
        with self.assertRaises(SessionExpired):
            portal.read_selections()

        # 重新登录后续取：先重确认筛选，再从句柄位置继续，不重复交付。
        portal.relogin()
        result = PortalCollector(portal).run(sel(region), self.ckpt)
        self.assertTrue(result.resumed)
        self.assertEqual(result.receipt['status'], 'complete')
        self.assertEqual(result.receipt['progress']['delivered_count'], 5)
        # 模拟门户对重复领取直接报错；能完成即证明前两张未重复交付。
        self.assertEqual(len(portal.served_media), 5)
        # 收据覆盖整个 run：5 个条目证据、3 个列表页响应引用全部累积保留。
        self.assertEqual(len(result.receipt['items']), 5)
        self.assertEqual(
            len(result.receipt['response_chain']['list_pages']), 3)
        # 检查点里也保留了完整脱敏证据链。
        persisted = cp.load(self.ckpt)
        self.assertEqual(len(persisted.item_records), 5)
        self.assertEqual(len(persisted.page_refs), 3)
        verify = ledger.verify_receipt(result.receipt)
        self.assertTrue(verify['ok'], verify['problems'])

    def test_resume_rejects_changed_filter_fingerprint(self):
        region, portal = make_portal(page_size=2)
        # 制造一次会话过期，留下可恢复检查点。
        original_fetch = portal.fetch_original

        def expire_after_one(detail):
            response = original_fetch(detail)
            portal.fetch_original = original_fetch
            portal.expire_session()
            return response

        portal.fetch_original = expire_after_one
        with self.assertRaises(CollectionAborted):
            PortalCollector(portal).run(sel(region), self.ckpt)
        portal.relogin()
        # 续取时换了门店 -> 指纹不同，必须拒绝复用旧位置。
        with self.assertRaises(SelectionMismatch):
            PortalCollector(portal).run(
                sel(region, store='other-store'), self.ckpt)

    def test_stale_cursor_is_rejected_after_filter_changes(self):
        region, portal = make_portal(stores=('s-7', 's-8'), page_size=2)
        first = PortalCollector(portal).run(sel(region, 's-7'), self.ckpt)
        old_cursor = first.checkpoint.cursor
        # 完整跑完 s-7；切换到 s-8 后旧句柄不得被按新筛选重新解释。
        portal.apply_selections(sel(region, 's-8'))
        with self.assertRaises(SelectionMismatch):
            portal.read_next_page(old_cursor)

    def test_image_identity_mismatch_refuses_to_attach(self):
        region, portal = make_portal(page_size=2)
        portal.apply_selections(sel(region))
        page = portal.read_first_page(sel(region))
        entry = page.entries[0]
        portal.open_detail(entry)
        portal.wait_until(CONDITION_DETAIL_READY, 5.0, 0.0)
        detail = portal.read_detail(entry)

        # 弹层被切走（模拟界面漂移/相邻门店串号）后，原图领取必须被拒绝。
        portal._modal = portal._modal.__class__(audit_id='foreign-audit',
                                                loaded=True)
        with self.assertRaises(SelectionMismatch):
            portal.fetch_original(detail)

    def test_unavailable_image_is_recorded_as_gap_not_silent(self):
        region, portal = make_portal(page_size=5, unavailable=('s-7-m1',))
        result = PortalCollector(portal).run(sel(region), self.ckpt)
        gap_media = {g['media_id'] for g in result.checkpoint.gaps}
        self.assertEqual(gap_media, {'s-7-m1'})
        # 有缺口时收据不得自称 complete。
        self.assertFalse(result.receipt['integrity']['complete'])
        gap_item = next(i for i in result.receipt['items']
                        if i['entry']['media_id'] == 's-7-m1')
        self.assertIsNone(gap_item['image_response'])

    def test_duplicate_content_is_not_delivered_twice(self):
        region, portal = make_portal(page_size=5, duplicate_image=True)
        result = PortalCollector(portal).run(sel(region), self.ckpt)
        # a-4 与 a-0 字节完全相同：内容级去重，只交付一次该内容。
        shas = [i['image_response']['body_sha256']
                for i in result.receipt['items'] if i['image_response']]
        self.assertEqual(len(shas), len(set(shas)))
        self.assertEqual(len(set(shas)), 4)

    def test_resuming_completed_checkpoint_is_rejected(self):
        region, portal = make_portal(page_size=5)
        PortalCollector(portal).run(sel(region), self.ckpt)
        with self.assertRaises(ValueError):
            PortalCollector(portal).run(sel(region), self.ckpt)

    def test_receipt_never_contains_credentials(self):
        region, portal = make_portal(page_size=5)
        # 给响应注入凭据头，收据必须统一脱敏。
        original_build = portal._build_page

        def with_auth(selection, page_index):
            page = original_build(selection, page_index)
            resp = page.list_response
            object.__setattr__(resp, 'headers',
                               resp.headers + (('Authorization', 'Bearer xyz'),
                                               ('Set-Cookie', 'sid=secret')))
            return page

        portal._build_page = with_auth
        result = PortalCollector(portal).run(sel(region), self.ckpt)
        raw = json.dumps(result.receipt, ensure_ascii=False)
        self.assertNotIn('Bearer xyz', raw)
        self.assertNotIn('sid=secret', raw)
        self.assertIn('***REDACTED***', raw)
        verify = ledger.verify_receipt(result.receipt)
        self.assertTrue(verify['ok'], verify['problems'])
        # 检查点同样不含凭据。
        self.assertNotIn('Bearer xyz', Path(self.ckpt).read_text('utf-8'))

    def test_unauthorized_region_is_refused(self):
        region, portal = make_portal()
        with self.assertRaises(SelectionMismatch):
            PortalCollector(portal).run(
                sel(region='region-not-authorized'), self.ckpt)

    def test_checkpoint_is_atomic_and_resumable_file(self):
        region, portal = make_portal(page_size=2)
        PortalCollector(portal).run(sel(region), self.ckpt)
        data = json.loads(Path(self.ckpt).read_text('utf-8'))
        self.assertEqual(data['version'], cp.CHECKPOINT_VERSION)
        self.assertEqual(data['selection']['store'], 's-7')
        # 没有遗留临时文件。
        self.assertEqual(
            list(Path(self.tmp.name).glob('*.tmp')), [])


class ConditionWaitSemanticsTest(unittest.TestCase):
    def test_polling_advances_lazy_state(self):
        region, portal = make_portal(page_size=5)
        portal._detail_load_polls = 4
        portal.apply_selections(sel(region))
        page = portal.read_first_page(sel(region))
        portal.open_detail(page.entries[0])
        self.assertFalse(portal.is_condition_met(CONDITION_DETAIL_READY))
        portal.wait_until(CONDITION_DETAIL_READY, 5.0, 0.0)
        self.assertTrue(portal.is_condition_met(CONDITION_DETAIL_READY))

    def test_unknown_condition_is_an_error(self):
        _, portal = make_portal()
        with self.assertRaises(Exception):
            portal.is_condition_met('not-a-real-condition')


if __name__ == '__main__':
    unittest.main()
