"""可恢复的浏览器采集编排。

编排层只通过 :class:`~app.portal_driver.BrowserPort` 与门户交互，所有就绪
判定都走条件等待（``wait_until``），不使用固定 sleep。核心保证：

1. 领取前以门户回显确认筛选身份；筛选指纹变化即中止，不让旧页码被重新解释。
2. 每条原图在"当前弹层条目身份"匹配时才关联，防止挂到相邻门店。
3. 会话过期时停在可解释的位置（页/条目），检查点原子落盘后向上抛出。
4. 续取先重确认筛选与条目身份；已交付 media 跳过，无法取得的图片记缺口。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from . import checkpoint as cp
from . import portal_ledger as ledger
from .portal_driver import (
    BrowserPort, CONDITION_DETAIL_READY, Detail, ImageUnavailable, ListEntry,
    Page, Selection, SelectionMismatch, SessionExpired,
)

# 详情弹层与翻页的条件等待预算；真实适配器中以秒计，模拟实现不实际睡眠。
DETAIL_WAIT_TIMEOUT = 15.0
DETAIL_POLL_INTERVAL = 0.25


class CollectionAborted(RuntimeError):
    """采集在可解释位置中止（如会话过期），检查点已可用于续取。"""

    def __init__(self, reason: str, checkpoint: cp.Checkpoint):
        super().__init__(reason)
        self.reason = reason
        self.checkpoint = checkpoint


@dataclass
class RunResult:
    receipt: dict
    checkpoint: cp.Checkpoint
    resumed: bool


class PortalCollector:
    def __init__(self, driver: BrowserPort, *,
                 detail_timeout: float = DETAIL_WAIT_TIMEOUT,
                 detail_poll: float = DETAIL_POLL_INTERVAL):
        self._driver = driver
        self._detail_timeout = detail_timeout
        self._detail_poll = detail_poll

    # -- 入口：新建或续取 ---------------------------------------------------
    def run(self, selection: Selection,
            checkpoint_path: str) -> RunResult:
        try:
            existing = cp.load(checkpoint_path)
            resumed = True
        except FileNotFoundError:
            existing = None
            resumed = False

        if existing is not None:
            checkpoint = self._verify_resume(existing, selection)
        else:
            checkpoint = cp.Checkpoint(
                run_id='run-' + uuid.uuid4().hex[:12],
                selection_fingerprint=selection.fingerprint(),
                selection={'region': selection.region, 'store': selection.store,
                           'day': selection.day})

        # 无论新建/续取，都先让门户实际应用筛选并回显确认。
        echoed = self._driver.apply_selections(selection)
        if echoed != selection:
            raise SelectionMismatch('门户回显筛选与请求不一致，拒绝采集')
        live = self._driver.read_selections()
        if live.fingerprint() != checkpoint.selection_fingerprint:
            raise SelectionMismatch(
                '续取筛选指纹与检查点不一致；筛选已变化，旧页码不得复用')

        cp.save_atomic(checkpoint_path, checkpoint)

        try:
            if resumed and checkpoint.cursor is not None:
                # 从中间继续：先凭旧句柄读取下一页，句柄携带筛选指纹，
                # 驱动会在筛选漂移时拒绝它。
                page = self._driver.read_next_page(checkpoint.cursor)
            else:
                page = self._driver.read_first_page(selection)
                checkpoint.cursor = None
                checkpoint.page_index = 0

            self._collect_pages(selection, checkpoint, checkpoint_path, page)
        except SessionExpired as exc:
            checkpoint.mark_stopped(str(exc))
            cp.save_atomic(checkpoint_path, checkpoint)
            raise CollectionAborted(str(exc), checkpoint) from exc

        checkpoint.mark_complete()
        cp.save_atomic(checkpoint_path, checkpoint)
        receipt = ledger.build_run_receipt(
            run_id=checkpoint.run_id, selection=selection,
            checkpoint=checkpoint, page_refs=checkpoint.page_refs,
            item_records=checkpoint.item_records, gaps=checkpoint.gaps)
        return RunResult(receipt, checkpoint, resumed)

    # -- 续取身份重确认 -----------------------------------------------------
    def _verify_resume(self, checkpoint: cp.Checkpoint,
                       selection: Selection) -> cp.Checkpoint:
        if checkpoint.status == cp.STATUS_COMPLETE:
            raise ValueError('该检查点已采集完成，无需续取')
        if selection.fingerprint() != checkpoint.selection_fingerprint:
            raise SelectionMismatch(
                '续取请求的筛选与检查点记录不同；拒绝在变化后的筛选上复用旧位置')
        expected = checkpoint.selection
        if (selection.region, selection.store, selection.day) != (
                expected['region'], expected['store'], expected['day']):
            raise SelectionMismatch('续取筛选字段与检查点不一致')
        return checkpoint

    # -- 翻页循环 -----------------------------------------------------------
    def _collect_pages(self, selection: Selection, checkpoint: cp.Checkpoint,
                       checkpoint_path: str, page: Page) -> None:
        visited_cursors: set[str] = set()
        guard = 0
        max_pages = 10_000
        while True:
            guard += 1
            if guard > max_pages:
                raise RuntimeError('翻页保护上限触发，疑似句柄循环')

            # 每页再次确认：页面数据属于当前筛选。
            if page.selection.fingerprint() != selection.fingerprint():
                raise SelectionMismatch('列表页不属于当前筛选，拒绝解释页码')
            checkpoint.page_index = self._page_index_of(page, checkpoint)
            checkpoint.cursor = page.cursor
            checkpoint.pages_visited += 1
            # 脱敏后的列表响应引用随检查点持久化，续取不丢证据。
            checkpoint.page_refs.append(
                ledger.response_ref(page.list_response))
            cp.save_atomic(checkpoint_path, checkpoint)

            for entry in page.entries:
                self._collect_entry(selection, entry, checkpoint,
                                    checkpoint_path)

            if page.cursor is None:
                return  # 末页：没有下一页句柄才算遍历完成
            if page.cursor in visited_cursors:
                raise SelectionMismatch('翻页句柄重复，拒绝循环读取')
            visited_cursors.add(page.cursor)
            page = self._driver.read_next_page(page.cursor)

    @staticmethod
    def _page_index_of(page: Page, checkpoint: cp.Checkpoint) -> int:
        # 列表响应体中带 page_index（模拟实现写入）；真实适配器可从控件读取。
        try:
            payload = json.loads(page.list_response.body)
            return int(payload.get('page_index', checkpoint.page_index))
        except Exception:
            return checkpoint.page_index

    # -- 单条目：弹层身份确认 + 原图领取 ------------------------------------
    def _collect_entry(self, selection: Selection, entry: ListEntry,
                       checkpoint: cp.Checkpoint,
                       checkpoint_path: str) -> None:
        checkpoint.entries_seen += 1
        checkpoint.current_audit_id = entry.audit_id
        cp.save_atomic(checkpoint_path, checkpoint)

        # 条目身份必须落在当前门店/日期筛选内。
        if entry.store != selection.store or entry.day != selection.day:
            checkpoint.mark_gap(entry.media_id, entry.audit_id,
                                '条目不属于当前门店/日期筛选，拒绝关联')
            cp.save_atomic(checkpoint_path, checkpoint)
            return

        # 已交付（按 media_id 或内容摘要）直接跳过，绝不重复领取/交付。
        if checkpoint.is_delivered(entry.media_id):
            cp.save_atomic(checkpoint_path, checkpoint)
            return

        self._driver.open_detail(entry)
        # 条件等待弹层就绪：由驱动轮询门户真实状态判定"弹层已加载"，
        # 而非固定等待时间。read_detail 还会再次校验条目身份。
        self._driver.wait_until(
            CONDITION_DETAIL_READY, self._detail_timeout, self._detail_poll)
        detail = self._driver.read_detail(entry)
        self._assert_identity(entry, detail)

        try:
            image_response = self._driver.fetch_original(detail)
        except ImageUnavailable as exc:
            # 无法取得的图片明确留缺口，采集继续。
            checkpoint.mark_gap(detail.media_id, entry.audit_id, str(exc))
            record = ledger.item_record(entry, detail, None)
            checkpoint.item_records.append(record)
            cp.save_atomic(checkpoint_path, checkpoint)
            return

        # 内容级去重兜底：同字节原图即使 media_id 不同也不重复交付。
        if checkpoint.is_delivered(detail.media_id, image_response.sha256):
            cp.save_atomic(checkpoint_path, checkpoint)
            return

        checkpoint.mark_delivered(detail.media_id, image_response.sha256)
        checkpoint.item_records.append(
            ledger.item_record(entry, detail, image_response))
        cp.save_atomic(checkpoint_path, checkpoint)

    @staticmethod
    def _assert_identity(entry: ListEntry, detail: Detail) -> None:
        if detail.audit_id != entry.audit_id:
            raise SelectionMismatch('详情 audit_id 与列表条目不一致')
        if detail.media_id != entry.media_id:
            raise SelectionMismatch('详情 media_id 与列表条目不一致')
